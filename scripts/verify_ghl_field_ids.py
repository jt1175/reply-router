"""Verify every custom_field_id in a client config exists in GHL by write→read round-trip.

Usage: python3 scripts/verify_ghl_field_ids.py clients/clear_facility.json
Exit code 0 if all fields verified; 1 if any failed (with which one).

This is the Task 5.2 gate that catches the "wrong field ID populates a 200 but writes
nothing" failure mode — GHL silently drops writes to nonexistent field IDs. Plus
GHL validates by field type at write time (DATE rejects non-dates; SINGLE_OPTIONS may
reject values not in the option list), so the script fetches field metadata first and
sends type-appropriate sentinels.

The rolling-list field gets a ~2000-char sentinel to catch the LARGE_TEXT-vs-TEXT
mismatch case (TEXT truncates at 256 chars; spec §8.1 requires LARGE_TEXT for
this field). Wrong type = silently lost rolling-dedupe entries = duplicate replies sent.

Fetches contact by ID after each write (not by email search) — GHL's search index is
eventually-consistent on freshly-created contacts, so search readbacks flake.
"""
from __future__ import annotations

import os
import sys
import uuid

import requests
from dotenv import load_dotenv

from reply_router.config import load_client_config
from reply_router.ghl_client import GHL_API_VERSION, GHL_BASE_URL, GHLClient


def _fetch_field_metadata(api_key: str, location_id: str) -> dict[str, dict]:
    """Return {field_id: {dataType, picklistOptions}} for every custom field in the location."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": GHL_API_VERSION,
        "Accept": "application/json",
    }
    url = f"{GHL_BASE_URL}/locations/{location_id}/customFields"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return {f["id"]: f for f in resp.json().get("customFields", [])}


def _fetch_contact_by_id(api_key: str, contact_id: str) -> dict:
    """Direct GET (bypasses flaky search index for freshly-created contacts)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": GHL_API_VERSION,
        "Accept": "application/json",
    }
    resp = requests.get(f"{GHL_BASE_URL}/contacts/{contact_id}", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json().get("contact", {})


# Map GHL dataType → (sentinel-generator, comparator)
LONG_FIELD = "last_processed_smartlead_message_ids"


def _make_sentinel(field_name: str, field_meta: dict) -> tuple[str, str]:
    """Return (sentinel_value_to_write, expected_readback_substring)."""
    dtype = field_meta.get("dataType", "TEXT")
    if dtype == "DATE":
        # GHL accepts ISO 8601. Readback may come as epoch ms or ISO — we'll match by date part.
        # Use a unique date with millisecond precision to avoid collisions across runs.
        return "2031-06-15T12:00:00.000Z", "2031-06-15"
    if dtype == "SINGLE_OPTIONS":
        # Must be a value in the configured option list, else GHL rejects/strips silently.
        opts = field_meta.get("picklistOptions") or field_meta.get("options") or []
        if not opts:
            raise RuntimeError(f"SINGLE_OPTIONS field {field_name} has no configured options")
        chosen = opts[0]
        return chosen, chosen
    if dtype == "LARGE_TEXT" and field_name == LONG_FIELD:
        s = "verify-" + ("x" * 2000) + "-" + uuid.uuid4().hex[:8]
        return s, s
    # TEXT, LARGE_TEXT (non-rolling), and anything else: short string sentinel.
    s = f"verify-{uuid.uuid4().hex[:8]}"
    return s, s


def _readback_matches(written: str, expected_substring: str, readback) -> bool:
    """For DATE fields, expected_substring is just the date part; readback may be epoch ms or ISO.
    For others, exact equality on the full written value."""
    if readback is None:
        return False
    rb = str(readback)
    if written == expected_substring:
        # Exact-match types (TEXT, LARGE_TEXT, SINGLE_OPTIONS)
        return rb == written
    # DATE: substring match on the date part
    return expected_substring in rb


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

    print("Fetching field metadata from GHL...")
    field_meta = _fetch_field_metadata(api_key, cfg.ghl.sub_account_id)

    test_email = os.environ.get("GHL_VERIFY_TEST_EMAIL", "fieldverify@reply-router-test.invalid")
    contact, _ = ghl.resolve_contact_by_email(test_email)
    contact_id = contact["id"]
    print(f"Using test contact {contact_id} ({test_email})")

    failures = []
    for field_name, field_id in cfg.ghl.custom_field_ids.items():
        meta = field_meta.get(field_id)
        if not meta:
            failures.append((field_name, field_id, f"field id not found in GHL location"))
            continue
        try:
            sentinel, expected = _make_sentinel(field_name, meta)
        except RuntimeError as exc:
            failures.append((field_name, field_id, str(exc)))
            continue
        preview = sentinel[:40] + ("..." if len(sentinel) > 40 else "")
        print(f"  → {meta['dataType']:13} {field_name!r} (id={field_id}) = {preview} (len={len(sentinel)})")
        try:
            ghl.update_contact(contact_id, custom_fields={field_id: sentinel})
        except RuntimeError as exc:
            failures.append((field_name, field_id, f"write failed: {exc}"))
            continue
        # Direct fetch by ID — search index is eventually-consistent on fresh contacts.
        fetched = _fetch_contact_by_id(api_key, contact_id)
        readback = next(
            (cf.get("value") for cf in fetched.get("customFields", []) if cf.get("id") == field_id),
            None,
        )
        if not _readback_matches(sentinel, expected, readback):
            note = ""
            if field_name == LONG_FIELD and readback and len(str(readback)) < len(sentinel):
                note = "  ← truncation suggests wrong GHL field type (need LARGE_TEXT)"
            failures.append((
                field_name, field_id,
                f"readback mismatch: expected len={len(sentinel)} got {str(readback)[:80]!r} (len={len(str(readback)) if readback else 0}){note}",
            ))
        else:
            print(f"     ✓ verified {field_name}")

    # Clean up: clear all sentinels (DATE fields need null, not empty string)
    print("Cleaning up sentinels...")
    cleanup_payload = {}
    for fid in cfg.ghl.custom_field_ids.values():
        m = field_meta.get(fid, {})
        # DATE fields reject empty string — use a sentinel "1970-01-01" placeholder instead
        cleanup_payload[fid] = "1970-01-01T00:00:00.000Z" if m.get("dataType") == "DATE" else ""
    try:
        ghl.update_contact(contact_id, custom_fields=cleanup_payload)
    except RuntimeError as exc:
        print(f"  (cleanup warning: {exc})")

    if failures:
        print("\n❌ FAILED — these field IDs are wrong, missing, or read-only:")
        for name, fid, reason in failures:
            print(f"  {name} (id={fid}): {reason}")
        sys.exit(1)
    print(f"\n✅ All {len(cfg.ghl.custom_field_ids)} custom field IDs verified end-to-end.")


if __name__ == "__main__":
    main()
