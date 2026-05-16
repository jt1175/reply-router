"""Tests for reply_router.config — schema validation and loading."""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from reply_router.config import (
    ClientConfig,
    load_client_config,
    load_and_validate_all,
    ConfigError,
)


def _minimal_valid_config() -> dict:
    """The minimum keys needed for ClientConfig to validate."""
    return {
        "client_id": "test_client",
        "client_display_name": "Test Client",
        "ghl": {
            "sub_account_id": "loc_abc",
            "api_key_env": "TEST_GHL_API_KEY",
            "pipeline_id": "pipe_abc",
            "custom_field_ids": {
                "reply_classification": "cf_1",
                "reply_received_at": "cf_2",
                "contract_end_date": "cf_3",
                "nurture_bucket": "cf_4",
                "last_processed_smartlead_message_ids": "cf_5",
                "currently_processing_smartlead_message_id": "cf_6",
                "pending_draft_token": "cf_7",
                "pending_draft_text": "cf_8",
                "pending_draft_created_at": "cf_9",
                "pending_reply_message_id": "cf_10",
                "pending_reply_email_stats_id": "cf_11",
            },
        },
        "smartlead": {"api_key_env": "TEST_SMARTLEAD_API_KEY", "campaign_ids": ["c1"]},
        "slack": {"incoming_webhook_url_env": "TEST_SLACK_WEBHOOK_URL"},
        "auth": {"router_secret_env": "TEST_ROUTER_SECRET"},
        "sending_inboxes": ["test@example.com"],
        "monitoring_until": "2026-12-31",
        "classification_actions": {
            "unsubscribe":   {"auto_send": True,  "min_confidence": "low",    "slack_notify": False, "pipeline_stage_id": "s1"},
            "wrong_person":  {"auto_send": True,  "min_confidence": "medium", "slack_notify": True,  "pipeline_stage_id": "s2"},
            "interested":    {"auto_send": False, "min_confidence": "high",   "slack_notify": True,  "pipeline_stage_id": "s3"},
            "not_now":       {"auto_send": False, "min_confidence": "medium", "slack_notify": True,  "pipeline_stage_id": "s4", "nurture_bucket": "not_now"},
            "info_request":  {"auto_send": False, "min_confidence": "high",   "slack_notify": True,  "pipeline_stage_id": "s5"},
            "objection":     {"auto_send": False, "min_confidence": "high",   "slack_notify": True,  "pipeline_stage_id": "s5"},
        },
        "business_context": {
            "company_name": "Test Co",
            "service_area": "Test Area",
            "services_offered": [],
            "services_not_offered": [],
            "pricing_response": "Pricing depends on the space.",
            "booking_link": "https://example.com/book",
        },
    }


def test_minimal_valid_config_loads(tmp_path):
    cfg_file = tmp_path / "test.json"
    cfg_file.write_text(json.dumps(_minimal_valid_config()))
    cfg = load_client_config(cfg_file)
    assert isinstance(cfg, ClientConfig)
    assert cfg.client_id == "test_client"
    assert "test@example.com" in cfg.sending_inboxes


def test_missing_required_custom_field_id_raises(tmp_path):
    cfg = _minimal_valid_config()
    del cfg["ghl"]["custom_field_ids"]["pending_draft_token"]
    cfg_file = tmp_path / "test.json"
    cfg_file.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="missing required keys"):
        load_client_config(cfg_file)


def test_unknown_classification_key_raises(tmp_path):
    cfg = _minimal_valid_config()
    cfg["classification_actions"]["irrelevant_chitchat"] = {
        "auto_send": False, "min_confidence": "low",
        "slack_notify": False, "pipeline_stage_id": "x"
    }
    cfg_file = tmp_path / "test.json"
    cfg_file.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="unknown classification keys"):
        load_client_config(cfg_file)


def test_malformed_json_raises_clean_error(tmp_path):
    cfg_file = tmp_path / "test.json"
    cfg_file.write_text("{not json")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_client_config(cfg_file)


def test_load_and_validate_all_skips_underscore_files(tmp_path):
    cfg = _minimal_valid_config()
    (tmp_path / "good.json").write_text(json.dumps(cfg))
    (tmp_path / "_test.json").write_text("{intentionally broken")
    result = load_and_validate_all(tmp_path)
    assert "test_client" in result
    assert len(result) == 1


def test_duplicate_client_id_across_files_raises(tmp_path):
    cfg = _minimal_valid_config()
    (tmp_path / "a.json").write_text(json.dumps(cfg))
    (tmp_path / "b.json").write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="duplicate client_id"):
        load_and_validate_all(tmp_path)


def test_underscore_doc_keys_in_classification_actions_are_stripped(tmp_path):
    # Spec §8.1 puts _doc_schema and per-entry _doc keys inside classification_actions
    # as inline documentation. The loader must accept them without complaint.
    cfg = _minimal_valid_config()
    cfg["classification_actions"]["_doc_schema"] = "inline schema docstring per spec §8.1"
    cfg["classification_actions"]["unsubscribe"]["_doc"] = "inline action docstring per spec §8.1"
    cfg_file = tmp_path / "test.json"
    cfg_file.write_text(json.dumps(cfg))
    loaded = load_client_config(cfg_file)
    # _doc_schema sibling is stripped, so only the 6 real classifications remain
    assert set(loaded.classification_actions.keys()) == {
        "unsubscribe", "wrong_person", "interested",
        "not_now", "info_request", "objection",
    }
