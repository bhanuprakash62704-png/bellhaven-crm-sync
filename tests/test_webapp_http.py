"""HTTP-level review-route tests using a local server and a fake CRM client."""

import http.client
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from bellhaven_crm_sync.models import Proposal
from bellhaven_crm_sync.store import ReviewStore
from bellhaven_crm_sync.webapp import create_review_server


class HTTPFakeCRM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.account = {
            "id": "a1",
            "name": "Facility",
            "parent_id": "parent",
            "lifetime_revenue": 0,
            "outstanding_ar": 0,
        }

    def get_account(self, account_id: str) -> dict[str, object]:
        self.calls.append(("get", account_id))
        return dict(self.account)

    def update_account(self, account_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("update", (account_id, payload)))
        return {"id": account_id, **payload}


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "review.sqlite3"
        self.store = ReviewStore(self.database)
        self.client = HTTPFakeCRM()
        self.proposal = Proposal(
            "test-proposal",
            "field_correction",
            "Correct Facility",
            "a1",
            "https://example.test/location",
            {
                "kind": "update_account",
                "account_id": "a1",
                "expected_parent_id": "parent",
                "payload": {"city": "Town"},
            },
            [{"type": "website", "city": "Town"}],
        )
        self.store.add_proposals([self.proposal], {})
        self.server = create_review_server(
            "127.0.0.1",
            0,
            str(self.database),
            client=self.client,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp_dir.cleanup()

    def _get_page(self) -> tuple[http.client.HTTPResponse, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        return response, body

    def _post_decision(self, decision: str, csrf: str) -> http.client.HTTPResponse:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = urlencode({"decision": decision, "csrf": csrf})
        connection.request(
            "POST",
            "/review/test-proposal",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        return response

    def test_get_displays_proposal_and_does_not_call_crm(self):
        response, body = self._get_page()

        self.assertEqual(response.status, 200)
        self.assertIn("Correct Facility", body)
        self.assertIn("Approve and apply", body)
        self.assertEqual(self.client.calls, [])

    def test_reject_decision_persists_without_calling_crm(self):
        _, body = self._get_page()
        csrf = re.search(r'name="csrf" value="([^"]+)"', body).group(1)

        response = self._post_decision("rejected", csrf)

        self.assertEqual(response.status, 303)
        self.assertEqual(self.store.get_proposal("test-proposal")["status"], "rejected")
        self.assertEqual(self.client.calls, [])

    def test_approval_calls_crm_and_records_applied_result(self):
        _, body = self._get_page()
        csrf = re.search(r'name="csrf" value="([^"]+)"', body).group(1)

        response = self._post_decision("approved", csrf)

        proposal = self.store.get_proposal("test-proposal")
        self.assertEqual(response.status, 303)
        self.assertEqual(proposal["status"], "applied")
        self.assertEqual(proposal["result"]["updated_account_id"], "a1")
        self.assertEqual(
            self.client.calls,
            [
                ("get", "a1"),
                ("update", ("a1", {"city": "Town"})),
            ],
        )

    def test_invalid_csrf_does_not_decide_or_call_crm(self):
        response = self._post_decision("approved", "invalid-token")

        self.assertEqual(response.status, 303)
        self.assertEqual(self.store.get_proposal("test-proposal")["status"], "pending")
        self.assertEqual(self.client.calls, [])

    def test_invalid_decision_does_not_call_crm(self):
        _, body = self._get_page()
        csrf = re.search(r'name="csrf" value="([^"]+)"', body).group(1)

        response = self._post_decision("delete", csrf)

        self.assertEqual(response.status, 303)
        self.assertEqual(self.store.get_proposal("test-proposal")["status"], "pending")
        self.assertEqual(self.client.calls, [])

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "localhost only"):
            create_review_server(
                "0.0.0.0",
                0,
                str(self.database),
                client=self.client,
            )

    def test_unknown_route_returns_not_found_without_crm_access(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/admin")
        response = connection.getresponse()
        response.read()
        connection.close()

        self.assertEqual(response.status, 404)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
