"""Reply orchestrator — the pipeline shared by webhook and reconciler.

Built incrementally across Tasks 4.1d–4.1h. This stub exists from 4.1a so that
api/replies.py can import it; full behavior lands per the task sequence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ReplyPayload:
    message_id: str
    from_email: str
    lead_email: str
    campaign_id: str
    reply_text: str
    email_stats_id: str = ""           # Smartlead-specific; needed for send_reply_in_thread
    original_subject: str = ""
    sender_persona: str = ""

    @classmethod
    def from_smartlead_webhook(cls, payload: dict[str, Any]) -> "ReplyPayload":
        """Parse a Smartlead webhook payload. Field names verified against captured
        webhook in Task 5.3 step 1 — update this method then if Smartlead's keys differ.
        """
        return cls(
            message_id=str(payload.get("message_id") or payload.get("id") or ""),
            from_email=str(payload.get("from_email") or payload.get("from") or ""),
            lead_email=str(payload.get("lead_email") or payload.get("to") or ""),
            campaign_id=str(payload.get("campaign_id") or ""),
            reply_text=str(payload.get("reply_text") or payload.get("body") or ""),
            email_stats_id=str(payload.get("email_stats_id") or ""),
            original_subject=str(payload.get("subject") or ""),
            sender_persona=str(payload.get("sender_persona") or payload.get("sender_name") or ""),
        )


@dataclass
class ProcessResult:
    status: Literal[
        "processed", "ignored_self", "duplicate", "in_flight_elsewhere",
        "config_error", "auth_error", "deferred_for_retry", "urgent_handled",
    ]
    http_status: int = 200
    classification: str = ""
    send_mode: str = ""
    notes: list[str] = field(default_factory=list)

    def to_response(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "classification": self.classification,
            "send_mode": self.send_mode,
            "notes": self.notes,
        }


def process_reply(
    client_config,
    payload: ReplyPayload,
    source: Literal["webhook", "reconciler"] = "webhook",
) -> ProcessResult:
    """Full §4.1 pipeline. Filled in across Tasks 4.1b–4.1g.

    For Task 4.1a the only path implemented is the loop check (delegated to Task 4.1b).
    """
    raise NotImplementedError("orchestrator.process_reply filled in across Tasks 4.1b–4.1g")
