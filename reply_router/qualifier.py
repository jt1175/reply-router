"""Qualification routing — Claude reads form answers + enriched account context
+ ICP rubric, decides qualify / gray_zone / reject.

Failure mode is deliberately ambiguous: on any error, return gray_zone so a human
reviews. Never auto-reject on classifier failure.

Also hosts the URL + form CSRF token helpers for the qualification flow. URL
tokens authenticate the booking link itself (14-day TTL — prospect may sit on
the email for a week+). Form CSRF tokens prevent replay against the POST
endpoints (1-hour TTL from form render).
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import time

import anthropic
from anthropic import Anthropic

from reply_router.config import BusinessContext

logger = logging.getLogger(__name__)

# ─── URL + form CSRF tokens ──────────────────────────────────────────────────

URL_TOKEN_TTL_SEC = 14 * 24 * 3600   # 14 days — prospect may sit on email for a while
FORM_CSRF_TTL_SEC = 3600              # 1 hour — form must be submitted promptly


def url_token(router_secret: str, contact_id: str, issued_at_unix: int) -> str:
    """HMAC token gating the qualification URL. Encoded as `<iat>.<hmac>`."""
    msg = f"qualify:{contact_id}:{issued_at_unix}".encode("utf-8")
    sig = hmac.new(router_secret.encode("utf-8"), msg, "sha256").hexdigest()
    return f"{issued_at_unix}.{sig}"


def verify_url_token(router_secret: str, contact_id: str, submitted_token: str) -> bool:
    """Validate a URL token. Returns False on bad format, expired, or HMAC mismatch."""
    if not submitted_token or "." not in submitted_token:
        return False
    iat_str, sig = submitted_token.split(".", 1)
    try:
        iat = int(iat_str)
    except ValueError:
        return False
    if time.time() - iat > URL_TOKEN_TTL_SEC:
        return False
    expected = url_token(router_secret, contact_id, iat)
    # Compare full expected token (iat + sig) to submitted_token
    return hmac.compare_digest(expected, submitted_token)


def form_csrf(router_secret: str, contact_id: str, form_issued_at_unix: int) -> str:
    """HMAC for the qualification form's hidden CSRF field."""
    msg = f"qualify-form:{contact_id}:{form_issued_at_unix}".encode("utf-8")
    return hmac.new(router_secret.encode("utf-8"), msg, "sha256").hexdigest()


def verify_form_csrf(
    router_secret: str, contact_id: str, form_issued_at_unix: int, submitted_csrf: str
) -> bool:
    if time.time() - form_issued_at_unix > FORM_CSRF_TTL_SEC:
        return False
    expected = form_csrf(router_secret, contact_id, form_issued_at_unix)
    return hmac.compare_digest(expected, submitted_csrf)


# ─── Claude routing ──────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 600
RETRY_SLEEP = 0.5
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)

VALID_DECISIONS = frozenset({"qualify", "gray_zone", "reject"})
VALID_DEAL_TYPES = frozenset({"velocity", "mid_market", "enterprise", "disqualified"})
VALID_CONFIDENCE = frozenset({"high", "medium", "low"})

FAIL_SAFE_RESULT = {
    "decision": "gray_zone",
    "reasoning": "qualifier failed — routing to human review",
    "deal_type": "disqualified",
    "confidence": "low",
}


FORM_FIELD_SCHEMA = {
    "building_size_sqft": "Integer. Total facility square footage the prospect wants cleaned.",
    "building_type": "One of: office, warehouse, logistics, retail, medical, manufacturing, multi_tenant, other.",
    "current_vendor_status": "One of: have_vendor_happy, have_vendor_evaluating, no_vendor, inhouse.",
    "decision_timeline": "One of: this_month, next_3_months, this_year, not_set.",
    "monthly_budget_range": "One of: under_500, 500_to_2k, 2k_to_5k, 5k_to_15k, 15k_plus, not_disclosed.",
    "best_phone": "String. Phone number for follow-up.",
    "additional_context": "String. Free-text notes from the prospect (optional).",
}


def _strip_fences(text: str) -> str:
    m = FENCE_RE.match(text or "")
    return (m.group(1) if m else text or "").strip()


def _build_prompt(
    form_answers: dict,
    account_context: dict,
    business_context: BusinessContext,
    rubric: str,
) -> str:
    """Construct the prompt Claude uses for qualification routing.

    Includes the full ICP rubric, the form answers verbatim, and any enrichment
    context already captured on the GHL contact (signals, original qualification
    score, deal_type from the engine).
    """
    return (
        "You are routing a prospect through qualification for a commercial cleaning "
        "service. The prospect has filled out a qualification form. Apply the ICP "
        "rubric below to decide: qualify (book a discovery call), gray_zone (needs "
        "human review), or reject (out of scope, send polite decline).\n\n"
        "ICP RUBRIC:\n"
        f"{rubric}\n\n"
        "SERVICES OFFERED:\n"
        f"{', '.join(business_context.services_offered) or '(none listed)'}\n\n"
        "SERVICES NOT OFFERED (always reject if asking for these):\n"
        f"{', '.join(business_context.services_not_offered) or '(none listed)'}\n\n"
        "SERVICE AREA:\n"
        f"{business_context.service_area}\n\n"
        "PROSPECT FORM ANSWERS:\n"
        f"{json.dumps(form_answers, indent=2, default=str)}\n\n"
        "EXISTING ACCOUNT CONTEXT (from prior enrichment, may be empty for direct-form prospects):\n"
        f"{json.dumps(account_context, indent=2, default=str)}\n\n"
        "DECISION RULES (override the rubric where these apply):\n"
        "- If the prospect's industry or building type is in services_not_offered → decision=reject, "
        "deal_type=disqualified.\n"
        "- If current_vendor_status=have_vendor_happy AND decision_timeline=not_set → "
        "decision=reject (not in market), deal_type=disqualified.\n"
        "- If monthly_budget_range=under_500 AND building_size_sqft < 5000 → decision=reject "
        "(below profitable floor for this client), deal_type=disqualified.\n"
        "- If the prospect's signals are clearly in-scope (mid-market sweet spot, recent lease, "
        "active vendor evaluation) → decision=qualify with high confidence.\n"
        "- If any field is missing, ambiguous, or contradictory → decision=gray_zone (human review).\n"
        "- Never qualify a prospect whose industry maps to services_not_offered, regardless "
        "of other signals.\n\n"
        "Return ONLY this JSON, no markdown fences or commentary:\n"
        '{"decision": "qualify|gray_zone|reject", '
        '"deal_type": "velocity|mid_market|enterprise|disqualified", '
        '"confidence": "high|medium|low", '
        '"reasoning": "<one-to-two-sentence summary referencing the specific signals that drove the decision>"}'
    )


def _validate(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    if parsed.get("decision") not in VALID_DECISIONS:
        return False
    if parsed.get("deal_type") not in VALID_DEAL_TYPES:
        return False
    if parsed.get("confidence") not in VALID_CONFIDENCE:
        return False
    if not isinstance(parsed.get("reasoning"), str) or not parsed["reasoning"].strip():
        return False
    return True


def classify_form(
    form_answers: dict,
    account_context: dict,
    business_context: BusinessContext,
    rubric: str,
    anthropic_api_key: str,
) -> dict:
    """Run Claude against the form + context + rubric to qualify/gray/reject.

    Returns a dict with keys: decision, deal_type, confidence, reasoning.
    On any error, returns FAIL_SAFE_RESULT (gray_zone → human review).
    """
    client = Anthropic(api_key=anthropic_api_key)
    prompt = _build_prompt(form_answers, account_context, business_context, rubric)

    for attempt in (1, 2):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            logger.error("qualifier API error attempt=%d err=%s", attempt, exc)
            if attempt == 2:
                return FAIL_SAFE_RESULT.copy()
            time.sleep(RETRY_SLEEP)
            continue
        except Exception as exc:
            logger.error("qualifier unexpected error attempt=%d err=%s", attempt, exc)
            return FAIL_SAFE_RESULT.copy()

        raw = msg.content[0].text if msg.content else ""
        try:
            parsed = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            logger.warning(
                "qualifier JSON parse failed attempt=%d err=%s raw=%r",
                attempt, exc, raw,
            )
            if attempt == 2:
                logger.error("qualifier failed JSON parse after retry — fail-safe to gray_zone")
                return FAIL_SAFE_RESULT.copy()
            time.sleep(RETRY_SLEEP)
            continue

        if not _validate(parsed):
            logger.warning("qualifier validation failed attempt=%d parsed=%r", attempt, parsed)
            if attempt == 2:
                return FAIL_SAFE_RESULT.copy()
            continue

        # Defense in depth: even if Claude returns qualify, force gray_zone on low confidence
        if parsed["decision"] == "qualify" and parsed["confidence"] == "low":
            logger.info("qualifier downgrading low-confidence qualify to gray_zone")
            parsed["decision"] = "gray_zone"

        return parsed

    return FAIL_SAFE_RESULT.copy()
