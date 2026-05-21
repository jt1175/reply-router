"""Provision the qualification booking flow in GHL via API.

Reads current state, creates only what's missing, patches clear_facility.json
with the resolved IDs. Idempotent — safe to re-run.

What it does:
  1. List GHL calendars → find "Discovery Call" → capture calendar_id
  2. List existing custom fields → create the 3 qualification_* fields if absent
  3. List opportunities/pipelines/stages → suggest which existing stages to
     reuse for qualify/gray/reject (does NOT create new stages)
  4. Patch clients/clear_facility.json with resolved IDs

Run from repo root:
    source venv/bin/activate
    python scripts/provision_qualification_setup.py

Requires CFS_GHL_API_KEY in env (already loaded from .env via python-dotenv).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = REPO_ROOT / "clients" / "clear_facility.json"

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"

CALENDAR_NAME = "Discovery Call"
FIELDS_TO_CREATE = [
    # Qualification flow (Step 1)
    {"key": "qualification_form_answers", "name": "Qualification Form Answers", "dataType": "LARGE_TEXT"},
    {"key": "qualification_result", "name": "Qualification Result", "dataType": "SINGLE_OPTIONS",
     "options": ["qualify", "gray_zone", "reject"]},
    {"key": "qualification_submitted_at", "name": "Qualification Submitted At", "dataType": "DATE"},
    # Smartlead → GHL metrics sync (Step 3b)
    {"key": "email_open_count", "name": "Email Open Count", "dataType": "NUMERICAL"},
    {"key": "email_click_count", "name": "Email Click Count", "dataType": "NUMERICAL"},
    {"key": "email_bounce_count", "name": "Email Bounce Count", "dataType": "NUMERICAL"},
    {"key": "last_open_at", "name": "Last Email Open At", "dataType": "DATE"},
    {"key": "last_click_at", "name": "Last Email Click At", "dataType": "DATE"},
    {"key": "unsubscribed_at", "name": "Unsubscribed At", "dataType": "DATE"},
]


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": GHL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def list_calendars(api_key: str, location_id: str) -> list[dict]:
    """GET /calendars/?locationId=... — list all calendars in a location."""
    url = f"{GHL_BASE}/calendars/"
    resp = requests.get(url, headers=_headers(api_key), params={"locationId": location_id}, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"list_calendars failed: {resp.status_code} {resp.text[:200]}")
    return resp.json().get("calendars", [])


def list_custom_fields(api_key: str, location_id: str) -> list[dict]:
    """GET /locations/{loc}/customFields — list existing custom fields."""
    url = f"{GHL_BASE}/locations/{location_id}/customFields"
    resp = requests.get(url, headers=_headers(api_key), timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"list_custom_fields failed: {resp.status_code} {resp.text[:200]}")
    return resp.json().get("customFields", [])


def create_custom_field(api_key: str, location_id: str, field_spec: dict) -> dict:
    """POST /locations/{loc}/customFields — create a new custom field on the Contact model."""
    url = f"{GHL_BASE}/locations/{location_id}/customFields"
    body = {
        "name": field_spec["name"],
        "dataType": field_spec["dataType"],
        "model": "contact",
    }
    if "options" in field_spec:
        body["options"] = field_spec["options"]
    resp = requests.post(url, headers=_headers(api_key), json=body, timeout=15)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"create_custom_field failed for {field_spec['name']}: {resp.status_code} {resp.text[:300]}")
    return resp.json().get("customField") or resp.json()


def list_pipelines(api_key: str, location_id: str) -> list[dict]:
    """GET /opportunities/pipelines?locationId=... — returns pipelines with stages."""
    url = f"{GHL_BASE}/opportunities/pipelines"
    resp = requests.get(url, headers=_headers(api_key), params={"locationId": location_id}, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"list_pipelines failed: {resp.status_code} {resp.text[:200]}")
    return resp.json().get("pipelines", [])


def main():
    api_key = os.environ.get("CFS_GHL_API_KEY")
    if not api_key:
        sys.exit("ERROR: CFS_GHL_API_KEY not set in environment")
    cfg = json.loads(CFG_PATH.read_text())
    location_id = cfg["ghl"]["sub_account_id"]
    pipeline_id = cfg["ghl"]["pipeline_id"]

    print(f"\n=== READING GHL STATE for location {location_id} ===\n")

    # 1. Calendars
    print("[1/4] Listing calendars...")
    calendars = list_calendars(api_key, location_id)
    print(f"  Found {len(calendars)} calendar(s):")
    for cal in calendars:
        print(f"    - {cal.get('name', '?'):30s}  id={cal.get('id', '?')}")
    discovery = next((c for c in calendars if c.get("name") == CALENDAR_NAME), None)
    if not discovery:
        print(f"\n  WARNING: No calendar named '{CALENDAR_NAME}' found. JT must create it in GHL UI first.")
        calendar_id = None
    else:
        calendar_id = discovery["id"]
        print(f"\n  ✓ '{CALENDAR_NAME}' calendar_id: {calendar_id}")

    # 2. Custom fields — existing state
    print("\n[2/4] Listing existing custom fields...")
    existing_fields = list_custom_fields(api_key, location_id)
    by_name = {f.get("name", ""): f for f in existing_fields}
    print(f"  Found {len(existing_fields)} existing custom field(s).")
    resolved_field_ids: dict[str, str] = {}
    for spec in FIELDS_TO_CREATE:
        match = by_name.get(spec["name"])
        if match:
            resolved_field_ids[spec["key"]] = match["id"]
            print(f"  ✓ '{spec['name']}' already exists: {match['id']}")
        else:
            print(f"  ✗ '{spec['name']}' does NOT exist — would create (dataType={spec['dataType']})")
            resolved_field_ids[spec["key"]] = None

    # 3. Pipelines / stages
    print("\n[3/4] Listing pipelines/stages...")
    pipelines = list_pipelines(api_key, location_id)
    target_pipeline = next((p for p in pipelines if p.get("id") == pipeline_id), None)
    if not target_pipeline:
        print(f"  WARNING: configured pipeline_id={pipeline_id} not found in GHL.")
    else:
        print(f"  Pipeline '{target_pipeline.get('name', '?')}' has {len(target_pipeline.get('stages', []))} stage(s):")
        for st in target_pipeline.get("stages", []):
            print(f"    - {st.get('name', '?'):30s}  id={st.get('id', '?')}")

    # 4. Plan: what would be created
    needs_create = [s for s in FIELDS_TO_CREATE if resolved_field_ids.get(s["key"]) is None]
    print(f"\n=== PLAN ===")
    print(f"  Custom fields to CREATE: {len(needs_create)}")
    for spec in needs_create:
        print(f"    + {spec['name']} ({spec['dataType']})")
    if calendar_id:
        print(f"  Calendar ID to wire in: {calendar_id}")
    print(f"  Pipeline stages: REUSING existing stages (no creation needed).")
    print(f"    Suggested mappings (review and edit clear_facility.json after this script):")
    print(f"      qualify_pipeline_stage_id   — pick a stage like 'Walkthrough Scheduled' or 'Reply Engaged'")
    print(f"      gray_zone_pipeline_stage_id — pick 'Manual Review' (already configured)")
    print(f"      reject_pipeline_stage_id    — pick 'Closed Lost' or similar")

    # 5. Execute (custom field creates + config patch)
    if "--apply" not in sys.argv:
        print("\n=== DRY RUN ===")
        print("  Re-run with --apply to create the missing fields and patch clear_facility.json.")
        return

    print("\n=== APPLYING ===")
    for spec in needs_create:
        print(f"  Creating '{spec['name']}'...")
        created = create_custom_field(api_key, location_id, spec)
        resolved_field_ids[spec["key"]] = created.get("id")
        print(f"    ✓ id={created.get('id')}")

    # Patch clear_facility.json
    print("\n  Patching clients/clear_facility.json...")
    if calendar_id:
        cfg["ghl"]["calendar_id"] = calendar_id
    for key, fid in resolved_field_ids.items():
        if fid:
            cfg["ghl"]["custom_field_ids"][key] = fid
    CFG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"  ✓ Wrote {CFG_PATH}")

    print("\n=== DONE ===")
    print("  Next: edit qualify/reject pipeline_stage_ids in clear_facility.json by hand")
    print("  (pick stage IDs from the listing above).")


if __name__ == "__main__":
    main()
