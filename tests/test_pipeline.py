import tempfile
import unittest
from pathlib import Path

from bellhaven_crm_sync.models import Community
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


class PipelineTests(unittest.TestCase):
    def test_refresh_is_read_only_and_does_not_duplicate_pending_items(self):
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

        self.assertEqual(first["new_proposals_added"], 1)
        self.assertEqual(second["new_proposals_added"], 0)
        self.assertEqual(second["already_seen_or_decided"], 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(crm.read_calls, 4)


if __name__ == "__main__":
    unittest.main()
