"""Exhaustive tests for reply_router.routing.route().

Coverage:
  A. Unsubscribe carve-out (spec §5.4 bypass)
  B. Confidence gate — auto_send=True classifications
  C. Confidence gate — auto_send=False classifications
  D. Special flag overrides (ambiguous_contact, skeleton_contact, booking_link_placeholder)
  E. Tag construction
  F. not_now date pass-through
  G. nurture_bucket pass-through
"""
from __future__ import annotations

import pytest

from reply_router.config import ClassificationAction
from reply_router.routing import route, ActionBundle


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def unsub_action() -> ClassificationAction:
    return ClassificationAction(
        auto_send=True,
        min_confidence="low",
        slack_notify=False,
        pipeline_stage_id="s_closed_lost",
    )


@pytest.fixture
def wrong_person_action() -> ClassificationAction:
    """auto_send=True, min_confidence=medium — gate fires at medium+."""
    return ClassificationAction(
        auto_send=True,
        min_confidence="medium",
        slack_notify=True,
        pipeline_stage_id="s_nurture",
    )


@pytest.fixture
def interested_action() -> ClassificationAction:
    """auto_send=False — gate never fires regardless of confidence."""
    return ClassificationAction(
        auto_send=False,
        min_confidence="high",
        slack_notify=True,
        pipeline_stage_id="s_new_reply",
    )


@pytest.fixture
def not_now_action() -> ClassificationAction:
    """auto_send=False, has nurture_bucket."""
    return ClassificationAction(
        auto_send=False,
        min_confidence="medium",
        slack_notify=True,
        pipeline_stage_id="s_nurture",
        nurture_bucket="not_now",
    )


# ---------------------------------------------------------------------------
# A. Unsubscribe carve-out
# ---------------------------------------------------------------------------

def test_unsubscribe_high_confidence_bypasses_gate(unsub_action):
    """A-1: unsubscribe + high → auto_send, dnc=True, no urgent slack, no low-confidence tags."""
    bundle = route("unsubscribe", "high", None, unsub_action)

    assert bundle.send_mode == "auto_send"
    assert bundle.dnc is True
    assert bundle.slack_notify is False
    assert "low_confidence" not in bundle.tags_to_add
    assert "low_confidence_unsubscribe" not in bundle.tags_to_add
    assert bundle.extra_flags.get("low_confidence_unsubscribe") is False


def test_unsubscribe_medium_confidence_bypasses_gate(unsub_action):
    """A-2: unsubscribe + medium → auto_send, dnc=True, no urgent slack."""
    bundle = route("unsubscribe", "medium", None, unsub_action)

    assert bundle.send_mode == "auto_send"
    assert bundle.dnc is True
    assert bundle.slack_notify is False
    assert "low_confidence_unsubscribe" not in bundle.tags_to_add


def test_unsubscribe_low_confidence_urgent_carve_out(unsub_action):
    """A-3: unsubscribe + low → auto_send, dnc=True, slack_notify=True (URGENT),
    tags include both 'low_confidence' and 'low_confidence_unsubscribe',
    extra_flags['low_confidence_unsubscribe']=True."""
    bundle = route("unsubscribe", "low", None, unsub_action)

    assert bundle.send_mode == "auto_send"
    assert bundle.dnc is True
    assert bundle.slack_notify is True
    assert "low_confidence" in bundle.tags_to_add
    assert "low_confidence_unsubscribe" in bundle.tags_to_add
    assert bundle.extra_flags.get("low_confidence_unsubscribe") is True


def test_unsubscribe_always_clears_date_and_nurture(unsub_action):
    """A-4: unsubscribe ignores followup date and never sets nurture_bucket."""
    bundle = route("unsubscribe", "high", "2026-09-01", unsub_action)

    assert bundle.contract_end_date_iso is None
    assert bundle.nurture_bucket is None


# ---------------------------------------------------------------------------
# B. Confidence gate — auto_send=True (wrong_person, min_confidence=medium)
# ---------------------------------------------------------------------------

def test_wrong_person_high_confidence_auto_sends(wrong_person_action):
    """B-5: high >= medium → auto_send."""
    bundle = route("wrong_person", "high", None, wrong_person_action)

    assert bundle.send_mode == "auto_send"
    assert bundle.dnc is False


def test_wrong_person_medium_confidence_exactly_meets_gate(wrong_person_action):
    """B-6: medium >= medium → auto_send (meets gate exactly)."""
    bundle = route("wrong_person", "medium", None, wrong_person_action)

    assert bundle.send_mode == "auto_send"


def test_wrong_person_low_confidence_falls_below_gate(wrong_person_action):
    """B-7: low < medium → shadow_send."""
    bundle = route("wrong_person", "low", None, wrong_person_action)

    assert bundle.send_mode == "shadow_send"
    assert "low_confidence" in bundle.tags_to_add


# ---------------------------------------------------------------------------
# C. Confidence gate — auto_send=False (interested, min_confidence=high)
# ---------------------------------------------------------------------------

def test_interested_high_confidence_still_shadow(interested_action):
    """C-8: auto_send=False means shadow regardless of confidence level."""
    bundle = route("interested", "high", None, interested_action)

    assert bundle.send_mode == "shadow_send"
    assert bundle.dnc is False


def test_interested_medium_confidence_shadow(interested_action):
    """C-9: auto_send=False → shadow_send even at medium."""
    bundle = route("interested", "medium", None, interested_action)

    assert bundle.send_mode == "shadow_send"


def test_low_confidence_forces_shadow_mode_for_interested(interested_action):
    """C-10: interested + low → shadow_send + low_confidence tag.

    Named per plan's Critical-Scenario §7.3 #10 mapping.
    """
    bundle = route("interested", "low", None, interested_action)

    assert bundle.send_mode == "shadow_send"
    assert "low_confidence" in bundle.tags_to_add
    assert bundle.dnc is False


# ---------------------------------------------------------------------------
# D. Special flag overrides
# ---------------------------------------------------------------------------

def test_ambiguous_contact_forces_shadow_and_adds_tag(wrong_person_action):
    """D-11: ambiguous_contact=True overrides auto_send gate → shadow_send."""
    bundle = route("wrong_person", "high", None, wrong_person_action, ambiguous_contact=True)

    assert bundle.send_mode == "shadow_send"
    assert "ambiguous_contact_match" in bundle.tags_to_add


def test_skeleton_contact_adds_tag_but_does_not_change_send_mode(wrong_person_action):
    """D-12: skeleton_contact only adds a tag; does NOT override the gate result.

    wrong_person + high → gate passes → auto_send even when skeleton_contact=True.
    """
    bundle = route("wrong_person", "high", None, wrong_person_action, skeleton_contact=True)

    assert bundle.send_mode == "auto_send"   # gate result unchanged
    assert "auto_created_from_reply" in bundle.tags_to_add


def test_booking_link_placeholder_forces_shadow_for_auto_send_classification(wrong_person_action):
    """D-13a: booking_link_placeholder=True overrides gate for auto_send=True classifications."""
    bundle = route("wrong_person", "high", None, wrong_person_action, booking_link_placeholder=True)

    assert bundle.send_mode == "shadow_send"


def test_booking_link_placeholder_no_effect_when_already_shadow(interested_action):
    """D-13b: booking_link_placeholder=True has no additional effect when auto_send=False."""
    bundle = route("interested", "high", None, interested_action, booking_link_placeholder=True)

    # Already shadow due to auto_send=False; result unchanged
    assert bundle.send_mode == "shadow_send"


# ---------------------------------------------------------------------------
# E. Tag construction
# ---------------------------------------------------------------------------

def test_tags_always_start_with_replied_and_classification(wrong_person_action):
    """E-14: first two tags are always ['replied', <classification>]."""
    bundle = route("wrong_person", "high", None, wrong_person_action)

    assert bundle.tags_to_add[0] == "replied"
    assert bundle.tags_to_add[1] == "wrong_person"


def test_low_confidence_tag_added_only_when_confidence_is_low(wrong_person_action):
    """E-15: 'low_confidence' in tags iff confidence=='low'."""
    high_bundle = route("wrong_person", "high", None, wrong_person_action)
    med_bundle = route("wrong_person", "medium", None, wrong_person_action)
    low_bundle = route("wrong_person", "low", None, wrong_person_action)

    assert "low_confidence" not in high_bundle.tags_to_add
    assert "low_confidence" not in med_bundle.tags_to_add
    assert "low_confidence" in low_bundle.tags_to_add


def test_all_optional_tags_added_together(wrong_person_action):
    """E-16/17: ambiguous_contact_match and auto_created_from_reply added together."""
    bundle = route(
        "wrong_person", "low", None, wrong_person_action,
        ambiguous_contact=True,
        skeleton_contact=True,
    )

    assert "low_confidence" in bundle.tags_to_add
    assert "ambiguous_contact_match" in bundle.tags_to_add
    assert "auto_created_from_reply" in bundle.tags_to_add


# ---------------------------------------------------------------------------
# F. not_now date pass-through
# ---------------------------------------------------------------------------

def test_not_now_passes_followup_date_to_contract_end(not_now_action):
    """F-18: not_now classification passes suggested_followup_date_iso through."""
    bundle = route("not_now", "low", "2026-09-01", not_now_action)

    assert bundle.contract_end_date_iso == "2026-09-01"


def test_non_not_now_ignores_followup_date(interested_action):
    """F-19: other classifications always set contract_end_date_iso=None."""
    bundle = route("interested", "high", "2026-09-01", interested_action)

    assert bundle.contract_end_date_iso is None


def test_not_now_with_none_followup_date(not_now_action):
    """F-18b: not_now with None followup date → contract_end_date_iso=None (no crash)."""
    bundle = route("not_now", "medium", None, not_now_action)

    assert bundle.contract_end_date_iso is None


# ---------------------------------------------------------------------------
# G. nurture_bucket pass-through
# ---------------------------------------------------------------------------

def test_not_now_nurture_bucket_passed_through(not_now_action):
    """G-20: not_now config has nurture_bucket='not_now' → ActionBundle reflects it."""
    bundle = route("not_now", "low", None, not_now_action)

    assert bundle.nurture_bucket == "not_now"


def test_wrong_person_no_nurture_bucket(wrong_person_action):
    """G-21: wrong_person config has no nurture_bucket → ActionBundle.nurture_bucket==None."""
    bundle = route("wrong_person", "high", None, wrong_person_action)

    assert bundle.nurture_bucket is None


def test_unsubscribe_nurture_bucket_always_none_regardless_of_config():
    """G-22: unsubscribe carve-out forces nurture_bucket=None even if config defines one.

    This validates the hard-coded None in the carve-out path.
    """
    action_with_nurture = ClassificationAction(
        auto_send=True,
        min_confidence="low",
        slack_notify=False,
        pipeline_stage_id="s_closed_lost",
        nurture_bucket="some_bucket",
    )
    bundle = route("unsubscribe", "high", None, action_with_nurture)

    assert bundle.nurture_bucket is None


# ---------------------------------------------------------------------------
# H. pipeline_stage_id pass-through
# ---------------------------------------------------------------------------

def test_pipeline_stage_id_passed_through(wrong_person_action):
    """H: pipeline_stage_id from ClassificationAction always propagated."""
    bundle = route("wrong_person", "high", None, wrong_person_action)

    assert bundle.pipeline_stage_id == "s_nurture"


def test_unsubscribe_pipeline_stage_id_passed_through(unsub_action):
    """H: pipeline_stage_id propagated in the carve-out path too."""
    bundle = route("unsubscribe", "high", None, unsub_action)

    assert bundle.pipeline_stage_id == "s_closed_lost"
