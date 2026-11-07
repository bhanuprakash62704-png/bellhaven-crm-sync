import unittest

from bellhaven_crm_sync.matching import build_proposals, normalize_address
from bellhaven_crm_sync.models import Community


PARENT = {
    "id": "parent",
    "name": "Bellhaven Senior Living",
    "parent_id": "",
    "status": "Active",
}


def community(
    name: str = "Bellhaven of Maplewood",
    address: str = "210 Orchard Lane",
    city: str = "Maplewood",
    state: str = "OH",
    postal_code: str = "44280",
) -> Community:
    return Community(
        url="https://example.test/communities/maplewood",
        name=name,
        address=address,
        city=city,
        state=state,
        postal_code=postal_code,
        care_offerings=("Assisted Living",),
    )


def account(account_id: str = "a1", **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": account_id,
        "name": "Bellhaven of Maplewood",
        "street": "210 Orchard Lane",
        "city": "Maplewood",
        "state": "OH",
        "zip": "44280",
        "care_offerings": ["Assisted Living"],
        "parent_id": "parent",
        "status": "Active",
        "lifetime_revenue": 0,
        "outstanding_ar": 0,
    }
    result.update(overrides)
    return result


class MatchingTests(unittest.TestCase):
    def test_billing_rule_creates_chow_proposal_instead_of_reparenting(self):
        old_account = account(
            parent_id="former-parent",
            lifetime_revenue=125000,
            outstanding_ar=450,
        )
        proposals, summary = build_proposals([community()], [PARENT, old_account])

        proposal = next(item for item in proposals if item.category == "billing_chow")
        self.assertEqual(proposal.action["kind"], "billing_chow")
        self.assertEqual(proposal.action["old_account_id"], "a1")
        self.assertEqual(proposal.action["create_payload"]["parent_id"], "parent")
        self.assertNotIn("parent_id", proposal.action["old_account_patch"])
        self.assertEqual(summary["parent_account_id"], "parent")

    def test_account_without_outstanding_ar_is_reparented_directly(self):
        proposals, _ = build_proposals(
            [community()],
            [PARENT, account(parent_id="former-parent", lifetime_revenue=125000, outstanding_ar=0)],
        )

        proposal = next(item for item in proposals if item.category == "wrong_parent")
        self.assertEqual(proposal.action["kind"], "update_account")
        self.assertEqual(proposal.action["payload"]["parent_id"], "parent")

    def test_unmatched_community_and_bellhaven_account_are_proposed(self):
        stale = account("stale", name="Closed Community", street="99 Old Road")
        proposals, _ = build_proposals([community()], [PARENT, account(), stale])

        self.assertIn("not_on_website", {item.category for item in proposals})
        self.assertNotIn("missing_crm_account", {item.category for item in proposals})

        missing, _ = build_proposals(
            [community(), community(name="New Community", address="6 New Road", city="New Town")],
            [PARENT, account()],
        )
        self.assertIn("missing_crm_account", {item.category for item in missing})

    def test_duplicate_requires_evidence_and_proposes_inactivation_link(self):
        duplicate = account("a2", name="Old Maplewood Home")
        proposals, _ = build_proposals([community()], [PARENT, account(), duplicate])

        proposal = next(item for item in proposals if item.category == "duplicate_account")
        self.assertEqual(
            proposal.action["payload"],
            {
                "duplicate_of_account": "a1",
                "status": "Inactive",
                "note": proposal.action["payload"]["note"],
            },
        )
        self.assertIn("postal_code", proposal.evidence[1]["matching_fields"])

    def test_parent_account_must_be_unambiguous(self):
        with self.assertRaisesRegex(ValueError, "uniquely identify"):
            build_proposals([], [PARENT, {**PARENT, "id": "parent-2"}])

    def test_inactive_matched_account_proposes_reactivation(self):
        proposals, _ = build_proposals(
            [community()],
            [PARENT, account(status="Inactive")],
        )

        proposal = next(item for item in proposals if item.category == "field_correction")
        self.assertEqual(proposal.action["payload"]["status"], "Active")

    def test_address_normalization_matches_directions_and_street_suffixes(self):
        self.assertEqual(
            normalize_address("1250 NW Franklin Street"),
            normalize_address("1250 Northwest Franklin St"),
        )

        website_community = community(
            name="Bellhaven of Chesterton",
            address="1250 NW Franklin Street",
            city="Chesterton",
            state="IN",
            postal_code="46304",
        )
        legacy_account = account(
            "legacy",
            name="Chesterton Senior Commons",
            street="1250 Northwest Franklin St",
            city="Chesterton",
            state="IN",
            zip="46304",
        )
        proposals, _ = build_proposals([website_community], [PARENT, legacy_account])

        correction = next(item for item in proposals if item.category == "field_correction")
        self.assertEqual(correction.action["payload"], {"name": "Bellhaven of Chesterton"})
        self.assertNotIn("missing_crm_account", {item.category for item in proposals})

    def test_same_name_in_a_different_state_is_not_treated_as_a_match(self):
        unrelated = account(
            "unrelated",
            name="Amberly Manor",
            street="918 S Nevada Ave",
            city="Colorado Springs",
            state="CO",
            zip="80903",
        )
        website_community = community(
            name="Amberly Manor",
            address="4390 Darrow Rd",
            city="Hudson",
            state="OH",
            postal_code="44236",
        )
        proposals, _ = build_proposals([website_community], [PARENT, unrelated])

        self.assertIn("missing_crm_account", {item.category for item in proposals})
        self.assertNotIn("ambiguous_match", {item.category for item in proposals})


if __name__ == "__main__":
    unittest.main()
