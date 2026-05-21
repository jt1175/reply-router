"""Smartlead non-reply webhook handlers — OPEN, CLICK, BOUNCE, UNSUBSCRIBE.

These events surface as separate Smartlead webhook firings (different from EMAIL_REPLY)
and update GHL custom fields for the corresponding contact. Reconciler Phase 4 acts as
backstop for missed deliveries (Smartlead's circuit breaker can pause webhook delivery
after 4 consecutive 5xx — see reference_smartlead_webhook_shape memory).

Defensive design: Smartlead's exact payload shape for non-REPLY events is not
empirically verified (only EMAIL_REPLY is documented). Each handler tolerates field
absence by falling back to sensible defaults and logs unknown shapes for later inspection.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from reply_router.config import ClientConfig
from reply_router.ghl_client import GHLClient

logger = logging.getLogger(__name__)


# Known Smartlead event type values. Webhook payloads may use `event_type`, `type`,
# or be inferred from category. The dispatcher accepts any of these.
EVENT_REPLY = "EMAIL_REPLY"
EVENT_OPEN = "EMAIL_OPEN"
EVENT_CLICK = "LINK_CLICKED"
EVENT_BOUNCE = "EMAIL_BOUNCED"
EVENT_UNSUBSCRIBE = "EMAIL_UNSUBSCRIBED"

BOUNCE_RISK_THRESHOLD = 2  # 2+ bounces → add bounce_risk tag + DNC


def detect_event_type(payload: dict) -> str:
    """Best-effort detection of Smartlead event type from the webhook payload.

    Priority: explicit `event_type` field → `type` → presence of `reply_message`
    (REPLY signal). Falls back to "UNKNOWN" if nothing matches — caller should log
    and 200 the response to avoid tripping Smartlead's retry circuit.
    """
    if not isinstance(payload, dict):
        return "UNKNOWN"
    et = payload.get("event_type") or payload.get("type") or payload.get("event")
    if isinstance(et, str):
        normalized = et.upper().replace(" ", "_")
        # Map common variants to canonical
        if "REPLY" in normalized:
            return EVENT_REPLY
        if "OPEN" in normalized:
            return EVENT_OPEN
        if "CLICK" in normalized:
            return EVENT_CLICK
        if "BOUNC" in normalized:
            return EVENT_BOUNCE
        if "UNSUB" in normalized:
            return EVENT_UNSUBSCRIBE
        return normalized
    # No explicit event_type field — infer from payload shape
    if payload.get("reply_message"):
        return EVENT_REPLY
    return "UNKNOWN"


def _extract_lead_email(payload: dict) -> str:
    """Pull the lead's email from a Smartlead event payload.

    Per memory: REPLY uses `to_email` (NOT `lead_email` despite docs implying).
    For other events the field may be `to_email`, `lead_email`, or `email`.
    Tolerate any shape.
    """
    for key in ("to_email", "lead_email", "email"):
        v = payload.get(key)
        if isinstance(v, str) and "@" in v:
            return v.strip().lower()
    # Smartlead also nests within lead/{email} sometimes
    lead = payload.get("lead") or {}
    if isinstance(lead, dict):
        v = lead.get("email")
        if isinstance(v, str):
            return v.strip().lower()
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_field_value(contact: dict, field_id: str) -> str:
    """Read the current string value of a custom field on a contact, or '' if absent."""
    if not contact:
        return ""
    for cf in contact.get("customFields") or []:
        if cf.get("id") == field_id:
            v = cf.get("value")
            return str(v) if v is not None else ""
    return ""


def _increment(contact: dict, field_id: str) -> int:
    """Read current value as int (default 0), return value + 1."""
    raw = _get_field_value(contact, field_id)
    try:
        current = int(float(raw))
    except (ValueError, TypeError):
        current = 0
    return current + 1


def _resolve_contact_by_email(ghl: GHLClient, email: str) -> dict | None:
    """Look up a GHL contact by email — returns first match or None."""
    if not email:
        return None
    matches = ghl.get_contacts_by_email(email)
    if not matches:
        return None
    return matches[0]


def handle_open(payload: dict, ghl: GHLClient, client_config: ClientConfig) -> dict:
    email = _extract_lead_email(payload)
    contact = _resolve_contact_by_email(ghl, email)
    if not contact:
        return {"status": "ignored", "reason": "contact not found", "email": email}
    fids = client_config.ghl.custom_field_ids
    new_count = _increment(contact, fids["email_open_count"])
    ghl.update_contact(contact["id"], custom_fields={
        fids["email_open_count"]: str(new_count),
        fids["last_open_at"]: _now_iso(),
    })
    return {"status": "processed", "event": "open", "contact_id": contact["id"],
            "open_count": new_count}


def handle_click(payload: dict, ghl: GHLClient, client_config: ClientConfig) -> dict:
    email = _extract_lead_email(payload)
    contact = _resolve_contact_by_email(ghl, email)
    if not contact:
        return {"status": "ignored", "reason": "contact not found", "email": email}
    fids = client_config.ghl.custom_field_ids
    new_count = _increment(contact, fids["email_click_count"])
    ghl.update_contact(contact["id"], custom_fields={
        fids["email_click_count"]: str(new_count),
        fids["last_click_at"]: _now_iso(),
    })
    return {"status": "processed", "event": "click", "contact_id": contact["id"],
            "click_count": new_count}


def handle_bounce(payload: dict, ghl: GHLClient, client_config: ClientConfig) -> dict:
    """Increment bounce counter. After threshold, tag + DNC (treat as permanent bad address)."""
    email = _extract_lead_email(payload)
    contact = _resolve_contact_by_email(ghl, email)
    if not contact:
        return {"status": "ignored", "reason": "contact not found", "email": email}
    fids = client_config.ghl.custom_field_ids
    new_count = _increment(contact, fids["email_bounce_count"])
    ghl.update_contact(contact["id"], custom_fields={
        fids["email_bounce_count"]: str(new_count),
    })
    extra: dict[str, Any] = {}
    if new_count >= BOUNCE_RISK_THRESHOLD:
        try:
            ghl.add_tags(contact["id"], ["bounce_risk"])
            ghl.add_to_dnc(contact["id"])
            extra = {"tagged": True, "dnc": True}
        except Exception as exc:
            logger.exception("bounce handler tagging/DNC failed: %s", exc)
            extra = {"tagging_failed": str(exc)}
    return {"status": "processed", "event": "bounce", "contact_id": contact["id"],
            "bounce_count": new_count, **extra}


def handle_unsubscribe(payload: dict, ghl: GHLClient, client_config: ClientConfig) -> dict:
    """Honor unsubscribe immediately: DNC + timestamp. CAN-SPAM compliance."""
    email = _extract_lead_email(payload)
    contact = _resolve_contact_by_email(ghl, email)
    if not contact:
        return {"status": "ignored", "reason": "contact not found", "email": email}
    fids = client_config.ghl.custom_field_ids
    try:
        ghl.add_to_dnc(contact["id"])
    except Exception as exc:
        logger.exception("unsubscribe DNC failed: %s", exc)
        return {"status": "deferred", "reason": f"DNC failed: {exc}",
                "contact_id": contact["id"]}
    ghl.update_contact(contact["id"], custom_fields={
        fids["unsubscribed_at"]: _now_iso(),
    })
    ghl.add_tags(contact["id"], ["unsubscribed"])
    return {"status": "processed", "event": "unsubscribe", "contact_id": contact["id"]}


# Dispatcher
HANDLERS = {
    EVENT_OPEN: handle_open,
    EVENT_CLICK: handle_click,
    EVENT_BOUNCE: handle_bounce,
    EVENT_UNSUBSCRIBE: handle_unsubscribe,
}


def handle_non_reply_event(
    event_type: str, payload: dict, ghl: GHLClient, client_config: ClientConfig
) -> dict:
    """Dispatch to the right handler. Returns {status, ...} for the endpoint to JSONResponse."""
    handler = HANDLERS.get(event_type)
    if handler is None:
        logger.warning("non-reply event: no handler for %r; payload keys=%s",
                       event_type, list(payload.keys()) if isinstance(payload, dict) else type(payload))
        return {"status": "ignored", "reason": f"no handler for event_type={event_type}"}
    return handler(payload, ghl, client_config)
