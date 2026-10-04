import asyncio
import json
from datetime import date
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


def test_submitted_shipper_advice_stays_hidden_while_provider_feed_is_stale(monkeypatch):
    saved = {}
    monkeypatch.setattr(main, "get_app_setting", lambda _key, default="": json.dumps(saved) if saved else default)
    monkeypatch.setattr(main, "set_app_setting", lambda _key, value: saved.update(json.loads(value)) or True)
    monkeypatch.setattr(main, "order_details", [])

    async def stale_pending(_client, force=False):
        return [
            {"tracking_no": "223 17467960795", "external_reference_no": "PK1"},
            {"tracking_no": "22317467960796", "external_reference_no": "PK2"},
        ]

    monkeypatch.setattr(main, "fetch_pending_shipper_advice", stale_pending)
    assert main.acknowledge_shipper_advice("223 17467960795", now=1000)
    monkeypatch.setattr(main.time, "time", lambda: 1001)
    visible = main.load_shipper_advice_sync(force=True)
    assert [row["tracking_no"] for row in visible] == ["22317467960796"]


def test_shipper_advice_acknowledgement_expires(monkeypatch):
    saved = {"22317467960795": 1000}
    monkeypatch.setattr(main, "get_app_setting", lambda _key, default="": json.dumps(saved))
    monkeypatch.setattr(main, "set_app_setting", lambda _key, value: True)
    assert main.load_acknowledged_shipper_advice(
        now=1000 + main.SHIPPER_ADVICE_ACK_TTL_SECONDS + 1
    ) == {}


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
    assert main.build_undelivered_order_view([returned]) == []

    assert main.is_dispatched_not_delivered_order({**dispatched, "tags": []}) is True
    assert main.is_dispatched_not_delivered_order({**dispatched, "tags": [], "status": "Booked"}) is False
    assert main.is_dispatched_not_delivered_order({**dispatched, "tags": dispatched["tags"] + ["Delivered (2026-09-29)"]}) is False
    assert main.is_dispatched_not_delivered_order({**dispatched, "status": "Delivered"}) is False
    assert main.is_dispatched_not_delivered_order({**dispatched, "cancelled_at": "2026-09-29T10:00:00"}) is False


def test_undelivered_ui_has_two_sections_contact_workflow_and_folded_returns(monkeypatch):
    order = {
        "id": 1,
        "order_id": "PK1",
        "created_at": "2026-09-20T10:00:00",
        "status": "Delivery Unsuccessful",
        "tags": ["Dispatched (2026-09-21)"],
        "customer_details": {},
        "line_items": [{"product_title": "Lamp", "tracking_number": "LE123", "status": "Delivery Unsuccessful", "image_src": "https://example.com/lamp.jpg"}],
        "total_price": 1000,
    }
    monkeypatch.setattr(main, "order_details", [order])
    monkeypatch.setattr(main, "load_delivery_followup_contacts", lambda _ids: {})
    client = main.app.test_client()
    for url in ("/undelivered", "/undelivered?embedded=1"):
        html = client.get(url).get_data(as_text=True)
        assert "Delivery follow-up" in html
        assert 'id="hideReturns"' not in html
        assert "Undelivered" in html
        assert "Return Missed" in html
        assert "Contacted" in html
        assert ">View</a>" in html
        assert "Lamp" in html
        assert "LE123" in html
        assert 'id="udSort"' in html
        assert "Latest first" in html
        assert "Oldest first" in html
        assert "sortUndeliveredCards" in html
        assert 'data-section="return_missed"' in html
        assert 'data-section="return_missed" open' not in html


def test_delivery_followup_sections_and_days_are_classified_from_courier_statuses():
    base = {
        "created_at": "2026-09-10T10:00:00",
        "tags": ["Dispatched (2026-09-15)"],
        "customer_details": {},
        "total_price": 1000,
    }
    rows = main.build_undelivered_order_view([
        {**base, "id": 1, "order_id": "SIMPLE", "status": "In Transit", "line_items": []},
        {**base, "id": 2, "order_id": "FAILED", "status": "Delivery Unsuccessful", "line_items": []},
        {**base, "id": 3, "order_id": "CALL", "status": "RETURN SUBMITTED", "line_items": [{"status": "RETURN SUBMITTED", "return_marked_at": "2026-09-25"}]},
        {**base, "id": 4, "order_id": "LEOPARDS", "status": "Return To Sender", "line_items": [{"status": "Return To Sender", "return_marked_at": "2026-09-24"}]},
        {**base, "id": 5, "order_id": "DIGIDOKAAN", "status": "Returned to Shipper", "line_items": [{"status": "Returned to Shipper", "return_marked_at": "2026-09-23"}]},
        {**base, "id": 6, "order_id": "COURIER-DISPATCH", "status": "Dispatched to ISLAMABAD", "tags": [], "line_items": [{"status": "Dispatched to ISLAMABAD", "dispatched_at": "2026-09-18"}]},
    ])
    by_id = {row["order_id"]: row for row in rows}
    assert by_id["SIMPLE"]["followup_section"] == "undelivered"
    assert by_id["FAILED"]["followup_section"] == "undelivered"
    assert by_id["FAILED"]["needs_attention"] is True
    assert by_id["CALL"]["followup_section"] == "return_missed"
    assert by_id["CALL"]["needs_attention"] is True
    assert by_id["LEOPARDS"]["followup_section"] == "return_missed"
    assert by_id["DIGIDOKAAN"]["followup_section"] == "return_missed"
    assert by_id["COURIER-DISPATCH"]["followup_section"] == "undelivered"
    assert by_id["COURIER-DISPATCH"]["dispatch_date"] == "2026-09-18"
    assert by_id["CALL"]["order_age_days"] >= by_id["CALL"]["dispatch_age_days"]
    assert by_id["CALL"]["return_marked_days"] is not None


def test_return_courier_event_date_is_extracted_for_leopards_and_digidokaan():
    leopards = {"packet_list": [{"Tracking Detail": [
        {"Status": "In Transit", "Activity_Date": "23/09/2026", "Activity_Time": "09:00 AM"},
        {"Status": "Return To Sender", "Activity_Date": "25/09/2026", "Activity_Time": "10:00 AM"},
    ]}]}
    digidokaan = [
        {"ProcessDescForPortal": "In Transit", "TransactionDate": "23/09/2026 09:00 AM"},
        {"ProcessDescForPortal": "Returned to Shipper", "TransactionDate": "26/09/2026 02:13 PM"},
    ]
    assert main.courier_return_marked_date("LE123", leopards) == "2026-09-25"
    assert main.courier_return_marked_date("22312345678901", digidokaan) == "2026-09-26"
    assert main.courier_dispatched_date("LE123", leopards) == "2026-09-23"
    assert main.courier_dispatched_date("22312345678901", digidokaan) == "2026-09-23"


def test_delivery_contact_endpoint_validates_and_saves_history(monkeypatch):
    monkeypatch.setattr(main, "add_delivery_followup_contact", lambda order_id, outcome, remarks: {
        "id": 9, "shopify_order_id": order_id, "outcome": outcome,
        "remarks": remarks, "created_at": "2026-09-29T10:30:00+05:00",
    })
    client = main.app.test_client()
    invalid = client.post("/undelivered/contact", json={"order_id": "1", "outcome": "Unknown"})
    assert invalid.status_code == 400
    saved = client.post("/undelivered/contact", json={
        "order_id": "1",
        "outcome": "WhatsApp - Awaiting Reply",
        "remarks": "Customer messaged",
    })
    assert saved.status_code == 200
    assert saved.get_json()["contact"]["remarks"] == "Customer messaged"


def test_tickbags_invoice_groups_dispatched_and_lahore_beanbags_by_week():
    orders = [
        {
            "id": 1, "order_id": "PK1", "tags": ["Dispatched (2026-10-03)"], "status": "In Transit",
            "line_items": [
                {"product_title": "Cocoon Bean Bag Sofa", "variant_id": 11, "tracking_number": "2231", "quantity": 2, "unit_cost": 3000, "status": "In Transit"},
                {"product_title": "Table Lamp", "variant_id": 12, "tracking_number": "2232", "quantity": 1, "unit_cost": 1000, "status": "In Transit"},
            ],
        },
        {
            "id": 2, "order_id": "PK2", "tags": ["Delivered in Lahore Approved (2026-10-06)"], "status": "Delivered",
            "line_items": [{"product_title": "Football Beanbag", "variant_id": 13, "tracking_number": "N/A", "quantity": 1, "unit_cost": 2500, "status": "Delivered"}],
        },
    ]
    batches = main.build_tickbags_invoice_source(orders, today=date(2026, 10, 6))
    by_end = {batch["invoice"]["period_end"]: batch for batch in batches}
    assert by_end[date(2026, 10, 4)]["invoice"]["name"] == "Till 4 Oct'2026"
    assert [line["product_name"] for line in by_end[date(2026, 10, 4)]["lines"]] == ["Cocoon Bean Bag Sofa"]
    assert by_end[date(2026, 10, 11)]["lines"][0]["order_number"] == "PK2"


def test_tickbags_return_is_highlighted_and_totals_include_current_refund():
    orders = [{
        "id": 3, "order_id": "PK3", "tags": ["Dispatched (2026-10-06)", "Return Received (2026-10-08)"],
        "status": "Returned to Shipper", "line_items": [{
            "product_title": "Classic Bean Bag", "variant_id": 14, "tracking_number": "2233",
            "quantity": 2, "unit_cost": 2000, "status": "Returned to Shipper",
        }],
    }]
    line = main.build_tickbags_invoice_source(orders, today=date(2026, 10, 8))[0]["lines"][0]
    assert line["is_returned"] is True
    invoice = main.present_tickbags_invoices([{
        "id": 1, "status": "Draft", "lines": [{**line, "id": 9}],
        "refunds": [{"amount": 4000}],
    }])[0]
    assert invoice["products_total"] == 4000
    assert invoice["refunds_total"] == 4000
    assert invoice["payable"] == 0


def test_tickbags_invoice_template_has_required_ledger_controls():
    source = (Path(__file__).resolve().parents[1] / "templates" / "tickbags_invoices.html").read_text()
    for label in ("Products total", "Refunds", "Payable", "Adjusted in Payments", "Received in Bank", "Reverse in current invoice"):
        assert label in source

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
