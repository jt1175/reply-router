"""Tests for reply_router.slack_client — Slack notification formatting + POST."""
from __future__ import annotations

import json

import pytest
import responses

from reply_router.slack_client import (
    post_classification_notification,
    post_urgent,
)

WEBHOOK_URL = "https://hooks.slack.com/services/T_TEST/B_TEST/abc123"


@responses.activate
def test_auto_sent_interested_includes_monitoring_tag():
    responses.add(responses.POST, WEBHOOK_URL, status=200, body="ok")
    post_classification_notification(
        WEBHOOK_URL,
        classification="interested",
        confidence="high",
        send_mode="auto_send",
        account={"company_name": "Hennepin Office Park", "contact_name": "Sarah Miller",
                 "contact_title": "Facilities Manager", "pipeline_to": "Qualified - Mid-Market"},
        reply_text="We've been thinking about switching vendors.",
        response_text="Great to hear, Sarah...",
        monitoring=True,
        ghl_contact_url="https://app.gohighlevel.com/contact/abc123",
    )
    sent = json.loads(responses.calls[0].request.body)
    assert "MONITORING" in sent["text"]
    assert "🟢 INTERESTED REPLY" in sent["text"]
    # Block headers always present
    assert sent["blocks"][0]["type"] == "header"
    # Auto-response text appears
    flat = json.dumps(sent)
    assert "auto-response" in flat.lower()
    assert "Great to hear, Sarah" in flat


@responses.activate
def test_shadow_send_includes_approval_button():
    responses.add(responses.POST, WEBHOOK_URL, status=200)
    post_classification_notification(
        WEBHOOK_URL,
        classification="interested",
        confidence="high",
        send_mode="shadow_send",
        account={"company_name": "Acme", "contact_name": "Pat", "contact_title": "Ops",
                 "pipeline_to": "Qualified - Mid-Market"},
        reply_text="Interested — tell me more.",
        response_text="Great to hear, Pat...",
        approval_url="https://reply-router.vercel.app/v1/approvals/tok_abc",
        monitoring=False,
    )
    sent = json.loads(responses.calls[0].request.body)
    # Find the actions block and confirm the URL is the approval link
    actions = [b for b in sent["blocks"] if b.get("type") == "actions"]
    assert len(actions) == 1
    assert actions[0]["elements"][0]["url"] == "https://reply-router.vercel.app/v1/approvals/tok_abc"
    # No "auto-response sent" wording for shadow
    assert "NOT YET SENT" in json.dumps(sent)


@responses.activate
def test_urgent_notification_format():
    responses.add(responses.POST, WEBHOOK_URL, status=200)
    post_urgent(
        WEBHOOK_URL,
        title="Unsubscribe not honored in GHL — CFS",
        action_required="1. Open GHL contact\n2. Manually add to DNC\n3. Reply ✅ when done",
        reply_text="Please remove me.",
        ghl_contact_url="https://app.gohighlevel.com/contact/abc",
    )
    sent = json.loads(responses.calls[0].request.body)
    assert "🚨 URGENT" in sent["text"]
    # Action required block must appear
    assert "Action required" in json.dumps(sent)


@responses.activate
def test_slack_5xx_does_not_raise():
    """Slack failures are best-effort — they log and continue, never raise."""
    responses.add(responses.POST, WEBHOOK_URL, status=500)
    responses.add(responses.POST, WEBHOOK_URL, status=500)  # retry also fails
    # Must NOT raise
    post_classification_notification(
        WEBHOOK_URL,
        classification="not_now", confidence="medium", send_mode="shadow_send",
        account={"company_name": "X", "contact_name": "Y", "contact_title": "Z",
                 "pipeline_to": "Future Follow-Up"},
        reply_text="not now thanks",
        response_text="totally understand",
        approval_url="https://example/x",
    )


@responses.activate
def test_slack_network_error_does_not_raise():
    """Even a connection error should be swallowed."""
    responses.add(
        responses.POST,
        WEBHOOK_URL,
        body=ConnectionError("network down"),
    )
    post_urgent(WEBHOOK_URL, title="x", action_required="y")
