"""Tests for api/reconcile.py — nightly cron + 3-phase recovery."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from reply_router.reconciler import (
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
@patch("reply_router.reconciler.process_reply")
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


@patch("reply_router.reconciler.process_reply")
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


@patch("reply_router.reconciler.process_reply")
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
    with patch("reply_router.reconciler.GHLClient") as mock_ghl_cls, \
         patch("reply_router.reconciler.SmartleadClient") as mock_sl_cls, \
         patch("reply_router.reconciler.phase1_clear_stuck_soft_locks", return_value=2) as p1, \
         patch("reply_router.reconciler.phase2_smartlead_vs_ghl", return_value={"replies_seen": 5, "processed": 1, "skipped": 4, "errors": []}) as p2, \
         patch("reply_router.reconciler.phase3_expire_old_tokens", return_value=1) as p3:
        summary = reconcile_client(client_config)
    assert summary["client_id"] == "t"
    assert summary["phase_1"]["stuck_locks_recovered"] == 2
    assert summary["phase_2"]["processed"] == 1
    assert summary["phase_3"]["tokens_expired"] == 1
    p1.assert_called_once()
    p2.assert_called_once()
    p3.assert_called_once()


# ─── Phase 4 (Smartlead → GHL metrics sync) ───

from reply_router.reconciler import phase4_metrics_sync


def _phase4_config(**overrides):
    """Minimal client_config-shaped stub for Phase 4 tests."""
    cfg = MagicMock()
    cfg.client_id = "test_client"
    cfg.smartlead.campaign_ids = ["camp_1"]
    cfg.ghl.custom_field_ids = {
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
        "unsubscribed_at": "cf_unsub_at",
    }
    for k, v in overrides.items():
        if k == "campaign_ids":
            cfg.smartlead.campaign_ids = v
        elif k == "custom_field_ids":
            cfg.ghl.custom_field_ids = v
    return cfg


def test_phase4_skipped_when_metric_fields_missing():
    """Phase 4 requires the 6 metric fields; without them it's a no-op."""
    cfg = _phase4_config(custom_field_ids={"reply_classification": "x"})  # incomplete
    summary = phase4_metrics_sync(MagicMock(), MagicMock(), cfg)
    assert summary["status"] == "skipped"
    assert "missing custom_field_ids" in summary["reason"]


def test_phase4_skips_tbd_campaigns():
    """Campaigns whose IDs are still TBD_ placeholders are skipped."""
    cfg = _phase4_config(campaign_ids=["TBD_SMARTLEAD_CAMPAIGN_ID"])
    summary = phase4_metrics_sync(MagicMock(), MagicMock(), cfg)
    assert summary["campaigns_processed"] == 0


def test_phase4_skips_zero_stat_leads():
    """Leads with no opens/clicks/bounces/unsubs get skipped (no GHL work)."""
    sl = MagicMock()
    sl.get_campaign_statistics.return_value = {
        "data": [{"lead_email": "z@x.com", "open_count": 0, "click_count": 0,
                  "is_unsubscribed": False, "is_bounced": False}],
        "total_stats": 1,
    }
    ghl = MagicMock()
    cfg = _phase4_config()
    summary = phase4_metrics_sync(sl, ghl, cfg)
    assert summary["leads_skipped_zero_stats"] == 1
    ghl.get_contacts_by_email.assert_not_called()


def test_phase4_pushes_open_delta_to_ghl():
    """Smartlead shows 5 opens, GHL shows 2 → update GHL to 5 + last_open_at."""
    sl = MagicMock()
    sl.get_campaign_statistics.return_value = {
        "data": [{"lead_email": "pat@acme.com", "open_count": 5, "click_count": 0,
                  "open_time": "2026-05-21T10:00:00Z", "is_unsubscribed": False, "is_bounced": False}],
        "total_stats": 1,
    }
    ghl = MagicMock()
    ghl.get_contacts_by_email.return_value = [{
        "id": "ct_1", "customFields": [
            {"id": "cf_open_n", "value": "2"},
            {"id": "cf_click_n", "value": "0"},
        ],
    }]
    cfg = _phase4_config()
    summary = phase4_metrics_sync(sl, ghl, cfg)
    assert summary["opens_synced"] == 1
    write = ghl.update_contact.call_args.kwargs["custom_fields"]
    assert write["cf_open_n"] == "5"
    assert write["cf_last_open"] == "2026-05-21T10:00:00Z"


def test_phase4_idempotent_when_ghl_already_caught_up():
    """If GHL count >= Smartlead count, no update — phase 4 must be idempotent."""
    sl = MagicMock()
    sl.get_campaign_statistics.return_value = {
        "data": [{"lead_email": "pat@acme.com", "open_count": 3, "click_count": 0,
                  "is_unsubscribed": False, "is_bounced": False}],
        "total_stats": 1,
    }
    ghl = MagicMock()
    ghl.get_contacts_by_email.return_value = [{
        "id": "ct_1", "customFields": [{"id": "cf_open_n", "value": "5"}],  # GHL ahead
    }]
    cfg = _phase4_config()
    summary = phase4_metrics_sync(sl, ghl, cfg)
    assert summary["opens_synced"] == 0
    ghl.update_contact.assert_not_called()


def test_phase4_handles_unsubscribed_flag():
    """is_unsubscribed=True + no unsubscribed_at in GHL → DNC + set timestamp."""
    sl = MagicMock()
    sl.get_campaign_statistics.return_value = {
        "data": [{"lead_email": "p@x.com", "open_count": 1, "click_count": 0,
                  "is_unsubscribed": True, "is_bounced": False}],
        "total_stats": 1,
    }
    ghl = MagicMock()
    ghl.get_contacts_by_email.return_value = [{
        "id": "ct_1", "customFields": [{"id": "cf_open_n", "value": "0"}],
    }]
    cfg = _phase4_config()
    summary = phase4_metrics_sync(sl, ghl, cfg)
    assert summary["unsubscribes_synced"] == 1
    ghl.add_to_dnc.assert_called_with("ct_1")


def test_phase4_handles_contact_not_found_in_ghl():
    """Lead with stats but no GHL contact → counted, no crash."""
    sl = MagicMock()
    sl.get_campaign_statistics.return_value = {
        "data": [{"lead_email": "ghost@nowhere.com", "open_count": 1, "click_count": 0,
                  "is_unsubscribed": False, "is_bounced": False}],
        "total_stats": 1,
    }
    ghl = MagicMock()
    ghl.get_contacts_by_email.return_value = []
    cfg = _phase4_config()
    summary = phase4_metrics_sync(sl, ghl, cfg)
    assert summary["contacts_not_found_in_ghl"] == 1
    ghl.update_contact.assert_not_called()


def test_phase4_paginates_when_total_exceeds_limit():
    """Stats endpoint returns up to 100 per page; phase 4 keeps fetching until exhausted."""
    sl = MagicMock()
    sl.get_campaign_statistics.side_effect = [
        # Page 1
        {"data": [{"lead_email": f"p{i}@x.com", "open_count": 1, "click_count": 0,
                   "is_unsubscribed": False, "is_bounced": False}
                  for i in range(100)], "total_stats": 150},
        # Page 2
        {"data": [{"lead_email": f"p{i}@x.com", "open_count": 1, "click_count": 0,
                   "is_unsubscribed": False, "is_bounced": False}
                  for i in range(100, 150)], "total_stats": 150},
    ]
    ghl = MagicMock()
    ghl.get_contacts_by_email.return_value = []  # all not-found, simplifies test
    cfg = _phase4_config()
    summary = phase4_metrics_sync(sl, ghl, cfg)
    assert summary["leads_seen"] == 150
    assert sl.get_campaign_statistics.call_count == 2
