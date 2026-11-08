"""Fixture-based tests for matching confidence and assessment-specific CRM rules."""

import unittest

from bellhaven_crm_sync.matching import _proposal, build_proposals, normalize_address
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
    """Keep live account data out of matcher tests and assert proposed actions only."""

    def test_billing_rule_creates_chow_proposal_instead_of_reparenting(self):
        # Both billing conditions are positive, so do not edit the old account's parent.
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
        # Revenue alone does not trigger CHOW; the SOP requires revenue AND outstanding AR.
        proposals, _ = build_proposals(
            [community()],
            [PARENT, account(parent_id="former-parent", lifetime_revenue=125000, outstanding_ar=0)],
        )

        proposal = next(item for item in proposals if item.category == "wrong_parent")
        self.assertEqual(proposal.action["kind"], "update_account")
        self.assertEqual(proposal.action["payload"]["parent_id"], "parent")

    def test_unmatched_community_and_bellhaven_account_are_proposed(self):
        # Exercise both directions: CRM-only and website-only locations.
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
        # Duplicate status and survivor link are proposed, not written automatically.
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
        # An ambiguous parent could attach new facilities to the wrong company.
        with self.assertRaisesRegex(ValueError, "uniquely identify"):
            build_proposals([], [PARENT, {**PARENT, "id": "parent-2"}])

    def test_parent_account_accepts_sandbox_parent_label(self):
        parent = {**PARENT, "name": "Bellhaven Senior Living (Parent Account)"}

        proposals, summary = build_proposals([], [parent])

        self.assertEqual(proposals, [])
        self.assertEqual(summary["parent_account_id"], "parent")

    def test_missing_account_id_fails_before_matching(self):
        with self.assertRaisesRegex(ValueError, "missing its account id"):
            build_proposals([community()], [PARENT, account(id="")])

    def test_empty_crm_facility_list_proposes_creation_for_each_community(self):
        locations = [
            community(),
            community(name="Second Facility", address="2 Oak St", city="Other Town"),
        ]

        proposals, summary = build_proposals(locations, [PARENT])

        self.assertEqual([item.category for item in proposals], ["missing_crm_account"] * 2)
        self.assertEqual(summary["community_count"], 2)
        self.assertEqual(summary["crm_account_count"], 1)

    def test_same_name_without_location_is_ambiguous_not_auto_matched(self):
        record = account(
            street="",
            city="",
            state="",
            zip="",
        )

        proposals, _ = build_proposals([community()], [PARENT, record])

        proposal = next(item for item in proposals if item.category == "ambiguous_match")
        self.assertEqual(proposal.action["status"], "Needs Review")
        self.assertEqual(proposal.account_id, "a1")

    def test_two_equally_plausible_candidates_go_to_review_with_alternative(self):
        first = account("a1", street="210 Different Street")
        second = account("a2", street="210 Another Street")

        proposals, _ = build_proposals([community()], [PARENT, first, second])

        proposal = next(item for item in proposals if item.category == "ambiguous_match")
        candidate_evidence = next(item for item in proposal.evidence if item["type"] == "crm_candidate")
        self.assertEqual(len(candidate_evidence["alternatives"]), 1)
        self.assertEqual(candidate_evidence["alternatives"][0]["account_id"], "a2")

    def test_exact_address_match_remains_match_when_names_need_rebranding(self):
        legacy = account("old", name="Maplewood Care Center")

        proposals, summary = build_proposals([community()], [PARENT, legacy])

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].category, "field_correction")
        self.assertEqual(proposals[0].action["payload"]["name"], "Bellhaven of Maplewood")
        self.assertEqual(summary["classifications"][0]["account_id"], "old")

    def test_cosmetic_street_abbreviation_does_not_create_field_correction(self):
        existing = account(street="210 Orchard Ln")

        proposals, summary = build_proposals([community()], [PARENT, existing])

        self.assertEqual(proposals, [])
        self.assertEqual(summary["classifications"][0]["classification"], "confident_match")

    def test_not_on_website_proposals_only_include_children_of_bellhaven(self):
        other_parent_child = account(
            "other-child",
            name="Unrelated Facility",
            street="9 Other Rd",
            parent_id="different-parent",
        )
        bellhaven_child = account(
            "bellhaven-child",
            name="Old Bellhaven Facility",
            street="8 Old Rd",
        )

        proposals, _ = build_proposals([], [PARENT, other_parent_child, bellhaven_child])

        self.assertEqual([item.account_id for item in proposals], ["bellhaven-child"])
        self.assertEqual(proposals[0].category, "not_on_website")

    def test_proposal_id_is_stable_when_only_evidence_changes(self):
        first = _proposal(
            "field_correction",
            "Correct Facility",
            "a1",
            "https://example.test/location",
            {"kind": "update_account", "account_id": "a1", "payload": {"city": "Town"}},
            [{"type": "crm", "updated_at": "yesterday"}],
        )
        refreshed = _proposal(
            "field_correction",
            "Correct Facility",
            "a1",
            "https://example.test/location",
            {"kind": "update_account", "account_id": "a1", "payload": {"city": "Town"}},
            [{"type": "crm", "updated_at": "today"}],
        )

        self.assertEqual(first.proposal_id, refreshed.proposal_id)

    def test_inactive_matched_account_proposes_reactivation(self):
        # A website-listed community is active; a confident inactive CRM match is drift.
        proposals, _ = build_proposals(
            [community()],
            [PARENT, account(status="Inactive")],
        )

        proposal = next(item for item in proposals if item.category == "field_correction")
        self.assertEqual(proposal.action["payload"]["status"], "Active")

    def test_noncanonical_status_casing_proposes_valid_status_value(self):
        proposals, _ = build_proposals(
            [community()],
            [PARENT, account(status="active")],
        )

        proposal = next(item for item in proposals if item.category == "field_correction")
        self.assertEqual(proposal.action["payload"]["status"], "Active")

    def test_address_normalization_matches_directions_and_street_suffixes(self):
        # Postal abbreviations should not create a false missing-account proposal.
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
        # Name equality alone must not link a facility to a geographically unrelated account.
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
