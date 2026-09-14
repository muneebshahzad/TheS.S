import asyncio
import base64
import hashlib
import hmac
import json
import os
import smtplib
import sys
import threading
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from urllib.parse import quote, urlencode, urlparse

import aiohttp
import requests
import shopify
import lazop
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from markupsafe import Markup

from db import (
    delete_order_status,
    get_app_setting,
    init_db,
    load_aghaje_item_cost_overrides,
    load_aghaje_order_item_cost_overrides,
    load_aghaje_order_overrides,
    load_order_statuses,
    set_app_setting,
    upsert_aghaje_item_cost_override,
    upsert_aghaje_order_item_cost_override,
    upsert_aghaje_order_override,
    upsert_order_status,
)
from shopify_protected_data import (
    create_oauth_state,
    exchange_oauth_code_for_token,
    fetch_protected_order_details,
    get_graphql_api_version,
    get_graphql_endpoint,
    get_graphql_token,
    get_install_url,
    get_protected_data_config_status,
    get_shop_domain,
    save_offline_token,
    verify_oauth_hmac,
)
from token_manager import get_access_token, load_tokens, save_tokens


app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "default_secret_key")
from analytics_routes import analytics
app.register_blueprint(analytics)


def analytics_enabled():
    return os.getenv("ORDER_ANALYTICS_ENABLED") == "true"

EMPLOYEE_PORTAL_SESSION_KEY = "employee_portal_authenticated"
ADMIN_PORTAL_SESSION_KEY = "admin_portal_authenticated"
AGHAJE_PORTAL_SESSION_KEY = "aghaje_portal_authenticated"
SHOPIFY_OAUTH_STATE_SESSION_KEY = "shopify_oauth_state"
EMPLOYEE_PORTAL_PASSWORD = os.getenv("EMPLOYEE_PORTAL_PASSWORD", "@@@t")
ADMIN_PORTAL_PASSWORD = os.getenv("ADMIN_PORTAL_PASSWORD", "security")
AGHAJE_PORTAL_PASSWORD = os.getenv("AGHAJE_PORTAL_PASSWORD", "security")

order_details = []
daraz_orders_cache = []
daraz_refresh_lock = threading.Lock()
daraz_refresh_attempted = False
daraz_last_error = ""
product_image_cache = {}
aghaje_product_cache = {}
aghaje_inventory_item_cost_cache = {}
tracking_summary_cache = {}
tracking_summary_cache_loaded = False
tracking_refresh_lock = threading.Lock()
RATE_LIMIT = 2
LAST_REQUEST_TIME = 0.0
PRODUCT_COSTS_SETTING_KEY = "product_cost_overrides_v1"
AGHAJE_NET_PAYMENT_RECEIVED_SETTING_KEY = "aghaje_net_payment_received_v1"
TRACKING_SUMMARY_CACHE_SETTING_KEY = "tracking_summary_cache_v1"
ABANDONED_VIEWED_SETTING_KEY = "abandoned_checkout_viewed_v1"
ABANDONED_CACHE_SETTING_KEY = "abandoned_checkout_cache_v1"
DARAZ_API_URL = os.getenv("DARAZ_API_URL", "https://api.daraz.pk/rest").rstrip("/")
DARAZ_ORDER_STATUSES = tuple(
    status.strip()
    for status in os.getenv("DARAZ_ORDER_STATUSES", "shipped,pending,ready_to_ship,packed").split(",")
    if status.strip()
)
DARAZ_ORDER_LOOKBACK_DAYS = max(int(os.getenv("DARAZ_ORDER_LOOKBACK_DAYS", "365")), 1)
DARAZ_PAGE_LIMIT = min(max(int(os.getenv("DARAZ_PAGE_LIMIT", "50")), 1), 100)
DARAZ_MAX_PAGES_PER_STATUS = max(int(os.getenv("DARAZ_MAX_PAGES_PER_STATUS", "1")), 1)
inventory_item_cost_cache = {}
TRACKING_REFRESH_SYNC_LIMIT = int(os.getenv("TRACKING_REFRESH_SYNC_LIMIT", "24"))
TRACKING_REFRESH_BACKGROUND_LIMIT = int(os.getenv("TRACKING_REFRESH_BACKGROUND_LIMIT", "250"))
TRACKING_REFRESH_FRESH_SECONDS = int(os.getenv("TRACKING_REFRESH_FRESH_SECONDS", "45"))
AGHAJE_AUTO_TRACK_FRESH_SECONDS = int(os.getenv("AGHAJE_AUTO_TRACK_FRESH_SECONDS", "1800"))
TRACKING_REFRESH_SYNC_DEADLINE_SECONDS = float(os.getenv("TRACKING_REFRESH_SYNC_DEADLINE_SECONDS", "16"))
TRACKING_REFRESH_BACKGROUND_DEADLINE_SECONDS = float(os.getenv("TRACKING_REFRESH_BACKGROUND_DEADLINE_SECONDS", "90"))
TRACKING_REFRESH_PER_SHIPMENT_TIMEOUT_SECONDS = float(os.getenv("TRACKING_REFRESH_PER_SHIPMENT_TIMEOUT_SECONDS", "8"))
ABANDONED_CACHE_FRESH_SECONDS = int(os.getenv("ABANDONED_CACHE_FRESH_SECONDS", "900"))
abandoned_cache_refresh_lock = threading.Lock()

_TAG_STYLES = {
    "Call Courier": "background:#ede7f6;color:#4527a0",
    "Leopards": "background:#e6f6f8;color:#0a5c6e",
    "Order Confirmed": "background:#e8f5e9;color:#1b5e20",
    "Fulfilment Not Set": "background:#fff8e1;color:#e65100",
    "No Throw": "background:#fce4ec;color:#880e4f",
    "Lahore": "background:#fff3cd;color:#8b5a00",
}


def is_leopards_tracking(tracking_number):
    return str(tracking_number or "").strip().upper().startswith("LE")


def courier_label_for_tracking(courier_name="", tracking_number=""):
    if is_leopards_tracking(tracking_number):
        return "Leopards"
    return str(courier_name or "").strip()


def is_postex_courier(courier_name=""):
    normalized = str(courier_name or "").strip().lower().replace(" ", "").replace("-", "")
    return "postex" in normalized


def normalize_scan_term(term):
    return (term or "").strip().lower().replace("#", "")


def is_lahore_city(city):
    normalized = (city or "").strip().lower()
    return "lahore" in normalized or "lhr" in normalized


def is_undelivered_status(status):
    normalized = (status or "").strip().upper()
    if not normalized:
        return False
    delivered_like = {"DELIVERED", "BOOKED", "UN-BOOKED", "UN-FULFILLED", "UNFULFILLED", "CANCELLED"}
    if normalized in delivered_like:
        return False
    return any(
        keyword in normalized
        for keyword in ("OUT FOR", "RETURN", "REFUS", "CALL NOT", "HOLD", "UNTRACEABLE", "ATTEMPT", "DELAY")
    ) or "UNDELIVERED" in normalized


def is_delivered_status(status):
    normalized = (status or "").strip().upper()
    return normalized == "DELIVERED" or normalized.startswith("DELIVERED ")


def normalize_status_bucket(status):
    raw = (status or "Un-Booked").strip()
    upper = raw.upper()
    if "PARTIALLY DELIVERED" in upper:
        return "Partially Delivered"
    if "RETURNED TO SHIPPER" in upper:
        return "RETURNED TO SHIPPER"
    if "BEING RETURN" in upper or "OUT FOR RETURN" in upper or "RETURN SUBMISSION" in upper:
        return "Being Return"
    if "UNDELIVERED" in upper:
        return "Undelivered"
    if "OUT FOR DELIVERY" in upper:
        return "Out For Delivery"
    if is_delivered_status(raw):
        return "Delivered"
    if "PICKED FROM SHIPPER" in upper:
        return "Picked From Shipper"
    if upper == "BOOKED" or "CONSIGNMENT BOOKED" in upper:
        return "Booked"
    if upper in {"UN-BOOKED", "UNBOOKED"}:
        return "Un-Booked"
    return raw


def should_keep_order_in_active_list(order_info, order=None):
    if not order_info:
        return False
    if order is not None and (getattr(order, "cancelled_at", None) or getattr(order, "closed_at", None)):
        return False
    if order_info.get("cancelled_at") or order_info.get("closed_at"):
        return False
    return True


def split_customer_name(name):
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "", "Customer"
    if len(parts) == 1:
        return parts[0], "Customer"
    return parts[0], " ".join(parts[1:])


def parse_money(value, default=0.0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


def parse_int(value, default=0):
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return int(default)


def active_shopify_line_item_quantity(line_item, *, use_original_quantity=False):
    if use_original_quantity:
        raw_quantity = line_item.get("quantity") if isinstance(line_item, dict) else getattr(line_item, "quantity", 0)
    elif isinstance(line_item, dict) and line_item.get("current_quantity") is not None:
        raw_quantity = line_item.get("current_quantity")
    elif hasattr(line_item, "current_quantity"):
        raw_quantity = getattr(line_item, "current_quantity")
    elif isinstance(line_item, dict):
        raw_quantity = line_item.get("quantity")
    else:
        raw_quantity = getattr(line_item, "quantity", 0)
    try:
        return max(int(raw_quantity or 0), 0)
    except (TypeError, ValueError):
        return 0


def current_shopify_order_money(order, current_field, original_field):
    """Prefer Shopify's post-edit amount while preserving legitimate zero totals."""
    if isinstance(order, dict):
        current_value = order.get(current_field)
        original_value = order.get(original_field)
    else:
        current_value = getattr(order, current_field, None)
        original_value = getattr(order, original_field, None)
    return parse_money(original_value if current_value is None else current_value, 0)


def parse_date_for_sort(value):
    if not value:
        return datetime.min
    raw = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%b %d, %Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return datetime.min
    # Shopify's normalized order dates are timezone-naive, while Daraz sends
    # offset-aware ISO timestamps. Keep their local wall-clock value so mixed
    # marketplace feeds remain comparable and sortable.
    return parsed.replace(tzinfo=None)


def parse_date_timestamp(value):
    parsed = parse_date_for_sort(value)
    if parsed == datetime.min:
        return 0.0
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def format_daraz_date(value):
    """Return a compact date without failing on Daraz's supported timestamp variants."""
    if not value:
        return "N/A"
    parsed = parse_date_for_sort(value)
    if parsed == datetime.min:
        return str(value)
    return parsed.strftime("%Y-%m-%d")


def daraz_configuration():
    values = {
        "app_key": (os.getenv("DARAZ_APP_KEY") or "").strip(),
        "app_secret": (os.getenv("DARAZ_APP_SECRET") or "").strip(),
    }
    missing = ["DARAZ_" + name.upper() for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Daraz is not configured. Set {', '.join(missing)}.")
    return values


def execute_daraz_request(client, path, access_token, **parameters):
    api_request = lazop.LazopRequest(path, "GET")
    for key, value in parameters.items():
        if value is not None:
            api_request.add_api_param(key, str(value))
    response = client.execute(api_request, access_token)
    body = response.body if isinstance(response.body, dict) else {}
    code = str(body.get("code", "0"))
    if code != "0":
        message = body.get("message") or body.get("detail") or "Unknown Daraz API error"
        raise RuntimeError(f"Daraz API {path} failed ({code}): {message}")
    return body


def daraz_tracking_statuses(trace_body):
    tracking_statuses = {}
    result = trace_body.get("result") or {}
    trace_groups = result.get("data") or []
    if isinstance(trace_groups, dict):
        trace_groups = [trace_groups]
    for trace_group in trace_groups:
        if not isinstance(trace_group, dict):
            continue
        packages = trace_group.get("package_detail_info_list") or []
        for package in packages:
            tracking_number = str(package.get("tracking_number") or "").strip()
            events = package.get("logistic_detail_info_list") or []
            latest_event = events[-1] if events and isinstance(events[-1], dict) else {}
            if tracking_number:
                tracking_statuses[tracking_number] = latest_event.get("title") or "N/A"
    return tracking_statuses


def normalize_daraz_order(order, status, items, tracking_statuses):
    shipping_address = order.get("address_shipping") or {}
    if not isinstance(shipping_address, dict):
        shipping_address = {"address1": str(shipping_address)}

    address = shipping_address.get("address") or " ".join(
        str(shipping_address.get(key) or "").strip()
        for key in ("address1", "address2", "address3", "address4", "address5")
        if shipping_address.get(key)
    )
    item_details = []
    for item in items:
        tracking_number = str(item.get("tracking_code") or "").strip()
        item_title = str(item.get("name") or "Unknown item").strip()
        variation = str(item.get("variation") or "").strip()
        if variation:
            item_title = f"{item_title} - {variation.replace('Color family:', '').strip()}"
        item_details.append(
            {
                "item_image": item.get("product_main_image") or "",
                "item_title": item_title,
                "quantity": 1,
                "tracking_number": tracking_number or "N/A",
                "status": tracking_statuses.get(tracking_number, "N/A"),
            }
        )

    first_name = str(order.get("customer_first_name") or "").strip()
    last_name = str(order.get("customer_last_name") or "").strip()
    customer_name = " ".join(part for part in (first_name, last_name) if part) or "Unknown"
    return {
        "order_id": order.get("order_id") or "Unknown",
        "customer": {
            "name": customer_name,
            "address": address or "N/A",
            "phone": shipping_address.get("phone") or order.get("customer_phone") or "N/A",
        },
        "status": str(status).replace("_", " ").title(),
        "date": format_daraz_date(order.get("created_at")),
        "created_at": order.get("created_at") or "",
        "total_price": order.get("price") or "0.00",
        "items_list": item_details,
    }


def get_daraz_orders(statuses=None):
    config = daraz_configuration()
    access_token = get_access_token()
    client = lazop.LazopClient(
        DARAZ_API_URL,
        config["app_key"],
        config["app_secret"],
        timeout=30,
    )
    selected_statuses = tuple(statuses or DARAZ_ORDER_STATUSES)
    now = datetime.now().astimezone()
    created_after = (now - timedelta(days=DARAZ_ORDER_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    all_orders = []

    for status in selected_statuses:
        offset = 0
        for _ in range(DARAZ_MAX_PAGES_PER_STATUS):
            order_body = execute_daraz_request(
                client,
                "/orders/get",
                access_token,
                sort_direction="DESC",
                sort_by="updated_at",
                created_after=created_after,
                update_after=created_after,
                offset=offset,
                limit=DARAZ_PAGE_LIMIT,
                status=status,
            )
            orders = (order_body.get("data") or {}).get("orders") or []
            for order in orders:
                order_id = order.get("order_id")
                item_body = execute_daraz_request(
                    client,
                    "/order/items/get",
                    access_token,
                    order_id=order_id,
                )
                items = item_body.get("data") or []
                trace_body = execute_daraz_request(
                    client,
                    "/logistic/order/trace",
                    access_token,
                    order_id=order_id,
                )
                all_orders.append(
                    normalize_daraz_order(order, status, items, daraz_tracking_statuses(trace_body))
                )
            if len(orders) < DARAZ_PAGE_LIMIT:
                break
            offset += DARAZ_PAGE_LIMIT

    all_orders.sort(key=lambda order: parse_date_timestamp(order.get("created_at")), reverse=True)
    return all_orders


def get_app_base_url():
    explicit = (
        os.getenv("APP_BASE_URL")
        or os.getenv("PUBLIC_APP_BASE_URL")
        or os.getenv("SHOPIFY_APP_BASE_URL")
        or ""
    ).strip()
    return explicit.rstrip("/") if explicit else "https://dashboard.thesleekspace.com"


def get_daraz_callback_url():
    return f"{get_app_base_url()}/daraz"


def get_daraz_authorize_url():
    app_key = (os.getenv("DARAZ_APP_KEY") or "").strip()
    if not app_key:
        return ""
    callback = quote(get_daraz_callback_url(), safe="")
    return (
        "https://api.daraz.pk/oauth/authorize"
        f"?response_type=code&redirect_uri={callback}&client_id={quote(app_key, safe='')}"
    )


def refresh_daraz_cache_if_needed(force=False):
    global daraz_orders_cache, daraz_refresh_attempted, daraz_last_error
    if daraz_orders_cache and not force:
        return daraz_orders_cache
    if daraz_refresh_attempted and not force:
        return daraz_orders_cache
    if not daraz_refresh_lock.acquire(blocking=False):
        return daraz_orders_cache
    try:
        daraz_refresh_attempted = True
        refreshed_orders = get_daraz_orders()
        daraz_last_error = ""
        if refreshed_orders or force:
            daraz_orders_cache = refreshed_orders
        return daraz_orders_cache
    except Exception as error:
        daraz_last_error = str(error)
        print(f"Could not refresh Daraz cache: {error}")
        return daraz_orders_cache
    finally:
        daraz_refresh_lock.release()


def normalize_customer_lookup_value(value):
    return str(value or "").strip().lower()


def normalize_customer_phone(value):
    if isinstance(value, dict):
        value = value.get("phone") or value.get("number") or ""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("92") and len(digits) > 10:
        digits = digits[2:]
    return digits[-10:] if len(digits) >= 10 else digits


def normalize_country_code(value):
    return str(value or "").strip().upper()


def infer_country_code(country):
    normalized = str(country or "").strip().lower()
    aliases = {
        "pakistan": "PK",
        "united states": "US",
        "usa": "US",
        "united kingdom": "GB",
        "uk": "GB",
        "united arab emirates": "AE",
        "uae": "AE",
        "saudi arabia": "SA",
    }
    return aliases.get(normalized, "")


def get_phone_country_prefix(country_code):
    prefixes = {
        "PK": "92",
        "US": "1",
        "CA": "1",
        "GB": "44",
        "AE": "971",
        "SA": "966",
    }
    return prefixes.get(normalize_country_code(country_code), "")


def format_customer_phone(phone, country_code=""):
    raw = str(phone or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw
    if raw.startswith("+"):
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    prefix = get_phone_country_prefix(country_code)
    local = normalize_customer_phone(raw)
    if prefix and local:
        return f"+{prefix}{local}"
    return raw


def as_dict(value):
    return value if isinstance(value, dict) else {}


def first_dict(*values):
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def join_nonempty(values, separator=" "):
    return separator.join(str(value).strip() for value in values if str(value or "").strip())


def get_customer_phone_candidates(checkout, shipping, billing, customer):
    default_address = as_dict(checkout.get("default_address"))
    customer_default_address = as_dict(customer.get("default_address") or customer.get("defaultAddress"))
    buyer_identity = as_dict(checkout.get("buyer_identity") or checkout.get("buyerIdentity"))
    return [
        checkout.get("phone"),
        checkout.get("customer_phone"),
        checkout.get("sms_marketing_phone"),
        checkout.get("phone_number"),
        shipping.get("phone"),
        billing.get("phone"),
        customer.get("phone"),
        customer.get("customer_phone"),
        as_dict(customer.get("default_phone_number") or customer.get("defaultPhoneNumber")).get("phoneNumber"),
        default_address.get("phone"),
        customer_default_address.get("phone"),
        buyer_identity.get("phone"),
    ]


def first_present(values):
    for value in values:
        if value:
            return value
    return ""


def get_checkout_customer_blocks(checkout):
    customer = first_dict(
        checkout.get("customer"),
        checkout.get("customer_details"),
        checkout.get("customerDetails"),
        checkout.get("buyer_identity"),
        checkout.get("buyerIdentity"),
    )
    shipping = first_dict(
        checkout.get("shipping_address"),
        checkout.get("shippingAddress"),
        checkout.get("shippingAddressV2"),
        as_dict(customer.get("default_address") or customer.get("defaultAddress")),
    )
    billing = first_dict(
        checkout.get("billing_address"),
        checkout.get("billingAddress"),
        checkout.get("billingAddressV2"),
    )
    return customer, shipping, billing


def get_checkout_customer_name(checkout, shipping, billing, customer):
    customer_name = join_nonempty(
        [
            customer.get("first_name") or customer.get("firstName"),
            customer.get("last_name") or customer.get("lastName"),
        ]
    )
    return first_present(
        [
            shipping.get("name"),
            billing.get("name"),
            customer.get("name"),
            customer_name,
            checkout.get("customer_name"),
            checkout.get("name"),
            checkout.get("email"),
            checkout.get("phone"),
            "No customer",
        ]
    )


def get_checkout_customer_email(checkout, customer):
    return first_present(
        [
            checkout.get("email"),
            checkout.get("customer_email"),
            customer.get("email"),
            as_dict(checkout.get("buyer_identity") or checkout.get("buyerIdentity")).get("email"),
        ]
    )


def get_checkout_country(shipping, billing, checkout):
    return first_present(
        [
            shipping.get("country"),
            shipping.get("country_name"),
            shipping.get("countryName"),
            billing.get("country"),
            billing.get("country_name"),
            billing.get("countryName"),
            checkout.get("country"),
        ]
    )


def get_checkout_country_code(shipping, billing, checkout, country):
    return normalize_country_code(
        first_present(
            [
                shipping.get("country_code"),
                shipping.get("countryCodeV2"),
                shipping.get("countryCode"),
                billing.get("country_code"),
                billing.get("countryCodeV2"),
                billing.get("countryCode"),
                checkout.get("buyer_accepts_sms_marketing_country"),
                checkout.get("country_code"),
                infer_country_code(country),
            ]
        )
    )


def get_checkout_customer_address(shipping, billing):
    address = first_present(
        [
            join_nonempty([shipping.get("address1"), shipping.get("address2")]),
            join_nonempty([billing.get("address1"), billing.get("address2")]),
            shipping.get("address"),
            billing.get("address"),
        ]
    )
    return address


def format_currency_amount(value, currency="PKR"):
    amount = parse_money(value, 0)
    currency = str(currency or "PKR").upper()
    amount_text = f"{int(amount):,}" if amount == int(amount) else f"{amount:,.2f}"
    symbols = {
        "USD": "$",
        "GBP": "£",
        "EUR": "€",
    }
    if currency in symbols:
        return f"{symbols[currency]}{amount_text} ({currency})"
    if currency == "PKR":
        return f"PKR {amount_text}"
    return f"{amount_text} {currency}"


def get_abandoned_created_at_min(days=7):
    return (datetime.now() - timedelta(days=days)).replace(microsecond=0).isoformat()


def load_product_cost_overrides():
    raw = get_app_setting(PRODUCT_COSTS_SETTING_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_product_cost_overrides(overrides):
    return set_app_setting(PRODUCT_COSTS_SETTING_KEY, json.dumps(overrides or {}))


def get_aghaje_net_payment_received_entries():
    raw = get_app_setting(AGHAJE_NET_PAYMENT_RECEIVED_SETTING_KEY, "")
    raw = str(raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            entries = []
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                amount = parse_money(entry.get("amount", 0))
                if amount == 0:
                    continue
                entries.append(
                    {
                        "amount": amount,
                        "created_at": entry.get("created_at") or "",
                    }
                )
            return entries
    except Exception:
        pass

    legacy_amount = parse_money(raw)
    if legacy_amount == 0:
        return []
    return [{"amount": legacy_amount, "created_at": "Legacy saved amount"}]


def add_aghaje_net_payment_received_entry(value):
    amount = parse_money(value)
    if amount == 0:
        raise ValueError("Amount received must not be zero.")
    entries = get_aghaje_net_payment_received_entries()
    entries.append(
        {
            "amount": amount,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return set_app_setting(AGHAJE_NET_PAYMENT_RECEIVED_SETTING_KEY, json.dumps(entries))


def get_aghaje_daily_key(value):
    parsed = parse_date_for_sort(value)
    if parsed == datetime.min:
        return "Undated"
    return parsed.date().isoformat()


def build_aghaje_daily_balance_ledger(orders, cash_entries):
    daily = {}

    def get_day(day_key):
        return daily.setdefault(
            day_key or "Undated",
            {
                "date": day_key or "Undated",
                "order_count": 0,
                "cod_order_count": 0,
                "cash_entry_count": 0,
                "cod": 0.0,
                "cost": 0.0,
                "cash_paid": 0.0,
                "change": 0.0,
                "running_balance": 0.0,
                "details": [],
            },
        )

    for order in orders or []:
        if str(order.get("delivery_status") or "").strip() == "Cancelled":
            continue
        amount_received = parse_money(order.get("amount_received", 0))
        order_cost = 0.0
        if is_aghaje_fulfilled_or_partial(order.get("fulfillment_status_raw")):
            order_cost = (
                parse_money(order.get("item_cost", 0))
                + parse_money(order.get("packaging_cost", 0))
                + parse_money(order.get("delivery_cost", 0))
            )
        status_label = order.get("delivery_status") or order.get("fulfillment_status") or ""
        if order_cost:
            cost_day = get_day(get_aghaje_daily_key(order.get("created_at")))
            cost_day["order_count"] += 1
            cost_day["cost"] += order_cost
            cost_day["details"].append(
                {
                    "type": "cost",
                    "label": order.get("order_id") or "Order",
                    "status": status_label,
                    "cod": 0.0,
                    "cost": round(order_cost, 2),
                    "cash_paid": 0.0,
                    "note": "Product, packaging, and delivery cost applied from Shopify order date.",
                }
            )
        if amount_received:
            cod_day = get_day(get_aghaje_daily_key(order.get("amount_received_updated_at") or order.get("created_at")))
            cod_day["cod_order_count"] += 1
            cod_day["cod"] += amount_received
            cod_day["details"].append(
                {
                    "type": "cod",
                    "label": order.get("order_id") or "Order",
                    "status": status_label,
                    "cod": round(amount_received, 2),
                    "cost": 0.0,
                    "cash_paid": 0.0,
                    "note": "Amount received saved on this date.",
                }
            )

    for entry in cash_entries or []:
        amount = parse_money(entry.get("amount", 0))
        if amount == 0:
            continue
        day = get_day(get_aghaje_daily_key(entry.get("created_at")))
        day["cash_entry_count"] += 1
        day["cash_paid"] += amount
        day["details"].append(
            {
                "type": "cash",
                "label": "Cash paid entry",
                "status": entry.get("created_at") or "",
                "cod": 0.0,
                "cost": 0.0,
                "cash_paid": round(amount, 2),
                "note": "Manual cash payment entry.",
            }
        )

    running_balance = 0.0
    rows = []
    for day_key in sorted(daily.keys()):
        row = daily[day_key]
        row["cod"] = round(row["cod"], 2)
        row["cost"] = round(row["cost"], 2)
        row["cash_paid"] = round(row["cash_paid"], 2)
        row["change"] = round(row["cod"] + row["cash_paid"] - row["cost"], 2)
        running_balance = round(running_balance + row["change"], 2)
        row["running_balance"] = running_balance
        rows.append(row)
    return list(reversed(rows))


def product_cost_key(product_id=None, variant_id=None, title=""):
    if variant_id:
        return f"variant:{variant_id}"
    if product_id:
        return f"product:{product_id}"
    return f"title:{str(title or '').strip().lower()}"


def get_cost_override_for_item(overrides, product_id=None, variant_id=None, title=""):
    for key in (
        product_cost_key(product_id=product_id, variant_id=variant_id, title=title),
        product_cost_key(product_id=product_id, title=title),
        product_cost_key(title=title),
    ):
        entry = overrides.get(key)
        if isinstance(entry, dict):
            return parse_money(entry.get("cost", 0))
    return 0.0


def set_cost_override(overrides, *, product_id=None, variant_id=None, title="", price=None, cost=None):
    key = product_cost_key(product_id=product_id, variant_id=variant_id, title=title)
    payload = overrides.get(key, {}) if isinstance(overrides.get(key), dict) else {}
    payload.update(
        {
            "product_id": product_id,
            "variant_id": variant_id,
            "title": title,
            "price": parse_money(price, 0) if price is not None else parse_money(payload.get("price", 0)),
            "cost": parse_money(cost, 0) if cost is not None else parse_money(payload.get("cost", 0)),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    overrides[key] = payload
    return overrides


@app.template_filter("parse_date")
def parse_date_filter(value):
    return parse_date_for_sort(value)


@app.template_filter("format_number")
def format_number(value):
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value)


@app.template_global()
def tag_style(label):
    return _TAG_STYLES.get(label, "background:#e8eaf6;color:#283593")


@app.template_global()
def status_badge(status):
    status = status or ""
    upper = status.upper()
    if "PARTIALLY DELIVERED" in upper:
        bg, color, dot = "#fff0d9", "#8b5a00", "#f6c23e"
    elif "UNDELIVERED" in upper or "RETURN" in upper or "CANCEL" in upper:
        bg, color, dot = "#fce8e6", "#8b1a10", "#e74a3b"
    elif is_delivered_status(status):
        bg, color, dot = "#d4f5e9", "#0f6848", "#1cc88a"
    elif status == "Booked":
        bg, color, dot = "#dde4fb", "#2346a8", "#4e73df"
    elif status == "Un-Booked":
        bg, color, dot = "#ebebed", "#4a4b55", "#858796"
    elif "OUT FOR" in upper or "DISPATCH" in upper or "TRANSIT" in upper:
        bg, color, dot = "#fef8e4", "#7a5c00", "#f6c23e"
    elif "CONFIRM" in upper:
        bg, color, dot = "#d4f5e9", "#0f6848", "#1cc88a"
    elif "CALL NOT" in upper:
        bg, color, dot = "#e8f8fb", "#0a5c6e", "#36b9cc"
    else:
        bg, color, dot = "#e8f8fb", "#0a5c6e", "#36b9cc"

    return Markup(
        f'<span style="display:inline-flex;align-items:center;gap:5px;padding:3px 10px;'
        f'border-radius:99px;font-size:11px;font-weight:600;background:{bg};color:{color};white-space:nowrap;">'
        f'<span style="width:6px;height:6px;border-radius:50%;background:{dot};flex-shrink:0;"></span>'
        f'{status or "—"}</span>'
    )


PENDING_LINE_ITEM_STATUSES = {
    "Booked",
    "Un-Booked",
    "Out For Delivery",
    "Drop Off at Express Center",
    "CONSIGNMENT BOOKED",
    "Picked From Shipper",
    "Call Not Attended",
    "Delay",
    "Hold",
    "Untraceable",
    "Attempt to deliver",
    "Ready To Ship",
    "Packed by seller / warehouse",
}

PAID_FINANCIAL_STATUSES = {"paid", "partially paid", "partially_paid", "partially-paid"}


def is_pending_line_item_status(status):
    normalized = normalize_status_bucket(status)
    return normalized in {"Booked", "Un-Booked"}


def is_need_attention_status(status):
    upper = str(status or "").strip().upper()
    normalized = normalize_status_bucket(status)
    attention_keywords = (
        "UNDELIVERED",
        "CONTACTING CONSIGNEE",
        "MOVED TO ORIGIN BRANCH",
        "RETURN SUBMITTED",
        "RETURN SUBMISSION",
        "BEING RETURN",
        "OUT FOR RETURN",
    )
    return normalized in {"Undelivered", "Being Return"} or any(keyword in upper for keyword in attention_keywords)


def aggregate_order_status(line_items):
    if not line_items:
        return "Un-Booked"

    normalized_statuses = [normalize_status_bucket(item.get("status")) for item in line_items]
    tracking_numbers = {
        str(item.get("tracking_number") or "").strip()
        for item in line_items
        if str(item.get("tracking_number") or "").strip() and str(item.get("tracking_number") or "").strip() != "N/A"
    }
    has_different_tracked_statuses = len(tracking_numbers) > 1 and len(set(normalized_statuses)) > 1
    delivered_count = sum(1 for status in normalized_statuses if status == "Delivered")
    pending_statuses = [status for status in normalized_statuses if is_pending_line_item_status(status)]

    if any(status == "RETURNED TO SHIPPER" for status in normalized_statuses):
        return "RETURNED TO SHIPPER"
    if any(status == "Being Return" for status in normalized_statuses):
        return "Being Return"
    if any(status == "Undelivered" for status in normalized_statuses):
        return "Undelivered"
    if any(is_need_attention_status(item.get("status")) for item in line_items):
        return "Need Attention"
    if has_different_tracked_statuses:
        return "Mixed Status"
    if delivered_count and pending_statuses:
        return "Partially Delivered"
    if delivered_count and delivered_count == len(normalized_statuses):
        return "Delivered"
    if any(status == "Out For Delivery" for status in normalized_statuses):
        return "Out For Delivery"
    if any(status == "Booked" for status in normalized_statuses):
        return "Booked"
    if any(status == "Un-Booked" for status in normalized_statuses):
        return "Un-Booked"
    return normalized_statuses[-1] if normalized_statuses else "Un-Booked"


@app.context_processor
def inject_now():
    return {
        "now": datetime.now(),
        "skip_base_password_prompt": bool(session.get(ADMIN_PORTAL_SESSION_KEY)),
        "embedded_mode": request.args.get("embedded") == "1",
        "daraz_oauth_url": get_daraz_authorize_url(),
    }


def employee_portal_is_authenticated():
    return bool(session.get(EMPLOYEE_PORTAL_SESSION_KEY) or session.get(ADMIN_PORTAL_SESSION_KEY))


def admin_portal_is_authenticated():
    return bool(session.get(ADMIN_PORTAL_SESSION_KEY))


def aghaje_portal_is_authenticated():
    return bool(session.get(AGHAJE_PORTAL_SESSION_KEY))


def employee_portal_safe_next_url(candidate):
    if candidate and str(candidate).startswith("/employee_portal"):
        return candidate
    return url_for("employee_portal")


def setup_shopify():
    shop_url = get_shop_domain() or (os.getenv("SHOP_URL") or "").strip()
    legacy_token = (os.getenv("PASSWORD") or "").strip()
    oauth_token = get_graphql_token()
    token = legacy_token or oauth_token
    api_key = (os.getenv("API_KEY") or "").strip()
    if not shop_url or not token:
        print("SHOP_URL or PASSWORD missing; Shopify client not configured.")
        return

    try:
        shopify.ShopifyResource.clear_session()
    except Exception:
        pass
    if legacy_token:
        base_url = shopify_rest_base_url()
        shopify.ShopifyResource.set_site(base_url)
        if api_key:
            shopify.ShopifyResource.set_user(api_key)
        shopify.ShopifyResource.set_password(legacy_token)
        return

    try:
        session_obj = shopify.Session(shop_url, get_graphql_api_version(), oauth_token)
        shopify.ShopifyResource.activate_session(session_obj)
        return
    except Exception as error:
        print(f"Could not activate Shopify session: {error}")


def shopify_rest_base_url():
    raw_shop_url = (os.getenv("SHOP_URL") or "").strip()
    parsed = urlparse(raw_shop_url if "://" in raw_shop_url else f"https://{raw_shop_url}")
    host = parsed.netloc or parsed.path
    if not host:
        raise RuntimeError("SHOP_URL is not configured.")
    api_version = os.getenv("SHOPIFY_ADMIN_API_VERSION", "2026-04")
    return f"https://{host}/admin/api/{api_version}"


def shopify_rest_headers(prefer_graphql_token=False):
    graphql_token = get_graphql_token()
    legacy_token = (os.getenv("PASSWORD") or "").strip()
    token = (graphql_token or legacy_token) if prefer_graphql_token else (legacy_token or graphql_token)
    if not token:
        raise RuntimeError("Shopify admin token is missing.")
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def async_shopify_fetch(session_obj, resource_path, params=None, prefer_graphql_token=False):
    query = f"?{urlencode(params)}" if params else ""
    url = f"{shopify_rest_base_url()}/{resource_path}{query}"
    async with session_obj.get(url, headers=shopify_rest_headers(prefer_graphql_token=prefer_graphql_token), timeout=30) as response:
        response.raise_for_status()
        return await response.json()


async def fetch_shopify_rest_resource(session_obj, resource_path, params=None, prefer_graphql_token=False):
    return await async_shopify_fetch(
        session_obj,
        resource_path,
        params,
        prefer_graphql_token=prefer_graphql_token,
    )


async def fetch_shopify_paginated_rest(
    session_obj,
    resource_path,
    params=None,
    root_key=None,
    max_pages=10,
    prefer_graphql_token=False,
):
    collected = []
    since_id = None
    params = dict(params or {})
    for _ in range(max_pages):
        page_params = dict(params)
        if since_id:
            page_params["since_id"] = since_id
        payload = await fetch_shopify_rest_resource(
            session_obj,
            resource_path,
            page_params,
            prefer_graphql_token=prefer_graphql_token,
        )
        if not payload:
            break
        rows = payload.get(root_key) if root_key else None
        if rows is None:
            rows = payload.get(resource_path.split(".", 1)[0], [])
        rows = rows or []
        collected.extend(rows)
        if len(rows) < int(page_params.get("limit", 250)):
            break
        since_id = as_dict(rows[-1]).get("id")
        if not since_id:
            break
    return collected


def checkout_has_customer_contact(checkout):
    checkout = as_dict(checkout)
    customer, shipping, billing = get_checkout_customer_blocks(checkout)
    return bool(
        get_checkout_customer_name(checkout, shipping, billing, customer) != "No customer"
        or get_checkout_customer_email(checkout, customer)
        or first_present(get_customer_phone_candidates(checkout, shipping, billing, customer))
        or get_checkout_customer_address(shipping, billing)
    )


def merge_checkout_detail(summary_checkout, detail_checkout):
    summary_checkout = as_dict(summary_checkout)
    detail_checkout = as_dict(detail_checkout)
    merged = dict(summary_checkout)
    for key, value in detail_checkout.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


async def hydrate_shopify_abandoned_checkout(session_obj, checkout):
    checkout = as_dict(checkout)
    if checkout_has_customer_contact(checkout):
        return checkout

    identifiers = [
        checkout.get("id"),
        checkout.get("token"),
        checkout.get("cart_token"),
    ]
    for identifier in identifiers:
        if not identifier:
            continue
        payload = None
        for prefer_graphql_token in (True, False):
            try:
                payload = await fetch_shopify_rest_resource(
                    session_obj,
                    f"checkouts/{identifier}.json",
                    prefer_graphql_token=prefer_graphql_token,
                )
                break
            except Exception as error:
                print(f"Could not hydrate abandoned checkout {identifier}: {error}")
        if not payload:
            continue
        detailed_checkout = as_dict(
            payload.get("checkout")
            or payload.get("abandoned_checkout")
            or payload.get("abandonedCheckout")
            or payload
        )
        merged = merge_checkout_detail(checkout, detailed_checkout)
        if checkout_has_customer_contact(merged):
            return merged
    return checkout


def get_checkout_image_url(line_item):
    image = line_item.get("image_url") or line_item.get("image") or ""
    if isinstance(image, dict):
        return image.get("src") or image.get("url") or ""
    return image or ""


def get_checkout_shipping_total(checkout):
    shipping_lines = checkout.get("shipping_lines") or []
    if isinstance(shipping_lines, dict):
        shipping_lines = [shipping_lines]
    shipping_total = sum(parse_money(as_dict(line).get("price"), 0) for line in shipping_lines)
    if shipping_total:
        return round(shipping_total, 2)

    for key in ("shipping_price", "shipping_rate"):
        value = checkout.get(key)
        candidate = parse_money(value.get("price"), 0) if isinstance(value, dict) else parse_money(value, 0)
        if candidate:
            return candidate

    total = parse_money(checkout.get("total_price"), 0)
    subtotal = parse_money(checkout.get("subtotal_price"), parse_money(checkout.get("total_line_items_price"), 0))
    if total > subtotal:
        return round(total - subtotal, 2)
    return 0.0


def money_bag_to_amount_and_currency(money_bag):
    money_bag = as_dict(money_bag)
    money = as_dict(money_bag.get("presentmentMoney")) or as_dict(money_bag.get("shopMoney"))
    return parse_money(money.get("amount"), 0), (money.get("currencyCode") or "PKR")


def graphql_address_to_rest(address):
    address = as_dict(address)
    return {
        "name": address.get("name") or join_nonempty([address.get("firstName"), address.get("lastName")]),
        "address1": address.get("address1") or "",
        "address2": address.get("address2") or "",
        "city": address.get("city") or "",
        "country": address.get("country") or "",
        "country_code": address.get("countryCodeV2") or address.get("countryCode") or "",
        "phone": address.get("phone") or "",
    }


def graphql_line_item_to_rest(line_item):
    line_item = as_dict(line_item)
    unit_price, _ = money_bag_to_amount_and_currency(
        line_item.get("discountedUnitPriceSet")
        or line_item.get("discountedUnitPriceWithCodeDiscount")
        or line_item.get("originalUnitPriceSet")
    )
    product = as_dict(line_item.get("product"))
    variant = as_dict(line_item.get("variant"))
    image = as_dict(line_item.get("image"))
    return {
        "id": line_item.get("id"),
        "title": line_item.get("title") or as_dict(line_item.get("product")).get("title") or "Product",
        "variant_title": line_item.get("variantTitle") or as_dict(line_item.get("variant")).get("title") or "",
        "quantity": line_item.get("quantity") or 0,
        "price": unit_price,
        "image_url": image.get("url") or image.get("src") or "",
        "product_id": product.get("legacyResourceId"),
        "variant_id": variant.get("legacyResourceId"),
    }


def graphql_abandoned_checkout_to_rest(node):
    node = as_dict(node)
    customer = as_dict(node.get("customer"))
    default_phone = as_dict(customer.get("defaultPhoneNumber")).get("phoneNumber")
    default_email = as_dict(customer.get("defaultEmailAddress")).get("emailAddress")
    default_address = graphql_address_to_rest(customer.get("defaultAddress"))
    customer_payload = {
        "first_name": customer.get("firstName") or "",
        "last_name": customer.get("lastName") or "",
        "email": default_email or "",
        "phone": default_phone or "",
        "number_of_orders": customer.get("numberOfOrders"),
        "default_address": default_address,
    }
    total_price, currency = money_bag_to_amount_and_currency(node.get("totalPriceSet"))
    subtotal_price, subtotal_currency = money_bag_to_amount_and_currency(
        node.get("subtotalPriceSet") or node.get("totalLineItemsPriceSet")
    )
    line_items = [graphql_line_item_to_rest(item) for item in (as_dict(node.get("lineItems")).get("nodes") or [])]
    return {
        "id": node.get("id"),
        "token": node.get("name") or node.get("id"),
        "cart_token": node.get("defaultCursor") or "",
        "created_at": node.get("createdAt") or "",
        "updated_at": node.get("updatedAt") or "",
        "completed_at": node.get("completedAt") or "",
        "abandoned_checkout_url": node.get("abandonedCheckoutUrl") or "",
        "email": default_email or "",
        "phone": default_phone or "",
        "customer": customer_payload,
        "shipping_address": graphql_address_to_rest(node.get("shippingAddress")),
        "billing_address": graphql_address_to_rest(node.get("billingAddress")),
        "line_items": line_items,
        "total_price": total_price,
        "subtotal_price": subtotal_price,
        "total_line_items_price": subtotal_price,
        "currency": currency or subtotal_currency or "PKR",
        "presentment_currency": currency or subtotal_currency or "PKR",
    }


def build_abandoned_checkout_row_from_payload(checkout):
    checkout = as_dict(checkout)
    customer, shipping, billing = get_checkout_customer_blocks(checkout)
    customer_name = get_checkout_customer_name(checkout, shipping, billing, customer)
    created_at = checkout.get("created_at") or checkout.get("createdAt") or ""
    country = get_checkout_country(shipping, billing, checkout)
    country_code = get_checkout_country_code(shipping, billing, checkout, country)
    raw_phone = first_present(get_customer_phone_candidates(checkout, shipping, billing, customer))
    customer_phone = format_customer_phone(raw_phone, country_code)
    customer_email = get_checkout_customer_email(checkout, customer)
    customer_address = get_checkout_customer_address(shipping, billing)
    customer_city = first_present([shipping.get("city"), billing.get("city"), checkout.get("city")])
    currency = checkout.get("presentment_currency") or checkout.get("currency") or "PKR"
    total_price = parse_money(checkout.get("total_price") or checkout.get("totalPrice"), 0)
    subtotal_price = parse_money(
        checkout.get("subtotal_price") or checkout.get("total_line_items_price") or checkout.get("subtotalPrice"),
        total_price,
    )
    shipping_total = get_checkout_shipping_total(checkout)
    checkout_line_items = checkout.get("line_items") or checkout.get("lineItems") or []
    if isinstance(checkout_line_items, dict):
        checkout_line_items = checkout_line_items.get("nodes") or [checkout_line_items]
    items = []
    for line_item in checkout_line_items or []:
        line_item = as_dict(line_item)
        if "variantTitle" in line_item or "discountedUnitPriceSet" in line_item:
            line_item = graphql_line_item_to_rest(line_item)
        quantity = parse_int(line_item.get("quantity"), 0)
        unit_price = parse_money(line_item.get("price", 0))
        title = line_item.get("title") or line_item.get("name") or line_item.get("presentment_title") or "Product"
        variant_title = line_item.get("variant_title") or line_item.get("presentment_variant_title") or ""
        display_title = f"{title} - {variant_title}" if variant_title and variant_title != "Default Title" else title
        line_total = round(unit_price * quantity, 2)
        items.append(
            {
                "title": display_title,
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": line_total,
                "image": get_checkout_image_url(line_item),
                "display_line_total": format_currency_amount(line_total, currency),
                "display_unit_price": format_currency_amount(unit_price, currency),
            }
        )

    token = str(checkout.get("token") or checkout.get("cart_token") or checkout.get("id") or checkout.get("name") or "")
    viewed_tokens = load_abandoned_viewed_tokens()
    return {
        "id": checkout.get("id"),
        "token": token,
        "created_at": created_at,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_phone": customer_phone,
        "customer_city": customer_city,
        "customer_country": country,
        "customer_country_code": country_code,
        "customer_address": customer_address,
        "customer_orders_count": get_checkout_customer_total_orders(checkout, {}),
        "total_price": total_price,
        "subtotal_price": subtotal_price,
        "shipping_charges": shipping_total,
        "currency": currency,
        "display_total": format_currency_amount(total_price, currency),
        "display_subtotal": format_currency_amount(subtotal_price, currency),
        "display_shipping": format_currency_amount(shipping_total, currency),
        "viewed": token in viewed_tokens,
        "abandoned_checkout_url": checkout.get("abandoned_checkout_url") or checkout.get("abandonedCheckoutUrl") or "",
        "recovered": bool(checkout.get("completed_at") or checkout.get("completedAt")),
        "recovered_order_name": "",
        "recovered_order_link": "",
        "completed_at": checkout.get("completed_at") or checkout.get("completedAt") or "",
        "items": items,
        "created_date": str(created_at or "")[:10],
        "whatsapp_url": build_abandoned_whatsapp_url(customer_phone, customer_name),
        "is_today": parse_date_for_sort(created_at).date() == datetime.now().date(),
    }


def fetch_shopify_abandoned_checkouts_graphql(days=7):
    token = get_graphql_token()
    endpoint = get_graphql_endpoint()
    if not token or not endpoint:
        return []

    query_text = f"created_at:>={get_abandoned_created_at_min(days)}"
    query = """
    query AbandonedCheckouts($first: Int!, $after: String, $query: String) {
      abandonedCheckouts(first: $first, after: $after, query: $query, sortKey: CREATED_AT, reverse: true) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          name
          defaultCursor
          abandonedCheckoutUrl
          completedAt
          createdAt
          updatedAt
          customer {
            firstName
            lastName
            numberOfOrders
            defaultEmailAddress {
              emailAddress
            }
            defaultPhoneNumber {
              phoneNumber
            }
            defaultAddress {
              name
              address1
              address2
              city
              country
              countryCodeV2
              phone
            }
          }
          shippingAddress {
            name
            address1
            address2
            city
            country
            countryCodeV2
            phone
          }
          billingAddress {
            name
            address1
            address2
            city
            country
            countryCodeV2
            phone
          }
          subtotalPriceSet {
            shopMoney { amount currencyCode }
            presentmentMoney { amount currencyCode }
          }
          totalLineItemsPriceSet {
            shopMoney { amount currencyCode }
            presentmentMoney { amount currencyCode }
          }
          totalPriceSet {
            shopMoney { amount currencyCode }
            presentmentMoney { amount currencyCode }
          }
          lineItems(first: 50) {
            nodes {
              id
              title
              variantTitle
              quantity
              image {
                url
              }
              discountedUnitPriceSet {
                shopMoney { amount currencyCode }
                presentmentMoney { amount currencyCode }
              }
              originalUnitPriceSet {
                shopMoney { amount currencyCode }
                presentmentMoney { amount currencyCode }
              }
              product {
                legacyResourceId
                title
              }
              variant {
                legacyResourceId
                title
              }
            }
          }
        }
      }
    }
    """
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }
    rows = []
    after = None
    for _ in range(10):
        response = requests.post(
            endpoint,
            json={"query": query, "variables": {"first": 100, "after": after, "query": query_text}},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []
        if errors:
            raise RuntimeError("; ".join(error.get("message", "Unknown Shopify GraphQL error") for error in errors))
        connection = as_dict(as_dict(payload.get("data")).get("abandonedCheckouts"))
        rows.extend(graphql_abandoned_checkout_to_rest(node) for node in (connection.get("nodes") or []))
        page_info = as_dict(connection.get("pageInfo"))
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
    return rows


async def get_checkout_line_item_image(session_obj, line_item, product_cache):
    image_src = get_checkout_image_url(line_item)
    if image_src:
        return image_src

    product_id = line_item.get("product_id")
    variant_id = line_item.get("variant_id")
    if not product_id:
        return ""

    product_id_key = str(product_id)
    if product_id_key not in product_cache:
        product_cache[product_id_key] = None
        try:
            product_data = await async_shopify_fetch(session_obj, f"products/{product_id}.json")
            product_cache[product_id_key] = as_dict(product_data.get("product") if product_data else {})
        except Exception as error:
            print(f"Could not fetch product image for abandoned checkout item {product_id}: {error}")

    product = as_dict(product_cache.get(product_id_key))
    if not product:
        return ""

    if variant_id:
        for variant in product.get("variants", []) or []:
            variant = as_dict(variant)
            if str(variant.get("id")) == str(variant_id) and variant.get("image_id"):
                for image in product.get("images", []) or []:
                    image = as_dict(image)
                    if str(image.get("id")) == str(variant.get("image_id")):
                        return image.get("src") or ""

    return as_dict(product.get("image")).get("src") or ""


def build_abandoned_whatsapp_url(phone, customer_name):
    phone = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not phone:
        return ""
    text = f"Hello {customer_name or ''}, you left items in your cart at The Sleek Space. Would you like help completing your order?"
    return f"https://wa.me/{phone}?{urlencode({'text': text})}"


def load_abandoned_viewed_tokens():
    try:
        return set(json.loads(get_app_setting(ABANDONED_VIEWED_SETTING_KEY, "[]")) or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def save_abandoned_viewed_tokens(tokens):
    cleaned = sorted({str(token) for token in tokens if token})
    return set_app_setting(ABANDONED_VIEWED_SETTING_KEY, json.dumps(cleaned))


def empty_abandoned_summary():
    return {
        "last_7_days": 0,
        "today": 0,
        "recovered": 0,
        "open": 0,
        "viewed": 0,
        "not_viewed": 0,
        "value": 0.0,
    }


def summarize_abandoned_checkout_rows(rows):
    rows = rows or []
    return {
        "last_7_days": len(rows),
        "today": sum(1 for row in rows if row.get("is_today")),
        "recovered": sum(1 for row in rows if row.get("recovered")),
        "open": sum(1 for row in rows if not row.get("recovered")),
        "viewed": sum(1 for row in rows if row.get("viewed")),
        "not_viewed": sum(1 for row in rows if not row.get("viewed")),
        "value": round(sum(parse_money(row.get("total_price", 0)) for row in rows), 2),
    }


def load_abandoned_cache():
    raw = get_app_setting(ABANDONED_CACHE_SETTING_KEY, "")
    if not raw:
        return {"rows": [], "summary": empty_abandoned_summary(), "refreshed_at": 0.0}
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Abandoned cache payload is not an object.")
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else summarize_abandoned_checkout_rows(rows)
        return {
            "rows": rows,
            "summary": {**empty_abandoned_summary(), **summary},
            "refreshed_at": float(payload.get("refreshed_at") or 0),
        }
    except Exception as error:
        print(f"Could not read abandoned checkout cache: {error}")
        return {"rows": [], "summary": empty_abandoned_summary(), "refreshed_at": 0.0}


def save_abandoned_cache(rows, summary=None):
    rows = rows or []
    summary = summary or summarize_abandoned_checkout_rows(rows)
    payload = {
        "rows": rows,
        "summary": summary,
        "refreshed_at": time.time(),
    }
    return set_app_setting(ABANDONED_CACHE_SETTING_KEY, json.dumps(payload, default=str))


def abandoned_cache_is_fresh(cache=None, max_age_seconds=None):
    cache = cache or load_abandoned_cache()
    max_age_seconds = ABANDONED_CACHE_FRESH_SECONDS if max_age_seconds is None else max_age_seconds
    return bool(cache.get("rows")) and (time.time() - float(cache.get("refreshed_at") or 0)) <= max_age_seconds


def refresh_abandoned_checkouts_cache_sync(force=False):
    if not force and abandoned_cache_is_fresh():
        return load_abandoned_cache()
    if not abandoned_cache_refresh_lock.acquire(blocking=False):
        return load_abandoned_cache()
    try:
        rows, summary = asyncio.run(build_abandoned_checkouts_data())
        save_abandoned_cache(rows, summary)
        return {"rows": rows, "summary": summary, "refreshed_at": time.time()}
    except Exception as error:
        print(f"Could not refresh abandoned checkout cache: {error}")
        cache = load_abandoned_cache()
        cache["error"] = str(error)
        return cache
    finally:
        abandoned_cache_refresh_lock.release()


def refresh_abandoned_checkouts_cache_background(force=False):
    if not force and abandoned_cache_is_fresh():
        return
    thread = threading.Thread(
        target=refresh_abandoned_checkouts_cache_sync,
        kwargs={"force": force},
        daemon=True,
    )
    thread.start()


def merge_cached_abandoned_row(row):
    row = as_dict(row)
    token = str(row.get("token") or row.get("cart_token") or row.get("id") or "").strip()
    if not token:
        return False
    cache = load_abandoned_cache()
    rows = cache.get("rows") or []
    merged = False
    for index, existing in enumerate(rows):
        if str(as_dict(existing).get("token") or as_dict(existing).get("id")) == token:
            rows[index] = {**as_dict(existing), **row}
            merged = True
            break
    if not merged:
        rows.append(row)
    rows = sorted(rows, key=lambda current: parse_date_timestamp(as_dict(current).get("created_at")), reverse=True)
    return save_abandoned_cache(rows, summarize_abandoned_checkout_rows(rows))


def mark_abandoned_cache_viewed(token):
    token = str(token or "").strip()
    if not token:
        return False
    cache = load_abandoned_cache()
    rows = cache.get("rows") or []
    changed = False
    for row in rows:
        row = as_dict(row)
        if str(row.get("token") or row.get("id")) == token and not row.get("viewed"):
            row["viewed"] = True
            changed = True
    if changed:
        return save_abandoned_cache(rows, summarize_abandoned_checkout_rows(rows))
    return True


async def fetch_shopify_abandoned_checkouts(days=7):
    try:
        graph_rows = fetch_shopify_abandoned_checkouts_graphql(days)
        if graph_rows:
            return graph_rows
    except Exception as error:
        print(f"Could not fetch abandoned checkouts through Shopify GraphQL: {error}")

    created_at_min = get_abandoned_created_at_min(days)
    seen = {}
    async with aiohttp.ClientSession() as session_obj:
        for status in ("open", "closed"):
            params = {
                "limit": 250,
                "created_at_min": created_at_min,
                "status": status,
            }
            try:
                rows = await fetch_shopify_paginated_rest(
                    session_obj,
                    "checkouts.json",
                    params,
                    "checkouts",
                    prefer_graphql_token=True,
                )
            except Exception as error:
                print(f"Could not fetch protected abandoned checkouts with status={status}: {error}")
                rows = await fetch_shopify_paginated_rest(
                    session_obj,
                    "checkouts.json",
                    params,
                    "checkouts",
                    prefer_graphql_token=False,
                )
            for row in rows:
                row = await hydrate_shopify_abandoned_checkout(session_obj, as_dict(row))
                seen[str(row.get("id") or row.get("token") or row.get("cart_token"))] = row
    return list(seen.values())


async def fetch_recent_shopify_orders_for_recovery(days=30):
    async with aiohttp.ClientSession() as session_obj:
        params = {
            "limit": 250,
            "status": "any",
            "created_at_min": get_abandoned_created_at_min(days),
            "fields": "id,name,created_at,email,phone,total_price,customer,checkout_token,cart_token",
        }
        try:
            return await fetch_shopify_paginated_rest(
                session_obj,
                "orders.json",
                params,
                "orders",
                prefer_graphql_token=True,
            )
        except Exception as error:
            print(f"Could not fetch protected recent orders for abandoned recovery: {error}")
            return await fetch_shopify_paginated_rest(
                session_obj,
                "orders.json",
                params,
                "orders",
                prefer_graphql_token=False,
            )


def build_order_recovery_indexes(orders):
    by_checkout_token = {}
    by_cart_token = {}
    by_email = {}
    by_phone = {}

    for order in orders or []:
        customer = as_dict(order.get("customer"))
        checkout_token = normalize_customer_lookup_value(order.get("checkout_token"))
        cart_token = normalize_customer_lookup_value(order.get("cart_token"))
        email = normalize_customer_lookup_value(order.get("email") or customer.get("email"))
        phone = normalize_customer_phone(order.get("phone") or customer.get("phone"))
        if checkout_token:
            by_checkout_token.setdefault(checkout_token, []).append(order)
        if cart_token:
            by_cart_token.setdefault(cart_token, []).append(order)
        if email:
            by_email.setdefault(email, []).append(order)
        if phone:
            by_phone.setdefault(phone, []).append(order)

    return {
        "checkout_token": by_checkout_token,
        "cart_token": by_cart_token,
        "email": by_email,
        "phone": by_phone,
    }


def find_recovered_order(checkout, indexes):
    checkout_created_at = parse_date_timestamp(checkout.get("created_at"))
    customer = as_dict(checkout.get("customer"))
    token = normalize_customer_lookup_value(checkout.get("token"))
    cart_token = normalize_customer_lookup_value(checkout.get("cart_token"))
    email = normalize_customer_lookup_value(checkout.get("email") or customer.get("email"))
    phone = normalize_customer_phone(checkout.get("phone") or customer.get("phone"))
    candidates = []

    for key, index in (
        (token, indexes.get("checkout_token", {})),
        (cart_token, indexes.get("cart_token", {})),
        (email, indexes.get("email", {})),
        (phone, indexes.get("phone", {})),
    ):
        if key:
            candidates.extend(index.get(key, []))

    unique_candidates = {str(order.get("id")): order for order in candidates if order.get("id")}.values()
    dated_candidates = [
        order for order in unique_candidates
        if parse_date_timestamp(order.get("created_at")) >= checkout_created_at
    ]
    if not dated_candidates:
        return None
    return sorted(dated_candidates, key=lambda order: parse_date_timestamp(order.get("created_at")))[0]


def build_abandoned_checkout_customer_counts(recovery_orders):
    counts = {}
    for order in recovery_orders or []:
        customer = as_dict(order.get("customer"))
        customer_id = str(customer.get("id") or "").strip()
        email = normalize_customer_lookup_value(order.get("email") or customer.get("email"))
        phone = normalize_customer_phone(order.get("phone") or customer.get("phone"))
        for key in (
            f"id:{customer_id}" if customer_id else "",
            f"email:{email}" if email else "",
            f"phone:{phone}" if phone else "",
        ):
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def get_checkout_customer_total_orders(checkout, fallback_counts):
    customer = as_dict(checkout.get("customer"))
    for field in ("orders_count", "order_count", "number_of_orders", "numberOfOrders"):
        if customer.get(field) is not None:
            try:
                return int(customer.get(field) or 0)
            except (TypeError, ValueError):
                pass

    customer_id = str(customer.get("id") or "").strip()
    email = normalize_customer_lookup_value(checkout.get("email") or customer.get("email"))
    phone = normalize_customer_phone(checkout.get("phone") or customer.get("phone"))
    for key in (
        f"id:{customer_id}" if customer_id else "",
        f"email:{email}" if email else "",
        f"phone:{phone}" if phone else "",
    ):
        if key and key in fallback_counts:
            return fallback_counts[key]
    return 0


def get_shopify_admin_store_slug():
    raw_shop_url = (get_shop_domain() or os.getenv("SHOP_URL") or "").strip()
    parsed = urlparse(raw_shop_url if "://" in raw_shop_url else f"https://{raw_shop_url}")
    host = parsed.netloc or parsed.path
    return (host.split(".", 1)[0] or "the-sleek-space").strip("/")


def shopify_order_admin_link(order_id):
    if not order_id:
        return ""
    return f"https://admin.shopify.com/store/{get_shopify_admin_store_slug()}/orders/{order_id}"


async def build_abandoned_checkouts_data(days=7):
    checkouts = await fetch_shopify_abandoned_checkouts(days)
    recovery_orders = await fetch_recent_shopify_orders_for_recovery(max(days, 30))
    recovery_indexes = build_order_recovery_indexes(recovery_orders)
    fallback_counts = build_abandoned_checkout_customer_counts(recovery_orders)
    viewed_tokens = load_abandoned_viewed_tokens()
    today = datetime.now().date()
    rows = []
    product_cache = {}

    async with aiohttp.ClientSession() as session_obj:
        for checkout in checkouts:
            checkout = as_dict(checkout)
            customer, shipping, billing = get_checkout_customer_blocks(checkout)
            recovered_order = find_recovered_order(checkout, recovery_indexes)
            completed_at = checkout.get("completed_at")
            is_recovered = bool(completed_at or recovered_order)
            customer_name = get_checkout_customer_name(checkout, shipping, billing, customer)
            checkout_line_items = checkout.get("line_items", []) or []
            if isinstance(checkout_line_items, dict):
                checkout_line_items = [checkout_line_items]
            items = []
            for line_item in checkout_line_items:
                line_item = as_dict(line_item)
                quantity = parse_int(line_item.get("quantity"), 0)
                unit_price = parse_money(line_item.get("price", 0))
                title = line_item.get("title") or line_item.get("name") or "Product"
                variant_title = line_item.get("variant_title") or ""
                items.append(
                    {
                        "title": f"{title} - {variant_title}" if variant_title and variant_title != "Default Title" else title,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "line_total": round(unit_price * quantity, 2),
                        "image": await get_checkout_line_item_image(session_obj, line_item, product_cache),
                    }
                )

            created_at = checkout.get("created_at", "")
            country = get_checkout_country(shipping, billing, checkout)
            country_code = get_checkout_country_code(shipping, billing, checkout, country)
            raw_phone = first_present(get_customer_phone_candidates(checkout, shipping, billing, customer))
            customer_phone = format_customer_phone(raw_phone, country_code)
            customer_email = get_checkout_customer_email(checkout, customer)
            customer_address = get_checkout_customer_address(shipping, billing)
            customer_city = first_present([shipping.get("city"), billing.get("city"), checkout.get("city")])
            currency = checkout.get("presentment_currency") or checkout.get("currency") or "PKR"
            total_price = parse_money(checkout.get("total_price", 0))
            subtotal_price = parse_money(checkout.get("subtotal_price", checkout.get("total_line_items_price", total_price)))
            shipping_total = get_checkout_shipping_total(checkout)
            for item in items:
                item["display_line_total"] = format_currency_amount(item.get("line_total"), currency)
                item["display_unit_price"] = format_currency_amount(item.get("unit_price"), currency)

            token = str(checkout.get("token") or checkout.get("cart_token") or checkout.get("id") or "")
            rows.append(
                {
                    "id": checkout.get("id"),
                    "token": token,
                    "created_at": created_at,
                    "customer_name": customer_name,
                    "customer_email": customer_email,
                    "customer_phone": customer_phone,
                    "customer_city": customer_city,
                    "customer_country": country,
                    "customer_country_code": country_code,
                    "customer_address": customer_address,
                    "customer_orders_count": get_checkout_customer_total_orders(checkout, fallback_counts),
                    "total_price": total_price,
                    "subtotal_price": subtotal_price,
                    "shipping_charges": shipping_total,
                    "currency": currency,
                    "display_total": format_currency_amount(total_price, currency),
                    "display_subtotal": format_currency_amount(subtotal_price, currency),
                    "display_shipping": format_currency_amount(shipping_total, currency),
                    "viewed": token in viewed_tokens,
                    "abandoned_checkout_url": checkout.get("abandoned_checkout_url") or "",
                    "recovered": is_recovered,
                    "recovered_order_name": as_dict(recovered_order).get("name", "") if recovered_order else "",
                    "recovered_order_link": shopify_order_admin_link(as_dict(recovered_order).get("id")) if recovered_order else "",
                    "completed_at": completed_at or (as_dict(recovered_order).get("created_at") if recovered_order else ""),
                    "items": items,
                    "created_date": str(created_at or "")[:10],
                    "whatsapp_url": build_abandoned_whatsapp_url(customer_phone, customer_name),
                    "is_today": parse_date_for_sort(created_at).date() == today,
                }
            )

    rows = sorted(rows, key=lambda row: parse_date_timestamp(row.get("created_at")), reverse=True)
    summary = summarize_abandoned_checkout_rows(rows)
    return rows, summary


async def build_abandoned_checkouts_summary(days=7):
    checkouts = await fetch_shopify_abandoned_checkouts(days)
    viewed_tokens = load_abandoned_viewed_tokens()
    today = datetime.now().date()
    checkout_tokens = {
        str(as_dict(checkout).get("token") or as_dict(checkout).get("cart_token") or as_dict(checkout).get("id"))
        for checkout in checkouts
    }
    return {
        "last_7_days": len(checkouts),
        "today": sum(1 for checkout in checkouts if parse_date_for_sort(as_dict(checkout).get("created_at")).date() == today),
        "recovered": sum(1 for checkout in checkouts if as_dict(checkout).get("completed_at")),
        "open": sum(1 for checkout in checkouts if not as_dict(checkout).get("completed_at")),
        "viewed": len(checkout_tokens & viewed_tokens),
        "not_viewed": max(len(checkout_tokens - viewed_tokens), 0),
        "value": round(sum(parse_money(as_dict(checkout).get("total_price", 0)) for checkout in checkouts), 2),
    }


def get_abandoned_summary_safe():
    cache = load_abandoned_cache()
    if not abandoned_cache_is_fresh(cache):
        refresh_abandoned_checkouts_cache_background()
    return cache.get("summary") or empty_abandoned_summary()


def fetch_shopify_inventory_item_costs(inventory_item_ids):
    ids = [str(item_id).strip() for item_id in (inventory_item_ids or []) if str(item_id).strip()]
    if not ids:
        return {}
    uncached_ids = [item_id for item_id in ids if item_id not in inventory_item_cost_cache]
    if uncached_ids:
        base_url = shopify_rest_base_url()
        headers = shopify_rest_headers()
        for offset in range(0, len(uncached_ids), 50):
            batch = uncached_ids[offset : offset + 50]
            response = requests.get(
                f"{base_url}/inventory_items.json",
                headers=headers,
                params={"ids": ",".join(batch)},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json().get("inventory_items", []) or []
            for item in payload:
                item_id = str(item.get("id") or "").strip()
                if item_id:
                    inventory_item_cost_cache[item_id] = parse_money(item.get("cost", 0))
            for missing_id in batch:
                inventory_item_cost_cache.setdefault(missing_id, 0.0)
    return {item_id: inventory_item_cost_cache.get(item_id, 0.0) for item_id in ids}


def update_shopify_inventory_item_cost(inventory_item_id, cost):
    if not inventory_item_id:
        return
    base_url = shopify_rest_base_url()
    headers = shopify_rest_headers()
    response = requests.put(
        f"{base_url}/inventory_items/{inventory_item_id}.json",
        headers=headers,
        json={"inventory_item": {"id": int(inventory_item_id), "cost": str(parse_money(cost, 0))}},
        timeout=20,
    )
    response.raise_for_status()
    inventory_item_cost_cache[str(inventory_item_id)] = parse_money(cost, 0)


def get_aghaje_shop_domain():
    return (os.getenv("AGHAJE_SHOP_URL") or os.getenv("AGHAJE_SHOP_DOMAIN") or "").strip()


def aghaje_rest_base_url():
    raw_shop_url = get_aghaje_shop_domain()
    if not raw_shop_url:
        raise RuntimeError("AGHAJE_SHOP_URL is not configured.")
    parsed = urlparse(raw_shop_url if "://" in raw_shop_url else f"https://{raw_shop_url}")
    host = parsed.netloc or parsed.path
    if not host:
        raise RuntimeError("AGHAJE_SHOP_URL is not configured.")
    api_version = os.getenv("AGHAJE_ADMIN_API_VERSION", "2026-04")
    return f"https://{host}/admin/api/{api_version}"


def aghaje_rest_headers():
    token = (os.getenv("AGHAJE_PASSWORD") or os.getenv("AGHAJE_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("AGHAJE_PASSWORD is not configured.")
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_aghaje_webhook_base_url():
    return (
        os.getenv("AJ_WEBHOOK")
        or os.getenv("AGHAJE_WEBHOOK_BASE_URL")
        or os.getenv("SHOPIFY_APP_BASE_URL")
        or ""
    ).strip().rstrip("/")


def verify_aghaje_webhook(req):
    webhook_secret = (os.getenv("AJ_WEBHOOK_SECRET") or os.getenv("SHOPIFY_WEBHOOK_SECRET") or "").strip()
    if not webhook_secret:
        raise ValueError("AJ_WEBHOOK_SECRET or SHOPIFY_WEBHOOK_SECRET is not set.")
    aj_hmac = req.headers.get("X-Shopify-Hmac-Sha256")
    digest = hmac.new(webhook_secret.encode("utf-8"), req.get_data(), hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed_hmac, aj_hmac or "")


def get_aghaje_created_at_min():
    raw_value = (os.getenv("AGHAJE_CREATED_AT_MIN") or "2026-05-31T00:00:00+05:00").strip()
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        print(f"Invalid AGHAJE_CREATED_AT_MIN '{raw_value}', using 2026-05-31T00:00:00+05:00.")
        return "2026-05-31T00:00:00+05:00"


def get_aghaje_fetch_status():
    return (os.getenv("AGHAJE_ORDER_FETCH_STATUS") or "any").strip().lower() or "any"


def parse_shopify_next_link(link_header):
    if not link_header:
        return None
    for part in str(link_header).split(","):
        chunk = part.strip()
        if 'rel="next"' not in chunk:
            continue
        url_part = chunk.split(";", 1)[0].strip()
        if url_part.startswith("<") and url_part.endswith(">"):
            return url_part[1:-1]
    return None


def extract_note_attributes(order):
    notes = {}
    for item in (order or {}).get("note_attributes") or []:
        name = str((item or {}).get("name") or "").strip()
        if name:
            notes[name] = (item or {}).get("value")
    return notes


def get_aghaje_delivery_status(order):
    note_attributes = extract_note_attributes(order)
    raw_status = str(note_attributes.get("hxs_courier_status") or "").strip()
    if raw_status:
        return classify_aghaje_delivery_status(raw_status)

    tracking_number = str(note_attributes.get("hxs_courier_tracking") or "").strip()
    fulfillment_status = str((order or {}).get("fulfillment_status") or "").strip().lower()
    if (order or {}).get("cancelled_at"):
        if tracking_number or fulfillment_status in {"fulfilled", "partial"}:
            return "Returned", "Order cancelled in Shopify after dispatch; courier/order costs remain applied."
        return "Cancelled", "Order cancelled in Shopify."

    if fulfillment_status == "fulfilled":
        return "Inprocess", "Fulfilled in Shopify; courier status not available yet."
    if fulfillment_status == "partial":
        return "Inprocess", "Partially fulfilled in Shopify"
    return "Inprocess", "No courier status yet"


def classify_aghaje_delivery_status(raw_status):
    raw_status = str(raw_status or "").strip()
    upper = raw_status.upper()
    if not raw_status:
        return "Inprocess", "No courier status yet"
    if "UNDELIVERED" in upper or "RETURN" in upper or "REFUS" in upper:
        return "Returned", raw_status
    if is_delivered_status(raw_status):
        return "Delivered", raw_status
    if "CANCEL" in upper:
        return "Cancelled", raw_status
    return "Other status", raw_status


def is_aghaje_fulfilled_or_partial(status):
    normalized = str(status or "").strip().lower().replace("_", " ")
    return normalized in {"fulfilled", "partial", "partially fulfilled"}


def fetch_tracking_data_sync(tracking_number):
    async def run_lookup():
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session_obj:
            return await fetch_tracking_data(session_obj, tracking_number)

    return asyncio.run(run_lookup())


def build_tracking_summary_payload(tracking_number):
    data = fetch_tracking_data_sync(tracking_number)
    summary = summarize_tracking_result(tracking_number, data)
    if tracking_data_is_valid(tracking_number, data):
        remember_tracking_summary(tracking_number, summary)
    events = []
    if is_leopards_tracking(tracking_number):
        packet_list = (data or {}).get("packet_list") or []
        packet = packet_list[0] if packet_list else {}
        for detail in list((packet or {}).get("Tracking Detail") or [])[::-1]:
            title = detail.get("Status") or "Tracking update"
            meta = " ".join(str(part or "").strip() for part in [detail.get("Activity_Date"), detail.get("Activity_Time")] if str(part or "").strip())
            reason = detail.get("Reason") or ""
            events.append({"title": title, "meta": meta, "reason": "" if reason == "N/A" else reason})
    elif isinstance(data, list):
        for detail in list(data)[::-1]:
            title = detail.get("ProcessDescForPortal") or detail.get("ProcessDesc") or "Tracking update"
            meta = detail.get("TransactionDate") or ""
            reason = detail.get("ReasonDesc") or ""
            events.append({"title": title, "meta": meta, "reason": "" if reason == "OK" else reason})
    return {
        "tracking_number": tracking_number,
        "courier": courier_label_for_tracking("", tracking_number) or "Call Courier",
        "status": summary.get("status") or "No tracking status",
        "tracking_observed": tracking_data_is_valid(tracking_number, data),
        "customer": summary.get("name") or "",
        "city": summary.get("city") or "",
        "phone": summary.get("phone") or "",
        "address": summary.get("address") or "",
        "events": events,
    }


def normalize_tracking_summary_cache_entry(entry):
    if not isinstance(entry, dict):
        return None
    summary = entry.get("summary")
    if not isinstance(summary, dict) or not summary.get("status"):
        return None
    try:
        fetched_at = float(entry.get("fetched_at") or time.time())
    except (TypeError, ValueError):
        fetched_at = time.time()
    return {"fetched_at": fetched_at, "summary": summary}


def load_persisted_tracking_summary_cache():
    raw = get_app_setting(TRACKING_SUMMARY_CACHE_SETTING_KEY, "{}")
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    loaded = {}
    for tracking_number, entry in payload.items():
        key = str(tracking_number or "").strip().upper()
        normalized = normalize_tracking_summary_cache_entry(entry)
        if key and normalized:
            loaded[key] = normalized
    return loaded


def ensure_tracking_summary_cache_loaded():
    global tracking_summary_cache_loaded
    if tracking_summary_cache_loaded:
        return
    persisted = load_persisted_tracking_summary_cache()
    if persisted:
        tracking_summary_cache.update(persisted)
    tracking_summary_cache_loaded = True


def persist_tracking_summary_cache():
    payload = {
        key: entry
        for key, entry in tracking_summary_cache.items()
        if normalize_tracking_summary_cache_entry(entry)
    }
    return set_app_setting(TRACKING_SUMMARY_CACHE_SETTING_KEY, json.dumps(payload))


def remember_tracking_summary(tracking_number, summary, fetched_at=None):
    tracking_number = str(tracking_number or "").strip()
    if not tracking_number or not summary or not summary.get("status"):
        return False
    if analytics_enabled():
        from analytics_store import sync_tracking
        from datetime import timezone
        observed = datetime.fromtimestamp(fetched_at or time.time(), timezone.utc)
        sync_tracking(tracking_number, summary, observed)
    ensure_tracking_summary_cache_loaded()
    tracking_summary_cache[tracking_number.upper()] = {
        "fetched_at": fetched_at or time.time(),
        "summary": summary,
    }
    return persist_tracking_summary_cache()


def get_tracking_summary_cached(tracking_number, ttl_seconds=300):
    tracking_number = str(tracking_number or "").strip()
    if not tracking_number:
        return None
    ensure_tracking_summary_cache_loaded()
    cache_key = tracking_number.upper()
    cached = tracking_summary_cache.get(cache_key)
    now = time.time()
    if cached and now - cached.get("fetched_at", 0) < ttl_seconds:
        return cached.get("summary")
    try:
        data = fetch_tracking_data_sync(tracking_number)
        summary = summarize_tracking_result(tracking_number, data)
    except Exception as error:
        print(f"Could not fetch tracking summary for {tracking_number}: {error}")
        return cached.get("summary") if cached else None
    if summary and summary.get("status"):
        tracking_summary_cache[cache_key] = {"fetched_at": time.time(), "summary": summary}
        persist_tracking_summary_cache()
    return summary


def get_tracking_summary_cache_only(tracking_number):
    tracking_number = str(tracking_number or "").strip()
    if not tracking_number:
        return None
    ensure_tracking_summary_cache_loaded()
    cached = tracking_summary_cache.get(tracking_number.upper())
    return cached.get("summary") if cached else None


def normalize_tracking_numbers(tracking_numbers):
    unique_numbers = []
    seen = set()
    for tracking_number in tracking_numbers:
        tracking_number = str(tracking_number or "").strip()
        if not tracking_number or tracking_number == "N/A":
            continue
        key = tracking_number.upper()
        if key in seen:
            continue
        seen.add(key)
        unique_numbers.append(tracking_number)
    return unique_numbers


def refresh_tracking_summaries_sync(
    tracking_numbers,
    *,
    limit=TRACKING_REFRESH_SYNC_LIMIT,
    fresh_seconds=TRACKING_REFRESH_FRESH_SECONDS,
    deadline_seconds=TRACKING_REFRESH_SYNC_DEADLINE_SECONDS,
):
    unique_numbers = normalize_tracking_numbers(tracking_numbers)
    ensure_tracking_summary_cache_loaded()
    now = time.time()
    stale_numbers = []
    for tracking_number in unique_numbers:
        cached = tracking_summary_cache.get(tracking_number.upper())
        if cached and now - cached.get("fetched_at", 0) < fresh_seconds:
            continue
        stale_numbers.append(tracking_number)
        if limit and len(stale_numbers) >= limit:
            break

    async def refresh_all():
        timeout = aiohttp.ClientTimeout(total=TRACKING_REFRESH_PER_SHIPMENT_TIMEOUT_SECONDS)
        semaphore = asyncio.Semaphore(8)
        refreshed_at = time.time()
        updates = {}
        async with aiohttp.ClientSession(timeout=timeout) as session_obj:
            async def refresh_one(tracking_number):
                async with semaphore:
                    try:
                        data = await asyncio.wait_for(
                            fetch_tracking_data(session_obj, tracking_number),
                            timeout=TRACKING_REFRESH_PER_SHIPMENT_TIMEOUT_SECONDS,
                        )
                        summary = summarize_tracking_result(tracking_number, data)
                    except Exception as error:
                        print(f"Could not refresh tracking summary for {tracking_number}: {error}")
                        return None
                    if not tracking_data_is_valid(tracking_number, data) or not summary or not summary.get("status"):
                        return None
                    return tracking_number.upper(), summary

            tasks = [asyncio.create_task(refresh_one(number)) for number in stale_numbers]
            done, pending = await asyncio.wait(tasks, timeout=deadline_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            refreshed_count = 0
            for task in done:
                try:
                    result = task.result()
                    if not result:
                        continue
                    cache_key, summary = result
                    updates[cache_key] = {
                        "fetched_at": refreshed_at,
                        "summary": summary,
                    }
                    refreshed_count += 1
                except Exception as error:
                    print(f"Could not complete tracking refresh task: {error}")
            if updates:
                if analytics_enabled():
                    from analytics_store import sync_tracking
                    from datetime import timezone
                    for number, entry in updates.items():
                        sync_tracking(number, entry["summary"], datetime.fromtimestamp(entry["fetched_at"], timezone.utc))
                tracking_summary_cache.update(updates)
                persist_tracking_summary_cache()
            if pending:
                print(f"Tracking refresh deadline hit; skipped {len(pending)} pending shipment(s).")
            return refreshed_count

    if not stale_numbers:
        return 0
    return asyncio.run(refresh_all())


def start_tracking_summaries_background_refresh(
    tracking_numbers,
    *,
    limit=TRACKING_REFRESH_BACKGROUND_LIMIT,
    fresh_seconds=TRACKING_REFRESH_FRESH_SECONDS,
    deadline_seconds=TRACKING_REFRESH_BACKGROUND_DEADLINE_SECONDS,
):
    unique_numbers = normalize_tracking_numbers(tracking_numbers)
    if not unique_numbers:
        return False

    def run_background_refresh():
        if not tracking_refresh_lock.acquire(blocking=False):
            return
        try:
            refresh_tracking_summaries_sync(
                unique_numbers,
                limit=limit,
                fresh_seconds=fresh_seconds,
                deadline_seconds=deadline_seconds,
            )
        finally:
            tracking_refresh_lock.release()

    threading.Thread(target=run_background_refresh, daemon=True).start()
    return True


def is_final_aghaje_delivery_status(status):
    normalized = str(status or "").strip().lower()
    return normalized in {"delivered", "returned", "cancelled"}


def get_active_aghaje_tracking_numbers(orders):
    return normalize_tracking_numbers(
        order.get("tracking_number")
        for order in orders or []
        if not is_final_aghaje_delivery_status(order.get("delivery_status"))
    )


def start_aghaje_active_tracking_auto_refresh(orders):
    tracking_numbers = get_active_aghaje_tracking_numbers(orders)
    if not tracking_numbers:
        return False
    ensure_tracking_summary_cache_loaded()
    now = time.time()
    tracking_numbers = [
        tracking_number
        for tracking_number in tracking_numbers
        if now - (tracking_summary_cache.get(tracking_number.upper()) or {}).get("fetched_at", 0) >= AGHAJE_AUTO_TRACK_FRESH_SECONDS
    ]
    if not tracking_numbers:
        return False
    return start_tracking_summaries_background_refresh(
        tracking_numbers,
        fresh_seconds=AGHAJE_AUTO_TRACK_FRESH_SECONDS,
    )


def aghaje_item_cost_key(product_id=None, variant_id=None, title=""):
    product_id = str(product_id or "").strip()
    variant_id = str(variant_id or "").strip()
    normalized_title = " ".join(str(title or "").strip().lower().split())
    if variant_id:
        return f"variant:{variant_id}"
    if product_id:
        return f"product:{product_id}"
    return f"title:{normalized_title}"


def _aghaje_product_cache_key(product_id):
    return str(product_id or "").strip()


def fetch_aghaje_product_details(product_id):
    cache_key = _aghaje_product_cache_key(product_id)
    if not cache_key:
        return None
    if cache_key in aghaje_product_cache:
        return aghaje_product_cache[cache_key]

    base_url = aghaje_rest_base_url()
    headers = aghaje_rest_headers()
    response = requests.get(f"{base_url}/products/{cache_key}.json", headers=headers, timeout=30)
    response.raise_for_status()
    product = (response.json() or {}).get("product") or {}

    images = list(product.get("images") or [])
    base_image = ""
    if product.get("image") and isinstance(product.get("image"), dict):
        base_image = product["image"].get("src") or ""

    variant_map = {}
    for variant in product.get("variants") or []:
        variant_id = str((variant or {}).get("id") or "").strip()
        variant_title = str((variant or {}).get("title") or "").strip()
        image_src = base_image or "/static/sleekspace-wordmark.svg"
        image_id = str((variant or {}).get("image_id") or "").strip()
        if image_id:
            for image in images:
                if str((image or {}).get("id") or "").strip() == image_id:
                    image_src = (image or {}).get("src") or image_src
                    break
        variant_map[variant_id] = {
            "variant_title": "" if variant_title in {"", "Default Title"} else variant_title,
            "inventory_item_id": (variant or {}).get("inventory_item_id"),
            "image_src": image_src,
        }

    payload = {
        "product_title": product.get("title", ""),
        "image": base_image or "/static/sleekspace-wordmark.svg",
        "variants": variant_map,
    }
    aghaje_product_cache[cache_key] = payload
    return payload


def fetch_aghaje_inventory_item_costs(inventory_item_ids):
    ids = [str(item_id).strip() for item_id in (inventory_item_ids or []) if str(item_id).strip()]
    if not ids:
        return {}

    uncached_ids = [item_id for item_id in ids if item_id not in aghaje_inventory_item_cost_cache]
    if uncached_ids:
        base_url = aghaje_rest_base_url()
        headers = aghaje_rest_headers()
        for offset in range(0, len(uncached_ids), 50):
            batch = uncached_ids[offset : offset + 50]
            response = requests.get(
                f"{base_url}/inventory_items.json",
                headers=headers,
                params={"ids": ",".join(batch)},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json().get("inventory_items", []) or []
            for item in payload:
                item_id = str(item.get("id") or "").strip()
                if item_id:
                    aghaje_inventory_item_cost_cache[item_id] = parse_money(item.get("cost", 0))
            for missing_id in batch:
                aghaje_inventory_item_cost_cache.setdefault(missing_id, 0.0)
    return {item_id: aghaje_inventory_item_cost_cache.get(item_id, 0.0) for item_id in ids}


def fetch_aghaje_orders(created_at_min, fetch_status):
    collected = []
    base_url = aghaje_rest_base_url()
    headers = aghaje_rest_headers()
    fields = ",".join(
        [
            "id",
            "name",
            "created_at",
            "updated_at",
            "financial_status",
            "fulfillment_status",
            "current_subtotal_price",
            "current_total_price",
            "subtotal_price",
            "total_price",
            "total_discounts",
            "total_shipping_price_set",
            "line_items",
            "customer",
            "billing_address",
            "shipping_address",
            "note_attributes",
            "fulfillments",
            "tags",
            "cancelled_at",
            "closed_at",
        ]
    )

    url = f"{base_url}/orders.json"
    params = {
        "limit": 250,
        "order": "created_at DESC",
        "created_at_min": created_at_min,
        "status": fetch_status,
        "fields": fields,
    }

    while url:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        collected.extend(response.json().get("orders", []) or [])
        url = parse_shopify_next_link(response.headers.get("Link"))
        params = None

    print(
        f"Fetched {len(collected)} Aghaje orders with status={fetch_status}, created_at_min={created_at_min}."
    )
    return collected


def load_aghaje_order_sync_state():
    return load_aghaje_order_overrides() or {}


def build_aghaje_orders_page_data():
    overrides = load_aghaje_order_sync_state()
    item_cost_overrides = load_aghaje_item_cost_overrides()
    order_item_cost_overrides = load_aghaje_order_item_cost_overrides()
    net_payment_received_entries = get_aghaje_net_payment_received_entries()
    created_at_min = get_aghaje_created_at_min()
    fetch_status = get_aghaje_fetch_status()
    try:
        raw_orders = fetch_aghaje_orders(created_at_min, fetch_status)
    except Exception as error:
        print(f"Could not fetch Aghaje orders: {error}")
        return [], {"total_orders": 0, "total_items_qty": 0, "net_payment": 0.0}, str(error)

    active_tracking_candidates = []
    for raw_order in raw_orders:
        note_attributes = extract_note_attributes(raw_order)
        tracking_number = str(note_attributes.get("hxs_courier_tracking") or "").strip()
        if not tracking_number:
            continue
        delivery_status, _ = get_aghaje_delivery_status(raw_order)
        if not is_final_aghaje_delivery_status(delivery_status):
            active_tracking_candidates.append(tracking_number)
    refresh_tracking_summaries_sync(
        active_tracking_candidates,
        limit=TRACKING_REFRESH_SYNC_LIMIT,
        fresh_seconds=AGHAJE_AUTO_TRACK_FRESH_SECONDS,
        deadline_seconds=TRACKING_REFRESH_SYNC_DEADLINE_SECONDS,
    )

    results = []
    shop_domain = get_aghaje_shop_domain()
    parsed_shop = urlparse(shop_domain if "://" in shop_domain else f"https://{shop_domain}") if shop_domain else None
    shop_name = ((parsed_shop.netloc or parsed_shop.path) if parsed_shop else "").split(".")[0]

    for order in raw_orders:
        order_id = order.get("name") or f"#{order.get('order_number', '')}"
        note_attributes = extract_note_attributes(order)
        delivery_status, delivery_detail = get_aghaje_delivery_status(order)
        tracking_number = str(note_attributes.get("hxs_courier_tracking") or "").strip()
        courier_name = courier_label_for_tracking(note_attributes.get("hxs_courier_name"), tracking_number)
        tracking_url = str(note_attributes.get("hxs_courier_url") or "").strip()
        courier_tracking_ready = bool(tracking_number)
        live_summary = None
        if tracking_number:
            live_summary = get_tracking_summary_cache_only(tracking_number)
        if live_summary and live_summary.get("status"):
            delivery_status, delivery_detail = classify_aghaje_delivery_status(live_summary.get("status"))
            if is_leopards_tracking(tracking_number):
                delivery_detail = f"Leopards: {delivery_detail}"
        customer = order.get("customer") or {}
        shipping = order.get("shipping_address") or {}
        billing = order.get("billing_address") or {}
        customer_name = (customer.get("first_name") or "").strip()
        customer_name = f"{customer_name} {(customer.get('last_name') or '').strip()}".strip()
        customer_name = customer_name or shipping.get("name") or billing.get("name") or "No customer name"

        item_rows = []
        item_qty_total = 0
        use_original_item_quantities = bool(order.get("cancelled_at"))
        for line_item in order.get("line_items", []) or []:
            quantity = active_shopify_line_item_quantity(
                line_item,
                use_original_quantity=use_original_item_quantities,
            )
            if quantity <= 0:
                continue
            product_id = line_item.get("product_id")
            variant_id = line_item.get("variant_id")
            try:
                product_payload = fetch_aghaje_product_details(product_id) if product_id else None
            except Exception as error:
                print(f"Could not fetch Aghaje product {product_id}: {error}")
                product_payload = None
            variant_payload = (product_payload or {}).get("variants", {}).get(str(variant_id).strip(), {})
            image_src = variant_payload.get("image_src") or (product_payload or {}).get("image") or "/static/sleekspace-wordmark.svg"
            variant_title = variant_payload.get("variant_title") or ""

            unit_price = parse_money(line_item.get("price", 0))
            product_title = line_item.get("title") or line_item.get("name") or "Product"
            display_title = f"{product_title} - {variant_title}" if variant_title and variant_title != "Default Title" else product_title
            cost_key = aghaje_item_cost_key(product_id=product_id, variant_id=variant_id, title=display_title)
            default_item_cost_override = item_cost_overrides.get(cost_key) or {}
            order_item_cost_override = (order_item_cost_overrides.get(order_id) or {}).get(cost_key) or {}
            unit_cost = parse_money(
                order_item_cost_override.get(
                    "cost",
                    default_item_cost_override.get("cost", 0),
                )
            )
            default_unit_cost = parse_money(default_item_cost_override.get("cost", 0))

            item_rows.append(
                {
                    "cost_key": cost_key,
                    "product_id": product_id,
                    "variant_id": variant_id,
                    "title": display_title,
                    "image": image_src,
                    "qty": quantity,
                    "unit_price": unit_price,
                    "line_price": round(unit_price * quantity, 2),
                    "unit_cost": unit_cost,
                    "default_unit_cost": default_unit_cost,
                    "has_order_cost_override": bool(order_item_cost_override),
                    "line_cost": round(unit_cost * quantity, 2),
                }
            )
            item_qty_total += quantity

        raw_fulfillment_status = str(order.get("fulfillment_status") or "").strip().lower()
        if raw_fulfillment_status == "fulfilled":
            fulfillment_status_label = "Fulfilled"
        elif raw_fulfillment_status == "partial":
            fulfillment_status_label = "Partially fulfilled"
        else:
            fulfillment_status_label = "Unfulfilled"
        is_lahore_order = is_aghaje_fulfilled_or_partial(raw_fulfillment_status) and not tracking_number

        results.append(
            {
                "order_id": order_id,
                "shopify_order_id": order.get("id"),
                "shopify_link": f"https://admin.shopify.com/store/{shop_name}/orders/{order.get('id')}" if shop_name else "#",
                "created_at": order.get("created_at", ""),
                "fulfillment_status": fulfillment_status_label,
                "fulfillment_status_raw": raw_fulfillment_status,
                "customer_name": customer_name,
                "customer_phone": shipping.get("phone") or billing.get("phone") or (customer.get("phone") or ""),
                "customer_city": shipping.get("city") or billing.get("city") or "",
                "customer_address": shipping.get("address1") or billing.get("address1") or "",
                "courier_name": courier_name,
                "tracking_number": tracking_number,
                "tracking_url": tracking_url,
                "courier_tracking_ready": courier_tracking_ready,
                "is_lahore": is_lahore_order,
                "items": item_rows,
                "item_cost": 0.0,
                "item_qty": item_qty_total,
                "packaging_cost": 0.0,
                "delivery_cost": 0.0,
                "amount_received": 0.0,
                "payment_status": "Not Payable" if delivery_status == "Cancelled" else "Pending",
                "delivery_status": delivery_status,
                "delivery_status_detail": delivery_detail,
                "price": parse_money(order.get("current_subtotal_price", order.get("subtotal_price", 0))),
                "order_total": parse_money(order.get("current_total_price", order.get("total_price", 0))),
                "payable": 0.0,
                "financial_status": (order.get("financial_status") or "").title(),
                "tags": [tag.strip() for tag in str(order.get("tags") or "").split(",") if tag.strip()],
                "note_attributes": note_attributes,
                "cancelled_at": order.get("cancelled_at"),
            }
        )

    net_payment = 0.0
    total_amount_received = 0.0
    total_cost = 0.0
    total_items_qty = 0
    for order in results:
        total_items_qty += int(order.get("item_qty") or 0)
        order_item_cost = 0.0
        for item in order.get("items", []):
            quantity = int(item.get("qty") or 0)
            order_item_cost += item["line_cost"]

        order_id = str(order.get("order_id") or "")
        override = overrides.get(order_id) or {}
        override_updated_at = override.get("updated_at")
        override_delivery_status = str(override.get("delivery_status") or "").strip()
        if override_delivery_status:
            delivery_status = override_delivery_status
        elif order.get("courier_tracking_ready"):
            delivery_status = str(order.get("delivery_status") or "Inprocess").strip() or "Inprocess"
        else:
            delivery_status = str(order.get("delivery_status") or "Inprocess").strip() or "Inprocess"
        packaging_cost = parse_money(override.get("packaging_cost", order.get("packaging_cost", 0)))
        delivery_cost = parse_money(override.get("delivery_cost", order.get("delivery_cost", 0)))
        financial_status = str(order.get("financial_status") or "").strip().lower()
        amount_received = parse_money(override.get("amount_received", 0.0))
        default_payment_status = "Not Payable" if delivery_status == "Cancelled" else "Pending"
        payment_status = str(override.get("payment_status") or default_payment_status).strip() or default_payment_status
        is_postex = is_postex_courier(order.get("courier_name"))

        if financial_status in PAID_FINANCIAL_STATUSES and "amount_received" not in override:
            amount_received = 0.0

        if is_postex:
            amount_received = 0.0
            delivery_cost = 0.0

        if delivery_status == "Cancelled":
            payment_status = "Not Payable"
            amount_received = 0.0
            packaging_cost = 0.0
            delivery_cost = 0.0

        payable = 0.0 if payment_status == "Not Payable" or delivery_status == "Cancelled" else round(amount_received - order_item_cost - packaging_cost - delivery_cost, 2)
        order["item_cost"] = round(order_item_cost, 2)
        order["packaging_cost"] = round(packaging_cost, 2)
        order["delivery_cost"] = round(delivery_cost, 2)
        order["amount_received"] = round(amount_received, 2)
        order["amount_received_updated_at"] = override_updated_at
        order["payment_status"] = payment_status
        order["delivery_status"] = delivery_status
        order["is_postex"] = is_postex
        order["payable"] = round(payable, 2)
        net_payment += order["payable"]
        total_amount_received += order["amount_received"]
        if delivery_status != "Cancelled" and is_aghaje_fulfilled_or_partial(order.get("fulfillment_status_raw")):
            total_cost += order["item_cost"] + order["packaging_cost"] + order["delivery_cost"]

    total_cash_paid = round(sum(parse_money(entry.get("amount", 0)) for entry in net_payment_received_entries), 2)
    total_cod = round(total_amount_received, 2)
    results.sort(key=lambda row: parse_date_for_sort(row.get("created_at")), reverse=True)
    summary = {
        "total_orders": len(results),
        "total_items_qty": total_items_qty,
        "total_cost": round(total_cost, 2),
        "total_cod": total_cod,
        "total_cash_paid": total_cash_paid,
        "balance": round(total_cod + total_cash_paid - total_cost, 2),
        "net_payment": round(net_payment, 2),
        "net_payment_received": total_cash_paid,
        "net_payment_received_auto": total_cod,
        "net_payment_received_entries": net_payment_received_entries,
    }
    summary["daily_ledger"] = build_aghaje_daily_balance_ledger(results, net_payment_received_entries)
    return results, summary, ""


def is_shopify_unpaid_order(order):
    financial_status = str(order.get("financial_status") or "").strip().lower()
    return financial_status not in PAID_FINANCIAL_STATUSES


def sum_shopify_unpaid_value(orders):
    return round(
        sum(parse_money(order.get("order_total", 0)) for order in orders if is_shopify_unpaid_order(order)),
        2,
    )


def sum_aghaje_delivered_order_total(orders):
    return round(
        sum(parse_money(order.get("order_total", order.get("price", 0))) for order in orders),
        2,
    )


def build_aghaje_orders_total_row(orders):
    totals = {
        "count": len(orders),
        "item_qty": 0,
        "item_cost": 0.0,
        "packaging_cost": 0.0,
        "delivery_cost": 0.0,
        "price": 0.0,
        "order_total": 0.0,
        "amount_received": 0.0,
        "payable": 0.0,
    }
    for order in orders:
        totals["item_qty"] += sum(int(item.get("qty") or 0) for item in order.get("items", []))
        for field in (
            "item_cost",
            "packaging_cost",
            "delivery_cost",
            "price",
            "order_total",
            "amount_received",
            "payable",
        ):
            totals[field] += parse_money(order.get(field, 0))
    return {key: round(value, 2) if isinstance(value, float) else value for key, value in totals.items()}


def fulfilled_status_filter_bucket(order):
    delivery_status = str(order.get("delivery_status") or "").strip()
    detail = str(order.get("delivery_status_detail") or "").upper()
    if delivery_status == "Delivered":
        return "delivered"
    if "OUT FOR DELIVERY" in detail:
        return "out-for-delivery"
    undelivered_terms = (
        "UNDELIVERED",
        "CONTACTING CONSIGNEE",
        "MOVED TO ORIGIN",
        "RETURN SUBMITTED",
        "BEING RETURN",
        "OUT FOR RETURN",
        "RETURNED",
        "REFUS",
    )
    if delivery_status == "Returned" or any(term in detail for term in undelivered_terms):
        return "undelivered"
    return "other"


def build_aghaje_portal_page_data():
    orders, summary, error_message = build_aghaje_orders_page_data()
    orders = [order for order in orders if str(order.get("delivery_status") or "").strip() != "Cancelled"]
    fulfilled_orders = []
    closed_orders = []
    unfulfilled_orders = []
    delivery_counts = {"Delivered": 0, "Returned": 0, "Cancelled": 0, "Other status": 0, "Inprocess": 0}
    payment_counts = {"Paid": 0, "Pending": 0, "Not Payable": 0}

    for order in orders:
        payment_status = str(order.get("payment_status") or "Pending").strip() or "Pending"
        raw_fulfillment = str(order.get("fulfillment_status_raw") or "").strip().lower()
        if payment_status == "Paid":
            closed_orders.append(order)
        elif is_aghaje_fulfilled_or_partial(raw_fulfillment):
            order["fulfilled_filter_status"] = fulfilled_status_filter_bucket(order)
            fulfilled_orders.append(order)
        else:
            unfulfilled_orders.append(order)

        delivery_status = str(order.get("delivery_status") or "Inprocess").strip() or "Inprocess"
        if delivery_status not in delivery_counts:
            delivery_counts["Other status"] += 1
        else:
            delivery_counts[delivery_status] += 1

        if payment_status not in payment_counts:
            payment_counts["Pending"] += 1
        else:
            payment_counts[payment_status] += 1

    total_cost = round(
        sum(
            parse_money(order.get("item_cost", 0)) + parse_money(order.get("packaging_cost", 0)) + parse_money(order.get("delivery_cost", 0))
            for order in orders
            if is_aghaje_fulfilled_or_partial(order.get("fulfillment_status_raw"))
        ),
        2,
    )
    total_cod = round(sum(parse_money(order.get("amount_received", 0)) for order in orders), 2)
    total_cash_paid = parse_money(summary.get("net_payment_received", 0))
    balance = round(total_cod + total_cash_paid - total_cost, 2)
    fulfilled_filter_orders = {
        "delivered": [order for order in fulfilled_orders if order.get("fulfilled_filter_status") == "delivered"],
        "out_for_delivery": [order for order in fulfilled_orders if order.get("fulfilled_filter_status") == "out-for-delivery"],
        "in_transit": [order for order in fulfilled_orders if order.get("fulfilled_filter_status") != "delivered"],
    }
    closed_received_value = round(sum(parse_money(order.get("amount_received", 0)) for order in closed_orders), 2)
    portal_summary = {
        **summary,
        "total_orders": len(orders),
        "fulfilled_orders": len(fulfilled_orders),
        "closed_orders": len(closed_orders),
        "unfulfilled_orders": len(unfulfilled_orders),
        "delivered_orders": delivery_counts["Delivered"],
        "returned_orders": delivery_counts["Returned"],
        "cancelled_orders": delivery_counts["Cancelled"],
        "other_status_orders": delivery_counts["Other status"],
        "paid_orders": payment_counts["Paid"],
        "pending_orders": payment_counts["Pending"],
        "not_payable_orders": payment_counts["Not Payable"],
        "total_cost": total_cost,
        "total_cod": total_cod,
        "total_cash_paid": total_cash_paid,
        "net_payment_received": total_cash_paid,
        "net_payment_received_auto": total_cod,
        "balance": balance,
        "fulfilled_unpaid_value": sum_shopify_unpaid_value(fulfilled_orders),
        "unfulfilled_unpaid_value": sum_shopify_unpaid_value(unfulfilled_orders),
        "closed_received_value": closed_received_value,
        "fulfilled_filter_values": {
            "delivered": sum_aghaje_delivered_order_total(fulfilled_filter_orders["delivered"]),
            "out_for_delivery": sum_shopify_unpaid_value(fulfilled_filter_orders["out_for_delivery"]),
            "in_transit": sum_shopify_unpaid_value(fulfilled_filter_orders["in_transit"]),
        },
    }
    return {
        "orders": orders,
        "fulfilled_orders": fulfilled_orders,
        "closed_orders": closed_orders,
        "unfulfilled_orders": unfulfilled_orders,
        "summary": portal_summary,
        "error_message": error_message,
    }


def ensure_required_shopify_webhooks():
    base_url = shopify_rest_base_url()
    headers = shopify_rest_headers()
    app_base_url = (os.getenv("SHOPIFY_APP_BASE_URL") or "").rstrip("/")
    if not app_base_url:
        raise RuntimeError("SHOPIFY_APP_BASE_URL is not configured.")

    desired = {
        "orders/create": f"{app_base_url}/shopify/webhook/order_created",
        "orders/updated": f"{app_base_url}/shopify/webhook/order_updated",
        "checkouts/create": f"{app_base_url}/shopify/webhook/checkout_created",
        "checkouts/update": f"{app_base_url}/shopify/webhook/checkout_updated",
    }
    if analytics_enabled():
        desired.update({
            "orders/cancelled": f"{app_base_url}/shopify/webhook/order_cancelled",
            "fulfillments/create": f"{app_base_url}/shopify/webhook/fulfillment_updated",
            "fulfillments/update": f"{app_base_url}/shopify/webhook/fulfillment_updated",
            "refunds/create": f"{app_base_url}/shopify/webhook/refund_created",
        })

    response = requests.get(f"{base_url}/webhooks.json", headers=headers, timeout=20)
    response.raise_for_status()
    existing_hooks = response.json().get("webhooks", []) or []
    existing_pairs = {
        (
            str((hook or {}).get("topic") or "").strip().lower(),
            str((hook or {}).get("address") or "").strip().rstrip("/"),
        )
        for hook in existing_hooks
    }

    for topic, address in desired.items():
        if (topic.lower(), address.rstrip("/")) in existing_pairs:
            continue
        payload = {
            "webhook": {
                "topic": topic,
                "address": address,
                "format": "json",
            }
        }
        create_response = requests.post(f"{base_url}/webhooks.json", headers=headers, json=payload, timeout=20)
        create_response.raise_for_status()


def ensure_required_aghaje_webhooks():
    base_url = aghaje_rest_base_url()
    headers = aghaje_rest_headers()
    app_base_url = get_aghaje_webhook_base_url()
    if not app_base_url:
        raise RuntimeError("AJ_WEBHOOK is not configured.")

    desired = {
        "orders/create": f"{app_base_url}/aghaje/webhook/order_created",
        "orders/updated": f"{app_base_url}/aghaje/webhook/order_updated",
    }

    response = requests.get(f"{base_url}/webhooks.json", headers=headers, timeout=20)
    response.raise_for_status()
    existing_hooks = response.json().get("webhooks", []) or []
    existing_pairs = {
        (
            str((hook or {}).get("topic") or "").strip().lower(),
            str((hook or {}).get("address") or "").strip().rstrip("/"),
        )
        for hook in existing_hooks
    }

    for topic, address in desired.items():
        if (topic.lower(), address.rstrip("/")) in existing_pairs:
            continue
        payload = {
            "webhook": {
                "topic": topic,
                "address": address,
                "format": "json",
            }
        }
        create_response = requests.post(f"{base_url}/webhooks.json", headers=headers, json=payload, timeout=20)
        create_response.raise_for_status()


async def fetch_tracking_data(session_obj, tracking_number):
    if not tracking_number or tracking_number == "N/A":
        return {}
    if is_leopards_tracking(tracking_number):
        api_key = os.getenv("LEOPARD_API_KEY")
        api_password = os.getenv("LEOPARD_PASSWORD") or os.getenv("LEOPARD_API_PASSWORD")
        url = (
            "https://merchantapi.leopardscourier.com/api/trackBookedPacket/"
            f"?api_key={api_key}&api_password={api_password}&track_numbers={tracking_number}"
        )
    else:
        url = f"https://cod.callcourier.com.pk/api/CallCourier/GetTackingHistory?cn={tracking_number}"

    async with session_obj.get(url) as response:
        return await response.json()


def get_variant_image_and_title(product, line_item):
    cache_key = (getattr(product, "id", None), getattr(line_item, "variant_id", None))
    if cache_key in product_image_cache:
        return product_image_cache[cache_key]

    line_item_variant_id = str(getattr(line_item, "variant_id", "") or "").strip()
    line_item_image_id = str(getattr(line_item, "image_id", "") or "").strip()
    base_image = ""
    if getattr(product, "image", None):
        base_image = getattr(product.image, "src", "") or ""

    variant_name = ""
    image_src = base_image or "/static/sleekspace-wordmark.svg"
    inventory_item_id = None
    images = list(getattr(product, "images", []) or [])
    fetched_images = False

    if line_item_image_id and images:
        for image in images:
            if str(getattr(image, "id", "") or "").strip() == line_item_image_id:
                image_src = getattr(image, "src", "") or image_src
                break

    for variant in getattr(product, "variants", []) or []:
        if str(getattr(variant, "id", "") or "").strip() != line_item_variant_id:
            continue
        variant_name = "" if getattr(variant, "title", "") in {"", "Default Title"} else getattr(variant, "title", "")
        inventory_item_id = getattr(variant, "inventory_item_id", None)
        variant_image_id = getattr(variant, "image_id", None)
        if variant_image_id and not images and not fetched_images:
            try:
                images = list(shopify.Image.find(product_id=getattr(product, "id", None)) or [])
            except Exception:
                images = []
            fetched_images = True
        if variant_image_id and images:
            for image in images:
                if str(getattr(image, "id", "") or "").strip() == str(variant_image_id).strip():
                    image_src = getattr(image, "src", "") or image_src
                    break
                attached_variant_ids = getattr(image, "variant_ids", None) or []
                if str(getattr(variant, "id", "") or "").strip() in {str(v).strip() for v in attached_variant_ids}:
                    image_src = getattr(image, "src", "") or image_src
                    break
        break

    product_image_cache[cache_key] = (image_src, variant_name, inventory_item_id)
    return image_src, variant_name, inventory_item_id


def tracking_data_is_valid(tracking_number, data):
    if is_leopards_tracking(tracking_number):
        packets = data.get("packet_list", []) if isinstance(data, dict) else []
        return bool(packets and (packets[0].get("booked_packet_status") or packets[0].get("Tracking Detail")))
    return bool(isinstance(data, list) and data and data[-1].get("ProcessDescForPortal"))


def summarize_tracking_result(tracking_number, data):
    if not tracking_number or tracking_number == "N/A":
        return {
            "status": "Un-Booked",
            "name": "",
            "address": "",
            "phone": "",
            "city": "",
        }

    if is_leopards_tracking(tracking_number):
        packet_list = (data or {}).get("packet_list") or []
        if not packet_list:
            return {"status": "Booked", "name": "", "address": "", "phone": "", "city": ""}
        packet = packet_list[0]
        tracking_details = packet.get("Tracking Detail") or []
        final_status = packet.get("booked_packet_status") or "Booked"
        if tracking_details:
            last_tracking = tracking_details[-1]
            final_status = last_tracking.get("Status") or final_status
            reason = last_tracking.get("Reason") or ""
            if reason and reason != "N/A":
                final_status = f"{final_status} - {reason}"
        if final_status in {"Pickup Request not Send", "Pickup Request Sent"}:
            final_status = "Booked"
        return {
            "status": final_status,
            "name": packet.get("consignment_name_eng") or "",
            "address": packet.get("consignment_address") or "",
            "phone": packet.get("consignment_phone") or "",
            "city": packet.get("destination_city_name") or "",
        }

    if isinstance(data, list) and data:
        first = data[0]
        last = data[-1]
        status = last.get("ProcessDescForPortal") or "Booked"
        return {
            "status": status,
            "name": first.get("ConsigneeName") or "",
            "address": first.get("ConsigneeAddress") or "",
            "phone": first.get("ContactNo") or "",
            "city": first.get("ConsigneeCity") or "",
        }
    return {"status": "Booked", "name": "", "address": "", "phone": "", "city": ""}


async def process_line_item(session_obj, line_item, fulfillments):
    active_quantity = active_shopify_line_item_quantity(line_item)
    if active_quantity <= 0:
        return []

    tracking_info = []
    remaining_quantity = active_quantity

    if line_item.fulfillment_status == "fulfilled":
        for fulfillment in fulfillments:
            if remaining_quantity <= 0:
                break
            if getattr(fulfillment, "status", "") == "cancelled":
                continue
            for item in getattr(fulfillment, "line_items", []) or []:
                if remaining_quantity <= 0:
                    break
                if getattr(item, "id", None) != getattr(line_item, "id", None):
                    continue
                fulfilled_quantity = parse_int(getattr(item, "quantity", active_quantity), active_quantity)
                quantity = min(max(fulfilled_quantity, 0), remaining_quantity)
                if quantity <= 0:
                    continue
                tracking_number = getattr(fulfillment, "tracking_number", "") or "N/A"
                try:
                    data = await fetch_tracking_data(session_obj, tracking_number)
                except Exception as error:
                    print(f"Error fetching tracking for {tracking_number}: {error}")
                    data = {}
                summary = summarize_tracking_result(tracking_number, data)
                tracking_info.append(
                    {
                        "tracking_number": tracking_number,
                        "courier_name": courier_label_for_tracking("", tracking_number) or "Call Courier",
                        "status": summary["status"],
                        "tracking_observed": tracking_data_is_valid(tracking_number, data),
                        "quantity": quantity,
                        "name": summary["name"],
                        "address": summary["address"],
                        "city": summary["city"],
                        "phone": summary["phone"],
                    }
                )
                remaining_quantity -= quantity

    if tracking_info:
        return tracking_info

    return [
        {
            "tracking_number": "N/A",
            "courier_name": "",
            "status": "Un-Booked",
            "name": "",
            "address": "",
            "phone": "",
            "city": "",
            "quantity": active_quantity,
        }
    ]


async def process_order(session_obj, order):
    global LAST_REQUEST_TIME

    elapsed_time = time.time() - LAST_REQUEST_TIME
    if elapsed_time < 1 / RATE_LIMIT:
        await asyncio.sleep((1 / RATE_LIMIT) - elapsed_time)
    LAST_REQUEST_TIME = time.time()

    created_at_raw = getattr(order, "created_at", "") or ""
    created_at_display = created_at_raw
    try:
        created_at_display = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass

    fulfillment_status = getattr(order, "fulfillment_status", None)
    order_status = fulfillment_status.title() if fulfillment_status else "Un-fulfilled"

    billing = getattr(order, "billing_address", None)
    shipping = getattr(order, "shipping_address", None)
    customer_details = {
        "name": getattr(billing, "name", None) or getattr(shipping, "name", None) or "",
        "address": getattr(billing, "address1", None) or getattr(shipping, "address1", None) or "",
        "city": getattr(billing, "city", None) or getattr(shipping, "city", None) or "",
        "phone": getattr(billing, "phone", None) or getattr(shipping, "phone", None) or getattr(order, "phone", "") or "",
    }

    original_total_price = parse_money(getattr(order, "total_price", 0))
    original_subtotal_price = parse_money(getattr(order, "subtotal_price", 0))
    current_total_price = current_shopify_order_money(order, "current_total_price", "total_price")
    current_subtotal_price = current_shopify_order_money(order, "current_subtotal_price", "subtotal_price")
    current_total_discounts = current_shopify_order_money(order, "current_total_discounts", "total_discounts")

    order_info = {
        "order_link": f"https://admin.shopify.com/store/{get_shop_domain().split('.')[0]}/orders/{order.id}",
        "order_id": getattr(order, "name", ""),
        "tracking_id": "N/A",
        "created_at": created_at_display,
        "total_price": current_total_price,
        "subtotal_price": current_subtotal_price,
        "current_total_price": current_total_price,
        "current_subtotal_price": current_subtotal_price,
        "original_total_price": original_total_price,
        "original_subtotal_price": original_subtotal_price,
        "shipping_charges": parse_money(getattr(order, "total_shipping_price_set", {}).get("shop_money", {}).get("amount", 0) if isinstance(getattr(order, "total_shipping_price_set", None), dict) else 0),
        "total_discounts": current_total_discounts,
        "line_items": [],
        "financial_status": (getattr(order, "financial_status", "") or "").title(),
        "fulfillment_status": order_status,
        "customer_details": customer_details,
        "tags": [tag for tag in (getattr(order, "tags", "") or "").split(", ") if tag],
        "id": getattr(order, "id", None),
        "cancelled_at": getattr(order, "cancelled_at", None),
        "closed_at": getattr(order, "closed_at", None),
    }

    tasks = [process_line_item(session_obj, line_item, getattr(order, "fulfillments", [])) for line_item in getattr(order, "line_items", [])]
    results = await asyncio.gather(*tasks)

    for tracking_info_list, line_item in zip(results, getattr(order, "line_items", [])):
        if tracking_info_list is None:
            continue
        variant_name = ""
        image_src = "https://cdn.shopify.com/s/files/1/0936/0949/2789/files/7.png?v=1741033934"
        if getattr(line_item, "product_id", None):
            try:
                product = shopify.Product.find(getattr(line_item, "product_id", None))
                if product:
                    image_src, variant_name, inventory_item_id = get_variant_image_and_title(product, line_item)
                else:
                    inventory_item_id = None
            except Exception as error:
                print(f"Could not fetch product image for {getattr(line_item, 'product_id', None)}: {error}")
                inventory_item_id = None
        else:
            inventory_item_id = None

        base_title = getattr(line_item, "title", "") or "Product"
        product_title = base_title if not variant_name else f"{base_title} - {variant_name}"
        unit_price = parse_money(getattr(line_item, "price", 0))
        unit_cost = 0.0

        for info in tracking_info_list:
            quantity = int(info["quantity"] or 0)
            line_total = round(unit_price * quantity, 2)
            line_cost_total = round(unit_cost * quantity, 2)
            order_info["line_items"].append(
                {
                    "fulfillment_status": getattr(line_item, "fulfillment_status", None),
                    "image_src": image_src,
                    "product_title": product_title,
                    "product_id": getattr(line_item, "product_id", None),
                    "variant_id": getattr(line_item, "variant_id", None),
                    "inventory_item_id": inventory_item_id,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "unit_cost": unit_cost,
                    "line_total": line_total,
                    "line_cost_total": line_cost_total,
                    "tracking_number": info["tracking_number"],
                    "courier_name": info.get("courier_name", ""),
                    "status": info["status"],
                    "tracking_observed": info.get("tracking_observed", False),
                    "name": info.get("name", ""),
                    "address": info.get("address", ""),
                    "city": info.get("city", ""),
                    "phone": info.get("phone", ""),
                }
            )
    order_info["status"] = aggregate_order_status(order_info["line_items"])

    if analytics_enabled():
        from analytics_store import sync_order, sync_tracking
        sync_order(order.to_dict(), source="shopify_sync")
        for item in order_info["line_items"]:
            number = item.get("tracking_number")
            if number and number != "N/A" and item.get("tracking_observed"):
                sync_tracking(number, {"status": item["status"]})

    return order_info


async def limited_request(coroutine, semaphore_obj):
    async with semaphore_obj:
        await asyncio.sleep(0.5)
        return await coroutine


def enrich_orders_with_protected_customer_data(orders):
    if not orders:
        return orders
    protected_details, errors = fetch_protected_order_details(
        [order.get("id") for order in orders if order.get("id")]
    )
    for error in errors:
        print(f"Shopify protected data warning: {error}")
    for order in orders:
        order_id = str(order.get("id") or "")
        if not order_id or order_id not in protected_details:
            continue
        details = protected_details[order_id]
        customer = order.setdefault("customer_details", {})
        customer["name"] = details.get("name") or customer.get("name", "")
        customer["address"] = details.get("address") or customer.get("address", "")
        customer["city"] = details.get("city") or customer.get("city", "")
        customer["phone"] = details.get("phone") or customer.get("phone", "")
        for item in order.get("line_items", []):
            item["name"] = details.get("name") or item.get("name", "")
            item["address"] = details.get("address") or item.get("address", "")
            item["city"] = details.get("city") or item.get("city", "")
            item["phone"] = details.get("phone") or item.get("phone", "")
    return orders


def enrich_orders_with_shopify_costs(orders):
    if not orders:
        return orders
    overrides = load_product_cost_overrides()
    inventory_ids = []
    for order in orders:
        for item in order.get("line_items", []):
            inventory_id = item.get("inventory_item_id")
            if inventory_id:
                inventory_ids.append(str(inventory_id))
    cost_map = {}
    if inventory_ids:
        try:
            cost_map = fetch_shopify_inventory_item_costs(inventory_ids)
        except Exception as error:
            print(f"Could not fetch Shopify inventory costs: {error}")

    for order in orders:
        for item in order.get("line_items", []):
            inventory_id = str(item.get("inventory_item_id") or "").strip()
            fallback_cost = get_cost_override_for_item(
                overrides,
                product_id=item.get("product_id"),
                variant_id=item.get("variant_id"),
                title=item.get("product_title", ""),
            )
            unit_cost = cost_map.get(inventory_id, fallback_cost) if inventory_id else fallback_cost
            quantity = int(item.get("quantity") or 0)
            item["unit_cost"] = parse_money(unit_cost, 0)
            item["line_cost_total"] = round(parse_money(unit_cost, 0) * quantity, 2)
    return orders


def sort_orders_newest_first(orders):
    return sorted(
        orders or [],
        key=lambda order: parse_date_for_sort(order.get("created_at")),
        reverse=True,
    )


def get_shopify_created_at_min():
    raw_value = (os.getenv("SHOPIFY_CREATED_AT_MIN") or "2024-09-01T00:00:00+00:00").strip()
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        print(f"Invalid SHOPIFY_CREATED_AT_MIN '{raw_value}', using 2024-09-01T00:00:00+00:00.")
        return "2024-09-01T00:00:00+00:00"


def get_shopify_fetch_status():
    return (os.getenv("SHOPIFY_ORDER_FETCH_STATUS") or "open").strip().lower() or "open"


def fetch_all_shopify_orders(created_at_min, fetch_status):
    collected = []

    try:
        orders = shopify.Order.find(
            limit=250,
            order="created_at DESC",
            created_at_min=created_at_min,
            status=fetch_status,
        )
    except Exception as error:
        print(f"Error fetching Shopify orders: {error}")
        return None

    page_count = 0
    while True:
        page_orders = list(orders)
        page_count += 1
        collected.extend(page_orders)
        try:
            if not orders.has_next_page():
                break
            orders = orders.next_page()
        except Exception as error:
            print(f"Error loading next page: {error}")
            break

    print(
        f"Fetched {len(collected)} Shopify orders across {page_count} page(s) "
        f"with status={fetch_status}, created_at_min={created_at_min}."
    )
    return collected


async def get_shopify_orders(force_status=None):
    created_at_min = get_shopify_created_at_min()
    fetch_status = (force_status or get_shopify_fetch_status() or "open").strip().lower() or "open"
    fetched_orders = fetch_all_shopify_orders(created_at_min, fetch_status)
    if fetched_orders is None:
        return None

    collected = []
    request_semaphore = asyncio.Semaphore(2)
    async with aiohttp.ClientSession() as session_obj:
        tasks = [limited_request(process_order(session_obj, order), request_semaphore) for order in fetched_orders]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                print(f"Error processing Shopify order: {result}")
                continue
            if not should_keep_order_in_active_list(result):
                continue
            collected.append(result)

    collected = enrich_orders_with_protected_customer_data(collected)
    collected = enrich_orders_with_shopify_costs(collected)
    return sort_orders_newest_first(collected)


def merge_order_refresh(existing_orders, refreshed_orders):
    merged = {}
    for order in existing_orders or []:
        key = str(order.get("id") or order.get("order_id") or "").strip()
        if key:
            merged[key] = order
    for order in refreshed_orders or []:
        key = str(order.get("id") or order.get("order_id") or "").strip()
        if key:
            merged[key] = order
    return sort_orders_newest_first(list(merged.values()))


def reload_orders(preserve_existing_on_partial=False):
    global order_details
    try:
        setup_shopify()
        fetched_orders = asyncio.run(get_shopify_orders())
        if fetched_orders is None:
            print("Keeping existing order cache because Shopify fetch failed.")
            return False
        if preserve_existing_on_partial and order_details and len(fetched_orders) < len(order_details):
            print(
                f"Partial Shopify refresh returned {len(fetched_orders)} orders; "
                f"keeping {len(order_details)} cached orders and merging refreshed tracking data."
            )
            order_details = merge_order_refresh(order_details, fetched_orders)
            return False
        order_details = sort_orders_newest_first(fetched_orders)
        return True
    except Exception as error:
        print(f"Could not reload orders: {error}")
        return False


def find_shopify_order_by_order_name(order_id):
    normalized = normalize_scan_term(order_id)
    for order in order_details:
        if normalize_scan_term(order.get("order_id")) == normalized:
            return order
    return None


def find_shopify_order_by_tracking_number(tracking_number):
    normalized = normalize_scan_term(tracking_number)
    if not normalized:
        return None
    for order in order_details:
        for item in order.get("line_items", []):
            if normalize_scan_term(item.get("tracking_number")) == normalized:
                return order
    return None


def serialize_shopify_order_for_employee(order):
    customer = order.get("customer_details") or {}
    return {
        "source": "shopify",
        "source_label": "Sleek Space",
        "shopify_id": order.get("id"),
        "order_id": str(order.get("order_id", "")),
        "status": order.get("status", ""),
        "customer_name": customer.get("name", ""),
        "customer_phone": customer.get("phone", ""),
        "customer_city": customer.get("city", ""),
        "total_price": order.get("total_price", 0),
        "created_at": order.get("created_at", ""),
        "items": [
            {
                "title": item.get("product_title", ""),
                "quantity": item.get("quantity", 0),
                "image": item.get("image_src", ""),
                "tracking_number": item.get("tracking_number", "N/A"),
                "status": item.get("status", ""),
            }
            for item in order.get("line_items", [])
        ],
    }


def serialize_daraz_order_for_employee(order):
    customer = order.get("customer") or {}
    return {
        "source": "daraz",
        "source_label": "Daraz",
        "shopify_id": None,
        "order_id": str(order.get("order_id", "")),
        "status": order.get("status", ""),
        "customer_name": customer.get("name", ""),
        "customer_phone": customer.get("phone", ""),
        "customer_city": "",
        "total_price": order.get("total_price", 0),
        "created_at": order.get("created_at") or order.get("date", ""),
        "items": [
            {
                "title": item.get("item_title", ""),
                "quantity": item.get("quantity", 0),
                "image": item.get("item_image", ""),
                "tracking_number": item.get("tracking_number", "N/A"),
                "status": item.get("status", ""),
            }
            for item in order.get("items_list", [])
        ],
    }


def serialize_aghaje_order_for_employee(order):
    return {
        "source": "aghaje",
        "source_label": "AghaJe",
        "shopify_id": order.get("shopify_order_id"),
        "order_id": str(order.get("order_id", "")),
        "status": order.get("delivery_status") or order.get("fulfillment_status") or "",
        "customer_name": order.get("customer_name", ""),
        "customer_phone": order.get("customer_phone", ""),
        "customer_city": order.get("customer_city", ""),
        "total_price": order.get("order_total", 0),
        "created_at": order.get("created_at", ""),
        "items": [
            {
                "title": item.get("title", ""),
                "quantity": item.get("qty", 0),
                "image": item.get("image", ""),
                "tracking_number": order.get("tracking_number") or "N/A",
                "status": order.get("delivery_status_detail") or order.get("delivery_status", ""),
            }
            for item in order.get("items", [])
        ],
    }


def build_aghaje_employee_portal_orders():
    try:
        orders, _, error_message = build_aghaje_orders_page_data()
    except Exception as error:
        print(f"Could not load AghaJe employee portal orders: {error}")
        return []
    if error_message:
        print(f"Could not load AghaJe employee portal orders: {error_message}")
        return []
    return [serialize_aghaje_order_for_employee(order) for order in orders]


def build_employee_portal_orders():
    refresh_daraz_cache_if_needed()
    employee_orders = [serialize_shopify_order_for_employee(order) for order in order_details]
    employee_orders.extend(serialize_daraz_order_for_employee(order) for order in daraz_orders_cache)
    employee_orders.extend(build_aghaje_employee_portal_orders())
    return sorted(
        employee_orders,
        key=lambda order: parse_date_for_sort(order.get("created_at")),
        reverse=True,
    )


def build_safe_employee_portal_orders():
    refresh_daraz_cache_if_needed()
    employee_orders = []
    try:
        employee_orders = [serialize_shopify_order_for_employee(order) for order in order_details]
    except Exception as error:
        print(f"Could not load Sleek Space employee portal orders: {error}")

    try:
        employee_orders.extend(serialize_daraz_order_for_employee(order) for order in daraz_orders_cache)
    except Exception as error:
        print(f"Could not load Daraz employee portal orders: {error}")

    try:
        employee_orders.extend(build_aghaje_employee_portal_orders())
    except Exception as error:
        print(f"Could not load AghaJe employee portal orders: {error}")

    try:
        return sorted(
            employee_orders,
            key=lambda order: parse_date_for_sort(order.get("created_at")),
            reverse=True,
        )
    except Exception as error:
        print(f"Could not sort employee portal orders: {error}")
        return employee_orders


def apply_aghaje_order_tag(order_id, tag, include_date=False):
    order_id = str(order_id or "").strip()
    if not order_id:
        return False
    clean_tag = tag.strip()
    if include_date:
        clean_tag = f"{clean_tag} ({datetime.now().strftime('%Y-%m-%d')})"

    base_url = aghaje_rest_base_url()
    headers = aghaje_rest_headers()
    response = requests.get(f"{base_url}/orders/{order_id}.json", headers=headers, params={"fields": "id,tags"}, timeout=20)
    response.raise_for_status()
    order = response.json().get("order") or {}
    tags = [item.strip() for item in str(order.get("tags") or "").split(",") if item.strip()]
    if clean_tag not in tags:
        tags.append(clean_tag)
    update_response = requests.put(
        f"{base_url}/orders/{order_id}.json",
        headers=headers,
        json={"order": {"id": order_id, "tags": ", ".join(tags)}},
        timeout=20,
    )
    update_response.raise_for_status()
    return True


def apply_employee_portal_order_tag(source, order_id, tag_name):
    source = str(source or "").strip().lower()
    if source == "aghaje":
        return apply_aghaje_order_tag(order_id, tag_name, include_date=True)
    if source in {"shopify", "sleekspace", "sleek-space"}:
        return apply_shopify_order_tag(order_id, tag_name, include_date=True)
    return False


def employee_portal_source_label(source):
    source = str(source or "").strip().lower()
    if source == "aghaje":
        return "AghaJe"
    if source == "daraz":
        return "Daraz"
    if source in {"shopify", "sleekspace", "sleek-space"}:
        return "Sleek Space"
    return source or "Unknown"


def build_pending_orders_mobile_data():
    refresh_daraz_cache_if_needed()
    all_orders = []
    statuses = load_order_statuses()

    for daraz_order in daraz_orders_cache:
        if daraz_order.get("status") not in {"Ready To Ship", "Pending", "Packed", "Packed by seller / warehouse"}:
            continue
        items = []
        for item in daraz_order.get("items_list", []):
            track_num = item.get("tracking_number", "N/A")
            key = f"{daraz_order.get('order_id')}:{track_num}"
            normalized_item = dict(item)
            normalized_item.update(
                {
                    "product_id": None,
                    "variant_id": None,
                    "unit_price": 0,
                    "unit_cost": 0,
                    "line_total": 0,
                    "line_cost_total": 0,
                    "applied_status": statuses.get(key, ""),
                }
            )
            items.append(normalized_item)
        if not items:
            continue
        customer = daraz_order.get("customer") or {}
        total_price = parse_money(daraz_order.get("total_price", 0))
        all_orders.append(
            {
                "order_via": "Daraz",
                "shopify_id": None,
                "order_link": None,
                "order_id": daraz_order.get("order_id"),
                "status": daraz_order.get("status", ""),
                "tags": [],
                "customer_name": customer.get("name", ""),
                "customer_phone": customer.get("phone", ""),
                "customer_address": customer.get("address", ""),
                "customer_city": "",
                "is_lahore": False,
                "date": daraz_order.get("created_at") or daraz_order.get("date", ""),
                "items_list": items,
                "financial_status": "pending",
                "payment_status_label": "Pending",
                "payment_status_class": "pending",
                "subtotal_price": total_price,
                "current_subtotal_price": total_price,
                "shipping_charges": 0,
                "total_discounts": 0,
                "total_price": total_price,
                "current_total_price": total_price,
                "display_total_price": total_price,
                "pending_total_price": total_price,
                "pending_total_cost": 0,
            }
        )

    for shopify_order in order_details:
        if any(tag.startswith("Dispatched") for tag in shopify_order.get("tags", [])):
            continue
        order_status = shopify_order.get("status", "")
        filtered_tags = [tag.strip() for tag in shopify_order.get("tags", []) if tag and tag.strip() != "Leopards Courier"]
        customer_city = ((shopify_order.get("customer_details") or {}).get("city") or "").strip()
        items = []
        for item in shopify_order.get("line_items", []):
            item_status = normalize_status_bucket(item.get("status", ""))
            if not is_pending_line_item_status(item_status):
                continue
            track_num = item.get("tracking_number", "N/A")
            key = f"{shopify_order['order_id']}:{track_num}"
            items.append(
                {
                    "item_image": item.get("image_src", ""),
                    "item_title": item.get("product_title", ""),
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "quantity": item.get("quantity", 0),
                    "unit_price": item.get("unit_price", 0),
                    "unit_cost": item.get("unit_cost", 0),
                    "line_total": item.get("line_total", 0),
                    "line_cost_total": item.get("line_cost_total", 0),
                    "tracking_number": track_num,
                    "status": item_status,
                    "applied_status": statuses.get(key, ""),
                }
            )
        if not items:
            continue
        pending_total_price = round(sum(parse_money(item.get("line_total", 0)) for item in items), 2)
        pending_total_cost = round(sum(parse_money(item.get("line_cost_total", 0)) for item in items), 2)
        financial_status = str(shopify_order.get("financial_status", "") or "").strip().lower()
        payment_label = "Pending"
        payment_class = "pending"
        if financial_status in PAID_FINANCIAL_STATUSES:
            payment_label = "Partially Paid" if "partially" in financial_status else "Paid"
            payment_class = "partial" if "partially" in financial_status else "paid"
        all_orders.append(
            {
                "order_via": "Shopify",
                "shopify_id": shopify_order.get("id"),
                "order_link": shopify_order.get("order_link"),
                "order_id": shopify_order.get("order_id"),
                "status": normalize_status_bucket(order_status),
                "tags": filtered_tags,
                "customer_name": (shopify_order.get("customer_details") or {}).get("name", ""),
                "customer_phone": (shopify_order.get("customer_details") or {}).get("phone", ""),
                "customer_address": (shopify_order.get("customer_details") or {}).get("address", ""),
                "customer_city": customer_city,
                "is_lahore": is_lahore_city(customer_city),
                "date": shopify_order.get("created_at", ""),
                "items_list": items,
                "financial_status": shopify_order.get("financial_status", ""),
                "payment_status_label": payment_label,
                "payment_status_class": payment_class,
                "subtotal_price": parse_money(shopify_order.get("subtotal_price", 0)),
                "current_subtotal_price": parse_money(shopify_order.get("current_subtotal_price", shopify_order.get("subtotal_price", 0))),
                "shipping_charges": parse_money(shopify_order.get("shipping_charges", 0)),
                "total_discounts": parse_money(shopify_order.get("total_discounts", 0)),
                "total_price": parse_money(shopify_order.get("total_price", 0)),
                "current_total_price": parse_money(shopify_order.get("current_total_price", shopify_order.get("current_subtotal_price", shopify_order.get("total_price", 0)))),
                "display_total_price": parse_money(shopify_order.get("current_subtotal_price", shopify_order.get("subtotal_price", shopify_order.get("total_price", 0)))),
                "pending_total_price": pending_total_price,
                "pending_total_cost": pending_total_cost,
            }
        )

    all_orders.sort(key=lambda order: parse_date_for_sort(order.get("date")), reverse=True)
    return all_orders


def build_pending_items_table_data():
    pending_items = {}
    all_orders = []
    paid_pending_value = 0.0
    unpaid_pending_value = 0.0
    total_items_cost = 0.0
    for order in build_pending_orders_mobile_data():
        all_orders.append(order)
        financial_status = str(order.get("financial_status", "") or "").strip().lower()
        pending_value = parse_money(order.get("pending_total_price", 0))
        pending_cost = parse_money(order.get("pending_total_cost", 0))
        total_items_cost += pending_cost
        if financial_status in PAID_FINANCIAL_STATUSES:
            paid_pending_value += pending_value
        else:
            unpaid_pending_value += pending_value
        for item in order.get("items_list", []):
            product_title = item["item_title"]
            quantity = int(item.get("quantity") or 0)
            item_image = item.get("item_image", "")
            key = product_title
            if key not in pending_items:
                pending_items[key] = {
                    "item_image": item_image,
                    "item_title": product_title,
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "unit_price": parse_money(item.get("unit_price", 0)),
                    "unit_cost": parse_money(item.get("unit_cost", 0)),
                    "quantity": 0,
                    "total_price": 0.0,
                    "total_cost": 0.0,
                    "statuses": {},
                }
            pending_items[key]["quantity"] += quantity
            pending_items[key]["total_price"] += parse_money(item.get("line_total", 0))
            pending_items[key]["total_cost"] += parse_money(item.get("line_cost_total", 0))
            status = item.get("status", "")
            pending_items[key]["statuses"][status] = pending_items[key]["statuses"].get(status, 0) + quantity
    pending_items_sorted = sorted(pending_items.values(), key=lambda item: item["quantity"], reverse=True)
    for item in pending_items_sorted:
        item["total_price"] = round(parse_money(item.get("total_price", 0)), 2)
        item["total_cost"] = round(parse_money(item.get("total_cost", 0)), 2)
    summary = {
        "paid_pending_value": round(paid_pending_value, 2),
        "unpaid_pending_value": round(unpaid_pending_value, 2),
        "total_items_cost": round(total_items_cost, 2),
    }
    return all_orders, pending_items_sorted, summary


def build_employee_approval_items():
    approvals = []
    statuses = load_order_statuses()
    approval_statuses = {"Delivered in Lahore", "Cancelled by Employee"}
    for shopify_order in order_details:
        customer = shopify_order.get("customer_details") or {}
        for item in shopify_order.get("line_items", []):
            tracking_number = item.get("tracking_number", "N/A")
            key = f"{shopify_order['order_id']}:{tracking_number}"
            applied_status = statuses.get(key, "")
            if applied_status not in approval_statuses:
                continue
            approvals.append(
                {
                    "shopify_id": shopify_order.get("id"),
                    "order_id": shopify_order.get("order_id"),
                    "tracking_number": tracking_number,
                    "requested_status": applied_status,
                    "item_title": item.get("product_title", ""),
                    "item_image": item.get("image_src", ""),
                    "quantity": item.get("quantity", 0),
                    "customer_name": customer.get("name") or item.get("name") or "",
                    "customer_city": customer.get("city") or item.get("city") or "",
                    "customer_phone": customer.get("phone") or item.get("phone") or "",
                    "total_price": shopify_order.get("total_price", 0),
                    "date": shopify_order.get("created_at", ""),
                    "tags": shopify_order.get("tags", []),
                }
            )
    approvals.sort(key=lambda item: parse_date_for_sort(item.get("date")), reverse=True)
    return approvals


def find_employee_portal_order(term):
    normalized = normalize_scan_term(term)
    if not normalized:
        return None
    for order in build_safe_employee_portal_orders():
        if normalize_scan_term(order.get("order_id")) == normalized:
            return order
        for item in order.get("items", []):
            if normalize_scan_term(item.get("tracking_number")) == normalized:
                return order
    return None


def apply_shopify_order_tag(order_id, tag, include_date=False):
    order = shopify.Order.find(order_id)
    tags = [t.strip() for t in order.tags.split(",")] if getattr(order, "tags", "") else []
    clean_tag = tag.strip()
    if include_date:
        clean_tag = f"{clean_tag} ({datetime.now().strftime('%Y-%m-%d')})"
    if clean_tag not in tags:
        tags.append(clean_tag)
    order.tags = ", ".join(tags)
    return order.save()


def remove_shopify_order_tag(order_id, tag):
    order = shopify.Order.find(order_id)
    target = str(tag or "").strip().lower()
    tags = [item.strip() for item in str(getattr(order, "tags", "") or "").split(",") if item.strip()]
    remaining_tags = [item for item in tags if item.lower() != target]
    if remaining_tags == tags:
        return True
    order.tags = ", ".join(remaining_tags)
    return order.save()


def mark_shopify_order_as_paid(order_id):
    token = get_graphql_token()
    endpoint = get_graphql_endpoint()
    if not token or not endpoint:
        raise RuntimeError("Shopify GraphQL payment auth is not configured.")

    mutation = """
    mutation MarkOrderAsPaid($input: OrderMarkAsPaidInput!) {
      orderMarkAsPaid(input: $input) {
        order {
          id
          displayFinancialStatus
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {"input": {"id": f"gid://shopify/Order/{int(order_id)}"}}
    response = requests.post(
        endpoint,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        json={"query": mutation, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("errors") or []
    if errors:
        raise RuntimeError("; ".join(error.get("message", "Unknown Shopify GraphQL error") for error in errors))
    result = (payload.get("data") or {}).get("orderMarkAsPaid") or {}
    user_errors = result.get("userErrors") or []
    if user_errors:
        raise RuntimeError("; ".join(error.get("message", "Unknown Shopify user error") for error in user_errors))
    return result.get("order") or {}


def capture_shopify_payment(order):
    financial_status = ((getattr(order, "financial_status", "") or "")).lower()
    if financial_status in {"paid", "partially_paid"}:
        return []
    response = requests.get(
        f"{shopify_rest_base_url()}/orders/{order.id}/transactions.json",
        headers=shopify_rest_headers(),
        timeout=30,
    )
    response.raise_for_status()
    transactions = response.json().get("transactions", [])
    authorization = next(
        (
            transaction
            for transaction in transactions
            if transaction.get("kind") == "authorization" and transaction.get("status") == "success"
        ),
        None,
    )
    if not authorization:
        return ["No capturable authorization transaction found."]
    payload = {
        "transaction": {
            "kind": "capture",
            "parent_id": authorization["id"],
            "amount": str(getattr(order, "total_price", "")),
            "currency": authorization.get("currency") or getattr(order, "currency", "PKR") or "PKR",
        }
    }
    capture_response = requests.post(
        f"{shopify_rest_base_url()}/orders/{order.id}/transactions.json",
        headers=shopify_rest_headers(),
        json=payload,
        timeout=30,
    )
    capture_response.raise_for_status()
    return []


def fulfill_shopify_order(order):
    fulfillment_status = ((getattr(order, "fulfillment_status", "") or "")).lower()
    if fulfillment_status == "fulfilled":
        return []
    response = requests.get(
        f"{shopify_rest_base_url()}/orders/{order.id}/fulfillment_orders.json",
        headers=shopify_rest_headers(),
        timeout=30,
    )
    response.raise_for_status()
    fulfillment_orders = response.json().get("fulfillment_orders", [])
    open_fulfillment_orders = [
        {"fulfillment_order_id": fulfillment_order["id"]}
        for fulfillment_order in fulfillment_orders
        if fulfillment_order.get("status") not in {"closed", "cancelled", "incomplete"}
    ]
    if not open_fulfillment_orders:
        return ["No open fulfillment orders found."]
    payload = {
        "fulfillment": {
            "notify_customer": False,
            "line_items_by_fulfillment_order": open_fulfillment_orders,
        }
    }
    fulfillment_response = requests.post(
        f"{shopify_rest_base_url()}/fulfillments.json",
        headers=shopify_rest_headers(),
        json=payload,
        timeout=30,
    )
    fulfillment_response.raise_for_status()
    return []


def approve_shopify_delivery(order):
    warnings = []
    try:
        mark_shopify_order_as_paid(order.id)
    except Exception as error:
        try:
            warnings.extend(capture_shopify_payment(order))
        except Exception as capture_error:
            warnings.append(f"Could not mark order as paid: {error}")
            warnings.append(f"Could not capture payment: {capture_error}")
    try:
        warnings.extend(fulfill_shopify_order(order))
    except Exception as error:
        warnings.append(f"Could not create fulfillment: {error}")
    try:
        result = order.close()
        if result is False:
            warnings.append("Shopify order close returned false.")
    except Exception as error:
        warnings.append(f"Could not close Shopify order: {error}")
    try:
        if apply_shopify_order_tag(order.id, "Delivered in Lahore Approved", include_date=True) is False:
            warnings.append("Could not save approval tag.")
    except Exception as error:
        warnings.append(f"Could not save approval tag: {error}")
    return warnings


def approve_shopify_cancellation(order):
    warnings = []
    try:
        result = order.cancel()
        if result is False:
            warnings.append("Shopify order cancel returned false.")
    except Exception as error:
        warnings.append(f"Could not cancel Shopify order: {error}")
    try:
        if apply_shopify_order_tag(order.id, "Cancelled by Employee", include_date=True) is False:
            warnings.append("Could not save cancellation tag.")
    except Exception as error:
        warnings.append(f"Could not save cancellation tag: {error}")
    return warnings


def format_employee_order_note(payment_method, delivery_method, customer_phone, discount_amount, delivery_charges, advance_amount, custom_items, extra_notes=""):
    lines = [
        "Created from Sleek Space employee portal.",
        f"Payment method: {payment_method or 'Not specified'}",
        f"Delivery method: {delivery_method or 'Not specified'}",
        f"Phone: {customer_phone or 'Not provided'}",
    ]
    if discount_amount:
        lines.append(f"Discount amount: PKR {discount_amount}")
    if delivery_charges:
        lines.append(f"Delivery charges: PKR {delivery_charges}")
    if advance_amount:
        lines.append(f"Advance paid by customer: PKR {advance_amount}")
    if custom_items:
        lines.append("Custom items:")
        for item in custom_items:
            lines.append(
                f"- {item.get('title', 'Custom item')} | Qty {item.get('quantity', 1)} | "
                f"PKR {item.get('price', 0)} | Image {'Uploaded in portal' if item.get('image') else 'N/A'}"
            )
    if extra_notes:
        lines.append(f"Notes: {extra_notes}")
    return "\n".join(lines)


def get_active_shopify_products(limit=250):
    cost_overrides = load_product_cost_overrides()
    try:
        products = shopify.Product.find(limit=limit, published_status="published")
    except Exception as error:
        print(f"Could not fetch Shopify products: {error}")
        return []

    results = []
    inventory_ids = []
    while True:
        for product in products:
            if getattr(product, "status", "active") != "active":
                continue
            base_image = product.image.src if getattr(product, "image", None) else ""
            product_images = list(getattr(product, "images", []) or [])
            fetched_images = False
            for variant in getattr(product, "variants", []) or []:
                variant_title = getattr(variant, "title", "") or ""
                display_title = product.title if variant_title in {"Default Title", ""} else f"{product.title} - {variant_title}"
                variant_image = base_image
                variant_image_id = getattr(variant, "image_id", None)
                if variant_image_id and not product_images and not fetched_images:
                    try:
                        product_images = list(shopify.Image.find(product_id=getattr(product, "id", None)) or [])
                    except Exception:
                        product_images = []
                    fetched_images = True
                if variant_image_id and product_images:
                    for image in product_images:
                        if getattr(image, "id", None) == variant_image_id:
                            variant_image = getattr(image, "src", "") or base_image
                            break
                        attached_variant_ids = getattr(image, "variant_ids", None) or []
                        if getattr(variant, "id", None) in attached_variant_ids:
                            variant_image = getattr(image, "src", "") or base_image
                            break
                results.append(
                    {
                        "product_id": getattr(product, "id", None),
                        "variant_id": getattr(variant, "id", None),
                        "inventory_item_id": getattr(variant, "inventory_item_id", None),
                        "title": display_title,
                        "product_title": getattr(product, "title", ""),
                        "variant_title": variant_title,
                        "price": float(getattr(variant, "price", 0) or 0),
                        "cost": 0,
                        "image": variant_image,
                        "sku": getattr(variant, "sku", "") or "",
                    }
                )
                if getattr(variant, "inventory_item_id", None):
                    inventory_ids.append(str(getattr(variant, "inventory_item_id", None)))
        try:
            if not products.has_next_page():
                break
            products = products.next_page()
        except Exception as error:
            print(f"Could not load next Shopify product page: {error}")
            break

    cost_map = {}
    if inventory_ids:
        try:
            cost_map = fetch_shopify_inventory_item_costs(inventory_ids)
        except Exception as error:
            print(f"Could not fetch Shopify product costs: {error}")

    for product in results:
        inventory_id = str(product.get("inventory_item_id") or "").strip()
        fallback_cost = get_cost_override_for_item(
            cost_overrides,
            product_id=product.get("product_id"),
            variant_id=product.get("variant_id"),
            title=product.get("title", ""),
        )
        product["cost"] = cost_map.get(inventory_id, fallback_cost) if inventory_id else fallback_cost
    return results


def build_product_cost_rows(limit=250):
    rows = []
    for product in get_active_shopify_products(limit=limit):
        rows.append(
            {
                "product_id": product.get("product_id"),
                "variant_id": product.get("variant_id"),
                "inventory_item_id": product.get("inventory_item_id"),
                "title": product.get("title", ""),
                "product_title": product.get("product_title", ""),
                "variant_title": product.get("variant_title", ""),
                "sku": product.get("sku", ""),
                "image": product.get("image", ""),
                "price": parse_money(product.get("price", 0)),
                "cost": parse_money(product.get("cost", 0)),
            }
        )
    return sorted(rows, key=lambda row: str(row.get("title", "")).lower())


def build_employee_invoice_payload(order_name, customer_name, phone, city, address, payment_method, delivery_method, catalog_items, custom_items, discount_amount, delivery_charges, advance_amount):
    items = []
    subtotal = 0.0

    for item in catalog_items:
        quantity = int(item.get("quantity") or 1)
        unit_price = parse_money(item.get("price"))
        line_total = round(unit_price * quantity, 2)
        subtotal += line_total
        items.append(
            {
                "title": item.get("title") or "Product",
                "quantity": quantity,
                "image": item.get("image") or "",
                "unit_price": unit_price,
                "line_total": line_total,
            }
        )

    for item in custom_items:
        quantity = int(item.get("quantity") or 1)
        unit_price = parse_money(item.get("price"))
        line_total = round(unit_price * quantity, 2)
        subtotal += line_total
        items.append(
            {
                "title": item.get("title") or "Custom product",
                "quantity": quantity,
                "image": item.get("image") or "",
                "unit_price": unit_price,
                "line_total": line_total,
            }
        )

    total = round(subtotal - discount_amount + delivery_charges, 2)
    balance_due = round(max(total - advance_amount, 0), 2)
    return {
        "order_id": order_name,
        "customer_name": customer_name,
        "customer_phone": phone,
        "customer_city": city,
        "customer_address": address,
        "status": "Created",
        "summary_lines": [
            "Shopify order",
            f"Payment: {payment_method or 'Not specified'}",
            f"Delivery: {delivery_method or 'Not specified'}",
        ],
        "items": items,
        "totals": {
            "subtotal": round(subtotal, 2),
            "discount": round(discount_amount, 2),
            "delivery_charges": round(delivery_charges, 2),
            "total": round(total, 2),
            "advance_paid": round(advance_amount, 2),
            "balance_due": round(balance_due, 2),
        },
    }


def create_shopify_employee_order(payload):
    customer_name = (payload.get("customer_name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    city = (payload.get("city") or "").strip()
    address = (payload.get("address") or "").strip()
    discount_amount = parse_money(payload.get("discount_amount"))
    delivery_charges = parse_money(payload.get("delivery_charges"))
    payment_method = (payload.get("payment_method") or "").strip()
    delivery_method = (payload.get("delivery_method") or "").strip()
    advance_amount = parse_money(payload.get("advance_amount"))
    catalog_items = payload.get("catalog_items") or []
    custom_items = payload.get("custom_items") or []
    extra_notes = (payload.get("notes") or "").strip()

    if not customer_name:
        raise ValueError("Customer name is required.")
    if not phone:
        raise ValueError("Phone number is required.")
    if payment_method.lower() == "partial" and advance_amount <= 0:
        raise ValueError("Enter the advance paid amount for partial payment.")

    first_name, last_name = split_customer_name(customer_name)
    line_items = []

    for item in catalog_items:
        variant_id = item.get("variant_id")
        quantity = int(item.get("quantity") or 1)
        if not variant_id or quantity < 1:
            continue
        line_item = {"variant_id": int(variant_id), "quantity": quantity}
        override_price = parse_money(item.get("price"))
        if override_price > 0:
            line_item["original_unit_price"] = override_price
        line_items.append(line_item)

    normalized_custom_items = []
    for item in custom_items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        price = parse_money(item.get("price"))
        quantity = int(item.get("quantity") or 1)
        normalized_custom_items.append(
            {
                "title": title,
                "price": price,
                "quantity": quantity,
                "image": (item.get("image") or "").strip(),
            }
        )
        line_items.append(
            {"title": title, "original_unit_price": price, "quantity": quantity}
        )

    if not line_items:
        raise ValueError("At least one product is required.")

    estimated_total = 0.0
    for item in catalog_items:
        estimated_total += parse_money(item.get("price")) * int(item.get("quantity") or 1)
    for item in normalized_custom_items:
        estimated_total += parse_money(item.get("price")) * int(item.get("quantity") or 1)
    estimated_total = round(estimated_total - discount_amount + delivery_charges, 2)
    if advance_amount > estimated_total:
        raise ValueError("Advance paid cannot be greater than the order total.")

    note = format_employee_order_note(
        payment_method=payment_method,
        delivery_method=delivery_method,
        customer_phone=phone,
        discount_amount=discount_amount,
        delivery_charges=delivery_charges,
        advance_amount=advance_amount,
        custom_items=normalized_custom_items,
        extra_notes=extra_notes,
    )

    draft_order = shopify.DraftOrder()
    draft_order.line_items = line_items
    draft_order.note = note
    draft_order.tags = "Employee Portal"
    draft_order.use_customer_default_address = False
    draft_order.shipping_address = {
        "first_name": first_name,
        "last_name": last_name or "Customer",
        "phone": phone,
        "address1": address,
        "city": city,
        "country": "Pakistan",
    }
    draft_order.billing_address = draft_order.shipping_address
    draft_order.customer = {
        "first_name": first_name,
        "last_name": last_name or "Customer",
        "phone": phone,
    }

    if discount_amount > 0:
        draft_order.applied_discount = {
            "description": "Employee portal discount",
            "value_type": "fixed_amount",
            "value": discount_amount,
            "amount": discount_amount,
            "title": "Employee portal discount",
        }

    if delivery_charges > 0:
        draft_order.shipping_line = {
            "title": "Delivery Charges",
            "price": delivery_charges,
            "custom": True,
        }

    if not draft_order.save():
        raise RuntimeError(json.dumps(getattr(draft_order, "errors", {}) or {"error": "Could not save draft order"}))

    complete_params = {}
    if payment_method.lower() == "partial":
        complete_params["payment_pending"] = True
    try:
        draft_order.complete(complete_params)
    except Exception as error:
        raise RuntimeError(f"Shopify could not complete the employee order: {error or getattr(draft_order, 'errors', None) or 'Unknown completion error'}")

    try:
        refreshed_draft_order = shopify.DraftOrder.find(draft_order.id)
    except Exception:
        refreshed_draft_order = draft_order

    order_id = getattr(refreshed_draft_order, "order_id", None) or getattr(draft_order, "order_id", None)
    order_name = getattr(refreshed_draft_order, "name", "") or getattr(draft_order, "name", "") or ""
    if not order_id:
        raise RuntimeError("Shopify created the draft, but the completed order ID did not come back. Please check Draft Orders in Shopify.")

    if payment_method.lower() == "full":
        try:
            mark_shopify_order_as_paid(order_id)
        except Exception as error:
            print(f"Could not immediately mark employee order {order_id} as paid: {error}")

    invoice_payload = build_employee_invoice_payload(
        order_name=order_name,
        customer_name=customer_name,
        phone=phone,
        city=city,
        address=address,
        payment_method=payment_method,
        delivery_method=delivery_method,
        catalog_items=catalog_items,
        custom_items=normalized_custom_items,
        discount_amount=discount_amount,
        delivery_charges=delivery_charges,
        advance_amount=advance_amount if payment_method.lower() == "partial" else 0,
    )
    return {
        "draft_order_id": getattr(draft_order, "id", None),
        "order_id": order_id,
        "order_name": order_name,
        "invoice": invoice_payload,
    }


def verify_shopify_webhook(req):
    shopify_hmac = req.headers.get("X-Shopify-Hmac-Sha256")
    data = req.get_data()
    secret = os.getenv("SHOPIFY_WEBHOOK_SECRET")
    if not secret:
        raise ValueError("SHOPIFY_WEBHOOK_SECRET is not set.")
    digest = hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed_hmac, shopify_hmac or "")


def build_admin_mobile_sections():
    return [
        {"id": "dashboard", "label": "Dashboard", "icon": "🏠", "src": "/?embedded=1"},
        {"id": "analytics", "label": "Analytics", "icon": "📊", "src": "/analytics"},
        {"id": "scanner", "label": "Scanner", "icon": "🔍", "src": "/employee_portal"},
        {"id": "employee-orders", "label": "Orders", "icon": "🧾", "src": "/employee_portal/orders"},
        {"id": "pending", "label": "Pending", "icon": "📋", "src": "/pending?embedded=1"},
        {"id": "undelivered", "label": "Undelivered", "icon": "🚚", "src": "/undelivered?embedded=1"},
    ]


@app.route("/send-email", methods=["POST"])
def send_email():
    data = request.get_json() or {}
    to_emails = data.get("to", [])
    cc_emails = data.get("cc", [])
    subject = data.get("subject", "")
    body = data.get("body", "")
    try:
        smtp_server = "smtp.gmail.com"
        smtp_port = 587
        smtp_user = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASSWORD")
        msg = MIMEText(body)
        msg["From"] = smtp_user
        msg["To"] = ", ".join(to_emails)
        msg["Cc"] = ", ".join(cc_emails)
        msg["Subject"] = subject

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_emails + cc_emails, msg.as_string())
        server.quit()
        return jsonify({"message": "Email sent successfully"})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/generate_loadsheet", methods=["POST"])
def generate_loadsheet():
    data = request.get_json() or {}
    cn_numbers = data.get("cn_numbers", [])
    if not cn_numbers:
        return jsonify({"error": "No CN numbers provided"}), 400

    payload = {
        "api_key": os.getenv("LEOPARD_API_KEY"),
        "api_password": os.getenv("LEOPARD_PASSWORD"),
        "cn_numbers": cn_numbers,
        "courier_name": "1",
        "courier_code": "1",
    }
    try:
        response = requests.post(
            "https://merchantapi.leopardscourier.com/api/generateLoadSheet/",
            json=payload,
            timeout=30,
        )
        return jsonify(response.json())
    except requests.RequestException as error:
        return jsonify({"error": f"Failed to connect to the API: {error}"}), 500


@app.route("/apply_tag", methods=["POST"])
def apply_tag():
    data = request.get_json() or {}
    order_id = data.get("order_id")
    tag = (data.get("tag") or "").strip()
    if not order_id or not tag:
        return jsonify({"success": False, "error": "order_id and tag are required."}), 400
    try:
        order = shopify.Order.find(order_id)
        if tag.lower() == "returned":
            try:
                order.cancel()
            except Exception:
                pass
        if tag.lower() == "delivered":
            try:
                order.close()
            except Exception:
                pass
        if apply_shopify_order_tag(order_id, tag, include_date=True):
            return jsonify({"success": True, "message": "Tag applied successfully."})
        return jsonify({"success": False, "error": "Failed to save order changes."}), 500
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/")
def tracking():
    if not daraz_orders_cache:
        refresh_daraz_cache_if_needed()
    return render_template(
        "track.html",
        order_details=order_details,
        darazOrders=daraz_orders_cache,
        employee_approvals=build_employee_approval_items(),
        abandoned_summary=get_abandoned_summary_safe(),
    )


@app.route("/refresh", methods=["POST"])
def refresh_data():
    try:
        refreshed_all = reload_orders(preserve_existing_on_partial=True)
        daraz_rows = refresh_daraz_cache_if_needed(force=True)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            message = "Data refreshed successfully" if refreshed_all else "Tracking refreshed; existing order cache preserved"
            return jsonify({"message": message, "daraz_count": len(daraz_rows)})
        return render_template(
            "track.html",
            order_details=order_details,
            darazOrders=daraz_rows,
            employee_approvals=build_employee_approval_items(),
            abandoned_summary=get_abandoned_summary_safe(),
        )
    except Exception as error:
        return jsonify({"message": f"Failed to refresh data: {error}"}), 500


@app.route("/abandoned")
def abandoned_orders():
    cache = load_abandoned_cache()
    if not abandoned_cache_is_fresh(cache):
        refresh_abandoned_checkouts_cache_background()
    abandoned_checkouts = cache.get("rows") or []
    summary = cache.get("summary") or summarize_abandoned_checkout_rows(abandoned_checkouts)
    error = cache.get("error")
    return render_template(
        "abandoned.html",
        abandoned_checkouts=abandoned_checkouts,
        summary=summary,
        error=error,
    )


@app.route("/abandoned/mark-viewed", methods=["POST"])
def mark_abandoned_viewed():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token") or "").strip()
    if not token:
        return jsonify({"success": False, "error": "Missing checkout token"}), 400
    viewed_tokens = load_abandoned_viewed_tokens()
    viewed_tokens.add(token)
    saved = save_abandoned_viewed_tokens(viewed_tokens)
    mark_abandoned_cache_viewed(token)
    return jsonify({"success": saved, "token": token, "viewed_count": len(viewed_tokens)})


@app.route("/track/<tracking_num>")
def display_tracking(tracking_num):
    async def run_lookup():
        async with aiohttp.ClientSession() as session_obj:
            return await fetch_tracking_data(session_obj, tracking_num)

    data = asyncio.run(run_lookup())
    summary = summarize_tracking_result(tracking_num, data)
    if tracking_data_is_valid(tracking_num, data):
        remember_tracking_summary(tracking_num, summary)
    matched_order = find_shopify_order_by_tracking_number(tracking_num)
    return render_template("trackingdata.html", data=data, tracking_number=tracking_num, matched_order=matched_order)


@app.route("/tracking-summary/<tracking_num>")
def tracking_summary(tracking_num):
    try:
        return jsonify({"success": True, "tracking": build_tracking_summary_payload(tracking_num)})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/pending")
def pending_orders():
    all_orders, pending_items, summary = build_pending_items_table_data()
    return render_template("pending.html", all_orders=all_orders, pending_items=pending_items, summary=summary)


@app.route("/daraz")
def daraz_callback():
    code = (request.args.get("code") or "").strip()
    if code:
        try:
            config = daraz_configuration()
            client = lazop.LazopClient(DARAZ_API_URL, config["app_key"], config["app_secret"])
            token_request = lazop.LazopRequest("/auth/token/create")
            token_request.add_api_param("code", code)
            body = client.execute(token_request).body or {}
            if not body.get("access_token"):
                raise RuntimeError(body.get("message") or body.get("code") or "Daraz authorization failed")
            save_tokens(
                body["access_token"],
                body.get("refresh_token"),
                body.get("expires_in") or 604800,
            )
            refresh_daraz_cache_if_needed(force=True)
            return redirect(url_for("daraz_orders_page"))
        except Exception as error:
            return render_template("daraz.html", darazOrders=[], error_message=str(error)), 400
    return daraz_orders_page()


@app.route("/daraz/orders")
def daraz_orders_page():
    rows = refresh_daraz_cache_if_needed(force=True)
    tokens = load_tokens()
    error_message = daraz_last_error
    if not error_message and not rows and not tokens:
        error_message = "Daraz is not authenticated. Use Connect Daraz to authorize this store."
    return render_template("daraz.html", darazOrders=rows, error_message=error_message)


@app.route("/daraz/token-status")
def daraz_token_status():
    tokens = load_tokens()
    if not tokens:
        return jsonify({"status": "missing"})
    expires_at_raw = str(tokens.get("expires_at") or "")
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
        days_left = (expires_at - datetime.now()).days
    except ValueError:
        days_left = None
    return jsonify({"status": "ok", "expires_at": expires_at_raw, "days_left": days_left})


@app.route("/aghaje-orders")
def aghaje_orders():
    orders, summary, error_message = build_aghaje_orders_page_data()
    start_aghaje_active_tracking_auto_refresh(orders)
    fulfilled_orders = []
    closed_orders = []
    unfulfilled_orders = []
    returned_orders = []
    cancelled_orders = []
    for order in orders:
        payment_status = str(order.get("payment_status") or "Pending").strip() or "Pending"
        raw_fulfillment = str(order.get("fulfillment_status_raw") or "").strip().lower()
        delivery_status = str(order.get("delivery_status") or "").strip()
        is_cancelled = delivery_status == "Cancelled"
        is_returned = delivery_status == "Returned"
        if is_cancelled:
            cancelled_orders.append(order)
        elif is_returned:
            returned_orders.append(order)
        else:
            if is_aghaje_fulfilled_or_partial(raw_fulfillment):
                fulfilled_orders.append(order)
            if payment_status == "Paid":
                closed_orders.append(order)
            elif not is_aghaje_fulfilled_or_partial(raw_fulfillment):
                unfulfilled_orders.append(order)
    tab_unpaid_values = {
        "fulfilled": sum_shopify_unpaid_value(fulfilled_orders),
        "unfulfilled": sum_shopify_unpaid_value(unfulfilled_orders),
    }
    all_order_totals = build_aghaje_orders_total_row(orders)
    all_order_fulfilled_totals = build_aghaje_orders_total_row(
        [order for order in orders if is_aghaje_fulfilled_or_partial(order.get("fulfillment_status_raw"))]
    )
    return render_template(
        "aghaje_orders.html",
        orders=orders,
        fulfilled_orders=fulfilled_orders,
        closed_orders=closed_orders,
        unfulfilled_orders=unfulfilled_orders,
        returned_orders=returned_orders,
        cancelled_orders=cancelled_orders,
        tab_unpaid_values=tab_unpaid_values,
        all_order_totals=all_order_totals,
        all_order_fulfilled_totals=all_order_fulfilled_totals,
        summary=summary,
        error_message=error_message,
    )


@app.route("/aghaje-orders/paid-order-ids")
def aghaje_paid_order_ids():
    orders, _, error_message = build_aghaje_orders_page_data()
    if error_message:
        return jsonify({"success": False, "error": error_message, "paid_order_ids": []}), 502
    paid_order_ids = [
        str(order.get("order_id") or "").strip()
        for order in orders
        if str(order.get("payment_status") or "").strip().lower() == "paid"
    ]
    return jsonify({"success": True, "paid_order_ids": paid_order_ids})


@app.route("/aghaje-orders/refresh", methods=["POST"])
def refresh_aghaje_orders_tracking():
    try:
        orders, _, error_message = build_aghaje_orders_page_data()
        if error_message:
            return jsonify({"success": False, "error": error_message}), 502
        tracking_numbers = get_active_aghaje_tracking_numbers(orders)
        refreshed_count = refresh_tracking_summaries_sync(tracking_numbers, fresh_seconds=0)
        background_started = start_tracking_summaries_background_refresh(tracking_numbers)
        message = f"Refreshed {refreshed_count} active courier shipment statuses."
        if background_started:
            message += " Remaining shipments will continue updating in the background."
        return jsonify({"success": True, "message": message, "refreshed_count": refreshed_count})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/product-costs")
def product_costs():
    return render_template("product_costs.html", products=build_product_cost_rows())


@app.route("/product-costs/update", methods=["POST"])
def update_product_costs():
    data = request.get_json() or {}
    product_id = data.get("product_id")
    variant_id = data.get("variant_id")
    inventory_item_id = data.get("inventory_item_id")
    title = (data.get("title") or "").strip()
    submitted_price = parse_money(data.get("price", 0))
    original_price = parse_money(data.get("original_price", submitted_price))
    submitted_cost = parse_money(data.get("cost", 0))

    if not variant_id and not product_id and not title:
        return jsonify({"success": False, "error": "Product identity is required."}), 400

    try:
        if variant_id and round(original_price, 2) != round(submitted_price, 2):
            current_product = None
            current_product = shopify.Variant.find(int(variant_id))
            current_product.price = submitted_price
            if not current_product.save():
                raise RuntimeError("Shopify price update failed.")

        if inventory_item_id:
            update_shopify_inventory_item_cost(inventory_item_id, submitted_cost)

        overrides = load_product_cost_overrides()
        set_cost_override(
            overrides,
            product_id=product_id,
            variant_id=variant_id,
            title=title,
            price=submitted_price,
            cost=submitted_cost,
        )
        if not save_product_cost_overrides(overrides):
            raise RuntimeError("Could not save cost override.")

        reload_orders()
        return jsonify({"success": True, "price": submitted_price, "cost": submitted_cost})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/aghaje-orders/update", methods=["POST"])
def update_aghaje_order():
    data = request.get_json() or {}
    order_id = str(data.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"success": False, "error": "Order id is required."}), 400

    payment_status = str(data.get("payment_status") or "Pending").strip() or "Pending"
    delivery_status = str(data.get("delivery_status") or "Inprocess").strip() or "Inprocess"
    amount_received = parse_money(data.get("amount_received", 0))
    packaging_cost = parse_money(data.get("packaging_cost", 0))
    delivery_cost = parse_money(data.get("delivery_cost", 0))
    is_postex = bool(data.get("is_postex")) or is_postex_courier(data.get("courier_name"))
    item_costs = data.get("item_costs") or []

    if is_postex:
        amount_received = 0.0
        delivery_cost = 0.0

    if delivery_status == "Cancelled":
        payment_status = "Not Payable"
        amount_received = 0.0
        packaging_cost = 0.0
        delivery_cost = 0.0

    try:
        existing_default_item_costs = load_aghaje_item_cost_overrides()
        for item in item_costs:
            item_key = str(item.get("cost_key") or "").strip()
            title = str(item.get("title") or "").strip()
            if not item_key or not title:
                continue
            submitted_cost = parse_money(item.get("cost", 0))
            if not upsert_aghaje_order_item_cost_override(
                order_id=order_id,
                item_key=item_key,
                title=title,
                cost=submitted_cost,
                product_id=item.get("product_id"),
                variant_id=item.get("variant_id"),
            ):
                raise RuntimeError(f"Could not save item cost for {title}.")

            existing_default = existing_default_item_costs.get(item_key) or {}
            if submitted_cost != 0 and parse_money(existing_default.get("cost", 0)) == 0:
                if not upsert_aghaje_item_cost_override(
                    item_key=item_key,
                    title=title,
                    cost=submitted_cost,
                    product_id=item.get("product_id"),
                    variant_id=item.get("variant_id"),
                ):
                    raise RuntimeError(f"Could not save default item cost for {title}.")

        if not upsert_aghaje_order_override(
            order_id,
            amount_received,
            packaging_cost,
            delivery_cost,
            payment_status,
            delivery_status,
        ):
            raise RuntimeError("Could not save Aghaje order override.")
        return jsonify(
            {
                "success": True,
                "order_id": order_id,
                "amount_received": amount_received,
                "packaging_cost": packaging_cost,
                "delivery_cost": delivery_cost,
                "payment_status": payment_status,
                "delivery_status": delivery_status,
            }
        )
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/aghaje-orders/update-summary", methods=["POST"])
def update_aghaje_orders_summary():
    data = request.get_json() or {}
    try:
        amount_received = parse_money(data.get("amount_received", 0))
        if not add_aghaje_net_payment_received_entry(amount_received):
            raise RuntimeError("Could not save Aghaje received amount.")
        entries = get_aghaje_net_payment_received_entries()
        total_received = round(sum(parse_money(entry.get("amount", 0)) for entry in entries), 2)
        return jsonify(
            {
                "success": True,
                "amount_received": amount_received,
                "total_received": total_received,
                "entries": entries,
            }
        )
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/aghaje_portal", methods=["GET", "POST"])
@app.route("/aghaje-portal", methods=["GET", "POST"])
def aghaje_portal():
    if request.method == "POST":
        submitted_password = (request.form.get("password") or "").strip()
        if submitted_password == AGHAJE_PORTAL_PASSWORD:
            session[AGHAJE_PORTAL_SESSION_KEY] = True
            return redirect(url_for("aghaje_portal"))
        return render_template("aghaje_portal.html", view="login", login_error="Wrong password. Try again."), 401

    if not aghaje_portal_is_authenticated():
        return render_template("aghaje_portal.html", view="login", login_error="")

    portal_data = build_aghaje_portal_page_data()
    start_aghaje_active_tracking_auto_refresh(portal_data["orders"])
    return render_template(
        "aghaje_portal.html",
        view="portal",
        orders=portal_data["orders"],
        fulfilled_orders=portal_data["fulfilled_orders"],
        closed_orders=portal_data["closed_orders"],
        unfulfilled_orders=portal_data["unfulfilled_orders"],
        summary=portal_data["summary"],
        error_message=portal_data["error_message"],
    )


@app.route("/aghaje_portal/refresh", methods=["POST"])
@app.route("/aghaje-portal/refresh", methods=["POST"])
def aghaje_portal_refresh():
    if not aghaje_portal_is_authenticated():
        return jsonify({"success": False, "error": "Not authenticated."}), 401
    try:
        orders, _, _ = build_aghaje_orders_page_data()
        tracking_numbers = get_active_aghaje_tracking_numbers(orders)
        refreshed_count = refresh_tracking_summaries_sync(tracking_numbers, fresh_seconds=0)
        background_started = start_tracking_summaries_background_refresh(tracking_numbers)
        message = f"Refreshed {refreshed_count} active courier shipment statuses."
        if background_started:
            message += " Remaining stale shipments will continue updating in the background."
        return jsonify({"success": True, "message": message, "refreshed_count": refreshed_count})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/aghaje_portal/logout", methods=["POST"])
@app.route("/aghaje-portal/logout", methods=["POST"])
def aghaje_portal_logout():
    session.pop(AGHAJE_PORTAL_SESSION_KEY, None)
    return redirect(url_for("aghaje_portal"))


@app.route("/orders")
def pending_orders_mobile():
    return render_template("orders.html", all_orders=build_pending_orders_mobile_data(), employee_portal_mode=False)


@app.route("/undelivered")
def undelivered():
    refresh_daraz_cache_if_needed()
    undelivered_orders = [order for order in order_details if is_undelivered_status(order.get("status"))]
    daraz_undelivered_orders = [
        order
        for order in daraz_orders_cache
        if is_undelivered_status(order.get("status"))
        or any(is_undelivered_status(item.get("status")) for item in order.get("items_list", []))
    ]
    return render_template(
        "undelivered.html",
        order_details=undelivered_orders,
        darazOrders=daraz_undelivered_orders,
    )


@app.route("/shopify/protected-data/status")
def protected_data_status():
    return jsonify(get_protected_data_config_status())


@app.route("/shopify/install")
def shopify_install():
    state = create_oauth_state()
    session[SHOPIFY_OAUTH_STATE_SESSION_KEY] = state
    return redirect(get_install_url(state))


@app.route("/shopify/callback")
def shopify_callback():
    shop = (request.args.get("shop") or "").strip()
    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    saved_state = session.get(SHOPIFY_OAUTH_STATE_SESSION_KEY, "")
    hmac_valid = verify_oauth_hmac(request.query_string)

    if state != saved_state:
        return jsonify({"success": False, "error": "Invalid Shopify callback state"}), 400
    if not hmac_valid and shop != get_shop_domain():
        return jsonify({"success": False, "error": "Invalid Shopify callback signature"}), 400
    if not shop or not code:
        return jsonify({"success": False, "error": "Missing Shopify callback parameters"}), 400

    try:
        payload = exchange_oauth_code_for_token(shop, code)
        save_offline_token(shop, payload)
        setup_shopify()
        try:
            ensure_required_shopify_webhooks()
        except Exception as webhook_error:
            print(f"Warning: could not ensure Shopify webhooks: {webhook_error}")
        session.pop(SHOPIFY_OAUTH_STATE_SESSION_KEY, None)
        reload_orders()
        return redirect("/shopify/protected-data/status?connected=1")
    except Exception as error:
        return jsonify({"success": False, "error": f"Shopify token exchange failed: {error}"}), 500


def _handle_shopify_order_webhook():
    global order_details
    try:
        if not verify_shopify_webhook(request):
            return jsonify({"error": "Invalid webhook signature"}), 401

        order_data = request.get_json(silent=True)
        if not order_data:
            return jsonify({"message": "Empty payload received. Ignored."}), 200

        order_id = order_data.get("id")
        if not order_id:
            return jsonify({"error": "No order id found in payload"}), 400

        if analytics_enabled():
            from analytics_store import sync_order
            sync_order(order_data, event_id=request.headers.get("X-Shopify-Event-Id") or request.headers.get("X-Shopify-Webhook-Id"),
                       topic=request.headers.get("X-Shopify-Topic", "orders/updated"))

        if order_data.get("cancelled_at") or order_data.get("closed_at"):
            order_details = [order for order in order_details if order.get("id") != order_id]
            return jsonify({"success": True, "message": f"Order {order_id} closed and removed"}), 200

        setup_shopify()
        order = shopify.Order.find(order_id)

        async def update_order():
            async with aiohttp.ClientSession() as session_obj:
                return await process_order(session_obj, order)

        updated_order_info = asyncio.run(update_order())
        enrich_orders_with_protected_customer_data([updated_order_info])

        if not should_keep_order_in_active_list(updated_order_info, order):
            order_details = [existing_order for existing_order in order_details if existing_order.get("id") != updated_order_info.get("id")]
            return jsonify({"success": True, "message": f"Order {order_id} no longer belongs in active orders and was removed."})

        for index, existing_order in enumerate(order_details):
            if existing_order.get("id") == updated_order_info.get("id"):
                order_details[index] = updated_order_info
                order_details[:] = sort_orders_newest_first(order_details)
                break
        else:
            order_details.append(updated_order_info)
            order_details[:] = sort_orders_newest_first(order_details)
        refresh_abandoned_checkouts_cache_background(force=True)
        return jsonify({"success": True, "message": f"Order {order_id} processed successfully"})
    except Exception as error:
        app.logger.error("Shopify order webhook processing failed; retry required")
        return jsonify({"success": False, "error": "Order synchronization failed"}), 500


def _handle_shopify_checkout_webhook():
    try:
        if not verify_shopify_webhook(request):
            return jsonify({"error": "Invalid webhook signature"}), 401

        checkout_data = request.get_json(silent=True)
        if not checkout_data:
            return jsonify({"message": "Empty payload received. Ignored."}), 200

        row = build_abandoned_checkout_row_from_payload(checkout_data)
        if not row.get("token"):
            return jsonify({"error": "No checkout token found in payload"}), 400

        merge_cached_abandoned_row(row)
        if not checkout_has_customer_contact(checkout_data) or not any(item.get("image") for item in row.get("items", [])):
            refresh_abandoned_checkouts_cache_background()
        return jsonify({"success": True, "message": f"Checkout {row.get('token')} cached successfully"})
    except Exception as error:
        print(f"Checkout webhook processing error: {error}")
        return jsonify({"success": False, "error": str(error)}), 500


def _handle_aghaje_order_webhook():
    global aghaje_product_cache, aghaje_inventory_item_cost_cache
    try:
        if not verify_aghaje_webhook(request):
            return jsonify({"error": "Invalid webhook signature"}), 401

        payload = request.get_json(silent=True) or {}
        order_id = payload.get("id")
        if not order_id:
            return jsonify({"error": "No order id found in payload"}), 400

        aghaje_product_cache.clear()
        aghaje_inventory_item_cost_cache.clear()
        print(f"Aghaje webhook processed for order {order_id}")
        return jsonify({"success": True, "message": f"Order {order_id} processed successfully"})
    except Exception as error:
        print(f"Aghaje webhook processing error: {error}")
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/shopify/webhook/order_cancelled", methods=["POST"])
def analytics_order_cancelled_webhook():
    return _handle_shopify_order_webhook()


@app.route("/shopify/webhook/fulfillment_updated", methods=["POST"])
@app.route("/shopify/webhook/refund_created", methods=["POST"])
def analytics_related_webhook():
    if not verify_shopify_webhook(request):
        return jsonify(error="Invalid webhook signature"), 401
    if not analytics_enabled():
        return jsonify(error="Analytics not enabled"), 503
    payload = request.get_json(silent=True) or {}
    if not payload.get("order_id"):
        return jsonify(error="Missing order_id"), 400
    try:
        from analytics_store import sync_order
        setup_shopify()
        order = shopify.Order.find(payload["order_id"])
        sync_order(order.to_dict(), event_id=request.headers.get("X-Shopify-Event-Id") or request.headers.get("X-Shopify-Webhook-Id"),
                   topic=request.headers.get("X-Shopify-Topic", "related_update"))
        return jsonify(success=True)
    except Exception:
        app.logger.error("Analytics related webhook failed; Shopify should retry")
        return jsonify(error="Order synchronization failed"), 503


@app.route("/shopify/webhook/order_created", methods=["POST"])
def shopify_order_created():
    return _handle_shopify_order_webhook()


@app.route("/shopify/webhook/order_updated", methods=["POST"])
def shopify_order_updated():
    return _handle_shopify_order_webhook()


@app.route("/shopify/webhook/checkout_created", methods=["POST"])
def shopify_checkout_created():
    return _handle_shopify_checkout_webhook()


@app.route("/shopify/webhook/checkout_updated", methods=["POST"])
def shopify_checkout_updated():
    return _handle_shopify_checkout_webhook()


@app.route("/aghaje/webhook/order_created", methods=["POST"])
def aghaje_order_created_webhook():
    return _handle_aghaje_order_webhook()


@app.route("/aghaje/webhook/order_updated", methods=["POST"])
def aghaje_order_updated_webhook():
    return _handle_aghaje_order_webhook()


@app.route("/scan", methods=["GET", "POST"])
def search():
    search_term = (request.args.get("term") or request.form.get("search_term") or "").split(",")[0].strip()
    if not search_term:
        return render_template("scan.html")

    order_found = find_employee_portal_order(search_term)
    if request.method == "POST":
        if order_found:
            order_found = {
                "line_items": [
                    {"product_title": item.get("title"), "quantity": item.get("quantity"), "image_src": item.get("image")}
                    for item in order_found.get("items", [])
                ]
            }
        return render_template("scan.html", search_term=search_term, order_found=order_found)

    return jsonify(order_found if order_found else {"error": "Order not found"}), 200 if order_found else 404


@app.route("/dispatch", methods=["GET"])
def dispatch():
    return jsonify(build_safe_employee_portal_orders())


@app.route("/return", methods=["GET"])
def return_orders():
    return jsonify(build_safe_employee_portal_orders())


@app.route("/update_status", methods=["POST"])
def update_status():
    data = request.get_json() or {}
    order_id = str(data.get("order_id"))
    tracking_number = str(data.get("tracking_number", "N/A"))
    status = str(data.get("status") or "")
    key = f"{order_id}:{tracking_number}"
    upsert_order_status(key, status)
    response_message = f"Status updated to {status} for {order_id} ({tracking_number})"

    if status == "Delivered in Lahore":
        matching_order = find_shopify_order_by_order_name(order_id)
        if matching_order and matching_order.get("id"):
            try:
                if apply_shopify_order_tag(matching_order["id"], "Delivered in Lahore"):
                    local_tags = list(matching_order.get("tags") or [])
                    if "Delivered in Lahore" not in local_tags:
                        local_tags.append("Delivered in Lahore")
                    matching_order["tags"] = local_tags
                    response_message = (
                        f"Status updated to {status} for {order_id} ({tracking_number}). "
                        "Shopify tag applied: Delivered in Lahore."
                    )
            except Exception as error:
                print(f"Could not apply Lahore tag: {error}")

    return jsonify({"message": response_message})


@app.route("/employee_status/approve", methods=["POST"])
def approve_employee_status():
    data = request.get_json() or {}
    order_id = str(data.get("order_id") or "")
    tracking_number = str(data.get("tracking_number") or "N/A")
    requested_status = str(data.get("requested_status") or "").strip()
    key = f"{order_id}:{tracking_number}"

    if requested_status not in {"Delivered in Lahore", "Cancelled by Employee"}:
        return jsonify({"success": False, "error": "Unsupported employee approval status."}), 400

    matching_order = find_shopify_order_by_order_name(order_id)
    if not matching_order or not matching_order.get("id"):
        return jsonify({"success": False, "error": "Shopify order not found."}), 404

    try:
        order = shopify.Order.find(matching_order["id"])
        warnings = []
        if requested_status == "Delivered in Lahore":
            warnings = approve_shopify_delivery(order)
            order_details[:] = [existing_order for existing_order in order_details if existing_order.get("id") != matching_order.get("id")]
        else:
            warnings = approve_shopify_cancellation(order)
            order_details[:] = [existing_order for existing_order in order_details if existing_order.get("id") != matching_order.get("id")]
        delete_order_status(key)
        message = f"Approved {requested_status} for {order_id}."
        if warnings:
            message = f"{message} Warnings: {' '.join(warnings)}"
        return jsonify({"success": True, "message": message, "warnings": warnings})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/employee_status/reject", methods=["POST"])
def reject_employee_status():
    data = request.get_json() or {}
    order_id = str(data.get("order_id") or "")
    tracking_number = str(data.get("tracking_number") or "N/A")
    requested_status = str(data.get("requested_status") or "").strip()
    key = f"{order_id}:{tracking_number}"

    if requested_status not in {"Delivered in Lahore", "Cancelled by Employee"}:
        return jsonify({"success": False, "error": "Unsupported employee approval status."}), 400

    warnings = []
    if requested_status == "Delivered in Lahore":
        matching_order = find_shopify_order_by_order_name(order_id)
        if matching_order and matching_order.get("id"):
            try:
                if remove_shopify_order_tag(matching_order["id"], "Delivered in Lahore") is False:
                    warnings.append("Could not remove the preliminary Shopify delivery tag.")
                else:
                    matching_order["tags"] = [
                        tag for tag in matching_order.get("tags", [])
                        if str(tag).strip().lower() != "delivered in lahore"
                    ]
            except Exception as error:
                warnings.append(f"Could not remove the preliminary Shopify delivery tag: {error}")

    if delete_order_status(key) is False:
        return jsonify({"success": False, "error": "Could not clear the employee approval request."}), 500

    message = f"Rejected {requested_status} for {order_id}."
    if warnings:
        message = f"{message} Warnings: {' '.join(warnings)}"
    return jsonify({"success": True, "message": message, "warnings": warnings})


@app.route("/employee_portal", methods=["GET", "POST"])
def employee_portal():
    next_url = employee_portal_safe_next_url(request.values.get("next"))
    if request.method == "POST":
        submitted_password = (request.form.get("password") or "").strip()
        if submitted_password == EMPLOYEE_PORTAL_PASSWORD:
            session[EMPLOYEE_PORTAL_SESSION_KEY] = True
            return redirect(next_url)
        return render_template("employee_portal.html", view="login", login_error="Wrong password. Try again.", next_url=next_url), 401
    if not employee_portal_is_authenticated():
        return render_template("employee_portal.html", view="login", login_error="", next_url=next_url)
    return render_template("employee_portal.html", view="portal", employee_orders=build_safe_employee_portal_orders())


@app.route("/employee_portal/orders")
def employee_portal_orders():
    if not employee_portal_is_authenticated():
        return redirect(url_for("employee_portal", next="/employee_portal/orders"))
    return render_template("orders.html", all_orders=build_pending_orders_mobile_data(), employee_portal_mode=True)


@app.route("/employee_portal/products")
def employee_portal_products():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    return jsonify({"success": True, "products": get_active_shopify_products()})


@app.route("/employee_portal/create-order", methods=["POST"])
def employee_portal_create_order():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json() or {}
    try:
        result = create_shopify_employee_order(data)
        reload_orders()
        return jsonify(
            {
                "success": True,
                "draft_order_id": result.get("draft_order_id"),
                "order_id": result.get("order_id"),
                "order_name": result.get("order_name"),
                "invoice": result.get("invoice"),
            }
        )
    except Exception as error:
        print(f"Employee order create failed: {error}")
        return jsonify({"success": False, "error": str(error)}), 400


@app.route("/employee_portal/logout", methods=["POST"])
def employee_portal_logout():
    session.pop(EMPLOYEE_PORTAL_SESSION_KEY, None)
    return redirect(url_for("employee_portal"))


@app.route("/employee_portal/updates")
def employee_portal_updates():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    orders = build_safe_employee_portal_orders()
    summaries = [
        {
            "id": f"{order.get('source')}:{order.get('order_id')}",
            "order_id": order.get("order_id"),
            "source": order.get("source"),
            "created_at": order.get("created_at"),
        }
        for order in orders
    ]
    summaries.sort(key=lambda item: parse_date_for_sort(item.get("created_at")), reverse=True)
    return jsonify(
        {
            "success": True,
            "count": len(summaries),
            "order_ids": [item["id"] for item in summaries],
            "latest": summaries[:6],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )


@app.route("/employee_portal-manifest.webmanifest")
def employee_portal_manifest():
    return send_from_directory("static", "employee-portal.webmanifest", mimetype="application/manifest+json")


@app.route("/employee_portal-sw.js")
def employee_portal_service_worker():
    return send_from_directory("static", "employee-portal-sw.js", mimetype="application/javascript")


@app.route("/employee_portal/report", methods=["POST"])
def employee_portal_report():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json() or {}
    mode = (data.get("mode") or "").strip().lower()
    scanned_orders = data.get("orders") or []
    if mode not in {"dispatch", "return"}:
        return jsonify({"success": False, "error": "Invalid report mode."}), 400
    if not scanned_orders:
        return jsonify({"success": False, "error": "No scanned orders provided."}), 400
    tag_name = "Dispatched" if mode == "dispatch" else "Return Received"
    tagged_count = 0
    skipped_count = 0
    failed = []
    seen_order_keys = set()
    tagged_by_source = {}
    for entry in scanned_orders:
        source = str(entry.get("source") or "").strip().lower()
        shopify_id = str(entry.get("shopify_id") or "").strip()
        if not source or not shopify_id:
            skipped_count += 1
            continue
        order_key = (source, shopify_id)
        if order_key in seen_order_keys:
            skipped_count += 1
            continue
        seen_order_keys.add(order_key)
        try:
            tagged = apply_employee_portal_order_tag(source, shopify_id, tag_name)
        except Exception as error:
            failed.append(
                {
                    "source": source,
                    "order_id": entry.get("order_id") or shopify_id,
                    "error": str(error),
                }
            )
            continue
        if tagged:
            tagged_count += 1
            label = employee_portal_source_label(source)
            tagged_by_source[label] = tagged_by_source.get(label, 0) + 1
        else:
            skipped_count += 1
    return jsonify(
        {
            "success": not failed,
            "tagged_count": tagged_count,
            "skipped_count": skipped_count,
            "failed_count": len(failed),
            "failed": failed[:5],
            "tagged_by_source": tagged_by_source,
            "tag_name": tag_name,
        }
    ), 207 if failed else 200


@app.route("/admin_portal", methods=["GET", "POST"])
def admin_portal():
    selected = (request.values.get("section") or "dashboard").strip().lower()
    sections = build_admin_mobile_sections()
    section_ids = {section["id"] for section in sections}
    if selected not in section_ids:
        selected = "dashboard"

    if request.method == "POST":
        submitted_password = (request.form.get("password") or "").strip()
        if submitted_password == ADMIN_PORTAL_PASSWORD:
            session[ADMIN_PORTAL_SESSION_KEY] = True
            return redirect(url_for("admin_portal", section=selected))
        return render_template("admin_portal.html", view="login", login_error="Wrong password. Try again.", sections=sections, selected_section=selected), 401

    if not admin_portal_is_authenticated():
        return render_template("admin_portal.html", view="login", login_error="", sections=sections, selected_section=selected)

    return render_template("admin_portal.html", view="portal", sections=sections, selected_section=selected, employee_approvals=build_employee_approval_items())


@app.route("/admin_portal/logout", methods=["POST"])
def admin_portal_logout():
    session.pop(ADMIN_PORTAL_SESSION_KEY, None)
    return redirect(url_for("admin_portal"))


@app.route("/admin_portal-manifest.webmanifest")
def admin_portal_manifest():
    return send_from_directory("static", "admin-portal.webmanifest", mimetype="application/manifest+json")


@app.route("/admin_portal-sw.js")
def admin_portal_service_worker():
    return send_from_directory("static", "admin-portal-sw.js", mimetype="application/javascript")


@app.route("/aghaje-portal-manifest.webmanifest")
def aghaje_portal_manifest():
    return send_from_directory("static", "aghaje-portal.webmanifest", mimetype="application/manifest+json")


@app.route("/aghaje-portal-sw.js")
def aghaje_portal_service_worker():
    return send_from_directory("static", "aghaje-portal-sw.js", mimetype="application/javascript")


def restart_program():
    print("Restarting the program...")
    os.execv(sys.executable, ["python"] + sys.argv)


def check_restart_times():
    target_times = ["10:00", "20:00", "02:00"]
    while True:
        if datetime.now().strftime("%H:%M") in target_times:
            restart_program()
        time.sleep(30)


def warm_runtime_caches():
    """Populate remote caches without blocking the web worker from becoming ready."""
    reload_orders()
    try:
        refresh_abandoned_checkouts_cache_sync(force=True)
    except Exception:
        print("Warning: could not warm abandoned checkout cache")


if os.getenv("INITIALIZE_APP", "true") == "true":
    init_db()
    setup_shopify()
    try:
        ensure_required_shopify_webhooks()
    except Exception:
        print("Warning: could not ensure Shopify webhooks")
    try:
        ensure_required_aghaje_webhooks()
    except Exception:
        print("Warning: could not ensure Aghaje webhooks")
    threading.Thread(target=warm_runtime_caches, daemon=True, name="runtime-cache-warm").start()


if __name__ == "__main__":
    restart_thread = threading.Thread(target=check_restart_times, daemon=True)
    restart_thread.start()
    app.run(host="0.0.0.0", port=5001, debug=True)
