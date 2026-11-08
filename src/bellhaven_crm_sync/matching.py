"""Evidence-based matching and safe proposal construction for Bellhaven CRM records."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from .models import CRMAccount, Community, Proposal


def normalize(value: str) -> str:
    """Normalize names and scalar location fields for punctuation-insensitive comparison."""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def normalize_address(value: str) -> str:
    """Normalize common postal abbreviations while preserving address token order."""
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
    """Build a composite location key; a street alone is not sufficient for deduplication."""
    return (
        normalize_address(account.address),
        normalize(account.city),
        normalize(account.state),
        normalize(account.postal_code),
    )


def _candidate_score(community: Community, account: CRMAccount) -> tuple[int, list[str]]:
    """Score identity/location evidence and return readable reasons for the score."""
    score = 0
    evidence: list[str] = []
    name_ratio = SequenceMatcher(
        None, normalize(community.name), normalize(account.name)
    ).ratio()
    if normalize(community.name) == normalize(account.name):
        # Exact normalized names get more weight than approximate names.
        score += 45
        evidence.append("Community and CRM names match after punctuation/case normalization.")
    elif name_ratio >= 0.86:
        # Similar names are useful evidence but need location corroboration.
        score += 30
        evidence.append(f"Community and CRM names are similar ({name_ratio:.0%}).")

    if normalize_address(community.address) and normalize_address(community.address) == normalize_address(account.address):
        # Street matches carry substantial weight, but are not enough alone to
        # auto-approve a match because addresses can be reused or incomplete.
        score += 40
        evidence.append("Street address matches.")
    if normalize(community.postal_code) and normalize(community.postal_code) == normalize(account.postal_code):
        # ZIP is a strong geographic discriminator when both sides provide it.
        score += 10
        evidence.append("Postal code matches.")
    if normalize(community.city) and normalize(community.city) == normalize(account.city):
        # City and state are supporting signals rather than identity by themselves.
        score += 8
        evidence.append("City matches.")
    if normalize(community.state) and normalize(community.state) == normalize(account.state):
        score += 5
        evidence.append("State matches.")
    return score, evidence


def _plausible_candidate(community: Community, account: CRMAccount) -> bool:
    """Reject same-name records whose geographic evidence contradicts the website."""
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
    # Missing CRM location fields are not treated as contradictions, but populated
    # fields must provide enough agreement to distinguish same-name facilities.
    location_matches = sum(normalize(left) == normalize(right) for left, right in populated_pairs)
    # A full street match is strong; otherwise require a compatible name plus location.
    return (
        street_matches
        or (
            # Nearly identical names may tolerate one missing/mismatched location field,
            # but a record with no geographic agreement remains untrusted.
            name_ratio >= 0.9
            and (location_matches > 0 or not populated_pairs)
            and location_matches >= len(populated_pairs) - 1
        )
        # Different but related names need multiple matching geographic fields.
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
    """Assign a stable ID from the proposed action so reruns can preserve decisions."""
    identity = {
        "category": category,
        "account_id": account_id,
        "community_url": community_url,
        "action": action,
    }
    proposal_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    # Evidence is deliberately excluded from identity: refreshed evidence for an
    # unchanged action should update one pending proposal rather than create another.
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
    """Require one unambiguous top-level Bellhaven account before proposing parent links."""
    candidates = [
        account
        for account in accounts
        if not account.parent_id
        and normalize(account.name).removesuffix("parentaccount") == "bellhavenseniorliving"
    ]
    if len(candidates) != 1:
        # A guessed parent could cause writes to the wrong CRM hierarchy.
        raise ValueError(
            "Could not uniquely identify the Bellhaven Senior Living parent account. "
            f"Found {len(candidates)} exact top-level matches; inspect the CRM data."
        )
    return candidates[0]


def build_proposals(
    communities: list[Community],
    raw_accounts: list[dict[str, Any]],
) -> tuple[list[Proposal], dict[str, Any]]:
    """Classify website communities and CRM child accounts without mutating CRM data.

    Matching remains advisory: uncertain candidates and every proposed write are
    represented as evidence-bearing proposals for a human to approve or reject.
    """
    accounts = [CRMAccount.from_mapping(row) for row in raw_accounts]
    if any(not account.account_id for account in accounts):
        # IDs are required for both audit evidence and any later approved write.
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
        # Prefer candidates with compatible location evidence; a global nearest-name
        # match can otherwise attach a facility to an unrelated account in another state.
        plausible_ranked = [
            item for item in all_ranked if _plausible_candidate(community, item[1])
        ]
        ranked = plausible_ranked or all_ranked
        (score, match_evidence), account = ranked[0] if ranked else ((0, []), None)
        second_score = ranked[1][0][0] if len(ranked) > 1 else -1
        # Automatically classify only a decisive full-address match or a separated score.
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
                # Plausible but non-decisive candidates go to a reviewer; no field
                # correction or parent change is inferred from a weak match.
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
                # Preserve the nearest candidate in evidence so reviewers can spot
                # legacy names that the conservative plausibility gate excluded.
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
                # Normalize common street abbreviations to avoid cosmetic updates.
                normalized_website = normalize_address(website_value)
                normalized_crm = normalize_address(crm_value)
            elif field == "status":
                # CRM status values are an enum with exact casing, not free-form text.
                normalized_website = str(website_value).strip()
                normalized_crm = str(crm_value).strip()
            else:
                normalized_website = normalize(website_value)
                normalized_crm = normalize(crm_value)
            if normalized_website != normalized_crm:
                # Keep the website's literal value as the proposed correction.
                changes[field] = website_value

        if account.parent_id != parent.account_id:
            if account.lifetime_revenue > 0 and account.outstanding_ar > 0:
                # Billing SOP: retain the old account and create a linked replacement.
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
                # The SOP permits direct reparenting if either billing condition is false.
                action = {
                    "kind": "update_account",
                    "account_id": account.account_id,
                    "expected_parent_id": account.parent_id,
                    "payload": {**changes, "parent_id": parent.account_id},
                }
                category = "wrong_parent"
        elif changes:
            # Parent is already correct; only propose fields that materially differ.
            action = {
                "kind": "update_account",
                "account_id": account.account_id,
                "expected_parent_id": account.parent_id,
                "payload": changes,
            }
            category = "field_correction"
        else:
            # A confident match with no material field drift needs no review action.
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
            # A single record at a location is not a duplicate candidate.
            continue
        # Only infer a duplicate when exactly one same-location account is a website match.
        matched = [account for account in group if account.account_id in matched_website_keys]
        if len(matched) != 1:
            # If zero or multiple records match the website, there is no safe,
            # unambiguous survivor to reference in duplicate_of_account.
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
            # Accounts already matched or proposed as duplicates are not separately
            # labeled stale, preventing duplicate/conflicting review actions.
            continue
        # A name may be reused at another location; do not call it stale by name alone.
        if normalize(account.name) in website_names:
            # Location mismatch still needs investigation; avoid asserting closure
            # merely because a same-named community exists elsewhere.
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
