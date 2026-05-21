"""Refactor the CFS GHL pipeline to JT's preferred 8-stage layout.

NEW STAGE ORDER (per JT 2026-05-20):
  0. Outreach            (NEW)
  1. Replied             (rename of "New Reply")
  2. Call Scheduled      (NEW)
  3. Walkthrough Scheduled  (preserve id — referenced in config)
  4. Walkthrough Done    (NEW)
  5. Proposal Sent       (rename of "Proposal / RFP Sent")
  6. Nurture             (preserve id — used by gray_zone qualifier flow)
  7. Closed Won          (preserve id)
  8. Closed Lost         (preserve id — referenced in config as reject_pipeline_stage_id)

REMOVED:
  - Qualified - Velocity   (deal_type tracked via custom field instead)
  - Qualified - Mid-Market (deal_type tracked via custom field instead)

Safety:
  - Re-runs are idempotent: reads current stage names → only mutates if not already matching target
  - Captures all new stage IDs and patches clients/clear_facility.json
  - DRY RUN by default; pass --apply to make changes

Usage:
  python scripts/refactor_ghl_pipeline.py             # dry-run, shows diff
  python scripts/refactor_ghl_pipeline.py --apply     # mutates GHL + config
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
PIPELINE_ID = "WaNo1BZftVUmpieCPweb"

# Map current stage names → new stage name (for ID preservation)
RENAME_MAP = {
    "New Reply": "Replied",
    "Proposal / RFP Sent": "Proposal Sent",
}

# Stages to drop entirely
DROP_STAGES = {"Qualified - Velocity", "Qualified - Mid-Market"}

# Final desired order (name → position)
TARGET_ORDER = [
    "Outreach",
    "Replied",
    "Call Scheduled",
    "Walkthrough Scheduled",
    "Walkthrough Done",
    "Proposal Sent",
    "Nurture",
    "Closed Won",
    "Closed Lost",
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

    # Fetch current pipeline state
    r = requests.get(
        "https://services.leadconnectorhq.com/opportunities/pipelines",
        params={"locationId": location_id},
        headers=headers, timeout=15,
    )
    r.raise_for_status()
    pipelines = r.json().get("pipelines", [])
    pipeline = next((p for p in pipelines if p["id"] == PIPELINE_ID), None)
    if not pipeline:
        sys.exit(f"ERROR: pipeline {PIPELINE_ID} not found")

    current_stages = pipeline.get("stages", [])
    print(f"=== Current pipeline: {pipeline['name']} ({len(current_stages)} stages) ===")
    for s in sorted(current_stages, key=lambda s: s.get("position", 0)):
        print(f"  {s.get('position', '?'):>2}. {s.get('name')!r:35} id={s.get('id')}")

    # Build new stages list. For each target stage:
    #   - if current pipeline has a stage with that name → preserve id
    #   - if a renamed-from-old-name stage exists → preserve id with new name
    #   - else → no id (create new)
    name_to_existing_id = {s["name"]: s["id"] for s in current_stages}
    # Add renamed entries
    for old_name, new_name in RENAME_MAP.items():
        if old_name in name_to_existing_id:
            name_to_existing_id[new_name] = name_to_existing_id[old_name]

    new_stages = []
    for idx, name in enumerate(TARGET_ORDER):
        stage: dict = {"name": name, "position": idx}
        if name in name_to_existing_id:
            stage["id"] = name_to_existing_id[name]
            origin = "preserve"
            if name in RENAME_MAP.values():
                # find which old name maps here
                old = next((o for o, n in RENAME_MAP.items() if n == name), None)
                if old and old in [s["name"] for s in current_stages]:
                    origin = f"rename from {old!r}"
        else:
            origin = "NEW"
        new_stages.append((stage, origin))

    print(f"\n=== Proposed new pipeline ({len(new_stages)} stages) ===")
    for stage, origin in new_stages:
        id_str = stage.get("id", "(will-create)")
        print(f"  {stage['position']:>2}. {stage['name']!r:35} {origin:25} id={id_str}")

    will_drop = [s for s in current_stages if s["name"] in DROP_STAGES]
    if will_drop:
        print(f"\n=== Stages to be REMOVED ===")
        for s in will_drop:
            print(f"  - {s['name']} (id={s['id']})  — currently position {s.get('position', '?')}")
        print("  NOTE: GHL may refuse to remove a stage with active opportunities. Cohort hasn't sent yet, so should be safe.")

    if not apply_changes:
        print("\n[DRY RUN] Re-run with --apply to mutate GHL + patch config.")
        return

    # Apply: PUT the new pipeline shape
    print("\n[APPLYING] PUT /opportunities/pipelines/{id}...")
    body = {
        "name": pipeline["name"],
        "stages": [s for s, _ in new_stages],
    }
    r = requests.put(
        f"https://services.leadconnectorhq.com/opportunities/pipelines/{PIPELINE_ID}",
        params={"locationId": location_id},
        headers=headers,
        json=body,
        timeout=20,
    )
    print(f"  Status: {r.status_code}")
    if r.status_code not in (200, 201):
        sys.exit(f"  ERROR body: {r.text[:600]}")
    response_data = r.json()
    updated_pipeline = response_data.get("pipeline") or response_data
    updated_stages = updated_pipeline.get("stages", [])
    print(f"  ✓ Pipeline updated. New state ({len(updated_stages)} stages):")
    name_to_new_id = {}
    for s in sorted(updated_stages, key=lambda s: s.get("position", 0)):
        sid = s.get("id")
        print(f"    {s.get('position', '?'):>2}. {s.get('name')!r:35} id={sid}")
        name_to_new_id[s["name"]] = sid

    # Patch clear_facility.json with new stage IDs
    print("\n[PATCHING CONFIG]")
    # qualify_pipeline_stage_id stays = Walkthrough Scheduled
    new_qualify_id = name_to_new_id.get("Walkthrough Scheduled", cfg.get("qualify_pipeline_stage_id"))
    # gray_zone stays = Nurture
    new_gray_id = name_to_new_id.get("Nurture", cfg.get("gray_zone_pipeline_stage_id"))
    # reject stays = Closed Lost
    new_reject_id = name_to_new_id.get("Closed Lost", cfg.get("reject_pipeline_stage_id"))
    # pause_on_stage_ids: Closed Won + Closed Lost
    new_pause_ids = [
        name_to_new_id.get("Closed Won"),
        name_to_new_id.get("Closed Lost"),
    ]
    new_pause_ids = [x for x in new_pause_ids if x]

    changed = False
    if cfg.get("qualify_pipeline_stage_id") != new_qualify_id:
        cfg["qualify_pipeline_stage_id"] = new_qualify_id
        changed = True
        print(f"  qualify_pipeline_stage_id → {new_qualify_id}")
    if cfg.get("gray_zone_pipeline_stage_id") != new_gray_id:
        cfg["gray_zone_pipeline_stage_id"] = new_gray_id
        changed = True
        print(f"  gray_zone_pipeline_stage_id → {new_gray_id}")
    if cfg.get("reject_pipeline_stage_id") != new_reject_id:
        cfg["reject_pipeline_stage_id"] = new_reject_id
        changed = True
        print(f"  reject_pipeline_stage_id → {new_reject_id}")
    if cfg.get("pause_on_stage_ids") != new_pause_ids:
        cfg["pause_on_stage_ids"] = new_pause_ids
        changed = True
        print(f"  pause_on_stage_ids → {new_pause_ids}")

    if changed:
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"  ✓ Saved {cfg_path}")
    else:
        print("  (no config changes needed)")

    print("\n=== DONE ===")
    print("Next: commit clients/clear_facility.json if it was patched.")


if __name__ == "__main__":
    main()
