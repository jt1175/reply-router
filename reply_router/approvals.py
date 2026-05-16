"""Approval tokens for shadow-mode drafts.

Per spec §3.4 / §4.3: 32-byte token (urlsafe), stored on GHL contact custom fields
with 7-day TTL. CSRF HMAC on the approval form prevents URL leakage from being
exploitable via referrer logs.
"""
from __future__ import annotations

import hmac
import secrets
import time
from datetime import datetime, timezone

from reply_router.ghl_client import GHLClient

TOKEN_TTL_SEC = 7 * 24 * 3600
CSRF_TTL_SEC = 3600


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def store_draft(
    client: GHLClient,
    contact_id: str,
    token_field_id: str,
    text_field_id: str,
    created_at_field_id: str,
    token: str,
    draft_text: str,
) -> None:
    client.update_contact(
        contact_id,
        custom_fields={
            token_field_id: token,
            text_field_id: draft_text,
            created_at_field_id: datetime.now(timezone.utc).isoformat(),
        },
    )


def find_draft_by_token(
    client: GHLClient,
    token: str,
    token_field_id: str,
) -> dict | None:
    """Search GHL contacts for one whose token_field == token. Returns contact dict or None.

    Delegates to GHLClient.search_contacts_by_custom_field (Task 2.3 step 7) which uses
    GHL's `/contacts/search` endpoint with `customField.{id}={value}` filter, requesting
    a unique match. If >1 contact has this token, raises (likely indicates a duplicate
    token generation bug — tokens are 32-byte urlsafe, collision probability is
    negligible, so multi-match means something else is wrong and a silent pick is unsafe).
    """
    matches = client.search_contacts_by_custom_field(token_field_id, token, unique=True)
    if not matches:
        return None
    return matches[0]


def is_expired(created_at_iso: str) -> bool:
    created = datetime.fromisoformat(created_at_iso)
    age = (datetime.now(timezone.utc) - created).total_seconds()
    return age > TOKEN_TTL_SEC


def csrf_token(router_secret: str, token: str, form_issued_at_unix: int) -> str:
    """Compute the CSRF HMAC for the approval form."""
    msg = f"{token}:{form_issued_at_unix}".encode("utf-8")
    key = router_secret.encode("utf-8")
    return hmac.new(key, msg, "sha256").hexdigest()


def verify_csrf(
    router_secret: str, token: str, form_issued_at_unix: int, submitted_csrf: str
) -> bool:
    if time.time() - form_issued_at_unix > CSRF_TTL_SEC:
        return False
    expected = csrf_token(router_secret, token, form_issued_at_unix)
    return hmac.compare_digest(expected, submitted_csrf)


def clear_draft(
    client: GHLClient,
    contact_id: str,
    token_field_id: str,
    text_field_id: str,
    created_at_field_id: str,
    reply_message_id_field_id: str | None = None,
    reply_email_stats_id_field_id: str | None = None,
) -> None:
    """Consume the token: clear all draft-related fields in one PATCH.

    The two `reply_*_field_id` params are optional for backwards compat with the test
    fixtures, but production callers (api/approvals.py + reconciler phase 3) MUST pass
    them so the threading-param fields are also cleared. See Task 4.1e — these fields
    were added to the schema to fix iteration 2 blocker #1.
    """
    fields: dict[str, str] = {
        token_field_id: "", text_field_id: "", created_at_field_id: "",
    }
    if reply_message_id_field_id:
        fields[reply_message_id_field_id] = ""
    if reply_email_stats_id_field_id:
        fields[reply_email_stats_id_field_id] = ""
    client.update_contact(contact_id, custom_fields=fields)
