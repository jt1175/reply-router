"""Live smoke test for GHL client — hits real sandbox GHL.

Run: make verify-live
Requires: TEST_GHL_API_KEY, TEST_GHL_SUB_ACCOUNT_ID env vars set.
"""
from __future__ import annotations

import os
import pytest
from dotenv import load_dotenv

from reply_router.ghl_client import GHLClient

load_dotenv()


@pytest.mark.skipif(
    not os.environ.get("TEST_GHL_API_KEY"),
    reason="TEST_GHL_API_KEY not set — skipping live smoke",
)
def test_live_ghl_contact_lookup():
    client = GHLClient(
        api_key=os.environ["TEST_GHL_API_KEY"],
        sub_account_id=os.environ["TEST_GHL_SUB_ACCOUNT_ID"],
        campaign_ids=["test"],
    )
    # Sandbox should have at least one seeded test contact at this address:
    contacts = client.get_contacts_by_email(os.environ.get("TEST_SEED_EMAIL", "seed@test.invalid"))
    print(f"\n  GHL lookup returned {len(contacts)} contacts")
    assert isinstance(contacts, list)
