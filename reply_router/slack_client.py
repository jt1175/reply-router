"""Slack notification formatter + POSTer.

Plain POST to incoming webhook URL. No Slack App OAuth (per spec §2 non-goals).
Notifications are best-effort: a Slack failure logs but never raises (spec §6.2
principle 4). The `actions` block with a plain URL button works on incoming
webhooks without app installation — see spec Appendix D note.

Format reference: spec Appendix D.1 (auto-send), D.2 (shadow), D.3 (URGENT).
"""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 5

# Visual prefix per classification (spec Appendix D shows 🟢 INTERESTED).
CLASSIFICATION_HEADER = {
    "interested":    ("🟢", "INTERESTED REPLY"),
    "not_now":       ("🟡", "NOT NOW REPLY"),
    "wrong_person":  ("🔵", "WRONG PERSON REPLY"),
    "unsubscribe":   ("⚪", "UNSUBSCRIBE"),
    "info_request":  ("🟣", "INFO REQUEST REPLY"),
    "objection":     ("🟠", "OBJECTION REPLY"),
    "unknown":       ("⚫", "UNKNOWN CLASSIFICATION"),
}


def _truncate(text: str, n: int = 500) -> str:
    if not text or len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


def _build_classification_blocks(
    *,
    classification: str,
    confidence: str,
    send_mode: str,
    account: dict[str, Any],
    reply_text: str,
    response_text: str,
    approval_url: str | None,
    monitoring: bool,
    ghl_contact_url: str | None,
) -> list[dict[str, Any]]:
    emoji, label = CLASSIFICATION_HEADER.get(classification, ("⚫", classification.upper()))
    header_suffix = " 🔍 MONITORING" if monitoring else ""
    header_text = f"{emoji} {label}{header_suffix}"

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Company:*\n{account.get('company_name','—')}"},
                {"type": "mrkdwn",
                 "text": f"*Contact:*\n{account.get('contact_name','—')} ({account.get('contact_title','—')})"},
                {"type": "mrkdwn",
                 "text": f"*Classification:*\n{classification} (confidence: {confidence})"},
                {"type": "mrkdwn",
                 "text": f"*Pipeline:*\nNew Reply → {account.get('pipeline_to','—')}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*Their reply:*\n> {_truncate(reply_text)}"},
        },
    ]

    if send_mode == "auto_send":
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*Our auto-response* (sent):\n> {_truncate(response_text)}"},
        })
    else:  # shadow_send
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*Proposed response* (NOT YET SENT — approval needed):\n> {_truncate(response_text)}"},
        })
        if approval_url:
            blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review & Send →"},
                    "url": approval_url,
                    "style": "primary",
                }],
            })

    if ghl_contact_url:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"<{ghl_contact_url}|GHL contact>"}],
        })
    return blocks


def post_classification_notification(
    webhook_url: str,
    *,
    classification: str,
    confidence: str,
    send_mode: str,
    account: dict[str, Any],
    reply_text: str,
    response_text: str,
    approval_url: str | None = None,
    monitoring: bool = False,
    ghl_contact_url: str | None = None,
) -> None:
    emoji, label = CLASSIFICATION_HEADER.get(classification, ("⚫", classification.upper()))
    monitor_suffix = " 🔍 MONITORING" if monitoring else ""
    payload = {
        "text": f"{emoji} {label} — {account.get('company_name','—')}{monitor_suffix}",
        "blocks": _build_classification_blocks(
            classification=classification, confidence=confidence, send_mode=send_mode,
            account=account, reply_text=reply_text, response_text=response_text,
            approval_url=approval_url, monitoring=monitoring,
            ghl_contact_url=ghl_contact_url,
        ),
    }
    _post(webhook_url, payload)


def post_urgent(
    webhook_url: str,
    *,
    title: str,
    action_required: str,
    reply_text: str | None = None,
    ghl_contact_url: str | None = None,
) -> None:
    blocks: list[dict[str, Any]] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"🚨 URGENT: {title}"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*Action required NOW:*\n{action_required}"}},
    ]
    if reply_text:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*Their reply (full text):*\n> {_truncate(reply_text, 2000)}"},
        })
    if ghl_contact_url:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<{ghl_contact_url}|GHL contact>"}],
        })
    payload = {"text": f"🚨 URGENT: {title}", "blocks": blocks}
    _post(webhook_url, payload)


def _post(webhook_url: str, payload: dict[str, Any]) -> None:
    """Best-effort POST. Retries once on 5xx. Never raises (logs and continues)."""
    logger.info(
        "slack post: url_prefix=%s url_len=%d block_count=%d",
        webhook_url[:50], len(webhook_url), len(payload.get("blocks", [])),
    )
    for attempt in (1, 2):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=DEFAULT_TIMEOUT_SEC)
        except (requests.RequestException, ConnectionError) as exc:
            logger.error("slack post failed attempt=%d err=%s", attempt, exc)
            if attempt == 2:
                return
            continue
        if 200 <= resp.status_code < 300:
            return
        logger.error(
            "slack post non-2xx attempt=%d status=%d body=%s payload=%s",
            attempt, resp.status_code, resp.text[:200],
            __import__("json").dumps(payload)[:1500],
        )
        if attempt == 2 or resp.status_code < 500:
            # Don't retry on 4xx (bad URL, malformed payload — retry won't help)
            return
