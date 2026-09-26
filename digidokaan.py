import asyncio
import os
import ssl
import time
from datetime import datetime

import certifi
from aiohttp import ClientTimeout


_token = ""
_token_created_at = 0.0
_status_cache = {}
_inflight = {}
_payments_cache = {}
_TOKEN_TTL_SECONDS = 6 * 60 * 60
_ACTIVE_CACHE_SECONDS = 5 * 60
_TERMINAL_CACHE_SECONDS = 24 * 60 * 60
_PAYMENTS_CACHE_SECONDS = 5 * 60
_TERMINAL_STATUSES = {"delivered", "returned", "return delivered", "cancelled"}
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _event_timestamp(event):
    raw = str((event or {}).get("date_time") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).timestamp()
        except ValueError:
            continue
    return None


def _latest_tracking_event(events):
    valid = [event for event in (events or []) if isinstance(event, dict) and str(event.get("status") or "").strip()]
    if not valid:
        return None
    dated = [(timestamp, event) for event in valid if (timestamp := _event_timestamp(event)) is not None]
    # DigiDokaan currently sends newest-first. Keep that as the fallback if a
    # future courier payload contains an unrecognised date format.
    return max(dated, key=lambda item: item[0])[1] if dated else valid[0]


def _money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _normalise_date(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
        pass
    for pattern in ("%d-%m-%Y %I:%M %p", "%d/%m/%Y %I:%M %p", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(raw, pattern).isoformat(timespec="seconds")
        except ValueError:
            continue
    return raw


def _payment_record(order, detail_body):
    data = detail_body.get("data") if isinstance(detail_body, dict) else None
    detail = data.get("order_detail") if isinstance(data, dict) else None
    tracking = data.get("tracking_response") if isinstance(data, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    tracking = tracking if isinstance(tracking, dict) else {}
    latest = _latest_tracking_event(tracking.get("data"))
    shipment_status = str(
        (latest or {}).get("status")
        or order.get("courier_status")
        or order.get("status")
        or "Unknown"
    ).strip()
    upper_status = shipment_status.upper()
    order_amount = _money(detail.get("amount") if detail else order.get("price"))
    delivery_charges = _money(tracking.get("delivery_charges"))
    other_charges = _money(tracking.get("other_charges"))
    reserve_amount = _money(tracking.get("reserve_amount"))
    net_cod = _money(tracking.get("total_cod_amount"))
    if not net_cod and order_amount:
        net_cod = max(round(order_amount - delivery_charges - other_charges - reserve_amount, 2), 0)
    amount_paid = _money(tracking.get("amount_paid"))
    settlement_id = str(tracking.get("settlement_id") or "").strip()
    raw_payment_status = str(detail.get("payment_status") or "").strip()
    final_returns = (
        "RETURNED TO SHIPPER", "RETURNED TO SENDER", "RETURN SUBMITTED",
        "RETURN COMPLETED", "RETURN DELIVERED", "RTO DELIVERED",
        "RTO COMPLETED", "CANCELLED", "CANCELED",
    )
    is_delivered = "UNDELIVERED" not in upper_status and (
        upper_status == "DELIVERED"
        or upper_status.endswith(" - DELIVERED")
        or upper_status.startswith("DELIVERED ")
    )
    is_not_payable = any(value in upper_status for value in final_returns)
    raw_paid = raw_payment_status.casefold() in {"1", "paid", "settled", "completed"}
    if settlement_id or raw_paid or (net_cod > 0 and amount_paid >= net_cod):
        payment_status = "Settled"
    elif amount_paid > 0:
        payment_status = "Partially paid"
    elif is_not_payable:
        payment_status = "Not payable"
    elif is_delivered:
        payment_status = "Pending settlement"
    else:
        payment_status = "Awaiting delivery"
    outstanding = max(round(net_cod - amount_paid, 2), 0)
    return {
        "order_id": str(order.get("external_reference_no") or order.get("order_id") or "").strip(),
        "digidokaan_order_id": str(order.get("order_id") or "").strip(),
        "tracking_number": str(order.get("tracking_no") or "").strip(),
        "created_at": _normalise_date(order.get("created_at")),
        "shipment_status": shipment_status,
        "payment_mode": str(detail.get("payment_mode") or order.get("payment_mode") or "COD").strip(),
        "payment_status": payment_status,
        "raw_payment_status": raw_payment_status,
        "order_amount": order_amount,
        "delivery_charges": delivery_charges,
        "other_charges": other_charges,
        "reserve_amount": reserve_amount,
        "net_cod": net_cod,
        "amount_paid": amount_paid,
        "outstanding": outstanding,
        "settlement_id": settlement_id,
    }


def summarize_payment_records(records):
    summary = {
        "shipments": len(records),
        "gross_cod": 0.0,
        "deductions": 0.0,
        "expected_net_cod": 0.0,
        "amount_paid": 0.0,
        "pending_settlement": 0.0,
        "awaiting_delivery": 0.0,
        "settled_shipments": 0,
    }
    for record in records:
        summary["gross_cod"] += _money(record.get("order_amount"))
        summary["deductions"] += sum(
            _money(record.get(field)) for field in ("delivery_charges", "other_charges", "reserve_amount")
        )
        summary["expected_net_cod"] += _money(record.get("net_cod"))
        summary["amount_paid"] += _money(record.get("amount_paid"))
        if record.get("payment_status") in {"Pending settlement", "Partially paid"}:
            summary["pending_settlement"] += _money(record.get("outstanding"))
        if record.get("payment_status") == "Awaiting delivery":
            summary["awaiting_delivery"] += _money(record.get("net_cod"))
        if record.get("payment_status") == "Settled":
            summary["settled_shipments"] += 1
    for key, value in list(summary.items()):
        if isinstance(value, float):
            summary[key] = round(value, 2)
    return summary


def configuration():
    values = {
        "base_url": (os.getenv("DIGIDOKAAN_API_URL") or "https://digidokaan.pk").rstrip("/"),
        "phone": (os.getenv("DIGIDOKAAN_PHONE") or "").strip(),
        "password": os.getenv("DIGIDOKAAN_PASSWORD") or "",
        "gateway_id": str(os.getenv("DIGIDOKAAN_GATEWAY_ID") or "5").strip(),
    }
    if not values["phone"] or not values["password"]:
        return None
    return values


async def _access_token(session, config):
    global _token, _token_created_at
    if _token and time.monotonic() - _token_created_at < _TOKEN_TTL_SECONDS:
        return _token

    loop = asyncio.get_running_loop()
    lock = getattr(loop, "digidokaan_auth_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        loop.digidokaan_auth_lock = lock

    async with lock:
        if _token and time.monotonic() - _token_created_at < _TOKEN_TTL_SECONDS:
            return _token
        async with session.post(
            config["base_url"] + "/api/auth/login",
            json={"phone": config["phone"], "password": config["password"]},
            headers={"Accept": "application/json"},
            timeout=ClientTimeout(total=20),
            ssl=_SSL_CONTEXT,
        ) as response:
            payload = await response.json(content_type=None)
        token = str(payload.get("token") or "").strip() if isinstance(payload, dict) else ""
        if response.status != 200 or not token:
            raise RuntimeError("DigiDokaan authentication failed")
        _token = token
        _token_created_at = time.monotonic()
        return token


def _cached_status(tracking_number):
    cached = _status_cache.get(tracking_number)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    if cached:
        _status_cache.pop(tracking_number, None)
    return None


async def _fetch_status(session, tracking_number, config):
    global _token, _token_created_at
    token = await _access_token(session, config)
    payload = {
        "phone": config["phone"],
        "search_value": tracking_number,
        "gateway_id": config["gateway_id"],
    }
    headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
    async with session.post(
        config["base_url"] + "/api/orders/search-courier-order",
        json=payload,
        headers=headers,
        timeout=ClientTimeout(total=20),
        ssl=_SSL_CONTEXT,
    ) as response:
        body = await response.json(content_type=None)

    if response.status == 401 or (isinstance(body, dict) and body.get("code") == 401):
        _token = ""
        _token_created_at = 0.0
        headers["Authorization"] = "Bearer " + await _access_token(session, config)
        async with session.post(
            config["base_url"] + "/api/orders/search-courier-order",
            json=payload,
            headers=headers,
            timeout=ClientTimeout(total=20),
            ssl=_SSL_CONTEXT,
        ) as response:
            body = await response.json(content_type=None)

    rows = body.get("data") if isinstance(body, dict) else None
    if response.status != 200 or not isinstance(rows, list) or not rows:
        return None
    exact = next(
        (row for row in rows if str(row.get("tracking_no") or "").strip() == tracking_number),
        rows[0],
    )
    summary_status = str(exact.get("courier_status") or exact.get("status") or "").strip() or None
    order_id = str(exact.get("order_id") or "").strip()
    if not order_id:
        return summary_status

    # The search endpoint's courier_status can lag behind the shipment
    # timeline. The order detail endpoint is the authoritative source for the
    # latest tracking event shown in DigiDokaan itself.
    try:
        async with session.post(
            config["base_url"] + "/api/seller/order/get_single_order_detail",
            json={"phone": config["phone"], "order_no": order_id},
            headers=headers,
            timeout=ClientTimeout(total=20),
            ssl=_SSL_CONTEXT,
        ) as detail_response:
            detail_body = await detail_response.json(content_type=None)
        data = detail_body.get("data") if isinstance(detail_body, dict) else None
        tracking = data.get("tracking_response") if isinstance(data, dict) else None
        events = tracking.get("data") if isinstance(tracking, dict) else None
        latest = _latest_tracking_event(events)
        if detail_response.status == 200 and latest:
            return str(latest.get("status") or "").strip() or summary_status
    except Exception:
        # Preserve the usable search result if the detail endpoint is
        # temporarily unavailable.
        pass
    return summary_status


async def fetch_tracking_status(session, tracking_number):
    """Return a DigiDokaan status while deduplicating concurrent lookups."""
    normalized = "".join(character for character in str(tracking_number or "") if character.isdigit())
    if not normalized:
        return None
    cached = _cached_status(normalized)
    if cached:
        return cached
    config = configuration()
    if not config:
        return None

    task = _inflight.get(normalized)
    if task is None:
        task = asyncio.create_task(_fetch_status(session, normalized, config))
        _inflight[normalized] = task
    try:
        status = await task
        if status:
            ttl = _TERMINAL_CACHE_SECONDS if status.casefold() in _TERMINAL_STATUSES else _ACTIVE_CACHE_SECONDS
            _status_cache[normalized] = (status, time.monotonic() + ttl)
        return status
    except Exception as error:
        print(f"DigiDokaan tracking unavailable for {normalized}: {error}")
        return None
    finally:
        if _inflight.get(normalized) is task:
            _inflight.pop(normalized, None)


async def fetch_tracking_history(session, tracking_number):
    """Return DigiDokaan events in the portal's existing tracking-history shape."""
    normalized = "".join(character for character in str(tracking_number or "") if character.isdigit())
    config = configuration()
    if not normalized or not config:
        return []
    try:
        token = await _access_token(session, config)
        headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
        timeout = ClientTimeout(total=20)
        async with session.post(
            config["base_url"] + "/api/orders/search-courier-order",
            json={"phone": config["phone"], "search_value": normalized, "gateway_id": config["gateway_id"]},
            headers=headers,
            timeout=timeout,
            ssl=_SSL_CONTEXT,
        ) as response:
            search_body = await response.json(content_type=None)
        rows = search_body.get("data") if isinstance(search_body, dict) else None
        if response.status != 200 or not isinstance(rows, list) or not rows:
            return []
        order = next(
            (row for row in rows if str(row.get("tracking_no") or "").strip() == normalized),
            rows[0],
        )
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            return []
        async with session.post(
            config["base_url"] + "/api/seller/order/get_single_order_detail",
            json={"phone": config["phone"], "order_no": order_id},
            headers=headers,
            timeout=timeout,
            ssl=_SSL_CONTEXT,
        ) as response:
            detail_body = await response.json(content_type=None)
        data = detail_body.get("data") if isinstance(detail_body, dict) else None
        detail = data.get("order_detail") if isinstance(data, dict) else None
        tracking = data.get("tracking_response") if isinstance(data, dict) else None
        events = tracking.get("data") if isinstance(tracking, dict) else None
        if response.status != 200 or not isinstance(detail, dict) or not isinstance(events, list):
            return []
        history = []
        for event in reversed(events):
            if not isinstance(event, dict) or not str(event.get("status") or "").strip():
                continue
            history.append(
                {
                    "ConsignmentNo": normalized,
                    "TransactionDate": event.get("date_time") or "",
                    "ProcessDescForPortal": event.get("status") or "",
                    "ReasonDesc": event.get("status_reason") or "",
                    "ConsigneeName": detail.get("customer_name") or "",
                    "ConsigneeCity": detail.get("customer_city") or "",
                }
            )
        return history
    except Exception as error:
        print(f"DigiDokaan tracking history unavailable for {normalized}: {error}")
        return []


async def fetch_payment_records(session, search_value=None, force=False):
    """Return non-customer DigiDokaan COD and settlement fields for the account."""
    query = str(search_value or os.getenv("DIGIDOKAAN_TRACKING_SEARCH_PREFIX") or "223").strip()
    cached = _payments_cache.get(query)
    if not force and cached and cached[1] > time.monotonic():
        return cached[0]
    config = configuration()
    if not config or not query:
        return []
    try:
        token = await _access_token(session, config)
        headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
        timeout = ClientTimeout(total=30)
        async with session.post(
            config["base_url"] + "/api/orders/search-courier-order",
            json={"phone": config["phone"], "search_value": query, "gateway_id": config["gateway_id"]},
            headers=headers,
            timeout=timeout,
            ssl=_SSL_CONTEXT,
        ) as response:
            body = await response.json(content_type=None)
        rows = body.get("data") if isinstance(body, dict) else None
        if response.status != 200 or not isinstance(rows, list):
            return []

        semaphore = asyncio.Semaphore(6)

        async def fetch_one(order):
            order_id = str(order.get("order_id") or "").strip()
            detail_body = {}
            if order_id:
                try:
                    async with semaphore:
                        async with session.post(
                            config["base_url"] + "/api/seller/order/get_single_order_detail",
                            json={"phone": config["phone"], "order_no": order_id},
                            headers=headers,
                            timeout=timeout,
                            ssl=_SSL_CONTEXT,
                        ) as detail_response:
                            if detail_response.status == 200:
                                detail_body = await detail_response.json(content_type=None)
                except Exception:
                    detail_body = {}
            return _payment_record(order, detail_body)

        records = await asyncio.gather(*(fetch_one(order) for order in rows if isinstance(order, dict)))
        records.sort(key=lambda record: record.get("created_at") or "", reverse=True)
        _payments_cache[query] = (records, time.monotonic() + _PAYMENTS_CACHE_SECONDS)
        return records
    except Exception as error:
        print(f"DigiDokaan payments unavailable: {error}")
        return []
