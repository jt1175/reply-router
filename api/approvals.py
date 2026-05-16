"""Approval UI for shadow-mode drafts. Spec §4.3.

Routes:
- GET  /v1/clients/{client_id}/approvals/{token}        → render form (200) or 410 if missing/expired
- POST /v1/clients/{client_id}/approvals/{token}/send   → CSRF check → Smartlead send → clear token
- POST /v1/clients/{client_id}/approvals/{token}/discard→ CSRF check → clear token
"""
from __future__ import annotations

import logging
import os
import time
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse

from reply_router.approvals import (
    csrf_token, verify_csrf, clear_draft, find_draft_by_token, is_expired,
)
from reply_router.config import load_client_config, ConfigError
from reply_router.ghl_client import GHLClient
from reply_router.slack_client import _post  # internal best-effort post for confirmation messages
from reply_router.smartlead_client import SmartleadClient, SmartleadError

app = FastAPI(title="reply-router-approvals")
logger = logging.getLogger("api.approvals")


def _clients_dir() -> Path:
    return Path(os.environ.get("REPLY_ROUTER_CLIENTS_DIR", "clients"))


def _load_client(client_id: str):
    try:
        return load_client_config(_clients_dir() / f"{client_id}.json")
    except (ConfigError, FileNotFoundError):
        raise HTTPException(500, "config_load_failed")


def _build_ghl(cfg) -> GHLClient:
    return GHLClient(
        api_key=os.environ[cfg.ghl.api_key_env],
        sub_account_id=cfg.ghl.sub_account_id,
        campaign_ids=cfg.smartlead.campaign_ids,
    )


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
  <strong>Contact:</strong> {escape(contact.get('firstName') or contact.get('name', '—'))}<br>
  <strong>Company:</strong> {escape(contact.get('companyName', '—'))}<br>
  <strong>Email:</strong> {escape(contact.get('email', '—'))}
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

    # Read the Smartlead threading params stored by the orchestrator at draft time
    # (Task 4.1e). Both fields are required in the config schema; they're populated
    # when the shadow draft is stored. Missing → 409 (defensive: never send non-threaded).
    email_stats_id = by_id.get(fids["pending_reply_email_stats_id"], "")
    reply_message_id = by_id.get(fids["pending_reply_message_id"], "")
    if not (email_stats_id and reply_message_id):
        logger.error(
            "approval send blocked: missing threading params for contact=%s token=%s",
            contact["id"], token,
        )
        raise HTTPException(409, "draft_missing_threading_params")

    smartlead = SmartleadClient(api_key=os.environ[cfg.smartlead.api_key_env])
    try:
        smartlead.send_reply_in_thread(
            campaign_id=cfg.smartlead.campaign_ids[0],
            email_stats_id=email_stats_id,
            body=draft_text,
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

    # Best-effort Slack confirm
    slack_url = os.environ.get(cfg.slack.incoming_webhook_url_env, "")
    if slack_url:
        try:
            _post(slack_url, {"text": f"Approved + sent for contact {contact.get('email', '—')} at {time.strftime('%H:%M UTC')}"})
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
