"""Tests for api/replies.py — webhook handler integration tests."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    """Spin up the FastAPI app with a minimal test config in tmp_path/clients/."""
    # Write a minimal valid client config
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
        "smartlead": {"api_key_env": "TEST_SL_API_KEY", "campaign_ids": ["c1"]},
        "slack": {"incoming_webhook_url_env": "TEST_SLACK_URL"},
        "auth": {"router_secret_env": "TEST_ROUTER_SECRET"},
        "sending_inboxes": ["sender@test.invalid"],
        "monitoring_until": "2099-01-01",
        "classification_actions": {
          "unsubscribe":  {"auto_send": true, "min_confidence": "low", "slack_notify": false, "pipeline_stage_id": "s1"},
          "wrong_person": {"auto_send": true, "min_confidence": "medium", "slack_notify": true, "pipeline_stage_id": "s2"},
          "interested":   {"auto_send": false, "min_confidence": "high", "slack_notify": true, "pipeline_stage_id": "s3"},
          "not_now":      {"auto_send": false, "min_confidence": "medium", "slack_notify": true, "pipeline_stage_id": "s4"},
          "info_request": {"auto_send": false, "min_confidence": "high", "slack_notify": true, "pipeline_stage_id": "s5"},
          "objection":    {"auto_send": false, "min_confidence": "high", "slack_notify": true, "pipeline_stage_id": "s5"}},
        "business_context": {
          "company_name": "Test Co", "service_area": "Test Area",
          "services_offered": [], "services_not_offered": [],
          "pricing_response": "depends.", "booking_link": "https://example.com/book"}
    }""")

    monkeypatch.setenv("TEST_ROUTER_SECRET", "supersecret")
    monkeypatch.setenv("REPLY_ROUTER_CLIENTS_DIR", str(clients_dir))
    from api.replies import app
    return TestClient(app)


def test_401_on_missing_secret(app_client):
    resp = app_client.post("/v1/clients/test_client/replies", json={})
    assert resp.status_code == 401


def test_401_on_wrong_secret(app_client):
    resp = app_client.post(
        "/v1/clients/test_client/replies",
        json={},
        headers={"X-Router-Secret": "wrong"},
    )
    assert resp.status_code == 401


def test_500_on_unknown_client(app_client):
    resp = app_client.post(
        "/v1/clients/no_such_client/replies",
        json={},
        headers={"X-Router-Secret": "supersecret"},
    )
    assert resp.status_code == 500
    assert "config_load_failed" in resp.text


def test_200_on_valid_secret_with_minimal_payload(app_client):
    """With the correct secret, the handler proceeds past auth. The from_email IS in
    sending_inboxes so the loop check short-circuits at the first step and returns 200."""
    resp = app_client.post(
        "/v1/clients/test_client/replies",
        json={"message_id": "m1", "from_email": "sender@test.invalid",
              "lead_email": "p@example.com", "campaign_id": "c1", "reply_text": "hi"},
        headers={"X-Router-Secret": "supersecret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored_self"
