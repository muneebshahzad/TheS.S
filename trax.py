import html
import re
import ssl

import certifi
from aiohttp import ClientTimeout


PUBLIC_TRACKING_URL = "https://sonic.pk/tracking"
PUBLIC_TRACKING_ENDPOINT = "https://sonic.pk/tracking/track"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


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


async def fetch_public_tracking_history(session, tracking_number):
    """Read the no-login Trax/Sonic tracker and return portal-shaped events."""
    digits = re.sub(r"\D", "", str(tracking_number or ""))
    if not is_trax_tracking_number(digits):
        return []
    timeout = ClientTimeout(total=12)
    try:
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
    except Exception as error:
        print(f"Trax public tracking unavailable for {digits}: {error}")
        return []
