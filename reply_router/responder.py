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
