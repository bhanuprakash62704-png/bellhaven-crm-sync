"""Offline tests for CRM response normalization, pagination, and HTTP failures."""

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from bellhaven_crm_sync.api_client import (
    CRMClient,
    _records_from_response,
    load_local_env,
)


class APIClientTests(unittest.TestCase):
    """Use mocked HTTP responses so tests never call or modify the live sandbox."""

    def test_account_list_reads_all_pages(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        first_page = [{"id": str(index)} for index in range(50)]
        second_page = [{"id": str(index)} for index in range(50, 63)]

        with patch.object(
            client,
            "_request",
            side_effect=[{"accounts": first_page}, {"accounts": second_page}],
        ) as request:
            accounts = client.get_all_accounts()

        self.assertEqual(len(accounts), 63)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["query"]["page"], 1)
        self.assertEqual(request.call_args_list[1].kwargs["query"]["page"], 2)

    def test_account_list_handles_empty_and_exactly_one_full_page(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        full_page = [{"id": str(index)} for index in range(50)]

        with patch.object(
            client,
            "_request",
            side_effect=[{"accounts": []}, {"accounts": full_page}, {"accounts": []}],
        ) as request:
            empty_result = client.get_all_accounts()
            one_page_result = client.get_all_accounts()

        self.assertEqual(empty_result, [])
        self.assertEqual(len(one_page_result), 50)
        self.assertEqual(request.call_count, 3)

    def test_account_list_rejects_invalid_page_size(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")

        for page_size in (0, -1):
            with self.subTest(page_size=page_size):
                with self.assertRaisesRegex(ValueError, "at least 1"):
                    client.get_all_accounts(page_size)

    def test_account_list_fails_if_api_repeats_a_full_page(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        repeated_page = {"accounts": [{"id": str(index)} for index in range(50)]}

        with patch.object(client, "_request", side_effect=[repeated_page, repeated_page]):
            with self.assertRaisesRegex(RuntimeError, "repeated a full page"):
                client.get_all_accounts()

    def test_account_list_stops_at_maximum_page_bound(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        next_account_id = 0

        def unique_page(*args, **kwargs):
            nonlocal next_account_id
            page = [{"id": str(index)} for index in range(next_account_id, next_account_id + 50)]
            next_account_id += 50
            return {"accounts": page}

        with patch.object(client, "_request", side_effect=unique_page) as request:
            with self.assertRaisesRegex(RuntimeError, "exceeded 100 pages"):
                client.get_all_accounts()

        self.assertEqual(request.call_count, 100)

    def test_response_normalizer_accepts_nested_wrappers_and_id_maps(self):
        records = [{"id": "a1"}, {"id": "a2"}]

        self.assertEqual(_records_from_response({"data": {"accounts": records}}), records)
        self.assertEqual(_records_from_response({"a1": {"id": "a1"}}), [{"id": "a1"}])
        self.assertEqual(
            _records_from_response({"accounts": [None, {"id": "a1"}]}),
            [{"id": "a1"}],
        )

    def test_response_normalizer_rejects_scalar_and_unsupported_shapes(self):
        with self.assertRaisesRegex(ValueError, "JSON object or array"):
            _records_from_response("not-json-records")
        self.assertEqual(_records_from_response({"unexpected": "scalar"}), [])

    def test_identity_uses_documented_me_endpoint(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        with patch.object(client, "_request", return_value={"user": "assessment"}) as request:
            identity = client.get_identity()

        self.assertEqual(identity, {"user": "assessment"})
        request.assert_called_once_with("GET", "/me")

    def test_identity_requires_object_response(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")

        with patch.object(client, "_request", return_value=[]):
            with self.assertRaisesRegex(ValueError, "identity response"):
                client.get_identity()

    def test_client_requires_a_token(self):
        with patch.dict(os.environ, {"BELLHAVEN_API_TOKEN": ""}):
            with self.assertRaisesRegex(RuntimeError, "API token is missing"):
                CRMClient(base_url="https://example.test/api/v1")

    def test_local_env_loads_values_without_overriding_process_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "# ignored\nFROM_FILE='file-value'\nPRESET=file-value\nINVALID_LINE\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"PRESET": "process-value"}, clear=True):
                load_local_env(env_file)

                self.assertEqual(os.environ["FROM_FILE"], "file-value")
                self.assertEqual(os.environ["PRESET"], "process-value")
                self.assertNotIn("INVALID_LINE", os.environ)

    def test_local_env_ignores_missing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            load_local_env(Path(temp_dir) / "missing.env")

    def test_request_encodes_json_and_decodes_response(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true}'
        captured: dict[str, object] = {}

        def fake_urlopen(request, **kwargs):
            captured["request"] = request
            captured["kwargs"] = kwargs
            return response

        with patch(
            "bellhaven_crm_sync.api_client.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = client._request(
                "POST",
                "/accounts",
                query={"page": 2},
                body={"name": "Facility"},
            )

        request = captured["request"]
        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.full_url, "https://example.test/api/v1/accounts?page=2")
        self.assertEqual(json.loads(request.data), {"name": "Facility"})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertIn("context", captured["kwargs"])

    def test_request_returns_empty_mapping_for_empty_success(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b""

        with patch(
            "bellhaven_crm_sync.api_client.urllib.request.urlopen",
            return_value=response,
        ):
            self.assertEqual(client._request("PATCH", "/accounts/a1"), {})

    def test_request_exposes_bounded_http_error_detail(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        error = urllib.error.HTTPError(
            "https://example.test/api/v1/accounts",
            422,
            "Unprocessable Entity",
            {},
            None,
        )
        error.read = lambda: b"bad account data"

        with patch(
            "bellhaven_crm_sync.api_client.urllib.request.urlopen",
            side_effect=error,
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 422: bad account data"):
                client._request("POST", "/accounts", body={"name": "bad"})

    def test_request_reports_transport_and_invalid_json_failures(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        with patch(
            "bellhaven_crm_sync.api_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be reached"):
                client._request("GET", "/accounts")

        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"not-json"
        with patch(
            "bellhaven_crm_sync.api_client.urllib.request.urlopen",
            return_value=response,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                client._request("GET", "/accounts")

    def test_single_account_methods_unwrap_successful_responses(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"account": {"id": "a1"}},
                {"account": {"id": "a2"}},
                {"account": {"id": "a3"}},
            ],
        ):
            self.assertEqual(client.get_account("a1"), {"id": "a1"})
            self.assertEqual(client.create_account({"name": "A"}), {"id": "a2"})
            self.assertEqual(
                client.update_account("a3", {"city": "Town"}),
                {"id": "a3"},
            )

    def test_single_account_methods_reject_non_object_responses(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        methods = (
            (client.get_account, ("a1",)),
            (client.create_account, ({"name": "A"},)),
            (client.update_account, ("a1", {"city": "Town"})),
        )

        for method, args in methods:
            with self.subTest(method=method.__name__):
                with patch.object(client, "_request", return_value=[]):
                    with self.assertRaises(ValueError):
                        method(*args)


if __name__ == "__main__":
    unittest.main()
