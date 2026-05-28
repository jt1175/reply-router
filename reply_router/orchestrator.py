"""Reply orchestrator — the pipeline shared by webhook and reconciler.

Built incrementally across Tasks 4.1d–4.1h. This stub exists from 4.1a so that
api/replies.py can import it; full behavior lands per the task sequence.
"""
from __future__ import annotations

import logging
import os
import re
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parseaddr
from typing import Any, Literal

from reply_router.approvals import generate_token, store_draft
from reply_router.classifier import classify
from reply_router.dedupe import check_rolling, check_soft_lock, acquire_soft_lock, mark_complete, SoftLockState
from reply_router.ghl_client import GHLClient, MultiContactResolution
from reply_router.responder import generate_contextual, generate_template, requires_shadow
from reply_router.routing import route
from reply_router.slack_client import post_classification_notification, post_urgent
from reply_router.smartlead_client import SmartleadClient, SmartleadError

logger = logging.getLogger(__name__)


_HTML_BLOCK_END_RE = re.compile(r"</(p|div|h[1-6]|li|blockquote)>", re.IGNORECASE)
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(s: str) -> str:
    """Convert email HTML body to readable plain text, preserving paragraph breaks.

    Smartlead delivers reply bodies as HTML by default. Without conversion, the
    raw markup leaks into Slack notifications and Claude prompts — which produced
    drafts that misread the prospect's intent and Slack messages that looked
    like a wall of `<div dir="auto">…</div>` markup. This helper is used in
    `from_smartlead_webhook` so the downstream code path always sees plain text.
    """
    if not s:
        return ""
    s = _HTML_BLOCK_END_RE.sub("\n\n", s)
    s = _HTML_BR_RE.sub("\n", s)
    s = _HTML_TAG_RE.sub("", s)
    # Decode common HTML entities (no full parser needed — these cover ~99% of
    # what we see in Smartlead-delivered bodies).
    s = (s.replace("&nbsp;", " ")
           .replace("&#39;", "'").replace("&apos;", "'")
           .replace("&quot;", '"')
           .replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">"))
    # Collapse runs of >2 blank lines + trim trailing spaces.
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]+(?=\n)", "", s)
    return s.strip()


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
        """Parse a Smartlead webhook payload.

        Field names verified empirically from sandbox-router-test on 2026-05-20:
        - ``to_email`` / ``to_name``: the LEAD's identity (Smartlead's naming —
          NOT ``lead_email``/``lead_name``).
        - ``sent_message``: nested object with the original outbound's
          ``message_id``, ``html``, ``text``, ``time``, ``subject``.
        - ``stats_id``: top-level (per-sent-message UUID).
        - The REPLY's content is in a sibling nested object — observed shape is
          ``reply_message``; we also fall through to ``incoming_message`` and
          message-history-style flat ``email_body`` for resilience across
          Smartlead plan tiers.

        Each accessor uses ``a or b or c or ""`` rather than ``payload.get(k, default)``
        so that explicit None values fall through to the next candidate.
        """
        reply = payload.get("reply_message") or payload.get("incoming_message") or {}
        if not isinstance(reply, dict):
            reply = {}
        sent_msg = payload.get("sent_message") if isinstance(payload.get("sent_message"), dict) else {}

        # CRITICAL: For real Smartlead reply webhooks, `payload.from_email` is
        # the SENDING MAILBOX (e.g. sarah.jones@clearfacilitymn.com — whoever
        # sent the original outbound), NOT the sender of the reply. Using it
        # for loop_check caused every real reply to false-positive as a self-
        # loop and get silently ignored at .info level. Empirically diagnosed
        # 2026-05-28 after JT's E2E test: webhook returned 200 with no GHL
        # writes; breadcrumb logging caught `from=mike.brooks@... matches
        # sending_inboxes`. The actual sender of an inbound reply is, by the
        # definition of "inbound reply", the lead. So derive both from_email
        # and lead_email from the lead identity (sl_lead_email > to_email >
        # lead_email > to). This means loop_check still catches the real
        # loop case (lead address accidentally equal to a sending mailbox).
        lead_identity = str(
            payload.get("sl_lead_email")
            or payload.get("to_email")
            or payload.get("lead_email")
            or payload.get("to")
            or ""
        )
        return cls(
            # REPLY's own message_id — used for dedupe rolling list. Must NOT
            # collide with sent_message.message_id, which is the outbound's id.
            message_id=str(
                reply.get("message_id")
                or payload.get("reply_message_id")
                or payload.get("incoming_message_id")
                or payload.get("message_id")
                or ""
            ),
            from_email=lead_identity,
            lead_email=lead_identity,
            campaign_id=str(payload.get("campaign_id") or ""),
            reply_text=_html_to_text(
                reply.get("text")
                or reply.get("email_body")
                or payload.get("reply_text")
                or payload.get("body")
                or reply.get("html")
                or ""
            ),
            email_stats_id=str(
                payload.get("stats_id")
                or payload.get("email_stats_id")
                or ""
            ),
            original_subject=str(
                payload.get("subject")
                or sent_msg.get("subject")
                or reply.get("subject")
                or ""
            ),
            # NOTE: previously this fell back to `to_name`, but Smartlead's
            # `to_name` is the LEAD's name (the email RECIPIENT) — using it as
            # sender_persona caused the AI to think the lead was signing the
            # email and emit generic "The Clear Facility Team" sign-offs.
            # Now: explicit sender_persona/sender_name only; downstream falls
            # back to a sensible default if absent. TODO: resolve from
            # email_account_id via Smartlead /email-accounts/{id} lookup.
            sender_persona=str(
                payload.get("sender_persona")
                or payload.get("sender_name")
                or ""
            ),
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
    # Top-of-pipeline visibility: Vercel's Python runtime suppresses .info-level
    # records, so silent early-returns were invisible in production. Logging the
    # entry point at .warning means every webhook execution leaves a breadcrumb.
    logger.warning(
        "process_reply ENTER client=%s source=%s message_id=%s from=%s lead=%s campaign=%s",
        client_config.client_id, source, payload.message_id,
        payload.from_email, payload.lead_email, payload.campaign_id,
    )

    # §4.1 step 4 — loop check
    if _loop_check(payload.from_email, client_config.sending_inboxes):
        logger.warning(
            "loop ignored: from=%s matches sending_inboxes (client=%s, source=%s)",
            payload.from_email, client_config.client_id, source,
        )
        return ProcessResult(status="ignored_self", http_status=200)

    ghl = _build_ghl_client(client_config)
    fids = client_config.ghl.custom_field_ids

    # §4.1 step 5a/5b — resolve contact (creates skeleton if 0 matches)
    contact, resolution = ghl.resolve_contact_by_email(payload.lead_email)
    logger.warning(
        "resolved contact=%s resolution=%s for lead=%s",
        contact.get("id"), resolution, payload.lead_email,
    )

    # §4.1 step 5c — dedupe rolling list
    if check_rolling(contact, fids["last_processed_smartlead_message_ids"], payload.message_id):
        logger.warning("dedupe: rolling list hit for message_id=%s contact=%s",
                       payload.message_id, contact["id"])
        return ProcessResult(status="duplicate", http_status=200)

    # §4.1 step 5d — soft lock
    lock_state = check_soft_lock(
        contact, fids["currently_processing_smartlead_message_id"], payload.message_id
    )
    if lock_state == SoftLockState.IN_FLIGHT:
        logger.warning("dedupe: soft lock IN_FLIGHT for message_id=%s contact=%s",
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
        company_name=contact.get("companyName") or "",
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
    )
    classification = cls_result["classification"]
    confidence = cls_result["confidence"]
    logger.warning(
        "classified message_id=%s contact=%s classification=%s confidence=%s",
        payload.message_id, contact["id"], classification, confidence,
    )

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
            name=(
                contact.get("contactName")
                or " ".join(filter(None, [contact.get("firstName"), contact.get("lastName")])).strip()
                or contact.get("email")
                or contact["id"]
            ),
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

    # Unknown classification — handed off to human review (§7.3 #13 + §4.1 decision table).
    # Per spec: handoff IS the action; mark complete + return processed.
    if action_bundle is None:
        slack_url = os.environ.get(client_config.slack.incoming_webhook_url_env, "")
        _handle_unknown(ghl, slack_url, contact, payload, cls_result)
        mark_complete(
            ghl, contact,
            rolling_field_id=fids["last_processed_smartlead_message_ids"],
            soft_lock_field_id=fids["currently_processing_smartlead_message_id"],
            message_id=payload.message_id,
        )
        return ProcessResult(
            status="processed", http_status=200, classification="unknown",
        )

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
        # Track which campaign the original outbound came from so the approval
        # endpoint can send the threaded reply via the right Smartlead campaign.
        # Configs without pending_reply_campaign_id wired fall back to
        # campaign_ids[0] in api/index.py — matches pre-2026-05-21 behavior.
        update_fields = {
            fids["pending_reply_message_id"]: payload.message_id,
            fids["pending_reply_email_stats_id"]: payload.email_stats_id,
        }
        if fids.get("pending_reply_campaign_id"):
            update_fields[fids["pending_reply_campaign_id"]] = payload.campaign_id
        ghl.update_contact(contact["id"], custom_fields=update_fields)
        approval_url = f"{_vercel_base_url()}/v1/clients/{client_config.client_id}/approvals/{token}"
        logger.info("shadow draft stored token=%s contact=%s", token, contact["id"])

    # §4.1 step 13d — unsubscribe-only post-send: tell Smartlead to drop the lead
    # so future campaign emails don't get sent. Failures here do NOT 5xx the response
    # (the prospect already got the "removed you" reply; what failed is internal
    # bookkeeping). The URGENT Slack alert IS the recovery path — see §7.3 #4.
    unsub_failed = False
    if classification == "unsubscribe":
        slack_url = os.environ.get(client_config.slack.incoming_webhook_url_env, "")
        unsub_failed = not _smartlead_unsub_with_retry(
            smartlead, payload, contact, slack_url
        )

    # §4.1 step 14 — mark_complete decision table.
    # We only reach this code if responder didn't fail, send didn't fail, DNC didn't
    # escalate. So mark complete unconditionally (the deferred paths returned 503 above).
    # Note: unsub_failed is True only when Smartlead mark_unsubscribe failed — but we STILL
    # mark complete (URGENT Slack alert is recovery; we don't want Smartlead to retry).
    mark_complete(
        ghl, contact,
        rolling_field_id=fids["last_processed_smartlead_message_ids"],
        soft_lock_field_id=fids["currently_processing_smartlead_message_id"],
        message_id=payload.message_id,
    )

    # Defensive pause-on-reply: belt-and-suspenders over Smartlead's
    # stop_lead_settings=REPLY_TO_AN_EMAIL. Observed 2026-05-28: a paused-then-
    # resumed campaign can re-fire a queued follow-up touch to a lead that
    # already replied, because the resume re-arms without re-checking reply
    # state. By explicitly pausing the lead here, we guarantee no further
    # touches even if the campaign is paused/resumed later. Skip on unsubscribe
    # — mark_unsubscribe already handles that path with stronger semantics.
    if (
        smartlead is not None
        and classification != "unsubscribe"
        and payload.campaign_id
    ):
        try:
            sl_lead = smartlead.find_lead_by_email(payload.lead_email)
            if sl_lead and sl_lead.get("id"):
                smartlead.pause_lead(payload.campaign_id, str(sl_lead["id"]))
                logger.warning(
                    "defensive pause_lead OK campaign=%s smartlead_lead=%s contact=%s",
                    payload.campaign_id, sl_lead["id"], contact["id"],
                )
        except SmartleadError as exc:
            # Non-fatal: Smartlead's own stop-on-reply remains the primary
            # mechanism. Log loudly so we notice if this fails consistently.
            logger.error(
                "defensive pause_lead failed (continuing): %s campaign=%s lead=%s",
                exc, payload.campaign_id, payload.lead_email,
            )

    # §4.1 step 15 — Slack notify (best-effort).
    # Routing already encoded the spec's behavior into action_bundle.slack_notify:
    # - normal-confidence unsubscribe → False (per spec §7.3 #1)
    # - low-confidence unsubscribe → True (URGENT path, per spec §5.4)
    # - all other classifications → per classification_action.slack_notify
    # Do NOT override here with `or classification == "unsubscribe"` — that contradicts
    # spec §7.3 #1 and was caught by reviewer iteration 2 blocker #2.
    if action_bundle and action_bundle.slack_notify:
        slack_url_for_notify = os.environ.get(client_config.slack.incoming_webhook_url_env, "")
        if slack_url_for_notify:
            try:
                monitoring_until_date = date.fromisoformat(client_config.monitoring_until)
            except (TypeError, ValueError):
                monitoring_until_date = date.today()  # fallback: assume monitoring already over
            try:
                post_classification_notification(
                    slack_url_for_notify,
                    classification=classification,
                    confidence=confidence,
                    send_mode=effective_send_mode,
                    account={
                        # contact.get(K, default) returns None when the key exists with
                        # a None value (skeleton contacts have this shape). Use `or` to
                        # also fall through on falsy/None values.
                        "company_name": contact.get("companyName") or "—",
                        "contact_name": contact.get("firstName") or contact.get("name") or "—",
                        "contact_title": contact.get("title") or "—",
                        "pipeline_to": action_bundle.pipeline_stage_id,  # raw ID — v1.1 maps to display name
                    },
                    reply_text=payload.reply_text,
                    response_text=responder_result.text if responder_result else "",
                    approval_url=approval_url,
                    monitoring=(date.today() < monitoring_until_date),
                    ghl_contact_url=f"https://app.gohighlevel.com/contact/{contact['id']}",
                )
            except Exception as exc:
                logger.error("slack notify raised: %s", exc)

    return ProcessResult(
        status="processed",
        http_status=200,
        classification=classification,
        send_mode=effective_send_mode,
    )


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


def _render_booking_link(client_config, contact_id: str) -> str:
    """Substitute {contact_id} and {token} placeholders in business_context.booking_link.

    The qualification URL token is signed with the client's router secret and
    encodes a 14-day TTL via verify_url_token. Returns the raw link unchanged if
    no placeholders are present (e.g., still set to PLACEHOLDER sentinel pre-launch).
    """
    raw = client_config.business_context.booking_link or ""
    if "{contact_id}" not in raw and "{token}" not in raw:
        return raw
    from reply_router.qualifier import url_token as _url_token
    secret = os.environ.get(client_config.auth.router_secret_env, "")
    tok = _url_token(secret, contact_id, int(_time.time()))
    return raw.replace("{contact_id}", contact_id).replace("{token}", tok)


def _generate_response(
    classification: str,
    payload: ReplyPayload,
    contact: dict,
    client_config,
):
    """Dispatch to template or contextual responder based on classification."""
    # Render per-contact booking link (no-op if config is still PLACEHOLDER)
    rendered_link = _render_booking_link(client_config, contact["id"])
    business_context = client_config.business_context.model_copy(
        update={"booking_link": rendered_link}
    )

    if classification == "unsubscribe":
        return generate_template(
            classification="unsubscribe",
            account=_to_account(contact),
            business_context=business_context,
            anthropic_api_key="",  # unsubscribe is static; key not used
        )
    if classification in ("not_now", "wrong_person"):
        return generate_template(
            classification=classification,
            account=_to_account(contact),
            business_context=business_context,
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    if classification in ("interested", "info_request", "objection"):
        # 'interested' was previously routed through the template path (200 max-tokens,
        # 1-2 sentence ack + link). That produced terse replies that ignored the
        # specifics prospects mentioned in their reply. Routing through contextual
        # now ensures we engage with substance (sqft, vendor situation, etc.) while
        # still ending with the booking-link CTA.
        return generate_contextual(
            classification=classification,
            reply_text=payload.reply_text,
            account=_to_account(contact),
            business_context=business_context,
            # If we don't know the actual sender persona (Sarah/Mike/Jessica),
            # fall back to a first-person voice with no name — the responder
            # prompt is built to handle this gracefully and avoids inventing
            # a generic "Clear Facility Team" signoff.
            sender_persona_name=payload.sender_persona or "",
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    raise ValueError(f"unsupported classification: {classification}")


def _to_account(contact: dict) -> dict:
    # Same None-vs-default trap as the inline account dict above —
    # use `or` to coerce explicit None values to the empty-string default.
    return {
        "contact_name": contact.get("firstName") or contact.get("name") or "there",
        "company_name": contact.get("companyName") or "",
        "contact_title": contact.get("title") or "",
    }


def _vercel_base_url() -> str:
    url = (
        os.environ.get("VERCEL_URL_OVERRIDE")
        or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
        or "https://reply-router.vercel.app"
    )
    # VERCEL_PROJECT_PRODUCTION_URL is a bare hostname (no scheme). Slack
    # rejects scheme-less URLs in button blocks with `invalid_blocks`.
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def _smartlead_unsub_with_retry(
    smartlead,
    payload: ReplyPayload,
    contact: dict,
    slack_url: str,
) -> bool:
    """Returns True on success, False after all retries fail (Slack alerted).

    Per §7.3 #4: dedupe IS marked complete regardless (the URGENT Slack alert is the
    recovery path; we do NOT 5xx because that would make Smartlead retry the whole
    webhook including re-sending the 'removed you' reply, which already went out).
    """
    if smartlead is None:
        return True
    last_err = None
    for attempt in range(3):
        try:
            smartlead.mark_unsubscribe(
                campaign_id=payload.campaign_id,
                lead_id=contact.get("id"),
            )
            return True
        except SmartleadError as exc:
            last_err = exc
            logger.warning(
                "Smartlead mark_unsubscribe failed attempt=%d err=%s", attempt + 1, exc
            )
            _time.sleep(0.5 * (attempt + 1))
    if slack_url:
        post_urgent(
            slack_url,
            title="GHL DNC done but Smartlead may keep sending",
            action_required=(
                f"GHL DNC has been set for {payload.lead_email}, and the 'removed you' reply "
                f"was sent. But Smartlead `mark_unsubscribe` failed after 3 retries.\n\n"
                f"1. Open Smartlead campaign {payload.campaign_id}\n"
                f"2. Find lead {payload.lead_email} and manually click Unsubscribe\n"
                f"3. Reply ✅ when done"
            ),
            reply_text=payload.reply_text,
        )
    logger.error("Smartlead mark_unsubscribe failed after 3 retries: %s", last_err)
    return False


def _handle_unknown(ghl, slack_url: str, contact: dict, payload: ReplyPayload, cls_result: dict) -> None:
    """Classifier returned 'unknown' after retry — hand off to human, but mark complete.

    Per §7.3 #13 + §4.1 decision table: handoff IS the action.
    """
    ghl.add_tags(contact["id"], ["replied", "unknown"])
    ghl.add_note(
        contact["id"],
        f"MANUAL CLASSIFICATION NEEDED — classifier returned unknown.\n\nReply:\n{payload.reply_text}",
    )
    if slack_url:
        post_urgent(
            slack_url,
            title="MANUAL CLASSIFICATION NEEDED — classifier returned unknown",
            action_required=(
                "1. Open the GHL contact and read the prospect's reply\n"
                "2. Manually set classification + reply via Smartlead UI\n"
                "3. Reply ✅ when done"
            ),
            reply_text=payload.reply_text,
        )
