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


@responses.activate
def test_list_replies_flattens_email_history_to_replies(client):
    """Verified live 2026-05-21 — list_replies uses POST /master-inbox/inbox-replies.
    Each lead returned has email_history; we pair SENT+REPLY by stats_id and flatten
    REPLY entries into ReplyPayload-shaped dicts."""
    responses.add(
        responses.POST,
        f"{SMARTLEAD_BASE}/master-inbox/inbox-replies",
        json={
            "ok": True,
            "data": [
                {
                    "lead_email": "pat@acme.com",
                    "email_lead_id": "lead_111",
                    "email_campaign_id": 100,
                    "lead_first_name": "Pat",
                    "lead_last_name": "X",
                    "last_reply_time": "2026-05-20T05:00:00Z",
                    "email_history": [
                        {"type": "SENT", "stats_id": "stats_A",
                         "message_id": "<sent_A@x>", "subject": "Quick question",
                         "email_body": "<p>hi</p>", "time": "2026-05-20T03:00:00Z"},
                        {"type": "REPLY", "stats_id": "stats_A",
                         "message_id": "<reply_A@mail.gmail.com>",
                         "from": "pat@acme.com", "to": "sender@x.com",
                         "email_body": "<div>Interested!</div>",
                         "time": "2026-05-20T05:00:00Z"},
                    ],
                },
            ],
            "offset": 0, "limit": 20,
        },
        status=200,
    )
    since = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    replies = client.list_replies(campaign_ids=["100"], since=since)
    assert len(replies) == 1
    r = replies[0]
    assert r["message_id"] == "<reply_A@mail.gmail.com>"
    assert r["lead_email"] == "pat@acme.com"
    assert r["from_email"] == "pat@acme.com"
    assert r["campaign_id"] == "100"
    assert "Interested!" in r["reply_text"]
    assert r["email_stats_id"] == "stats_A"  # paired from the SENT
    assert r["subject"] == "Quick question"  # from the SENT (original subject)
    assert r["lead_first_name"] == "Pat"


@responses.activate
def test_list_replies_paginates_until_short_page(client):
    """No total_count in response — stop fetching when page < limit."""
    def _lead(i):
        return {
            "lead_email": f"p{i}@x.com", "email_lead_id": f"l{i}",
            "email_campaign_id": 100, "lead_first_name": f"P{i}",
            "email_history": [{"type": "REPLY", "stats_id": "", "message_id": f"<r{i}>",
                              "email_body": "ok", "time": "2026-05-20T05:00:00Z"}],
        }
    responses.add(
        responses.POST,
        f"{SMARTLEAD_BASE}/master-inbox/inbox-replies",
        json={"ok": True, "data": [_lead(i) for i in range(20)], "offset": 0, "limit": 20},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{SMARTLEAD_BASE}/master-inbox/inbox-replies",
        json={"ok": True, "data": [_lead(i) for i in range(100, 105)], "offset": 20, "limit": 20},
        status=200,
    )
    since = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    replies = client.list_replies(campaign_ids=["100"], since=since)
    assert len(replies) == 25
    assert len(responses.calls) == 2  # stopped after short page


def test_list_replies_skips_tbd_campaign_ids(client):
    """Campaigns whose IDs are still TBD_ placeholders are filtered out before calling Smartlead."""
    since = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    replies = client.list_replies(campaign_ids=["TBD_FOO", "TBD_BAR"], since=since)
    assert replies == []


def test_list_replies_rejects_non_numeric_campaign_ids(client):
    """Smartlead campaign IDs are integers — non-numeric strings should error early."""
    since = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(SmartleadError, match="non-numeric"):
        client.list_replies(campaign_ids=["not-a-number"], since=since)


def test_list_replies_rejects_too_many_campaigns(client):
    """Smartlead caps campaignId filter at 5 entries — chunk before calling."""
    since = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(SmartleadError, match="max of 5"):
        client.list_replies(
            campaign_ids=["1", "2", "3", "4", "5", "6"],
            since=since,
        )


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
