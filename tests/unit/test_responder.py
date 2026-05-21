"""Unit tests for reply_router.responder — template-mode generation + shadow gate."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from reply_router.config import BusinessContext
from reply_router.responder import (
    UNSUBSCRIBE_STATIC,
    ResponderResult,
    _clean_value_prop,
    _format_common_objections,
    _format_credentials,
    _format_value_props,
    _is_unconfirmed,
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
    """> 1200 char response is failure. Cap was raised from 800 → 1200 on
    2026-05-21 to accommodate info_request responses that need to answer
    2-3 specific prospect questions + include the booking link URL (which
    is itself ~100 chars after token expansion)."""
    mock_anthropic_cls.return_value = _mock_claude("x" * 1300)
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


# --- Contextual mode unit tests (Task 3.5) ---

@patch("reply_router.responder.Anthropic")
def test_contextual_returns_text_on_happy_path(mock_anthropic_cls):
    mock_anthropic_cls.return_value = _mock_claude(
        "Happy to scope this on a quick walkthrough — here's a link: https://calendar.app.google/abc"
    )
    from reply_router.responder import generate_contextual
    result = generate_contextual(
        classification="info_request",
        reply_text="how much?",
        account={"contact_name": "X", "company_name": "Y", "contact_title": "Z"},
        business_context=_bc(),
        sender_persona_name="Sarah",
        anthropic_api_key="k",
    )
    assert "walkthrough" in result.text.lower()
    assert not result.failed


@patch("reply_router.responder.Anthropic")
def test_contextual_short_response_marked_failed(mock_anthropic_cls):
    mock_anthropic_cls.return_value = _mock_claude("ok")
    from reply_router.responder import generate_contextual
    result = generate_contextual(
        classification="info_request", reply_text="?",
        account={"contact_name": "X", "company_name": "Y", "contact_title": "Z"},
        business_context=_bc(), sender_persona_name="S", anthropic_api_key="k",
    )
    assert result.failed
    assert result.requires_shadow


def test_contextual_rejects_non_contextual_classification():
    from reply_router.responder import generate_contextual
    with pytest.raises(ValueError, match="non-contextual"):
        generate_contextual(
            classification="interested", reply_text="x",
            account={}, business_context=_bc(),
            sender_persona_name="S", anthropic_api_key="k",
        )


# --- AWAITING_SHAWN_CONFIRM filter tests ---

def test_is_unconfirmed_detects_awaiting_shawn():
    assert _is_unconfirmed("JT_DRAFT_AWAITING_SHAWN_CONFIRM — some claim")
    assert _is_unconfirmed("AWAITING_SHAWN something")


def test_is_unconfirmed_detects_tbd_prefix():
    assert _is_unconfirmed("TBD_CFS_PHONE")
    assert _is_unconfirmed("TBD_confirm_with_Shawn — typical: 'something'")


def test_is_unconfirmed_false_for_confirmed_value():
    assert not _is_unconfirmed("Fully insured and bonded; happy to share a COI.")
    assert not _is_unconfirmed("Specialized auto-scrubber equipment")


def test_is_unconfirmed_handles_non_string():
    assert not _is_unconfirmed(None)
    assert not _is_unconfirmed(["list", "not string"])


def test_clean_value_prop_strips_typical_prefix():
    cleaned = _clean_value_prop("TBD_confirm — typical: 'Real content here.'")
    assert cleaned == "Real content here."


def test_clean_value_prop_strips_jt_draft_prefix():
    cleaned = _clean_value_prop("JT_DRAFT_2026-05-20 — Real content here.")
    assert cleaned == "Real content here."


def test_clean_value_prop_passes_through_clean_values():
    assert _clean_value_prop("Already a clean string") == "Already a clean string"


# --- _format_value_props ---

def test_format_value_props_drops_unconfirmed_entries():
    props = [
        "Specialized auto-scrubber equipment for hard-floor properties",
        "TBD_confirm_with_Shawn — typical: 'not surfaced'",
        "Twin Cities owner-operated",
    ]
    out = _format_value_props(props)
    assert "auto-scrubber" in out
    assert "Twin Cities" in out
    assert "TBD_" not in out
    assert "not surfaced" not in out


def test_format_value_props_empty_when_all_unconfirmed():
    props = ["TBD_a", "AWAITING_SHAWN something"]
    assert _format_value_props(props) == "(none listed)"


def test_format_value_props_handles_non_list():
    assert _format_value_props(None) == "(none listed)"
    assert _format_value_props("not a list") == "(none listed)"


# --- _format_credentials (safety-critical) ---

def test_format_credentials_returns_none_when_all_awaiting():
    """When every credential is marked AWAITING, output must explicitly tell Claude NOT to reference any."""
    creds = {
        "issa": "JT_DRAFT_AWAITING_SHAWN_CONFIRM — ISSA-certified team",
        "insurance": "JT_DRAFT_AWAITING_SHAWN_CONFIRM — fully insured",
    }
    out = _format_credentials(creds)
    assert "none confirmed" in out
    assert "do not reference" in out
    assert "ISSA" not in out
    assert "insured" not in out


def test_format_credentials_surfaces_only_confirmed():
    creds = {
        "issa": "JT_DRAFT_AWAITING_SHAWN_CONFIRM — ISSA-certified",
        "insurance": "Fully insured and bonded; happy to share a COI.",
        "background_checks": "All on-site staff background-checked and W-2 employees.",
    }
    out = _format_credentials(creds)
    assert "insured" in out
    assert "background-checked" in out
    assert "ISSA" not in out


def test_format_credentials_skips_doc_keys():
    """Underscore-prefixed keys are documentation, not real credentials."""
    creds = {
        "_doc": "Confirmed creds: ISSA, etc.",  # This should NOT leak — starts with _
        "insurance": "Fully insured.",
    }
    out = _format_credentials(creds)
    assert "Confirmed creds" not in out
    assert "Fully insured." in out


# --- _format_common_objections ---

def test_format_objections_includes_all_confirmed_keys():
    obj = {
        "already_have_vendor": "Totally understood — most folks already have someone.",
        "send_pricing_first": "Pricing hinges on a walkthrough.",
    }
    out = _format_common_objections(obj)
    assert "already_have_vendor" in out
    assert "send_pricing_first" in out
    assert "already have someone" in out


def test_format_objections_skips_unconfirmed():
    obj = {
        "real": "Use this one verbatim.",
        "fake": "JT_DRAFT_AWAITING_SHAWN_CONFIRM — should be hidden",
    }
    out = _format_common_objections(obj)
    assert "Use this one" in out
    assert "should be hidden" not in out


def test_format_objections_empty_returns_sentinel():
    assert _format_common_objections({}) == "(none configured)"
    assert _format_common_objections(None) == "(none configured)"


# --- End-to-end with real cfg fixture ---

def test_contextual_prompt_uses_business_context_correctly():
    """Verify the assembled prompt: includes confirmed value_props, excludes AWAITING credentials."""
    from reply_router.responder import generate_contextual
    bc_data = dict(
        company_name="Clear Facility Services",
        service_area="Twin Cities",
        services_offered=["office cleaning"],
        services_not_offered=["restaurants"],
        pricing_response="Depends on size.",
        booking_link="https://example.com/book",
        # Extras (pydantic allows them via extra="allow"):
        value_props=["Auto-scrubber equipment", "W-2 employees not subcontractors"],
        credential_mentions={
            "issa": "JT_DRAFT_AWAITING_SHAWN_CONFIRM — ISSA-certified",
            "insurance": "Fully insured.",
        },
        common_objections={
            "already_have_vendor": "Totally understood — happy to be on your bench.",
        },
    )
    bc = BusinessContext(**bc_data)

    with patch("reply_router.responder.Anthropic") as mock_anthropic_cls:
        mock_anthropic_cls.return_value = _mock_claude("Test response that is long enough to pass length check.")
        generate_contextual(
            classification="objection", reply_text="we already have a vendor",
            account={"contact_name": "Pat", "company_name": "Acme", "contact_title": "GM"},
            business_context=bc, sender_persona_name="Sarah",
            anthropic_api_key="k",
        )
        # Inspect what was sent to Claude
        sent_prompt = mock_anthropic_cls.return_value.messages.create.call_args.kwargs["messages"][0]["content"]
        # Confirmed value_props present
        assert "Auto-scrubber equipment" in sent_prompt
        assert "W-2 employees" in sent_prompt
        # Confirmed credential present, unconfirmed NOT
        assert "Fully insured" in sent_prompt
        assert "ISSA" not in sent_prompt
        # Confirmed objection guidance present
        assert "happy to be on your bench" in sent_prompt
        # Rules text present
        assert "NEVER claim a credential" in sent_prompt
