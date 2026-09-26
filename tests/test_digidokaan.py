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


def test_sleek_space_routes_digidokaan_tracking(monkeypatch):
    monkeypatch.setenv("INITIALIZE_APP", "false")
    import main

    assert main.is_digidokaan_tracking("22322367960798")
    assert main.courier_label_for_tracking("", "22322367960798") == "DigiDokaan"
    assert not main.is_digidokaan_tracking("LE123456")
