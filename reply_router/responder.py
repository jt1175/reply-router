"""Responder — generates reply text. Two modes: template and contextual.

Template mode (this task): unsubscribe is static, others are light Claude
personalization. Contextual mode (Task 3.5): full Claude generation for
info_request and objection.
"""
from __future__ import annotations

import logging
import re

from anthropic import Anthropic

from reply_router.config import BusinessContext

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

PLACEHOLDER_RE = re.compile(r"placeholder", re.IGNORECASE)
LINK_USING_CLASSIFICATIONS = frozenset({"interested", "info_request", "objection"})
TEMPLATE_CLASSIFICATIONS = frozenset({"unsubscribe", "interested", "not_now", "wrong_person"})

UNSUBSCRIBE_STATIC = "Removed you from our list. Sorry for the interruption."


class ResponderResult:
    def __init__(self, text: str, requires_shadow: bool = False, failed: bool = False):
        self.text = text
        self.requires_shadow = requires_shadow
        self.failed = failed


def requires_shadow(classification: str, business_context: BusinessContext) -> bool:
    """Returns True iff the booking-link sentinel forces shadow mode for this classification."""
    if classification not in LINK_USING_CLASSIFICATIONS:
        return False
    return bool(PLACEHOLDER_RE.search(business_context.booking_link or ""))


def _template_prompt(classification: str, account: dict, business_context: BusinessContext) -> str:
    """Light personalization prompt for template-mode classifications."""
    base = (
        f"Write a 1-2 sentence opening for a cold-reply response.\n"
        f"Contact: {account.get('contact_name', 'there')} at {account.get('company_name', 'their company')}\n"
        f"Their reply context: {classification}\n"
    )
    if classification == "interested":
        return base + (
            f"Personalize a warm acknowledgment, then offer the walkthrough link: "
            f"{business_context.booking_link}\n"
            "Sign off as the sender persona. Return ONLY the email text."
        )
    if classification == "not_now":
        return base + (
            "Acknowledge their timing and ask when would be better. If they mentioned a timeframe, "
            "reference it. Return ONLY the email text."
        )
    if classification == "wrong_person":
        return base + (
            f"Thank them, ask who handles facility decisions at {account.get('company_name')}. "
            "Return ONLY the email text."
        )
    raise ValueError(f"template-mode prompt not defined for classification: {classification}")


def generate_template(
    classification: str,
    account: dict,
    business_context: BusinessContext,
    anthropic_api_key: str,
) -> ResponderResult:
    """Generate a template-mode response for unsubscribe/interested/not_now/wrong_person."""
    if classification == "unsubscribe":
        return ResponderResult(text=UNSUBSCRIBE_STATIC)

    if classification not in TEMPLATE_CLASSIFICATIONS:
        raise ValueError(
            f"generate_template called with non-template classification: {classification}. "
            f"Use generate_contextual for info_request/objection."
        )

    if requires_shadow(classification, business_context):
        # Caller forces shadow_send; still generate the draft for the human to review
        logger.info("requires_shadow=True for classification=%s (booking link placeholder)", classification)

    prompt = _template_prompt(classification, account, business_context)
    client = Anthropic(api_key=anthropic_api_key)
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (msg.content[0].text if msg.content else "").strip()
    except Exception as exc:
        logger.error("template generation failed: %s", exc)
        return ResponderResult(text="", failed=True)

    if not (20 <= len(text) <= 800):
        logger.warning("response length validation failed: len=%d", len(text))
        return ResponderResult(text=text, requires_shadow=True, failed=True)

    return ResponderResult(
        text=text,
        requires_shadow=requires_shadow(classification, business_context),
    )


CONTEXTUAL_CLASSIFICATIONS = frozenset({"info_request", "objection"})

CONTEXTUAL_SYSTEM_PROMPT = """You are responding to a prospect's email on behalf of {company_name}, a commercial cleaning company in {service_area}.

PROSPECT'S REPLY:
{reply_text}

PROSPECT INFO:
Name: {contact_name}
Company: {contact_company}
Title: {contact_title}

BUSINESS CONTEXT:
Services offered: {services_offered}
Services NOT offered (politely decline if asked): {services_not_offered}
Pricing response (use verbatim or paraphrase — NEVER quote specific dollar figures): {pricing_response}
Booking link: {booking_link}

LOAD-BEARING RULES (do not violate; failures cause test failures):
- Answer their specific question or address their specific objection FIRST.
- NEVER quote specific prices — use the pricing_response from business_context verbatim or paraphrase. Never include a "$" symbol or a number with currency.
- NEVER make commitments about timing, capacity, or scope of services.
  - If asked "when could you start?" — do NOT say "we can start by [date]" or "we'll have you running within [N] weeks." Say "happy to confirm specifics on a quick call."
  - If asked "can you handle [X size] facility?" — confirm only if it's clearly in services_offered; otherwise say "happy to scope it on a walkthrough."
  - If asked about contract length / minimums — defer: "happy to walk through what works for your situation on a call."
  - This rule exists because the responder cannot verify the client's current capacity, schedule, or contract bandwidth — making commitments here creates legal/operational risk.
- If they asked about a service we offer (services_offered), confirm it specifically by name.
- If they asked about a service we don't do (services_not_offered), politely say it's not our focus area. NEVER promise to do an excluded service.
- Keep it to 3-5 sentences max.
- End with the booking link as the next step — UNLESS the response naturally doesn't lead there (e.g., a flat-no objection).
- Sign off as {sender_persona_name}.

TONE GUIDANCE:
- Be helpful and informative, not evasive.
- Professional but not stiff, transparent, client-focused.
- Sound human, not like a bot.

Return ONLY the email response text. No subject line, no JSON wrapping."""


def generate_contextual(
    classification: str,
    reply_text: str,
    account: dict,
    business_context: BusinessContext,
    sender_persona_name: str,
    anthropic_api_key: str,
) -> ResponderResult:
    """Full-Claude response for info_request / objection.

    Per spec Appendix C.2 + critical scenarios #14, #15, #15b.
    """
    if classification not in CONTEXTUAL_CLASSIFICATIONS:
        raise ValueError(
            f"generate_contextual called with non-contextual classification: {classification}"
        )

    prompt = CONTEXTUAL_SYSTEM_PROMPT.format(
        company_name=business_context.company_name,
        service_area=business_context.service_area,
        reply_text=reply_text,
        contact_name=account.get("contact_name", "there"),
        contact_company=account.get("company_name", "their company"),
        contact_title=account.get("contact_title", "—"),
        services_offered=", ".join(business_context.services_offered) or "(none listed)",
        services_not_offered=", ".join(business_context.services_not_offered) or "(none listed)",
        pricing_response=business_context.pricing_response,
        booking_link=business_context.booking_link,
        sender_persona_name=sender_persona_name,
    )

    client = Anthropic(api_key=anthropic_api_key)
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (msg.content[0].text if msg.content else "").strip()
    except Exception as exc:
        logger.error("contextual responder API call failed: %s", exc)
        return ResponderResult(text="", failed=True)

    if not (20 <= len(text) <= 800):
        logger.warning("contextual response length validation failed: len=%d", len(text))
        return ResponderResult(text=text, requires_shadow=True, failed=True)

    return ResponderResult(
        text=text,
        requires_shadow=requires_shadow(classification, business_context),
    )
