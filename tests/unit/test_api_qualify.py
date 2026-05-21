"""Tests for the qualification booking flow endpoints in api/index.py."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def qualify_client(monkeypatch, tmp_path):
    """FastAPI TestClient with a config that has the qualification flow fully wired."""
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    test_cfg = clients_dir / "test_client.json"
    test_cfg.write_text("""{
        "client_id": "test_client", "client_display_name": "Test Cleaning Co",
        "ghl": {"sub_account_id": "loc_xyz", "api_key_env": "TEST_GHL_API_KEY",
                "pipeline_id": "p", "calendar_id": "cal_real_xyz",
                "custom_field_ids": {
                  "reply_classification": "cf_class", "reply_received_at": "cf_at",
                  "contract_end_date": "cf_end", "nurture_bucket": "cf_nb",
                  "last_processed_smartlead_message_ids": "cf_roll",
                  "currently_processing_smartlead_message_id": "cf_lock",
                  "pending_draft_token": "cf_tok", "pending_draft_text": "cf_dtext",
                  "pending_draft_created_at": "cf_dat",
                  "pending_reply_message_id": "cf_rmid",
                  "pending_reply_email_stats_id": "cf_resid",
                  "qualification_form_answers": "cf_qfa",
                  "qualification_result": "cf_qr",
                  "qualification_submitted_at": "cf_qsa"}},
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
          "company_name": "Test Cleaning Co", "service_area": "Twin Cities",
          "services_offered": ["office cleaning"], "services_not_offered": ["restaurants"],
          "pricing_response": "depends on space.", "booking_link": "https://example.com/qualify/{contact_id}?token={token}"},
        "qualification_rubric": "Score the prospect's fit for commercial cleaning. Qualify if in scope, reject if restaurant or out of geography.",
        "qualify_pipeline_stage_id": "stage_walkthrough_scheduled",
        "gray_zone_pipeline_stage_id": "stage_manual_review",
        "reject_pipeline_stage_id": "stage_closed_lost"
    }""")
    monkeypatch.setenv("TEST_ROUTER_SECRET", "qsecret")
    monkeypatch.setenv("TEST_GHL_API_KEY", "ghl-test")
    monkeypatch.setenv("TEST_SMARTLEAD_API_KEY", "sl-test")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/services/X/Y/Z")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    monkeypatch.setenv("REPLY_ROUTER_CLIENTS_DIR", str(clients_dir))
    from api.index import app
    return TestClient(app)


@pytest.fixture
def unconfigured_client(monkeypatch, tmp_path):
    """Config with TBD calendar_id — qualification endpoints should 503."""
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    test_cfg = clients_dir / "test_client.json"
    test_cfg.write_text("""{
        "client_id": "test_client", "client_display_name": "Test Co",
        "ghl": {"sub_account_id": "loc", "api_key_env": "TEST_GHL_API_KEY",
                "pipeline_id": "p", "calendar_id": "TBD_GHL_CALENDAR_ID",
                "custom_field_ids": {
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
          "company_name": "Test Co", "service_area": "Area",
          "services_offered": [], "services_not_offered": [],
          "pricing_response": "depends.", "booking_link": "https://example.com/PLACEHOLDER"}
    }""")
    monkeypatch.setenv("TEST_ROUTER_SECRET", "qsecret")
    monkeypatch.setenv("REPLY_ROUTER_CLIENTS_DIR", str(clients_dir))
    from api.index import app
    return TestClient(app)


def _make_contact(contact_id: str = "ct_1") -> dict:
    return {
        "id": contact_id,
        "firstName": "Pat", "companyName": "Acme", "email": "pat@acme.com",
        "customFields": [],
    }


def _make_url_token(contact_id: str, secret: str = "qsecret") -> str:
    from reply_router.qualifier import url_token
    return url_token(secret, contact_id, int(time.time()))


# ─── GET /qualify/{contact_id} ───

def test_get_form_returns_503_when_unconfigured(unconfigured_client):
    resp = unconfigured_client.get(
        "/v1/clients/test_client/qualify/ct_1?token=any"
    )
    assert resp.status_code == 503
    assert "not yet available" in resp.text


def test_get_form_returns_403_on_invalid_url_token(qualify_client):
    resp = qualify_client.get(
        "/v1/clients/test_client/qualify/ct_1?token=invalid"
    )
    assert resp.status_code == 403
    assert "expired or is invalid" in resp.text


@patch("api.index.GHLClient")
def test_get_form_returns_404_when_contact_missing(MockGHL, qualify_client):
    MockGHL.return_value.get_contact_by_id.return_value = None
    token = _make_url_token("ct_1")
    resp = qualify_client.get(
        f"/v1/clients/test_client/qualify/ct_1?token={token}"
    )
    assert resp.status_code == 404


@patch("api.index.GHLClient")
def test_get_form_renders_with_valid_token(MockGHL, qualify_client):
    MockGHL.return_value.get_contact_by_id.return_value = _make_contact()
    token = _make_url_token("ct_1")
    resp = qualify_client.get(
        f"/v1/clients/test_client/qualify/ct_1?token={token}"
    )
    assert resp.status_code == 200
    assert "Pat" in resp.text  # firstName rendered
    assert "Acme" in resp.text  # companyName rendered
    assert 'name="building_size_sqft"' in resp.text
    assert 'name="csrf"' in resp.text


# ─── POST /qualify/{contact_id} ───

def _post_form_with_valid_csrf(client, contact_id, secret, **form_overrides):
    """Helper that builds and POSTs a form with valid URL token + CSRF."""
    from reply_router.qualifier import form_csrf, url_token
    iat = int(time.time())
    url_tok = url_token(secret, contact_id, iat)
    csrf = form_csrf(secret, contact_id, iat)
    body = {
        "token": url_tok,
        "csrf": csrf,
        "form_issued_at_unix": str(iat),
        "building_size_sqft": "25000",
        "building_type": "office",
        "current_vendor_status": "no_vendor",
        "decision_timeline": "this_month",
        "monthly_budget_range": "2k_to_5k",
        "best_phone": "555-5555",
        "additional_context": "",
    }
    body.update(form_overrides)
    return client.post(
        f"/v1/clients/test_client/qualify/{contact_id}", data=body
    )


def test_post_form_returns_403_on_csrf_fail(qualify_client):
    iat = int(time.time())
    from reply_router.qualifier import url_token
    url_tok = url_token("qsecret", "ct_1", iat)
    resp = qualify_client.post(
        "/v1/clients/test_client/qualify/ct_1",
        data={
            "token": url_tok, "csrf": "bogus", "form_issued_at_unix": str(iat),
            "building_size_sqft": "10000", "building_type": "office",
        },
    )
    assert resp.status_code == 403


@patch("api.index.classify_form")
@patch("api.index.GHLClient")
def test_post_form_qualify_renders_slot_picker(MockGHL, mock_classify, qualify_client):
    """qualify decision → fetches slots → renders slot-picker page."""
    ghl = MockGHL.return_value
    ghl.get_contact_by_id.return_value = _make_contact()
    ghl.get_calendar_free_slots.return_value = {
        "2026-05-26": {"slots": ["2026-05-26T10:00:00-05:00", "2026-05-26T10:30:00-05:00"]},
        "2026-05-27": {"slots": ["2026-05-27T14:00:00-05:00"]},
    }
    mock_classify.return_value = {
        "decision": "qualify", "deal_type": "mid_market",
        "confidence": "high", "reasoning": "fits the sweet spot",
    }
    resp = _post_form_with_valid_csrf(qualify_client, "ct_1", "qsecret")
    assert resp.status_code == 200
    assert "Pick a time" in resp.text or "walkthrough" in resp.text.lower()
    # Slot buttons rendered
    assert "2026-05-26T10:00:00-05:00" in resp.text
    # GHL update_contact called with the qualification field IDs
    ghl.update_contact.assert_called_once()
    write_call = ghl.update_contact.call_args
    written_fields = write_call.kwargs.get("custom_fields") or write_call.args[1] if len(write_call.args) > 1 else {}
    if not written_fields and "custom_fields" in write_call.kwargs:
        written_fields = write_call.kwargs["custom_fields"]
    assert "cf_qfa" in written_fields
    assert written_fields.get("cf_qr") == "qualify"


@patch("api.index._slack_post")
@patch("api.index.classify_form")
@patch("api.index.GHLClient")
def test_post_form_gray_zone_moves_stage_and_slacks(
    MockGHL, mock_classify, mock_slack, qualify_client
):
    """gray_zone decision → moves to gray stage + posts to Slack + renders neutral page."""
    ghl = MockGHL.return_value
    ghl.get_contact_by_id.return_value = _make_contact()
    mock_classify.return_value = {
        "decision": "gray_zone", "deal_type": "mid_market",
        "confidence": "medium", "reasoning": "ambiguous timing",
    }
    resp = _post_form_with_valid_csrf(qualify_client, "ct_1", "qsecret")
    assert resp.status_code == 200
    ghl.move_to_pipeline_stage.assert_called_once()
    move_call = ghl.move_to_pipeline_stage.call_args
    # stage_id arg should be the gray_zone stage configured in the fixture
    assert "stage_manual_review" in str(move_call)
    mock_slack.assert_called_once()


@patch("api.index.classify_form")
@patch("api.index.GHLClient")
def test_post_form_reject_moves_stage_and_renders_reject_page(
    MockGHL, mock_classify, qualify_client
):
    ghl = MockGHL.return_value
    ghl.get_contact_by_id.return_value = _make_contact()
    mock_classify.return_value = {
        "decision": "reject", "deal_type": "disqualified",
        "confidence": "high", "reasoning": "restaurant industry — out of scope",
    }
    resp = _post_form_with_valid_csrf(qualify_client, "ct_1", "qsecret", building_type="retail")
    assert resp.status_code == 200
    assert "not the right fit" in resp.text or "Thanks for reaching out" in resp.text
    ghl.move_to_pipeline_stage.assert_called_once()
    assert "stage_closed_lost" in str(ghl.move_to_pipeline_stage.call_args)


# ─── POST /qualify/{contact_id}/book ───

def _post_book(client, contact_id, secret, slot_iso, **overrides):
    from reply_router.qualifier import form_csrf, url_token
    iat = int(time.time())
    url_tok = url_token(secret, contact_id, iat)
    csrf = form_csrf(secret, contact_id, iat)
    body = {
        "token": url_tok, "csrf": csrf, "form_issued_at_unix": str(iat),
        "selected_slot_iso": slot_iso,
    }
    body.update(overrides)
    return client.post(
        f"/v1/clients/test_client/qualify/{contact_id}/book", data=body
    )


def test_post_book_403_on_csrf_fail(qualify_client):
    iat = int(time.time())
    from reply_router.qualifier import url_token
    url_tok = url_token("qsecret", "ct_1", iat)
    resp = qualify_client.post(
        "/v1/clients/test_client/qualify/ct_1/book",
        data={
            "token": url_tok, "csrf": "wrong", "form_issued_at_unix": str(iat),
            "selected_slot_iso": "2026-05-26T10:00:00-05:00",
        },
    )
    assert resp.status_code == 403


@patch("api.index.GHLClient")
def test_post_book_creates_appointment_moves_stage_renders_confirmation(
    MockGHL, qualify_client
):
    ghl = MockGHL.return_value
    ghl.get_contact_by_id.return_value = _make_contact()
    ghl.create_appointment.return_value = {"id": "appt_1", "startTime": "2026-05-26T10:00:00-05:00"}
    resp = _post_book(qualify_client, "ct_1", "qsecret", "2026-05-26T10:00:00-05:00")
    assert resp.status_code == 200
    assert "confirmed" in resp.text.lower() or "Walkthrough confirmed" in resp.text
    ghl.create_appointment.assert_called_once()
    create_kwargs = ghl.create_appointment.call_args.kwargs
    assert create_kwargs["calendar_id"] == "cal_real_xyz"
    assert create_kwargs["contact_id"] == "ct_1"
    assert create_kwargs["start_time_iso"] == "2026-05-26T10:00:00-05:00"
    ghl.move_to_pipeline_stage.assert_called_once()
    assert "stage_walkthrough_scheduled" in str(ghl.move_to_pipeline_stage.call_args)


@patch("api.index.GHLClient")
def test_post_book_502_on_appointment_failure(MockGHL, qualify_client):
    ghl = MockGHL.return_value
    ghl.get_contact_by_id.return_value = _make_contact()
    ghl.create_appointment.side_effect = RuntimeError("GHL create_appointment failed: status=409")
    resp = _post_book(qualify_client, "ct_1", "qsecret", "2026-05-26T10:00:00-05:00")
    assert resp.status_code == 502
    # Apostrophe is HTML-escaped (`&#x27;t`), so match a substring without one
    assert "lock in that time" in resp.text


def test_post_book_400_on_missing_slot(qualify_client):
    resp = _post_book(qualify_client, "ct_1", "qsecret", "")
    # CSRF passes (helper used), but missing slot → 400
    assert resp.status_code == 400


# ─── URL token verification edge cases ───

def test_url_token_expired_rejected():
    """Tokens older than 14 days must fail verify_url_token."""
    from reply_router.qualifier import url_token, verify_url_token, URL_TOKEN_TTL_SEC
    secret = "qsecret"
    old_iat = int(time.time()) - URL_TOKEN_TTL_SEC - 60
    tok = url_token(secret, "ct_1", old_iat)
    assert not verify_url_token(secret, "ct_1", tok)


def test_url_token_wrong_contact_rejected():
    """A token signed for contact_id A must not validate for contact_id B."""
    from reply_router.qualifier import url_token, verify_url_token
    secret = "qsecret"
    tok = url_token(secret, "ct_A", int(time.time()))
    assert not verify_url_token(secret, "ct_B", tok)


def test_url_token_happy_path():
    from reply_router.qualifier import url_token, verify_url_token
    secret = "qsecret"
    tok = url_token(secret, "ct_1", int(time.time()))
    assert verify_url_token(secret, "ct_1", tok)
