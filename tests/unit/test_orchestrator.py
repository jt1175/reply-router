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

def test_soft_lock_stale_proceeds_to_classification(monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
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
    # Soft lock was acquired (overwritten)
    ghl_mock.update_contact.assert_called_once()


# ---------------------------------------------------------------------------
# §7.3 #18 — no existing contact → CREATED_SKELETON → acquires lock, then NotImplementedError
# ---------------------------------------------------------------------------

def test_skeleton_contact_created_when_no_match(monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
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
    # Soft lock was acquired
    ghl_mock.update_contact.assert_called_once()


# ---------------------------------------------------------------------------
# §7.3 #17 setup — RESOLVED_BY_CAMPAIGN → proceeds normally (NotImplementedError)
# ---------------------------------------------------------------------------

def test_single_contact_proceeds_normally(monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    ghl_mock = MagicMock()
    contact = {
        "id": "ct_single",
        "customFields": [],
    }
    ghl_mock.resolve_contact_by_email.return_value = (contact, MultiContactResolution.RESOLVED_BY_CAMPAIGN)
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        with pytest.raises(NotImplementedError):
            process_reply(_stub_config(), _payload(mid="m_single"))
    # Soft lock acquired
    ghl_mock.update_contact.assert_called_once()


# ---------------------------------------------------------------------------
# §7.3 #17 — AMBIGUOUS → acquires lock and proceeds (shadow-forcing in 4.1d)
# ---------------------------------------------------------------------------

def test_ambiguous_contact_still_acquires_lock(monkeypatch):
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    ghl_mock = MagicMock()
    contact = {
        "id": "ct_ambig",
        "customFields": [],
    }
    ghl_mock.resolve_contact_by_email.return_value = (contact, MultiContactResolution.AMBIGUOUS)
    with patch("reply_router.orchestrator._build_ghl_client", return_value=ghl_mock):
        with pytest.raises(NotImplementedError):
            process_reply(_stub_config(), _payload(mid="m_ambig"))
    # Soft lock must have been acquired on the ambiguous contact
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
