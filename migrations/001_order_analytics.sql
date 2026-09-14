BEGIN;

CREATE TABLE IF NOT EXISTS order_analytics (
    shopify_order_id TEXT PRIMARY KEY,
    order_number TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    normalized_status TEXT NOT NULL CHECK (normalized_status IN ('Delivered', 'Cancelled', 'In process')),
    raw_shopify_status TEXT,
    raw_financial_status TEXT,
    raw_fulfillment_status TEXT,
    raw_courier_status TEXT,
    status_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cancelled_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    cancellation_reason TEXT,
    currency TEXT NOT NULL,
    order_value NUMERIC(14,2) NOT NULL DEFAULT 0,
    subtotal NUMERIC(14,2) NOT NULL DEFAULT 0,
    discounts NUMERIC(14,2) NOT NULL DEFAULT 0,
    shipping NUMERIC(14,2) NOT NULL DEFAULT 0,
    refunded_value NUMERIC(14,2) NOT NULL DEFAULT 0,
    items JSONB NOT NULL DEFAULT '[]'::jsonb,
    payment_method TEXT,
    courier TEXT,
    tracking_number TEXT,
    attribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    final_status_version INTEGER NOT NULL DEFAULT 0,
    source_updated_at TIMESTAMPTZ,
    last_source TEXT NOT NULL DEFAULT 'shopify',
    CHECK (jsonb_typeof(items) = 'array'),
    CHECK (jsonb_typeof(attribution) = 'object')
);

CREATE INDEX IF NOT EXISTS order_analytics_created_at_idx ON order_analytics (created_at);
CREATE INDEX IF NOT EXISTS order_analytics_status_idx ON order_analytics (normalized_status);
CREATE INDEX IF NOT EXISTS order_analytics_campaign_idx ON order_analytics ((attribution->>'campaign_id'));

CREATE TABLE IF NOT EXISTS order_status_transitions (
    id BIGSERIAL PRIMARY KEY,
    shopify_order_id TEXT NOT NULL REFERENCES order_analytics(shopify_order_id) ON DELETE CASCADE,
    previous_status TEXT,
    new_status TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source TEXT NOT NULL,
    source_event_id TEXT,
    raw_status TEXT,
    UNIQUE (shopify_order_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS webhook_receipts (
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, event_id)
);

CREATE TABLE IF NOT EXISTS analytics_event_outbox (
    id BIGSERIAL PRIMARY KEY,
    shopify_order_id TEXT NOT NULL REFERENCES order_analytics(shopify_order_id) ON DELETE CASCADE,
    event_name TEXT NOT NULL,
    final_status_version INTEGER NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (shopify_order_id, event_name, final_status_version)
);

CREATE TABLE IF NOT EXISTS order_refunds (
    shopify_order_id TEXT NOT NULL REFERENCES order_analytics(shopify_order_id) ON DELETE CASCADE,
    refund_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    currency TEXT NOT NULL,
    value NUMERIC(14,2) NOT NULL DEFAULT 0,
    items JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (shopify_order_id, refund_id)
);

CREATE TABLE IF NOT EXISTS ad_daily_snapshots (
    snapshot_date DATE NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('google', 'meta')),
    campaign_id TEXT NOT NULL,
    campaign_name TEXT,
    group_id TEXT NOT NULL DEFAULT '',
    group_name TEXT,
    ad_id TEXT NOT NULL DEFAULT '',
    ad_name TEXT,
    impressions BIGINT NOT NULL DEFAULT 0,
    reach BIGINT,
    clicks BIGINT NOT NULL DEFAULT 0,
    link_clicks BIGINT,
    spend NUMERIC(14,2) NOT NULL DEFAULT 0,
    platform_purchases NUMERIC(14,2) NOT NULL DEFAULT 0,
    platform_purchase_value NUMERIC(14,2) NOT NULL DEFAULT 0,
    currency TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (snapshot_date, channel, campaign_id, group_id, ad_id)
);

CREATE TABLE IF NOT EXISTS ga4_daily_snapshots (
    snapshot_date DATE NOT NULL,
    source_medium TEXT NOT NULL DEFAULT '',
    campaign_id TEXT NOT NULL DEFAULT '',
    campaign_name TEXT NOT NULL DEFAULT '',
    product_id TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    users BIGINT NOT NULL DEFAULT 0,
    sessions BIGINT NOT NULL DEFAULT 0,
    engaged_sessions BIGINT NOT NULL DEFAULT 0,
    product_views BIGINT NOT NULL DEFAULT 0,
    add_to_carts BIGINT NOT NULL DEFAULT 0,
    begin_checkouts BIGINT NOT NULL DEFAULT 0,
    purchases BIGINT NOT NULL DEFAULT 0,
    refunds BIGINT NOT NULL DEFAULT 0,
    purchase_revenue NUMERIC(14,2) NOT NULL DEFAULT 0,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (snapshot_date, source_medium, campaign_id, campaign_name, product_id, product_name)
);

COMMIT;
