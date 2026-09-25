import asyncio
import os
import ssl
import time

import certifi
from aiohttp import ClientTimeout


_token = ""
_token_created_at = 0.0
_status_cache = {}
_inflight = {}
_TOKEN_TTL_SECONDS = 6 * 60 * 60
_ACTIVE_CACHE_SECONDS = 5 * 60
_TERMINAL_CACHE_SECONDS = 24 * 60 * 60
_TERMINAL_STATUSES = {"delivered", "returned", "return delivered", "cancelled"}
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


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
    return str(exact.get("courier_status") or exact.get("status") or "").strip() or None


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
