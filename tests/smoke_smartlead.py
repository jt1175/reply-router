"""Live smoke test for Smartlead client — hits real sandbox.

Run: make verify-live
Requires: TEST_SMARTLEAD_API_KEY, TEST_SMARTLEAD_CAMPAIGN_ID, TEST_SMARTLEAD_EMAIL_STATS_ID,
          TEST_SMARTLEAD_REPLY_MESSAGE_ID env vars.

CAUTION: This actually sends an email. Use a sandbox campaign with disposable inboxes.
"""
from __future__ import annotations

import os
import pytest
from dotenv import load_dotenv

from reply_router.smartlead_client import SmartleadClient

load_dotenv()


@pytest.mark.skipif(
    not os.environ.get("TEST_SMARTLEAD_API_KEY"),
    reason="TEST_SMARTLEAD_API_KEY not set",
)
def test_live_send_reply_in_thread():
    client = SmartleadClient(api_key=os.environ["TEST_SMARTLEAD_API_KEY"])
    client.send_reply_in_thread(
        campaign_id=os.environ["TEST_SMARTLEAD_CAMPAIGN_ID"],
        email_stats_id=os.environ["TEST_SMARTLEAD_EMAIL_STATS_ID"],
        body="reply-router smoke test — please ignore.",
        reply_message_id=os.environ["TEST_SMARTLEAD_REPLY_MESSAGE_ID"],
    )
    # If no exception, smoke passed. JT manually verifies the reply threaded in Gmail.
    print("\n  Smartlead smoke: send_reply_in_thread returned 200. Verify thread manually.")
