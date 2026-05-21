"""Tests for Smartlead non-reply event handlers (OPEN, CLICK, BOUNCE, UNSUBSCRIBE)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from reply_router.event_handlers import (
    EVENT_BOUNCE, EVENT_CLICK, EVENT_OPEN, EVENT_REPLY, EVENT_UNSUBSCRIBE,
    detect_event_type, handle_bounce, handle_click, handle_non_reply_event,
    handle_open, handle_unsubscribe,
)


# Minimal stand-in client config for handler tests
class FakeBC:
    company_name = "X"


class FakeGHL:
    api_key_env = "X"
    sub_account_id = "loc"
    pipeline_id = "p"
    custom_field_ids = {
        "email_open_count": "cf_open_n",
        "email_click_count": "cf_click_n",
        "email_bounce_count": "cf_bounce_n",
        "last_open_at": "cf_last_open",
        "last_click_at": "cf_last_click",
        "unsubscribed_at": "cf_unsub_at",
    }


class FakeCfg:
    business_context = FakeBC()
    ghl = FakeGHL()


@pytest.fixture
def cfg():
    return FakeCfg()


@pytest.fixture
def ghl():
    g = MagicMock()
    g.get_contacts_by_email.return_value = [{
        "id": "ct_1",
        "email": "pat@acme.com",
        "customFields": [
            {"id": "cf_open_n", "value": "3"},
            {"id": "cf_click_n", "value": "1"},
            {"id": "cf_bounce_n", "value": "0"},
        ],
    }]
    return g


# ─── detect_event_type ───

def test_detect_explicit_event_type_reply():
    assert detect_event_type({"event_type": "EMAIL_REPLY"}) == EVENT_REPLY


def test_detect_explicit_event_type_open():
    assert detect_event_type({"event_type": "EMAIL_OPEN"}) == EVENT_OPEN


def test_detect_event_type_variant_strings():
    """Variants like 'email opened', 'link click', etc. should normalize."""
    assert detect_event_type({"event_type": "Email Opened"}) == EVENT_OPEN
    assert detect_event_type({"event_type": "LINK_CLICK"}) == EVENT_CLICK
    assert detect_event_type({"event_type": "EMAIL_BOUNCE"}) == EVENT_BOUNCE
    assert detect_event_type({"type": "EMAIL_UNSUBSCRIBED"}) == EVENT_UNSUBSCRIBE


def test_detect_reply_from_payload_shape():
    """No event_type field → infer REPLY from presence of reply_message."""
    assert detect_event_type({"reply_message": {"text": "interested"}}) == EVENT_REPLY


def test_detect_unknown_when_empty():
    assert detect_event_type({}) == "UNKNOWN"
    assert detect_event_type({"foo": "bar"}) == "UNKNOWN"


# ─── handle_open ───

def test_handle_open_increments_count_and_sets_timestamp(ghl, cfg):
    result = handle_open({"to_email": "pat@acme.com"}, ghl, cfg)
    assert result["status"] == "processed"
    assert result["open_count"] == 4  # was 3, now 4
    ghl.update_contact.assert_called_once()
    write = ghl.update_contact.call_args.kwargs["custom_fields"]
    assert write["cf_open_n"] == "4"
    assert "cf_last_open" in write


def test_handle_open_ignored_when_contact_not_found(cfg):
    g = MagicMock()
    g.get_contacts_by_email.return_value = []
    result = handle_open({"to_email": "unknown@x.com"}, g, cfg)
    assert result["status"] == "ignored"
    g.update_contact.assert_not_called()


def test_handle_open_finds_email_in_lead_subfield(ghl, cfg):
    """Some Smartlead payloads nest email under lead.email rather than to_email."""
    handle_open({"lead": {"email": "pat@acme.com"}}, ghl, cfg)
    assert ghl.update_contact.called


# ─── handle_click ───

def test_handle_click_increments_count_and_sets_timestamp(ghl, cfg):
    result = handle_click({"to_email": "pat@acme.com"}, ghl, cfg)
    assert result["status"] == "processed"
    assert result["click_count"] == 2  # was 1
    write = ghl.update_contact.call_args.kwargs["custom_fields"]
    assert write["cf_click_n"] == "2"


# ─── handle_bounce ───

def test_handle_bounce_first_bounce_increments_only(ghl, cfg):
    """1st bounce → just increment, no DNC."""
    result = handle_bounce({"to_email": "pat@acme.com"}, ghl, cfg)
    assert result["status"] == "processed"
    assert result["bounce_count"] == 1
    assert "tagged" not in result
    ghl.add_to_dnc.assert_not_called()


def test_handle_bounce_second_bounce_triggers_dnc(cfg):
    g = MagicMock()
    g.get_contacts_by_email.return_value = [{
        "id": "ct_1", "email": "pat@x.com",
        "customFields": [{"id": "cf_bounce_n", "value": "1"}],
    }]
    result = handle_bounce({"to_email": "pat@x.com"}, g, cfg)
    assert result["status"] == "processed"
    assert result["bounce_count"] == 2
    assert result.get("tagged") is True
    assert result.get("dnc") is True
    g.add_tags.assert_called_with("ct_1", ["bounce_risk"])
    g.add_to_dnc.assert_called_with("ct_1")


# ─── handle_unsubscribe ───

def test_handle_unsubscribe_dnc_and_tagged(ghl, cfg):
    result = handle_unsubscribe({"to_email": "pat@acme.com"}, ghl, cfg)
    assert result["status"] == "processed"
    ghl.add_to_dnc.assert_called_with("ct_1")
    ghl.add_tags.assert_called_with("ct_1", ["unsubscribed"])
    write = ghl.update_contact.call_args.kwargs["custom_fields"]
    assert "cf_unsub_at" in write


def test_handle_unsubscribe_deferred_when_dnc_fails(ghl, cfg):
    ghl.add_to_dnc.side_effect = RuntimeError("simulated GHL outage")
    result = handle_unsubscribe({"to_email": "pat@acme.com"}, ghl, cfg)
    assert result["status"] == "deferred"
    assert "DNC failed" in result["reason"]


# ─── handle_non_reply_event dispatcher ───

def test_dispatcher_routes_to_correct_handler(ghl, cfg):
    """Each known event type goes to the right handler."""
    handle_non_reply_event(EVENT_OPEN, {"to_email": "pat@acme.com"}, ghl, cfg)
    assert ghl.update_contact.called  # handle_open writes


def test_dispatcher_unknown_returns_ignored(ghl, cfg):
    result = handle_non_reply_event("WEIRD_EVENT", {}, ghl, cfg)
    assert result["status"] == "ignored"
    assert "no handler" in result["reason"]
    ghl.update_contact.assert_not_called()


# ─── Webhook dispatch integration (via /replies endpoint) ───

@pytest.fixture
def webhook_client(monkeypatch, tmp_path):
    """TestClient with the 6 metrics custom field IDs configured."""
    from fastapi.testclient import TestClient
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    test_cfg = clients_dir / "test_client.json"
    test_cfg.write_text("""{
        "client_id": "test_client", "client_display_name": "Test",
        "ghl": {"sub_account_id": "loc", "api_key_env": "TEST_GHL_API_KEY",
                "pipeline_id": "p", "custom_field_ids": {
                  "reply_classification": "cf_class", "reply_received_at": "cf_at",
                  "contract_end_date": "cf_end", "nurture_bucket": "cf_nb",
                  "last_processed_smartlead_message_ids": "cf_roll",
                  "currently_processing_smartlead_message_id": "cf_lock",
                  "pending_draft_token": "cf_tok", "pending_draft_text": "cf_dtext",
                  "pending_draft_created_at": "cf_dat",
                  "pending_reply_message_id": "cf_rmid",
                  "pending_reply_email_stats_id": "cf_resid",
                  "email_open_count": "cf_open_n",
                  "email_click_count": "cf_click_n",
                  "email_bounce_count": "cf_bounce_n",
                  "last_open_at": "cf_last_open",
                  "last_click_at": "cf_last_click",
                  "unsubscribed_at": "cf_unsub_at"}},
        "smartlead": {"api_key_env": "TEST_SMARTLEAD_API_KEY", "campaign_ids": ["c1"]},
        "slack": {"incoming_webhook_url_env": "TEST_SLACK_URL"},
        "auth": {"router_secret_env": "TEST_ROUTER_SECRET"},
        "sending_inboxes": ["s@t.invalid"],
        "monitoring_until": "2099-01-01",
        "classification_actions": {
          "unsubscribe":  {"auto_send": true, "min_confidence": "low", "slack_notify": false, "pipeline_stage_id": "s1"},
          "wrong_person": {"auto_send": true, "min_confidence": "medium", "slack_notify": true, "pipeline_stage_id": "s2"},
          "interested":   {"auto_send": false, "min_confidence": "high", "slack_notify": true, "pipeline_stage_id": "s3"},
          "not_now":      {"auto_send": false, "min_confidence": "medium", "slack_notify": true, "pipeline_stage_id": "s4"},
          "info_request": {"auto_send": false, "min_confidence": "high", "slack_notify": true, "pipeline_stage_id": "s5"},
          "objection":    {"auto_send": false, "min_confidence": "high", "slack_notify": true, "pipeline_stage_id": "s5"}},
        "business_context": {
          "company_name": "T", "service_area": "A",
          "services_offered": [], "services_not_offered": [],
          "pricing_response": "depends.", "booking_link": "https://x/PLACEHOLDER"}
    }""")
    monkeypatch.setenv("TEST_ROUTER_SECRET", "supersecret")
    monkeypatch.setenv("TEST_GHL_API_KEY", "ghl-test")
    monkeypatch.setenv("TEST_SMARTLEAD_API_KEY", "sl-test")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/X/Y/Z")
    monkeypatch.setenv("REPLY_ROUTER_CLIENTS_DIR", str(clients_dir))
    from api.index import app
    return TestClient(app)


def test_replies_endpoint_dispatches_open_event(monkeypatch, webhook_client):
    """Sending an EMAIL_OPEN payload to /replies routes through handle_open."""
    from unittest.mock import patch
    with patch("api.index.GHLClient") as MockGHL:
        ghl = MockGHL.return_value
        ghl.get_contacts_by_email.return_value = [{
            "id": "ct_1", "email": "pat@acme.com",
            "customFields": [{"id": "cf_open_n", "value": "5"}],
        }]
        resp = webhook_client.post(
            "/v1/clients/test_client/replies?secret=supersecret",
            json={"event_type": "EMAIL_OPEN", "to_email": "pat@acme.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "processed"
        assert body["event"] == "open"
        assert body["open_count"] == 6


def test_replies_endpoint_unknown_event_does_not_use_event_handler(monkeypatch, webhook_client):
    """Empty/UNKNOWN payload should NOT trigger the event-handler path.

    Verifies routing by patching handle_non_reply_event and asserting it was NOT called.
    """
    from unittest.mock import patch
    with patch("api.index.handle_non_reply_event") as mock_handler:
        with patch("api.index.process_reply") as mock_process:
            mock_process.return_value = MagicMock(
                http_status=200, to_response=lambda: {"status": "ok"}
            )
            webhook_client.post(
                "/v1/clients/test_client/replies?secret=supersecret",
                json={"to_email": "external@whatever.com"},
            )
            mock_handler.assert_not_called()
            mock_process.assert_called_once()


def test_replies_endpoint_explicit_unknown_event_type_returns_200_ignored(webhook_client):
    """Explicit but-unhandled event_type → 200 with status=ignored. Never 500.

    Smartlead's circuit breaker pauses webhook delivery after 4 consecutive 5xx
    (reference_smartlead_webhook_shape memory). Future event types must NOT crash.
    """
    resp = webhook_client.post(
        "/v1/clients/test_client/replies?secret=supersecret",
        json={"event_type": "FUTURE_SMARTLEAD_EVENT_WE_HAVE_NO_HANDLER_FOR"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert "unhandled event_type" in resp.json()["reason"]


def test_replies_endpoint_malformed_reply_payload_returns_200_ignored(webhook_client):
    """Malformed reply-shape payload → 200 ignored, NOT 500.

    Same circuit-breaker reason as above. We never want a malformed webhook to 5xx.
    """
    from unittest.mock import patch
    # Make ReplyPayload.from_smartlead_webhook raise (simulating a malformed payload)
    with patch("api.index.ReplyPayload.from_smartlead_webhook",
               side_effect=KeyError("missing required field")):
        resp = webhook_client.post(
            "/v1/clients/test_client/replies?secret=supersecret",
            json={"some_garbage": "field"},  # No event_type → falls through to reply path
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert "malformed" in resp.json()["reason"]
