import unittest
from unittest.mock import patch

import digidokaan
import main


class DigiDokaanLoadSheetTests(unittest.TestCase):
    def test_load_sheet_resolves_scanned_tracking_and_requests_one_pdf(self):
        class Response:
            def __init__(self, body=None, text="", status_code=200):
                self._body, self.text, self.status_code = body or {}, text, status_code

            def json(self):
                return self._body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(self.status_code)

        class ApiSession:
            def post(self, url, **kwargs):
                if url.endswith("/api/auth/login"):
                    return Response({"token": "api-token"})
                tracking = kwargs["json"]["search_value"]
                return Response({"data": [{"tracking_no": tracking, "order_id": "order-" + tracking[-2:]}]})

        class WebSession:
            submitted = None

            def get(self, _url, **_kwargs):
                return Response(text='<meta name="csrf-token" content="csrf-token">')

            def post(self, _url, **kwargs):
                self.submitted = kwargs["data"]
                return Response({"code": "200", "data": {"link": "https://web.digidokaan.pk/load-sheet.pdf"}})

        web_session = WebSession()
        config = {"base_url": "https://digidokaan.pk", "web_url": "https://web.digidokaan.pk", "phone": "923000000000", "password": "secret", "gateway_id": "5"}
        with patch.object(digidokaan, "configuration", return_value=config), patch.object(digidokaan.requests, "Session", return_value=ApiSession()), patch.object(digidokaan, "_web_login_session", return_value=web_session):
            result = digidokaan.generate_trax_load_sheet(["2230000000001", "2230000000002", "2230000000001"])
        self.assertEqual(result["shipment_count"], 2)
        self.assertEqual([value for key, value in web_session.submitted if key == "tracking_numbers[]"], ["2230000000001", "2230000000002"])

    def test_load_sheet_route_requires_employee_login(self):
        response = main.app.test_client().post("/api/scan/load-sheet", json={"tracking_numbers": ["2230000000001"]})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
