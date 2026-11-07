from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from .models import CRMAccount, Community, Proposal


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def normalize_address(value: str) -> str:
    aliases = {
        "north": "n",
        "south": "s",
        "east": "e",
        "west": "w",
        "northeast": "ne",
        "northwest": "nw",
        "southeast": "se",
        "southwest": "sw",
        "street": "st",
        "avenue": "ave",
        "road": "rd",
        "drive": "dr",
        "lane": "ln",
        "boulevard": "blvd",
        "court": "ct",
        "circle": "cir",
        "parkway": "pkwy",
        "highway": "hwy",
        "terrace": "ter",
        "place": "pl",
        "trail": "trl",
    }
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return " ".join(aliases.get(word, word) for word in words)


def _address_key(account: CRMAccount | Community) -> tuple[str, str, str, str]:
    return (
        normalize_address(account.address),
        normalize(account.city),
        normalize(account.state),
        normalize(account.postal_code),
    )


def _candidate_score(community: Community, account: CRMAccount) -> tuple[int, list[str]]:
    score = 0
    evidence: list[str] = []
    name_ratio = SequenceMatcher(
        None, normalize(community.name), normalize(account.name)
    ).ratio()
    if normalize(community.name) == normalize(account.name):
        score += 45
        evidence.append("Community and CRM names match after punctuation/case normalization.")
    elif name_ratio >= 0.86:
        score += 30
        evidence.append(f"Community and CRM names are similar ({name_ratio:.0%}).")

    if normalize_address(community.address) and normalize_address(community.address) == normalize_address(account.address):
        score += 40
        evidence.append("Street address matches.")
    if normalize(community.postal_code) and normalize(community.postal_code) == normalize(account.postal_code):
        score += 10
        evidence.append("Postal code matches.")
    if normalize(community.city) and normalize(community.city) == normalize(account.city):
        score += 8
        evidence.append("City matches.")
    if normalize(community.state) and normalize(community.state) == normalize(account.state):
        score += 5
        evidence.append("State matches.")
    return score, evidence


def _plausible_candidate(community: Community, account: CRMAccount) -> bool:
    name_ratio = SequenceMatcher(
        None, normalize(community.name), normalize(account.name)
    ).ratio()
    street_matches = bool(
        normalize_address(community.address)
        and normalize_address(community.address) == normalize_address(account.address)
    )
    location_pairs = (
        (community.city, account.city),
        (community.state, account.state),
        (community.postal_code, account.postal_code),
    )
    populated_pairs = [
        (left, right)
        for left, right in location_pairs
        if normalize(left) and normalize(right)
    ]
    location_matches = sum(normalize(left) == normalize(right) for left, right in populated_pairs)
    return (
        street_matches
        or (
            name_ratio >= 0.9
            and (location_matches > 0 or not populated_pairs)
            and location_matches >= len(populated_pairs) - 1
        )
        or (name_ratio >= 0.6 and location_matches >= 2)
    )


def _proposal(
    category: str,
    title: str,
    account_id: str | None,
    community_url: str | None,
    action: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> Proposal:
    identity = {
        "category": category,
        "account_id": account_id,
        "community_url": community_url,
        "action": action,
    }
    proposal_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return Proposal(
        proposal_id=proposal_id,
        category=category,
        title=title,
        account_id=account_id,
        community_url=community_url,
        action=action,
        evidence=evidence,
    )


def find_parent_account(accounts: list[CRMAccount]) -> CRMAccount:
    candidates = [
        account
        for account in accounts
        if not account.parent_id
        and normalize(account.name).removesuffix("parentaccount") == "bellhavenseniorliving"
    ]
    if len(candidates) != 1:
        raise ValueError(
            "Could not uniquely identify the Bellhaven Senior Living parent account. "
            f"Found {len(candidates)} exact top-level matches; inspect the CRM data."
        )
    return candidates[0]


def build_proposals(
    communities: list[Community],
    raw_accounts: list[dict[str, Any]],
) -> tuple[list[Proposal], dict[str, Any]]:
    accounts = [CRMAccount.from_mapping(row) for row in raw_accounts]
    if any(not account.account_id for account in accounts):
        raise ValueError("At least one CRM account response is missing its account id.")
    parent = find_parent_account(accounts)
    facilities = [account for account in accounts if account.account_id != parent.account_id]
    proposals: list[Proposal] = []
    matched_website_keys: set[str] = set()
    matched_communities: dict[str, Community] = {}
    classifications: list[dict[str, Any]] = []

    for community in communities:
        all_ranked = sorted(
            ((_candidate_score(community, account), account) for account in facilities),
            key=lambda item: (-item[0][0], item[1].account_id),
        )
        plausible_ranked = [
            item for item in all_ranked if _plausible_candidate(community, item[1])
        ]
        ranked = plausible_ranked or all_ranked
        (score, match_evidence), account = ranked[0] if ranked else ((0, []), None)
        second_score = ranked[1][0][0] if len(ranked) > 1 else -1
        clear_candidate = bool(
            account
            and (
                (
                    bool(normalize_address(community.address))
                    and normalize_address(community.address) == normalize_address(account.address)
                    and normalize(community.postal_code) == normalize(account.postal_code)
                    and normalize(community.state) == normalize(account.state)
                )
                and _plausible_candidate(community, account)
                or score >= 58 and score - second_score >= 8
                and _plausible_candidate(community, account)
            )
        )
        website_evidence = {
            "source": community.url,
            "name": community.name,
            "address": community.address,
            "city": community.city,
            "state": community.state,
            "postal_code": community.postal_code,
            "care_offerings": list(community.care_offerings),
        }

        if not clear_candidate:
            if account and _plausible_candidate(community, account):
                proposals.append(
                    _proposal(
                        "ambiguous_match",
                        f"Resolve possible match for {community.name}",
                        account.account_id,
                        community.url,
                        {
                            "kind": "set_status",
                            "account_id": account.account_id,
                            "expected_parent_id": account.parent_id,
                            "status": "Needs Review",
                            "note": f"Ambiguous website/CRM match for {community.name}; review before changing ownership.",
                        },
                        [
                            {"type": "website", **website_evidence},
                            {
                                "type": "crm_candidate",
                                "account_id": account.account_id,
                                "name": account.name,
                                "score": score,
                                "address": account.address,
                                "city": account.city,
                                "state": account.state,
                                "postal_code": account.postal_code,
                                "parent_id": account.parent_id,
                                "alternatives": [
                                    {
                                        "account_id": candidate.account_id,
                                        "name": candidate.name,
                                        "score": candidate_score,
                                        "address": candidate.address,
                                        "city": candidate.city,
                                        "state": candidate.state,
                                        "postal_code": candidate.postal_code,
                                        "parent_id": candidate.parent_id,
                                    }
                                    for (candidate_score, _), candidate in ranked[1:3]
                                ],
                            },
                            {"type": "matching", "details": match_evidence},
                        ],
                    )
                )
                classifications.append(
                    {"community": community.name, "classification": "ambiguous", "account_id": account.account_id, "score": score}
                )
            else:
                payload = {
                    "name": community.name,
                    "billing_street": community.address,
                    "billing_city": community.city,
                    "billing_state": community.state,
                    "billing_zip": community.postal_code,
                    "care_type": ", ".join(community.care_offerings),
                    "parent_id": parent.account_id,
                    "status": "Active",
                }
                proposals.append(
                    _proposal(
                        "missing_crm_account",
                        f"Create CRM account for {community.name}",
                        None,
                        community.url,
                        {"kind": "create_account", "payload": payload},
                        [
                            {"type": "website", **website_evidence},
                            {
                                "type": "closest_crm_candidate",
                                "account_id": all_ranked[0][1].account_id if all_ranked else None,
                                "name": all_ranked[0][1].name if all_ranked else None,
                                "score": score,
                                "address": all_ranked[0][1].address if all_ranked else None,
                                "city": all_ranked[0][1].city if all_ranked else None,
                                "state": all_ranked[0][1].state if all_ranked else None,
                                "postal_code": all_ranked[0][1].postal_code if all_ranked else None,
                                "parent_id": all_ranked[0][1].parent_id if all_ranked else None,
                            },
                            {"type": "matching", "score": score, "details": match_evidence},
                        ],
                    )
                )
                classifications.append(
                    {"community": community.name, "classification": "missing_crm_account", "account_id": None, "score": score}
                )
            continue

        assert account is not None
        matched_website_keys.add(account.account_id)
        matched_communities[account.account_id] = community
        changes: dict[str, Any] = {}
        field_pairs = (
            ("name", community.name, account.name),
            ("billing_street", community.address, account.address),
            ("billing_city", community.city, account.city),
            ("billing_state", community.state, account.state),
            ("billing_zip", community.postal_code, account.postal_code),
            ("status", "Active", account.status),
        )
        for field, website_value, crm_value in field_pairs:
            if field == "billing_street":
                normalized_website = normalize_address(website_value)
                normalized_crm = normalize_address(crm_value)
            else:
                normalized_website = normalize(website_value)
                normalized_crm = normalize(crm_value)
            if normalized_website != normalized_crm:
                changes[field] = website_value

        if account.parent_id != parent.account_id:
            if account.lifetime_revenue > 0 and account.outstanding_ar > 0:
                action = {
                    "kind": "billing_chow",
                    "create_payload": {
                        "name": community.name,
                        "billing_street": community.address,
                        "billing_city": community.city,
                        "billing_state": community.state,
                        "billing_zip": community.postal_code,
                        "care_type": ", ".join(community.care_offerings),
                        "parent_id": parent.account_id,
                        "status": "Active",
                    },
                    "old_account_id": account.account_id,
                    "expected_parent_id": account.parent_id,
                    "old_account_patch": {
                        "chow_current_account": "__CREATED_ACCOUNT_ID__",
                        "note": f"Preserved due to lifetime revenue ${account.lifetime_revenue:,.2f} and outstanding AR ${account.outstanding_ar:,.2f}; new Bellhaven account created after approval.",
                    },
                }
                category = "billing_chow"
            else:
                action = {
                    "kind": "update_account",
                    "account_id": account.account_id,
                    "expected_parent_id": account.parent_id,
                    "payload": {**changes, "parent_id": parent.account_id},
                }
                category = "wrong_parent"
        elif changes:
            action = {
                "kind": "update_account",
                "account_id": account.account_id,
                "expected_parent_id": account.parent_id,
                "payload": changes,
            }
            category = "field_correction"
        else:
            classifications.append(
                {"community": community.name, "classification": "confident_match", "account_id": account.account_id, "score": score}
            )
            continue

        crm_values = {
            "name": account.name,
            "billing_street": account.address,
            "billing_city": account.city,
            "billing_state": account.state,
            "billing_zip": account.postal_code,
            "status": account.status,
        }
        details = [
            {"field": field, "crm": crm_values[field], "website": value}
            for field, value in changes.items()
        ]
        if account.parent_id != parent.account_id:
            details.append({"field": "parent_id", "crm": account.parent_id, "website": parent.account_id})
        proposals.append(
            _proposal(
                category,
                f"{category.replace('_', ' ').title()}: {community.name}",
                account.account_id,
                community.url,
                action,
                [
                    {"type": "website", **website_evidence},
                    {
                        "type": "crm_account",
                        "account_id": account.account_id,
                        "name": account.name,
                        "care_type": list(account.care_offerings),
                        "parent_id": account.parent_id,
                        "lifetime_revenue": account.lifetime_revenue,
                        "outstanding_ar": account.outstanding_ar,
                    },
                    {"type": "matching", "score": score, "details": match_evidence},
                    {"type": "proposed_changes", "details": details},
                ],
            )
        )
        classifications.append(
            {"community": community.name, "classification": category, "account_id": account.account_id, "score": score}
        )

    duplicate_groups: dict[tuple[str, str, str, str], list[CRMAccount]] = defaultdict(list)
    for account in facilities:
        if account.parent_id != parent.account_id:
            continue
        key = _address_key(account)
        if all(key):
            duplicate_groups[key].append(account)
    duplicate_ids: set[str] = set()
    for group in duplicate_groups.values():
        if len(group) < 2:
            continue
        matched = [account for account in group if account.account_id in matched_website_keys]
        if len(matched) != 1:
            continue
        survivor = matched[0]
        community = matched_communities[survivor.account_id]
        for duplicate in group:
            if duplicate.account_id == survivor.account_id:
                continue
            duplicate_ids.add(duplicate.account_id)
            proposals.append(
                _proposal(
                    "duplicate_account",
                    f"Review possible duplicate: {duplicate.name}",
                    duplicate.account_id,
                    community.url,
                    {
                        "kind": "update_account",
                        "account_id": duplicate.account_id,
                        "expected_parent_id": duplicate.parent_id,
                        "payload": {
                            "duplicate_of_account": survivor.account_id,
                            "status": "Inactive",
                            "note": f"Marked as a duplicate of account {survivor.account_id}; both accounts share the same street, city, state, and postal code as {community.name}.",
                        },
                    },
                    [
                        {
                            "type": "website",
                            "source": community.url,
                            "name": community.name,
                            "address": community.address,
                            "city": community.city,
                            "state": community.state,
                            "postal_code": community.postal_code,
                        },
                        {
                            "type": "duplicate_evidence",
                            "survivor_account_id": survivor.account_id,
                            "survivor_name": survivor.name,
                            "survivor_address": survivor.address,
                            "duplicate_account_id": duplicate.account_id,
                            "duplicate_name": duplicate.name,
                            "duplicate_address": duplicate.address,
                            "matching_fields": ["street", "city", "state", "postal_code"],
                        },
                    ],
                )
            )
            classifications.append(
                {"community": community.name, "classification": "possible_duplicate", "account_id": duplicate.account_id}
            )

    website_names = {normalize(community.name) for community in communities}
    for account in facilities:
        if (
            account.parent_id != parent.account_id
            or account.account_id in matched_website_keys
            or account.account_id in duplicate_ids
        ):
            continue
        if normalize(account.name) in website_names:
            continue
        proposals.append(
            _proposal(
                "not_on_website",
                f"Review CRM account not listed on website: {account.name}",
                account.account_id,
                None,
                {
                    "kind": "set_status",
                    "account_id": account.account_id,
                    "expected_parent_id": account.parent_id,
                    "status": "Needs Review",
                    "note": "This account is currently under Bellhaven but no current website community matched it. Verify ownership before changing its status or parent.",
                },
                [
                    {
                        "type": "crm_account",
                        "account_id": account.account_id,
                        "name": account.name,
                        "address": account.address,
                        "city": account.city,
                        "state": account.state,
                        "postal_code": account.postal_code,
                        "parent_id": account.parent_id,
                        "lifetime_revenue": account.lifetime_revenue,
                        "outstanding_ar": account.outstanding_ar,
                    },
                    {"type": "website_check", "details": "No website community was confidently matched to this Bellhaven child account."},
                ],
            )
        )

    summary = {
        "community_count": len(communities),
        "crm_account_count": len(accounts),
        "parent_account_id": parent.account_id,
        "proposal_count": len(proposals),
        "classifications": classifications,
    }
    return proposals, summary
