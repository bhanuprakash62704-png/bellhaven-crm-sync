"""Authenticated, read/write access to the assessment CRM API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .http_utils import trusted_ssl_context


def load_local_env(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overriding the caller's environment.

    Missing files are allowed because deployments may inject environment variables
    directly. Existing process variables take precedence over values in the file.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # Ignore blank/comment lines and malformed entries rather than treating them as credentials.
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _records_from_response(response: Any) -> list[dict[str, Any]]:
    """Normalize common paginated/list response wrappers to account records.

    The sandbox may wrap records under different keys; recursively unwrap known
    containers, but reject scalar responses so callers cannot mistake errors for
    an empty account list.
    """
    if isinstance(response, list):
        # Filter non-object entries because account mapping expects named fields.
        return [item for item in response if isinstance(item, dict)]
    if not isinstance(response, dict):
        raise ValueError("CRM account-list response must be a JSON object or array.")
    for key in ("accounts", "results", "items", "data"):
        if key in response:
            # Prefer recognized collection keys in a stable order.
            return _records_from_response(response[key])
    if response and all(isinstance(value, dict) for value in response.values()):
        # Accept ID-keyed maps, where each value is one account record.
        return list(response.values())
    # An empty object represents an empty collection; an unrecognized non-empty
    # object is not safely interpretable as a list of accounts.
    return []


class CRMClient:
    """Small API client that keeps authentication and HTTP errors in one place."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: int = 20,
    ) -> None:
        """Set endpoint/authentication options and reject missing credentials early."""
        self.base_url = (base_url or os.getenv(
            "BELLHAVEN_API_BASE",
            "https://analyst-assessment-production.up.railway.app/api/v1",
        )).rstrip("/")
        self.token = token or os.getenv("BELLHAVEN_API_TOKEN", "")
        self.timeout = timeout
        if not self.token:
            # Fail before any network request, rather than issuing an anonymous call.
            raise RuntimeError(
                "CRM API token is missing. Set BELLHAVEN_API_TOKEN in your local .env file."
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Send one JSON API request and translate transport failures into context.

        ``body`` is omitted for reads; when present it is JSON encoded and labeled
        accordingly. The exception branches distinguish server responses,
        connectivity problems, and malformed successful responses.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "BellhavenCRMReview/1.0 (assessment project)",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=trusted_ssl_context(),
            ) as response:
                raw = response.read()
                if not raw:
                    # Some successful write endpoints may return an empty body.
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Preserve a short server-provided explanation without dumping an
            # arbitrarily large response into logs or terminal output.
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"CRM API {method} {path} failed with HTTP {error.code}: {detail[:500]}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            # These failures occur before a usable HTTP response is received.
            raise RuntimeError(f"CRM API {method} {path} could not be reached: {error}") from error
        except json.JSONDecodeError as error:
            # A 2xx response with invalid JSON is still an API failure to this client.
            raise RuntimeError(f"CRM API {method} {path} returned invalid JSON.") from error

    def get_all_accounts(self, page_size: int = 50) -> list[dict[str, Any]]:
        """Fetch every page, rejecting pagination loops instead of returning partial data."""
        if page_size < 1:
            # A zero/negative limit could make pagination incomplete or non-terminating.
            raise ValueError("CRM page size must be at least 1.")
        accounts: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        page = 1
        while True:
            response = self._request(
                "GET",
                "/accounts",
                query={"page": page, "page_size": page_size},
            )
            current = _records_from_response(response)
            current_ids = {
                str(item.get("id") or item.get("account_id") or item.get("crm_id"))
                for item in current
                if item.get("id") or item.get("account_id") or item.get("crm_id")
            }
            # Some APIs ignore page parameters; a repeated full page would otherwise loop forever.
            if len(current) >= page_size and current_ids and current_ids <= seen_ids:
                raise RuntimeError(
                    "CRM account pagination repeated a full page; refusing to continue with incomplete data."
                )
            seen_ids.update(current_ids)
            accounts.extend(current)
            if len(current) < page_size:
                # A short page is the API's signal that all records have been read.
                return accounts
            if page >= 100:
                # Bound unexpected/malformed pagination rather than looping forever.
                raise RuntimeError("CRM account pagination exceeded 100 pages.")
            page += 1

    def get_identity(self) -> dict[str, Any]:
        """Validate the configured token via the API's identity endpoint."""
        response = self._request("GET", "/me")
        if not isinstance(response, dict):
            raise ValueError("CRM identity response was not a JSON object.")
        return response

    def get_account(self, account_id: str) -> dict[str, Any]:
        """Fetch one account by ID for a final pre-write state check."""
        response = self._request("GET", f"/accounts/{urllib.parse.quote(account_id, safe='')}")
        if not isinstance(response, dict):
            raise ValueError(f"CRM account {account_id} response was not a JSON object.")
        if isinstance(response.get("account"), dict):
            # Accept APIs that wrap a single record under an "account" property.
            return response["account"]
        return response

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create an account only when called by an explicitly approved action."""
        response = self._request("POST", "/accounts", body=payload)
        if not isinstance(response, dict):
            raise ValueError("CRM create-account response was not a JSON object.")
        # The sandbox can return the created record directly or wrap it.
        return response.get("account", response)

    def update_account(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Patch one account; callers are responsible for approval and SOP checks."""
        response = self._request(
            "PATCH",
            f"/accounts/{urllib.parse.quote(account_id, safe='')}",
            body=payload,
        )
        if not isinstance(response, dict):
            raise ValueError(f"CRM update-account response for {account_id} was not an object.")
        # Keep response unwrapping consistent with create_account().
        return response.get("account", response)
