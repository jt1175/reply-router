"""Tests for the GHL stage-change → Smartlead pause sync endpoint."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


PAUSE_STAGE_A = "stage_closed_won"
PAUSE_STAGE_B = "stage_closed_lost"
NON_PAUSE_STAGE = "stage_walkthrough_scheduled"


@pytest.fixture
def stage_change_client(monkeypatch, tmp_path):
    """FastAPI TestClient with pause_on_stage_ids configured."""
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
                  "pending_reply_email_stats_id": "cf_resid"}},
        "smartlead": {"api_key_env": "TEST_SMARTLEAD_API_KEY", "campaign_ids": ["camp_1", "camp_2"]},
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
          "pricing_response": "depends.", "booking_link": "https://x/PLACEHOLDER"},
        "pause_on_stage_ids": ["stage_closed_won", "stage_closed_lost"]
    }""")
    monkeypatch.setenv("TEST_ROUTER_SECRET", "supersecret")
    monkeypatch.setenv("TEST_GHL_API_KEY", "ghl-test")
    monkeypatch.setenv("TEST_SMARTLEAD_API_KEY", "sl-test")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/services/X/Y/Z")
    monkeypatch.setenv("REPLY_ROUTER_CLIENTS_DIR", str(clients_dir))
    from api.index import app
    return TestClient(app)


# ─── Auth ───

def test_stage_change_401_missing_secret(stage_change_client):
    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change",
        json={"contactId": "ct_1", "currentStage": PAUSE_STAGE_A},
    )
    assert resp.status_code == 401


def test_stage_change_401_wrong_secret(stage_change_client):
    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change?secret=wrong",
        json={"contactId": "ct_1", "currentStage": PAUSE_STAGE_A},
    )
    assert resp.status_code == 401


# ─── Payload-shape edge cases ───

def test_stage_change_200_ignored_when_missing_contactId(stage_change_client):
    """Per spec: return 200 even on malformed payloads — don't trip GHL's retry circuit."""
    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change?secret=supersecret",
        json={"currentStage": PAUSE_STAGE_A},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_stage_change_200_ignored_when_missing_currentStage(stage_change_client):
    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change?secret=supersecret",
        json={"contactId": "ct_1"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


# ─── Stage-not-in-pause-list — no smartlead call ───

@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_stage_change_ignored_when_stage_not_in_pause_list(
    MockGHL, MockSmartlead, stage_change_client
):
    """Non-pause stage (e.g. Walkthrough Scheduled) → ignored, no smartlead call."""
    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change?secret=supersecret",
        json={"contactId": "ct_1", "currentStage": NON_PAUSE_STAGE},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ignored"
    assert "not in pause list" in body["reason"]
    MockSmartlead.assert_not_called()
    MockGHL.assert_not_called()


# ─── Happy path: stage in pause list → smartlead pause called ───

@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_stage_change_pauses_lead_in_all_campaigns(
    MockGHL, MockSmartlead, stage_change_client
):
    ghl = MockGHL.return_value
    ghl.get_contact_by_id.return_value = {
        "id": "ct_1", "email": "pat@acme.com", "firstName": "Pat",
        "companyName": "Acme",
    }
    sl = MockSmartlead.return_value
    sl.find_lead_by_email.return_value = {"id": 9001, "email": "pat@acme.com"}

    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change?secret=supersecret",
        json={"contactId": "ct_1", "currentStage": PAUSE_STAGE_A},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"
    assert body["smartlead_lead_id"] == 9001
    # Both configured campaigns paused (camp_1 and camp_2)
    assert set(body["paused_in_campaigns"]) == {"camp_1", "camp_2"}
    assert sl.pause_lead.call_count == 2
    # Audit note written
    ghl.add_note.assert_called_once()


@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_stage_change_ignored_when_contact_has_no_email(
    MockGHL, MockSmartlead, stage_change_client
):
    """Contact with no email → can't look up smartlead lead → ignored, no failure."""
    MockGHL.return_value.get_contact_by_id.return_value = {
        "id": "ct_1", "email": "", "firstName": "Pat",
    }
    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change?secret=supersecret",
        json={"contactId": "ct_1", "currentStage": PAUSE_STAGE_B},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert "no email" in resp.json()["reason"]
    MockSmartlead.return_value.pause_lead.assert_not_called()


@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_stage_change_ignored_when_lead_not_found_in_smartlead(
    MockGHL, MockSmartlead, stage_change_client
):
    MockGHL.return_value.get_contact_by_id.return_value = {
        "id": "ct_1", "email": "missing@nowhere.com",
    }
    MockSmartlead.return_value.find_lead_by_email.return_value = None
    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change?secret=supersecret",
        json={"contactId": "ct_1", "currentStage": PAUSE_STAGE_A},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ignored"
    assert "not found in smartlead" in body["reason"]
    MockSmartlead.return_value.pause_lead.assert_not_called()


@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_stage_change_partial_failure_continues_other_campaigns(
    MockGHL, MockSmartlead, stage_change_client
):
    """If pause fails in campaign A, still try campaign B; report both states."""
    from reply_router.smartlead_client import SmartleadError
    MockGHL.return_value.get_contact_by_id.return_value = {
        "id": "ct_1", "email": "pat@acme.com",
    }
    sl = MockSmartlead.return_value
    sl.find_lead_by_email.return_value = {"id": 9001}
    sl.pause_lead.side_effect = [SmartleadError("simulated 502"), None]

    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change?secret=supersecret",
        json={"contactId": "ct_1", "currentStage": PAUSE_STAGE_A},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"
    # Second campaign succeeded
    assert "camp_2" in body["paused_in_campaigns"]
    # First campaign failed and is reported
    assert any("camp_1" in e for e in body["errors"])


@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_stage_change_uses_header_secret_when_query_param_absent(
    MockGHL, MockSmartlead, stage_change_client
):
    """X-Router-Secret header should also work (parity with /replies endpoint)."""
    MockGHL.return_value.get_contact_by_id.return_value = {
        "id": "ct_1", "email": "pat@acme.com",
    }
    MockSmartlead.return_value.find_lead_by_email.return_value = {"id": 9001}
    resp = stage_change_client.post(
        "/v1/clients/test_client/ghl-stage-change",
        json={"contactId": "ct_1", "currentStage": PAUSE_STAGE_A},
        headers={"X-Router-Secret": "supersecret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"
