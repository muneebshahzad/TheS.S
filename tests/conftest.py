import os
from pathlib import Path
import pytest


@pytest.fixture
def database(monkeypatch):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set TEST_DATABASE_URL to a disposable PostgreSQL database')
    monkeypatch.setenv('DATABASE_URL', url)
    from analytics_store import transaction
    with transaction() as cur:
        # Use a private schema per test; never truncate existing production tables.
        import uuid
        schema = 'analytics_test_' + uuid.uuid4().hex
        from psycopg2 import sql
        cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    from db import get_conn
    from contextlib import contextmanager
    from psycopg2.extras import RealDictCursor
    @contextmanager
    def isolated_transaction():
        conn = get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
                    yield cur
        finally:
            conn.close()
    monkeypatch.setattr('analytics_store.transaction', isolated_transaction)
    with isolated_transaction() as cur:
        for filename in ('001_order_analytics.sql','002_analytics_integrity.sql'):
            content = (Path(__file__).resolve().parents[1] / 'migrations' / filename).read_text()
            cur.execute(content.replace('BEGIN;', '').replace('COMMIT;', ''))
    yield isolated_transaction
    with transaction() as cur:
        cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        cur.execute('SET search_path TO public')


@pytest.fixture
def order():
    from order_analytics import now
    timestamp = now().isoformat()
    return dict(id=101,name='PK101A01',created_at=timestamp,updated_at=timestamp,currency='PKR',
        total_price='2200.00',subtotal_price='2000.00',total_discounts='0',
        total_shipping_price_set={'shop_money':{'amount':'200.00'}},financial_status='pending',
        fulfillment_status='fulfilled',payment_gateway_names=['Cash on Delivery'],
        fulfillments=[dict(id=1,status='success',tracking_number='TRACK101',tracking_company='Leopards')],
        line_items=[dict(id=1001,product_id=501,variant_id=601,title='Oak tray',sku='OAK-01',price='1000',quantity=2)],
        note_attributes=[dict(name='ss_'+k,value=v) for k,v in dict(utm_source='facebook',utm_medium='paid_social',
            utm_campaign='123',utm_term='456',utm_content='789',client_id='12345.67890',analytics_consent='granted').items()])
