from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .api_client import CRMClient, load_local_env
from .matching import build_proposals
from .models import Community
from .scraper import scrape_communities
from .store import ReviewStore


def refresh_review_queue(
    database_path: str | Path = "data/review.sqlite3",
    *,
    client: CRMClient | None = None,
    communities: list[Community] | None = None,
) -> dict[str, Any]:
    load_local_env()
    crm_client = client or CRMClient()
    website_communities = communities if communities is not None else scrape_communities()
    crm_client.get_identity()
    crm_accounts = crm_client.get_all_accounts()
    proposals, summary = build_proposals(website_communities, crm_accounts)
    inserted_count = ReviewStore(database_path).add_proposals(proposals, summary)
    result = {
        **summary,
        "new_proposals_added": inserted_count,
        "already_seen_or_decided": len(proposals) - inserted_count,
    }

    snapshot_path = Path(database_path).parent / "latest_communities.json"
    snapshot_path.write_text(
        json.dumps([community.to_dict() for community in website_communities], indent=2),
        encoding="utf-8",
    )
    return result
