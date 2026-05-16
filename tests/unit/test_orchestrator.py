"""Unit tests for reply_router.orchestrator."""
from __future__ import annotations

import json
import time

import pytest
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
    # Return 'unknown' so GHL writes are skipped — update_contact called only once (soft lock)
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
        with pytest.raises(NotImplementedError):
            process_reply(_stub_config(), _payload(mid="m_stale"))
    # Soft lock was acquired (overwritten); GHL writes skipped (unknown → action_bundle=None)
    ghl_mock.update_contact.assert_called_once()


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
        with pytest.raises(NotImplementedError):
            process_reply(_stub_config(), _payload(mid="m_skel"))
    # Soft lock was acquired; GHL writes skipped (unknown → action_bundle=None)
    ghl_mock.update_contact.assert_called_once()


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
        with pytest.raises(NotImplementedError):
            process_reply(_stub_config(), _payload(mid="m_single"))
    # Soft lock acquired; GHL writes skipped (unknown → action_bundle=None)
    ghl_mock.update_contact.assert_called_once()


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
        with pytest.raises(NotImplementedError):
            process_reply(_stub_config(), _payload(mid="m_ambig"))
    # Soft lock must have been acquired on the ambiguous contact; GHL writes skipped (unknown)
    ghl_mock.update_contact.assert_called_once()
    call_args = ghl_mock.update_contact.call_args
    assert call_args[0][0] == "ct_ambig"


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

@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_interested_high_confidence_writes_ghl_then_raises_notimpl(mock_build, mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_1"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "interested", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "asked for call",
    }
    with pytest.raises(NotImplementedError):
        process_reply(_stub_config_full(), _payload(mid="m_new"))
    ghl_mock.update_contact.assert_called()
    ghl_mock.add_tags.assert_called()
    ghl_mock.add_note.assert_called()
    ghl_mock.move_to_pipeline_stage.assert_called_with(
        contact_id="ct_1", pipeline_id="p", stage_id="s3"
    )
    ghl_mock.add_to_dnc.assert_not_called()


# ---------------------------------------------------------------------------
# §4.1 step 12 — unsubscribe → DNC call made
# ---------------------------------------------------------------------------

@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_unsubscribe_triggers_dnc_call(mock_build, mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/fake")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_unsub"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "high",
        "suggested_followup_date_iso": None, "reasoning": "please remove me",
    }
    with pytest.raises(NotImplementedError):
        process_reply(_stub_config_full(), _payload(mid="m_unsub"))
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

@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_not_now_writes_contract_end_date(mock_build, mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_nn"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "not_now", "confidence": "medium",
        "suggested_followup_date_iso": "2026-09-01", "reasoning": "busy until fall",
    }
    with pytest.raises(NotImplementedError):
        process_reply(_stub_config_full(), _payload(mid="m_nn"))
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

@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_unsubscribe_low_confidence_still_honored(mock_build, mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/fake")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_low_unsub"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unsubscribe", "confidence": "low",
        "suggested_followup_date_iso": None, "reasoning": "maybe unsubscribe",
    }
    with pytest.raises(NotImplementedError):
        process_reply(_stub_config_full(), _payload(mid="m_low_unsub"))
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

@patch("reply_router.orchestrator.classify")
@patch("reply_router.orchestrator._build_ghl_client")
def test_unknown_classification_skips_ghl_writes(mock_build, mock_classify, monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    ghl_mock = MagicMock()
    ghl_mock.resolve_contact_by_email.return_value = (
        _clean_contact("ct_unk"), MultiContactResolution.SINGLE,
    )
    mock_build.return_value = ghl_mock
    mock_classify.return_value = {
        "classification": "unknown", "confidence": "low",
        "suggested_followup_date_iso": None, "reasoning": "classifier gave up",
    }
    with pytest.raises(NotImplementedError):
        process_reply(_stub_config_full(), _payload(mid="m_unk"))
    # Only the soft-lock update_contact should have been called (not GHL writes)
    # update_contact called exactly once (soft lock), never with cf_class in custom_fields
    for c in ghl_mock.update_contact.call_args_list:
        cf = c[1].get("custom_fields") or {}
        assert "cf_class" not in cf, "GHL write should not happen for unknown classification"
    ghl_mock.add_tags.assert_not_called()
    ghl_mock.add_to_dnc.assert_not_called()
