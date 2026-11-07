import unittest
from unittest.mock import patch

from bellhaven_crm_sync.api_client import CRMClient


class APIClientTests(unittest.TestCase):
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

    def test_account_list_fails_if_api_repeats_a_full_page(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        repeated_page = {"accounts": [{"id": str(index)} for index in range(50)]}

        with patch.object(client, "_request", side_effect=[repeated_page, repeated_page]):
            with self.assertRaisesRegex(RuntimeError, "repeated a full page"):
                client.get_all_accounts()

    def test_identity_uses_documented_me_endpoint(self):
        client = CRMClient(base_url="https://example.test/api/v1", token="test-token")
        with patch.object(client, "_request", return_value={"user": "assessment"}) as request:
            identity = client.get_identity()

        self.assertEqual(identity, {"user": "assessment"})
        request.assert_called_once_with("GET", "/me")


if __name__ == "__main__":
    unittest.main()
