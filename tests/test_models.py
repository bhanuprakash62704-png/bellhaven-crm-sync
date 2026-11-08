"""Tests for CRM field normalization and JSON-ready model serialization."""

import unittest

from bellhaven_crm_sync.models import CRMAccount, Community, Proposal


class ModelTests(unittest.TestCase):
    def test_account_mapping_prefers_canonical_fields_over_aliases(self):
        account = CRMAccount.from_mapping(
            {
                "id": "canonical",
                "account_id": "alias",
                "name": "Canonical Name",
                "account_name": "Alias Name",
                "billing_street": "1 Main St",
                "street": "2 Oak St",
                "billing_city": "Town",
                "billing_state": "OH",
                "billing_zip": "00123",
                "care_type": "Assisted Living, Memory Care",
                "lifetime_revenue": "$1,250.50",
                "outstanding_ar": "200",
                "status": "Inactive",
            }
        )

        self.assertEqual(account.account_id, "canonical")
        self.assertEqual(account.name, "Canonical Name")
        self.assertEqual(account.address, "1 Main St")
        self.assertEqual(account.postal_code, "00123")
        self.assertEqual(account.care_offerings, ("Assisted Living", "Memory Care"))
        self.assertEqual(account.lifetime_revenue, 1250.5)
        self.assertEqual(account.outstanding_ar, 200.0)
        self.assertEqual(account.status, "Inactive")

    def test_account_mapping_supports_alternate_keys_and_defaults(self):
        account = CRMAccount.from_mapping(
            {
                "crm_id": 25,
                "company_name": "Facility",
                "address": "4 Pine Rd",
                "city": "Village",
                "state_code": "PA",
                "zip_code": "17000",
                "services": ["Skilled Nursing", "Memory Care"],
                "lifetime_revenue": None,
                "outstanding_ar": "0",
                "status": "",
            }
        )

        self.assertEqual(account.account_id, "25")
        self.assertEqual(account.name, "Facility")
        self.assertEqual(account.care_offerings, ("Skilled Nursing", "Memory Care"))
        self.assertEqual(account.lifetime_revenue, 0)
        self.assertEqual(account.outstanding_ar, 0)
        self.assertEqual(account.status, "Active")
        self.assertEqual(account.raw["crm_id"], 25)

    def test_account_mapping_handles_unsupported_care_value(self):
        account = CRMAccount.from_mapping({"id": "1", "care_type": {"unsupported": "value"}})

        self.assertEqual(account.care_offerings, ())

    def test_account_mapping_discards_null_care_labels_and_preserves_zero_billing(self):
        account = CRMAccount.from_mapping(
            {
                "id": "1",
                "care_offerings": ["Assisted Living", None, ""],
                "lifetime_revenue": "0",
                "outstanding_ar": None,
            }
        )

        self.assertEqual(account.care_offerings, ("Assisted Living",))
        self.assertEqual(account.lifetime_revenue, 0)
        self.assertEqual(account.outstanding_ar, 0)

    def test_account_mapping_fails_closed_for_invalid_billing_amounts(self):
        for invalid_amount in ("not-a-number", "NaN", "Infinity", "-Infinity"):
            with self.subTest(invalid_amount=invalid_amount):
                with self.assertRaises(ValueError):
                    CRMAccount.from_mapping(
                        {"id": "1", "lifetime_revenue": invalid_amount}
                    )

    def test_community_serialization_converts_tuple_to_json_array(self):
        community = Community(
            url="https://example.test/location",
            name="Facility",
            address="1 Main St",
            city="Town",
            state="OH",
            postal_code="00123",
            care_offerings=("Assisted Living",),
        )

        serialized = community.to_dict()

        self.assertEqual(serialized["care_offerings"], ["Assisted Living"])
        self.assertEqual(serialized["postal_code"], "00123")

    def test_proposal_serialization_retains_nested_action_and_evidence(self):
        proposal = Proposal(
            "proposal-id",
            "field_correction",
            "Correct Facility",
            "account-id",
            "https://example.test/location",
            {"kind": "update_account", "payload": {"city": "Town"}},
            [{"type": "website", "city": "Town"}],
        )

        serialized = proposal.to_dict()

        self.assertEqual(serialized["action"]["payload"]["city"], "Town")
        self.assertEqual(serialized["evidence"][0]["type"], "website")


if __name__ == "__main__":
    unittest.main()
