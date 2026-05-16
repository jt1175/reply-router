"""Unit tests for reply_router.classifier — Claude JSON parsing + fallback behavior."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from reply_router.classifier import classify, UNKNOWN_RESULT, _strip_fences


def _mock_claude_response(text: str) -> MagicMock:
    msg = MagicMock()
    block = MagicMock()
    block.text = text
    msg.content = [block]
    return msg


def test_strip_fences_handles_plain_json():
    assert _strip_fences('{"x": 1}') == '{"x": 1}'


def test_strip_fences_strips_json_code_fence():
    raw = '```json\n{"x": 1}\n```'
    assert _strip_fences(raw) == '{"x": 1}'


def test_strip_fences_strips_bare_code_fence():
    raw = '```\n{"x": 1}\n```'
    assert _strip_fences(raw) == '{"x": 1}'


def test_strip_fences_handles_empty():
    assert _strip_fences("") == ""
    assert _strip_fences(None) == ""


@patch("reply_router.classifier.Anthropic")
def test_classify_happy_path(mock_anthropic_cls):
    """Claude returns valid JSON first try → parsed and returned as-is."""
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_claude_response(
        '{"classification": "interested", "confidence": "high", '
        '"suggested_followup_date_iso": null, "reasoning": "asked for a call"}'
    )
    mock_anthropic_cls.return_value = fake_client

    result = classify(
        reply_text="Sounds great, can we talk Tuesday?",
        sender_persona="Sarah Jones",
        sender_email="sarah@x.com",
        original_subject="Quick question about your space",
        company_name="Acme Co",
        anthropic_api_key="test-key",
    )
    assert result["classification"] == "interested"
    assert result["confidence"] == "high"
    assert fake_client.messages.create.call_count == 1


@patch("reply_router.classifier.Anthropic")
def test_classify_retries_on_bad_json_then_succeeds(mock_anthropic_cls):
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        _mock_claude_response("not json at all"),
        _mock_claude_response(
            '{"classification": "not_now", "confidence": "medium", '
            '"suggested_followup_date_iso": "2026-09-01", "reasoning": "busy until fall"}'
        ),
    ]
    mock_anthropic_cls.return_value = fake_client

    result = classify(
        reply_text="busy till fall", sender_persona="X", sender_email="x@x",
        original_subject="s", company_name="c", anthropic_api_key="k",
    )
    assert result["classification"] == "not_now"
    assert fake_client.messages.create.call_count == 2


@patch("reply_router.classifier.Anthropic")
def test_classify_second_failure_returns_unknown(mock_anthropic_cls):
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        _mock_claude_response("garbage 1"),
        _mock_claude_response("garbage 2"),
    ]
    mock_anthropic_cls.return_value = fake_client

    result = classify(
        reply_text="?", sender_persona="X", sender_email="x@x",
        original_subject="s", company_name="c", anthropic_api_key="k",
    )
    assert result == UNKNOWN_RESULT
    assert fake_client.messages.create.call_count == 2


@patch("reply_router.classifier.Anthropic")
def test_classify_missing_required_field_returns_unknown_after_retry(mock_anthropic_cls):
    """Shape-valid JSON but missing `confidence` should be treated as malformed."""
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        _mock_claude_response('{"classification": "interested"}'),
        _mock_claude_response('{"only": "wrong"}'),
    ]
    mock_anthropic_cls.return_value = fake_client

    result = classify(
        reply_text="?", sender_persona="X", sender_email="x@x",
        original_subject="s", company_name="c", anthropic_api_key="k",
    )
    assert result["classification"] == "unknown"


@patch("reply_router.classifier.Anthropic")
def test_classify_api_error_raises(mock_anthropic_cls):
    """Anthropic API errors (network/rate-limit) should raise so orchestrator returns 5xx
    and Smartlead retries. Different from JSON-parse errors which we handle gracefully."""
    import anthropic
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = anthropic.APIError(
        message="rate limited", request=MagicMock(), body=None
    )
    mock_anthropic_cls.return_value = fake_client

    with pytest.raises(anthropic.APIError):
        classify(
            reply_text="?", sender_persona="X", sender_email="x@x",
            original_subject="s", company_name="c", anthropic_api_key="k",
        )
