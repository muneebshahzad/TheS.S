"""Current order state and privacy-limited GA4 events. No network access on import."""
import copy
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qs, urlsplit

STATUSES = ('Delivered', 'Cancelled', 'In process')
ATTRIBUTION_KEYS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
                    'landing_page', 'referring_url', 'gclid', 'gbraid', 'wbraid', 'fbclid',
                    'client_id', 'session_id', 'meta_campaign_id', 'meta_adset_id', 'meta_ad_id',
                    'google_campaign_id', 'google_adgroup_id', 'analytics_consent')


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def courier_state(raw):
    value = re.sub(r'[_\s-]+', ' ', str(raw or '').upper()).strip()
    # Some couriers append a reason after the authoritative status.
    if value == 'DELIVERED' or value.startswith('DELIVERED '):
        return 'Delivered'
    finals = ('RETURNED TO SHIPPER', 'RETURNED TO SENDER', 'RETURN SUBMITTED',
              'RETURN COMPLETED', 'RETURN DELIVERED', 'RTO DELIVERED', 'RTO COMPLETED',
              'CANCELLED', 'CANCELED', 'RETURNED')
    if any(value == s or value.startswith(s + ' ') for s in finals):
        return 'Cancelled'
    return 'In process'


def normalize_status(cancelled_at=None, shipments=None, raw_courier_status=''):
    # Explicit Shopify cancellation takes precedence over stale courier delivery.
    if cancelled_at:
        return 'Cancelled'
    states = [courier_state(s.get('status')) for s in (shipments or {}).values()]
    if not states:
        return courier_state(raw_courier_status)
    if all(s == 'Delivered' for s in states):
        return 'Delivered'
    if all(s == 'Cancelled' for s in states):
        return 'Cancelled'
    return 'In process'  # Mixed/partial shipments do not imply full delivery.


def attribution(order, previous=None):
    result = copy.deepcopy(previous or {})
    attrs = {str(a.get('name')): str(a.get('value') or '')[:2048]
             for a in order.get('note_attributes', [])}
    for key in ATTRIBUTION_KEYS:
        value = attrs.get('ss_' + key) or attrs.get(key)
        if value:
            result[key] = value
        first = attrs.get('ss_first_' + key)
        if first:
            result.setdefault('first_touch', {}).setdefault(key, first)
    for key, field in [('landing_page', 'landing_site'), ('referring_url', 'referring_site')]:
        if order.get(field):
            result.setdefault(key, str(order[field])[:2048])
    query = parse_qs(urlsplit(result.get('landing_page', '')).query)
    for key in ATTRIBUTION_KEYS:
        if query.get(key) and not result.get(key):
            result[key] = query[key][0][:2048]
    source = result.get('utm_source', '').lower()
    medium = result.get('utm_medium', '').lower()
    meta = source in ('facebook', 'facebook.com', 'm.facebook.com', 'instagram', 'instagram.com') and medium in ('paid', 'paid_social', 'cpc')
    google = source == 'google' and medium in ('cpc', 'ppc', 'paid', 'paid_search')
    if result.get('meta_campaign_id') or meta:
        result['channel'] = 'meta'
        result['campaign_id'] = result.get('meta_campaign_id') or result.get('utm_campaign', '')
        result['group_id'] = result.get('meta_adset_id') or result.get('utm_term', '')
        result['ad_id'] = result.get('meta_ad_id') or result.get('utm_content', '')
    elif result.get('google_campaign_id') or google or result.get('gclid') or result.get('gbraid') or result.get('wbraid'):
        result['channel'] = 'google'
        result['campaign_id'] = result.get('google_campaign_id', '')
        if not result['campaign_id'] and result.get('utm_campaign', '').isdigit():
            result['campaign_id'] = result['utm_campaign']
        result['group_id'] = result.get('google_adgroup_id', '')
        result['ad_id'] = ''
    else:
        result['channel'] = source or 'Unattributed'
        result['campaign_id'] = result.get('utm_campaign', '')
        result['group_id'] = result['ad_id'] = ''
    # Names and historical free-text UTMs remain evidence, never join them to ad IDs.
    for key in ('campaign_id', 'group_id', 'ad_id'):
        if not result.get(key, '').isdigit():
            result[key] = ''
    return result


def line_items(order):
    result = []
    for item in order.get('line_items', []):
        qty = int(item.get('quantity') or 0)
        allocations = sum((money(a.get('amount')) for a in item.get('discount_allocations', [])), Decimal(0))
        total = max(Decimal(0), money(item.get('price')) * qty - allocations)
        result.append(dict(line_item_id=str(item['id']), item_id=str(item.get('product_id') or item['id']),
                           item_name=str(item.get('title') or ''), sku=str(item.get('sku') or ''),
                           variant_id=str(item.get('variant_id') or ''), item_variant=str(item.get('variant_title') or ''),
                           quantity=qty, price=float(total / qty) if qty else 0, value=float(total),
                           catalog_item=bool(item.get('product_id'))))
    return result


def refund_record(refund, currency):
    items = []
    for r in refund.get('refund_line_items', []):
        item, qty = r.get('line_item', {}), int(r.get('quantity') or 0)
        subtotal = money(r.get('subtotal'))
        items.append(dict(line_item_id=str(r.get('line_item_id') or item.get('id')),
                          item_id=str(item.get('product_id') or item.get('id') or r.get('line_item_id')),
                          item_name=str(item.get('title') or ''), quantity=qty,
                          price=float(subtotal / qty) if qty else 0, value=float(subtotal),
                          catalog_item=bool(item.get('product_id'))))
    transactions = [entry for entry in refund.get('transactions', []) if entry.get('kind') == 'refund']
    txs = [entry for entry in transactions if entry.get('status') == 'success']
    # Successful transactions capture tax/shipping and custom refunds. COD returns can lack them.
    value = sum((money(t.get('amount')) for t in txs), Decimal(0)) if transactions else sum((money(r.get('subtotal')) + money(r.get('total_tax')) for r in refund.get('refund_line_items', [])), Decimal(0))
    if not transactions:
        value += sum((money(s.get('subtotal_amount', {}).get('shop_money', {}).get('amount')) + money(s.get('tax_amount', {}).get('shop_money', {}).get('amount')) for s in refund.get('refund_shipping_lines', [])), Decimal(0))
    if transactions and not txs:
        items = []
    return dict(refund_id=str(refund['id']), created_at=refund.get('created_at') or now().isoformat(),
                currency=currency, value=float(max(value, 0)), items=items)


def build_record(order, old=None):
    old = old or {}
    currency = str(order.get('currency') or old.get('currency') or 'PKR')
    shipments = copy.deepcopy(old.get('shipments') or {})
    active = set()
    for f in order.get('fulfillments', []):
        if f.get('status') in ('cancelled', 'failure', 'error'):
            continue
        numbers = f.get('tracking_numbers') or [f.get('tracking_number')]
        for number in filter(None, numbers):
            number = str(number).upper()
            active.add(number)
            shipments.setdefault(number, dict(status='Booked', observed_at=None))
            shipments[number]['courier'] = f.get('tracking_company') or ''
    if order.get('fulfillment_status') not in ('fulfilled',) and active:
        active.add('__unfulfilled__')
        shipments['__unfulfilled__'] = dict(status='Un-booked', observed_at=None, courier='')
    shipments = {k: v for k, v in shipments.items() if k in active}
    raw = '; '.join(s['status'] for s in shipments.values()) or 'Un-booked'
    record = dict(shopify_order_id=str(order['id']), order_number=str(order.get('name') or order.get('order_number') or order['id']),
                  transaction_id=str(old.get('transaction_id') or order.get('name') or order['id']),
                  created_at=stamp(order['created_at']), updated_at=now(), source_updated_at=stamp(order.get('updated_at') or order['created_at']),
                  raw_shopify_status='cancelled' if order.get('cancelled_at') else ('closed' if order.get('closed_at') else 'open'),
                  raw_financial_status=order.get('financial_status'), raw_fulfillment_status=order.get('fulfillment_status'),
                  raw_courier_status=raw, cancelled_at=order.get('cancelled_at'), cancellation_reason=order.get('cancel_reason'),
                  currency=currency, order_value=money(order.get('total_price')), subtotal=money(order.get('subtotal_price')),
                  current_order_value=money(order['current_total_price']) if order.get('current_total_price') is not None else None,
                  discounts=money(order.get('total_discounts')), shipping=money(order.get('total_shipping_price_set', {}).get('shop_money', {}).get('amount')),
                  items=line_items(order), payment_method=', '.join(order.get('payment_gateway_names') or []),
                  courier=', '.join(sorted(set(s.get('courier', '') for s in shipments.values()) - {''})),
                  tracking_number=', '.join(k for k in shipments if k != '__unfulfilled__'), shipments=shipments,
                  attribution=attribution(order, old.get('attribution')), purchase_recorded=bool(old.get('purchase_recorded')),
                  is_test=bool(order.get('test')), last_source='shopify',
                  final_status_version=int(old.get('final_status_version') or 0),
                  status_changed_at=old.get('status_changed_at') or now(), delivered_at=old.get('delivered_at'))
    refunds = {r['refund_id']: r for r in old.get('refund_details', [])}
    refunds.update({str(r['id']): refund_record(r, currency) for r in order.get('refunds', [])})
    record['refund_details'] = list(refunds.values())
    record['refunded_value'] = min(record['order_value'], sum((money(r['value']) for r in refunds.values()), Decimal(0)))
    record['normalized_status'] = normalize_status(record['cancelled_at'], shipments, raw)
    if record['normalized_status'] == 'Cancelled' and not record['cancelled_at']:
        record['cancelled_at'] = old.get('cancelled_at')
    return record


def transition(record, old, at=None):
    if old and old['normalized_status'] == record['normalized_status']:
        return False
    record['status_changed_at'] = at or now()
    if record['normalized_status'] in ('Delivered', 'Cancelled'):
        record['final_status_version'] += 1
    if record['normalized_status'] == 'Delivered':
        record['delivered_at'] = record['status_changed_at']
    elif record['normalized_status'] == 'Cancelled':
        record['cancelled_at'] = record.get('cancelled_at') or record['status_changed_at']
    return True


def safe_name(value):
    value = str(value or '')[:100]
    return '' if re.search(r'@|\b\d[\d\s+()-]{7,}\d\b|https?://', value) else value


def event_items(items):
    return [dict(item_id=str(i['item_id']), item_name=safe_name(i['item_name']) if i.get('catalog_item') else '',
                 price=float(i['price']), quantity=int(i['quantity'])) for i in items if i['quantity'] > 0]


def event_payload(record, name, value=None, items=None):
    attr = record['attribution']
    # Require the actual GA identifier and recorded analytics consent; never substitute PII.
    if attr.get('analytics_consent') != 'granted' or not re.fullmatch(r'\d+\.\d+', attr.get('client_id', '')):
        return None
    if not re.fullmatch(r'[A-Za-z0-9#_-]{1,100}',record['transaction_id']) or not re.fullmatch(r'[A-Z]{3}',record['currency']):
        return None
    if name == 'order_delivered' and value is None:
        value = net_order_value(record)
        refunded_quantities = {}
        for refund in record['refund_details']:
            for item in refund['items']:
                key = item['line_item_id']
                refunded_quantities[key] = refunded_quantities.get(key, 0) + item['quantity']
        items = [dict(item, quantity=max(0, item['quantity'] - refunded_quantities.get(item['line_item_id'], 0))) for item in record['items']]
    params = dict(transaction_id=record['transaction_id'], value=float(record['order_value'] if value is None else value),
                  currency=record['currency'], items=event_items(record['items'] if items is None else items),
                  order_status=record['normalized_status'].lower(),
                  courier=next((c for c in ('Leopards', 'Call Courier', 'PostEx', 'TCS', 'Trax') if c.lower() in record['courier'].lower()), 'other'),
                  payment_method='cod' if any(s in record['payment_method'].lower() for s in ('cash', 'cod')) else 'other')
    if name == 'order_cancelled':
        params['cancellation_reason'] = record.get('cancellation_reason') if record.get('cancellation_reason') in ('customer', 'fraud', 'inventory', 'declined', 'other', 'staff') else 'returned_or_cancelled'
    for key in ('campaign_id', 'group_id', 'ad_id'):
        if attr.get(key, '').isdigit():
            params[key] = attr[key]
    # Late fulfilment must not fabricate a new visit or attach to an expired session.
    return dict(client_id=attr['client_id'], timestamp_micros=int(stamp(record['status_changed_at']).timestamp() * 1000000),
                events=[dict(name=name, params=params)])


def net_order_value(record):
    value = max(money(0), money(record['order_value']) - money(record['refunded_value']))
    current = record.get('current_order_value')
    return min(value, max(money(0), money(current))) if current is not None else value
