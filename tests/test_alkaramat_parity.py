import asyncio
import json
from datetime import date
from types import SimpleNamespace
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


def test_leopards_shipper_advice_uses_ra_rt_payload(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": 1, "error": 0, "data": "Updated"}

    def fake_post(url, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setenv("LEOPARD_API_KEY", "key")
    monkeypatch.setenv("LEOPARD_PASSWORD", "password")
    monkeypatch.setattr(main.requests, "post", fake_post)
    result = main.submit_leopards_shipper_advice(12, "LE123", "reattempt", "Customer interested")
    assert result["data"] == "Updated"
    assert captured["payload"]["data"][0] == {
        "id": 12, "cn_number": "LE123", "shipper_advice_status": "RA",
        "shipper_remarks": "Customer interested",
    }


def test_leopards_shipper_advice_normalizes_pending_feed(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": 1, "data": [{
                "id": 7, "cn_number": "LE999", "reason": "Consignee refused",
                "shipper_advice_status": "",
            }]}

    monkeypatch.setenv("LEOPARD_API_KEY", "key")
    monkeypatch.setenv("LEOPARD_PASSWORD", "password")
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: Response())
    rows = main.fetch_leopards_shipper_advice_sync()
    assert rows == [{
        "advice_id": 7, "tracking_no": "LE999", "external_reference_no": "",
        "courier_status_reason": "Consignee refused", "courier_status": "",
        "shipper_remarks": "", "courier_source": "leopards",
    }]


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


def test_pending_page_separates_beanbags_from_other_vendor_items(monkeypatch):
    monkeypatch.setattr(main, "build_pending_orders_mobile_data", lambda: [{
        "financial_status": "pending", "pending_total_price": 3000, "pending_total_cost": 1200,
        "items_list": [
            {"item_title": "ComfyNest Bean Bag - Red", "quantity": 1, "is_beanbag": True},
            {"item_title": "Wall Clock", "quantity": 2, "is_beanbag": False},
        ],
    }])
    _, items, _ = main.build_pending_items_table_data()
    by_title = {item["item_title"]: item for item in items}
    assert by_title["ComfyNest Bean Bag - Red"]["is_beanbag"] is True
    assert by_title["Wall Clock"]["is_beanbag"] is False

    source = (Path(__file__).resolve().parents[1] / "templates" / "pending.html").read_text()
    assert "🧺 Bean Bags" in source
    assert "📦 Other Products" in source
    assert "Add all bean bags" in source
    assert "Add all other items" in source


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


def test_tickbags_tag_includes_custom_products_and_adjustments_reduce_balance():
    orders = [{
        "id": 4, "order_id": "PK4", "tags": ["BeanBag", "Dispatched (2026-10-06)"],
        "status": "In Transit", "line_items": [{
            "product_title": "Custom product", "variant_id": 15, "tracking_number": "2234",
            "quantity": 2, "unit_cost": 5000, "status": "In Transit",
        }],
    }]
    line = main.build_tickbags_invoice_source(orders, today=date(2026, 10, 8))[0]["lines"][0]
    assert line["product_name"] == "Custom product"
    invoice = main.present_tickbags_invoices([{
        "id": 2, "status": "Draft", "lines": [{**line, "id": 10}], "refunds": [],
        "adjustments": [{"amount": 5000}],
    }])[0]
    assert invoice["products_total"] == 10000
    assert invoice["adjustments_total"] == 5000
    assert invoice["payable"] == 5000
    assert invoice["receivable"] == 0


def test_tickbags_overpayment_is_preserved_as_receivable():
    invoice = main.present_tickbags_invoices([{
        "id": 3, "status": "Draft", "lines": [], "refunds": [],
        "adjustments": [{"amount": 44624}],
    }])[0]
    assert invoice["net_balance"] == -44624
    assert invoice["payable"] == 0
    assert invoice["receivable"] == 44624


def test_tickbags_fulfilled_without_tracking_requires_lahore_approval():
    base = {
        "id": 5, "order_id": "PK5", "tags": ["BeanBag", "Delivered in Lahore"],
        "status": "Fulfilled", "created_at": "2026-10-06T10:00:00+05:00",
        "line_items": [{"product_title": "Custom beanbag", "tracking_number": "N/A", "quantity": 1}],
    }
    assert all(not batch["lines"] for batch in main.build_tickbags_invoice_source([base], today=date(2026, 10, 8)))
    approved = {**base, "tags": ["BeanBag", "Delivered in Lahore Approved (2026-10-08)"]}
    lines = [line for batch in main.build_tickbags_invoice_source([approved], today=date(2026, 10, 8)) for line in batch["lines"]]
    assert len(lines) == 1
    assert lines[0]["tracking_number"] == ""


def test_tickbags_removed_shopify_item_is_not_in_invoice():
    order = {
        "id": 2937, "order_id": "PK2937A01",
        "tags": ["BeanBag", "Delivered in Lahore Approved (2026-09-25)"],
        "status": "Fulfilled", "line_items": [
            {"product_title": "Kept football beanbag", "quantity": 2, "current_quantity": 2},
            {"product_title": "Removed ottoman", "quantity": 2, "current_quantity": 0},
        ],
    }
    lines = [line for batch in main.build_tickbags_invoice_source([order], today=date(2026, 10, 5)) for line in batch["lines"]]
    assert [(line["product_name"], line["quantity"]) for line in lines] == [("Kept football beanbag", 2)]


def test_tickbags_pending_lahore_orders_excludes_tracked_orders(monkeypatch):
    monkeypatch.setattr(main, "load_order_statuses", lambda: {})
    orders = [{
        "id": 6, "order_id": "PK6", "tags": ["BeanBag"], "status": "Pending",
        "customer_details": {"name": "Customer", "city": "Lahore"},
        "line_items": [{"product_title": "Custom product", "tracking_number": "N/A", "quantity": 1}],
    }, {
        "id": 7, "order_id": "PK7", "tags": ["BeanBag"], "status": "In Transit",
        "customer_details": {"name": "Customer", "city": "Lahore"},
        "line_items": [{"product_title": "Custom product", "tracking_number": "223123", "quantity": 1}],
    }]
    pending = main.build_tickbags_pending_lahore_orders(orders)
    assert [row["order_id"] for row in pending] == ["PK6"]


def test_tickbags_invoice_template_has_required_ledger_controls():
    source = (Path(__file__).resolve().parents[1] / "templates" / "tickbags_invoices.html").read_text()
    for label in ("Products total", "Refunds", "Payable", "Receivable", "Adjusted in Payments", "Received in Bank", "Reverse in current invoice", "Current {{ current_balance_type }}", "Record partial payment", "Mark as Final Invoice", "Product costs", "Update all changes", "Hide products with saved costs"):
        assert label in source


def test_tickbags_product_cost_rows_sort_by_open_invoice_units():
    invoices = [{"status": "Draft", "lines": [
        {"variant_id": "1", "product_name": "Low", "quantity": 1, "unit_cost": 10},
        {"variant_id": "2", "product_name": "High", "quantity": 4, "unit_cost": 20},
        {"variant_id": "2", "product_name": "High", "quantity": 2, "unit_cost": 20},
    ]}, {"status": "Final", "lines": [
        {"variant_id": "3", "product_name": "Locked", "quantity": 99, "unit_cost": 30},
    ]}]
    rows = main.build_tickbags_product_cost_rows(invoices, catalog=[])
    assert [(row["product_name"], row["units_sold"]) for row in rows] == [("High", 6), ("Low", 1)]


def test_tickbags_product_cost_groups_keep_colours_together():
    rows = [
        {"product_title": "Cocoon", "product_name": "Cocoon - Olive", "units_sold": 1},
        {"product_title": "Other", "product_name": "Other - Red", "units_sold": 2},
        {"product_title": "Cocoon", "product_name": "Cocoon - Black", "units_sold": 4},
    ]
    groups = main.group_tickbags_product_cost_rows(rows)
    assert groups[0]["name"] == "Cocoon"
    assert [row["product_name"] for row in groups[0]["variants"]] == ["Cocoon - Black", "Cocoon - Olive"]
    assert groups[0]["units_sold"] == 5


def test_tickbags_invoice_template_renders_pending_lahore_items():
    with main.app.test_request_context("/tickbags-invoices"):
        rendered = main.app.jinja_env.get_template("tickbags_invoices.html").render(
            invoices=[], current_balance=0, pending_lahore_orders=[{
                "order_id": "PK-LHR", "created_at": "2026-10-05T10:00:00+05:00",
                "customer_name": "Customer", "city": "Lahore", "total_price": 10000,
                "awaiting_approval": False,
                "items": [{"image_src": "", "product_title": "Custom BeanBag", "quantity": 1}],
            }],
        )
    assert "PK-LHR" in rendered
    assert "Custom BeanBag" in rendered
    assert "Mark delivered in Lahore" in rendered


def test_tickbags_legacy_backfill_uses_first_courier_dispatch_date(monkeypatch):
    order = SimpleNamespace(
        id=10, name="PK10", tags="", cancelled_at=None, fulfillment_status="fulfilled",
        created_at="2026-09-20T10:00:00+00:00",
        fulfillments=[SimpleNamespace(tracking_number="223123", status="success", created_at="2026-09-25T12:00:00+00:00", updated_at="")],
        line_items=[SimpleNamespace(title="Football Seat", variant_title="Red", variant_id=44, product_id=4, quantity=2)],
    )
    captured = {}
    monkeypatch.setattr(main, "setup_shopify", lambda: None)
    monkeypatch.setattr(main, "fetch_all_shopify_orders", lambda *_args: [order])
    monkeypatch.setattr(main, "get_active_shopify_products", lambda **_kwargs: [{
        "variant_id": 44, "product_id": 4, "product_type": "bean bag", "cost": 1500, "image": "seat.jpg",
    }])
    monkeypatch.setattr(main, "refresh_tracking_summaries_sync", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(main, "get_tracking_summary_cache_only", lambda _tracking: {
        "status": "Out for Delivery", "dispatched_at": "2026-09-25",
    })
    monkeypatch.setattr(main, "sync_vendor_invoice", lambda invoice, lines: captured.update(invoice=invoice, lines=lines))
    monkeypatch.setattr(main, "prune_vendor_invoice_lines", lambda *_args: 0)
    assert main.backfill_tickbags_invoice(main.tickbags_invoice_period(date(2026, 10, 4))) == 1
    assert captured["lines"][0]["eligible_date"] == date(2026, 9, 25)
    assert captured["lines"][0]["unit_cost"] == 1500
    assert captured["lines"][0]["tracking_number"] == "223123"
    assert captured["lines"][0]["order_status"] == "Out for Delivery"


def test_tickbags_legacy_backfill_includes_fulfilled_plain_lahore_delivery(monkeypatch):
    order = SimpleNamespace(
        id=2937, name="PK2937A01", tags="BeanBag, Delivered in Lahore",
        cancelled_at=None, fulfillment_status="fulfilled",
        created_at="2026-09-25T10:00:00+05:00", processed_at="2026-09-25T10:00:00+05:00",
        fulfillments=[],
        line_items=[SimpleNamespace(
            title="Black&White Football xxxl with stool-Leather", variant_title="Default Title",
            variant_id=None, product_id=None, quantity=2,
        )],
    )
    captured = {}
    monkeypatch.setattr(main, "setup_shopify", lambda: None)
    monkeypatch.setattr(main, "fetch_all_shopify_orders", lambda *_args: [order])
    monkeypatch.setattr(main, "get_active_shopify_products", lambda **_kwargs: [])
    monkeypatch.setattr(main, "sync_vendor_invoice", lambda invoice, lines: captured.update(invoice=invoice, lines=lines))
    monkeypatch.setattr(main, "prune_vendor_invoice_lines", lambda *_args: 0)

    assert main.backfill_tickbags_invoice(main.tickbags_invoice_period(date(2026, 10, 4))) == 1
    assert captured["lines"][0]["order_number"] == "PK2937A01"
    assert captured["lines"][0]["quantity"] == 2
    assert captured["lines"][0]["order_status"] == "Delivered in Lahore"


def test_tickbags_booked_tracking_waits_for_actual_dispatch_week():
    booked = {
        "id": 11, "order_id": "PK11", "tags": ["BeanBag"], "status": "Booked",
        "line_items": [{
            "product_title": "Custom beanbag", "tracking_number": "LE123", "quantity": 1,
            "status": "Booked", "dispatched_at": "",
        }],
    }
    assert all(not batch["lines"] for batch in main.build_tickbags_invoice_source(
        [booked], today=date(2026, 10, 8)
    ))

    dispatched = {
        **booked,
        "status": "In Transit",
        "line_items": [{
            **booked["line_items"][0], "status": "In Transit", "dispatched_at": "2026-10-06",
        }],
    }
    lines = [line for batch in main.build_tickbags_invoice_source(
        [dispatched], today=date(2026, 10, 8)
    ) for line in batch["lines"]]
    assert len(lines) == 1
    assert lines[0]["eligible_date"] == date(2026, 10, 6)
    assert main.tickbags_invoice_period(lines[0]["eligible_date"])["period_end"] == date(2026, 10, 11)


def test_shopify_fulfillment_tracking_supports_tracking_numbers_list():
    fulfillment = SimpleNamespace(tracking_number=None, tracking_numbers=["", "LE123"])
    assert main.shopify_fulfillment_tracking(fulfillment) == "LE123"

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
