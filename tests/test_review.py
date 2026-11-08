"""Tests for approval safeguards, persisted decisions, and the rendered review UI."""

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
    """Exercise actions with a fake CRM so no test mutates the assessment sandbox."""

    def test_billing_chow_creates_new_account_then_links_old_account(self):
        # Verify required order: read old account, create replacement, then set the CHOW link.
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
        # If the SOP condition changed after review, no replacement account is created.
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
        # A formerly safe proposal becomes unsafe if both billing values are now positive.
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
        # A rejected decision is final and survives an identical later refresh.
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
        # Refresh current evidence while retaining history for resolved discrepancies.
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
        # The state transition prevents double decisions from stale browser tabs.
        proposal = Proposal("p", "test", "Test", None, None, {"kind": "x"}, [])
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")
            store.add_proposals([proposal], {})
            store.decide("p", "rejected")
            with self.assertRaisesRegex(ValueError, "already rejected"):
                store.decide("p", "approved")

    def test_store_rejects_unknown_decisions_and_missing_proposals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")

            with self.assertRaisesRegex(ValueError, "Unsupported review decision"):
                store.decide("missing", "deferred")
            with self.assertRaisesRegex(KeyError, "does not exist"):
                store.decide("missing", "approved")

    def test_only_approved_proposals_can_record_success_or_failure(self):
        proposal = Proposal("p", "test", "Test", None, None, {"kind": "x"}, [])
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")
            store.add_proposals([proposal], {})

            with self.assertRaisesRegex(ValueError, "not in the approved state"):
                store.record_result("p", {"ok": True})
            with self.assertRaisesRegex(ValueError, "not in the approved state"):
                store.record_error("p", "failed")

            store.decide("p", "approved")
            store.record_result("p", {"account_id": "created"})

            self.assertEqual(store.get_proposal("p")["status"], "applied")
            self.assertEqual(store.get_proposal("p")["result"], {"account_id": "created"})

    def test_failed_approved_action_is_recorded_and_not_reopened(self):
        proposal = Proposal("p", "test", "Test", None, None, {"kind": "x"}, [])
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")
            store.add_proposals([proposal], {})
            store.decide("p", "approved")
            store.record_error("p", "API failure")

            # A matching refresh must not re-open an action that may have partially applied.
            store.add_proposals([proposal], {})

            failed = store.get_proposal("p")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"], "API failure")

    def test_superseded_proposal_can_become_pending_if_finding_returns(self):
        proposal = Proposal("p", "test", "Test", None, None, {"kind": "x"}, [])
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")
            store.add_proposals([proposal], {})
            store.add_proposals([], {})
            self.assertEqual(store.get_proposal("p")["status"], "superseded")

            self.assertEqual(store.add_proposals([proposal], {}), 1)
            self.assertEqual(store.get_proposal("p")["status"], "pending")

    def test_latest_run_is_empty_before_first_run_and_tracks_latest_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")

            self.assertIsNone(store.latest_run())
            store.add_proposals([], {"run": 1})
            store.add_proposals([], {"run": 2})

            self.assertEqual(store.latest_run()["summary"], {"run": 2})

    def test_review_page_shows_evidence_and_explicit_approval_actions(self):
        # The reviewer must see evidence and deliberately choose one of the two decisions.
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

    def test_review_page_escapes_untrusted_evidence_and_shows_empty_queue(self):
        proposal = Proposal(
            "p",
            "field_correction",
            "<script>alert(1)</script>",
            "a1",
            None,
            {"kind": "update_account", "account_id": "a1", "payload": {"name": "<b>"}},
            [{"type": "crm", "name": "<img src=x onerror=alert(1)>"}],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "review.sqlite3")
            store.add_proposals([proposal], {})

            page = _page(store, "csrf")
            store.add_proposals([], {})
            empty_page = _page(store, "csrf")

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("review queue is empty", empty_page)


if __name__ == "__main__":
    unittest.main()
