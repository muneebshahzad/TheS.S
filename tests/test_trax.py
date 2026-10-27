from unittest import IsolatedAsyncioTestCase

import trax
from trax import _csrf_token, _portal_history, fetch_public_tracking_history


class FakeResponse:
    def __init__(self, body, status=200, json_body=None):
        self.body = body
        self.status = status
        self.json_body = json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        return self.body

    async def json(self, content_type=None):
        return self.json_body


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.post_data = None

    def get(self, *_args, **_kwargs):
        return FakeResponse("<script>data: {'_token': 'public-token'}</script>")

    def post(self, *_args, **kwargs):
        self.post_data = kwargs.get("data")
        return FakeResponse("", json_body=self.payload)


class TraxPublicTrackingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        trax._history_cache.clear()
        trax._inflight.clear()

    def test_token_and_history_are_normalized(self):
        self.assertEqual(_csrf_token("'_token': 'abc123'"), "abc123")
        history = _portal_history({"shipments": {"1": {
            "tracking_number": "22322660078220",
            "consignee": {"name": "Customer", "destination": "Lahore"},
            "tracking_history": [
                {"date_time": "2026-10-28 12:00:00", "status": "Shipment - In Transit"},
                {"date_time": "2026-10-27 10:00:00", "status": "Shipment - Booked"},
            ],
        }}}, "22322660078220")
        self.assertEqual(history[-1]["ProcessDescForPortal"], "Shipment - In Transit")
        self.assertEqual(history[-1]["TrackingSource"], "Trax Direct")

    async def test_public_tracker_posts_cn_and_returns_history(self):
        payload = {"shipments": {"1": {
            "tracking_number": "22322660078220",
            "tracking_history": [{"date_time": "2026-10-28", "status": "Shipment - Rider Picked"}],
        }}}
        session = FakeSession(payload)
        history = await fetch_public_tracking_history(session, "223 22660078220")
        self.assertEqual(session.post_data["tracking_numbers"], "22322660078220")
        self.assertEqual(history[-1]["ProcessDescForPortal"], "Shipment - Rider Picked")
