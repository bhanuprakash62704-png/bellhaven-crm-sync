"""Immutable records shared by the scraper, matcher, review queue, and API layer."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Community:
    """Website-sourced location fields used as the comparison reference.

    The frozen dataclass prevents later pipeline steps from accidentally changing
    the scraped evidence after a proposal has been built.
    """

    url: str  # Source page reviewers can open to verify the scraped fields.
    name: str  # Public-facing community name from the detail-page heading.
    address: str  # Street line; city/state/postal code are stored separately.
    city: str
    state: str
    postal_code: str  # Kept as text to preserve leading zeroes and ZIP+4 suffixes.
    care_offerings: tuple[str, ...]  # Immutable collection of labels scraped from badges.

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible community data for the scraper snapshot.

        ``dataclasses.asdict`` preserves the tuple type, while JSON consumers
        expect an array; convert only that field to a list for serialization.
        """
        data = asdict(self)
        data["care_offerings"] = list(self.care_offerings)
        return data


@dataclass(frozen=True)
class CRMAccount:
    """Normalized CRM fields plus the untouched source payload.

    The normalized fields give matching and billing checks consistent names;
    ``raw`` retains the original API record so reviewers can inspect its source.
    """

    account_id: str  # Stable CRM identifier used by account read/update endpoints.
    name: str
    parent_id: str  # Empty for top-level accounts; otherwise identifies the owner.
    address: str
    city: str
    state: str
    postal_code: str
    care_offerings: tuple[str, ...]
    lifetime_revenue: float  # Numeric form is required by the billing-move SOP.
    outstanding_ar: float  # Numeric form is required by the billing-move SOP.
    status: str
    raw: dict[str, Any]  # Original API payload, including fields not modeled above.

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "CRMAccount":
        """Convert one API record into the stable internal account representation.

        When the API has synonymous field names, the first non-empty alias below
        wins. The original mapping is copied into ``raw`` so normalization never
        discards evidence needed by the review UI.
        """
        def first(*keys: str, default: Any = "") -> Any:
            """Return the first populated alias, or the field-specific default.

            Empty strings and ``None`` mean "not supplied", so they do not mask
            a later alias that may contain the actual CRM value.
            """
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    return value
            return default

        # The sandbox currently sends a single care_type string; accept older or
        # alternate response shapes as well, but keep the internal value uniform.
        care = first("care_type", "care_offerings", "services", default=[])
        if isinstance(care, str):
            # Split comma-separated labels and discard whitespace-only entries.
            care = [part.strip() for part in care.split(",") if part.strip()]
        elif not isinstance(care, list):
            # Ignore unsupported payload types rather than iterating their contents.
            care = []

        def amount(value: Any) -> float:
            """Convert numeric or currency-formatted API values to a float.

            Commas and dollar signs are presentation characters, not part of the
            amount. Missing values mean zero; malformed or non-finite amounts fail
            closed because they cannot be evaluated safely against the billing SOP.
            """
            try:
                # ``value or "0"`` also treats missing values and numeric zero uniformly.
                parsed = float(str(value or "0").replace(",", "").replace("$", ""))
            except (TypeError, ValueError) as error:
                # Never turn malformed billing history into a zero eligible for re-parenting.
                raise ValueError(f"Invalid CRM billing amount: {value!r}") from error
            if not math.isfinite(parsed):
                raise ValueError(f"Non-finite CRM billing amount: {value!r}")
            return parsed

        return cls(
            # Prefer the API's canonical key, then accept common legacy aliases.
            account_id=str(first("id", "account_id", "crm_id")).strip(),
            name=str(first("name", "account_name", "company_name")).strip(),
            parent_id=str(first("parent_id")).strip(),
            # Billing-prefixed fields are canonical in this sandbox; the other
            # aliases keep the mapper usable with exported or older CRM records.
            address=str(first("billing_street", "street", "address", "street_address")).strip(),
            city=str(first("billing_city", "city")).strip(),
            state=str(first("billing_state", "state", "state_code")).strip(),
            postal_code=str(first("billing_zip", "zip", "postal_code", "billing_postal_code", "zip_code")).strip(),
            care_offerings=tuple(
                str(item).strip()
                for item in care
                if item is not None and str(item).strip()
            ),
            # Missing billing amounts mean zero; amount() also handles formatted strings.
            lifetime_revenue=amount(first("lifetime_revenue", default=0)),
            outstanding_ar=amount(first("outstanding_ar", default=0)),
            status=str(first("status", default="Active")).strip(),
            # Copy rather than retain the caller's dict, keeping this record's evidence isolated.
            raw=dict(row),
        )


@dataclass(frozen=True)
class Proposal:
    """An evidence-backed action that must be reviewed before any CRM write.

    ``proposal_id`` is derived from the proposed action so unchanged refreshes
    refer to the same decision instead of duplicating it in the review queue.
    """

    proposal_id: str  # Stable local key for persistence and decision tracking.
    category: str  # Human-readable classification, e.g. field_correction or billing_chow.
    title: str  # Short summary shown in the review queue.
    account_id: str | None  # None when the proposal creates a new CRM account.
    community_url: str | None  # None for CRM-only records with no website detail page.
    action: dict[str, Any]  # Typed by ``kind``; executed only after reviewer approval.
    evidence: list[dict[str, Any]]  # Website/CRM facts explaining why the action was proposed.

    def to_dict(self) -> dict[str, Any]:
        """Convert the proposal to a plain mapping for display or JSON output."""
        return asdict(self)
