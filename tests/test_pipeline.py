"""Tests that refresh reads snapshots and persists proposals without writes."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bellhaven_crm_sync.models import Community, Proposal
from bellhaven_crm_sync.pipeline import refresh_review_queue
from bellhaven_crm_sync.store import ReviewStore


class ReadOnlyCRMFixture:
    def __init__(self) -> None:
        self.read_calls = 0

    def get_identity(self) -> dict[str, str]:
        self.read_calls += 1
        return {"user": "fixture"}

    def get_all_accounts(self) -> list[dict[str, str]]:
        self.read_calls += 1
        return [{"id": "parent", "name": "Bellhaven Senior Living", "parent_id": ""}]


class FailingCRMFixture(ReadOnlyCRMFixture):
    def get_identity(self) -> dict[str, str]:
        raise RuntimeError("authentication failed")


class PipelineTests(unittest.TestCase):
    """Use a read-only CRM fixture to exercise rerun behavior safely."""

    def test_refresh_is_read_only_and_does_not_duplicate_pending_items(self):
        # Repeating the same snapshot should preserve one pending item and issue reads only.
        crm = ReadOnlyCRMFixture()
        community = Community(
            url="https://example.test/communities/new",
            name="New Community",
            address="15 Main Street",
            city="Maplewood",
            state="OH",
            postal_code="44280",
            care_offerings=("Assisted Living",),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "review.sqlite3"

            first = refresh_review_queue(database, client=crm, communities=[community])
            second = refresh_review_queue(database, client=crm, communities=[community])
            pending = ReviewStore(database).list_proposals(("pending",))

        # The first refresh inserts once; the second reports the proposal as already known.
        self.assertEqual(first["new_proposals_added"], 1)
        self.assertEqual(second["new_proposals_added"], 0)
        self.assertEqual(second["already_seen_or_decided"], 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(crm.read_calls, 4)

    def test_refresh_fails_auth_before_fetching_accounts_or_replacing_queue(self):
        crm = FailingCRMFixture()
        community = Community(
            "https://example.test/location",
            "New Community",
            "15 Main Street",
            "Maplewood",
            "OH",
            "44280",
            ("Assisted Living",),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "review.sqlite3"
            store = ReviewStore(database)
            store.add_proposals(
                [
                    Proposal(
                        "existing",
                        "test",
                        "Keep this",
                        None,
                        None,
                        {"kind": "test"},
                        [],
                    )
                ],
                {},
            )
            previous_run = store.latest_run()

            with patch("bellhaven_crm_sync.pipeline.load_local_env"):
                with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                    refresh_review_queue(database, client=crm, communities=[community])

            self.assertEqual(store.get_proposal("existing")["status"], "pending")
            self.assertEqual(store.latest_run(), previous_run)

    def test_refresh_fails_if_parent_is_ambiguous_without_saving_snapshot(self):
        class AmbiguousParentCRM(ReadOnlyCRMFixture):
            def get_all_accounts(self) -> list[dict[str, str]]:
                return [
                    {"id": "parent-1", "name": "Bellhaven Senior Living", "parent_id": ""},
                    {"id": "parent-2", "name": "Bellhaven Senior Living", "parent_id": ""},
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "review.sqlite3"
            with patch("bellhaven_crm_sync.pipeline.load_local_env"):
                with self.assertRaisesRegex(ValueError, "uniquely identify"):
                    refresh_review_queue(
                        database,
                        client=AmbiguousParentCRM(),
                        communities=[],
                    )

            self.assertFalse((Path(temp_dir) / "latest_communities.json").exists())


if __name__ == "__main__":
    unittest.main()
