import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import digidokaan
import main


def test_delivery_failure_reason_becomes_need_attention_status():
    body = {"data": {"tracking_response": {
        "courier_status": "Awaiting Shipper advice",
        "data": [{"status": "Shipment - Shipper Advise Requested", "status_reason": "Consignee Refused"}],
    }}}
    assert digidokaan.display_status_from_detail(body, "In Transit") == "Undelivered - Consignee Refused"


def test_submit_shipper_advice_uses_validated_payload(monkeypatch):
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def json(self, **kwargs): return {"code": 200, "msg": "Saved"}

    class Session:
        payload = None
        def post(self, _url, **kwargs):
            self.payload = kwargs["json"]
            return Response()

    session = Session()
    monkeypatch.setattr(digidokaan, "configuration", lambda: {"base_url": "https://digidokaan.pk", "phone": "923000000000", "password": "secret", "gateway_id": "5"})
    monkeypatch.setattr(digidokaan, "_access_token", AsyncMock(return_value="token"))
    asyncio.run(digidokaan.submit_shipper_advice(session, "223 17467960795", 5, "Reattempt", "Try tomorrow"))
    assert session.payload["tracking_no"] == "22317467960795"
    assert session.payload["shipper_advice_status"] == "reattempt"


def test_payment_ledger_merges_entries_and_requires_explicit_paid_cheque():
    payments = {
        "balance": {"deliver_orders_payments": 1200},
        "ledger": {"total_balance": 4600, "data": [
            {"tracking_no": "T1", "order_no": "O1", "payment_type": "COD", "amount": 3000, "sub_amount": 0, "cheque_no": "C1"},
            {"tracking_no": "T1", "order_no": "O1", "payment_type": "DC", "amount": 0, "sub_amount": 200, "cheque_no": "C1"},
            {"tracking_no": "T2", "order_no": "O2", "payment_type": "COD", "amount": 2000, "sub_amount": 0},
        ]},
        "cheques": [{"cheque_no": "C1", "status": "Paid", "amount": 2800, "shipments": {"data": []}}],
    }
    shipments = {row["tracking_no"]: row for row in main.build_digidokaan_payment_dashboard(payments)["shipments"]}
    assert shipments["T1"]["net"] == 2800
    assert shipments["T1"]["payment_status"] == "Paid"
    assert shipments["T2"]["payment_status"] == "Not paid"


def test_unpaid_cheque_is_never_paid():
    payments = {
        "balance": {},
        "ledger": {"data": [{"tracking_no": "T1", "payment_type": "COD", "amount": 1000, "cheque_no": "C1"}]},
        "cheques": [{"cheque_no": "C1", "status": "Unpaid", "shipments": {"data": []}}],
    }
    assert main.build_digidokaan_payment_dashboard(payments)["shipments"][0]["payment_status"] != "Paid"


def test_admin_mobile_portal_exposes_install_passkey_and_embedded_navigation():
    client = main.app.test_client()
    login_html = client.get("/admin_portal").get_data(as_text=True)
    assert "Install mobile app" in login_html

    with client.session_transaction() as session:
        session[main.ADMIN_PORTAL_SESSION_KEY] = True
    portal_html = client.get("/admin_portal?section=abandoned").get_data(as_text=True)

    assert "Install App" in portal_html
    assert "setupPasskeyBtn" in portal_html
    assert "bottom-nav" in portal_html
    assert 'data-bottom-section="payments"' in portal_html
    assert '"src": "/abandoned?embedded=1"' in portal_html
    assert '"src": "/payments?embedded=1"' in portal_html
    assert not any(section.get("direct") for section in main.build_admin_mobile_sections())
    assert "/static/sleekspace-logo-v2.png" in portal_html
    assert "/static/admin-portal-icon-v2-192.png" in portal_html


def test_pending_page_has_mobile_friendly_vendor_order_builder():
    source = (Path(__file__).resolve().parents[1] / "templates" / "pending.html").read_text()
    assert 'id="vendorBuilder"' in source
    assert 'class="vendor-dock"' in source
    assert "Add all items" in source
    assert "Create vendor order" in source
    assert "Vendor Purchase Order" in source
    assert "Unit cost" in source
    assert "itemSummarySearch" in source
    assert "existing.quantity = Math.max" in source
    assert '.items-grid { display:grid' in source
    assert 'table, tbody { display:block' in source
    assert 'table { min-width:860px; }' not in source
    assert 'table { min-width:920px; }' not in source


def test_undelivered_uses_dispatch_and_dated_return_received_tags():
    dispatched = {
        "id": 1,
        "order_id": "PK1",
        "created_at": "2026-09-20T10:00:00",
        "status": "In Transit",
        "tags": ["Dispatched (2026-09-21)"],
    }
    assert main.is_dispatched_not_delivered_order(dispatched) is True
    assert main.is_return_received_order({**dispatched, "tags": dispatched["tags"] + ["Return Received"]}) is False
    returned = {**dispatched, "tags": dispatched["tags"] + ["Return Received (2026-09-28)"]}
    assert main.is_return_received_order(returned) is True
    assert main.build_undelivered_order_view([returned])[0]["return_received"] is True

    assert main.is_dispatched_not_delivered_order({**dispatched, "tags": []}) is False
    assert main.is_dispatched_not_delivered_order({**dispatched, "tags": dispatched["tags"] + ["Delivered (2026-09-29)"]}) is False
    assert main.is_dispatched_not_delivered_order({**dispatched, "status": "Delivered"}) is False


def test_undelivered_ui_has_return_filter_in_desktop_and_admin_embed(monkeypatch):
    order = {
        "id": 1,
        "order_id": "PK1",
        "created_at": "2026-09-20T10:00:00",
        "status": "In Transit",
        "tags": ["Dispatched (2026-09-21)", "Return Received (2026-09-28)"],
        "customer_details": {},
        "line_items": [],
        "total_price": 1000,
    }
    monkeypatch.setattr(main, "order_details", [order])
    client = main.app.test_client()
    for url in ("/undelivered", "/undelivered?embedded=1"):
        html = client.get(url).get_data(as_text=True)
        assert "Dispatched, not delivered." in html
        assert 'id="hideReturns"' in html
        assert 'data-return="true"' in html
        assert "Return received 2026-09-28" in html

class FakeDraftOrder:
    instances = []
    def __init__(self):
        self.id, self.order_id, self.name, self.complete_payload = 44, 99, "#1001", None
        self.__class__.instances.append(self)
    def save(self): return True
    def complete(self, payload): self.complete_payload = payload; return True
    @classmethod
    def find(cls, _draft_id): return cls.instances[-1]


@pytest.mark.parametrize(("status", "pending"), [("Unpaid", True), ("Paid", False)])
def test_employee_order_only_marks_explicit_paid_as_paid(monkeypatch, status, pending):
    FakeDraftOrder.instances.clear()
    monkeypatch.setattr(main.shopify, "DraftOrder", FakeDraftOrder)
    monkeypatch.setattr(main, "mark_shopify_order_as_paid", lambda _order_id: True)
    main.create_shopify_employee_order({
        "customer_name": "Test Customer", "phone": "03000000000",
        "city": "Lahore", "address": "Test address",
        "payment_method": "Bank Deposit", "payment_status": status,
        "custom_items": [{"title": "Test item", "price": 1000, "quantity": 1}],
    })
    assert FakeDraftOrder.instances[-1].complete_payload == {"payment_pending": pending}
