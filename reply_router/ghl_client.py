"""GHL (GoHighLevel) CRM API client.

Read operations live here in Task 2.1; write operations + multi-contact
resolution land in Tasks 2.2–2.3.

Per spec §3.2: this module is the only place that knows about GHL's REST
API. Other modules consume this through method calls, never construct
URLs themselves.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

GHL_BASE_URL = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"


class MultiContactResolution:  # populated in Task 2.3 (multi-contact resolution)
    pass


class GHLClient:
    def __init__(self, api_key: str, sub_account_id: str, campaign_ids: list[str]):
        self.api_key = api_key
        self.sub_account_id = sub_account_id
        self.campaign_ids = campaign_ids

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Version": GHL_API_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def get_contacts_by_email(self, email: str) -> list[dict[str, Any]]:
        """Return all GHL contacts matching this email in the configured sub-account.

        Returns [] if none match. Raises RuntimeError on network/5xx errors so the
        orchestrator can return 5xx to Smartlead for retry.
        """
        url = f"{GHL_BASE_URL}/contacts/search"
        params = {"locationId": self.sub_account_id, "query": email}
        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=10)
        except requests.RequestException as exc:
            raise RuntimeError(f"GHL contact lookup failed: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"GHL contact lookup failed: status={resp.status_code} body={resp.text[:200]}"
            )
        return resp.json().get("contacts", [])
