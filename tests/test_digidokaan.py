import asyncio
import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import digidokaan


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, content_type=None):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)

    def post(self, *_args, **_kwargs):
        return FakeResponse(*next(self.responses))


class DigiDokaanTrackingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        digidokaan._status_cache.clear()
        digidokaan._inflight.clear()
        digidokaan._payments_cache.clear()

    def test_requires_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(digidokaan.configuration())

    async def test_concurrent_requests_are_deduplicated_and_cached(self):
        async def delayed_status(*args):
            await asyncio.sleep(0)
            return "Delivered"

        environment = {
            "DIGIDOKAAN_PHONE": "923000000000",
            "DIGIDOKAAN_PASSWORD": "secret",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            digidokaan, "_fetch_status", AsyncMock(side_effect=delayed_status)
        ) as fetch:
            statuses = await asyncio.gather(
                digidokaan.fetch_tracking_status(object(), "223 22367960798"),
                digidokaan.fetch_tracking_status(object(), "22322367960798"),
            )
            cached = await digidokaan.fetch_tracking_status(object(), "22322367960798")

        self.assertEqual(statuses, ["Delivered", "Delivered"])
        self.assertEqual(cached, "Delivered")
        fetch.assert_awaited_once()

    async def test_detail_history_wins_over_stale_search_status(self):
        config = {
            "base_url": "https://digidokaan.example",
            "phone": "923000000000",
            "password": "secret",
            "gateway_id": "5",
        }
        session = FakeSession(
            [
                ({"data": [{
                    "tracking_no": "22315868148789",
                    "order_id": "PK2924A01",
                    "courier_status": "Delivery In Transit",
                }]},),
                ({"data": {"tracking_response": {"data": [
                    {"status": "Shipment - Delivery Unsuccessful", "date_time": "26/09/2026 02:13 PM"},
                    {"status": "Shipment - Out for Delivery", "date_time": "26/09/2026 11:28 AM"},
                ]}}},),
            ]
        )
        with patch.object(digidokaan, "_access_token", AsyncMock(return_value="token")):
            status = await digidokaan._fetch_status(session, "22315868148789", config)

        self.assertEqual(status, "Shipment - Delivery Unsuccessful")

    async def test_search_status_is_fallback_when_detail_is_unavailable(self):
        config = {
            "base_url": "https://digidokaan.example",
            "phone": "923000000000",
            "password": "secret",
            "gateway_id": "5",
        }
        session = FakeSession(
            [
                ({"data": [{
                    "tracking_no": "22315868148789",
                    "order_id": "PK2924A01",
                    "courier_status": "Delivery In Transit",
                }]},),
                ({"message": "temporarily unavailable"}, 503),
            ]
        )
        with patch.object(digidokaan, "_access_token", AsyncMock(return_value="token")):
            status = await digidokaan._fetch_status(session, "22315868148789", config)

        self.assertEqual(status, "Delivery In Transit")

    def test_payment_record_separates_active_cod_from_settlement(self):
        record = digidokaan._payment_record(
            {
                "order_id": "10498383",
                "external_reference_no": "PK2924A01",
                "tracking_no": "22315868148789",
                "courier_status": "Delivery In Transit",
                "created_at": "2026-09-25 14:15:00",
                "price": "11950",
            },
            {"data": {
                "order_detail": {"amount": "11950", "payment_mode": "COD", "payment_status": "0"},
                "tracking_response": {
                    "data": [{"status": "Shipment - Delivery Unsuccessful", "date_time": "26/09/2026 02:13 PM"}],
                    "delivery_charges": "307",
                    "other_charges": "0",
                    "reserve_amount": "0",
                    "total_cod_amount": "11643",
                    "amount_paid": "0",
                    "settlement_id": "",
                },
            }},
        )

        self.assertEqual(record["order_id"], "PK2924A01")
        self.assertEqual(record["shipment_status"], "Shipment - Delivery Unsuccessful")
        self.assertEqual(record["payment_status"], "Awaiting delivery")
        self.assertEqual(record["net_cod"], 11643.0)
        self.assertEqual(record["amount_paid"], 0.0)

    def test_payment_record_marks_only_final_payment_states(self):
        base_order = {"order_id": "1", "tracking_no": "2231", "price": "10000"}
        delivered = digidokaan._payment_record(base_order, {"data": {
            "order_detail": {"amount": "10000", "payment_status": "0"},
            "tracking_response": {"data": [{"status": "Shipment - Delivered"}], "total_cod_amount": "9500", "amount_paid": "0"},
        }})
        returned = digidokaan._payment_record(base_order, {"data": {
            "order_detail": {"amount": "10000", "payment_status": "0"},
            "tracking_response": {"data": [{"status": "Shipment - Returned to Shipper"}], "total_cod_amount": "9500", "amount_paid": "0"},
        }})
        settled = digidokaan._payment_record(base_order, {"data": {
            "order_detail": {"amount": "10000", "payment_status": "1"},
            "tracking_response": {"data": [{"status": "Shipment - Delivered"}], "total_cod_amount": "9500", "amount_paid": "9500", "settlement_id": "SET-1"},
        }})

        self.assertEqual(delivered["payment_status"], "Pending settlement")
        self.assertEqual(returned["payment_status"], "Not payable")
        self.assertEqual(settled["payment_status"], "Settled")

    def test_payment_summary_does_not_treat_active_shipments_as_due(self):
        records = [
            {"order_amount": 10000, "delivery_charges": 500, "other_charges": 0, "reserve_amount": 0, "net_cod": 9500, "amount_paid": 0, "outstanding": 9500, "payment_status": "Awaiting delivery"},
            {"order_amount": 12000, "delivery_charges": 500, "other_charges": 0, "reserve_amount": 0, "net_cod": 11500, "amount_paid": 0, "outstanding": 11500, "payment_status": "Pending settlement"},
        ]

        summary = digidokaan.summarize_payment_records(records)

        self.assertEqual(summary["expected_net_cod"], 21000.0)
        self.assertEqual(summary["awaiting_delivery"], 9500.0)
        self.assertEqual(summary["pending_settlement"], 11500.0)


def test_sleek_space_routes_digidokaan_tracking(monkeypatch):
    monkeypatch.setenv("INITIALIZE_APP", "false")
    import main

    assert main.is_digidokaan_tracking("22322367960798")
    assert main.courier_label_for_tracking("", "22322367960798") == "DigiDokaan"
    assert not main.is_digidokaan_tracking("LE123456")


def test_digidokaan_delivery_exceptions_need_attention(monkeypatch):
    monkeypatch.setenv("INITIALIZE_APP", "false")
    import main

    for status in (
        "Shipment - Shipper Advise Requested",
        "Shipment - Shipper Advice Requested",
        "Shipment - Reason Validation Required",
        "Shipment - Delivery Unsuccessful",
        "Shipment - Delivery Failed",
    ):
        assert main.is_need_attention_status(status)
        assert main.aggregate_order_status([
            {"status": status, "tracking_number": "22325168148793"}
        ]) == "Need Attention"

    assert main.aggregate_order_status([
        {"status": "Shipment - In Transit", "tracking_number": "22325168148793"}
    ]) == "Shipment - In Transit"


def test_payments_page_is_private_and_api_returns_digidokaan_ledger(monkeypatch):
    monkeypatch.setenv("INITIALIZE_APP", "false")
    import main

    client = main.app.test_client()
    response = client.get("/payments")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin_portal?next=/payments")

    record = {
        "order_id": "PK2924A01",
        "digidokaan_order_id": "10498383",
        "tracking_number": "22315868148789",
        "created_at": "2026-09-25 14:15:00",
        "shipment_status": "Shipment - Delivery Unsuccessful",
        "payment_mode": "COD",
        "payment_status": "Awaiting delivery",
        "raw_payment_status": "0",
        "order_amount": 11950.0,
        "delivery_charges": 307.0,
        "other_charges": 0.0,
        "reserve_amount": 0.0,
        "net_cod": 11643.0,
        "amount_paid": 0.0,
        "outstanding": 11643.0,
        "settlement_id": "",
    }
    monkeypatch.setattr(main, "load_digidokaan_payments_sync", lambda force=False: [record])
    with client.session_transaction() as session:
        session[main.ADMIN_PORTAL_SESSION_KEY] = True

    assert client.get("/payments").status_code == 200
    payload = client.get("/api/payments").get_json()
    assert payload["records"] == [record]
    assert payload["summary"]["awaiting_delivery"] == 11643.0
    assert payload["summary"]["pending_settlement"] == 0.0
