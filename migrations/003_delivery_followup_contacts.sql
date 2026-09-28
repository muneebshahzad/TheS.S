BEGIN;
CREATE TABLE IF NOT EXISTS delivery_followup_contacts (
    id BIGSERIAL PRIMARY KEY,
    shopify_order_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    remarks TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS delivery_followup_contacts_order_idx
    ON delivery_followup_contacts (shopify_order_id, created_at DESC);
COMMIT;
