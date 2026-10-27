import asyncio
import html
import re
import ssl
import time

import certifi
from aiohttp import ClientTimeout


PUBLIC_TRACKING_URL = "https://sonic.pk/tracking"
PUBLIC_TRACKING_ENDPOINT = "https://sonic.pk/tracking/track"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_history_cache = {}
_inflight = {}
_ACTIVE_CACHE_SECONDS = 5 * 60
_TERMINAL_CACHE_SECONDS = 24 * 60 * 60
_TERMINAL_MARKERS = ("delivered", "returned to shipper", "delivered to shipper", "cancelled")


def is_trax_tracking_number(value):
    """Trax/Sonic public CNs are fourteen numeric characters."""
    digits = re.sub(r"\D", "", str(value or ""))
    return len(digits) == 14


def _csrf_token(page):
    # The public tracker currently embeds the Laravel token in its AJAX call.
    match = re.search(r"['_\"]token['\"]\s*:\s*['\"]([^'\"]+)", page or "")
    if not match:
        match = re.search(r'name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', page or "", re.I)
    return html.unescape(match.group(1)) if match else ""


def _portal_history(payload, tracking_number):
    shipments = payload.get("shipments") if isinstance(payload, dict) else None
    if not isinstance(shipments, dict):
        return []
    digits = re.sub(r"\D", "", str(tracking_number or ""))
    shipment = next(
        (
            row for row in shipments.values()
            if re.sub(r"\D", "", str((row or {}).get("tracking_number") or "")) == digits
        ),
        None,
    )
    if not isinstance(shipment, dict):
        return []
    consignee = shipment.get("consignee") or {}
    history = []
    # Trax returns newest first; the rest of the portal expects oldest first.
    for event in reversed(shipment.get("tracking_history") or []):
        if not isinstance(event, dict) or not str(event.get("status") or "").strip():
            continue
        history.append({
            "ProcessDescForPortal": str(event.get("status") or "").strip(),
            "TransactionDate": str(event.get("date_time") or "").strip(),
            "ConsigneeName": str(consignee.get("name") or "").strip(),
            "ConsigneeCity": str(consignee.get("destination") or "").strip(),
            "TrackingSource": "Trax Direct",
        })
    return history


async def _fetch_public_tracking_history(session, digits):
    timeout = ClientTimeout(total=12)
    async with session.get(PUBLIC_TRACKING_URL, timeout=timeout, ssl=_SSL_CONTEXT) as response:
        if response.status != 200:
            return []
        page = await response.text()
    token = _csrf_token(page)
    if not token:
        return []
    async with session.post(
        PUBLIC_TRACKING_ENDPOINT,
        data={"tracking_numbers": digits, "_token": token},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        if response.status != 200:
            return []
        payload = await response.json(content_type=None)
    return _portal_history(payload, digits)


async def fetch_public_tracking_history(session, tracking_number):
    """Read and cache the no-login Trax/Sonic tracking timeline."""
    digits = re.sub(r"\D", "", str(tracking_number or ""))
    if not is_trax_tracking_number(digits):
        return []
    cached = _history_cache.get(digits)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    if cached:
        _history_cache.pop(digits, None)
    task = _inflight.get(digits)
    if task is None:
        task = asyncio.create_task(_fetch_public_tracking_history(session, digits))
        _inflight[digits] = task
    try:
        history = await task
        if history:
            latest = str(history[-1].get("ProcessDescForPortal") or "").casefold()
            ttl = _TERMINAL_CACHE_SECONDS if any(marker in latest for marker in _TERMINAL_MARKERS) else _ACTIVE_CACHE_SECONDS
            _history_cache[digits] = (history, time.monotonic() + ttl)
        return history
    except Exception as error:
        print(f"Trax public tracking unavailable for {digits}: {error}")
        return []
    finally:
        if _inflight.get(digits) is task:
            _inflight.pop(digits, None)
