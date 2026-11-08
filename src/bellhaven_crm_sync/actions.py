"""Apply reviewer-approved proposals while rechecking safety-critical CRM state."""

from __future__ import annotations

from typing import Any

from .api_client import CRMClient
from .models import CRMAccount


def _account_id(response: dict[str, Any]) -> str:
    """Extract the created record ID across response shapes accepted by the API."""
    account_id = response.get("id") or response.get("account_id") or response.get("crm_id")
    if not account_id and isinstance(response.get("account"), dict):
        # The create endpoint may wrap its result even when other API methods do not.
        account_id = response["account"].get("id") or response["account"].get("account_id")
    if not account_id:
        # Without the ID, a later CHOW link cannot safely point to the new account.
        raise ValueError("CRM create response did not include the new account id.")
    return str(account_id)


def apply_action(client: CRMClient, action: dict[str, Any]) -> dict[str, Any]:
    """Execute one approved proposal, refusing stale or SOP-violating changes."""
    kind = action.get("kind")
    if kind == "create_account":
        # The reviewer approved this exact payload; return the ID for audit history.
        created = client.create_account(action["payload"])
        return {"created_account_id": _account_id(created)}

    if kind == "update_account":
        account_id = str(action["account_id"])
        current = CRMAccount.from_mapping(client.get_account(account_id))
        expected_parent = action.get("expected_parent_id")
        # A parent change can become unsafe after proposal generation if billing data changes.
        if expected_parent is not None and current.parent_id != expected_parent:
            # The proposal is stale if another actor changed its parent meanwhile.
            raise RuntimeError(
                f"Account {account_id} parent changed since review; refresh and review again."
            )
        target_parent = action["payload"].get("parent_id")
        if (
            target_parent
            and target_parent != current.parent_id
            and current.lifetime_revenue > 0
            and current.outstanding_ar > 0
        ):
            # Billing facts may have changed since proposal generation; refuse the
            # direct reparent and require a freshly generated billing_chow proposal.
            raise RuntimeError(
                "The account now has both lifetime revenue and outstanding AR. "
                "No parent change was made; refresh and review the billing-preserving proposal."
            )
        updated = client.update_account(account_id, action["payload"])
        return {"updated_account_id": account_id, "response": updated}

    if kind == "set_status":
        account_id = str(action["account_id"])
        current = CRMAccount.from_mapping(client.get_account(account_id))
        expected_parent = action.get("expected_parent_id")
        if expected_parent is not None and current.parent_id != expected_parent:
            # Do not change status based on evidence collected under an old owner.
            raise RuntimeError(
                f"Account {account_id} parent changed since review; refresh and review again."
            )
        payload = {"status": action["status"]}
        if action.get("note"):
            payload["note"] = action["note"]
        updated = client.update_account(account_id, payload)
        return {"updated_account_id": account_id, "response": updated}

    if kind == "billing_chow":
        old_account_id = str(action["old_account_id"])
        old_account = CRMAccount.from_mapping(client.get_account(old_account_id))
        # Re-read both billing fields at approval time; the proposal's earlier snapshot is not enough.
        if old_account.lifetime_revenue <= 0 or old_account.outstanding_ar <= 0:
            raise RuntimeError(
                "The billing condition changed since review. No account was created; refresh and review again."
            )
        if old_account.parent_id != action.get("expected_parent_id"):
            raise RuntimeError(
                "The old account's parent changed since review. No account was created; refresh and review again."
            )
        if old_account.parent_id == action["create_payload"]["parent_id"]:
            # Avoid creating a redundant replacement if another process already moved it.
            raise RuntimeError(
                "The account is already under the target parent. Refresh and review again."
            )
        created = client.create_account(action["create_payload"])
        new_account_id = _account_id(created)
        # The API has no transaction: if linking fails, retain both IDs for manual reconciliation.
        patch = {
            key: new_account_id if value == "__CREATED_ACCOUNT_ID__" else value
            for key, value in action["old_account_patch"].items()
        }
        try:
            client.update_account(old_account_id, patch)
        except Exception as error:
            # Account creation already succeeded, so rollback is unavailable; surface
            # both IDs and require an operator to complete/reconcile the link manually.
            raise RuntimeError(
                f"Created replacement account {new_account_id}, but could not link the preserved old account {old_account_id}: {error}. Reconcile manually; do not approve this proposal again."
            ) from error
        return {
            "created_account_id": new_account_id,
            "preserved_account_id": old_account_id,
            "chow_linked": True,
        }

    # Unknown action kinds must fail closed instead of guessing at a CRM operation.
    raise ValueError(f"Unsupported approved action kind: {kind!r}")
