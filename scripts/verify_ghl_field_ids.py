"""Verify every custom_field_id in a client config exists in GHL by write→read round-trip.

Usage: python3 scripts/verify_ghl_field_ids.py clients/clear_facility.json
Exit code 0 if all fields verified; 1 if any failed (with which one).

This is the Task 5.2 gate that catches the "wrong field ID populates a 200 but writes
nothing" failure mode — GHL silently drops writes to nonexistent field IDs, returning
200 OK. A wrong ID is invisible until the data isn't there, so verify end-to-end.

The rolling-list field gets a ~2000-char sentinel to catch the field-type mismatch
case (GHL "standard text" truncates at 256 chars; spec §8.1 requires "large text" for
this field). Wrong type = silently lost rolling-dedupe entries = duplicate replies sent.
"""
from __future__ import annotations

import os
import sys
import time
import uuid

from dotenv import load_dotenv

from reply_router.config import load_client_config
from reply_router.ghl_client import GHLClient


def main():
    load_dotenv()
    if len(sys.argv) != 2:
        print(f"usage: python3 {sys.argv[0]} <client_config.json>", file=sys.stderr)
        sys.exit(2)
    cfg = load_client_config(sys.argv[1])
    api_key = os.environ[cfg.ghl.api_key_env]
    ghl = GHLClient(
        api_key=api_key,
        sub_account_id=cfg.ghl.sub_account_id,
        campaign_ids=cfg.smartlead.campaign_ids,
    )

    # Use a dedicated verification contact (create one if not present)
    test_email = os.environ.get("GHL_VERIFY_TEST_EMAIL", "fieldverify@reply-router-test.invalid")
    contact, _ = ghl.resolve_contact_by_email(test_email)
    contact_id = contact["id"]
    print(f"Using test contact {contact_id} ({test_email})")

    # The rolling-list field must be GHL field type "large text" (5000+ char capacity).
    # Per spec §8.1: "GHL field type MUST be 'large text'." Verify by writing a
    # ~2000-char sentinel and confirming it reads back unchanged. A "standard text"
    # field will truncate at 256 chars and the rolling list will silently lose entries.
    LONG_FIELD = "last_processed_smartlead_message_ids"

    failures = []
    for field_name, field_id in cfg.ghl.custom_field_ids.items():
        if field_name == LONG_FIELD:
            sentinel = "verify-" + ("x" * 2000) + "-" + uuid.uuid4().hex[:8]
        else:
            sentinel = f"verify-{uuid.uuid4().hex[:8]}"
        preview = sentinel[:40] + ("..." if len(sentinel) > 40 else "")
        print(f"  → writing {field_name!r} (id={field_id}) = {preview} (len={len(sentinel)})")
        try:
            ghl.update_contact(contact_id, custom_fields={field_id: sentinel})
        except RuntimeError as exc:
            failures.append((field_name, field_id, f"write failed: {exc}"))
            continue
        time.sleep(0.5)  # GHL eventual consistency
        # Re-fetch and check
        contacts = ghl.get_contacts_by_email(test_email)
        if not contacts:
            failures.append((field_name, field_id, "contact disappeared after write"))
            continue
        readback = next(
            (cf.get("value") for cf in contacts[0].get("customFields", []) if cf.get("id") == field_id),
            None,
        )
        if readback != sentinel:
            failures.append((
                field_name, field_id,
                f"readback mismatch: expected len={len(sentinel)}, got {readback!r}"
                + ("  ← truncation suggests wrong GHL field type (need 'large text')" if field_name == LONG_FIELD else ""),
            ))
        else:
            print(f"     ✓ verified {field_name}")

    # Clean up: clear all the sentinels
    print("Cleaning up sentinels...")
    ghl.update_contact(
        contact_id,
        custom_fields={fid: "" for fid in cfg.ghl.custom_field_ids.values()},
    )

    if failures:
        print("\n❌ FAILED — these field IDs are wrong, missing, or read-only:")
        for name, fid, reason in failures:
            print(f"  {name} (id={fid}): {reason}")
        sys.exit(1)
    print(f"\n✅ All {len(cfg.ghl.custom_field_ids)} custom field IDs verified end-to-end.")


if __name__ == "__main__":
    main()
