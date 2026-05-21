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
    p4 = phase4_metrics_sync(smartlead, ghl, client_config)

    summary = {
        "client_id": client_config.client_id,
        "phase_1": {"stuck_locks_recovered": p1},
        "phase_2": p2,
        "phase_3": {"tokens_expired": p3},
        "phase_4": p4,
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


def phase4_metrics_sync(smartlead, ghl, client_config) -> dict:
    """Backstop: pull per-lead stats from Smartlead and push deltas to GHL custom fields.

    Webhook delivery is the primary path for OPEN/CLICK/BOUNCE/UNSUBSCRIBE events,
    but Smartlead's circuit breaker pauses delivery after 4 consecutive 5xx responses
    (per reference_smartlead_webhook_shape memory). This phase catches any missed
    events on the next cron tick.

    For each lead in each campaign's stats:
      - Look up GHL contact by email
      - Compare Smartlead counts vs GHL custom field counts
      - If Smartlead > GHL: update GHL to match (idempotent, never decrement)
      - If Smartlead is_unsubscribed: ensure GHL contact is in DNC

    Skips campaigns whose IDs start with TBD_ (placeholder configs).
    Skips leads with zero engagement counters (no work to do).
    """
    fids = client_config.ghl.custom_field_ids

    # Phase 4 is only meaningful when the metrics custom fields are configured.
    required_fids = ("email_open_count", "email_click_count", "email_bounce_count",
                     "last_open_at", "last_click_at", "unsubscribed_at")
    missing = [k for k in required_fids if k not in fids]
    if missing:
        return {"status": "skipped", "reason": f"missing custom_field_ids: {missing}"}

    summary = {
        "campaigns_processed": 0,
        "leads_seen": 0,
        "leads_skipped_zero_stats": 0,
        "contacts_not_found_in_ghl": 0,
        "opens_synced": 0,
        "clicks_synced": 0,
        "bounces_synced": 0,
        "unsubscribes_synced": 0,
        "errors": [],
    }

    for cid in client_config.smartlead.campaign_ids:
        if cid.startswith("TBD_"):
            continue
        summary["campaigns_processed"] += 1
        offset = 0
        while True:
            try:
                page = smartlead.get_campaign_statistics(cid, limit=100, offset=offset)
            except Exception as exc:
                summary["errors"].append({"campaign": cid, "offset": offset, "error": str(exc)})
                logger.exception("phase4 stats fetch failed cid=%s offset=%d", cid, offset)
                break
            leads = page.get("data") or []
            if not leads:
                break
            for lead in leads:
                summary["leads_seen"] += 1
                # Skip zero-engagement leads — no GHL work needed
                opens = int(lead.get("open_count") or 0)
                clicks = int(lead.get("click_count") or 0)
                is_unsub = bool(lead.get("is_unsubscribed"))
                is_bounced = bool(lead.get("is_bounced"))
                if opens == 0 and clicks == 0 and not is_unsub and not is_bounced:
                    summary["leads_skipped_zero_stats"] += 1
                    continue

                email = (lead.get("lead_email") or "").strip().lower()
                if not email:
                    continue
                try:
                    matches = ghl.get_contacts_by_email(email)
                except Exception as exc:
                    summary["errors"].append({"email": email, "error": str(exc)})
                    continue
                if not matches:
                    summary["contacts_not_found_in_ghl"] += 1
                    continue
                contact = matches[0]
                contact_id = contact["id"]
                # Read current GHL values
                by_id = {cf["id"]: cf.get("value", "") for cf in contact.get("customFields") or []}
                current_opens = _safe_int(by_id.get(fids["email_open_count"], "0"))
                current_clicks = _safe_int(by_id.get(fids["email_click_count"], "0"))
                current_bounces = _safe_int(by_id.get(fids["email_bounce_count"], "0"))

                writes: dict[str, str] = {}
                if opens > current_opens:
                    writes[fids["email_open_count"]] = str(opens)
                    if lead.get("open_time"):
                        writes[fids["last_open_at"]] = str(lead["open_time"])
                    summary["opens_synced"] += 1
                if clicks > current_clicks:
                    writes[fids["email_click_count"]] = str(clicks)
                    if lead.get("click_time"):
                        writes[fids["last_click_at"]] = str(lead["click_time"])
                    summary["clicks_synced"] += 1
                if is_bounced and current_bounces == 0:
                    # Treat is_bounced=True as count=1 baseline; webhook may have set higher
                    writes[fids["email_bounce_count"]] = "1"
                    summary["bounces_synced"] += 1

                if writes:
                    try:
                        ghl.update_contact(contact_id, custom_fields=writes)
                    except Exception as exc:
                        summary["errors"].append({"contact_id": contact_id, "error": str(exc)})
                        continue

                if is_unsub and not by_id.get(fids["unsubscribed_at"]):
                    try:
                        ghl.add_to_dnc(contact_id)
                        ghl.update_contact(contact_id, custom_fields={
                            fids["unsubscribed_at"]: datetime.now(timezone.utc).isoformat(),
                        })
                        summary["unsubscribes_synced"] += 1
                    except Exception as exc:
                        summary["errors"].append({"contact_id": contact_id, "unsubscribe_error": str(exc)})

            # Pagination
            total = page.get("total_stats") or page.get("total") or len(leads)
            offset += len(leads)
            if offset >= total or not leads:
                break

    return summary


def _safe_int(value: str | int | None) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


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
