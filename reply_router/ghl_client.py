"""GHL (GoHighLevel) CRM API client.

Read operations live here in Task 2.1; write operations + multi-contact
resolution land in Tasks 2.2–2.3.

Per spec §3.2: this module is the only place that knows about GHL's REST
API. Other modules consume this through method calls, never construct
URLs themselves.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any

import requests

logger = logging.getLogger(__name__)

GHL_BASE_URL = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"


class MultiContactResolution(Enum):
    SINGLE = "single"
    RESOLVED_BY_CAMPAIGN = "resolved_by_campaign"
    AMBIGUOUS = "ambiguous"
    CREATED_SKELETON = "created_skeleton"


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

    def get_contact_by_id(self, contact_id: str) -> dict[str, Any] | None:
        """Fetch a single contact by ID. Bypasses the eventually-consistent
        search index — use this when you have an authoritative contact_id (e.g.
        from a `duplicate contact` create-rejection response). Returns None
        on 404; raises on other non-2xx.
        """
        url = f"{GHL_BASE_URL}/contacts/{contact_id}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(
                f"GHL get_contact_by_id failed: status={resp.status_code} body={resp.text[:200]}"
            )
        return resp.json().get("contact")

    def get_contacts_by_email(self, email: str) -> list[dict[str, Any]]:
        """Return all GHL contacts matching this email in the configured sub-account.

        Returns [] if none match. Raises RuntimeError on network/5xx errors so the
        orchestrator can return 5xx to Smartlead for retry.
        """
        url = f"{GHL_BASE_URL}/contacts/search"
        body = {
            "locationId": self.sub_account_id,
            "pageLimit": 20,
            "filters": [{"field": "email", "operator": "eq", "value": email}],
        }
        try:
            resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
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
        self, contact_id: str, pipeline_id: str, stage_id: str, name: str = ""
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
            # GHL v2 requires trailing slash on POST and a non-empty `name`.
            create_url = f"{GHL_BASE_URL}/opportunities/"
            create_payload = {
                "locationId": self.sub_account_id,
                "contactId": contact_id,
                "pipelineId": pipeline_id,
                "pipelineStageId": stage_id,
                "status": "open",
                "name": name or contact_id,
            }
            resp = requests.post(create_url, headers=self._headers(), json=create_payload, timeout=10)
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"GHL opportunity create failed: status={resp.status_code} body={resp.text[:200]}"
                )
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

    def resolve_contact_by_email(
        self, email: str
    ) -> tuple[dict[str, Any], MultiContactResolution]:
        """Resolve a single contact for this email per spec §4.1 step 5b.

        Returns (contact, resolution). Caller uses `resolution` to decide:
        - SINGLE: proceed normally
        - RESOLVED_BY_CAMPAIGN: proceed normally
        - AMBIGUOUS: caller forces shadow_send, adds 'ambiguous_contact_match' tag
        - CREATED_SKELETON: caller adds 'auto_created_from_reply' tag + Slack warn
        """
        contacts = self.get_contacts_by_email(email)
        if not contacts:
            # Create skeleton, then re-fetch to detect concurrent creation race
            create_url = f"{GHL_BASE_URL}/contacts"
            create_payload = {
                "locationId": self.sub_account_id,
                "email": email,
                "tags": ["auto_created_from_reply"],
            }
            resp = requests.post(
                create_url, headers=self._headers(), json=create_payload, timeout=10
            )
            if resp.status_code not in (200, 201):
                # GHL's create endpoint rejects duplicate emails with a 400 and
                # returns the existing contact's id in `meta.contactId`. The
                # search-then-create approach races against GHL's eventually-
                # consistent contact-search index: the contact exists, we just
                # didn't see it. Recover by fetching that contact directly.
                if resp.status_code == 400:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = {}
                    existing_id = (body.get("meta") or {}).get("contactId")
                    if existing_id and "duplicat" in (body.get("message") or "").lower():
                        existing = self.get_contact_by_id(existing_id)
                        if existing:
                            logger.warning(
                                "GHL search-index lag for email=%s — search returned 0 but "
                                "create rejected as duplicate; resolved via meta.contactId=%s",
                                email, existing_id,
                            )
                            return existing, MultiContactResolution.SINGLE
                raise RuntimeError(
                    f"GHL skeleton contact create failed: status={resp.status_code} body={resp.text[:200]}"
                )
            created_id = resp.json().get("contact", {}).get("id")
            # Re-fetch: if >1 result, the race happened — pick lowest-id, log warning
            refetched = self.get_contacts_by_email(email)
            if len(refetched) > 1:
                logger.warning(
                    "Concurrent skeleton creation race detected for email=%s; "
                    "found %d contacts post-create",
                    email, len(refetched),
                )
                # Pick the lowest-id (earliest) to keep deterministic
                chosen = min(refetched, key=lambda c: c["id"])
                # Delete the others we created — caller will be told CREATED_SKELETON
                for c in refetched:
                    if c["id"] != chosen["id"] and c["id"] == created_id:
                        # Try to clean up our own creation if it was the duplicate
                        try:
                            requests.delete(
                                f"{GHL_BASE_URL}/contacts/{c['id']}",
                                headers=self._headers(), timeout=10,
                            )
                        except requests.RequestException:
                            pass
                return chosen, MultiContactResolution.CREATED_SKELETON
            return refetched[0] if refetched else {"id": created_id, "email": email}, \
                MultiContactResolution.CREATED_SKELETON

        if len(contacts) == 1:
            return contacts[0], MultiContactResolution.SINGLE

        # 2+ matches — prefer one in our campaigns
        in_campaign = [
            c for c in contacts
            if any(camp in self.campaign_ids for camp in (c.get("campaigns") or []))
        ]
        if len(in_campaign) == 1:
            return in_campaign[0], MultiContactResolution.RESOLVED_BY_CAMPAIGN
        # Tied or none in campaign — pick most recently added, mark ambiguous
        sorted_by_date = sorted(
            contacts, key=lambda c: c.get("dateAdded", ""), reverse=True
        )
        logger.warning(
            "Ambiguous contact match for email=%s: %d candidates, picking most recent",
            email, len(contacts),
        )
        return sorted_by_date[0], MultiContactResolution.AMBIGUOUS

    def list_contacts_with_field(self, field_id: str) -> list[dict[str, Any]]:
        """List contacts that have ANY non-empty value for the given custom field.

        Used by reconciler phases 1 (find stuck soft locks) and 3 (find expired tokens).
        GHL doesn't have a documented "field is set" filter — we fall back to listing
        contacts by tag. The orchestrator tags contacts with `pending_draft` when
        storing a draft (Task 4.1e), so phase 3 uses that tag. For phase 1 (soft lock)
        there's no equivalent tag — phase 1 must fall back to scanning recently-modified
        contacts, which is a v1.1 optimization. For now this method returns [] and
        phase 1 logs a warning that it's unsupported until JT confirms the right
        GHL search approach.

        For v1: returns [] (no-op). Reconciler phases handle the empty list gracefully.
        TODO before launch: verify GHL search-by-customField-presence API and implement.
        """
        # TODO: implement against actual GHL search API. Stubbed to [] so reconciler
        # phases can be unit-tested with mocked GHLClient and don't crash in production
        # against a missing capability.
        logger.warning(
            "list_contacts_with_field is stubbed (returns []) — see Task 4.2 TODO. "
            "field_id=%s", field_id,
        )
        return []

    def search_contacts_by_custom_field(
        self, field_id: str, value: str, unique: bool = False
    ) -> list[dict[str, Any]]:
        """Search contacts where customField[field_id] == value.

        Used primarily by the approval handler (spec §4.3 step 1) to find the contact
        whose pending_draft_token matches a given URL token.

        Args:
            field_id: the GHL custom-field id to filter on
            value:    the value to match
            unique:   if True and >1 contact matches, raise RuntimeError. Use for token lookups
                      where multiple matches indicates a duplicate-token bug.

        Returns: list of contact dicts (each includes its customFields array). Empty if no match.
        """
        url = f"{GHL_BASE_URL}/contacts/search"
        body = {
            "locationId": self.sub_account_id,
            "pageLimit": 20,
            "filters": [
                {"field": f"customFields.{field_id}", "operator": "eq", "value": value}
            ],
        }
        try:
            resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
        except requests.RequestException as exc:
            raise RuntimeError(f"GHL search_contacts_by_custom_field failed: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"GHL search_contacts_by_custom_field failed: status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
        contacts = resp.json().get("contacts", [])
        if unique and len(contacts) > 1:
            raise RuntimeError(
                f"GHL search_contacts_by_custom_field returned multiple contacts with the same token "
                f"value for field_id={field_id} (got {len(contacts)})"
            )
        return contacts
