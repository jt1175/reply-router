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

    def update_contact(
        self,
        contact_id: str,
        custom_fields: dict[str, str] | None = None,
        **other_attrs,
    ) -> None:
        """Update contact custom fields (and optionally other attributes) in a single PATCH.

        Per spec §4.3 step 9 / §6.2 principle 1: multi-field updates use GHL's
        single PATCH request (closest available to atomic across fields).
        """
        url = f"{GHL_BASE_URL}/contacts/{contact_id}"
        payload: dict[str, Any] = dict(other_attrs)
        if custom_fields:
            payload["customFields"] = [
                {"id": cf_id, "value": value} for cf_id, value in custom_fields.items()
            ]
        try:
            resp = requests.put(url, headers=self._headers(), json=payload, timeout=10)
        except requests.RequestException as exc:
            raise RuntimeError(f"GHL update_contact failed: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"GHL update_contact failed: status={resp.status_code} body={resp.text[:200]}"
            )

    def add_tags(self, contact_id: str, tags: list[str]) -> None:
        url = f"{GHL_BASE_URL}/contacts/{contact_id}/tags"
        resp = requests.post(url, headers=self._headers(), json={"tags": tags}, timeout=10)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"GHL add_tags failed: status={resp.status_code} body={resp.text[:200]}"
            )

    def add_note(self, contact_id: str, body: str) -> None:
        url = f"{GHL_BASE_URL}/contacts/{contact_id}/notes"
        resp = requests.post(url, headers=self._headers(), json={"body": body}, timeout=10)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"GHL add_note failed: status={resp.status_code} body={resp.text[:200]}"
            )

    def move_to_pipeline_stage(
        self, contact_id: str, pipeline_id: str, stage_id: str
    ) -> None:
        """Find or create the contact's opportunity in this pipeline; move to stage_id."""
        # Find existing opportunity for this contact in this pipeline
        url = f"{GHL_BASE_URL}/opportunities/search"
        params = {"location_id": self.sub_account_id, "contact_id": contact_id, "pipeline_id": pipeline_id}
        resp = requests.get(url, headers=self._headers(), params=params, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(
                f"GHL opportunity search failed: status={resp.status_code}"
            )
        opportunities = resp.json().get("opportunities", [])
        if not opportunities:
            # Create new opportunity
            create_url = f"{GHL_BASE_URL}/opportunities"
            create_payload = {
                "locationId": self.sub_account_id,
                "contactId": contact_id,
                "pipelineId": pipeline_id,
                "pipelineStageId": stage_id,
                "status": "open",
            }
            resp = requests.post(create_url, headers=self._headers(), json=create_payload, timeout=10)
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"GHL opportunity create failed: status={resp.status_code}")
            return
        op_id = opportunities[0]["id"]
        update_url = f"{GHL_BASE_URL}/opportunities/{op_id}"
        resp = requests.put(
            update_url,
            headers=self._headers(),
            json={"pipelineStageId": stage_id},
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GHL opportunity update failed: status={resp.status_code}")

    def add_to_dnc(self, contact_id: str) -> None:
        """Add contact to GHL's Do-Not-Contact list.

        Per spec §6.2 principle 5: failures here are CAN-SPAM critical; the
        orchestrator handles 3× retry + URGENT escalation, not this method.
        This method just raises on failure and lets caller decide.
        """
        url = f"{GHL_BASE_URL}/contacts/{contact_id}/dnd"
        resp = requests.post(
            url, headers=self._headers(), json={"channel": "email", "value": True}, timeout=10
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"GHL add_to_dnc failed: status={resp.status_code} body={resp.text[:200]}"
            )
