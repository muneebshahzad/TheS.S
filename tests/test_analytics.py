import copy
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock
import pytest
from order_analytics import build_record, courier_state, event_payload, now, refund_record, transition
from analytics_store import sync_order, sync_tracking, purchase_evidence, dispatch_events
from analytics_reporting import report, date_range, order_metrics, product_identity


@pytest.mark.parametrize('raw,status', [('Un-booked','In process'),('Booked','In process'),('Out for Delivery','In process'),
    ('Need Attention','In process'),('Pending','In process'),('Processing','In process'),('Fulfilled','In process'),
    ('Being Return','In process'),('Return Submission','In process'),('Out for Return','In process'),
    ('Returned to Shipper','Cancelled'),('RETURN SUBMITTED','Cancelled'),('Returned','Cancelled'),
    ('Returned to sender - received','Cancelled'),('Delivered','Delivered'),('Delivered - OK','Delivered'),('Undelivered','In process')])
def test_status_classification(raw,status):
    assert courier_state(raw) == status


def test_new_order(order):
    assert build_record(order)['normalized_status'] == 'In process'


def counts(database):
    with database() as cur:
        cur.execute('SELECT normalized_status,count(*) AS n FROM order_analytics GROUP BY normalized_status')
        return {r['normalized_status']:r['n'] for r in cur.fetchall()}


def events(database):
    with database() as cur:
        cur.execute('SELECT event_name,payload,dispatch_state FROM analytics_event_outbox ORDER BY id')
        return list(cur.fetchall())


def test_delivered_leaves_process_and_replay_once(database,order):
    sync_order(order,event_id='create')
    sync_tracking('TRACK101',{'status':'Delivered'})
    sync_tracking('TRACK101',{'status':'Delivered'})
    sync_order(order,event_id='create')
    sync_order(order,event_id='repeat')
    assert counts(database) == {'Delivered':1}
    assert [r['event_name'] for r in events(database)] == ['order_delivered']
    with database() as cur:
        cur.execute('SELECT count(*) AS n FROM order_status_transitions')
        assert cur.fetchone()['n'] == 2


def test_cancelled_and_refund_once(database,order):
    sync_order(order)
    purchase_evidence(['PK101A01'])
    sync_tracking('TRACK101',{'status':'Being Return'})
    assert counts(database) == {'In process':1}
    sync_tracking('TRACK101',{'status':'Returned to Shipper'})
    sync_tracking('TRACK101',{'status':'Returned to Shipper'})
    assert counts(database) == {'Cancelled':1}
    names = [r['event_name'] for r in events(database)]
    assert names == ['order_cancelled','refund']
    assert events(database)[1]['payload']['events'][0]['params']['value'] == 2200


def test_shopify_cancelled_wins(database,order):
    sync_order(order)
    sync_tracking('TRACK101',{'status':'Delivered'})
    order['cancelled_at'] = now().isoformat()
    order['updated_at'] = now().isoformat()
    sync_order(order,event_id='cancel')
    sync_order(order,event_id='cancel')
    assert counts(database)=={'Cancelled':1}


def test_unknown_purchase_does_not_refund(database,order):
    sync_order(order)
    sync_tracking('TRACK101',{'status':'Returned'})
    assert [r['event_name'] for r in events(database)] == ['order_cancelled']


def test_late_purchase_evidence_uses_original_transaction(database,order):
    sync_order(order)
    sync_tracking('TRACK101',{'status':'Returned'})
    purchase_evidence(['101'])
    purchase_evidence(['101'])
    assert [r['event_name'] for r in events(database)] == ['order_cancelled','refund']
    assert events(database)[1]['payload']['events'][0]['params']['transaction_id']=='101'


def make_refund(order):
    return dict(id=991,created_at=now().isoformat(),refund_line_items=[dict(line_item_id=1001,line_item=order['line_items'][0],quantity=1,subtotal='1000',total_tax='0')],
                transactions=[dict(kind='refund',status='success',amount='1000')])


def test_pending_refund_then_success_is_counted_once(database, order):
    sync_order(order)
    purchase_evidence(['PK101A01'])
    refund = make_refund(order)
    refund['transactions'][0]['status'] = 'pending'
    order['refunds'] = [refund]
    order['updated_at'] = now().isoformat()
    record = sync_order(order)
    assert record['refunded_value'] == 0
    assert events(database) == []
    refund['transactions'][0]['status'] = 'success'
    order['updated_at'] = now().isoformat()
    record = sync_order(order)
    sync_order(order)
    assert record['refunded_value'] == 1000
    assert [entry['event_name'] for entry in events(database)] == ['refund']


def test_partial_refund_before_delivery_uses_remaining_value_and_items(order):
    order['refunds'] = [make_refund(order)]
    order['current_total_price'] = '1200'
    record = build_record(order)
    record['normalized_status'] = 'Delivered'
    payload = event_payload(record, 'order_delivered')['events'][0]['params']
    assert payload['value'] == 1200
    assert payload['items'][0]['quantity'] == 1
    assert order_metrics([record])['delivered_revenue'] == 1200


def test_zero_current_order_value_is_preserved(order):
    order['current_total_price'] = '0'
    record = build_record(order)
    record['normalized_status'] = 'Delivered'
    assert order_metrics([record])['delivered_revenue'] == 0


def test_cancelled_cod_retains_ad_cost_without_claiming_cash_refund(order):
    record = build_record(order)
    record['normalized_status'] = 'Cancelled'
    day = now().date()
    snapshots = [dict(source=source, report_kind=kind, report_date=day, fetched_at=now(), rows=[])
                 for source, kind in [('meta', 'ads'), ('google', 'ads'), ('ga4', 'events'),
                                      ('ga4', 'products'), ('ga4', 'sessions')]]
    snapshots[0]['rows'] = [dict(channel='meta', campaign_id='123', campaign_name='Campaign',
                                group_id='456', group_name='Group', ad_id='789', ad_name='Ad',
                                spend=300, currency='PKR', impressions=1000, clicks=20)]
    result = report([record], snapshots, {}, day, day)
    assert result['kpis']['cancelled'] == 1
    assert result['kpis']['delivered_revenue'] == 0
    assert result['kpis']['spend'] == 300
    assert result['kpis']['delivered_roas'] == 0
    assert result['orders'][0]['refunded_value'] == 0


def test_partial_refund_then_cancellation_no_double_refund(database,order):
    sync_order(order)
    purchase_evidence(['PK101A01'])
    sync_tracking('TRACK101',{'status':'Delivered'})
    order['refunds'] = [make_refund(order)]
    order['updated_at'] = now().isoformat()
    record = sync_order(order,event_id='partial')
    sync_order(order,event_id='partial')
    assert record['refunded_value'] == Decimal('1000.00')
    assert order_metrics([record])['delivered_revenue'] == 1200
    assert order_metrics([record],product='501')['delivered_revenue'] == 1000
    order['cancelled_at']=now().isoformat()
    order['updated_at']=now().isoformat()
    sync_order(order,event_id='cancel')
    refunds=[r['payload']['events'][0]['params'] for r in events(database) if r['event_name']=='refund']
    assert [r['value'] for r in refunds] == [1000,1200]
    assert [r['items'][0]['quantity'] for r in refunds] == [1,1]


def test_attribution_unavailable_and_preserved(order):
    order.pop('note_attributes')
    assert build_record(order)['attribution']['channel']=='Unattributed'
    order['landing_site']='/?utm_source=facebook.com&utm_medium=paid&utm_campaign=123&utm_content=789'
    first=build_record(order)
    assert first['attribution']['channel']=='meta'
    assert first['attribution']['campaign_id']=='123'
    order.pop('landing_site')
    assert build_record(order,first)['attribution']['campaign_id']=='123'


def test_names_are_not_campaign_ids(order):
    order['note_attributes']=[dict(name='utm_source',value='facebook'),dict(name='utm_medium',value='paid'),dict(name='utm_campaign',value='Prospecting')]
    assert build_record(order)['attribution']['campaign_id']==''


def test_stale_webhook_and_courier_do_not_regress(database,order):
    sync_order(order)
    sync_tracking('TRACK101',{'status':'Delivered'},now())
    sync_tracking('TRACK101',{'status':'Booked'},now()-timedelta(days=1))
    order['updated_at']=(now()-timedelta(days=1)).isoformat()
    order['cancelled_at']=order['updated_at']
    sync_order(order,event_id='stale')
    assert counts(database)=={'Delivered':1}


def test_mixed_shipments(order):
    order['fulfillments'].append(dict(id=2,status='success',tracking_number='TRACK102'))
    record=build_record(order)
    record['shipments']['TRACK101']['status']='Delivered'
    assert build_record(order,record)['normalized_status']=='In process'


def test_historical_backfill_no_events(database,order):
    order['created_at']=order['updated_at']=(now()-timedelta(days=90)).isoformat()
    sync_order(order,source='backfill',emit=False)
    sync_tracking('TRACK101',{'status':'Delivered'},emit=False)
    assert events(database)==[]
    assert counts(database)=={'Delivered':1}


def test_payload_is_allowlisted_and_no_pii(order):
    order.update(email='private@example.com',phone='+923001234567',shipping_address={'name':'Private Person'})
    record=build_record(order)
    transition(record,None)
    record['cancellation_reason']='private@example.com'
    payload=event_payload(record,'order_cancelled')
    encoded=json.dumps(payload)
    for pii in ('private@example.com','+923001234567','Private Person','landing_page','referring_url','fbclid'):
        assert pii not in encoded
    assert payload['events'][0]['params']['items'][0]['item_name']=='Oak tray'
    record['attribution']['analytics_consent']='denied'
    assert event_payload(record,'order_delivered') is None


def test_campaign_and_product_filters_reconcile(order):
    records=[]
    for index,status in enumerate(('Delivered','Cancelled','In process')):
        payload=copy.deepcopy(order);payload['id']+=index
        r=build_record(payload);r['normalized_status']=status;records.append(r)
    start=end=now().date()
    result=report(records,[],{},start,end)
    assert result['kpis']['gross_orders']==3
    assert result['kpis']['delivery_rate']==.5
    assert result['kpis']['gross_orders']==sum(result['kpis'][k] for k in ('delivered','cancelled','in_process'))
    assert sum(c['gross_orders'] for c in result['campaigns'])==3
    assert report(records,[],{'campaign_id':'123','product':'501'},start,end)['kpis']['gross_orders']==3
    assert report(records,[],{'product':'nonexistent'},start,end)['kpis']['gross_orders']==0
    assert report(records,[],{'normalized_status':'Delivered'},start,end)['kpis']['gross_orders']==1
    assert result['kpis']['spend'] is None


def test_product_views_roll_variants_up_to_product(order):
    record = build_record(order)
    item = record['items'][0]
    viewed_other_variant = f"shopify_ZZ_{item['item_id']}_999999"
    assert product_identity(viewed_other_variant, [record]) == item['item_id']


def test_complete_meta_campaign_stats_show_when_google_is_unavailable(order):
    record = build_record(order)
    day = now().date()
    snapshots = [dict(source='meta', report_kind='ads', report_date=day, fetched_at=now(), rows=[
        dict(channel='meta', campaign_id='123', campaign_name='Campaign', group_id='456', group_name='Group',
             ad_id='789', ad_name='Ad', spend=300, currency='PKR', impressions=1000, clicks=20)
    ])]
    snapshots += [dict(source='ga4', report_kind=kind, report_date=day, fetched_at=now(), rows=[])
                  for kind in ('events', 'products', 'sessions')]
    result = report([record], snapshots, {}, day, day)
    campaign = next(row for row in result['campaigns'] if row['channel'] == 'meta' and row['campaign_id'] == '123')
    assert result['kpis']['spend'] is None
    assert campaign['spend'] == 300
    assert campaign['impressions'] == 1000
    assert result['funnel']['submitted_orders'] == 1


def test_date_boundaries(database,order):
    from analytics_store import load_orders
    order['created_at']='2026-09-01T00:00:00+05:00';order['updated_at']=order['created_at']
    sync_order(order,emit=False)
    _,_,start,end=date_range({'start':'2026-09-01','end':'2026-09-01'})
    assert len(load_orders(start,end))==1
    _,_,start,end=date_range({'start':'2026-08-31','end':'2026-08-31'})
    assert len(load_orders(start,end))==0


def test_dispatch_only_once_and_ambiguous_no_retry(database,order,monkeypatch):
    monkeypatch.setenv('GA4_MEASUREMENT_ID','G-TEST')
    monkeypatch.setenv('GA4_API_SECRET','test-only-secret')
    sync_order(order)
    sync_tracking('TRACK101',{'status':'Delivered'})
    validation=Mock(status_code=200);validation.json.return_value={'validationMessages':[]}
    success=Mock(status_code=204)
    post=Mock(side_effect=[validation,success]);monkeypatch.setattr('analytics_store.requests.post',post)
    assert dispatch_events()['sent']==1
    assert dispatch_events()['sent']==0
    assert post.call_count==2


def test_dispatch_timeout_retains_uncertain(database,order,monkeypatch):
    import requests
    monkeypatch.setenv('GA4_MEASUREMENT_ID','G-TEST');monkeypatch.setenv('GA4_API_SECRET','test-only-secret')
    sync_order(order);sync_tracking('TRACK101',{'status':'Delivered'})
    validation=Mock(status_code=200);validation.json.return_value={'validationMessages':[]}
    post=Mock(side_effect=[validation,requests.Timeout('secret must not leak')]);monkeypatch.setattr('analytics_store.requests.post',post)
    assert dispatch_events()['uncertain']==1
    assert dispatch_events()['uncertain']==0
    assert post.call_count==2
    assert events(database)[0]['dispatch_state']=='uncertain'


def test_authenticated_route(monkeypatch):
    from flask import Flask
    from analytics_routes import analytics
    app=Flask(__name__,template_folder='../templates');app.secret_key='test-only'
    app.register_blueprint(analytics);client=app.test_client()
    assert client.get('/api/analytics').status_code==401
    assert client.get('/analytics').status_code==302
    monkeypatch.setenv('ORDER_ANALYTICS_ENABLED','true')
    with client.session_transaction() as session: session['admin_portal_authenticated']=True
    response=client.get('/analytics')
    assert response.status_code==200
    assert response.headers['Cache-Control']=='private, no-store'
    assert "connect-src 'self'" in response.headers['Content-Security-Policy']
    assert client.get('/api/analytics?start=bad').status_code==400


def test_api_uses_json_service_account_for_unique_users(monkeypatch):
    from flask import Flask
    import analytics_integrations
    import analytics_routes

    app=Flask(__name__,template_folder='../templates');app.secret_key='test-only'
    app.register_blueprint(analytics_routes.analytics);client=app.test_client()
    monkeypatch.setenv('ORDER_ANALYTICS_ENABLED','true')
    monkeypatch.setenv('GOOGLE_REPORTING_SERVICE_ACCOUNT_JSON','{"type":"service_account"}')
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS',raising=False)
    monkeypatch.setattr(analytics_routes,'load_orders',lambda *args: [])
    monkeypatch.setattr(analytics_routes,'load_reports',lambda *args: [])
    monkeypatch.setattr(analytics_routes,'report',lambda *args: {'funnel':{},'warnings':[]})
    users=Mock(return_value=17)
    monkeypatch.setattr(analytics_integrations,'unique_users',users)
    with client.session_transaction() as session: session['admin_portal_authenticated']=True

    response=client.get('/api/analytics?start=2026-09-01&end=2026-09-02')

    assert response.status_code==200
    assert response.get_json()['funnel']['users']==17
    users.assert_called_once()
