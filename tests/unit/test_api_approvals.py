"""Tests for api/approvals.py — approval UI and token consumption."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def approvals_client(monkeypatch, tmp_path):
    """FastAPI TestClient against api.index.app with a minimal test config."""
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
        "smartlead": {"api_key_env": "TEST_SMARTLEAD_API_KEY", "campaign_ids": ["c1"]},
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
    monkeypatch.setenv("TEST_ROUTER_SECRET", "csrfsecret")
    monkeypatch.setenv("TEST_GHL_API_KEY", "ghl-test")
    monkeypatch.setenv("TEST_SMARTLEAD_API_KEY", "sl-test")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/services/X/Y/Z")
    monkeypatch.setenv("REPLY_ROUTER_CLIENTS_DIR", str(clients_dir))
    from api.index import app
    return TestClient(app)


def _fresh_draft_contact(token="tok_abc", with_threading=True):
    """Build a contact dict whose customFields include the draft + (optionally) threading params."""
    fields = [
        {"id": "cf_tok", "value": token},
        {"id": "cf_dtext", "value": "Hello, here's a quick call link: https://x/book"},
        {"id": "cf_dat", "value": datetime.now(timezone.utc).isoformat()},
        {"id": "cf_class", "value": "interested"},
    ]
    if with_threading:
        fields.append({"id": "cf_rmid", "value": "<msg-abc@mail.gmail.com>"})
        fields.append({"id": "cf_resid", "value": "stats_123"})
    return {"id": "ct_1", "firstName": "Pat", "companyName": "Acme",
            "email": "pat@acme.com", "customFields": fields}


# §7.3 #11 - full approval flow
@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_shadow_mode_approval_flow_full(MockGHL, MockSmartlead, approvals_client):
    """GET form → POST /send → Smartlead called with threading params → token cleared → 2nd GET → 410."""
    ghl_instance = MockGHL.return_value
    ghl_instance.search_contacts_by_custom_field.side_effect = [
        [_fresh_draft_contact()],   # GET form lookup
        [_fresh_draft_contact()],   # POST /send re-lookup
        [],                          # Second GET → empty (consumed)
    ]

    # GET form
    resp = approvals_client.get("/v1/clients/test_client/approvals/tok_abc")
    assert resp.status_code == 200
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert 'name="csrf"' in resp.text
    assert "Hello, here's a quick call link" in resp.text

    # Extract csrf + form_issued_at from rendered HTML
    import re
    csrf = re.search(r'name="csrf"\s+value="([^"]+)"', resp.text).group(1)
    iat = re.search(r'name="form_issued_at_unix"\s+value="(\d+)"', resp.text).group(1)

    # POST /send
    sml_instance = MockSmartlead.return_value
    resp = approvals_client.post(
        "/v1/clients/test_client/approvals/tok_abc/send",
        data={"draft_text": "Hello, here's a quick call link: https://x/book",
              "csrf": csrf, "form_issued_at_unix": iat},
    )
    assert resp.status_code == 200, resp.text
    sml_instance.send_reply_in_thread.assert_called_once()
    # Verify threading params were passed
    call_kwargs = sml_instance.send_reply_in_thread.call_args.kwargs
    assert call_kwargs["reply_message_id"] == "<msg-abc@mail.gmail.com>"
    assert call_kwargs["email_stats_id"] == "stats_123"
    # Token cleared
    cleared_call = [c for c in ghl_instance.update_contact.call_args_list
                    if c.kwargs.get("custom_fields", {}).get("cf_tok") == ""]
    assert cleared_call, "token field should have been cleared"

    # Second GET → 410
    resp2 = approvals_client.get("/v1/clients/test_client/approvals/tok_abc")
    assert resp2.status_code == 410


# §7.3 #12 - CSRF required
@patch("api.index.GHLClient")
def test_shadow_mode_csrf_required(MockGHL, approvals_client):
    """POST /send without csrf → 403."""
    resp = approvals_client.post(
        "/v1/clients/test_client/approvals/tok_abc/send",
        data={"draft_text": "hello"},  # no csrf, no form_issued_at_unix
    )
    assert resp.status_code == 403


@patch("api.index.GHLClient")
def test_post_send_with_stale_csrf_returns_403(MockGHL, approvals_client):
    """form_issued_at_unix older than 1h → 403."""
    from reply_router.approvals import csrf_token
    stale_iat = int(time.time()) - 7200  # 2 hours ago
    sig = csrf_token("csrfsecret", "tok_abc", stale_iat)
    resp = approvals_client.post(
        "/v1/clients/test_client/approvals/tok_abc/send",
        data={"draft_text": "hello", "csrf": sig, "form_issued_at_unix": str(stale_iat)},
    )
    assert resp.status_code == 403


def test_get_unknown_token_returns_410(approvals_client):
    with patch("api.index.GHLClient") as MockGHL:
        MockGHL.return_value.search_contacts_by_custom_field.return_value = []
        resp = approvals_client.get("/v1/clients/test_client/approvals/nonsuch")
        assert resp.status_code == 410


def test_get_expired_token_returns_410_and_clears_fields(approvals_client):
    """Expired (>7d) token → 410, all 5 pending_* fields cleared."""
    old_iso = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with patch("api.index.GHLClient") as MockGHL:
        ghl_inst = MockGHL.return_value
        ghl_inst.search_contacts_by_custom_field.return_value = [{
            "id": "ct_old", "customFields": [
                {"id": "cf_tok", "value": "tok_old"},
                {"id": "cf_dtext", "value": "very old draft"},
                {"id": "cf_dat", "value": old_iso},
                {"id": "cf_rmid", "value": "<old-msg@x>"},
                {"id": "cf_resid", "value": "stats_old"},
            ]
        }]
        resp = approvals_client.get("/v1/clients/test_client/approvals/tok_old")
        assert resp.status_code == 410
        ghl_inst.update_contact.assert_called()
        cf = ghl_inst.update_contact.call_args.kwargs["custom_fields"]
        assert cf == {"cf_tok": "", "cf_dtext": "", "cf_dat": "", "cf_rmid": "", "cf_resid": ""}


@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_post_send_missing_threading_params_returns_409(MockGHL, MockSml, approvals_client):
    """Defensive: if pending_reply_message_id or email_stats_id is missing, return 409
    rather than send a non-threaded reply. (iter-2 blocker #1 safety net.)"""
    MockGHL.return_value.search_contacts_by_custom_field.return_value = [
        _fresh_draft_contact(with_threading=False)
    ]
    from reply_router.approvals import csrf_token
    iat = int(time.time())
    sig = csrf_token("csrfsecret", "tok_abc", iat)
    resp = approvals_client.post(
        "/v1/clients/test_client/approvals/tok_abc/send",
        data={"draft_text": "hello", "csrf": sig, "form_issued_at_unix": str(iat)},
    )
    assert resp.status_code == 409
    MockSml.return_value.send_reply_in_thread.assert_not_called()


@patch("api.index.SmartleadClient")
@patch("api.index.GHLClient")
def test_discard_clears_token_no_smartlead(MockGHL, MockSML, approvals_client):
    MockGHL.return_value.search_contacts_by_custom_field.return_value = [_fresh_draft_contact()]
    from reply_router.approvals import csrf_token
    iat = int(time.time())
    sig = csrf_token("csrfsecret", "tok_abc", iat)
    resp = approvals_client.post(
        "/v1/clients/test_client/approvals/tok_abc/discard",
        data={"csrf": sig, "form_issued_at_unix": str(iat)},
    )
    assert resp.status_code == 200
    MockSML.return_value.send_reply_in_thread.assert_not_called()
    assert any(
        c.kwargs.get("custom_fields", {}).get("cf_tok") == ""
        for c in MockGHL.return_value.update_contact.call_args_list
    )
