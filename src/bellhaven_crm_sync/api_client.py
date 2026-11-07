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
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _records_from_response(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if not isinstance(response, dict):
        raise ValueError("CRM account-list response must be a JSON object or array.")
    for key in ("accounts", "results", "items", "data"):
        if key in response:
            return _records_from_response(response[key])
    if response and all(isinstance(value, dict) for value in response.values()):
        return list(response.values())
    return []


class CRMClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: int = 20,
    ) -> None:
        self.base_url = (base_url or os.getenv(
            "BELLHAVEN_API_BASE",
            "https://analyst-assessment-production.up.railway.app/api/v1",
        )).rstrip("/")
        self.token = token or os.getenv("BELLHAVEN_API_TOKEN", "")
        self.timeout = timeout
        if not self.token:
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
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"CRM API {method} {path} failed with HTTP {error.code}: {detail[:500]}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(f"CRM API {method} {path} could not be reached: {error}") from error
        except json.JSONDecodeError as error:
            raise RuntimeError(f"CRM API {method} {path} returned invalid JSON.") from error

    def get_all_accounts(self, page_size: int = 50) -> list[dict[str, Any]]:
        if page_size < 1:
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
            if len(current) >= page_size and current_ids and current_ids <= seen_ids:
                raise RuntimeError(
                    "CRM account pagination repeated a full page; refusing to continue with incomplete data."
                )
            seen_ids.update(current_ids)
            accounts.extend(current)
            if len(current) < page_size:
                return accounts
            if page >= 100:
                raise RuntimeError("CRM account pagination exceeded 100 pages.")
            page += 1

    def get_identity(self) -> dict[str, Any]:
        response = self._request("GET", "/me")
        if not isinstance(response, dict):
            raise ValueError("CRM identity response was not a JSON object.")
        return response

    def get_account(self, account_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/accounts/{urllib.parse.quote(account_id, safe='')}")
        if not isinstance(response, dict):
            raise ValueError(f"CRM account {account_id} response was not a JSON object.")
        if isinstance(response.get("account"), dict):
            return response["account"]
        return response

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", "/accounts", body=payload)
        if not isinstance(response, dict):
            raise ValueError("CRM create-account response was not a JSON object.")
        return response.get("account", response)

    def update_account(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            f"/accounts/{urllib.parse.quote(account_id, safe='')}",
            body=payload,
        )
        if not isinstance(response, dict):
            raise ValueError(f"CRM update-account response for {account_id} was not an object.")
        return response.get("account", response)
