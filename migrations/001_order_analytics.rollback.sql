BEGIN;
DROP TABLE IF EXISTS ga4_daily_snapshots;
DROP TABLE IF EXISTS ad_daily_snapshots;
DROP TABLE IF EXISTS order_refunds;
DROP TABLE IF EXISTS analytics_event_outbox;
DROP TABLE IF EXISTS webhook_receipts;
DROP TABLE IF EXISTS order_status_transitions;
DROP TABLE IF EXISTS order_analytics;
COMMIT;
