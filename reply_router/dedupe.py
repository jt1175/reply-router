"""Dedupe state — soft lock + rolling list of last 50 hashed message_ids.

All state lives in GHL custom fields per spec §3.2. This module owns the
4-method API and is the only place that knows about hashing or list-truncation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from enum import Enum

from reply_router.ghl_client import GHLClient

logger = logging.getLogger(__name__)

ROLLING_LIST_MAX = 50
SOFT_LOCK_TTL_SEC = 600  # 10 minutes


class SoftLockState(Enum):
    ABSENT = "absent"
    IN_FLIGHT = "in_flight"      # fresh, same message_id, < TTL
    STALE = "stale"              # > TTL


def hash16(message_id: str) -> str:
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]


def _get_field(contact: dict, field_id: str) -> str | None:
    """Read a custom field value from a GHL contact dict."""
    for cf in contact.get("customFields") or []:
        if cf.get("id") == field_id:
            return cf.get("value")
    return None


def check_rolling(contact: dict, rolling_field_id: str, message_id: str) -> bool:
    """Return True if message_id (hashed) is already in the rolling list → duplicate."""
    raw = _get_field(contact, rolling_field_id)
    if not raw:
        return False
    try:
        ids = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("rolling list field unparseable: %r", raw)
        return False
    return hash16(message_id) in ids


def check_soft_lock(
    contact: dict, soft_lock_field_id: str, message_id: str
) -> SoftLockState:
    raw = _get_field(contact, soft_lock_field_id)
    if not raw:
        return SoftLockState.ABSENT
    try:
        locked_mid, locked_ts_str = raw.rsplit(":", 1)
        locked_ts = int(locked_ts_str)
    except (ValueError, AttributeError):
        return SoftLockState.ABSENT
    age = time.time() - locked_ts
    if age > SOFT_LOCK_TTL_SEC:
        return SoftLockState.STALE
    if locked_mid == message_id:
        return SoftLockState.IN_FLIGHT
    # Different message_id is currently being processed — treat as absent for our purposes
    # (a different message can proceed; the soft lock is per-message)
    return SoftLockState.ABSENT


def acquire_soft_lock(
    client: GHLClient,
    contact_id: str,
    soft_lock_field_id: str,
    message_id: str,
) -> None:
    value = f"{message_id}:{int(time.time())}"
    client.update_contact(contact_id, custom_fields={soft_lock_field_id: value})


def mark_complete(
    client: GHLClient,
    contact: dict,
    rolling_field_id: str,
    soft_lock_field_id: str,
    message_id: str,
) -> None:
    """Append hash16(message_id) to rolling list (truncate to 50) AND clear soft lock,
    in a single GHL updateContact PATCH per spec §6.1 atomicity note."""
    raw = _get_field(contact, rolling_field_id)
    try:
        ids = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        ids = []
    h = hash16(message_id)
    if h not in ids:
        ids.append(h)
        if len(ids) > ROLLING_LIST_MAX:
            ids = ids[-ROLLING_LIST_MAX:]
    contact_id = contact["id"]
    client.update_contact(
        contact_id,
        custom_fields={
            rolling_field_id: json.dumps(ids),
            soft_lock_field_id: "",
        },
    )
