"""SQLite persistence for review decisions and repeatable pipeline runs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Proposal


class ReviewStore:
    """Keep proposals and reviewer decisions durable across refreshes and restarts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # SQLite creates the database file, but not missing parent directories.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            # CREATE IF NOT EXISTS makes startup safe for both first run and restart.
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
        """Create an independent connection so each operation has a clear transaction."""
        connection = sqlite3.connect(self.path, timeout=10)
        # Named row access keeps database decoding independent of column positions.
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def add_proposals(self, proposals: list[Proposal], summary: dict[str, Any]) -> int:
        """Upsert current pending evidence while preserving finalized decisions.

        Proposals absent from a successful refresh are superseded, not deleted, so
        their decision history remains inspectable if a discrepancy later returns.
        """
        inserted = 0
        with self._connect() as connection:
            for proposal in proposals:
                existing = connection.execute(
                    "SELECT status FROM proposals WHERE proposal_id = ?",
                    (proposal.proposal_id,),
                ).fetchone()
                # Insert new proposals, refresh evidence on pending/superseded ones,
                # and preserve decisions on rejected/approved/applied/failed items.
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
                    -- Never overwrite a reviewer decision or an already attempted write.
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
                # Placeholders are generated from the number of IDs; values remain
                # bound parameters and are never interpolated into SQL.
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
                # A successful run with zero findings supersedes every open item.
                # Failed refreshes never reach this method, so they cannot clear the queue.
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
        """Return one proposal with its JSON fields decoded."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        # A missing row is represented explicitly as None for the caller to handle.
        return self._decode_row(row) if row else None

    def list_proposals(self, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """List proposals, optionally restricted to a set of workflow states."""
        with self._connect() as connection:
            if statuses:
                # Bind each requested status separately to avoid interpolating values.
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM proposals WHERE status IN ({placeholders}) ORDER BY created_at, title",
                    statuses,
                ).fetchall()
            else:
                # Without a filter, return all states for command-line/audit views.
                rows = connection.execute(
                    "SELECT * FROM proposals ORDER BY created_at DESC, title"
                ).fetchall()
        return [self._decode_row(row) for row in rows]

    def decide(self, proposal_id: str, decision: str) -> dict[str, Any]:
        """Atomically transition a pending proposal to one final reviewer decision."""
        if decision not in {"approved", "rejected"}:
            # Keep the database state machine restricted to reviewer-supported choices.
            raise ValueError(f"Unsupported review decision: {decision}")
        with self._connect() as connection:
            # The status predicate prevents two browser submissions from both winning.
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
                    # Distinguish an invalid proposal ID from a stale/repeated form submit.
                    raise KeyError(f"Proposal {proposal_id} does not exist.")
                # The atomic UPDATE matched only pending proposals, so this row has
                # already been decided, attempted, or superseded.
                raise ValueError(f"Proposal is already {row['status']}; it cannot be decided again.")
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            # Defensive consistency check: the transaction above should leave this row present.
            raise KeyError(f"Proposal {proposal_id} disappeared after its decision.")
        return proposal

    def record_result(self, proposal_id: str, result: dict[str, Any]) -> None:
        """Mark an approved action as applied after the API confirms success."""
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
                # Only approved proposals may transition to applied.
                raise ValueError(f"Proposal {proposal_id} was not in the approved state.")

    def record_error(self, proposal_id: str, error: str) -> None:
        """Retain API failure details without making an unsafe automatic retry possible."""
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
                # A failure must be attached to a previously approved operation.
                raise ValueError(f"Proposal {proposal_id} was not in the approved state.")

    def latest_run(self) -> dict[str, Any] | None:
        """Return the most recent refresh summary for the review page."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary_json, created_at FROM pipeline_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            # The UI can render before the first pipeline refresh.
            return None
        return {"summary": json.loads(row["summary_json"]), "created_at": row["created_at"]}

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        """Decode stored JSON columns into the shape consumed by the UI and action runner."""
        data = dict(row)
        data["action"] = json.loads(data.pop("action_json"))
        data["evidence"] = json.loads(data.pop("evidence_json"))
        result_json = data.pop("result_json")
        # A result is absent before a write succeeds; otherwise restore its JSON type.
        data["result"] = json.loads(result_json) if result_json else None
        return data
