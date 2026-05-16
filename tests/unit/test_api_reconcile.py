"""Tests for api/reconcile.py — nightly cron + 3-phase recovery."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.reconcile import (
    reconcile_client,
    phase1_clear_stuck_soft_locks,
    phase2_smartlead_vs_ghl,
    phase3_expire_old_tokens,
)


@pytest.fixture
def stub_ghl():
    return MagicMock()


@pytest.fixture
def stub_smartlead():
    return MagicMock()


@pytest.fixture
def stub_slack_url():
    return "https://hooks.slack.com/services/X/Y/Z"


# §7.3 #20
def test_reconciler_clears_stuck_soft_locks(stub_ghl, stub_slack_url):
    """Phase 1: contact with soft lock > 10min old → cleared."""
    stale_ts = int(time.time()) - 15 * 60  # 15 min old
    stub_ghl.list_contacts_with_field.return_value = [
        {"id": "ct_stuck", "customFields": [
            {"id": "cf_lock", "value": f"msg_xyz:{stale_ts}"}
        ]}
    ]
    count = phase1_clear_stuck_soft_locks(stub_ghl, soft_lock_field_id="cf_lock", slack_url=stub_slack_url)
    assert count == 1
    stub_ghl.update_contact.assert_called_with(
        "ct_stuck", custom_fields={"cf_lock": ""}
    )


def test_phase1_skips_fresh_locks(stub_ghl, stub_slack_url):
    fresh_ts = int(time.time()) - 60  # 1 min old
    stub_ghl.list_contacts_with_field.return_value = [
        {"id": "ct_fresh", "customFields": [{"id": "cf_lock", "value": f"m:{fresh_ts}"}]}
    ]
    count = phase1_clear_stuck_soft_locks(stub_ghl, "cf_lock", stub_slack_url)
    assert count == 0
    stub_ghl.update_contact.assert_not_called()


def test_phase1_handles_malformed_lock_value(stub_ghl, stub_slack_url):
    """Malformed value (no ':' or non-int ts) → skipped, no crash."""
    stub_ghl.list_contacts_with_field.return_value = [
        {"id": "ct_bad", "customFields": [{"id": "cf_lock", "value": "garbage"}]}
    ]
    count = phase1_clear_stuck_soft_locks(stub_ghl, "cf_lock", stub_slack_url)
    assert count == 0


# §7.3 #19
@patch("api.reconcile.process_reply")
def test_reconciler_picks_up_missed_webhook(mock_process_reply, stub_smartlead):
    """Phase 2: Smartlead returns a reply not in GHL rolling list → orchestrator.process_reply called."""
    mock_process_reply.return_value = MagicMock(status="processed", http_status=200)
    stub_smartlead.list_replies.return_value = [{
        "message_id": "mid_missed", "from_email": "pat@x.com", "lead_email": "pat@x.com",
        "campaign_id": "c1", "reply_text": "interested!", "email_stats_id": "es1",
    }]
    client_config = MagicMock()
    client_config.smartlead.campaign_ids = ["c1"]
    counts = phase2_smartlead_vs_ghl(stub_smartlead, client_config)
    assert counts["processed"] >= 1
    assert mock_process_reply.call_count == 1
    args, kwargs = mock_process_reply.call_args
    assert kwargs.get("source") == "reconciler"


@patch("api.reconcile.process_reply")
def test_phase2_aggregates_status_counts(mock_process_reply, stub_smartlead):
    """Phase 2 counts replies_seen, processed, skipped (duplicate / in_flight / ignored), errors."""
    mock_process_reply.side_effect = [
        MagicMock(status="processed"),
        MagicMock(status="duplicate"),
        MagicMock(status="ignored_self"),
    ]
    stub_smartlead.list_replies.return_value = [
        {"message_id": f"m{i}", "from_email": "x@x", "lead_email": "x@x",
         "campaign_id": "c1", "reply_text": "x", "email_stats_id": "es"}
        for i in range(3)
    ]
    client_config = MagicMock()
    client_config.smartlead.campaign_ids = ["c1"]
    counts = phase2_smartlead_vs_ghl(stub_smartlead, client_config)
    assert counts["replies_seen"] == 3
    assert counts["processed"] == 1
    assert counts["skipped"] == 2


@patch("api.reconcile.process_reply")
def test_phase2_collects_errors(mock_process_reply, stub_smartlead):
    """An exception during process_reply is collected in errors[], not re-raised."""
    mock_process_reply.side_effect = RuntimeError("boom")
    stub_smartlead.list_replies.return_value = [{
        "message_id": "m_err", "from_email": "x@x", "lead_email": "x@x",
        "campaign_id": "c1", "reply_text": "x", "email_stats_id": "es",
    }]
    client_config = MagicMock()
    client_config.smartlead.campaign_ids = ["c1"]
    counts = phase2_smartlead_vs_ghl(stub_smartlead, client_config)
    assert len(counts["errors"]) == 1
    assert counts["errors"][0]["message_id"] == "m_err"


# §7.3 #21
def test_reconciler_clears_expired_tokens(stub_ghl):
    """Phase 3: contact with pending_draft_created_at > 7d old → all 5 fields cleared."""
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    stub_ghl.list_contacts_with_field.return_value = [
        {"id": "ct_expired", "customFields": [{"id": "cf_dat", "value": old}]}
    ]
    count = phase3_expire_old_tokens(
        stub_ghl,
        token_field_id="cf_tok", text_field_id="cf_text",
        created_at_field_id="cf_dat",
        reply_message_id_field_id="cf_rmid",
        reply_email_stats_id_field_id="cf_resid",
    )
    assert count == 1
    args, kwargs = stub_ghl.update_contact.call_args
    assert kwargs["custom_fields"] == {
        "cf_tok": "", "cf_text": "", "cf_dat": "",
        "cf_rmid": "", "cf_resid": "",
    }


def test_phase3_skips_fresh_tokens(stub_ghl):
    """A 1-day-old token is fresh; not cleared."""
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    stub_ghl.list_contacts_with_field.return_value = [
        {"id": "ct_fresh", "customFields": [{"id": "cf_dat", "value": fresh}]}
    ]
    count = phase3_expire_old_tokens(
        stub_ghl, "cf_tok", "cf_text", "cf_dat", "cf_rmid", "cf_resid",
    )
    assert count == 0


def test_reconcile_client_integration(monkeypatch):
    """End-to-end-ish: reconcile_client calls all 3 phases and returns a summary dict."""
    monkeypatch.setenv("TEST_GHL_API_KEY", "fake")
    monkeypatch.setenv("TEST_SL_API_KEY", "fake")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.com/x")
    client_config = MagicMock()
    client_config.client_id = "t"
    client_config.ghl.sub_account_id = "loc"
    client_config.ghl.api_key_env = "TEST_GHL_API_KEY"
    client_config.ghl.custom_field_ids = {
        "currently_processing_smartlead_message_id": "cf_lock",
        "pending_draft_token": "cf_tok",
        "pending_draft_text": "cf_text",
        "pending_draft_created_at": "cf_dat",
        "pending_reply_message_id": "cf_rmid",
        "pending_reply_email_stats_id": "cf_resid",
    }
    client_config.smartlead.campaign_ids = ["c1"]
    client_config.smartlead.api_key_env = "TEST_SL_API_KEY"
    client_config.slack.incoming_webhook_url_env = "TEST_SLACK_URL"
    with patch("api.reconcile.GHLClient") as mock_ghl_cls, \
         patch("api.reconcile.SmartleadClient") as mock_sl_cls, \
         patch("api.reconcile.phase1_clear_stuck_soft_locks", return_value=2) as p1, \
         patch("api.reconcile.phase2_smartlead_vs_ghl", return_value={"replies_seen": 5, "processed": 1, "skipped": 4, "errors": []}) as p2, \
         patch("api.reconcile.phase3_expire_old_tokens", return_value=1) as p3:
        summary = reconcile_client(client_config)
    assert summary["client_id"] == "t"
    assert summary["phase_1"]["stuck_locks_recovered"] == 2
    assert summary["phase_2"]["processed"] == 1
    assert summary["phase_3"]["tokens_expired"] == 1
    p1.assert_called_once()
    p2.assert_called_once()
    p3.assert_called_once()
