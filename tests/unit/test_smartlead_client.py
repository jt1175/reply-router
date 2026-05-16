"""Tests for reply_router.smartlead_client — Smartlead campaign API wrapper."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import responses

from reply_router.smartlead_client import SmartleadClient, SmartleadError

SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"


@pytest.fixture
def client():
    return SmartleadClient(api_key="test-key")


@responses.activate
def test_send_reply_in_thread_posts_correct_body(client):
    responses.add(
        responses.POST,
        f"{SMARTLEAD_BASE}/campaigns/c1/reply-email-thread",
        json={"ok": True},
        status=200,
    )
    client.send_reply_in_thread(
        campaign_id="c1",
        email_stats_id="stats_1",
        body="Removed you from our list.",
        reply_message_id="<msg-abc@mail.gmail.com>",
    )
    call = responses.calls[0]
    assert "api_key=test-key" in call.request.url
    import json
    body = json.loads(call.request.body)
    assert body == {
        "email_stats_id": "stats_1",
        "email_body": "Removed you from our list.",
        "reply_message_id": "<msg-abc@mail.gmail.com>",
    }


@responses.activate
def test_send_reply_in_thread_5xx_raises_smartlead_error(client):
    responses.add(
        responses.POST,
        f"{SMARTLEAD_BASE}/campaigns/c1/reply-email-thread",
        status=502,
        body="bad gateway",
    )
    with pytest.raises(SmartleadError, match="status=502"):
        client.send_reply_in_thread("c1", "stats_1", "body", "<mid@x>")


@responses.activate
def test_send_reply_in_thread_4xx_raises_smartlead_error(client):
    """4xx is a permanent failure — don't retry, but surface clearly."""
    responses.add(
        responses.POST,
        f"{SMARTLEAD_BASE}/campaigns/c1/reply-email-thread",
        status=400,
        json={"error": "invalid email_stats_id"},
    )
    with pytest.raises(SmartleadError, match="status=400"):
        client.send_reply_in_thread("c1", "bad_stats", "body", "<mid@x>")


def test_list_replies_raises_when_endpoint_unverified():
    """Until Task 2.4 step 2 research confirms the URL, this method MUST raise on call.
    Prevents an unverified placeholder from silently shipping."""
    from reply_router.smartlead_client import _LIST_REPLIES_ENDPOINT_VERIFIED
    if _LIST_REPLIES_ENDPOINT_VERIFIED:
        pytest.skip("endpoint now verified — test_list_replies_returns_replies_across_campaigns covers it")
    c = SmartleadClient(api_key="test-key")
    with pytest.raises(RuntimeError, match="not yet verified"):
        c.list_replies(campaign_ids=["c1"], since=datetime(2026, 5, 15, tzinfo=timezone.utc))


@responses.activate
def test_list_replies_returns_replies_across_campaigns(client):
    """Active test for the verified endpoint. Auto-skipped when the flag is False."""
    from reply_router.smartlead_client import _LIST_REPLIES_ENDPOINT_VERIFIED
    if not _LIST_REPLIES_ENDPOINT_VERIFIED:
        pytest.skip("Smartlead list_replies endpoint not yet verified — see Task 2.4 step 2")
    responses.add(
        responses.GET,
        f"{SMARTLEAD_BASE}/campaigns/c1/messages",
        json={"messages": [{"message_id": "m1", "body": "interested!"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{SMARTLEAD_BASE}/campaigns/c2/messages",
        json={"messages": [{"message_id": "m2", "body": "not now"}]},
        status=200,
    )
    since = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    replies = client.list_replies(campaign_ids=["c1", "c2"], since=since)
    assert len(replies) == 2
    assert {r["message_id"] for r in replies} == {"m1", "m2"}


def test_mark_unsubscribe_raises_when_endpoint_unverified():
    from reply_router.smartlead_client import _MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED
    if _MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED:
        pytest.skip("endpoint now verified — test_mark_unsubscribe_calls_status_endpoint covers it")
    c = SmartleadClient(api_key="test-key")
    with pytest.raises(RuntimeError, match="not yet verified"):
        c.mark_unsubscribe(campaign_id="c1", lead_id="lead_1")


@responses.activate
def test_mark_unsubscribe_calls_status_endpoint(client):
    """Active test for the verified endpoint. Auto-skipped when the flag is False."""
    from reply_router.smartlead_client import _MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED
    if not _MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED:
        pytest.skip("Smartlead mark_unsubscribe endpoint not yet verified — see Task 2.4 step 2")
    responses.add(
        responses.PATCH,
        f"{SMARTLEAD_BASE}/campaigns/c1/leads/lead_1/status",
        json={"ok": True},
        status=200,
    )
    client.mark_unsubscribe(campaign_id="c1", lead_id="lead_1")


def test_empty_api_key_raises():
    with pytest.raises(ValueError, match="non-empty api_key"):
        SmartleadClient(api_key="")
