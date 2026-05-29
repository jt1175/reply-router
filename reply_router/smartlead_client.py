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
#
# list_replies VERIFIED 2026-05-21 against live Smartlead API via
# POST /master-inbox/inbox-replies. Response shape documented inline below.
_LIST_REPLIES_ENDPOINT_VERIFIED = True
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
        """List replies received since `since` across given campaigns.

        Uses POST /master-inbox/inbox-replies with fetch_message_history=true.
        Verified empirically 2026-05-21 against live Smartlead.

        Real response shape (verified, NOT what the docs claim):
            {
              "ok": true,
              "data": [
                {
                  "lead_email": "...",
                  "email_lead_id": "...",        # = Smartlead's internal lead_id
                  "email_campaign_id": <int>,
                  "lead_first_name": "...", "lead_last_name": "...",
                  "last_reply_time": "ISO",
                  "email_history": [
                    {"type": "SENT", "stats_id": "...", "message_id": "<...>",
                     "subject": "...", "email_body": "<html>...", ...},
                    {"type": "REPLY", "stats_id": "<matches SENT>",
                     "message_id": "<...@mail.gmail.com>", "email_body": "<html>...",
                     "from": "<lead>", "to": "<sender>", "time": "ISO", ...}
                  ]
                }, ...
              ],
              "offset": N, "limit": M
            }

        For each lead, we flatten each REPLY message into a row caller can convert
        to ReplyPayload. The corresponding SENT's stats_id is paired in so threading
        downstream (send_reply_in_thread) keeps working.

        Pagination: response has no total_count. Loop until data array shorter than
        limit (= no more pages).
        """
        if not _LIST_REPLIES_ENDPOINT_VERIFIED:
            raise RuntimeError(
                "Smartlead list_replies endpoint not yet verified — see "
                "_LIST_REPLIES_ENDPOINT_VERIFIED in this module."
            )

        # Smartlead campaignId filter is max 5; chunk if needed.
        # In practice for CFS we have 1 campaign so this is a no-op.
        if len(campaign_ids) > 5:
            raise SmartleadError(
                f"list_replies: campaign_ids filter exceeds Smartlead's max of 5 "
                f"(got {len(campaign_ids)}). Chunk before calling."
            )
        # Smartlead uses NUMERIC campaign IDs; coerce strings to int.
        try:
            campaign_ids_int = [int(cid) for cid in campaign_ids if not cid.startswith("TBD_")]
        except (ValueError, AttributeError) as exc:
            raise SmartleadError(f"list_replies: non-numeric campaign_id: {exc}") from exc
        if not campaign_ids_int:
            return []

        now = datetime.now(since.tzinfo) if since.tzinfo else datetime.utcnow()
        body_filters: dict[str, Any] = {
            "campaignId": campaign_ids_int,
            "replyTimeBetween": [since.isoformat(), now.isoformat()],
        }

        url = f"{SMARTLEAD_BASE}/master-inbox/inbox-replies"
        page_limit = 20  # Smartlead max
        offset = 0
        flattened: list[dict[str, Any]] = []
        while True:
            try:
                resp = requests.post(
                    url,
                    params=self._params(fetch_message_history="true"),
                    json={"offset": offset, "limit": page_limit, "filters": body_filters,
                          "sortBy": "REPLY_TIME_DESC"},
                    timeout=DEFAULT_TIMEOUT_SEC,
                )
            except requests.RequestException as exc:
                raise SmartleadError(f"list_replies network error: {exc}") from exc
            if resp.status_code != 200:
                raise SmartleadError(
                    f"list_replies failed: status={resp.status_code} "
                    f"body={resp.text[:200]}"
                )
            payload_json = resp.json()
            leads = payload_json.get("data") or []
            for lead in leads:
                lead_email = lead.get("lead_email") or ""
                campaign_id = lead.get("email_campaign_id")
                history = lead.get("email_history") or []
                # Pair each REPLY with the SENT whose stats_id matches (for threading)
                sent_by_stats: dict[str, dict] = {
                    h["stats_id"]: h for h in history
                    if h.get("type") == "SENT" and h.get("stats_id")
                }
                for h in history:
                    if h.get("type") != "REPLY":
                        continue
                    stats_id = h.get("stats_id") or ""
                    sent = sent_by_stats.get(stats_id, {})
                    flattened.append({
                        "message_id": h.get("message_id", ""),
                        "from_email": h.get("from") or lead_email,
                        "lead_email": lead_email,
                        "campaign_id": str(campaign_id) if campaign_id else "",
                        "reply_text": h.get("email_body", ""),  # HTML; Claude tolerates
                        "subject": sent.get("subject") or h.get("subject", ""),
                        "email_stats_id": stats_id,
                        # Lead metadata for caller convenience:
                        "lead_first_name": lead.get("lead_first_name", ""),
                        "lead_last_name": lead.get("lead_last_name", ""),
                        "lead_id": lead.get("email_lead_id", ""),
                        "reply_time": h.get("time", ""),
                    })
            if len(leads) < page_limit:
                break  # last page
            offset += page_limit
        return flattened

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

    def resume_lead(self, campaign_id: str, lead_id: str) -> None:
        """Resume a paused lead's sequence in a campaign.

        Endpoint: POST /campaigns/{cid}/leads/{lid}/resume?api_key=...
        Used by the out-of-office carve-out in orchestrator: Smartlead's
        stop_lead_settings=REPLY_TO_AN_EMAIL auto-stops a lead on ANY reply
        including OOO autoresponders. For OOO, we want touches 2-4 to keep
        firing on schedule, so we explicitly resume after detection. Handles
        404 quietly — lead may have already completed sequence or never been
        paused.
        """
        url = f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/leads/{lead_id}/resume"
        try:
            resp = requests.post(url, params=self._params(), timeout=DEFAULT_TIMEOUT_SEC)
        except requests.RequestException as exc:
            raise SmartleadError(f"resume_lead network error: {exc}") from exc
        if resp.status_code == 404:
            logger.info("resume_lead 404 (lead may not exist): campaign=%s lead=%s",
                        campaign_id, lead_id)
            return
        if resp.status_code not in (200, 201, 204):
            raise SmartleadError(
                f"resume_lead failed: status={resp.status_code} "
                f"campaign={campaign_id} lead={lead_id} body={resp.text[:200]}"
            )

    def get_campaign_statistics(
        self,
        campaign_id: str,
        limit: int = 100,
        offset: int = 0,
        event_time_gt: str | None = None,
    ) -> dict:
        """Fetch per-lead engagement stats for a campaign.

        Endpoint: GET /campaigns/{cid}/lead-statistics?api_key=
        Returns dict with `data` list of leads, each with fields:
          lead_email, sequence_number, sent_time, open_time, click_time,
          reply_time, open_count, click_count, is_unsubscribed, is_bounced.

        Paginated — caller iterates `offset` up to `total_stats` (in response).
        Filter `event_time_gt` (YYYY-MM-DD) to skip leads with no events since
        the last reconcile tick.
        """
        url = f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/lead-statistics"
        params = self._params(limit=limit, offset=offset)
        if event_time_gt:
            params["event_time_gt"] = event_time_gt
        try:
            resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT_SEC)
        except requests.RequestException as exc:
            raise SmartleadError(f"get_campaign_statistics network error: {exc}") from exc
        if resp.status_code != 200:
            raise SmartleadError(
                f"get_campaign_statistics failed: status={resp.status_code} "
                f"campaign={campaign_id} body={resp.text[:200]}"
            )
        return resp.json()

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
