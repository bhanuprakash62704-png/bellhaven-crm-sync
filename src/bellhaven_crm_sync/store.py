from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Proposal


class ReviewStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    account_id TEXT,
                    community_url TEXT,
                    action_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision_at TEXT,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def add_proposals(self, proposals: list[Proposal], summary: dict[str, Any]) -> int:
        inserted = 0
        with self._connect() as connection:
            for proposal in proposals:
                existing = connection.execute(
                    "SELECT status FROM proposals WHERE proposal_id = ?",
                    (proposal.proposal_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO proposals
                    (proposal_id, category, title, account_id, community_url, action_json, evidence_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(proposal_id) DO UPDATE SET
                        category = excluded.category,
                        title = excluded.title,
                        account_id = excluded.account_id,
                        community_url = excluded.community_url,
                        action_json = excluded.action_json,
                        evidence_json = excluded.evidence_json,
                        status = 'pending',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE proposals.status IN ('pending', 'superseded')
                    """,
                    (
                        proposal.proposal_id,
                        proposal.category,
                        proposal.title,
                        proposal.account_id,
                        proposal.community_url,
                        json.dumps(proposal.action, sort_keys=True),
                        json.dumps(proposal.evidence, sort_keys=True),
                    ),
                )
                if existing is None or existing["status"] == "superseded":
                    inserted += 1
            current_ids = [proposal.proposal_id for proposal in proposals]
            if current_ids:
                placeholders = ",".join("?" for _ in current_ids)
                connection.execute(
                    f"""
                    UPDATE proposals
                    SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'pending' AND proposal_id NOT IN ({placeholders})
                    """,
                    current_ids,
                )
            else:
                connection.execute(
                    """
                    UPDATE proposals
                    SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'pending'
                    """
                )
            connection.execute(
                "INSERT INTO pipeline_runs (summary_json) VALUES (?)",
                (json.dumps(summary, sort_keys=True),),
            )
        return inserted

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return self._decode_row(row) if row else None

    def list_proposals(self, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM proposals WHERE status IN ({placeholders}) ORDER BY created_at, title",
                    statuses,
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM proposals ORDER BY created_at DESC, title"
                ).fetchall()
        return [self._decode_row(row) for row in rows]

    def decide(self, proposal_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError(f"Unsupported review decision: {decision}")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE proposals
                SET status = ?, decision_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                    error = NULL
                WHERE proposal_id = ? AND status = 'pending'
                """,
                (decision, proposal_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM proposals WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Proposal {proposal_id} does not exist.")
                raise ValueError(f"Proposal is already {row['status']}; it cannot be decided again.")
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise KeyError(f"Proposal {proposal_id} disappeared after its decision.")
        return proposal

    def record_result(self, proposal_id: str, result: dict[str, Any]) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE proposals
                SET status = 'applied', result_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE proposal_id = ? AND status = 'approved'
                """,
                (json.dumps(result, sort_keys=True), proposal_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Proposal {proposal_id} was not in the approved state.")

    def record_error(self, proposal_id: str, error: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE proposals
                SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE proposal_id = ? AND status = 'approved'
                """,
                (error[:2000], proposal_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Proposal {proposal_id} was not in the approved state.")

    def latest_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary_json, created_at FROM pipeline_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {"summary": json.loads(row["summary_json"]), "created_at": row["created_at"]}

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["action"] = json.loads(data.pop("action_json"))
        data["evidence"] = json.loads(data.pop("evidence_json"))
        result_json = data.pop("result_json")
        data["result"] = json.loads(result_json) if result_json else None
        return data
