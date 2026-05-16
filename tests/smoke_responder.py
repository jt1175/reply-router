"""Live smoke tests for the contextual responder.

These tests verify Claude's actual output against the spec's load-bearing rules
(no specific prices, no excluded services, no commitments). They live in smoke_*
because they hit real Claude — cost ~$0.04 per full run.

Run: make verify-live
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from dotenv import load_dotenv

from reply_router.config import BusinessContext
from reply_router.responder import generate_contextual

load_dotenv()

FIXTURES = Path(__file__).parent / "fixtures" / "replies"


def _bc() -> BusinessContext:
    return BusinessContext(
        company_name="Clear Facility Services",
        service_area="Twin Cities (45-min radius of Minneapolis/St. Paul)",
        services_offered=["warehouse", "office", "logistics"],
        services_not_offered=["restaurants", "hospitality", "retail", "auto dealerships"],
        pricing_response="Pricing depends on size, frequency, and specifics — happy to scope on a quick walkthrough call.",
        booking_link="https://calendar.app.google/abc123",
    )


def _generate(reply_text: str, classification: str = "info_request") -> str:
    return generate_contextual(
        classification=classification,
        reply_text=reply_text,
        account={"contact_name": "Pat", "company_name": "Hennepin Logistics",
                 "contact_title": "Operations Manager"},
        business_context=_bc(),
        sender_persona_name="Sarah Jones",
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
    ).text


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no key")
def test_contextual_response_does_not_quote_price():
    """§7.3 #14 — pricing question must NOT include $ figures."""
    reply = (FIXTURES / "info_request" / "03_price.txt").read_text()
    out = _generate(reply, "info_request")
    assert "$" not in out, f"response contains $: {out}"
    # Must contain phrasing that maps to pricing_response
    assert re.search(r"\b(depends|scope|walkthrough|call)\b", out, re.I), (
        f"response should defer to scoping call: {out}"
    )


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no key")
def test_contextual_response_does_not_offer_excluded_service():
    """§7.3 #15 — restaurants are in services_not_offered, must NOT be offered."""
    reply = (FIXTURES / "info_request" / "04_excluded_service.txt").read_text()
    out = _generate(reply, "info_request")
    lower = out.lower()
    # Must NOT promise restaurant cleaning
    assert not re.search(r"\b(yes,? we|we can|we do|happy to)\b.{0,40}\b(restaurant|food service)\b", lower), (
        f"response appears to offer restaurant cleaning: {out}"
    )
    # SHOULD acknowledge restaurants aren't a focus
    assert "restaurant" in lower, f"response should mention restaurants explicitly: {out}"


COMMITMENT_PHRASES = [
    r"\bwe can start\b",
    r"\bwe will start\b",
    r"\bwe'?ll have you (running|started|going|ready)\b",
    r"\bby (next |this )?\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|month)\b",
    r"\bwithin \d+ (days?|weeks?|months?)\b",
    r"\bin \d+ (days?|weeks?|months?)\b",
    r"\bwe can handle (that|a 100,000)\b",
    r"\bno minimum\b",
]

DEFLECTION_PHRASES = [
    r"\b(quick call|walkthrough|happy to confirm|scope (it|this) on|on a call|over the phone)\b",
]


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no key")
@pytest.mark.parametrize("fixture_name", [
    "objection/03_commitments_start.txt",
    "objection/04_commitments_capacity.txt",
    "objection/05_commitments_contract.txt",
])
def test_responder_does_not_make_commitments(fixture_name):
    """§7.3 #15b — load-bearing AI-liability content guard."""
    reply = (FIXTURES / fixture_name).read_text()
    out = _generate(reply, classification="objection")
    lower = out.lower()
    for pat in COMMITMENT_PHRASES:
        m = re.search(pat, lower)
        assert m is None, (
            f"\n  fixture: {fixture_name}"
            f"\n  matched commitment phrase: {pat!r} → {m.group(0)!r}"
            f"\n  full response:\n{out}"
        )
    # AND the response must include deflection language
    assert any(re.search(p, lower) for p in DEFLECTION_PHRASES), (
        f"\n  fixture: {fixture_name}"
        f"\n  response missing deflection (\"happy to confirm on a quick call\" / similar):"
        f"\n{out}"
    )
