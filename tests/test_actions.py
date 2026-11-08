"""Unit tests for each approved-action branch using a non-networking CRM fake."""

import unittest

from bellhaven_crm_sync.actions import _account_id, apply_action


class ActionCRM:
    def __init__(self) -> None:
        self.account = {
            "id": "old",
            "name": "Facility",
            "parent_id": "former-parent",
            "lifetime_revenue": 0,
            "outstanding_ar": 0,
        }
        self.calls: list[tuple[str, object]] = []
        self.create_response: dict[str, object] = {"id": "new"}
        self.update_error: Exception | None = None

    def get_account(self, account_id: str) -> dict[str, object]:
        self.calls.append(("get", account_id))
        return dict(self.account)

    def create_account(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("create", payload))
        return self.create_response

    def update_account(self, account_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("update", (account_id, payload)))
        if self.update_error:
            raise self.update_error
        return {"id": account_id, **payload}


class ActionTests(unittest.TestCase):
    def test_create_account_returns_id(self):
        client = ActionCRM()

        result = apply_action(
            client,
            {"kind": "create_account", "payload": {"name": "New Facility"}},
        )

        self.assertEqual(result, {"created_account_id": "new"})
        self.assertEqual(client.calls, [("create", {"name": "New Facility"})])

    def test_update_account_applies_payload_when_parent_is_unchanged(self):
        client = ActionCRM()

        result = apply_action(
            client,
            {
                "kind": "update_account",
                "account_id": "old",
                "expected_parent_id": "former-parent",
                "payload": {"city": "New Town"},
            },
        )

        self.assertEqual(result["updated_account_id"], "old")
        self.assertEqual(client.calls[-1], ("update", ("old", {"city": "New Town"})))

    def test_update_account_rejects_stale_parent_before_write(self):
        client = ActionCRM()
        client.account["parent_id"] = "different-parent"

        with self.assertRaisesRegex(RuntimeError, "parent changed"):
            apply_action(
                client,
                {
                    "kind": "update_account",
                    "account_id": "old",
                    "expected_parent_id": "former-parent",
                    "payload": {"city": "Town"},
                },
            )

        self.assertNotIn("update", [name for name, _ in client.calls])

    def test_direct_parent_update_is_allowed_if_either_billing_amount_is_zero(self):
        for revenue, outstanding_ar in ((100, 0), (0, 100), (0, 0)):
            with self.subTest(revenue=revenue, outstanding_ar=outstanding_ar):
                client = ActionCRM()
                client.account["lifetime_revenue"] = revenue
                client.account["outstanding_ar"] = outstanding_ar

                apply_action(
                    client,
                    {
                        "kind": "update_account",
                        "account_id": "old",
                        "expected_parent_id": "former-parent",
                        "payload": {"parent_id": "target-parent"},
                    },
                )

                self.assertIn("update", [name for name, _ in client.calls])

    def test_status_action_includes_optional_note(self):
        client = ActionCRM()

        apply_action(
            client,
            {
                "kind": "set_status",
                "account_id": "old",
                "expected_parent_id": "former-parent",
                "status": "Needs Review",
                "note": "Check current ownership",
            },
        )

        self.assertEqual(
            client.calls[-1],
            ("update", ("old", {"status": "Needs Review", "note": "Check current ownership"})),
        )

    def test_status_action_omits_empty_note(self):
        client = ActionCRM()

        apply_action(
            client,
            {
                "kind": "set_status",
                "account_id": "old",
                "status": "Inactive",
                "note": "",
            },
        )

        self.assertEqual(client.calls[-1], ("update", ("old", {"status": "Inactive"})))

    def test_status_action_rejects_stale_parent(self):
        client = ActionCRM()
        client.account["parent_id"] = "different-parent"

        with self.assertRaisesRegex(RuntimeError, "parent changed"):
            apply_action(
                client,
                {
                    "kind": "set_status",
                    "account_id": "old",
                    "expected_parent_id": "former-parent",
                    "status": "Needs Review",
                },
            )

        self.assertNotIn("update", [name for name, _ in client.calls])

    def test_billing_chow_stops_if_revenue_is_zero(self):
        client = ActionCRM()
        client.account["outstanding_ar"] = 100

        with self.assertRaisesRegex(RuntimeError, "billing condition changed"):
            apply_action(client, self._billing_action())

        self.assertNotIn("create", [name for name, _ in client.calls])

    def test_billing_chow_stops_if_parent_changed(self):
        client = ActionCRM()
        client.account.update(lifetime_revenue=100, outstanding_ar=50, parent_id="new-parent")

        with self.assertRaisesRegex(RuntimeError, "parent changed"):
            apply_action(client, self._billing_action())

        self.assertNotIn("create", [name for name, _ in client.calls])

    def test_billing_chow_stops_if_old_account_already_has_target_parent(self):
        client = ActionCRM()
        client.account.update(lifetime_revenue=100, outstanding_ar=50, parent_id="target-parent")
        action = self._billing_action()
        action["expected_parent_id"] = "target-parent"

        with self.assertRaisesRegex(RuntimeError, "already under the target parent"):
            apply_action(client, action)

        self.assertNotIn("create", [name for name, _ in client.calls])

    def test_billing_chow_reports_missing_new_account_id(self):
        client = ActionCRM()
        client.account.update(lifetime_revenue=100, outstanding_ar=50)
        client.create_response = {"name": "No ID"}

        with self.assertRaisesRegex(ValueError, "did not include the new account id"):
            apply_action(client, self._billing_action())

        self.assertNotIn("update", [name for name, _ in client.calls])

    def test_billing_chow_surfaces_partial_failure_with_both_account_ids(self):
        client = ActionCRM()
        client.account.update(lifetime_revenue=100, outstanding_ar=50)
        client.update_error = RuntimeError("API unavailable")

        with self.assertRaisesRegex(RuntimeError, "Created replacement account new.*old account old"):
            apply_action(client, self._billing_action())

        self.assertEqual([name for name, _ in client.calls], ["get", "create", "update"])

    def test_unknown_action_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unsupported approved action"):
            apply_action(ActionCRM(), {"kind": "delete_everything"})

    def test_account_id_supports_wrapped_and_alias_responses(self):
        self.assertEqual(_account_id({"account": {"account_id": 17}}), "17")
        self.assertEqual(_account_id({"crm_id": "legacy-id"}), "legacy-id")

    def test_account_id_rejects_missing_identifier(self):
        with self.assertRaisesRegex(ValueError, "did not include"):
            _account_id({"name": "Facility"})

    @staticmethod
    def _billing_action() -> dict[str, object]:
        return {
            "kind": "billing_chow",
            "old_account_id": "old",
            "expected_parent_id": "former-parent",
            "create_payload": {"name": "Facility", "parent_id": "target-parent"},
            "old_account_patch": {"chow_current_account": "__CREATED_ACCOUNT_ID__"},
        }


if __name__ == "__main__":
    unittest.main()
