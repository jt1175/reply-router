"""Unit tests for reply_router/dedupe.py — spec §3.2 + §6.1 + §7.3 scenarios #6, #7, #8."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import pytest
from freezegun import freeze_time

from reply_router.dedupe import (
    ROLLING_LIST_MAX,
    SOFT_LOCK_TTL_SEC,
    SoftLockState,
    _get_field,
    acquire_soft_lock,
    check_rolling,
    check_soft_lock,
    hash16,
    mark_complete,
)
from reply_router.ghl_client import GHLClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ROLLING_FIELD_ID = "rolling_field_001"
SOFT_LOCK_FIELD_ID = "soft_lock_field_001"
CONTACT_ID = "contact-abc-123"

FROZEN_NOW = "2026-05-16 12:00:00"
FROZEN_TS = 1778932800  # unix epoch for 2026-05-16 12:00:00 UTC


@pytest.fixture
def mock_client():
    return MagicMock(spec=GHLClient)


def _make_contact(
    contact_id: str = CONTACT_ID,
    rolling_value: str | None = None,
    soft_lock_value: str | None = None,
) -> dict:
    """Build a minimal GHL contact dict with optional custom field values."""
    custom_fields = []
    if rolling_value is not None:
        custom_fields.append({"id": ROLLING_FIELD_ID, "value": rolling_value})
    if soft_lock_value is not None:
        custom_fields.append({"id": SOFT_LOCK_FIELD_ID, "value": soft_lock_value})
    return {"id": contact_id, "customFields": custom_fields}


# ---------------------------------------------------------------------------
# hash16
# ---------------------------------------------------------------------------

class TestHash16:
    def test_hash16_deterministic_length_and_uniqueness(self):
        h1 = hash16("msg-001")
        h2 = hash16("msg-001")
        h3 = hash16("msg-002")

        assert h1 == h2, "Same input must yield same hash"
        assert h1 != h3, "Different inputs must yield different hashes"
        assert len(h1) == 16, "Output must be exactly 16 hex characters"
        assert h1.isalnum(), "Output should be hex (alphanumeric)"


# ---------------------------------------------------------------------------
# _get_field
# ---------------------------------------------------------------------------

class TestGetField:
    def test_returns_value_when_field_present(self):
        contact = {
            "id": "c1",
            "customFields": [
                {"id": "field-x", "value": "hello"},
                {"id": "field-y", "value": "world"},
            ],
        }
        assert _get_field(contact, "field-x") == "hello"
        assert _get_field(contact, "field-y") == "world"

    def test_returns_none_when_field_absent_or_missing(self):
        # Field ID not in list
        contact_missing_id = {
            "id": "c2",
            "customFields": [{"id": "other-field", "value": "val"}],
        }
        assert _get_field(contact_missing_id, "nonexistent") is None

        # customFields key absent entirely
        contact_no_key = {"id": "c3"}
        assert _get_field(contact_no_key, "field-x") is None

        # customFields is empty list
        contact_empty = {"id": "c4", "customFields": []}
        assert _get_field(contact_empty, "field-x") is None

        # customFields is None
        contact_none = {"id": "c5", "customFields": None}
        assert _get_field(contact_none, "field-x") is None


# ---------------------------------------------------------------------------
# check_rolling
# ---------------------------------------------------------------------------

class TestCheckRolling:
    def test_empty_field_returns_false(self):
        contact = _make_contact(rolling_value=None)
        assert check_rolling(contact, ROLLING_FIELD_ID, "msg-001") is False

    def test_empty_string_field_returns_false(self):
        contact = _make_contact(rolling_value="")
        assert check_rolling(contact, ROLLING_FIELD_ID, "msg-001") is False

    def test_dedupe_against_rolling_list(self):
        """spec §7.3 scenario #6 — duplicate detected against rolling list."""
        message_id = "msg-duplicate-001"
        h = hash16(message_id)
        # List contains the hash of this message_id
        rolling_value = json.dumps([hash16("other-msg"), h, hash16("yet-another")])
        contact = _make_contact(rolling_value=rolling_value)

        result = check_rolling(contact, ROLLING_FIELD_ID, message_id)

        assert result is True, "Should detect hash in rolling list"

    def test_hash_not_in_list_returns_false(self):
        rolling_value = json.dumps([hash16("msg-A"), hash16("msg-B")])
        contact = _make_contact(rolling_value=rolling_value)
        assert check_rolling(contact, ROLLING_FIELD_ID, "msg-C") is False

    def test_malformed_json_returns_false_and_logs_warning(self, caplog):
        import logging
        contact = _make_contact(rolling_value="not-valid-json[{{{")
        with caplog.at_level(logging.WARNING, logger="reply_router.dedupe"):
            result = check_rolling(contact, ROLLING_FIELD_ID, "msg-001")
        assert result is False
        assert "unparseable" in caplog.text


# ---------------------------------------------------------------------------
# check_soft_lock
# ---------------------------------------------------------------------------

class TestCheckSoftLock:
    def test_absent_when_no_field(self):
        contact = _make_contact(soft_lock_value=None)
        assert check_soft_lock(contact, SOFT_LOCK_FIELD_ID, "msg-001") == SoftLockState.ABSENT

    def test_absent_when_empty_string(self):
        contact = _make_contact(soft_lock_value="")
        assert check_soft_lock(contact, SOFT_LOCK_FIELD_ID, "msg-001") == SoftLockState.ABSENT

    @freeze_time(FROZEN_NOW)
    def test_soft_lock_in_flight_blocks_concurrent_webhook(self):
        """spec §7.3 scenario #7 — same message_id, fresh lock → IN_FLIGHT."""
        message_id = "msg-in-flight-007"
        # Lock was acquired 5 minutes ago (fresh, well within TTL)
        fresh_ts = FROZEN_TS - 300
        soft_lock_value = f"{message_id}:{fresh_ts}"
        contact = _make_contact(soft_lock_value=soft_lock_value)

        result = check_soft_lock(contact, SOFT_LOCK_FIELD_ID, message_id)

        assert result == SoftLockState.IN_FLIGHT

    @freeze_time(FROZEN_NOW)
    def test_soft_lock_stale_is_overwritten(self):
        """spec §7.3 scenario #8 — lock older than TTL → STALE (caller may overwrite)."""
        message_id = "msg-stale-008"
        # Lock is 11 minutes old — beyond 10-min TTL
        stale_ts = FROZEN_TS - (SOFT_LOCK_TTL_SEC + 60)
        soft_lock_value = f"{message_id}:{stale_ts}"
        contact = _make_contact(soft_lock_value=soft_lock_value)

        result = check_soft_lock(contact, SOFT_LOCK_FIELD_ID, message_id)

        assert result == SoftLockState.STALE

    @freeze_time(FROZEN_NOW)
    def test_fresh_lock_different_message_id_returns_absent(self):
        """Different message currently in-flight — this message can proceed."""
        other_message_id = "msg-other-999"
        this_message_id = "msg-this-001"
        fresh_ts = FROZEN_TS - 60  # 1 minute ago, well within TTL
        soft_lock_value = f"{other_message_id}:{fresh_ts}"
        contact = _make_contact(soft_lock_value=soft_lock_value)

        result = check_soft_lock(contact, SOFT_LOCK_FIELD_ID, this_message_id)

        assert result == SoftLockState.ABSENT

    def test_malformed_lock_value_returns_absent(self):
        contact = _make_contact(soft_lock_value="no-colon-ts-here")
        assert check_soft_lock(contact, SOFT_LOCK_FIELD_ID, "msg-001") == SoftLockState.ABSENT

        contact2 = _make_contact(soft_lock_value="msg:not-an-int")
        assert check_soft_lock(contact2, SOFT_LOCK_FIELD_ID, "msg-001") == SoftLockState.ABSENT


# ---------------------------------------------------------------------------
# acquire_soft_lock
# ---------------------------------------------------------------------------

class TestAcquireSoftLock:
    @freeze_time(FROZEN_NOW)
    def test_calls_update_contact_with_correct_format(self, mock_client):
        message_id = "msg-acquire-001"
        acquire_soft_lock(mock_client, CONTACT_ID, SOFT_LOCK_FIELD_ID, message_id)

        mock_client.update_contact.assert_called_once_with(
            CONTACT_ID,
            custom_fields={SOFT_LOCK_FIELD_ID: f"{message_id}:{FROZEN_TS}"},
        )


# ---------------------------------------------------------------------------
# mark_complete
# ---------------------------------------------------------------------------

class TestMarkComplete:
    def test_appends_hash_and_clears_soft_lock(self, mock_client):
        existing = [hash16("old-msg-1"), hash16("old-msg-2")]
        contact = _make_contact(
            rolling_value=json.dumps(existing),
            soft_lock_value=f"msg-new-001:{FROZEN_TS}",
        )
        message_id = "msg-new-001"

        mark_complete(mock_client, contact, ROLLING_FIELD_ID, SOFT_LOCK_FIELD_ID, message_id)

        mock_client.update_contact.assert_called_once()
        _, kwargs = mock_client.update_contact.call_args
        custom_fields = kwargs["custom_fields"]

        updated_list = json.loads(custom_fields[ROLLING_FIELD_ID])
        assert hash16(message_id) in updated_list
        assert len(updated_list) == 3  # two existing + one new

        assert custom_fields[SOFT_LOCK_FIELD_ID] == "", "soft_lock must be cleared"

    def test_idempotent_does_not_duplicate_hash(self, mock_client):
        message_id = "msg-already-in-list"
        existing = [hash16(message_id), hash16("other-msg")]
        contact = _make_contact(rolling_value=json.dumps(existing))

        mark_complete(mock_client, contact, ROLLING_FIELD_ID, SOFT_LOCK_FIELD_ID, message_id)

        _, kwargs = mock_client.update_contact.call_args
        updated_list = json.loads(kwargs["custom_fields"][ROLLING_FIELD_ID])
        assert updated_list.count(hash16(message_id)) == 1, "Should not duplicate the hash"
        assert len(updated_list) == 2  # unchanged count

    def test_truncates_at_50_items_and_drops_oldest(self, mock_client):
        # Pre-load exactly ROLLING_LIST_MAX (50) items
        existing = [hash16(f"old-msg-{i}") for i in range(ROLLING_LIST_MAX)]
        contact = _make_contact(rolling_value=json.dumps(existing))
        message_id = "msg-newest"

        mark_complete(mock_client, contact, ROLLING_FIELD_ID, SOFT_LOCK_FIELD_ID, message_id)

        _, kwargs = mock_client.update_contact.call_args
        updated_list = json.loads(kwargs["custom_fields"][ROLLING_FIELD_ID])

        assert len(updated_list) == ROLLING_LIST_MAX, "List must be truncated to 50"
        assert updated_list[-1] == hash16(message_id), "Newest hash must be last"
        assert updated_list[0] == hash16("old-msg-1"), "Oldest (index 0) must be dropped"

    def test_empty_rolling_field_starts_new_list(self, mock_client):
        contact = _make_contact(rolling_value=None)
        message_id = "msg-first"

        mark_complete(mock_client, contact, ROLLING_FIELD_ID, SOFT_LOCK_FIELD_ID, message_id)

        _, kwargs = mock_client.update_contact.call_args
        updated_list = json.loads(kwargs["custom_fields"][ROLLING_FIELD_ID])
        assert updated_list == [hash16(message_id)]
