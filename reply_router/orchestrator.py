"""Reply orchestrator — the pipeline shared by webhook and reconciler.

Built incrementally across Tasks 4.1d–4.1h. This stub exists from 4.1a so that
api/replies.py can import it; full behavior lands per the task sequence.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Literal

from reply_router.dedupe import check_rolling, check_soft_lock, acquire_soft_lock, SoftLockState
from reply_router.ghl_client import GHLClient, MultiContactResolution

logger = logging.getLogger(__name__)


@dataclass
class ReplyPayload:
    message_id: str
    from_email: str
    lead_email: str
    campaign_id: str
    reply_text: str
    email_stats_id: str = ""           # Smartlead-specific; needed for send_reply_in_thread
    original_subject: str = ""
    sender_persona: str = ""

    @classmethod
    def from_smartlead_webhook(cls, payload: dict[str, Any]) -> "ReplyPayload":
        """Parse a Smartlead webhook payload. Field names verified against captured
        webhook in Task 5.3 step 1 — update this method then if Smartlead's keys differ.
        """
        return cls(
            message_id=str(payload.get("message_id") or payload.get("id") or ""),
            from_email=str(payload.get("from_email") or payload.get("from") or ""),
            lead_email=str(payload.get("lead_email") or payload.get("to") or ""),
            campaign_id=str(payload.get("campaign_id") or ""),
            reply_text=str(payload.get("reply_text") or payload.get("body") or ""),
            email_stats_id=str(payload.get("email_stats_id") or ""),
            original_subject=str(payload.get("subject") or ""),
            sender_persona=str(payload.get("sender_persona") or payload.get("sender_name") or ""),
        )


@dataclass
class ProcessResult:
    status: Literal[
        "processed", "ignored_self", "duplicate", "in_flight_elsewhere",
        "config_error", "auth_error", "deferred_for_retry", "urgent_handled",
    ]
    http_status: int = 200
    classification: str = ""
    send_mode: str = ""
    notes: list[str] = field(default_factory=list)

    def to_response(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "classification": self.classification,
            "send_mode": self.send_mode,
            "notes": self.notes,
        }


def _normalize_email(raw: str) -> str:
    """Spec §4.4 normalization: parse RFC 5322, lowercase, strip plus-tag, strip whitespace.

    Returns "" for empty / unparseable input (so a missing from_email won't accidentally
    match a sending inbox).
    """
    if not raw:
        return ""
    _, addr = parseaddr(raw)
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return ""
    local, domain = addr.split("@", 1)
    if "+" in local:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def _build_ghl_client(client_config) -> GHLClient:
    api_key = os.environ.get(client_config.ghl.api_key_env, "")
    if not api_key:
        raise RuntimeError(f"missing env var {client_config.ghl.api_key_env}")
    return GHLClient(
        api_key=api_key,
        sub_account_id=client_config.ghl.sub_account_id,
        campaign_ids=client_config.smartlead.campaign_ids,
    )


def _loop_check(from_email: str, sending_inboxes: list[str]) -> bool:
    """Return True if from_email matches one of our sending inboxes (after normalization)."""
    normalized_from = _normalize_email(from_email)
    if not normalized_from:
        return False
    normalized_set = frozenset(_normalize_email(s) for s in sending_inboxes)
    return normalized_from in normalized_set


def process_reply(
    client_config,
    payload: ReplyPayload,
    source: Literal["webhook", "reconciler"] = "webhook",
) -> ProcessResult:
    """Full §4.1 pipeline. Filled in across Tasks 4.1b–4.1g."""
    # §4.1 step 4 — loop check
    if _loop_check(payload.from_email, client_config.sending_inboxes):
        logger.info(
            "loop ignored: from=%s matches sending_inboxes (client=%s, source=%s)",
            payload.from_email, client_config.client_id, source,
        )
        return ProcessResult(status="ignored_self", http_status=200)

    ghl = _build_ghl_client(client_config)
    fids = client_config.ghl.custom_field_ids

    # §4.1 step 5a/5b — resolve contact (creates skeleton if 0 matches)
    contact, resolution = ghl.resolve_contact_by_email(payload.lead_email)

    # §4.1 step 5c — dedupe rolling list
    if check_rolling(contact, fids["last_processed_smartlead_message_ids"], payload.message_id):
        logger.info("dedupe: rolling list hit for message_id=%s contact=%s",
                    payload.message_id, contact["id"])
        return ProcessResult(status="duplicate", http_status=200)

    # §4.1 step 5d — soft lock
    lock_state = check_soft_lock(
        contact, fids["currently_processing_smartlead_message_id"], payload.message_id
    )
    if lock_state == SoftLockState.IN_FLIGHT:
        logger.info("dedupe: soft lock IN_FLIGHT for message_id=%s contact=%s",
                    payload.message_id, contact["id"])
        return ProcessResult(status="in_flight_elsewhere", http_status=200)
    # STALE or ABSENT → proceed (and overwrite if STALE)

    # §4.1 step 5e — acquire soft lock
    acquire_soft_lock(
        ghl, contact["id"],
        fids["currently_processing_smartlead_message_id"],
        payload.message_id,
    )

    # Rest of pipeline lands in 4.1d–4.1g.
    raise NotImplementedError("rest of pipeline lands in Tasks 4.1d–4.1g")
