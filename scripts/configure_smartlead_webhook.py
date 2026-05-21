"""Provision (or update) a Smartlead campaign webhook to point at reply-router.

Smartlead's webhook config has no working DELETE/PATCH; POST with `{id: <existing>}`
acts as upsert (per reference_smartlead_webhook_shape memory).

This script:
  1. Lists existing webhooks on the campaign — if a matching one is found by name,
     we reuse its id (upsert mode)
  2. POSTs the webhook config with all 8 valid event categories enabled
     (Interested, Out Of Office, Not Interested, Information Request,
      Meeting Request, Wrong Person, Do Not Contact, Sender Originated Bounce)
  3. Verifies by listing again and confirming the URL + categories

Usage:
    python scripts/configure_smartlead_webhook.py <campaign_id> [<client_id>]

  <client_id> defaults to "clear_facility". The script reads
  clients/<client_id>.json for the Smartlead API key env var and the
  router secret env var (used to build the ?secret= query param on the
  webhook URL).

Pre-reqs:
  - Smartlead production campaign already created (you have its ID)
  - CFS_SMARTLEAD_API_KEY and CFS_ROUTER_SECRET in your local .env

Example:
    python scripts/configure_smartlead_webhook.py 3412345 clear_facility
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"
REPLY_ROUTER_URL = "https://reply-router.vercel.app"

# 8 valid event categories per Smartlead. Strings must match exactly (case + spacing).
ALL_CATEGORIES = [
    "Interested",
    "Out Of Office",
    "Not Interested",
    "Information Request",
    "Meeting Request",
    "Wrong Person",
    "Do Not Contact",
    "Sender Originated Bounce",
]

WEBHOOK_NAME = "reply-router-prod"  # Used for idempotency — script reuses an existing webhook with this name


def list_webhooks(api_key: str, campaign_id: str) -> list[dict]:
    url = f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/webhooks"
    resp = requests.get(url, params={"api_key": api_key}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("data") or []


def upsert_webhook(
    api_key: str,
    campaign_id: str,
    webhook_url: str,
    name: str,
    categories: list[str],
    existing_id: str | None = None,
) -> dict:
    """POST to webhook config. If existing_id is provided, treats it as upsert (per memory)."""
    body: dict = {
        "name": name,
        "webhook_url": webhook_url,
        "event_types": ["EMAIL_REPLY"],  # event type the webhook listens for
        "categories": categories,
    }
    if existing_id:
        body["id"] = existing_id
    url = f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/webhooks"
    resp = requests.post(url, params={"api_key": api_key}, json=body, timeout=15)
    if resp.status_code not in (200, 201):
        sys.exit(f"ERROR: webhook upsert failed: status={resp.status_code} body={resp.text[:400]}")
    return resp.json()


def main():
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        sys.exit(f"Usage: {sys.argv[0]} <campaign_id> [<client_id>]")

    campaign_id = sys.argv[1]
    client_id = sys.argv[2] if len(sys.argv) == 3 else "clear_facility"

    repo_root = Path(__file__).resolve().parent.parent
    cfg_path = repo_root / "clients" / f"{client_id}.json"
    if not cfg_path.exists():
        sys.exit(f"ERROR: client config not found at {cfg_path}")
    cfg = json.loads(cfg_path.read_text())

    sl_key = os.environ.get(cfg["smartlead"]["api_key_env"])
    router_secret = os.environ.get(cfg["auth"]["router_secret_env"])
    if not sl_key:
        sys.exit(f"ERROR: {cfg['smartlead']['api_key_env']} not in env")
    if not router_secret:
        sys.exit(f"ERROR: {cfg['auth']['router_secret_env']} not in env")

    webhook_url = (
        f"{REPLY_ROUTER_URL}/v1/clients/{client_id}/replies"
        f"?secret={router_secret}"
    )

    print(f"=== Configuring Smartlead webhook ===")
    print(f"  client_id:    {client_id}")
    print(f"  campaign_id:  {campaign_id}")
    print(f"  webhook_url:  {REPLY_ROUTER_URL}/v1/clients/{client_id}/replies?secret=<hidden>")
    print(f"  webhook_name: {WEBHOOK_NAME}")
    print(f"  categories:   {len(ALL_CATEGORIES)} ({', '.join(ALL_CATEGORIES)})")
    print()

    print("[1/3] Listing existing webhooks on this campaign...")
    existing = list_webhooks(sl_key, campaign_id)
    print(f"  Found {len(existing)} existing webhook(s):")
    for wh in existing:
        print(f"    - id={wh.get('id', '?'):>10}  name={wh.get('name', '?')!r:30}  "
              f"url={(wh.get('webhook_url') or '?')[:60]}")

    matching = next((wh for wh in existing if wh.get("name") == WEBHOOK_NAME), None)
    if matching:
        print(f"\n[2/3] Found existing webhook id={matching['id']} with our name — upserting...")
        result = upsert_webhook(
            sl_key, campaign_id, webhook_url, WEBHOOK_NAME,
            ALL_CATEGORIES, existing_id=matching["id"],
        )
    else:
        print(f"\n[2/3] No existing webhook named {WEBHOOK_NAME!r} — creating new...")
        result = upsert_webhook(
            sl_key, campaign_id, webhook_url, WEBHOOK_NAME, ALL_CATEGORIES,
        )
    print(f"  ✓ Webhook upserted: id={result.get('id', '?')}")

    print("\n[3/3] Verifying...")
    after = list_webhooks(sl_key, campaign_id)
    final = next((wh for wh in after if wh.get("name") == WEBHOOK_NAME), None)
    if not final:
        sys.exit("ERROR: webhook not present after upsert — investigation needed")
    print(f"  ✓ Verified webhook id={final.get('id')}")
    print(f"    name:       {final.get('name')}")
    print(f"    url:        {(final.get('webhook_url') or '')[:60]}...")
    print(f"    categories: {final.get('categories', [])}")
    print()
    print("=== DONE ===")
    print(f"Smartlead will now POST to {REPLY_ROUTER_URL}/v1/clients/{client_id}/replies")
    print("on any reply categorized as one of the 8 enabled categories.")
    print()
    print("Test by sending a reply to a campaign lead and watching Vercel function logs.")


if __name__ == "__main__":
    main()
