"""Tests for reply_router.approvals — token gen + CSRF + draft storage."""
from __future__ import annotations

import hmac
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from reply_router.approvals import (
    CSRF_TTL_SEC,
    TOKEN_TTL_SEC,
    clear_draft,
    csrf_token,
    find_draft_by_token,
    generate_token,
    is_expired,
    store_draft,
    verify_csrf,
)


def test_generate_token_is_urlsafe_and_long():
    t = generate_token()
    assert len(t) >= 40
    # urlsafe: contains only A-Za-z0-9-_
    assert all(c.isalnum() or c in "-_" for c in t)


def test_generate_token_is_unique():
    tokens = {generate_token() for _ in range(1000)}
    assert len(tokens) == 1000


def test_csrf_token_is_deterministic():
    t1 = csrf_token("secret", "tok_abc", 1700000000)
    t2 = csrf_token("secret", "tok_abc", 1700000000)
    assert t1 == t2
    assert len(t1) == 64  # sha256 hex


def test_csrf_token_differs_per_input():
    a = csrf_token("secret", "tok_abc", 1700000000)
    b = csrf_token("secret", "tok_xyz", 1700000000)
    c = csrf_token("secret", "tok_abc", 1700000001)
    d = csrf_token("other", "tok_abc", 1700000000)
    assert len({a, b, c, d}) == 4


def test_verify_csrf_accepts_fresh_valid():
    now = int(time.time())
    sig = csrf_token("secret", "tok_a", now)
    assert verify_csrf("secret", "tok_a", now, sig) is True


def test_verify_csrf_rejects_expired():
    stale = int(time.time()) - CSRF_TTL_SEC - 10
    sig = csrf_token("secret", "tok_a", stale)
    assert verify_csrf("secret", "tok_a", stale, sig) is False


def test_verify_csrf_rejects_wrong_signature():
    now = int(time.time())
    bad = "0" * 64
    assert verify_csrf("secret", "tok_a", now, bad) is False


def test_verify_csrf_uses_constant_time_comparison():
    """Defense-in-depth: use hmac.compare_digest to avoid timing side-channels."""
    import inspect
    from reply_router import approvals
    src = inspect.getsource(approvals.verify_csrf)
    assert "compare_digest" in src


def test_is_expired_within_ttl():
    iso = datetime.now(timezone.utc).isoformat()
    assert is_expired(iso) is False


def test_is_expired_past_ttl():
    iso = (datetime.now(timezone.utc) - timedelta(seconds=TOKEN_TTL_SEC + 100)).isoformat()
    assert is_expired(iso) is True


def test_store_draft_calls_update_with_three_fields():
    client = MagicMock()
    store_draft(
        client, "ct_1", "cf_token", "cf_text", "cf_created",
        token="tok_abc", draft_text="hello",
    )
    args, kwargs = client.update_contact.call_args
    assert args[0] == "ct_1"
    cfs = kwargs["custom_fields"]
    assert cfs["cf_token"] == "tok_abc"
    assert cfs["cf_text"] == "hello"
    # created_at is an ISO timestamp
    datetime.fromisoformat(cfs["cf_created"])


def test_clear_draft_writes_three_empties():
    client = MagicMock()
    clear_draft(client, "ct_1", "cf_token", "cf_text", "cf_created")
    cfs = client.update_contact.call_args.kwargs["custom_fields"]
    assert cfs == {"cf_token": "", "cf_text": "", "cf_created": ""}


def test_find_draft_by_token_returns_match():
    client = MagicMock()
    client.search_contacts_by_custom_field.return_value = [
        {"id": "ct_1", "customFields": [{"id": "cf_token", "value": "tok_abc"}]}
    ]
    contact = find_draft_by_token(client, "tok_abc", "cf_token")
    assert contact["id"] == "ct_1"
    client.search_contacts_by_custom_field.assert_called_with(
        "cf_token", "tok_abc", unique=True
    )


def test_find_draft_by_token_returns_none_when_absent():
    client = MagicMock()
    client.search_contacts_by_custom_field.return_value = []
    assert find_draft_by_token(client, "tok_xxx", "cf_token") is None
