"""Unit tests for reply_router.orchestrator."""
from __future__ import annotations

import json
import time

import pytest
from freezegun import freeze_time
from unittest.mock import MagicMock, patch

from reply_router.orchestrator import _normalize_email, process_reply, ReplyPayload
from reply_router.ghl_client import MultiContactResolution
from reply_router.dedupe import SoftLockState, hash16


@pytest.mark.parametrize("raw,expected", [
    ("sarah.jones@clearfacilitymn.com", "sarah.jones@clearfacilitymn.com"),
    ("Sarah.Jones@ClearFacilityMN.com", "sarah.jones@clearfacilitymn.com"),
    ("sarah.jones+test@clearfacilitymn.com", "sarah.jones@clearfacilitymn.com"),
    ('"Sarah Jones" <sarah.jones@clearfacilitymn.com>', "sarah.jones@clearfacilitymn.com"),
    ("Sarah Jones <SARAH.JONES@CLEARFACILITYMN.COM>", "sarah.jones@clearfacilitymn.com"),
    ("  sarah.jones@clearfacilitymn.com  ", "sarah.jones@clearfacilitymn.com"),
    ("", ""),
    ("not an email", ""),
])
def test_normalize_email_handles_all_documented_variants(raw, expected):
    assert _normalize_email(raw) == expected


# ---------------------------------------------------------------------------
# Helpers shared across §4.1 step 5a-5e tests
# ---------------------------------------------------------------------------

def _stub_config():
    cfg = MagicMock()
    cfg.client_id = "test"
    cfg.sending_inboxes = ["us@test.invalid"]
    cfg.ghl.custom_field_ids = {
        "last_processed_smartlead_message_ids": "cf_roll",
        "currently_processing_smartlead_message_id": "cf_lock",
        "reply_classification": "cf_class", "reply_received_at": "cf_at",
        "contract_end_date": "cf_end", "nurture_bucket": "cf_nb",
        "pending_draft_token": "cf_tok", "pending_draft_text": "cf_dtext",
        "pending_draft_created_at": "cf_dat",
        "pending_reply_message_id": "cf_rmid",
        "pending_reply_email_stats_id": "cf_resid",
    }
    cfg.ghl.sub_account_id = "loc"
    cfg.ghl.pipeline_id = "p"
    cfg.ghl.api_key_env = "TEST_GHL_API_KEY"
    cfg.smartlead.campaign_ids = ["c1"]
    cfg.slack.incoming_webhook_url_env = "TEST_SLACK_URL"
    return cfg


def _payload(mid="m_new", from_email="prospect@example.com"):
    return ReplyPayload(
        message_id=mid, from_email=from_email,
        lead_email="prospect@example.com",
        campaign_id="c1", reply_text="hi",
    )


# ---------------------------------------------------------------------------
# §7.3 #6 — rolling dedupe list hit → "duplicate"
# ---------------------------------------------------------------------------

def test_duplicate_message_returns_duplicate_status(monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    ghl_mock = MagicMock()
    contact = {
        "id": "ct_1",
        "customFields": [
            {"id": "cf_roll", "value": json.dumps([hash16("m_dup")])},
        ],
    }
    ghl_mock.resolve_contact_by_email.return_value = (contact, MultiContactResolution.SINGLE)
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        result = process_reply(_stub_config(), _payload(mid="m_dup"))
    assert result.status == "duplicate"
    assert result.http_status == 200
    # Did NOT acquire soft lock
    ghl_mock.update_contact.assert_not_called()


# ---------------------------------------------------------------------------
# §7.3 #7 — soft lock IN_FLIGHT → "in_flight_elsewhere"
# ---------------------------------------------------------------------------

def test_soft_lock_in_flight_returns_in_flight_status(monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    ghl_mock = MagicMock()
    # Fresh lock for same message_id
    lock_value = f"m_inflight:{int(time.time())}"
    contact = {
        "id": "ct_2",
        "customFields": [
            {"id": "cf_lock", "value": lock_value},
        ],
    }
    ghl_mock.resolve_contact_by_email.return_value = (contact, MultiContactResolution.SINGLE)
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        result = process_reply(_stub_config(), _payload(mid="m_inflight"))
    assert result.status == "in_flight_elsewhere"
    assert result.http_status == 200
    # Did NOT acquire a new soft lock
    ghl_mock.update_contact.assert_not_called()


# ---------------------------------------------------------------------------
# §7.3 #8 — soft lock STALE (>600 s) → orchestrator proceeds, eventually NotImplementedError
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.classify")
def test_soft_lock_stale_proceeds_to_classification(mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    # Return 'unknown' so GHL writes are skipped — _handle_unknown + mark_complete runs
    mock_classify.return_value = {"classification": "unknown", "confidence": "low",
                                  "suggested_followup_date_iso": None, "reasoning": "n/a"}
    ghl_mock = MagicMock()
    # Lock timestamp is 700 s ago — stale
    stale_ts = int(time.time()) - 700
    lock_value = f"m_stale:{stale_ts}"
    contact = {
        "id": "ct_3",
        "customFields": [
            {"id": "cf_lock", "value": lock_value},
        ],
    }
    ghl_mock.resolve_contact_by_email.return_value = (contact, MultiContactResolution.SINGLE)
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        result = process_reply(_stub_config(), _payload(mid="m_stale"))
    assert result.status == "processed"
    # Soft lock was acquired (overwritten); mark_complete also called update_contact
    ghl_mock.update_contact.assert_called()


# ---------------------------------------------------------------------------
# §7.3 #18 — no existing contact → CREATED_SKELETON → acquires lock, then NotImplementedError
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.classify")
def test_skeleton_contact_created_when_no_match(mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    mock_classify.return_value = {"classification": "unknown", "confidence": "low",
                                  "suggested_followup_date_iso": None, "reasoning": "n/a"}
    ghl_mock = MagicMock()
    skeleton_contact = {
        "id": "ct_skel",
        "email": "prospect@example.com",
        "customFields": [],
    }
    ghl_mock.resolve_contact_by_email.return_value = (
        skeleton_contact, MultiContactResolution.CREATED_SKELETON
    )
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        result = process_reply(_stub_config(), _payload(mid="m_skel"))
    assert result.status == "processed"
    # Soft lock was acquired; mark_complete also called update_contact
    ghl_mock.update_contact.assert_called()


# ---------------------------------------------------------------------------
# §7.3 #17 setup — RESOLVED_BY_CAMPAIGN → proceeds normally (NotImplementedError)
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.classify")
def test_single_contact_proceeds_normally(mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    mock_classify.return_value = {"classification": "unknown", "confidence": "low",
                                  "suggested_followup_date_iso": None, "reasoning": "n/a"}
    ghl_mock = MagicMock()
    contact = {
        "id": "ct_single",
        "customFields": [],
    }
    ghl_mock.resolve_contact_by_email.return_value = (contact, MultiContactResolution.RESOLVED_BY_CAMPAIGN)
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        result = process_reply(_stub_config(), _payload(mid="m_single"))
    assert result.status == "processed"
    # Soft lock acquired; mark_complete also called update_contact
    ghl_mock.update_contact.assert_called()


# ---------------------------------------------------------------------------
# §7.3 #17 — AMBIGUOUS → acquires lock and proceeds (shadow-forcing in 4.1d)
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.classify")
def test_ambiguous_contact_still_acquires_lock(mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    mock_classify.return_value = {"classification": "unknown", "confidence": "low",
                                  "suggested_followup_date_iso": None, "reasoning": "n/a"}
    ghl_mock = MagicMock()
    contact = {
        "id": "ct_ambig",
        "customFields": [],
    }
    ghl_mock.resolve_contact_by_email.return_value = (contact, MultiContactResolution.AMBIGUOUS)
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        result = process_reply(_stub_config(), _payload(mid="m_ambig"))
    assert result.status == "processed"
    # Soft lock must have been acquired on the ambiguous contact; mark_complete also ran
    ghl_mock.update_contact.assert_called()
    # First call was for soft lock on "ct_ambig"
    first_call = ghl_mock.update_contact.call_args_list[0]
    assert first_call[0][0] == "ct_ambig"


# ---------------------------------------------------------------------------
# Regression — loop check still short-circuits before any GHL call
# ---------------------------------------------------------------------------

def test_loop_check_short_circuits_before_ghl(monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    ghl_mock = MagicMock()
    # from_email matches sending_inbox → loop
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        result = process_reply(_stub_config(), _payload(from_email="us@test.invalid"))
    assert result.status == "ignored_self"
    assert result.http_status == 200
    # GHL client was never even built / called
    ghl_mock.resolve_contact_by_email.assert_not_called()


# ===========================================================================
# §4.1 step 6–12 tests (Task 4.1d)
# ===========================================================================

def _stub_config_full():
    """Full config with real ClassificationAction objects — required for route()."""
    cfg = MagicMock()
    cfg.client_id = "test"
    cfg.sending_inboxes = ["us@test.invalid"]
    cfg.ghl.custom_field_ids = {
        "last_processed_smartlead_message_ids": "cf_roll",
        "currently_processing_smartlead_message_id": "cf_lock",
        "reply_classification": "cf_class", "reply_received_at": "cf_at",
        "contract_end_date": "cf_end", "nurture_bucket": "cf_nb",
        "pending_draft_token": "cf_tok", "pending_draft_text": "cf_dtext",
        "pending_draft_created_at": "cf_dat",
        "pending_reply_message_id": "cf_rmid",
        "pending_reply_email_stats_id": "cf_resid",
    }
    cfg.ghl.sub_account_id = "loc"
    cfg.ghl.pipeline_id = "p"
    cfg.ghl.api_key_env = "TEST_GHL_API_KEY"
    cfg.smartlead.campaign_ids = ["c1"]
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    cfg.slack.incoming_webhook_url_env = "TEST_SLACK_URL"
    cfg.business_context.booking_link = "https://example.com/book"
    from reply_router.config import ClassificationAction
    cfg.classification_actions = {
        "unsubscribe":  ClassificationAction(auto_send=True, min_confidence="low", slack_notify=False, pipeline_stage_id="s1"),
        "wrong_person": ClassificationAction(auto_send=True, min_confidence="medium", slack_notify=True, pipeline_stage_id="s2"),
        "interested":   ClassificationAction(auto_send=False, min_confidence="high", slack_notify=True, pipeline_stage_id="s3"),
        "not_now":      ClassificationAction(auto_send=False, min_confidence="medium", slack_notify=True, pipeline_stage_id="s4", nurture_bucket="not_now"),
        "info_request": ClassificationAction(auto_send=False, min_confidence="high", slack_notify=True, pipeline_stage_id="s5"),
        "objection":    ClassificationAction(auto_send=False, min_confidence="high", slack_notify=True, pipeline_stage_id="s5"),
    }
    return cfg


def _clean_contact(contact_id="ct_1"):
    """A contact with no rolling list hits and no soft lock."""
    return {
        "id": contact_id,
        "customFields": [],
        "companyName": "Acme",
    }


# ---------------------------------------------------------------------------
# §4.1 steps 8–11 happy path — interested/high → GHL writes → NotImplementedError
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_interested_high_confidence_writes_ghl(mock_build, mock_classify, mock_gen_response, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    from reply_router.responder import ResponderResult
    mock_gen_response.return_value = ResponderResult(text="Draft reply text here.", requires_shadow=False, failed=False)
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_1"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "interested", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "asked for call",
    }
    result = process_reply(_stub_config_full(), _payload(mid="m_new"))
    assert result.status == "processed"
    ghl_mock.update_contact.assert_called()
    ghl_mock.add_tags.assert_called()
    ghl_mock.add_note.assert_called()
    ghl_mock.move_to_pipeline_stage.assert_called_once()
    call_kwargs = ghl_mock.move_to_pipeline_stage.call_args.kwargs
    assert call_kwargs["contact_id"] == "ct_1"
    assert call_kwargs["pipeline_id"] == "p"
    assert call_kwargs["stage_id"] == "s3"
    assert call_kwargs["name"]  # non-empty fallback chain — exact value depends on stub contact shape
    ghl_mock.add_to_dnc.assert_not_called()


# ---------------------------------------------------------------------------
# §4.1 step 12 — unsubscribe → DNC call made
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_unsubscribe_triggers_dnc_call(mock_build, mock_classify, mock_sl_cls, mock_gen_response, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    from reply_router.responder import ResponderResult
    mock_gen_response.return_value = ResponderResult(text="Removed you from our list.", requires_shadow=False, failed=False)
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_unsub"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "please remove me",
    }
    result = process_reply(_stub_config_full(), _payload(mid="m_unsub"))
    assert result.status == "processed"
    ghl_mock.add_to_dnc.assert_called_once_with("ct_unsub")


# ---------------------------------------------------------------------------
# §7.3 #3 — DNC write fails 3× → URGENT Slack alert → returns 503
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_urgent")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_dnc_write_failure_escalates(mock_build, mock_classify, mock_post_urgent, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/fake")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_dnc_fail"), MultiContactResolution.SINGLE,
    )
    ghl_mock.add_to_dnc.side_effect = RuntimeError("GHL 500")
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "remove me",
    }
    result = process_reply(_stub_config_full(), _payload(mid="m_dnc_fail"))
    # All 3 retries exhausted
    assert ghl_mock.add_to_dnc.call_count == 3
    # URGENT alert fired
    mock_post_urgent.assert_called_once()
    assert mock_post_urgent.call_args[1]["title"] == "Unsubscribe not honored in GHL"
    # Orchestrator returns (doesn't raise), 503
    assert result.status == "deferred_for_retry"
    assert result.http_status == 503


# ---------------------------------------------------------------------------
# §4.1 step 8 — not_now with followup date → contract_end_date written
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_not_now_writes_contract_end_date(mock_build, mock_classify, mock_gen_response, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    from reply_router.responder import ResponderResult
    mock_gen_response.return_value = ResponderResult(text="Sounds good, I'll follow up then.", requires_shadow=False, failed=False)
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_nn"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "not_now", "confidence": "medium",
        "suggested_followup_date_iso": "2026-09-01", "reasoning": "busy until fall",
    }
    result = process_reply(_stub_config_full(), _payload(mid="m_nn"))
    assert result.status == "processed"
    # Check contract_end_date written in update_contact custom_fields
    # update_contact is called twice: once for soft lock, once for GHL writes
    # Find the GHL-writes call (has cf_class in custom_fields)
    calls = ghl_mock.update_contact.call_args_list
    ghl_write_call = next(
        c for c in calls if "cf_class" in (c[1].get("custom_fields") or {})
    )
    assert ghl_write_call[1]["custom_fields"]["cf_end"] == "2026-09-01"


# ---------------------------------------------------------------------------
# §7.3 #2 — unsubscribe with low confidence → carve-out bypasses gate → DNC honored
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_unsubscribe_low_confidence_still_honored(mock_build, mock_classify, mock_sl_cls, mock_gen_response, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    from reply_router.responder import ResponderResult
    mock_gen_response.return_value = ResponderResult(text="Removed you from our list.", requires_shadow=False, failed=False)
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_low_unsub"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "low",
        "suggested_followup_date_iso": None, "reasoning": "maybe unsubscribe",
    }
    result = process_reply(_stub_config_full(), _payload(mid="m_low_unsub"))
    assert result.status == "processed"
    # GHL writes happened (carve-out bypasses confidence gate)
    ghl_mock.update_contact.assert_called()
    ghl_mock.add_tags.assert_called()
    # DNC honored even at low confidence
    ghl_mock.add_to_dnc.assert_called_once_with("ct_low_unsub")
    # Tags include both low_confidence markers
    tags_call = ghl_mock.add_tags.call_args[0]
    tags = tags_call[1]  # second positional arg is the tag list
    assert "low_confidence" in tags
    assert "low_confidence_unsubscribe" in tags


# ---------------------------------------------------------------------------
# §7.3 #13 setup — unknown classification → GHL writes skipped
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_urgent")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_unknown_classification_skips_ghl_writes(mock_build, mock_classify, mock_post_urgent, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/fake")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_unk"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unknown", "confidence": "low",
        "suggested_followup_date_iso": None, "reasoning": "classifier gave up",
    }
    result = process_reply(_stub_config_full(), _payload(mid="m_unk"))
    assert result.status == "processed"
    assert result.classification == "unknown"
    # Classification fields (cf_class) must NOT be written — no standard GHL pipeline write
    for c in ghl_mock.update_contact.call_args_list:
        cf = c[1].get("custom_fields") or {}
        assert "cf_class" not in cf, "GHL write should not happen for unknown classification"
    # add_tags called only via _handle_unknown (not the standard routing path)
    ghl_mock.add_tags.assert_called_once_with("ct_unk", ["replied", "unknown"])
    ghl_mock.add_to_dnc.assert_not_called()


# ===========================================================================
# §4.1 step 13b–13c tests (Task 4.1e) — responder + auto_send/shadow_send
# ===========================================================================

def _payload_full(mid="m_x", email_stats_id="es_1"):
    return ReplyPayload(
        message_id=mid, from_email="prospect@example.com",
        lead_email="prospect@example.com",
        campaign_id="c1", reply_text="Hi, interested in your services",
        email_stats_id=email_stats_id,
    )


# ---------------------------------------------------------------------------
# §7.3 — auto_send path: wrong_person/high → SmartleadClient.send_reply_in_thread called
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_responder_auto_send_calls_smartlead(mock_build, mock_classify, mock_sl_cls, mock_gen_response, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl-key")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_1", "customFields": [], "companyName": "Acme"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "wrong_person", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "redirected",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen_response.return_value = ResponderResult(
        text="Thanks Pat. Could you point me to who handles facilities at Acme?",
        requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    result = process_reply(cfg, _payload_full(mid="m_x"))
    assert result.status == "processed"
    sl_instance.send_reply_in_thread.assert_called_once()
    call = sl_instance.send_reply_in_thread.call_args
    assert call.kwargs["campaign_id"] == "c1"
    assert call.kwargs["body"].startswith("Thanks Pat")


# ---------------------------------------------------------------------------
# §7.3 — shadow_send path: interested/high + auto_send=False → store_draft + threading params
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_responder_shadow_send_stores_draft_and_threading_params(mock_build, mock_classify, mock_sl_cls, mock_gen_response, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl-key")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_2", "customFields": [], "companyName": "Beta Corp"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "interested", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "wants a demo",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen_response.return_value = ResponderResult(
        text="Great to hear! Here's a link to book a call.",
        requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    # interested has auto_send=False in _stub_config_full → shadow_send
    result = process_reply(cfg, _payload_full(mid="m_sh", email_stats_id="es_sh"))
    assert result.status == "processed"
    # NO Smartlead send
    sl_instance.send_reply_in_thread.assert_not_called()
    # Must have TWO update_contact calls after the soft lock:
    # 1) soft lock acquisition (cf_lock)
    # 2) store_draft (token + text + created_at)
    # 3) threading params (pending_reply_message_id + pending_reply_email_stats_id)
    all_calls = ghl_mock.update_contact.call_args_list
    # Find the store_draft call (has cf_tok)
    draft_call = next(
        (c for c in all_calls if "cf_tok" in (c[1].get("custom_fields") or {})),
        None,
    )
    assert draft_call is not None, "store_draft update_contact not found"
    # Find the threading-params call (has cf_rmid)
    threading_call = next(
        (c for c in all_calls if "cf_rmid" in (c[1].get("custom_fields") or {})),
        None,
    )
    assert threading_call is not None, "threading-params update_contact not found"
    assert threading_call[1]["custom_fields"]["cf_rmid"] == "m_sh"
    assert threading_call[1]["custom_fields"]["cf_resid"] == "es_sh"


# ---------------------------------------------------------------------------
# §7.3 #16 — booking-link placeholder forces shadow even when auto_send=True
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_booking_link_placeholder_forces_shadow(mock_build, mock_classify, mock_sl_cls, mock_gen_response, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl-key")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_ph", "customFields": [], "companyName": "Gamma Inc"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    # wrong_person has auto_send=True in _stub_config_full; but booking link is PLACEHOLDER
    mock_classify.return_value = {
        "classification": "wrong_person", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "redirected",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    # requires_shadow=True signals that placeholder triggered shadow
    mock_gen_response.return_value = ResponderResult(
        text="Who handles facilities at Gamma Inc?",
        requires_shadow=True, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    cfg.business_context.booking_link = "https://x/PLACEHOLDER"
    result = process_reply(cfg, _payload_full(mid="m_ph"))
    assert result.status == "processed"
    # Forced to shadow_send → no Smartlead send
    sl_instance.send_reply_in_thread.assert_not_called()
    # Draft stored
    all_calls = ghl_mock.update_contact.call_args_list
    draft_call = next(
        (c for c in all_calls if "cf_tok" in (c[1].get("custom_fields") or {})),
        None,
    )
    assert draft_call is not None, "store_draft update_contact not found even though placeholder forced shadow"


# ---------------------------------------------------------------------------
# §7.3 #9 — Smartlead send failure → deferred_for_retry 503, no mark_complete
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_responder_send_failure_defers_dedupe_complete(mock_build, mock_classify, mock_sl_cls, mock_gen_response, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl-key")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_sf", "customFields": [], "companyName": "Delta LLC"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "wrong_person", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "redirected",
    }
    sl_instance = MagicMock()
    from reply_router.smartlead_client import SmartleadError
    sl_instance.send_reply_in_thread.side_effect = SmartleadError("502 bad gateway")
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen_response.return_value = ResponderResult(
        text="Who should I speak with?", requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    result = process_reply(cfg, _payload_full(mid="m_sf"))
    # Must RETURN (not raise)
    assert result.status == "deferred_for_retry"
    assert result.http_status == 503
    # No rolling-list write (last_processed_smartlead_message_ids not written)
    for c in ghl_mock.update_contact.call_args_list:
        cf = c[1].get("custom_fields") or {}
        assert "cf_roll" not in cf, "rolling-list (mark_complete) should NOT be written on send failure"


# ---------------------------------------------------------------------------
# §7.3 #9b — responder generate failure → deferred_for_retry 503
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_responder_generate_failure_defers_dedupe_complete(mock_build, mock_classify, mock_sl_cls, mock_gen_response, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl-key")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_gf", "customFields": [], "companyName": "Epsilon Co"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "wrong_person", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "redirected",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen_response.return_value = ResponderResult(text="", failed=True)
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    result = process_reply(cfg, _payload_full(mid="m_gf"))
    # Must RETURN (not raise)
    assert result.status == "deferred_for_retry"
    assert result.http_status == 503
    # No Smartlead send attempted
    sl_instance.send_reply_in_thread.assert_not_called()


# ===========================================================================
# §4.1 step 13d tests (Task 4.1f) — mark_unsubscribe post-send + URGENT alert
# ===========================================================================

# ---------------------------------------------------------------------------
# §7.3 #1 — unsubscribe full path: correct call ordering
# ghl.add_to_dnc → smartlead.send_reply_in_thread → smartlead.mark_unsubscribe
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_unsubscribe_full_path_correct_ordering(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    call_order = []
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_1", "customFields": [], "companyName": "Acme"},
        MultiContactResolution.SINGLE,
    )
    ghl_mock.add_to_dnc.side_effect = lambda *a, **k: call_order.append("dnc")
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "explicit",
    }
    sl_instance = MagicMock()
    sl_instance.send_reply_in_thread.side_effect = lambda *a, **k: call_order.append("send")
    sl_instance.mark_unsubscribe.side_effect = lambda *a, **k: call_order.append("unsub")
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Removed you from our list. Sorry for the interruption.",
        requires_shadow=False, failed=False,
    )
    result = process_reply(_stub_config_full(), _payload(mid="m_unsub"))
    assert result.status == "processed"
    assert call_order == ["dnc", "send", "unsub"], f"wrong order: {call_order}"


# ---------------------------------------------------------------------------
# §7.3 #4 — mark_unsubscribe fails 3× → URGENT Slack alert → raises NotImplementedError
# (post-send block does NOT 5xx — reply already sent)
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator.post_urgent")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_smartlead_mark_unsubscribe_failure_escalates(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_post, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_unsub_fail", "customFields": [], "companyName": "Acme"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "explicit",
    }
    from reply_router.smartlead_client import SmartleadError
    sl_instance = MagicMock()
    sl_instance.mark_unsubscribe.side_effect = SmartleadError("502")
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Removed you from our list. Sorry for the interruption.",
        requires_shadow=False, failed=False,
    )
    # Must return processed (not raise) — reply already sent; mark_unsubscribe failure
    # triggers URGENT Slack but does NOT 5xx the response
    result = process_reply(_stub_config_full(), _payload(mid="m_unsub_fail"))
    assert result.status == "processed"
    # All 3 retries attempted
    assert sl_instance.mark_unsubscribe.call_count == 3
    # URGENT Slack alert fired once with correct title
    mock_post.assert_called_once()
    assert mock_post.call_args[1]["title"] == "GHL DNC done but Smartlead may keep sending"


# ---------------------------------------------------------------------------
# Successful unsubscribe → mark_unsubscribe succeeds → post_urgent NOT called
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator.post_urgent")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_normal_confidence_unsubscribe_does_not_trigger_slack(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_post, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_unsub_ok", "customFields": [], "companyName": "Acme"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "explicit",
    }
    sl_instance = MagicMock()
    sl_instance.mark_unsubscribe.return_value = None  # success
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Removed you from our list. Sorry for the interruption.",
        requires_shadow=False, failed=False,
    )
    result = process_reply(_stub_config_full(), _payload(mid="m_unsub_ok"))
    assert result.status == "processed"
    # mark_unsubscribe succeeded → no URGENT alert
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Non-unsubscribe classification → mark_unsubscribe never called
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_smartlead_mark_unsubscribe_not_called_for_non_unsubscribe(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_wp", "customFields": [], "companyName": "Acme"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "wrong_person", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "redirected",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Who should I speak with?",
        requires_shadow=False, failed=False,
    )
    result = process_reply(_stub_config_full(), _payload(mid="m_wp"))
    assert result.status == "processed"
    sl_instance.mark_unsubscribe.assert_not_called()


# ===========================================================================
# Task 4.1g — mark_complete decision table + _handle_unknown (§4.1 step 14)
# ===========================================================================

# ---------------------------------------------------------------------------
# §7.3 #13 — unknown classification → _handle_unknown called → mark_complete → "processed"
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_urgent")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_classifier_unknown_fallback(mock_build, mock_classify, mock_post_urgent, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/fake")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_unk"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unknown", "confidence": "low",
        "suggested_followup_date_iso": None, "reasoning": "classifier gave up",
    }
    result = process_reply(_stub_config_full(), _payload(mid="m_unk_fg"))
    # Returns processed (not raises)
    assert result.status == "processed"
    assert result.classification == "unknown"
    assert result.http_status == 200
    # ghl.add_tags called with ["replied", "unknown"]
    ghl_mock.add_tags.assert_called_once_with("ct_unk", ["replied", "unknown"])
    # ghl.add_note called with text containing "MANUAL CLASSIFICATION NEEDED"
    note_call = ghl_mock.add_note.call_args
    assert "MANUAL CLASSIFICATION NEEDED" in note_call[0][1] or "MANUAL CLASSIFICATION NEEDED" in str(note_call)
    # post_urgent called with correct title
    mock_post_urgent.assert_called_once()
    assert mock_post_urgent.call_args[1]["title"] == "MANUAL CLASSIFICATION NEEDED — classifier returned unknown"
    # mark_complete happened: rolling field written with hash of message_id
    all_calls = ghl_mock.update_contact.call_args_list
    rolling_call = next(
        (c for c in all_calls if "cf_roll" in (c[1].get("custom_fields") or {})),
        None,
    )
    assert rolling_call is not None, "mark_complete should have written the rolling field"
    written_ids = json.loads(rolling_call[1]["custom_fields"]["cf_roll"])
    assert hash16("m_unk_fg") in written_ids


# ---------------------------------------------------------------------------
# §7.3 #9c — responder returns failed=True, requires_shadow=True → deferred, no mark_complete
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_response_length_validation_defers_dedupe_complete(mock_build, mock_classify, mock_sl_cls, mock_gen, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl-key")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_short", "customFields": [], "companyName": "Short Co"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "wrong_person", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "redirected",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    # Text outside 20-800 char range → failed=True, requires_shadow=True
    mock_gen.return_value = ResponderResult(text="too short", requires_shadow=True, failed=True)
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    result = process_reply(cfg, _payload_full(mid="m_short"))
    # Returns deferred (not raises)
    assert result.status == "deferred_for_retry"
    assert result.http_status == 503
    # mark_complete NOT called: rolling field must NOT be written
    for c in ghl_mock.update_contact.call_args_list:
        cf = c[1].get("custom_fields") or {}
        assert "cf_roll" not in cf, "mark_complete (rolling list write) must NOT happen on responder failure"


# ---------------------------------------------------------------------------
# §4.1 step 14 — auto_send success path → mark_complete called before 4.1h handoff
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_mark_complete_called_on_auto_send_happy_path(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_mc_auto", "customFields": [], "companyName": "Mark Co"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "remove me",
    }
    sl_instance = MagicMock()
    sl_instance.mark_unsubscribe.return_value = None  # success
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Removed you from our list.", requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    result = process_reply(cfg, _payload(mid="m_mc_auto"))
    assert result.status == "processed"
    # mark_complete wrote rolling field with hash of message_id
    all_calls = ghl_mock.update_contact.call_args_list
    rolling_call = next(
        (c for c in all_calls if "cf_roll" in (c[1].get("custom_fields") or {})),
        None,
    )
    assert rolling_call is not None, "mark_complete must write rolling field on auto_send success"
    written_ids = json.loads(rolling_call[1]["custom_fields"]["cf_roll"])
    assert hash16("m_mc_auto") in written_ids


# ---------------------------------------------------------------------------
# §4.1 step 14 — shadow_send path → mark_complete called before 4.1h handoff
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_mark_complete_called_on_shadow_send_path(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_mc_shad", "customFields": [], "companyName": "Shadow Corp"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "interested", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "wants a demo",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Great to hear! Here's a link to book a call.",
        requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    # interested has auto_send=False → shadow_send
    result = process_reply(cfg, _payload_full(mid="m_mc_shad"))
    assert result.status == "processed"
    all_calls = ghl_mock.update_contact.call_args_list
    rolling_call = next(
        (c for c in all_calls if "cf_roll" in (c[1].get("custom_fields") or {})),
        None,
    )
    assert rolling_call is not None, "mark_complete must write rolling field on shadow_send"
    written_ids = json.loads(rolling_call[1]["custom_fields"]["cf_roll"])
    assert hash16("m_mc_shad") in written_ids


# ---------------------------------------------------------------------------
# §7.3 #4 supplement — unsub_failed=True still marks complete
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator.post_urgent")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_mark_complete_called_when_unsub_failed(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_post, mock_slack, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_unsub_mc", "customFields": [], "companyName": "Acme"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "explicit",
    }
    from reply_router.smartlead_client import SmartleadError
    sl_instance = MagicMock()
    sl_instance.mark_unsubscribe.side_effect = SmartleadError("502")
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Removed you from our list. Sorry for the interruption.",
        requires_shadow=False, failed=False,
    )
    # mark_unsubscribe fails 3× → unsub_failed=True — but mark_complete still called
    result = process_reply(_stub_config_full(), _payload(mid="m_unsub_mc"))
    assert result.status == "processed"
    all_calls = ghl_mock.update_contact.call_args_list
    rolling_call = next(
        (c for c in all_calls if "cf_roll" in (c[1].get("custom_fields") or {})),
        None,
    )
    assert rolling_call is not None, "mark_complete must write rolling field even when unsub_failed=True"
    written_ids = json.loads(rolling_call[1]["custom_fields"]["cf_roll"])
    assert hash16("m_unsub_mc") in written_ids


# ===========================================================================
# Task 4.1h — Slack notify (best-effort) + final ProcessResult (§4.1 step 15)
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Slack notify called when action_bundle.slack_notify=True (interested)
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_slack_notify_called_when_action_bundle_says_so(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_notify, monkeypatch):
    """classification=interested (slack_notify=True) → post_classification_notification called."""
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_slack1", "customFields": [], "companyName": "Notify Co"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "interested", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "wants a demo",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Great to hear! Here's a link to book a walkthrough.",
        requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    result = process_reply(cfg, _payload_full(mid="m_notify1"))
    assert result.status == "processed"
    # interested has auto_send=False → shadow_send
    assert result.send_mode == "shadow_send"
    # Slack notification must have fired
    mock_notify.assert_called_once()
    call_kwargs = mock_notify.call_args[1]
    assert call_kwargs["classification"] == "interested"
    assert call_kwargs["send_mode"] == "shadow_send"


# ---------------------------------------------------------------------------
# 2. Slack notify skipped for normal-confidence unsubscribe (§7.3 #1)
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_slack_notify_skipped_for_normal_unsubscribe(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_notify, monkeypatch):
    """classification=unsubscribe, confidence=high → action_bundle.slack_notify=False → NOT called."""
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_slack_unsub", "customFields": [], "companyName": "No Notify Co"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "remove me",
    }
    sl_instance = MagicMock()
    sl_instance.mark_unsubscribe.return_value = None
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Removed you from our list.", requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    result = process_reply(cfg, _payload(mid="m_skip_notify"))
    assert result.status == "processed"
    # unsubscribe config has slack_notify=False → must NOT call post_classification_notification
    mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Slack notify called for low-confidence unsubscribe (§5.4)
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_slack_notify_called_for_low_confidence_unsubscribe(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_notify, monkeypatch):
    """classification=unsubscribe, confidence=low → routing sets slack_notify=True → called."""
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_low_unsub_n", "customFields": [], "companyName": "Maybe Unsub Co"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "low",
        "suggested_followup_date_iso": None, "reasoning": "maybe unsubscribe",
    }
    sl_instance = MagicMock()
    sl_instance.mark_unsubscribe.return_value = None
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Removed you from our list.", requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    result = process_reply(cfg, _payload(mid="m_low_unsub_notify"))
    assert result.status == "processed"
    # Low-confidence unsubscribe → routing sets slack_notify=True → notification fires
    mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Slack failure does not break response (spec §6.2 principle 4)
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_slack_failure_does_not_break_response(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_notify, monkeypatch):
    """If post_classification_notification raises, process_reply still returns processed."""
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    mock_notify.side_effect = RuntimeError("simulated Slack bug")

    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_slack_err", "customFields": [], "companyName": "Error Co"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "interested", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "wants a demo",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Great to hear! Link to book a call.",
        requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    # Must NOT raise — Slack errors are swallowed; response still returns processed
    result = process_reply(cfg, _payload_full(mid="m_slack_err"))
    assert result.status == "processed"


# ---------------------------------------------------------------------------
# 5. monitoring=True when today < monitoring_until
# ---------------------------------------------------------------------------

@freeze_time("2026-05-16")
@patch("reply_router.orchestrator.post_classification_notification")
@patch("reply_router.orchestrator._generate_response")
@patch("reply_router.orchestrator.SmartleadClient")
@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_monitoring_badge_when_today_before_monitoring_until(mock_build, mock_classify, mock_sl_cls, mock_gen, mock_notify, monkeypatch):
    """When today < monitoring_until, monitoring=True is passed to post_classification_notification."""
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake-sl")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")

    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        {"id": "ct_mon", "customFields": [], "companyName": "Monitor Co"},
        MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "interested", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "wants a call",
    }
    sl_instance = MagicMock()
    mock_sl_cls.return_value = sl_instance
    from reply_router.responder import ResponderResult
    mock_gen.return_value = ResponderResult(
        text="Great to hear! Here's a booking link.",
        requires_shadow=False, failed=False,
    )
    cfg = _stub_config_full()
    cfg.smartlead.api_key_env = "TEST_SL_API_KEY"
    cfg.monitoring_until = "2099-12-31"
    result = process_reply(cfg, _payload_full(mid="m_mon"))
    assert result.status == "processed"
    mock_notify.assert_called_once()
    # monitoring=True because today (2026-05-16) < monitoring_until (2099-12-31)
    assert mock_notify.call_args[1]["monitoring"] is True


# ---------------------------------------------------------------------------
# _vercel_base_url scheme handling — Slack rejects scheme-less URLs in buttons
# ---------------------------------------------------------------------------

def test_vercel_base_url_prepends_https_to_bare_hostname(monkeypatch):
    """VERCEL_PROJECT_PRODUCTION_URL is set by Vercel runtime as a bare hostname
    (no scheme). Slack's Block Kit rejects scheme-less URLs in button blocks
    with `invalid_blocks`, so we must prepend https:// before use."""
    from reply_router.orchestrator import _vercel_base_url
    monkeypatch.delenv("VERCEL_URL_OVERRIDE", raising=False)
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "reply-router.vercel.app")
    assert _vercel_base_url() == "https://reply-router.vercel.app"


def test_vercel_base_url_respects_existing_scheme(monkeypatch):
    from reply_router.orchestrator import _vercel_base_url
    monkeypatch.delenv("VERCEL_URL_OVERRIDE", raising=False)
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "https://custom.example.com")
    assert _vercel_base_url() == "https://custom.example.com"


def test_vercel_base_url_override_wins(monkeypatch):
    from reply_router.orchestrator import _vercel_base_url
    monkeypatch.setenv("VERCEL_URL_OVERRIDE", "https://staging.example.com")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "reply-router.vercel.app")
    assert _vercel_base_url() == "https://staging.example.com"


def test_vercel_base_url_fallback_when_unset(monkeypatch):
    from reply_router.orchestrator import _vercel_base_url
    monkeypatch.delenv("VERCEL_URL_OVERRIDE", raising=False)
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
    assert _vercel_base_url() == "https://reply-router.vercel.app"


# ---------------------------------------------------------------------------
# ReplyPayload.from_smartlead_webhook — payload shape verified empirically
# from sandbox-router-test on 2026-05-20 (campaign 3360292, lead 3835638239)
# ---------------------------------------------------------------------------

def test_from_smartlead_webhook_with_real_payload_shape():
    """Locks the field mapping for the actual Smartlead webhook payload
    shape we captured during sandbox testing. Smartlead uses to_email /
    to_name / stats_id — NOT lead_email / sender_name / email_stats_id.

    IMPORTANT (2026-05-21): `to_name` is the LEAD's name (the recipient of
    the original outbound), NOT the sender persona. Earlier versions of
    this code aliased to_name → sender_persona, which caused Claude to be
    prompted "Sign off as JT Kolke" — Claude resolved that contradiction
    by inventing a generic 'The Clear Facility Team' collective signoff.
    The real sender persona (Sarah/Mike/Jessica) must be resolved from
    `email_account_id` via Smartlead /email-accounts/{id} (TODO post-launch).
    For now: sender_persona is empty when only to_name is provided, and
    the responder uses a name-less 'Best,' close that lets the per-mailbox
    signature carry sender identity downstream.
    """
    payload = {
        "campaign_id": 3360292,
        "stats_id": "7b27fb71-588d-4afa-b185-0259716ff44b",
        "to_email": "jt@ksquaredai.com",
        "to_name": "JT Kolke",
        "subject": "Re: Router sandbox test",
        "sent_message": {
            "message_id": "<7b27fb71-588d-sl54-4afa-b185-0259716ff44b@discoverclearfacility.com>",
            "html": "<p>original outbound...</p>",
            "text": "original outbound...",
            "time": "2026-05-20T03:31:27.910Z",
            "subject": "Router sandbox test — please reply",
        },
        "reply_message": {
            "message_id": "<CANVFsaQU9+-5OLCYogVDAYzLykuZy9i@mail.gmail.com>",
            "text": "Thumbs up!",
            "html": "<div>Thumbs up!</div>",
            "time": "2026-05-20T03:32:08.000Z",
        },
    }
    rp = ReplyPayload.from_smartlead_webhook(payload)
    # The REPLY's own message id (not the outbound's). Must not collide.
    assert rp.message_id == "<CANVFsaQU9+-5OLCYogVDAYzLykuZy9i@mail.gmail.com>"
    assert rp.message_id != payload["sent_message"]["message_id"]
    # The lead's email — pulled from Smartlead's `to_email`.
    assert rp.from_email == "jt@ksquaredai.com"
    assert rp.lead_email == "jt@ksquaredai.com"
    assert rp.campaign_id == "3360292"
    assert rp.reply_text == "Thumbs up!"
    assert rp.email_stats_id == "7b27fb71-588d-4afa-b185-0259716ff44b"
    assert rp.original_subject == "Re: Router sandbox test"
    # Empty — `to_name` is the LEAD's name, NOT the sender persona. See docstring.
    assert rp.sender_persona == ""


def test_from_smartlead_webhook_falls_through_none_values():
    """Skeleton-shaped contacts have explicit None values, not missing keys.
    dict.get(key, default) returns None then — we must `or` past it."""
    payload = {
        "to_email": None,        # present but None
        "lead_email": "fallback@example.com",  # the actual usable value
        "campaign_id": 99,
        "to_name": None,
        "sender_name": "Persona Fallback",
        "reply_text": "hi there",
        "stats_id": None,
        "email_stats_id": "stats_legacy",
    }
    rp = ReplyPayload.from_smartlead_webhook(payload)
    # lead_email picks up the truthy fallback (precedence: sl_lead_email → to_email → lead_email → to)
    assert rp.lead_email == "fallback@example.com"
    # from_email derives from the same lead identity as lead_email (2026-05-28
    # fix): for Smartlead inbound-reply webhooks the sender IS the lead. The
    # earlier design treated them as separate fields, but Smartlead's
    # payload.from_email is the OUTBOUND sending mailbox — using it for loop
    # detection caused every real reply to false-positive as a self-loop.
    assert rp.from_email == "fallback@example.com"
    assert rp.sender_persona == "Persona Fallback"
    assert rp.email_stats_id == "stats_legacy"
    assert rp.reply_text == "hi there"


def test_from_smartlead_webhook_reply_in_alternate_container():
    """Smartlead may put reply data in `incoming_message` rather than
    `reply_message` (varies across plan tiers / docs versions)."""
    payload = {
        "to_email": "lead@x.com",
        "campaign_id": 1,
        "stats_id": "s",
        "incoming_message": {
            "message_id": "<reply-mid>",
            "text": "reply body",
        },
    }
    rp = ReplyPayload.from_smartlead_webhook(payload)
    assert rp.message_id == "<reply-mid>"
    assert rp.reply_text == "reply body"


def test_from_smartlead_webhook_empty_payload_yields_empty_strings():
    """A wildly malformed payload should not crash — all fields default to ''."""
    rp = ReplyPayload.from_smartlead_webhook({})
    assert rp.message_id == ""
    assert rp.from_email == ""
    assert rp.lead_email == ""
    assert rp.campaign_id == ""
    assert rp.reply_text == ""
