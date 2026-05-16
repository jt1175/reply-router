"""Live smoke test for classifier — hits real Claude against each fixture reply.

Cost: 12 fixtures × ~$0.005 each ≈ $0.06 per run.
Run: make verify-live
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from reply_router.classifier import classify

load_dotenv()

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "replies"


def _load_pairs():
    """Yield (expected_classification, fixture_text, fixture_path) for each .txt under replies/."""
    pairs = []
    for category_dir in sorted(FIXTURES_DIR.iterdir()):
        if not category_dir.is_dir():
            continue
        expected = category_dir.name
        for f in sorted(category_dir.glob("*.txt")):
            pairs.append((expected, f.read_text(), f))
    return pairs


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
@pytest.mark.parametrize("expected,reply_text,path", _load_pairs())
def test_classifier_matches_fixture_expectation(expected, reply_text, path):
    result = classify(
        reply_text=reply_text,
        sender_persona="Sarah Jones",
        sender_email="sarah.jones@clearfacilitymn.com",
        original_subject="Quick question about your space",
        company_name="Clear Facility Services",
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
    )
    assert result["classification"] == expected, (
        f"\n  fixture: {path.relative_to(FIXTURES_DIR)}"
        f"\n  expected: {expected}"
        f"\n  got: {result['classification']} (confidence={result['confidence']})"
        f"\n  reasoning: {result.get('reasoning', '—')}"
        f"\n  reply text:\n{reply_text}"
    )
    assert result["confidence"] in ("high", "medium", "low")
