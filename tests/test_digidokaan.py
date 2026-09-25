import asyncio
import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import digidokaan


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


def test_sleek_space_routes_digidokaan_tracking(monkeypatch):
    monkeypatch.setenv("INITIALIZE_APP", "false")
    import main

    assert main.is_digidokaan_tracking("22322367960798")
    assert main.courier_label_for_tracking("", "22322367960798") == "DigiDokaan"
    assert not main.is_digidokaan_tracking("LE123456")
