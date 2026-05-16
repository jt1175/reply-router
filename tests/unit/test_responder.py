"""Unit tests for reply_router.responder — template-mode generation + shadow gate."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from reply_router.config import BusinessContext
from reply_router.responder import (
    UNSUBSCRIBE_STATIC,
    ResponderResult,
    generate_template,
    requires_shadow,
)


def _bc(**overrides) -> BusinessContext:
    base = dict(
        company_name="Clear Facility Services",
        service_area="Twin Cities",
        services_offered=["warehouse", "office"],
        services_not_offered=["restaurants", "auto dealerships"],
        pricing_response="Pricing depends on size and frequency — happy to scope on a call.",
        booking_link="https://calendar.app.google/abc123",
    )
    base.update(overrides)
    return BusinessContext(**base)


def _mock_claude(text: str):
    msg = MagicMock()
    block = MagicMock()
    block.text = text
    msg.content = [block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = msg
    return fake_client


# --- requires_shadow tests (spec §7.3 #16) ---

def test_requires_shadow_returns_true_for_interested_with_placeholder_link():
    bc = _bc(booking_link="https://calendar.app.google/PLACEHOLDER_REPLACE_ME")
    assert requires_shadow("interested", bc) is True


def test_requires_shadow_case_insensitive():
    bc = _bc(booking_link="https://example.com/placeholder-tmp")
    assert requires_shadow("info_request", bc) is True


def test_requires_shadow_false_for_real_link():
    bc = _bc(booking_link="https://calendar.app.google/abc123")
    assert requires_shadow("interested", bc) is False


def test_requires_shadow_false_for_classifications_that_dont_use_link():
    bc = _bc(booking_link="placeholder-here")
    # wrong_person and not_now templates do NOT include the booking link
    assert requires_shadow("wrong_person", bc) is False
    assert requires_shadow("not_now", bc) is False
    assert requires_shadow("unsubscribe", bc) is False


def test_requires_shadow_true_for_objection_with_placeholder():
    bc = _bc(booking_link="https://x/PLACEHOLDER")
    assert requires_shadow("objection", bc) is True


# --- Unsubscribe static ---

def test_unsubscribe_returns_static_no_claude():
    """Unsubscribe MUST never call Claude — it's CAN-SPAM critical and static."""
    with patch("reply_router.responder.Anthropic") as mock_anthropic_cls:
        result = generate_template(
            classification="unsubscribe",
            account={"contact_name": "Pat", "company_name": "Acme"},
            business_context=_bc(),
            anthropic_api_key="unused",
        )
    assert result.text == UNSUBSCRIBE_STATIC
    assert mock_anthropic_cls.call_count == 0
    assert not result.failed
    assert not result.requires_shadow


# --- Template Claude calls ---

@patch("reply_router.responder.Anthropic")
def test_interested_template_returns_claude_text(mock_anthropic_cls):
    mock_anthropic_cls.return_value = _mock_claude(
        "Great to hear, Pat — here's a link to book a walkthrough: https://calendar.app.google/abc123"
    )
    result = generate_template(
        classification="interested",
        account={"contact_name": "Pat", "company_name": "Acme"},
        business_context=_bc(),
        anthropic_api_key="k",
    )
    assert "walkthrough" in result.text
    assert not result.failed
    assert not result.requires_shadow


@patch("reply_router.responder.Anthropic")
def test_interested_with_placeholder_link_sets_requires_shadow(mock_anthropic_cls):
    mock_anthropic_cls.return_value = _mock_claude(
        "Great to hear, Pat — here's a link to book a walkthrough: PLACEHOLDER"
    )
    result = generate_template(
        classification="interested",
        account={"contact_name": "Pat", "company_name": "Acme"},
        business_context=_bc(booking_link="https://x/PLACEHOLDER"),
        anthropic_api_key="k",
    )
    assert result.requires_shadow is True


@patch("reply_router.responder.Anthropic")
def test_short_response_triggers_failure(mock_anthropic_cls):
    """< 20 char response is failure → caller forces shadow + alerts."""
    mock_anthropic_cls.return_value = _mock_claude("Thanks!")
    result = generate_template(
        classification="not_now",
        account={"contact_name": "X", "company_name": "Y"},
        business_context=_bc(),
        anthropic_api_key="k",
    )
    assert result.failed is True
    assert result.requires_shadow is True


@patch("reply_router.responder.Anthropic")
def test_long_response_triggers_failure(mock_anthropic_cls):
    """> 800 char response is failure."""
    mock_anthropic_cls.return_value = _mock_claude("x" * 900)
    result = generate_template(
        classification="wrong_person",
        account={"contact_name": "X", "company_name": "Y"},
        business_context=_bc(),
        anthropic_api_key="k",
    )
    assert result.failed is True


@patch("reply_router.responder.Anthropic")
def test_claude_exception_marked_failed(mock_anthropic_cls):
    mock_anthropic_cls.return_value.messages.create.side_effect = RuntimeError("API down")
    result = generate_template(
        classification="not_now",
        account={"contact_name": "X", "company_name": "Y"},
        business_context=_bc(),
        anthropic_api_key="k",
    )
    assert result.failed is True
    assert result.text == ""


def test_unsupported_classification_raises():
    """generate_template doesn't handle info_request / objection — those go to contextual mode."""
    with pytest.raises((ValueError, KeyError)):
        generate_template(
            classification="info_request",
            account={"contact_name": "X", "company_name": "Y"},
            business_context=_bc(),
            anthropic_api_key="k",
        )
