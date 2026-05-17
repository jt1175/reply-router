"""Nightly reconciler — 3-phase recovery for stuck soft locks, missed replies, and expired tokens.

Extracted from api/reconcile.py so the FastAPI route layer can stay thin.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from reply_router.dedupe import SOFT_LOCK_TTL_SEC
from reply_router.ghl_client import GHLClient
from reply_router.orchestrator import ReplyPayload, process_reply
from reply_router.smartlead_client import SmartleadClient

logger = logging.getLogger("reply_router.reconciler")


def reconcile_client(client_config) -> dict:
    ghl = GHLClient(
        api_key=os.environ[client_config.ghl.api_key_env],
        sub_account_id=client_config.ghl.sub_account_id,
        campaign_ids=client_config.smartlead.campaign_ids,
    )
    smartlead = SmartleadClient(api_key=os.environ[client_config.smartlead.api_key_env])
    slack_url = os.environ.get(client_config.slack.incoming_webhook_url_env, "")
    fids = client_config.ghl.custom_field_ids

    p1 = phase1_clear_stuck_soft_locks(
        ghl,
        soft_lock_field_id=fids["currently_processing_smartlead_message_id"],
        slack_url=slack_url,
    )
    p2 = phase2_smartlead_vs_ghl(smartlead, client_config)
    p3 = phase3_expire_old_tokens(
        ghl,
        token_field_id=fids["pending_draft_token"],
        text_field_id=fids["pending_draft_text"],
        created_at_field_id=fids["pending_draft_created_at"],
        reply_message_id_field_id=fids["pending_reply_message_id"],
        reply_email_stats_id_field_id=fids["pending_reply_email_stats_id"],
    )

    summary = {
        "client_id": client_config.client_id,
        "phase_1": {"stuck_locks_recovered": p1},
        "phase_2": p2,
        "phase_3": {"tokens_expired": p3},
    }
    logger.info("reconciler summary: %s", summary)
    return summary


def phase1_clear_stuck_soft_locks(ghl, soft_lock_field_id: str, slack_url: str) -> int:
    """Clear soft locks > SOFT_LOCK_TTL_SEC old. Returns count cleared."""
    contacts = ghl.list_contacts_with_field(soft_lock_field_id)
    cleared = 0
    for c in contacts:
        for cf in c.get("customFields", []):
            if cf.get("id") != soft_lock_field_id or not cf.get("value"):
                continue
            try:
                _, ts_str = cf["value"].rsplit(":", 1)
                age = time.time() - int(ts_str)
            except (ValueError, AttributeError):
                continue
            if age <= SOFT_LOCK_TTL_SEC:
                continue
            logger.info("clearing stuck soft lock on contact=%s age=%ds", c["id"], age)
            ghl.update_contact(c["id"], custom_fields={soft_lock_field_id: ""})
            cleared += 1
    return cleared


def phase2_smartlead_vs_ghl(smartlead, client_config) -> dict:
    """Iterate Smartlead replies in the last 36h, call orchestrator.process_reply for each.
    The orchestrator's dedupe layer (check_rolling) skips ones already processed."""
    since = datetime.now(timezone.utc) - timedelta(hours=36)
    seen = processed = skipped = 0
    errors = []
    try:
        replies = smartlead.list_replies(client_config.smartlead.campaign_ids, since)
    except RuntimeError as exc:
        # Smartlead.list_replies raises when _LIST_REPLIES_ENDPOINT_VERIFIED is False
        # (until JT confirms the URL — see Task 2.4 step 2). Surface as a single error.
        logger.warning("phase2 skipped: %s", exc)
        return {"replies_seen": 0, "processed": 0, "skipped": 0,
                "errors": [{"message_id": "<phase2_skipped>", "reason": str(exc)}]}
    for r in replies:
        seen += 1
        payload = ReplyPayload(
            message_id=r.get("message_id", ""),
            from_email=r.get("from_email", ""),
            lead_email=r.get("lead_email", ""),
            campaign_id=r.get("campaign_id", client_config.smartlead.campaign_ids[0]),
            reply_text=r.get("reply_text") or r.get("body", ""),
            email_stats_id=r.get("email_stats_id", ""),
            original_subject=r.get("subject", ""),
        )
        try:
            result = process_reply(client_config, payload, source="reconciler")
            if result.status == "duplicate":
                skipped += 1
            elif result.status == "processed":
                processed += 1
            elif result.status in ("ignored_self", "in_flight_elsewhere"):
                skipped += 1
            elif result.status == "deferred_for_retry":
                errors.append({"message_id": payload.message_id, "reason": "deferred"})
        except Exception as exc:
            logger.exception("reconcile phase2 error mid=%s", payload.message_id)
            errors.append({"message_id": payload.message_id, "reason": str(exc)})
    return {"replies_seen": seen, "processed": processed, "skipped": skipped, "errors": errors}


def phase3_expire_old_tokens(
    ghl,
    token_field_id: str,
    text_field_id: str,
    created_at_field_id: str,
    reply_message_id_field_id: str,
    reply_email_stats_id_field_id: str,
) -> int:
    """Clear all 5 pending_* fields on contacts where created_at + 7d < now.

    Clears the 2 threading-param fields alongside the original 3 — they were added to
    the schema to fix reviewer iteration 2 blocker #1.
    """
    contacts = ghl.list_contacts_with_field(created_at_field_id)
    cleared = 0
    threshold = datetime.now(timezone.utc) - timedelta(days=7)
    for c in contacts:
        for cf in c.get("customFields", []):
            if cf.get("id") != created_at_field_id or not cf.get("value"):
                continue
            try:
                created = datetime.fromisoformat(cf["value"])
            except ValueError:
                continue
            if created > threshold:
                continue
            ghl.update_contact(
                c["id"],
                custom_fields={
                    token_field_id: "",
                    text_field_id: "",
                    created_at_field_id: "",
                    reply_message_id_field_id: "",
                    reply_email_stats_id_field_id: "",
                },
            )
            cleared += 1
    return cleared
