import base64
import hashlib
import hmac
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

from analytics_store import sync_order
from order_analytics import now


def signed_headers(payload, event_id):
    signature = base64.b64encode(hmac.new(b'test-webhook-only', payload.encode(), hashlib.sha256).digest()).decode()
    return {'Content-Type': 'application/json', 'X-Shopify-Hmac-Sha256': signature, 'X-Shopify-Event-Id': event_id}


def test_cancel_webhook_signature_and_replay(database, order, monkeypatch):
    monkeypatch.setenv('INITIALIZE_APP', 'false')
    monkeypatch.setenv('ORDER_ANALYTICS_ENABLED', 'true')
    monkeypatch.setenv('SHOPIFY_WEBHOOK_SECRET', 'test-webhook-only')
    import main
    sync_order(order)
    order['cancelled_at'] = now().isoformat()
    order['updated_at'] = now().isoformat()
    payload = json.dumps(order)
    client = main.app.test_client()
    assert client.post('/shopify/webhook/order_cancelled', data=payload, content_type='application/json').status_code == 401
    for attempt in range(2):
        assert client.post('/shopify/webhook/order_cancelled', data=payload, headers=signed_headers(payload, 'cancel-one')).status_code == 200
    with database() as cursor:
        cursor.execute('SELECT normalized_status FROM order_analytics')
        assert cursor.fetchone()['normalized_status'] == 'Cancelled'
        cursor.execute("SELECT count(*) AS count FROM analytics_event_outbox WHERE event_name='order_cancelled'")
        assert cursor.fetchone()['count'] == 1


def test_related_webhook_fetches_full_order_without_leaking_errors(database, order, monkeypatch):
    monkeypatch.setenv('INITIALIZE_APP', 'false')
    monkeypatch.setenv('ORDER_ANALYTICS_ENABLED', 'true')
    monkeypatch.setenv('SHOPIFY_WEBHOOK_SECRET', 'test-webhook-only')
    import main
    monkeypatch.setattr(main, 'setup_shopify', lambda: None)
    remote_order = Mock()
    remote_order.to_dict.return_value = order
    lookup = Mock(return_value=remote_order)
    monkeypatch.setattr(main.shopify.Order, 'find', lookup)
    client = main.app.test_client()
    payload = json.dumps({'order_id': order['id']})
    headers = signed_headers(payload, 'related-one')
    for attempt in range(2):
        assert client.post('/shopify/webhook/fulfillment_updated', data=payload, headers=headers).status_code == 200
    with database() as cursor:
        cursor.execute('SELECT count(*) AS count FROM order_analytics')
        assert cursor.fetchone()['count'] == 1
    lookup.side_effect = RuntimeError('private credential must not appear')
    response = client.post('/shopify/webhook/refund_created', data=payload, headers=headers)
    assert response.status_code == 503
    assert 'private credential' not in response.get_data(as_text=True)


def test_migrations_reapply_and_rollback_in_private_schema(database):
    directory = Path(__file__).resolve().parents[1] / 'migrations'
    with database() as cursor:
        for filename in ('001_order_analytics.sql', '002_analytics_integrity.sql',
                         '002_analytics_integrity.rollback.sql', '001_order_analytics.rollback.sql',
                         '001_order_analytics.sql', '002_analytics_integrity.sql'):
            cursor.execute((directory / filename).read_text().replace('BEGIN;', '').replace('COMMIT;', ''))
        cursor.execute('SELECT current_order_value FROM order_analytics')
        assert cursor.fetchall() == []


def test_meta_purchase_aliases_are_not_added_together(monkeypatch):
    import analytics_integrations
    rows = [dict(campaign_id='1', campaign_name='Campaign', adset_id='2', adset_name='Group',
                 ad_id='3', ad_name='Ad', spend='20', account_currency='PKR',
                 actions=[{'action_type': 'purchase', 'value': '4'},
                          {'action_type': 'offsite_conversion.fb_pixel_purchase', 'value': '4'}])]
    monkeypatch.setattr(analytics_integrations, 'meta_pages', lambda *args: rows)
    save = Mock()
    monkeypatch.setattr(analytics_integrations, 'save_report', save)
    analytics_integrations.sync_meta('2026-09-01')
    assert save.call_args.args[3][0]['platform_purchases'] == 4


def test_unique_users_uses_one_range_query(monkeypatch):
    import analytics_integrations
    query = Mock(return_value=[{'totalUsers': 19}])
    monkeypatch.setattr(analytics_integrations, 'ga_report', query)
    assert analytics_integrations.unique_users('2026-09-01', '2026-09-07', {}) == 19
    assert query.call_args.kwargs['end'] == '2026-09-07'
    assert query.call_count == 1


def test_empty_shopify_journey_is_marked_checked(monkeypatch):
    import analytics_integrations
    monkeypatch.setattr('shopify_protected_data.get_graphql_token', lambda: 'test-token')
    monkeypatch.setattr('shopify_protected_data.get_graphql_endpoint', lambda: 'https://shop.example/graphql')
    monkeypatch.setattr(analytics_integrations, 'request_json', lambda *args, **kwargs: {
        'data': {'order': {'customerJourneySummary': None}}
    })
    save = Mock()
    monkeypatch.setattr('analytics_store.save_journey', save)

    analytics_integrations.sync_shopify_journey('123')

    save.assert_called_once_with('123', {})


def test_courier_refresh_commits_bounded_batches(monkeypatch):
    import analytics_cli
    import analytics_store
    import main

    class Cursor:
        def execute(self, *args, **kwargs):
            return None

        def fetchall(self):
            return [{'number': str(i)} for i in range(85)] + [{'number': '__unfulfilled__'}]

    @contextmanager
    def fake_transaction():
        yield Cursor()

    batches = []
    monkeypatch.setattr(analytics_store, 'transaction', fake_transaction)
    monkeypatch.setattr(main, 'refresh_tracking_summaries_sync',
                        lambda numbers, **kwargs: batches.append(list(numbers)) or len(numbers))

    assert analytics_cli.refresh_active() == {
        'shipments_requested': 85,
        'shipments_refreshed': 85,
    }
    assert [len(batch) for batch in batches] == [40, 40, 5]
