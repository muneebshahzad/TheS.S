import os

import psycopg2
from psycopg2.extras import RealDictCursor


_LAST_DB_ERROR = ""


def _set_last_db_error(message: str):
    global _LAST_DB_ERROR
    _LAST_DB_ERROR = message


def get_last_db_error() -> str:
    return _LAST_DB_ERROR


def _ensure_app_settings_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def _ensure_aghaje_order_overrides_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS aghaje_order_overrides (
            order_id TEXT PRIMARY KEY,
            amount_received NUMERIC(12,2) NOT NULL DEFAULT 0,
            packaging_cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            delivery_cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            delivery_status TEXT NOT NULL DEFAULT 'Inprocess',
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def _ensure_aghaje_item_cost_overrides_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS aghaje_item_cost_overrides (
            item_key TEXT PRIMARY KEY,
            product_id TEXT,
            variant_id TEXT,
            title TEXT NOT NULL,
            cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def _ensure_aghaje_order_item_cost_overrides_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS aghaje_order_item_cost_overrides (
            order_id TEXT NOT NULL,
            item_key TEXT NOT NULL,
            product_id TEXT,
            variant_id TEXT,
            title TEXT NOT NULL,
            cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (order_id, item_key)
        )
        """
    )


def _ensure_admin_passkeys_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_passkeys (
            credential_id BYTEA PRIMARY KEY,
            public_key BYTEA NOT NULL,
            sign_count BIGINT NOT NULL DEFAULT 0,
            device_name TEXT NOT NULL DEFAULT 'Mobile device',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_used_at TIMESTAMPTZ
        )
        """
    )


def _ensure_employee_passkeys_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_passkeys (
            credential_id BYTEA PRIMARY KEY,
            public_key BYTEA NOT NULL,
            sign_count BIGINT NOT NULL DEFAULT 0,
            device_name TEXT NOT NULL DEFAULT 'Mobile device',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_used_at TIMESTAMPTZ
        )
        """
    )


def _ensure_delivery_followup_contacts_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_followup_contacts (
            id BIGSERIAL PRIMARY KEY,
            shopify_order_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            remarks TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS delivery_followup_contacts_order_idx "
        "ON delivery_followup_contacts (shopify_order_id, created_at DESC)"
    )


def _ensure_vendor_invoices_tables(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_invoices (
            id BIGSERIAL PRIMARY KEY,
            vendor TEXT NOT NULL,
            name TEXT NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            status TEXT NOT NULL DEFAULT 'Draft',
            payment_method TEXT NOT NULL DEFAULT '',
            comments TEXT NOT NULL DEFAULT '',
            paid_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (vendor, period_start, period_end)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_invoice_lines (
            id BIGSERIAL PRIMARY KEY,
            invoice_id BIGINT NOT NULL REFERENCES vendor_invoices(id) ON DELETE CASCADE,
            line_key TEXT NOT NULL UNIQUE,
            shopify_order_id TEXT NOT NULL,
            order_number TEXT NOT NULL,
            eligible_date DATE NOT NULL,
            product_id TEXT,
            variant_id TEXT,
            product_name TEXT NOT NULL,
            image_url TEXT NOT NULL DEFAULT '',
            unit_cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            quantity INTEGER NOT NULL DEFAULT 1,
            order_status TEXT NOT NULL DEFAULT '',
            is_returned BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_invoice_refunds (
            id BIGSERIAL PRIMARY KEY,
            invoice_id BIGINT NOT NULL REFERENCES vendor_invoices(id) ON DELETE CASCADE,
            source_line_id BIGINT NOT NULL REFERENCES vendor_invoice_lines(id) ON DELETE CASCADE,
            amount NUMERIC(12,2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (source_line_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_invoice_adjustments (
            id BIGSERIAL PRIMARY KEY,
            invoice_id BIGINT NOT NULL REFERENCES vendor_invoices(id) ON DELETE CASCADE,
            amount NUMERIC(12,2) NOT NULL,
            method TEXT NOT NULL DEFAULT '',
            comments TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS vendor_invoice_lines_invoice_idx ON vendor_invoice_lines (invoice_id, eligible_date, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS vendor_invoice_refunds_invoice_idx ON vendor_invoice_refunds (invoice_id, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS vendor_invoice_adjustments_invoice_idx ON vendor_invoice_adjustments (invoice_id, id)")


def get_conn():
    url = (
        os.getenv("DATABASE_URL", "")
        or os.getenv("POSTGRES_URL", "")
        or os.getenv("POSTGRESQL_URL", "")
    )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url:
        return psycopg2.connect(url)

    host = os.getenv("PGHOST") or os.getenv("POSTGRES_HOST")
    port = os.getenv("PGPORT") or os.getenv("POSTGRES_PORT") or "5432"
    user = os.getenv("PGUSER") or os.getenv("POSTGRES_USER")
    password = os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD")
    database = (
        os.getenv("PGDATABASE")
        or os.getenv("POSTGRES_DB")
        or os.getenv("POSTGRES_DATABASE")
    )

    if host and user and database:
        return psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=database,
        )

    raise RuntimeError(
        "Database configuration missing. Set DATABASE_URL (preferred) or PGHOST/PGUSER/PGPASSWORD/PGDATABASE."
    )


def init_db():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS order_statuses (
                        key TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                _ensure_app_settings_table(cur)
                _ensure_aghaje_order_overrides_table(cur)
                _ensure_aghaje_item_cost_overrides_table(cur)
                _ensure_aghaje_order_item_cost_overrides_table(cur)
                _ensure_admin_passkeys_table(cur)
                _ensure_employee_passkeys_table(cur)
                _ensure_delivery_followup_contacts_table(cur)
                _ensure_vendor_invoices_tables(cur)
            conn.commit()
        _set_last_db_error("")
        print("DB initialized.")
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB init error: {e}")


def load_order_statuses() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT key, status FROM order_statuses")
                _set_last_db_error("")
                return {row["key"]: row["status"] for row in cur.fetchall()}
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load error: {e}")
        return {}


def upsert_order_status(key: str, status: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO order_statuses (key, status)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET status = EXCLUDED.status,
                            updated_at = NOW()
                    """,
                    (key, status),
                )
            conn.commit()
        _set_last_db_error("")
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert error: {e}")


def delete_order_status(key: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM order_statuses WHERE key = %s", (key,))
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB delete error: {e}")
        return False


def get_app_setting(key: str, default: str = "") -> str:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_app_settings_table(cur)
                cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
                row = cur.fetchone()
                _set_last_db_error("")
                return row[0] if row and row[0] is not None else default
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB get_app_setting error: {e}")
        return default


def set_app_setting(key: str, value: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_app_settings_table(cur)
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            updated_at = NOW()
                    """,
                    (key, value),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB set_app_setting error: {e}")
        return False


def _load_passkeys(table, ensure):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                ensure(cur)
                cur.execute(f"SELECT credential_id, public_key, sign_count, device_name, created_at, last_used_at FROM {table} ORDER BY created_at")
                rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        _set_last_db_error("")
        return rows
    except Exception as error:
        _set_last_db_error(str(error))
        print(f"DB load {table} error: {error}")
        return []


def _save_passkey(table, ensure, credential_id, public_key, sign_count, device_name):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure(cur)
                cur.execute(
                    f"""INSERT INTO {table} (credential_id, public_key, sign_count, device_name)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (credential_id) DO UPDATE SET
                        public_key=EXCLUDED.public_key, sign_count=EXCLUDED.sign_count,
                        device_name=EXCLUDED.device_name""",
                    (credential_id, public_key, sign_count, device_name),
                )
            conn.commit()
        return True
    except Exception as error:
        _set_last_db_error(str(error))
        print(f"DB save {table} error: {error}")
        return False


def _update_passkey_usage(table, ensure, credential_id, sign_count):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure(cur)
                cur.execute(f"UPDATE {table} SET sign_count=%s, last_used_at=NOW() WHERE credential_id=%s", (sign_count, credential_id))
            conn.commit()
        return True
    except Exception as error:
        _set_last_db_error(str(error))
        return False


def load_admin_passkeys():
    return _load_passkeys("admin_passkeys", _ensure_admin_passkeys_table)


def save_admin_passkey(credential_id, public_key, sign_count, device_name):
    return _save_passkey("admin_passkeys", _ensure_admin_passkeys_table, credential_id, public_key, sign_count, device_name)


def update_admin_passkey_usage(credential_id, sign_count):
    return _update_passkey_usage("admin_passkeys", _ensure_admin_passkeys_table, credential_id, sign_count)


def load_employee_passkeys():
    return _load_passkeys("employee_passkeys", _ensure_employee_passkeys_table)


def save_employee_passkey(credential_id, public_key, sign_count, device_name):
    return _save_passkey("employee_passkeys", _ensure_employee_passkeys_table, credential_id, public_key, sign_count, device_name)


def update_employee_passkey_usage(credential_id, sign_count):
    return _update_passkey_usage("employee_passkeys", _ensure_employee_passkeys_table, credential_id, sign_count)


def load_delivery_followup_contacts(order_ids=None):
    try:
        normalized_ids = [str(value) for value in (order_ids or []) if value is not None]
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_delivery_followup_contacts_table(cur)
                if normalized_ids:
                    cur.execute(
                        """SELECT id, shopify_order_id, outcome, remarks, created_at
                           FROM delivery_followup_contacts
                           WHERE shopify_order_id = ANY(%s)
                           ORDER BY created_at DESC, id DESC""",
                        (normalized_ids,),
                    )
                else:
                    return {}
                grouped = {}
                for row in cur.fetchall():
                    grouped.setdefault(row["shopify_order_id"], []).append(dict(row))
            conn.commit()
        _set_last_db_error("")
        return grouped
    except Exception as error:
        _set_last_db_error(str(error))
        print(f"DB load delivery follow-up contacts error: {error}")
        return {}


def add_delivery_followup_contact(order_id: str, outcome: str, remarks: str = ""):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_delivery_followup_contacts_table(cur)
                cur.execute(
                    """INSERT INTO delivery_followup_contacts (shopify_order_id, outcome, remarks)
                       VALUES (%s, %s, %s)
                       RETURNING id, shopify_order_id, outcome, remarks, created_at""",
                    (str(order_id), outcome, remarks),
                )
                row = dict(cur.fetchone())
            conn.commit()
        _set_last_db_error("")
        return row
    except Exception as error:
        _set_last_db_error(str(error))
        print(f"DB add delivery follow-up contact error: {error}")
        return None


def sync_vendor_invoice(invoice, lines):
    """Create/update one draft invoice and its source order lines."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_vendor_invoices_tables(cur)
                cur.execute(
                    """INSERT INTO vendor_invoices (vendor, name, period_start, period_end)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (vendor, period_start, period_end) DO UPDATE
                       SET name = EXCLUDED.name, updated_at = NOW()
                       RETURNING id, status""",
                    (invoice["vendor"], invoice["name"], invoice["period_start"], invoice["period_end"]),
                )
                saved = dict(cur.fetchone())
                if saved["status"] != "Paid":
                    for line in lines:
                        cur.execute(
                            """INSERT INTO vendor_invoice_lines
                               (invoice_id, line_key, shopify_order_id, order_number, eligible_date,
                                product_id, variant_id, product_name, image_url, unit_cost, quantity,
                                order_status, is_returned)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (line_key) DO UPDATE SET
                                 product_name=EXCLUDED.product_name, image_url=EXCLUDED.image_url,
                                 unit_cost=CASE WHEN vendor_invoice_lines.unit_cost = 0 OR EXCLUDED.unit_cost > 0
                                                THEN EXCLUDED.unit_cost ELSE vendor_invoice_lines.unit_cost END,
                                 quantity=EXCLUDED.quantity, order_status=EXCLUDED.order_status,
                                 is_returned=(vendor_invoice_lines.is_returned OR EXCLUDED.is_returned), updated_at=NOW()""",
                            (saved["id"], line["line_key"], line["shopify_order_id"], line["order_number"],
                             line["eligible_date"], line.get("product_id"), line.get("variant_id"),
                             line["product_name"], line.get("image_url", ""), line.get("unit_cost", 0),
                             line.get("quantity", 1), line.get("order_status", ""), bool(line.get("is_returned"))),
                        )
            conn.commit()
        _set_last_db_error("")
        return saved["id"]
    except Exception as error:
        _set_last_db_error(str(error))
        print(f"DB sync vendor invoice error: {error}")
        return None


def load_vendor_invoices(vendor="Tick Bags"):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_vendor_invoices_tables(cur)
                cur.execute("SELECT * FROM vendor_invoices WHERE vendor=%s ORDER BY period_end DESC, id DESC", (vendor,))
                invoices = [dict(row) for row in cur.fetchall()]
                for invoice in invoices:
                    cur.execute(
                        """SELECT l.*, COALESCE(r.amount, 0) AS refunded_amount,
                                  r.invoice_id AS refund_invoice_id
                           FROM vendor_invoice_lines l
                           LEFT JOIN vendor_invoice_refunds r ON r.source_line_id=l.id
                           WHERE l.invoice_id=%s ORDER BY l.eligible_date, l.order_number, l.id""",
                        (invoice["id"],),
                    )
                    invoice["lines"] = [dict(row) for row in cur.fetchall()]
                    cur.execute(
                        """SELECT r.*, l.order_number, l.product_name, l.image_url
                           FROM vendor_invoice_refunds r JOIN vendor_invoice_lines l ON l.id=r.source_line_id
                           WHERE r.invoice_id=%s ORDER BY r.id""",
                        (invoice["id"],),
                    )
                    invoice["refunds"] = [dict(row) for row in cur.fetchall()]
                    cur.execute("SELECT * FROM vendor_invoice_adjustments WHERE invoice_id=%s ORDER BY created_at, id", (invoice["id"],))
                    invoice["adjustments"] = [dict(row) for row in cur.fetchall()]
        _set_last_db_error("")
        return invoices
    except Exception as error:
        _set_last_db_error(str(error))
        print(f"DB load vendor invoices error: {error}")
        return []


def update_vendor_invoice_line_cost(line_id, unit_cost):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_vendor_invoices_tables(cur)
                cur.execute(
                    """UPDATE vendor_invoice_lines l SET unit_cost=%s, updated_at=NOW()
                       FROM vendor_invoices i WHERE l.id=%s AND i.id=l.invoice_id AND i.status!='Paid'
                       RETURNING l.*""",
                    (unit_cost, int(line_id)),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else None
    except Exception as error:
        _set_last_db_error(str(error))
        return None


def add_vendor_invoice_adjustment(invoice_id, amount, method, comments=""):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_vendor_invoices_tables(cur)
                cur.execute("SELECT status FROM vendor_invoices WHERE id=%s", (int(invoice_id),))
                invoice = cur.fetchone()
                if not invoice or invoice["status"] == "Paid":
                    return None
                cur.execute(
                    """INSERT INTO vendor_invoice_adjustments (invoice_id, amount, method, comments)
                       VALUES (%s,%s,%s,%s) RETURNING *""",
                    (int(invoice_id), amount, method, comments),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else None
    except Exception as error:
        _set_last_db_error(str(error))
        return None


def mark_vendor_invoice_paid(invoice_id, payment_method, comments=""):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_vendor_invoices_tables(cur)
                cur.execute(
                    """UPDATE vendor_invoices SET status='Paid', payment_method=%s, comments=%s,
                       paid_at=NOW(), updated_at=NOW() WHERE id=%s RETURNING *""",
                    (payment_method, comments, int(invoice_id)),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else None
    except Exception as error:
        _set_last_db_error(str(error))
        return None


def add_vendor_invoice_refund(invoice_id, source_line_id):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_vendor_invoices_tables(cur)
                cur.execute("SELECT unit_cost * quantity AS amount FROM vendor_invoice_lines WHERE id=%s", (int(source_line_id),))
                source = cur.fetchone()
                if not source:
                    return None
                cur.execute(
                    """INSERT INTO vendor_invoice_refunds (invoice_id, source_line_id, amount)
                       VALUES (%s,%s,%s) ON CONFLICT (source_line_id) DO NOTHING RETURNING *""",
                    (int(invoice_id), int(source_line_id), source["amount"]),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else {"already_reversed": True}
    except Exception as error:
        _set_last_db_error(str(error))
        return None


def load_aghaje_order_overrides() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_aghaje_order_overrides_table(cur)
                cur.execute(
                    """
                    SELECT order_id, amount_received, packaging_cost, delivery_cost, payment_status, delivery_status, updated_at
                    FROM aghaje_order_overrides
                    """
                )
                _set_last_db_error("")
                return {row["order_id"]: dict(row) for row in cur.fetchall()}
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load aghaje overrides error: {e}")
        return {}


def upsert_aghaje_order_override(
    order_id: str,
    amount_received: str | float,
    packaging_cost: str | float,
    delivery_cost: str | float,
    payment_status: str,
    delivery_status: str,
):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_aghaje_order_overrides_table(cur)
                cur.execute(
                    """
                    INSERT INTO aghaje_order_overrides (
                        order_id,
                        amount_received,
                        packaging_cost,
                        delivery_cost,
                        payment_status,
                        delivery_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id) DO UPDATE
                        SET amount_received = EXCLUDED.amount_received,
                            packaging_cost = EXCLUDED.packaging_cost,
                            delivery_cost = EXCLUDED.delivery_cost,
                            payment_status = EXCLUDED.payment_status,
                            delivery_status = EXCLUDED.delivery_status,
                            updated_at = NOW()
                    """,
                    (
                        order_id,
                        amount_received,
                        packaging_cost,
                        delivery_cost,
                        payment_status,
                        delivery_status,
                    ),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert aghaje override error: {e}")
        return False


def load_aghaje_item_cost_overrides() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_aghaje_item_cost_overrides_table(cur)
                cur.execute(
                    """
                    SELECT item_key, product_id, variant_id, title, cost
                    FROM aghaje_item_cost_overrides
                    """
                )
                _set_last_db_error("")
                return {row["item_key"]: dict(row) for row in cur.fetchall()}
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load aghaje item cost overrides error: {e}")
        return {}


def load_aghaje_order_item_cost_overrides() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_aghaje_order_item_cost_overrides_table(cur)
                cur.execute(
                    """
                    SELECT order_id, item_key, product_id, variant_id, title, cost
                    FROM aghaje_order_item_cost_overrides
                    """
                )
                _set_last_db_error("")
                grouped = {}
                for row in cur.fetchall():
                    grouped.setdefault(row["order_id"], {})[row["item_key"]] = dict(row)
                return grouped
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load aghaje order item cost overrides error: {e}")
        return {}


def upsert_aghaje_item_cost_override(
    item_key: str,
    title: str,
    cost: str | float,
    product_id: str | int | None = None,
    variant_id: str | int | None = None,
):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_aghaje_item_cost_overrides_table(cur)
                cur.execute(
                    """
                    INSERT INTO aghaje_item_cost_overrides (
                        item_key,
                        product_id,
                        variant_id,
                        title,
                        cost
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (item_key) DO UPDATE
                        SET product_id = EXCLUDED.product_id,
                            variant_id = EXCLUDED.variant_id,
                            title = EXCLUDED.title,
                            cost = EXCLUDED.cost,
                            updated_at = NOW()
                    """,
                    (
                        item_key,
                        str(product_id) if product_id is not None else None,
                        str(variant_id) if variant_id is not None else None,
                        title,
                        cost,
                    ),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert aghaje item cost override error: {e}")
        return False


def upsert_aghaje_order_item_cost_override(
    order_id: str,
    item_key: str,
    title: str,
    cost: str | float,
    product_id: str | int | None = None,
    variant_id: str | int | None = None,
):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_aghaje_order_item_cost_overrides_table(cur)
                cur.execute(
                    """
                    INSERT INTO aghaje_order_item_cost_overrides (
                        order_id,
                        item_key,
                        product_id,
                        variant_id,
                        title,
                        cost
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id, item_key) DO UPDATE
                        SET product_id = EXCLUDED.product_id,
                            variant_id = EXCLUDED.variant_id,
                            title = EXCLUDED.title,
                            cost = EXCLUDED.cost,
                            updated_at = NOW()
                    """,
                    (
                        order_id,
                        item_key,
                        str(product_id) if product_id is not None else None,
                        str(variant_id) if variant_id is not None else None,
                        title,
                        cost,
                    ),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert aghaje order item cost override error: {e}")
        return False
