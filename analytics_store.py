"""PostgreSQL ledger; a per-order advisory lock covers creation and all state writers."""
import copy
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import timedelta

import requests
from psycopg2.extras import Json, RealDictCursor
from psycopg2 import sql

from db import get_conn
from order_analytics import build_record, event_payload, money, normalize_status, now, stamp, transition
from order_analytics import attribution

JSON_FIELDS = {'items', 'attribution', 'shipments', 'refund_details'}


@contextmanager
def transaction():
    conn = get_conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
    finally:
        conn.close()


def locked_order(cur, order_id):
    cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('analytics:' + str(order_id),))
    cur.execute('SELECT * FROM order_analytics WHERE shopify_order_id=%s FOR UPDATE', (str(order_id),))
    row = cur.fetchone()
    return dict(row) if row else None


def queue_event(cur, record, name, version, value=None, items=None, at=None):
    if record['is_test']:
        return
    payload = event_payload(record, name, value, items)
    if not payload:
        return
    occurred = stamp(at or record['status_changed_at'])
    if occurred < now() - timedelta(hours=71) or occurred > now() + timedelta(minutes=5):
        return
    payload['timestamp_micros'] = int(occurred.timestamp() * 1000000)
    cur.execute('''INSERT INTO analytics_event_outbox
        (shopify_order_id, event_name, final_status_version, payload)
        VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
        (record['shopify_order_id'], name, version, Json(payload)))


def save(cur, record, old, source, event_id=None, emit=True, at=None):
    changed = transition(record, old, at)
    record['last_source'] = source
    record['updated_at'] = now()
    keys = list(record)
    query = sql.SQL('INSERT INTO order_analytics ({}) VALUES ({}) ON CONFLICT (shopify_order_id) DO UPDATE SET {}').format(
        sql.SQL(',').join(map(sql.Identifier, keys)), sql.SQL(',').join(sql.Placeholder() for _ in keys),
        sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(k), sql.Identifier(k)) for k in keys if k != 'shopify_order_id'))
    cur.execute(query, [Json(record[k]) if k in JSON_FIELDS else record[k] for k in keys])
    if changed:
        cur.execute('''INSERT INTO order_status_transitions
          (shopify_order_id,previous_status,new_status,changed_at,source,source_event_id,raw_status)
          VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
          (record['shopify_order_id'], old['normalized_status'] if old else None, record['normalized_status'],
           record['status_changed_at'], source, event_id, record['raw_courier_status']))
    for refund in record['refund_details']:
        cur.execute('''INSERT INTO order_refunds (shopify_order_id,refund_id,created_at,currency,value,items)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (shopify_order_id,refund_id) DO UPDATE
            SET value=EXCLUDED.value,items=EXCLUDED.items''', (record['shopify_order_id'], refund['refund_id'],
            refund['created_at'], refund['currency'], refund['value'], Json(refund['items'])))
    if not emit:
        return record
    if changed and record['normalized_status'] in ('Delivered', 'Cancelled'):
        name = 'order_' + record['normalized_status'].lower()
        queue_event(cur, record, name, record['final_status_version'])
    if record['purchase_recorded']:
        cur.execute("SELECT payload FROM analytics_event_outbox WHERE shopify_order_id=%s AND event_name='refund'", (record['shopify_order_id'],))
        queued = cur.fetchall()
        refunded = sum((money(e['payload']['events'][0]['params']['value']) for e in queued), money(0))
        quantities = {}
        for e in queued:
            for i in e['payload']['events'][0]['params']['items']:
                quantities[i['item_id']] = quantities.get(i['item_id'], 0) + i['quantity']
        previous_ids = {r['refund_id'] for r in (old or {}).get('refund_details', []) if money(r['value']) > 0} if (old or {}).get('purchase_recorded') else set()
        for index, refund in enumerate(record['refund_details'], 1):
            if refund['refund_id'] in previous_ids:
                continue
            value = min(money(refund['value']), max(money(0), record['order_value'] - refunded))
            if value:
                queue_event(cur, record, 'refund', -index, value, refund['items'], refund['created_at'])
                refunded += value
                for i in refund['items']:
                    quantities[i['item_id']] = quantities.get(i['item_id'], 0) + i['quantity']
        if (changed or not (old or {}).get('purchase_recorded')) and record['normalized_status'] == 'Cancelled':
            remaining = max(money(0), record['order_value'] - refunded)
            items = []
            for i in record['items']:
                i = dict(i)
                consumed = min(i['quantity'], quantities.get(i['item_id'], 0))
                quantities[i['item_id']] = quantities.get(i['item_id'], 0) - consumed
                i['quantity'] -= consumed
                if i['quantity']:
                    items.append(i)
            if remaining:
                queue_event(cur, record, 'refund', record['final_status_version'], remaining, items)
    return record


def sync_order(payload, event_id=None, topic='orders/updated', source='shopify', emit=True):
    with transaction() as cur:
        old = locked_order(cur, payload['id'])
        if event_id:
            cur.execute('''INSERT INTO webhook_receipts (source,event_id,topic) VALUES (%s,%s,%s)
                ON CONFLICT DO NOTHING RETURNING event_id''', (source, event_id, topic))
            if not cur.fetchone():
                return old
        if old and stamp(payload.get('updated_at') or payload['created_at']) < stamp(old['source_updated_at']):
            return old
        record = build_record(payload, old)
        return save(cur, record, old, source, event_id, emit, stamp(payload.get('updated_at') or payload['created_at']))


def sync_tracking(number, summary, observed_at=None, emit=True):
    number = str(number).upper().strip()
    observed = stamp(observed_at) if observed_at else now()
    with transaction() as cur:
        cur.execute('SELECT shopify_order_id FROM order_analytics WHERE shipments ? %s ORDER BY shopify_order_id', (number,))
        ids = [r['shopify_order_id'] for r in cur.fetchall()]
        for order_id in ids:
            old = locked_order(cur, order_id)
            record = copy.deepcopy(old)
            shipment = record['shipments'][number]
            if shipment.get('observed_at') and stamp(shipment['observed_at']) >= observed:
                continue
            shipment.update(status=str(summary['status']), observed_at=observed.isoformat())
            record['raw_courier_status'] = '; '.join(s['status'] for s in record['shipments'].values())
            record['normalized_status'] = normalize_status(record['raw_shopify_status'] == 'cancelled', record['shipments'])
            save(cur, record, old, 'courier', emit=emit, at=observed)


def purchase_evidence(transaction_ids):
    """Only GA4 purchase report evidence can mark an existing purchase, never a cart attribute."""
    for tid in transaction_ids:
        with transaction() as cur:
            cur.execute('''SELECT shopify_order_id FROM order_analytics WHERE
                transaction_id=%s OR shopify_order_id=%s OR order_number=%s''', (tid,tid,tid))
            candidates = cur.fetchall()
            if len(candidates) != 1:
                continue
            old = locked_order(cur, candidates[0]['shopify_order_id'])
            if old['purchase_recorded']:
                continue
            record = copy.deepcopy(old)
            record.update(purchase_recorded=True, transaction_id=tid)
            save(cur, record, old, 'ga4_purchase_evidence')


def dispatch_events(validate_only=False, limit=100):
    measurement = os.environ['GA4_MEASUREMENT_ID']
    secret = os.environ['GA4_API_SECRET']
    results = dict(sent=0, validated=0, invalid=0, uncertain=0, expired=0)
    for _ in range(limit):
        # Commit an attempt BEFORE networking. Unknown outcomes require reconciliation,
        # because Measurement Protocol provides no general exactly-once delivery contract.
        with transaction() as cur:
            cur.execute("""SELECT * FROM analytics_event_outbox WHERE dispatch_state='pending'
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""")
            event = cur.fetchone()
            if not event:
                break
            if event['payload']['timestamp_micros'] < int((now() - timedelta(hours=71)).timestamp() * 1000000):
                cur.execute("UPDATE analytics_event_outbox SET dispatch_state='expired' WHERE id=%s", (event['id'],))
                results['expired'] += 1
                continue
            cur.execute("UPDATE analytics_event_outbox SET dispatch_state='attempting', attempted_at=NOW(), attempts=attempts+1 WHERE id=%s", (event['id'],))
        state, error = 'uncertain', None
        try:
            payload = dict(event['payload'], validation_behavior='ENFORCE_RECOMMENDATIONS')
            response = requests.post('https://www.google-analytics.com/debug/mp/collect',
                                     params=dict(measurement_id=measurement, api_secret=secret), json=payload, timeout=20)
            response.raise_for_status()
            if response.json().get('validationMessages'):
                state, error = 'invalid', 'Measurement Protocol validation rejected payload'
                results['invalid'] += 1
            elif validate_only:
                state = 'validated'
                results['validated'] += 1
            else:
                response = requests.post('https://www.google-analytics.com/mp/collect',
                                         params=dict(measurement_id=measurement, api_secret=secret), json=event['payload'], timeout=20)
                if 200 <= response.status_code < 300:
                    state = 'sent'
                    results['sent'] += 1
                else:
                    error = 'Collection endpoint returned HTTP ' + str(response.status_code)
        except (requests.RequestException, ValueError):
            error = 'Transport or response failure; inspect outcome before retrying'
        if state == 'uncertain':
            results['uncertain'] += 1
        with transaction() as cur:
            cur.execute('''UPDATE analytics_event_outbox SET dispatch_state=%s,last_error=%s,
                sent_at=CASE WHEN %s='sent' THEN NOW() ELSE sent_at END WHERE id=%s''', (state,error,state,event['id']))
    return results


def load_orders(start, end):
    with transaction() as cur:
        cur.execute('SELECT * FROM order_analytics WHERE created_at >= %s AND created_at < %s AND NOT is_test ORDER BY created_at DESC', (start, end))
        return [dict(row) for row in cur.fetchall()]


def save_report(source, date, kind, rows):
    with transaction() as cur:
        cur.execute('''INSERT INTO analytics_reports (source,report_date,report_kind,rows) VALUES (%s,%s,%s,%s)
            ON CONFLICT (source,report_date,report_kind) DO UPDATE SET rows=EXCLUDED.rows,fetched_at=NOW()''', (source,date,kind,Json(rows)))


def resolve_google_clicks(clicks):
    """Match exact captured GCLIDs; no probabilistic or name-based attribution."""
    for click in clicks:
        with transaction() as cur:
            cur.execute("SELECT shopify_order_id FROM order_analytics WHERE attribution->>'gclid'=%s ORDER BY shopify_order_id", (click['gclid'],))
            ids = [r['shopify_order_id'] for r in cur.fetchall()]
            for oid in ids:
                record = locked_order(cur,oid)
                attr = record['attribution']
                if attr.get('campaign_id'):
                    continue
                attr.update(channel='google',campaign_id=click['campaign_id'],google_campaign_id=click['campaign_id'],
                            group_id=click['group_id'],google_adgroup_id=click['group_id'],evidence='exact_gclid')
                cur.execute('UPDATE order_analytics SET attribution=%s WHERE shopify_order_id=%s', (Json(attr),oid))


def save_journey(order_id, journey):
    with transaction() as cur:
        record = locked_order(cur,order_id)
        if not record:
            return
        captured = record['attribution']
        def visit_fields(visit):
            visit=visit or {}
            fields={'utm_'+k:v for k,v in (visit.get('utmParameters') or {}).items() if v}
            fields.update(landing_page=visit.get('landingPage'),referring_url=visit.get('referrerUrl'))
            return {k:v for k,v in fields.items() if v}
        fallback = visit_fields(journey.get('lastVisit') or journey.get('firstVisit'))
        attrs=[dict(name=k,value=v) for k,v in fallback.items() if not captured.get(k)]
        updated = attribution({'note_attributes':attrs},captured)
        updated.setdefault('first_touch',visit_fields(journey.get('firstVisit')))
        updated['journey_checked']=True
        cur.execute('UPDATE order_analytics SET attribution=%s WHERE shopify_order_id=%s', (Json(updated),str(order_id)))


def load_reports(start, end):
    with transaction() as cur:
        cur.execute('SELECT * FROM analytics_reports WHERE report_date >= %s AND report_date <= %s', (start,end))
        return [dict(row) for row in cur.fetchall()]
