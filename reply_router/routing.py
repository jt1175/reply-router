"""Pure routing function: classification + confidence + config → action bundle.

Hard-coded carve-out: `unsubscribe` bypasses the confidence gate.
See spec §5.4 for full reasoning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from reply_router.config import ClassificationAction

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class ActionBundle:
    pipeline_stage_id: str
    tags_to_add: list[str]
    nurture_bucket: str | None
    contract_end_date_iso: str | None
    dnc: bool
    slack_notify: bool
    send_mode: Literal["auto_send", "shadow_send"]
    extra_flags: dict[str, bool] = field(default_factory=dict)


def route(
    classification: str,
    confidence: str,
    suggested_followup_date_iso: str | None,
    classification_action: ClassificationAction,
    ambiguous_contact: bool = False,
    skeleton_contact: bool = False,
    booking_link_placeholder: bool = False,
) -> ActionBundle:
    # Build tags first (hard-coded by classification per spec §4.1 step 7)
    tags = ["replied", classification]
    if confidence == "low":
        tags.append("low_confidence")
    if ambiguous_contact:
        tags.append("ambiguous_contact_match")
    if skeleton_contact:
        tags.append("auto_created_from_reply")

    # Unsubscribe carve-out (spec §5.4) — bypasses confidence gate
    if classification == "unsubscribe":
        if confidence == "low":
            tags.append("low_confidence_unsubscribe")
        return ActionBundle(
            pipeline_stage_id=classification_action.pipeline_stage_id,
            tags_to_add=tags,
            nurture_bucket=None,
            contract_end_date_iso=None,
            dnc=True,
            slack_notify=(confidence == "low"),  # URGENT only if low-confidence
            send_mode="auto_send",
            extra_flags={"low_confidence_unsubscribe": confidence == "low"},
        )

    # Confidence gate for all other classifications
    confident_enough = (
        CONFIDENCE_RANK[confidence] >= CONFIDENCE_RANK[classification_action.min_confidence]
    )
    if classification_action.auto_send and confident_enough and not ambiguous_contact and not booking_link_placeholder:
        send_mode = "auto_send"
    else:
        send_mode = "shadow_send"

    return ActionBundle(
        pipeline_stage_id=classification_action.pipeline_stage_id,
        tags_to_add=tags,
        nurture_bucket=classification_action.nurture_bucket,
        contract_end_date_iso=(
            suggested_followup_date_iso if classification == "not_now" else None
        ),
        dnc=False,
        slack_notify=classification_action.slack_notify,
        send_mode=send_mode,
    )
