BEGIN;
ALTER TABLE order_analytics ADD COLUMN IF NOT EXISTS current_order_value NUMERIC(18,2);
ALTER TABLE order_analytics ADD COLUMN IF NOT EXISTS shipments JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE order_analytics ADD COLUMN IF NOT EXISTS purchase_recorded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE order_analytics ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE order_analytics ADD COLUMN IF NOT EXISTS refund_details JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE analytics_event_outbox ADD COLUMN IF NOT EXISTS dispatch_state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE analytics_event_outbox ADD COLUMN IF NOT EXISTS attempted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS analytics_outbox_pending_idx ON analytics_event_outbox (created_at) WHERE dispatch_state = 'pending';
CREATE TABLE IF NOT EXISTS analytics_reports (
    source TEXT NOT NULL,
    report_date DATE NOT NULL,
    report_kind TEXT NOT NULL,
    rows JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, report_date, report_kind)
);
COMMIT;
