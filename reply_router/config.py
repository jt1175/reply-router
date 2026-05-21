"""Client configuration schema, loader, and validation.

Per spec §3.2: invoked at deploy time via `make verify-configs`. Schema
errors here MUST raise with actionable messages — the spec's principle
is that malformed config is caught before the first webhook arrives, not
discovered when a prospect's reply lands in the void.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


class ConfigError(Exception):
    """Raised when a client config file fails schema validation."""


CONFIDENCE_LEVELS = ("low", "medium", "high")
ALLOWED_CLASSIFICATIONS = frozenset({
    "unsubscribe", "wrong_person", "interested",
    "not_now", "info_request", "objection",
})


class GHLConfig(BaseModel):
    sub_account_id: str
    api_key_env: str
    pipeline_id: str
    calendar_id: str | None = None   # Required for qualification booking flow; endpoint checks at runtime
    custom_field_ids: dict[str, str]

    @field_validator("custom_field_ids")
    @classmethod
    def _required_fields(cls, v: dict[str, str]) -> dict[str, str]:
        required = {
            "reply_classification", "reply_received_at", "contract_end_date",
            "nurture_bucket", "last_processed_smartlead_message_ids",
            "currently_processing_smartlead_message_id",
            "pending_draft_token", "pending_draft_text", "pending_draft_created_at",
            # Shadow-mode threading: the orchestrator stores these at draft time so the
            # approval handler (api/approvals.py) can pass them to Smartlead's
            # reply-email-thread endpoint at approve time. Without them, every approved
            # shadow reply would 4xx or send non-threaded. See spec §4.3 + Task 4.1e.
            "pending_reply_message_id", "pending_reply_email_stats_id",
        }
        # pending_reply_campaign_id is OPTIONAL (added 2026-05-21): tracks
        # which campaign the original outbound came from so the approval
        # endpoint sends the threaded reply via the right Smartlead campaign.
        # Configs without it fall back to campaign_ids[0], matching the
        # pre-2026-05-21 single-campaign-per-client behavior.
        # Note: qualification_form_answers/result/submitted_at are required only when the
        # qualification booking flow is wired up. The endpoint checks for their presence
        # at request time; schema-level absence is permitted so existing test fixtures
        # without the booking flow continue to pass.
        missing = required - v.keys()
        if missing:
            raise ValueError(f"custom_field_ids missing required keys: {sorted(missing)}")
        return v


class SmartleadConfig(BaseModel):
    api_key_env: str
    campaign_ids: list[str] = Field(min_length=1)


class SlackConfig(BaseModel):
    incoming_webhook_url_env: str


class AuthConfig(BaseModel):
    router_secret_env: str


class ClassificationAction(BaseModel):
    auto_send: bool
    min_confidence: Literal["low", "medium", "high"]
    slack_notify: bool
    pipeline_stage_id: str
    nurture_bucket: str | None = None


class BusinessContext(BaseModel):
    company_name: str
    service_area: str
    services_offered: list[str] = []
    services_not_offered: list[str] = []
    pricing_response: str
    booking_link: str
    # Other fields per spec §8.1 are optional and passed through.
    model_config = {"extra": "allow"}


class ClientConfig(BaseModel):
    client_id: str
    client_display_name: str
    ghl: GHLConfig
    smartlead: SmartleadConfig
    slack: SlackConfig
    auth: AuthConfig
    sending_inboxes: list[str] = Field(min_length=1)
    monitoring_until: str   # ISO date string
    classification_actions: dict[str, ClassificationAction]
    business_context: BusinessContext
    # Qualification booking flow — optional at schema level (endpoint checks at runtime).
    # All four must be populated for the /qualify/* endpoints to function; existing test
    # fixtures + clients without the booking flow continue to validate without these.
    qualification_rubric: str | None = None
    qualify_pipeline_stage_id: str | None = None
    gray_zone_pipeline_stage_id: str | None = None
    reject_pipeline_stage_id: str | None = None
    # Bidirectional sync — GHL stage IDs that, when an opportunity moves into them,
    # should pause the contact's Smartlead lead (no more follow-ups to closed deals).
    # Default: Closed Won + Closed Lost stages (when configured).
    pause_on_stage_ids: list[str] = []

    # Allow underscore-prefixed _doc_* and _pending_domains keys
    model_config = {"extra": "allow"}

    @field_validator("classification_actions", mode="before")
    @classmethod
    def _strip_doc_keys(cls, v):
        # Spec §8.1 places `_doc_schema` and `_doc` keys inside classification_actions
        # as inline documentation. Strip them here, BEFORE pydantic coerces values
        # to ClassificationAction — a string docstring can't coerce to the model.
        if isinstance(v, dict):
            return {k: val for k, val in v.items() if not k.startswith("_")}
        return v

    @field_validator("classification_actions")
    @classmethod
    def _all_classifications_present(cls, v: dict) -> dict:
        unknown = set(v.keys()) - ALLOWED_CLASSIFICATIONS
        if unknown:
            raise ValueError(
                f"classification_actions has unknown classification keys: {sorted(unknown)}. "
                f"Allowed: {sorted(ALLOWED_CLASSIFICATIONS)}"
            )
        missing = ALLOWED_CLASSIFICATIONS - v.keys()
        if missing:
            raise ValueError(
                f"classification_actions missing required keys: {sorted(missing)}"
            )
        return v


def load_client_config(path: str | Path) -> ClientConfig:
    """Load and validate a single client config file.

    Raises ConfigError with a wrapped pydantic message if validation fails.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{p}: invalid JSON — {exc}") from exc
    try:
        return ClientConfig(**raw)
    except ValidationError as exc:
        raise ConfigError(f"{p}: schema validation failed:\n{exc}") from exc


def load_and_validate_all(clients_dir: str | Path) -> dict[str, ClientConfig]:
    """Load every clients/*.json file and return {client_id: ClientConfig}.

    Used by `make verify-configs` (and CI) to catch malformed configs before
    deploy. Skips files starting with `_` (treated as test/sandbox configs
    that are loaded explicitly by tests).
    """
    d = Path(clients_dir)
    if not d.exists():
        raise ConfigError(f"clients directory not found: {d}")
    out: dict[str, ClientConfig] = {}
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        cfg = load_client_config(f)
        if cfg.client_id in out:
            raise ConfigError(
                f"duplicate client_id {cfg.client_id!r} in {f} and earlier file"
            )
        out[cfg.client_id] = cfg
    return out
