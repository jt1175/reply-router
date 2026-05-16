"""Reply classifier — one Claude call, structured JSON output."""
from __future__ import annotations

import json
import logging
import re
import time

import anthropic
from anthropic import Anthropic

from reply_router.config import ClientConfig

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 400
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
RETRY_SLEEP = 0.5

UNKNOWN_RESULT = {
    "classification": "unknown",
    "confidence": "low",
    "suggested_followup_date_iso": None,
    "reasoning": "classifier returned malformed JSON after retry",
}


def _strip_fences(text: str) -> str:
    m = FENCE_RE.match(text or "")
    return (m.group(1) if m else text or "").strip()


def _build_prompt(
    reply_text: str,
    sender_persona: str,
    sender_email: str,
    original_subject: str,
    company_name: str,
) -> str:
    return (
        "You are classifying a prospect's email reply to a cold outreach from a "
        "commercial cleaning company.\n\n"
        f"PROSPECT REPLY:\n{reply_text}\n\n"
        f"CONTEXT:\n"
        f"Original outreach was from: {sender_persona} ({sender_email})\n"
        f"Subject of original email: {original_subject}\n"
        f"Prospect company: {company_name}\n\n"
        "Classify the reply as ONE of:\n"
        "- interested: They want to engage, learn more, schedule a call, or are otherwise positive.\n"
        "- not_now: They acknowledge the outreach but indicate timing isn't right.\n"
        "- wrong_person: They're not the decision-maker; they redirect to someone else.\n"
        "- unsubscribe: They explicitly ask to be removed.\n"
        "- objection: They raise a specific concern (price, current vendor, fit, trust).\n"
        "- info_request: They ask a question that requires a substantive answer.\n\n"
        "Return ONLY this JSON, no markdown fences or commentary:\n"
        '{"classification": "interested|not_now|wrong_person|unsubscribe|objection|info_request", '
        '"confidence": "high|medium|low", '
        '"suggested_followup_date_iso": "YYYY-MM-DD" or null, '
        '"reasoning": "<one sentence>"}'
    )


def classify(
    reply_text: str,
    sender_persona: str,
    sender_email: str,
    original_subject: str,
    company_name: str,
    anthropic_api_key: str,
) -> dict:
    client = Anthropic(api_key=anthropic_api_key)
    prompt = _build_prompt(reply_text, sender_persona, sender_email, original_subject, company_name)

    for attempt in (1, 2):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            logger.error("classifier API error attempt=%d err=%s", attempt, exc)
            raise

        raw = msg.content[0].text if msg.content else ""
        try:
            parsed = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            logger.warning(
                "classifier JSON parse failed attempt=%d err=%s raw=%r",
                attempt, exc, raw,
            )
            if attempt == 2:
                logger.error("classifier failed JSON parse after retry — returning unknown")
                return UNKNOWN_RESULT.copy()
            time.sleep(RETRY_SLEEP)
            continue

        # Basic shape validation
        if not isinstance(parsed, dict) or "classification" not in parsed or "confidence" not in parsed:
            logger.warning("classifier invalid shape attempt=%d parsed=%r", attempt, parsed)
            if attempt == 2:
                return UNKNOWN_RESULT.copy()
            continue

        return parsed

    return UNKNOWN_RESULT.copy()
