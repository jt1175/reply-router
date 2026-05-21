"""Provision the CFS tag taxonomy in GHL.

Idempotent — checks existing tags first, only creates missing ones.

Tag taxonomy reasoning:
  cohort_*       Track which Apollo cohort the lead came from (for retro analysis)
  deal_*         Mirror of the Deal Type custom field; tags also drive Smartlead segmentation
  scoring_*      Tier bucket from the scoring run (also captured in Qualification Score)
  state_*        Lifecycle state markers used by smart lists / GHL automations
  vendor_*       Behavioral signals from reply content (e.g., "they're locked into a 2-year contract")

Each tag is single-purpose and queryable from GHL's smart-list builder.

Usage:
    python scripts/provision_ghl_tags.py             # dry-run
    python scripts/provision_ghl_tags.py --apply     # creates missing tags
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = "clear_facility"

# Taxonomy: tag name → purpose comment (purpose is documentation for the runbook, not stored in GHL)
TAG_TAXONOMY = [
    # Cohort tracking
    ("cohort_1",                 "Cohort 1 (first 500 leads from Apollo pull 2026-05-20)"),
    ("cohort_2",                 "Cohort 2 (next batch, TBD)"),
    # Deal type (mirrors Deal Type custom field for smart-list filters)
    ("deal_velocity",            "$500-1K/mo small offices"),
    ("deal_mid_market",          "$3-15K/mo, 20-50K sqft"),
    ("deal_enterprise",          "$20K+/mo, large campus"),
    ("deal_disqualified",        "Failed ICP gate (industry/size/exclusions)"),
    # Scoring tier (mirrors Qualification Score 1-10)
    ("scoring_hot",              "Score 7-10 (top tier, immediate outreach)"),
    ("scoring_warm",             "Score 4-6 (mid-tier, sequence per usual)"),
    ("scoring_cold",             "Score 1-3 (low fit, may be re-checked in future)"),
    # State / lifecycle markers (lightweight, supplements pipeline stage)
    ("state_in_outreach",        "Currently in active Smartlead sequence"),
    ("state_replied",            "Has replied to at least one outreach email"),
    ("state_walkthrough_booked", "Walkthrough has been scheduled"),
    ("state_walkthrough_done",   "Walkthrough completed, in proposal flow"),
    ("state_won",                "Closed Won deal"),
    ("state_lost",               "Closed Lost deal"),
    ("state_paused",             "Smartlead sequence paused (do-not-contact, manual hold, etc.)"),
    ("state_bounce_risk",        "Bounced 2+ times — held off list"),
    ("state_unsubscribed",       "Opted out, on DNC"),
    ("state_gray_zone",          "Manual review needed (qualifier ambiguous)"),
    # Behavioral / signal tags (reply-content-driven, set by automations or manually)
    ("vendor_locked_contract",   "Said they're locked into a vendor contract"),
    ("vendor_currently_unhappy", "Hinted at issues with current vendor"),
    ("vendor_in_house_cleaning", "Said they clean in-house (DQ-ish)"),
    ("vendor_seeking_quote",     "Actively seeking quotes from multiple vendors"),
    ("interest_pricing_first",   "Asked for pricing before discovery"),
    ("interest_walkthrough_yes", "Said yes to walkthrough"),
    ("interest_follow_up_later", "Asked for follow-up in N weeks/months"),
    ("interest_wrong_contact",   "Said they're not the decision-maker"),
]


def main() -> None:
    apply_changes = "--apply" in sys.argv
    repo_root = Path(__file__).resolve().parent.parent
    cfg_path = repo_root / "clients" / f"{CLIENT_ID}.json"
    cfg = json.loads(cfg_path.read_text())
    ghl = cfg["ghl"]
    api_key = os.environ.get(ghl["api_key_env"])
    if not api_key:
        sys.exit(f"ERROR: {ghl['api_key_env']} not in env")
    location_id = ghl["sub_account_id"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    print("=== Fetching existing tags ===")
    r = requests.get(
        f"https://services.leadconnectorhq.com/locations/{location_id}/tags",
        headers=headers, timeout=15,
    )
    r.raise_for_status()
    existing = {t["name"]: t["id"] for t in r.json().get("tags", [])}
    print(f"  Found {len(existing)} existing tags")
    for name, tid in sorted(existing.items()):
        print(f"    - {name:<35} id={tid}")

    print(f"\n=== Tag taxonomy plan ({len(TAG_TAXONOMY)} desired) ===")
    to_create = []
    to_keep = []
    for name, purpose in TAG_TAXONOMY:
        if name in existing:
            to_keep.append((name, existing[name], purpose))
        else:
            to_create.append((name, purpose))

    print(f"  Already exist (will keep): {len(to_keep)}")
    for name, tid, purpose in to_keep:
        print(f"    ✓ {name:<35} {purpose}")
    print(f"\n  To create: {len(to_create)}")
    for name, purpose in to_create:
        print(f"    + {name:<35} {purpose}")

    if not apply_changes:
        print("\n[DRY RUN] Re-run with --apply to provision missing tags.")
        return

    if not to_create:
        print("\n[NO-OP] All taxonomy tags already exist.")
        return

    print(f"\n[APPLYING] Creating {len(to_create)} new tags...")
    created = 0
    failed = []
    for name, _purpose in to_create:
        r = requests.post(
            f"https://services.leadconnectorhq.com/locations/{location_id}/tags",
            headers=headers,
            json={"name": name},
            timeout=10,
        )
        if r.status_code in (200, 201):
            tid = r.json().get("tag", {}).get("id", "?")
            print(f"  ✓ {name:<35} id={tid}")
            created += 1
        else:
            print(f"  ✗ {name:<35} status={r.status_code} body={r.text[:140]}")
            failed.append((name, r.status_code, r.text[:140]))

    print(f"\n=== DONE: created {created}/{len(to_create)} ===")
    if failed:
        print(f"FAILED ({len(failed)}):")
        for name, code, body in failed:
            print(f"  - {name}: {code} {body}")


if __name__ == "__main__":
    main()
