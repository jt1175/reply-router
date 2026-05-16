"""Reply orchestrator — the pipeline shared by webhook and reconciler.

Built incrementally across Tasks 4.1d–4.1h. This stub exists from 4.1a so that
api/replies.py can import it; full behavior lands per the task sequence.
"""
from __future__ import annotations

import logging
import os
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Literal

from reply_router.approvals import generate_token, store_draft
from reply_router.classifier import classify
from reply_router.dedupe import check_rolling, check_soft_lock, acquire_soft_lock, SoftLockState
from reply_router.ghl_client import GHLClient, MultiContactResolution
from reply_router.responder import generate_contextual, generate_template, requires_shadow
from reply_router.routing import route
from reply_router.slack_client import post_urgent
from reply_router.smartlead_client import SmartleadClient, SmartleadError

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

    # §4.1 step 6 — classify
    cls_result = classify(
        reply_text=payload.reply_text,
        sender_persona=payload.sender_persona,
        sender_email=payload.from_email,
        original_subject=payload.original_subject,
        company_name=contact.get("companyName", ""),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
    )
    classification = cls_result["classification"]
    confidence = cls_result["confidence"]

    # §4.1 step 13a pre-check — booking link sentinel forces shadow
    booking_link_placeholder = requires_shadow(classification, client_config.business_context)

    # §4.1 step 7 — routing (or special path for unknown)
    action_cfg = client_config.classification_actions.get(classification)
    if classification == "unknown" or action_cfg is None:
        # Classifier returned 'unknown' OR unknown classification key — special path,
        # full handling lands in 4.1g (_handle_unknown). For now, mark None so we
        # skip the GHL writes and raise NotImplementedError below.
        action_bundle = None
    else:
        action_bundle = route(
            classification=classification,
            confidence=confidence,
            suggested_followup_date_iso=cls_result.get("suggested_followup_date_iso"),
            classification_action=action_cfg,
            ambiguous_contact=(resolution == MultiContactResolution.AMBIGUOUS),
            skeleton_contact=(resolution == MultiContactResolution.CREATED_SKELETON),
            booking_link_placeholder=booking_link_placeholder,
        )

    # §4.1 steps 8–11 — GHL writes (only when we have a valid action_bundle)
    if action_bundle is not None:
        ghl.update_contact(
            contact["id"],
            custom_fields={
                fids["reply_classification"]: classification,
                fids["reply_received_at"]: datetime.now(timezone.utc).isoformat(),
                fids["contract_end_date"]: action_bundle.contract_end_date_iso or "",
                fids["nurture_bucket"]: action_bundle.nurture_bucket or "",
            },
        )
        ghl.add_tags(contact["id"], action_bundle.tags_to_add)
        ghl.add_note(
            contact["id"],
            body=(
                f"Classified as {classification} (confidence: {confidence})\n"
                f"Reasoning: {cls_result.get('reasoning', '—')}\n\n"
                f"Reply:\n{payload.reply_text}"
            ),
        )
        ghl.move_to_pipeline_stage(
            contact_id=contact["id"],
            pipeline_id=client_config.ghl.pipeline_id,
            stage_id=action_bundle.pipeline_stage_id,
        )

        # §4.1 step 12 — DNC if routing says so (only unsubscribe). 3-retry with URGENT.
        if action_bundle.dnc:
            slack_url = os.environ.get(client_config.slack.incoming_webhook_url_env, "")
            try:
                _ghl_dnc_with_retry(ghl, contact["id"], slack_url, contact, payload)
            except RuntimeError as exc:
                logger.error("DNC write escalated to URGENT after retries: %s", exc)
                return ProcessResult(
                    status="deferred_for_retry",
                    http_status=503,
                    classification=classification,
                    notes=[f"GHL DNC failed after retries: {exc}"],
                )

    # If action_bundle is None (unknown classification), defer to 4.1g's _handle_unknown.
    # For now raise NotImplementedError as the handoff marker.
    if action_bundle is None:
        raise NotImplementedError("unknown classification handling lands in Task 4.1g")

    # §4.1 step 13b — generate response
    responder_result = _generate_response(
        classification=classification,
        payload=payload,
        contact=contact,
        client_config=client_config,
    )

    if responder_result.failed:
        # §7.3 #9b / 9c — defer dedupe complete (don't mark), return 5xx for retry
        return ProcessResult(
            status="deferred_for_retry",
            http_status=503,
            classification=classification,
            notes=["responder failed — soft lock will time out in 10min, retry via Smartlead or reconciler"],
        )

    # §4.1 step 13c — effective send mode (booking-link sentinel + responder shadow can both force shadow)
    effective_send_mode = action_bundle.send_mode
    if responder_result.requires_shadow:
        effective_send_mode = "shadow_send"

    smartlead_api_key = os.environ.get(client_config.smartlead.api_key_env, "")
    smartlead = SmartleadClient(api_key=smartlead_api_key) if smartlead_api_key else None
    approval_url: str | None = None

    if effective_send_mode == "auto_send":
        if smartlead is None:
            return ProcessResult(
                status="deferred_for_retry", http_status=503,
                classification=classification,
                notes=[f"missing env var {client_config.smartlead.api_key_env}"],
            )
        try:
            smartlead.send_reply_in_thread(
                campaign_id=payload.campaign_id,
                email_stats_id=payload.email_stats_id,
                body=responder_result.text,
                reply_message_id=payload.message_id,
            )
        except SmartleadError as exc:
            logger.error("Smartlead send failed: %s", exc)
            # §7.3 #9 — defer dedupe, return 5xx
            return ProcessResult(
                status="deferred_for_retry", http_status=503,
                classification=classification,
                notes=[f"Smartlead send failed: {exc}"],
            )
        ghl.add_note(contact["id"], f"auto-response sent: {responder_result.text}")
    else:
        # shadow_send: store the draft + threading params
        token = generate_token()
        store_draft(
            ghl, contact["id"],
            token_field_id=fids["pending_draft_token"],
            text_field_id=fids["pending_draft_text"],
            created_at_field_id=fids["pending_draft_created_at"],
            token=token,
            draft_text=responder_result.text,
        )
        # ALSO store the Smartlead threading params on the contact so api/approvals.py
        # can pass them to send_reply_in_thread at approve time. Without these, every
        # approved shadow reply would fail or send non-threaded — see reviewer iteration
        # 2 blocker #1.
        ghl.update_contact(
            contact["id"],
            custom_fields={
                fids["pending_reply_message_id"]: payload.message_id,
                fids["pending_reply_email_stats_id"]: payload.email_stats_id,
            },
        )
        approval_url = f"{_vercel_base_url()}/v1/clients/{client_config.client_id}/approvals/{token}"
        logger.info("shadow draft stored token=%s contact=%s", token, contact["id"])

    # Tasks 4.1f (unsubscribe post-send mark_unsubscribe) and 4.1g (mark_complete +
    # _handle_unknown) and 4.1h (Slack notify + final response) land next.
    raise NotImplementedError("post-send + mark_complete + Slack land in Tasks 4.1f-h")


def _ghl_dnc_with_retry(ghl, contact_id: str, slack_url: str, contact: dict, payload: ReplyPayload) -> None:
    """§6.1 row 1: DNC failures retry 3× then URGENT Slack alert.

    Raises RuntimeError after 3 retries — caller catches and returns 5xx so Smartlead
    retries the webhook. Returns normally if any retry succeeds.
    """
    last_err = None
    for attempt in range(3):
        try:
            ghl.add_to_dnc(contact_id)
            return
        except RuntimeError as exc:
            last_err = exc
            logger.warning("GHL DNC write failed attempt=%d err=%s", attempt + 1, exc)
            _time.sleep(0.5 * (attempt + 1))
    # All 3 failed — alert and re-raise
    if slack_url:
        post_urgent(
            slack_url,
            title="Unsubscribe not honored in GHL",
            action_required=(
                f"1. Open GHL contact for {payload.lead_email}\n"
                f"2. Manually add to DNC list\n"
                f"3. Open Smartlead campaign {payload.campaign_id} and manually unsubscribe\n"
                f"4. Reply ✅ in this thread when done"
            ),
            reply_text=payload.reply_text,
        )
    raise RuntimeError(f"GHL DNC failed after 3 retries: {last_err}")


def _generate_response(
    classification: str,
    payload: ReplyPayload,
    contact: dict,
    client_config,
):
    """Dispatch to template or contextual responder based on classification."""
    if classification == "unsubscribe":
        return generate_template(
            classification="unsubscribe",
            account=_to_account(contact),
            business_context=client_config.business_context,
            anthropic_api_key="",  # unsubscribe is static; key not used
        )
    if classification in ("interested", "not_now", "wrong_person"):
        return generate_template(
            classification=classification,
            account=_to_account(contact),
            business_context=client_config.business_context,
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    if classification in ("info_request", "objection"):
        return generate_contextual(
            classification=classification,
            reply_text=payload.reply_text,
            account=_to_account(contact),
            business_context=client_config.business_context,
            sender_persona_name=payload.sender_persona or "the team",
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    raise ValueError(f"unsupported classification: {classification}")


def _to_account(contact: dict) -> dict:
    return {
        "contact_name": contact.get("firstName") or contact.get("name") or "there",
        "company_name": contact.get("companyName", ""),
        "contact_title": contact.get("title", ""),
    }


def _vercel_base_url() -> str:
    return os.environ.get("VERCEL_URL_OVERRIDE") or os.environ.get(
        "VERCEL_PROJECT_PRODUCTION_URL", "https://reply-router.vercel.app"
    )
