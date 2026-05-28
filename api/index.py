"""Single FastAPI app exposing all reply-router HTTP routes.

Replaces the previous per-route files (api/replies.py, api/approvals.py,
api/reconcile.py, api/health.py) because Vercel's Python serverless-function
auto-detection wasn't matching the multi-file pattern. One ASGI entrypoint at
api/index.py with a catch-all rewrite in vercel.json is the most reliable shape.

All route paths and behavior are preserved verbatim from the original files.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from reply_router.approvals import (
    clear_draft, csrf_token, find_draft_by_token, is_expired, verify_csrf,
)
from reply_router.config import ConfigError, load_and_validate_all, load_client_config
from reply_router.event_handlers import (
    EVENT_REPLY, detect_event_type, handle_non_reply_event,
)
from reply_router.ghl_client import GHLClient
from reply_router.orchestrator import ReplyPayload, process_reply
from reply_router.qualification_ui import (
    render_confirmation, render_error, render_form, render_gray_zone,
    render_reject, render_slot_picker,
)
from reply_router.qualifier import (
    FAIL_SAFE_RESULT, classify_form, form_csrf, verify_form_csrf,
    verify_url_token,
)
from reply_router.reconciler import reconcile_client
from reply_router.slack_client import _post as _slack_post
from reply_router.smartlead_client import SmartleadClient, SmartleadError

app = FastAPI(title="reply-router", version="0.1.0")
logger = logging.getLogger("api.index")


_BOOKING_URL_RE = re.compile(
    r"https://reply-router\.vercel\.app/v1/clients/[a-z_]+/qualify/[A-Za-z0-9_-]+\?token=[\d.a-f]+"
)


def _linkify_booking_url(html_body: str, anchor_text: str = "book a walkthrough call") -> str:
    """Replace bare booking-link URLs with HTML anchors so recipients see clickable
    text instead of a wall of token-laden URL.

    Bare URL example:
        https://reply-router.vercel.app/v1/clients/clear_facility/qualify/<id>?token=<ts>.<hex>
    After:
        <a href="…">book a walkthrough call</a>

    Runs AFTER _draft_to_html so the HTML wrapping is preserved. Idempotent —
    if the URL is already wrapped in an anchor, the regex won't double-match
    because anchor URLs are inside attribute values.
    """
    def _replace(match: re.Match) -> str:
        url = match.group(0)
        # Don't double-wrap if URL is already inside an <a href=...> attribute
        start = match.start()
        before = html_body[max(0, start - 12):start]
        if 'href="' in before or "href='" in before:
            return url
        return f'<a href="{url}">{anchor_text}</a>'
    return _BOOKING_URL_RE.sub(_replace, html_body)


def _draft_to_html(text: str) -> str:
    """Wrap a plain-text AI draft in minimal HTML so Smartlead's reply-email-thread
    renders paragraph breaks in Gmail. Without this, blank-line separated paragraphs
    collapse into a wall of text on the recipient's side (Smartlead treats
    `email_body` as HTML and strips whitespace formatting).

    Idempotent: if the input already contains HTML tags, returns it unchanged.
    Otherwise paragraphs are html.escape()'d before wrapping — covers the case
    where Claude writes a literal `<5,000 sqft` or `M&M Cleaning` in a reply.
    """
    import html
    if not text or not text.strip():
        return text
    lo = text.lower()
    if "<p>" in lo or "<div>" in lo or "<br" in lo:
        return text
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    return "".join(
        f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs
    )


def _clients_dir() -> Path:
    return Path(os.environ.get("REPLY_ROUTER_CLIENTS_DIR", "clients"))


def _load_client(client_id: str):
    try:
        return load_client_config(_clients_dir() / f"{client_id}.json")
    except (ConfigError, FileNotFoundError) as exc:
        logger.error("config load failed for client_id=%s err=%s", client_id, exc)
        raise HTTPException(status_code=500, detail="config_load_failed") from exc


def _check_router_secret(client_config, provided_secret: str) -> None:
    expected = os.environ.get(client_config.auth.router_secret_env, "")
    if not expected or provided_secret != expected:
        logger.warning("auth fail for client_id=%s", client_config.client_id)
        raise HTTPException(status_code=401, detail="unauthorized")


def _build_ghl(cfg) -> GHLClient:
    return GHLClient(
        api_key=os.environ[cfg.ghl.api_key_env],
        sub_account_id=cfg.ghl.sub_account_id,
        campaign_ids=cfg.smartlead.campaign_ids,
    )


# ─── Health ──────────────────────────────────────────────────────────────────

@app.get("/v1/health")
async def health():
    return {
        "status": "ok",
        "git_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA", "unknown")[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─── Smartlead reply webhook ─────────────────────────────────────────────────

@app.post("/v1/clients/{client_id}/replies")
async def handle_reply(
    client_id: str,
    request: Request,
    x_router_secret: str = Header(default=""),
    secret: str = Query(default=""),
):
    # Smartlead webhooks have no custom-header config — accept the shared secret
    # via ?secret= query param as a fallback. Header wins if both present.
    # The single webhook URL receives all event categories (REPLY, OPEN, CLICK,
    # BOUNCE, UNSUBSCRIBE). Dispatch by event_type.
    client_config = _load_client(client_id)
    _check_router_secret(client_config, x_router_secret or secret)
    payload = await request.json()

    event_type = detect_event_type(payload)
    # Three branches:
    #   1. Recognized non-reply event → dedicated handler
    #   2. EMAIL_REPLY or UNKNOWN (inferred-shape) → fall through to existing reply path
    #   3. Anything else explicitly typed → ignore gracefully (e.g. future Smartlead
    #      event types we don't handle yet, custom event_types in tests, etc.)
    #
    # Critical: ALL paths must return 200. Smartlead's circuit breaker pauses webhook
    # delivery after 4 consecutive 5xx — see reference_smartlead_webhook_shape memory.
    KNOWN_NON_REPLY = {"EMAIL_OPEN", "LINK_CLICKED", "EMAIL_BOUNCED", "EMAIL_UNSUBSCRIBED"}

    if event_type in KNOWN_NON_REPLY:
        ghl = _build_ghl(client_config)
        try:
            result = handle_non_reply_event(event_type, payload, ghl, client_config)
        except Exception as exc:
            logger.exception("non-reply event handler crashed: %s", exc)
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)
        return JSONResponse(result, status_code=200)

    if event_type not in (EVENT_REPLY, "UNKNOWN"):
        # Explicit event_type we don't handle (e.g. future Smartlead categories).
        # Ignore but log so we can build a handler later if it shows up in prod.
        logger.info("ignoring unhandled event_type=%r", event_type)
        return JSONResponse(
            {"status": "ignored", "reason": f"unhandled event_type={event_type}"},
            status_code=200,
        )

    # Fall through to the reply path. Wrap parsing + process_reply in a SINGLE
    # try/except so anything malformed (missing fields, junk payloads, downstream
    # crashes in process_reply) returns 200 ignored rather than 5xx + circuit
    # breaker risk. ReplyPayload uses .get() defaults so it tolerates missing fields,
    # but process_reply may crash on empty values (e.g. GHL lookup with empty email).
    try:
        rp = ReplyPayload.from_smartlead_webhook(payload)
        result = process_reply(client_config, rp, source="webhook")
        return JSONResponse(content=result.to_response(), status_code=result.http_status)
    except Exception as exc:
        logger.warning("reply path crashed on malformed payload — ignored: %s; keys=%s",
                       exc, list(payload.keys()) if isinstance(payload, dict) else type(payload))
        return JSONResponse(
            {"status": "ignored", "reason": f"malformed payload: {type(exc).__name__}"},
            status_code=200,
        )


# ─── Shadow-mode approval UI ─────────────────────────────────────────────────

def _render_form(token: str, draft: str, csrf: str, iat: int, contact: dict, client_id: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Review Draft Reply</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 800px; margin: 2em auto; padding: 0 1em; }}
  textarea {{ width: 100%; min-height: 200px; font-family: monospace; }}
  .ctx {{ background: #f4f4f4; padding: 1em; border-radius: 4px; margin: 1em 0; }}
  button {{ padding: 8px 16px; margin-right: 8px; }}
  .send {{ background: #2ecc71; color: white; border: none; border-radius: 4px; }}
  .discard {{ background: #95a5a6; color: white; border: none; border-radius: 4px; }}
</style></head>
<body>
<h1>Review draft reply</h1>
<div class="ctx">
  <strong>Contact:</strong> {escape(contact.get('firstName') or contact.get('name') or '—')}<br>
  <strong>Company:</strong> {escape(contact.get('companyName') or '—')}<br>
  <strong>Email:</strong> {escape(contact.get('email') or '—')}
</div>
<form method="POST" action="/v1/clients/{escape(client_id)}/approvals/{escape(token)}/send">
  <label><strong>Draft (edit before sending if needed):</strong></label>
  <textarea name="draft_text">{escape(draft, quote=False)}</textarea>
  <input type="hidden" name="csrf" value="{escape(csrf)}">
  <input type="hidden" name="form_issued_at_unix" value="{iat}">
  <p>
    <button class="send" type="submit">Send</button>
    <button class="discard" type="submit" formaction="/v1/clients/{escape(client_id)}/approvals/{escape(token)}/discard">Discard</button>
  </p>
</form>
</body></html>"""


def _gone_page(reason: str) -> HTMLResponse:
    return HTMLResponse(
        f"<h1>Draft no longer available</h1><p>{escape(reason)}</p>",
        status_code=410,
    )


@app.get("/v1/clients/{client_id}/approvals/{token}", response_class=HTMLResponse)
def get_approval(client_id: str, token: str):
    cfg = _load_client(client_id)
    ghl = _build_ghl(cfg)
    fids = cfg.ghl.custom_field_ids

    contact = find_draft_by_token(ghl, token, fids["pending_draft_token"])
    if contact is None:
        return _gone_page("This draft was already handled or never existed.")

    by_id = {cf["id"]: cf.get("value", "") for cf in contact.get("customFields") or []}
    created_at = by_id.get(fids["pending_draft_created_at"], "")
    draft_text = by_id.get(fids["pending_draft_text"], "")

    if not created_at or is_expired(created_at):
        clear_draft(
            ghl, contact["id"],
            token_field_id=fids["pending_draft_token"],
            text_field_id=fids["pending_draft_text"],
            created_at_field_id=fids["pending_draft_created_at"],
            reply_message_id_field_id=fids["pending_reply_message_id"],
            reply_email_stats_id_field_id=fids["pending_reply_email_stats_id"],
        )
        return _gone_page("This draft expired (7-day TTL).")

    iat = int(time.time())
    sig = csrf_token(os.environ[cfg.auth.router_secret_env], token, iat)
    headers = {"Referrer-Policy": "no-referrer"}
    return HTMLResponse(
        _render_form(token, draft_text, sig, iat, contact, client_id),
        headers=headers,
    )


@app.post("/v1/clients/{client_id}/approvals/{token}/send", response_class=HTMLResponse)
async def post_approval_send(
    client_id: str,
    token: str,
    draft_text: str = Form(""),
    csrf: str = Form(""),
    form_issued_at_unix: str = Form("0"),
):
    cfg = _load_client(client_id)
    secret = os.environ[cfg.auth.router_secret_env]
    try:
        iat = int(form_issued_at_unix)
    except ValueError:
        iat = 0
    if not verify_csrf(secret, token, iat, csrf):
        raise HTTPException(403, "csrf_invalid_or_expired")

    ghl = _build_ghl(cfg)
    fids = cfg.ghl.custom_field_ids
    contact = find_draft_by_token(ghl, token, fids["pending_draft_token"])
    if contact is None:
        return _gone_page("This draft was already handled.")

    by_id = {cf["id"]: cf.get("value", "") for cf in contact.get("customFields") or []}
    if is_expired(by_id.get(fids["pending_draft_created_at"], "")):
        clear_draft(
            ghl, contact["id"],
            token_field_id=fids["pending_draft_token"],
            text_field_id=fids["pending_draft_text"],
            created_at_field_id=fids["pending_draft_created_at"],
            reply_message_id_field_id=fids["pending_reply_message_id"],
            reply_email_stats_id_field_id=fids["pending_reply_email_stats_id"],
        )
        return _gone_page("This draft expired before it was sent.")

    email_stats_id = by_id.get(fids["pending_reply_email_stats_id"], "")
    reply_message_id = by_id.get(fids["pending_reply_message_id"], "")
    if not (email_stats_id and reply_message_id):
        logger.error(
            "approval send blocked: missing threading params for contact=%s token=%s",
            contact["id"], token,
        )
        raise HTTPException(409, "draft_missing_threading_params")

    # Pull the stored campaign_id (which campaign the original outbound came
    # from). Fallback to campaign_ids[0] for legacy drafts that pre-date the
    # pending_reply_campaign_id field (added 2026-05-21).
    reply_campaign_id = (
        by_id.get(fids.get("pending_reply_campaign_id", ""), "")
        or cfg.smartlead.campaign_ids[0]
    )

    smartlead = SmartleadClient(api_key=os.environ[cfg.smartlead.api_key_env])
    try:
        smartlead.send_reply_in_thread(
            campaign_id=reply_campaign_id,
            email_stats_id=email_stats_id,
            body=_linkify_booking_url(_draft_to_html(draft_text)),
            reply_message_id=reply_message_id,
        )
    except SmartleadError as exc:
        logger.error("approval send failed: %s", exc)
        raise HTTPException(502, "smartlead_send_failed")

    ghl.add_note(contact["id"], f"approved + sent at {time.time()}: {draft_text}")
    clear_draft(
        ghl, contact["id"],
        token_field_id=fids["pending_draft_token"],
        text_field_id=fids["pending_draft_text"],
        created_at_field_id=fids["pending_draft_created_at"],
        reply_message_id_field_id=fids["pending_reply_message_id"],
        reply_email_stats_id_field_id=fids["pending_reply_email_stats_id"],
    )

    slack_url = os.environ.get(cfg.slack.incoming_webhook_url_env, "")
    if slack_url:
        try:
            _slack_post(slack_url, {"text": f"Approved + sent for contact {contact.get('email', '—')} at {time.strftime('%H:%M UTC')}"})
        except Exception:
            pass

    return HTMLResponse("<h1>Sent</h1><p>The reply was sent to the prospect.</p>")


@app.post("/v1/clients/{client_id}/approvals/{token}/discard", response_class=HTMLResponse)
async def post_approval_discard(
    client_id: str,
    token: str,
    csrf: str = Form(""),
    form_issued_at_unix: str = Form("0"),
):
    cfg = _load_client(client_id)
    secret = os.environ[cfg.auth.router_secret_env]
    try:
        iat = int(form_issued_at_unix)
    except ValueError:
        iat = 0
    if not verify_csrf(secret, token, iat, csrf):
        raise HTTPException(403, "csrf_invalid_or_expired")

    ghl = _build_ghl(cfg)
    fids = cfg.ghl.custom_field_ids
    contact = find_draft_by_token(ghl, token, fids["pending_draft_token"])
    if contact is None:
        return _gone_page("Already handled.")

    ghl.add_note(contact["id"], "draft discarded by operator")
    clear_draft(
        ghl, contact["id"],
        token_field_id=fids["pending_draft_token"],
        text_field_id=fids["pending_draft_text"],
        created_at_field_id=fids["pending_draft_created_at"],
        reply_message_id_field_id=fids["pending_reply_message_id"],
        reply_email_stats_id_field_id=fids["pending_reply_email_stats_id"],
    )
    return HTMLResponse("<h1>Discarded</h1><p>The draft was cleared without sending.</p>")


# ─── Qualification booking flow ──────────────────────────────────────────────

def _qualification_is_configured(cfg) -> tuple[bool, str]:
    """Check that this client has the qualification flow fully wired up.

    Returns (True, '') if configured; (False, reason) otherwise. Reasons name
    the specific field so JT can see exactly what's missing.
    """
    if not cfg.ghl.calendar_id or cfg.ghl.calendar_id.startswith("TBD_"):
        return False, "ghl.calendar_id not set (placeholder TBD_ value)"
    if not cfg.qualification_rubric:
        return False, "qualification_rubric not set"
    if not cfg.qualify_pipeline_stage_id or cfg.qualify_pipeline_stage_id.startswith("TBD_"):
        return False, "qualify_pipeline_stage_id not set (placeholder TBD_ value)"
    if not cfg.gray_zone_pipeline_stage_id or cfg.gray_zone_pipeline_stage_id.startswith("TBD_"):
        return False, "gray_zone_pipeline_stage_id not set (placeholder TBD_ value)"
    if not cfg.reject_pipeline_stage_id or cfg.reject_pipeline_stage_id.startswith("TBD_"):
        return False, "reject_pipeline_stage_id not set (placeholder TBD_ value)"
    for key in ("qualification_form_answers", "qualification_result", "qualification_submitted_at"):
        v = cfg.ghl.custom_field_ids.get(key, "")
        if not v or v.startswith("TBD_"):
            return False, f"ghl.custom_field_ids.{key} not set (placeholder TBD_ value)"
    return True, ""


def _contact_to_account_context(contact: dict) -> dict:
    """Extract enrichment context from a GHL contact's customFields for the qualifier."""
    by_id = {cf["id"]: cf.get("value") for cf in contact.get("customFields") or []}
    # Pull whatever's there — qualifier handles missing fields gracefully.
    return {
        "company_name": contact.get("companyName") or "",
        "first_name": contact.get("firstName") or "",
        "email": contact.get("email") or "",
        "title": contact.get("title") or contact.get("companyTitle") or "",
        "ghl_custom_fields_by_id": by_id,
    }


def _parse_free_slots(ghl_response: dict, max_slots: int = 12) -> list[dict]:
    """Parse GHL's free-slots map into a flat list of {start_iso, label} dicts.

    GHL returns a map keyed by ISO date (YYYY-MM-DD), each holding a `slots` list
    of ISO datetimes. Returns the first `max_slots` slots across all dates.
    Defensive against shape variation: skips keys it can't parse.
    """
    from datetime import datetime
    slots: list[dict] = []
    if not isinstance(ghl_response, dict):
        return slots
    for date_key, day_data in ghl_response.items():
        if not date_key or len(date_key) < 10:  # skip non-date keys like "_dates_"
            continue
        if not isinstance(day_data, dict):
            continue
        for slot_iso in day_data.get("slots") or []:
            if not isinstance(slot_iso, str):
                continue
            try:
                dt = datetime.fromisoformat(slot_iso.replace("Z", "+00:00"))
                label = dt.strftime("%a %b %d, %-I:%M %p")
            except (ValueError, TypeError):
                label = slot_iso
            slots.append({"start_iso": slot_iso, "label": label})
            if len(slots) >= max_slots:
                return slots
    return slots


@app.get("/v1/clients/{client_id}/qualify/{contact_id}", response_class=HTMLResponse)
def get_qualification_form(client_id: str, contact_id: str, token: str = Query(default="")):
    """Render the qualification form. URL token authenticates the link (14-day TTL)."""
    cfg = _load_client(client_id)
    ok, reason = _qualification_is_configured(cfg)
    if not ok:
        logger.warning("qualification not configured for client=%s: %s", client_id, reason)
        return HTMLResponse(
            render_error(cfg.client_display_name, f"This booking flow is not yet available. (admin: {reason})"),
            status_code=503,
        )

    secret = os.environ.get(cfg.auth.router_secret_env, "")
    if not verify_url_token(secret, contact_id, token):
        logger.warning("invalid url_token for contact=%s", contact_id)
        return HTMLResponse(
            render_error(cfg.client_display_name, "This booking link has expired or is invalid. Please reply to the original email and we'll send a fresh one."),
            status_code=403,
        )

    ghl = _build_ghl(cfg)
    contact = ghl.get_contact_by_id(contact_id)
    if not contact:
        return HTMLResponse(
            render_error(cfg.client_display_name, "We couldn't find your record on our side. Please reply to the original email."),
            status_code=404,
        )

    iat = int(time.time())
    csrf = form_csrf(secret, contact_id, iat)
    action_path = f"/v1/clients/{client_id}/qualify/{contact_id}"
    return HTMLResponse(
        render_form(
            contact=contact,
            token=token,
            csrf=csrf,
            form_issued_at_unix=iat,
            company_display_name=cfg.client_display_name,
            action_path=action_path,
        ),
        headers={"Referrer-Policy": "no-referrer"},
    )


@app.post("/v1/clients/{client_id}/qualify/{contact_id}", response_class=HTMLResponse)
async def post_qualification_form(
    client_id: str,
    contact_id: str,
    request: Request,
    token: str = Form(""),
    csrf: str = Form(""),
    form_issued_at_unix: str = Form("0"),
    building_size_sqft: str = Form(""),
    building_type: str = Form(""),
    current_vendor_status: str = Form(""),
    decision_timeline: str = Form(""),
    monthly_budget_range: str = Form(""),
    best_phone: str = Form(""),
    additional_context: str = Form(""),
):
    """Process form submission → run Claude routing → branch to qualify/gray/reject."""
    import json as _json
    cfg = _load_client(client_id)
    ok, reason = _qualification_is_configured(cfg)
    if not ok:
        return HTMLResponse(
            render_error(cfg.client_display_name, f"This booking flow is not yet available. (admin: {reason})"),
            status_code=503,
        )

    secret = os.environ.get(cfg.auth.router_secret_env, "")
    if not verify_url_token(secret, contact_id, token):
        raise HTTPException(403, "url_token_invalid_or_expired")
    try:
        iat = int(form_issued_at_unix)
    except ValueError:
        iat = 0
    if not verify_form_csrf(secret, contact_id, iat, csrf):
        raise HTTPException(403, "csrf_invalid_or_expired")

    # Collect form answers
    try:
        sqft_int = int(building_size_sqft) if building_size_sqft else 0
    except ValueError:
        sqft_int = 0
    form_answers = {
        "building_size_sqft": sqft_int,
        "building_type": building_type,
        "current_vendor_status": current_vendor_status,
        "decision_timeline": decision_timeline,
        "monthly_budget_range": monthly_budget_range,
        "best_phone": best_phone,
        "additional_context": additional_context,
    }

    ghl = _build_ghl(cfg)
    contact = ghl.get_contact_by_id(contact_id)
    if not contact:
        return HTMLResponse(
            render_error(cfg.client_display_name, "Contact not found."),
            status_code=404,
        )

    # Run Claude routing
    account_context = _contact_to_account_context(contact)
    decision = classify_form(
        form_answers=form_answers,
        account_context=account_context,
        business_context=cfg.business_context,
        rubric=cfg.qualification_rubric,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )

    # Write form answers + decision to GHL (best-effort; don't block UX on GHL failure)
    fids = cfg.ghl.custom_field_ids
    try:
        ghl.update_contact(contact_id, custom_fields={
            fids["qualification_form_answers"]: _json.dumps(form_answers),
            fids["qualification_result"]: decision["decision"],
            fids["qualification_submitted_at"]: datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.exception("GHL field write failed for contact=%s (continuing): %s", contact_id, exc)

    # Branch on decision
    if decision["decision"] == "qualify":
        # Fetch free slots, render slot picker
        from datetime import timedelta
        now_ms = int(time.time() * 1000)
        end_ms = now_ms + 14 * 24 * 3600 * 1000  # 14-day booking window
        try:
            ghl_slots = ghl.get_calendar_free_slots(
                calendar_id=cfg.ghl.calendar_id,
                start_date_unix_ms=now_ms,
                end_date_unix_ms=end_ms,
                timezone="America/Chicago",
            )
            free_slots = _parse_free_slots(ghl_slots)
        except Exception as exc:
            logger.exception("free-slots fetch failed for contact=%s: %s", contact_id, exc)
            free_slots = []

        # Generate a fresh form CSRF for the slot-pick POST
        new_iat = int(time.time())
        new_csrf = form_csrf(secret, contact_id, new_iat)
        booking_action = f"/v1/clients/{client_id}/qualify/{contact_id}/book"
        return HTMLResponse(
            render_slot_picker(
                contact=contact,
                token=token,
                csrf=new_csrf,
                form_issued_at_unix=new_iat,
                company_display_name=cfg.client_display_name,
                booking_action_path=booking_action,
                free_slots=free_slots,
            ),
            headers={"Referrer-Policy": "no-referrer"},
        )

    if decision["decision"] == "gray_zone":
        try:
            ghl.move_to_pipeline_stage(
                contact_id, cfg.ghl.pipeline_id, cfg.gray_zone_pipeline_stage_id
            )
        except Exception:
            logger.exception("gray_zone stage move failed for contact=%s", contact_id)
        # Slack notify
        slack_url = os.environ.get(cfg.slack.incoming_webhook_url_env, "")
        if slack_url:
            try:
                _slack_post(slack_url, {
                    "text": (
                        f":warning: Gray-zone qualification — manual review needed.\n"
                        f"Contact: {contact.get('email') or contact_id}\n"
                        f"Company: {contact.get('companyName') or '—'}\n"
                        f"Reasoning: {decision.get('reasoning', '')[:300]}"
                    )
                })
            except Exception:
                pass
        return HTMLResponse(
            render_gray_zone(contact, cfg.client_display_name),
            headers={"Referrer-Policy": "no-referrer"},
        )

    # reject
    try:
        ghl.move_to_pipeline_stage(
            contact_id, cfg.ghl.pipeline_id, cfg.reject_pipeline_stage_id
        )
    except Exception:
        logger.exception("reject stage move failed for contact=%s", contact_id)
    return HTMLResponse(
        render_reject(contact, cfg.client_display_name),
        headers={"Referrer-Policy": "no-referrer"},
    )


@app.post("/v1/clients/{client_id}/qualify/{contact_id}/book", response_class=HTMLResponse)
async def post_qualification_book(
    client_id: str,
    contact_id: str,
    token: str = Form(""),
    csrf: str = Form(""),
    form_issued_at_unix: str = Form("0"),
    selected_slot_iso: str = Form(""),
):
    """Create the GHL appointment + move stage + render confirmation."""
    from datetime import datetime as _dt
    cfg = _load_client(client_id)
    ok, reason = _qualification_is_configured(cfg)
    if not ok:
        return HTMLResponse(
            render_error(cfg.client_display_name, f"Booking is not yet available. (admin: {reason})"),
            status_code=503,
        )

    secret = os.environ.get(cfg.auth.router_secret_env, "")
    if not verify_url_token(secret, contact_id, token):
        raise HTTPException(403, "url_token_invalid_or_expired")
    try:
        iat = int(form_issued_at_unix)
    except ValueError:
        iat = 0
    if not verify_form_csrf(secret, contact_id, iat, csrf):
        raise HTTPException(403, "csrf_invalid_or_expired")

    if not selected_slot_iso:
        return HTMLResponse(
            render_error(cfg.client_display_name, "No time slot selected."),
            status_code=400,
        )

    ghl = _build_ghl(cfg)
    contact = ghl.get_contact_by_id(contact_id)
    if not contact:
        return HTMLResponse(
            render_error(cfg.client_display_name, "Contact not found."),
            status_code=404,
        )

    title = f"Discovery Call: {contact.get('companyName') or contact.get('email') or contact_id}"
    try:
        ghl.create_appointment(
            calendar_id=cfg.ghl.calendar_id,
            contact_id=contact_id,
            start_time_iso=selected_slot_iso,
            title=title,
            appointment_status="confirmed",
            to_notify=True,
        )
    except Exception as exc:
        logger.exception("create_appointment failed for contact=%s slot=%s: %s",
                         contact_id, selected_slot_iso, exc)
        return HTMLResponse(
            render_error(
                cfg.client_display_name,
                "We couldn't lock in that time. Please reply to the original email and we'll book by hand.",
            ),
            status_code=502,
        )

    try:
        ghl.move_to_pipeline_stage(
            contact_id, cfg.ghl.pipeline_id, cfg.qualify_pipeline_stage_id
        )
    except Exception:
        logger.exception("qualify stage move failed for contact=%s", contact_id)

    # Format slot for display
    try:
        dt = _dt.fromisoformat(selected_slot_iso.replace("Z", "+00:00"))
        appt_label = dt.strftime("%A, %B %-d at %-I:%M %p (%Z)")
    except (ValueError, TypeError):
        appt_label = selected_slot_iso

    company_phone = getattr(cfg.business_context, "phone", None) if hasattr(cfg.business_context, "phone") else None
    return HTMLResponse(
        render_confirmation(
            contact=contact,
            appointment_label=appt_label,
            company_display_name=cfg.client_display_name,
            company_phone=company_phone,
        ),
        headers={"Referrer-Policy": "no-referrer"},
    )


# ─── Bidirectional sync: GHL stage change → Smartlead pause ──────────────────

@app.post("/v1/clients/{client_id}/ghl-stage-change")
async def handle_ghl_stage_change(
    client_id: str,
    request: Request,
    x_router_secret: str = Header(default=""),
    secret: str = Query(default=""),
):
    """GHL workflow webhook fires here on opportunity stage change.

    When the new stage is in `pause_on_stage_ids` (Closed Won/Lost by default),
    we look up the contact's Smartlead lead by email and pause its sequence so
    no more follow-ups go out to closed prospects. Idempotent — repeated firings
    for the same opp/stage are no-ops downstream.

    Expected GHL workflow payload (configurable in the workflow's HTTP action):
        {
            "contactId": "...",
            "opportunityId": "...",          # optional, logged but not used
            "currentStage": "<stage_id>",     # GHL stage ID, NOT name
            "previousStage": "<stage_id>",    # optional
            "locationId": "..."               # optional
        }

    Auth: shared secret via ?secret= query param OR X-Router-Secret header
    (GHL workflow HTTP actions support custom headers, but the query form
    matches the Smartlead webhook pattern for symmetry).
    """
    client_config = _load_client(client_id)
    _check_router_secret(client_config, x_router_secret or secret)
    payload = await request.json()

    contact_id = payload.get("contactId") or payload.get("contact_id")
    new_stage = payload.get("currentStage") or payload.get("current_stage") or payload.get("stage_id")
    if not contact_id or not new_stage:
        logger.warning("ghl-stage-change missing required fields: %s", list(payload.keys()))
        return JSONResponse(
            {"status": "ignored", "reason": "missing contactId or currentStage"},
            status_code=200,  # 200 so GHL doesn't trip its own retry circuit
        )

    pause_stages = set(client_config.pause_on_stage_ids or [])
    if not pause_stages:
        return JSONResponse(
            {"status": "ignored", "reason": "no pause_on_stage_ids configured"},
            status_code=200,
        )
    if new_stage not in pause_stages:
        return JSONResponse(
            {"status": "ignored", "reason": "stage not in pause list",
             "stage": new_stage},
            status_code=200,
        )

    # Resolve contact → email → Smartlead lead
    ghl = _build_ghl(client_config)
    contact = ghl.get_contact_by_id(contact_id)
    if not contact:
        logger.warning("ghl-stage-change: contact %s not found", contact_id)
        return JSONResponse(
            {"status": "ignored", "reason": "contact not found"}, status_code=200
        )
    email = contact.get("email") or ""
    if not email:
        return JSONResponse(
            {"status": "ignored", "reason": "contact has no email"}, status_code=200
        )

    smartlead_api_key = os.environ.get(client_config.smartlead.api_key_env, "")
    if not smartlead_api_key:
        return JSONResponse(
            {"status": "deferred", "reason": "smartlead key not configured"},
            status_code=503,
        )
    smartlead = SmartleadClient(api_key=smartlead_api_key)
    try:
        lead = smartlead.find_lead_by_email(email)
    except SmartleadError as exc:
        logger.error("ghl-stage-change: find_lead_by_email failed: %s", exc)
        return JSONResponse(
            {"status": "deferred", "reason": f"smartlead lookup failed: {exc}"},
            status_code=503,
        )
    if not lead or not lead.get("id"):
        return JSONResponse(
            {"status": "ignored", "reason": "lead not found in smartlead",
             "email": email},
            status_code=200,
        )

    lead_id = lead["id"]
    # Pause in every configured campaign the lead is enrolled in.
    paused_in: list[str] = []
    errors: list[str] = []
    for campaign_id in client_config.smartlead.campaign_ids:
        if campaign_id.startswith("TBD_"):
            continue
        try:
            smartlead.pause_lead(campaign_id, str(lead_id))
            paused_in.append(campaign_id)
        except SmartleadError as exc:
            errors.append(f"{campaign_id}: {exc}")
            logger.error("pause_lead failed in campaign=%s: %s", campaign_id, exc)

    # Note the action on the GHL contact for audit trail
    try:
        ghl.add_note(
            contact_id,
            f"Stage→{new_stage}; Smartlead pause: campaigns={paused_in or 'none'} "
            f"errors={errors or 'none'}",
        )
    except Exception:
        logger.exception("ghl-stage-change: failed to add audit note")

    return JSONResponse(
        {
            "status": "processed",
            "contact_id": contact_id,
            "smartlead_lead_id": lead_id,
            "paused_in_campaigns": paused_in,
            "errors": errors,
        },
        status_code=200,
    )


# ─── Nightly reconciler cron ─────────────────────────────────────────────────

@app.post("/api/reconcile")
async def reconcile_endpoint(authorization: str = Header(default="")):
    """Vercel cron POSTs here with Authorization: Bearer <VERCEL_CRON_SECRET>."""
    expected = f"Bearer {os.environ.get('VERCEL_CRON_SECRET', '')}"
    if not os.environ.get('VERCEL_CRON_SECRET') or authorization != expected:
        raise HTTPException(401, "unauthorized")

    configs = load_and_validate_all(_clients_dir())
    summaries = {}
    for client_id, cfg in configs.items():
        try:
            summaries[client_id] = reconcile_client(cfg)
        except Exception as exc:
            logger.exception("reconcile failed for client=%s", client_id)
            summaries[client_id] = {"error": str(exc)}
    return summaries
