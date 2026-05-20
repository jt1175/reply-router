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
from reply_router.ghl_client import GHLClient
from reply_router.orchestrator import ReplyPayload, process_reply
from reply_router.reconciler import reconcile_client
from reply_router.slack_client import _post as _slack_post
from reply_router.smartlead_client import SmartleadClient, SmartleadError

app = FastAPI(title="reply-router", version="0.1.0")
logger = logging.getLogger("api.index")


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
    client_config = _load_client(client_id)
    _check_router_secret(client_config, x_router_secret or secret)
    payload = await request.json()
    rp = ReplyPayload.from_smartlead_webhook(payload)
    result = process_reply(client_config, rp, source="webhook")
    return JSONResponse(content=result.to_response(), status_code=result.http_status)


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
