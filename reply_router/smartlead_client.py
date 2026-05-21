"""Smartlead API client.

Verified endpoints (2026-05-16):
- send_reply_in_thread: POST /api/v1/campaigns/{cid}/reply-email-thread
  body: {email_stats_id, email_body, reply_message_id}
  query: api_key

Gated endpoints (verification flag must be flipped to True before live use,
after JT/Step 2 research confirms the URL — see docs/smartlead-api-research.md
and Task 2.4 step 2):
- list_replies   → _LIST_REPLIES_ENDPOINT_VERIFIED
- mark_unsubscribe → _MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"
DEFAULT_TIMEOUT_SEC = 15

# Verification flags. MUST stay False until the endpoint URL is confirmed via the
# Step 2 research. When False, the corresponding method raises immediately on call,
# guaranteeing an unverified URL can never ship to production.
_LIST_REPLIES_ENDPOINT_VERIFIED = False
_MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED = False


class SmartleadError(RuntimeError):
    """Raised when Smartlead returns non-2xx or the network call fails."""


class SmartleadClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("SmartleadClient requires a non-empty api_key")
        self.api_key = api_key

    def _params(self, **extra) -> dict:
        return {"api_key": self.api_key, **extra}

    def send_reply_in_thread(
        self,
        campaign_id: str,
        email_stats_id: str,
        body: str,
        reply_message_id: str,
    ) -> None:
        """Send a reply that threads back into the original Gmail conversation.

        Per spec §11.1 + Task 2.4 step 1 (JT-gated manual verification): `reply_message_id`
        is the Smartlead-docs-confirmed knob for "Message-ID being replied to." JT must
        still complete the Gmail threading test before this is wired into responder.py
        (Task 3.4).
        """
        url = f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/reply-email-thread"
        payload = {
            "email_stats_id": email_stats_id,
            "email_body": body,
            "reply_message_id": reply_message_id,
        }
        try:
            resp = requests.post(
                url, params=self._params(), json=payload, timeout=DEFAULT_TIMEOUT_SEC
            )
        except requests.RequestException as exc:
            raise SmartleadError(f"send_reply_in_thread network error: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise SmartleadError(
                f"send_reply_in_thread failed: status={resp.status_code} "
                f"body={resp.text[:200]}"
            )

    def list_replies(
        self, campaign_ids: list[str], since: datetime
    ) -> list[dict[str, Any]]:
        """List replies received since `since` across all given campaigns.

        URL is TENTATIVE until Step 2 research confirms — see
        docs/smartlead-api-research.md. While _LIST_REPLIES_ENDPOINT_VERIFIED is
        False, this raises immediately on call.
        """
        if not _LIST_REPLIES_ENDPOINT_VERIFIED:
            raise RuntimeError(
                "Smartlead list_replies endpoint not yet verified — see Task 2.4 step 2 "
                "and docs/smartlead-api-research.md. Set _LIST_REPLIES_ENDPOINT_VERIFIED "
                "to True after replacing the URL with the confirmed one."
            )
        replies: list[dict[str, Any]] = []
        for cid in campaign_ids:
            # REPLACE with confirmed endpoint from Step 2 research:
            url = f"{SMARTLEAD_BASE}/campaigns/{cid}/messages"
            try:
                resp = requests.get(
                    url,
                    params=self._params(since=since.isoformat()),
                    timeout=DEFAULT_TIMEOUT_SEC,
                )
            except requests.RequestException as exc:
                raise SmartleadError(f"list_replies network error: {exc}") from exc
            if resp.status_code != 200:
                raise SmartleadError(
                    f"list_replies failed: status={resp.status_code} cid={cid}"
                )
            data = resp.json()
            # Adjust key if confirmed endpoint uses "replies" / "data" / etc.
            for r in data.get("messages", data.get("replies", [])):
                r["campaign_id"] = cid  # caller relies on this for routing
                replies.append(r)
        return replies

    def find_lead_by_email(self, email: str) -> dict | None:
        """Look up a Smartlead lead by email address.

        Endpoint: GET /leads/?api_key=...&email=...
        Returns lead dict with keys id, email, first_name, lead_campaign_data, ...
        Returns None when the lead doesn't exist (Smartlead returns `{}`).
        """
        if not email:
            return None
        url = f"{SMARTLEAD_BASE}/leads/"
        try:
            resp = requests.get(
                url, params=self._params(email=email), timeout=DEFAULT_TIMEOUT_SEC
            )
        except requests.RequestException as exc:
            raise SmartleadError(f"find_lead_by_email network error: {exc}") from exc
        if resp.status_code != 200:
            raise SmartleadError(
                f"find_lead_by_email failed: status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
        data = resp.json()
        if not data or not isinstance(data, dict) or not data.get("id"):
            return None
        return data

    def pause_lead(self, campaign_id: str, lead_id: str) -> None:
        """Pause a lead's sequence in a campaign. No further emails sent until resumed.

        Endpoint: POST /campaigns/{cid}/leads/{lid}/pause?api_key=...
        Empirically verified via Smartlead API docs (2026-05-20). Handles 404 quietly
        — lead may have already completed sequence or been removed; either way, the
        end state ("no more sends to this lead") is achieved.
        """
        url = f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/leads/{lead_id}/pause"
        try:
            resp = requests.post(url, params=self._params(), timeout=DEFAULT_TIMEOUT_SEC)
        except requests.RequestException as exc:
            raise SmartleadError(f"pause_lead network error: {exc}") from exc
        if resp.status_code == 404:
            logger.info("pause_lead 404 (lead may already be done): campaign=%s lead=%s",
                        campaign_id, lead_id)
            return
        if resp.status_code not in (200, 201, 204):
            raise SmartleadError(
                f"pause_lead failed: status={resp.status_code} "
                f"campaign={campaign_id} lead={lead_id} body={resp.text[:200]}"
            )

    def mark_unsubscribe(self, campaign_id: str, lead_id: str) -> None:
        """Mark a Smartlead lead as unsubscribed in the campaign.

        URL is TENTATIVE until Step 2 research confirms — see
        docs/smartlead-api-research.md. While _MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED is
        False, this raises immediately on call.
        """
        if not _MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED:
            raise RuntimeError(
                "Smartlead mark_unsubscribe endpoint not yet verified — see Task 2.4 step 2 "
                "and docs/smartlead-api-research.md. Set _MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED "
                "to True after replacing the URL with the confirmed one."
            )
        url = f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/leads/{lead_id}/status"
        try:
            resp = requests.patch(
                url,
                params=self._params(),
                json={"status": "unsubscribed"},
                timeout=DEFAULT_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            raise SmartleadError(f"mark_unsubscribe network error: {exc}") from exc
        if resp.status_code not in (200, 201, 204):
            raise SmartleadError(
                f"mark_unsubscribe failed: status={resp.status_code} "
                f"campaign={campaign_id} lead={lead_id}"
            )
