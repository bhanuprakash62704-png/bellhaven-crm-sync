import tempfile
import unittest
from pathlib import Path

from bellhaven_crm_sync.actions import apply_action
from bellhaven_crm_sync.models import Proposal
from bellhaven_crm_sync.store import ReviewStore
from bellhaven_crm_sync.webapp import _page


class FakeCRMClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.account = {
            "id": "old",
            "name": "Facility",
            "parent_id": "former-parent",
            "lifetime_revenue": 100,
            "outstanding_ar": 20,
        }

    def get_account(self, account_id: str) -> dict[str, object]:
        self.calls.append(("get", account_id))
        return dict(self.account)

    def create_account(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("create", payload))
        return {"id": "new"}

    def update_account(self, account_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("update", (account_id, payload)))
        return {"id": account_id, **payload}


class ReviewTests(unittest.TestCase):
    def test_billing_chow_creates_new_account_then_links_old_account(self):
        client = FakeCRMClient()
        result = apply_action(
            client,
            {
                "kind": "billing_chow",
                "old_account_id": "old",
                "expected_parent_id": "former-parent",
                "create_payload": {"name": "Facility", "parent_id": "target"},
                "old_account_patch": {
                    "chow_current_account": "__CREATED_ACCOUNT_ID__",
                    "note": "Preserve billing history",
                },
            },
        )

        self.assertEqual(client.calls[1][0], "create")
        self.assertEqual(client.calls[2], (
            "update",
            ("old", {"chow_current_account": "new", "note": "Preserve billing history"}),
        ))
        self.assertEqual(result["created_account_id"], "new")
        self.assertTrue(result["chow_linked"])

    def test_billing_guard_stops_before_creating_if_ar_is_zero(self):
        client = FakeCRMClient()
        client.account["outstanding_ar"] = 0

        with self.assertRaisesRegex(RuntimeError, "billing condition changed"):
            apply_action(
                client,
                {
                    "kind": "billing_chow",
                    "old_account_id": "old",
                    "expected_parent_id": "former-parent",
                    "create_payload": {"name": "Facility", "parent_id": "target"},
                    "old_account_patch": {"chow_current_account": "__CREATED_ACCOUNT_ID__"},
                },
            )

        self.assertNotIn("create", [call[0] for call in client.calls])

    def test_direct_reparent_guard_rechecks_billing_before_update(self):
        client = FakeCRMClient()

        with self.assertRaisesRegex(RuntimeError, "billing-preserving proposal"):
            apply_action(
                client,
                {
                    "kind": "update_account",
                    "account_id": "old",
                    "expected_parent_id": "former-parent",
                    "payload": {"parent_id": "target"},
                },
            )

        self.assertNotIn("update", [call[0] for call in client.calls])

    def test_decisions_are_persistent_and_not_reinserted_on_refresh(self):
        proposal = Proposal(
            proposal_id="stable-id",
            category="missing_crm_account",
            title="Create a facility",
            account_id=None,
            community_url="https://example.test/location",
            action={"kind": "create_account", "payload": {"name": "Facility"}},
            evidence=[{"type": "website", "name": "Facility"}],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")

            self.assertEqual(store.add_proposals([proposal], {"proposal_count": 1}), 1)
            store.decide("stable-id", "rejected")
            self.assertEqual(store.add_proposals([proposal], {"proposal_count": 1}), 0)
            self.assertEqual(store.get_proposal("stable-id")["status"], "rejected")

    def test_refresh_updates_pending_evidence_and_supersedes_stale_items(self):
        first = Proposal(
            "pending",
            "field_correction",
            "Correct Facility",
            "a1",
            "https://example.test/location",
            {"kind": "update_account", "account_id": "a1", "payload": {"city": "Maplewood"}},
            [{"type": "website", "city": "Maplewood"}],
        )
        stale = Proposal("stale", "test", "Old proposal", None, None, {"kind": "x"}, [])
        refreshed = Proposal(
            "pending",
            "field_correction",
            "Correct Facility",
            "a1",
            "https://example.test/location",
            {"kind": "update_account", "account_id": "a1", "payload": {"city": "Maplewood"}},
            [{"type": "website", "city": "Maplewood", "postal_code": "44280"}],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")
            store.add_proposals([first, stale], {})
            store.add_proposals([refreshed], {})

            self.assertEqual(store.get_proposal("pending")["evidence"][0]["postal_code"], "44280")
            self.assertEqual(store.get_proposal("stale")["status"], "superseded")

    def test_proposal_cannot_be_decided_twice(self):
        proposal = Proposal("p", "test", "Test", None, None, {"kind": "x"}, [])
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")
            store.add_proposals([proposal], {})
            store.decide("p", "rejected")
            with self.assertRaisesRegex(ValueError, "already rejected"):
                store.decide("p", "approved")

    def test_review_page_shows_evidence_and_explicit_approval_actions(self):
        proposal = Proposal(
            "p",
            "field_correction",
            "Correct Facility",
            "a1",
            "https://example.test/location",
            {"kind": "update_account", "account_id": "a1", "payload": {"city": "Maplewood"}},
            [{"type": "website", "city": "Maplewood"}],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")
            store.add_proposals([proposal], {"community_count": 1})

            page = _page(store, "csrf-value")

        self.assertIn("Approve and apply", page)
        self.assertIn("Reject", page)
        self.assertIn("Maplewood", page)
        self.assertIn('name="csrf" value="csrf-value"', page)


if __name__ == "__main__":
    unittest.main()
