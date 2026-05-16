"""Live smoke test for Slack client — posts to #reply-router-tests."""
from __future__ import annotations

import os
import pytest
from dotenv import load_dotenv

from reply_router.slack_client import (
    post_classification_notification,
    post_urgent,
)

load_dotenv()

WEBHOOK = os.environ.get("TEST_SLACK_WEBHOOK_URL")


@pytest.mark.skipif(not WEBHOOK, reason="TEST_SLACK_WEBHOOK_URL not set")
def test_live_post_interested_shadow():
    post_classification_notification(
        WEBHOOK,
        classification="interested", confidence="high", send_mode="shadow_send",
        account={"company_name": "Smoke Test Co", "contact_name": "Test User",
                 "contact_title": "QA", "pipeline_to": "Qualified - Mid-Market"},
        reply_text="This is a smoke test reply.",
        response_text="This is a smoke test response.",
        approval_url="https://example.invalid/abc",
        monitoring=True,
        ghl_contact_url="https://app.gohighlevel.com/contact/smoke",
    )
    print("\n  Posted to Slack — check #reply-router-tests")


@pytest.mark.skipif(not WEBHOOK, reason="TEST_SLACK_WEBHOOK_URL not set")
def test_live_post_urgent():
    post_urgent(
        WEBHOOK,
        title="Smoke test URGENT",
        action_required="1. Confirm this rendered\n2. Reply ✅",
        reply_text="The full reply text would appear here.",
    )
