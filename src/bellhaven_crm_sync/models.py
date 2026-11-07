from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Community:
    url: str
    name: str
    address: str
    city: str
    state: str
    postal_code: str
    care_offerings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["care_offerings"] = list(self.care_offerings)
        return data


@dataclass(frozen=True)
class CRMAccount:
    account_id: str
    name: str
    parent_id: str
    address: str
    city: str
    state: str
    postal_code: str
    care_offerings: tuple[str, ...]
    lifetime_revenue: float
    outstanding_ar: float
    status: str
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "CRMAccount":
        def first(*keys: str, default: Any = "") -> Any:
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    return value
            return default

        care = first("care_type", "care_offerings", "services", default=[])
        if isinstance(care, str):
            care = [part.strip() for part in care.split(",") if part.strip()]
        elif not isinstance(care, list):
            care = []

        def amount(value: Any) -> float:
            try:
                return float(str(value or "0").replace(",", "").replace("$", ""))
            except ValueError:
                return 0.0

        return cls(
            account_id=str(first("id", "account_id", "crm_id")).strip(),
            name=str(first("name", "account_name", "company_name")).strip(),
            parent_id=str(first("parent_id")).strip(),
            address=str(first("billing_street", "street", "address", "street_address")).strip(),
            city=str(first("billing_city", "city")).strip(),
            state=str(first("billing_state", "state", "state_code")).strip(),
            postal_code=str(first("billing_zip", "zip", "postal_code", "billing_postal_code", "zip_code")).strip(),
            care_offerings=tuple(str(item).strip() for item in care if str(item).strip()),
            lifetime_revenue=amount(first("lifetime_revenue", default=0)),
            outstanding_ar=amount(first("outstanding_ar", default=0)),
            status=str(first("status", default="Active")).strip(),
            raw=dict(row),
        )


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    category: str
    title: str
    account_id: str | None
    community_url: str | None
    action: dict[str, Any]
    evidence: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
