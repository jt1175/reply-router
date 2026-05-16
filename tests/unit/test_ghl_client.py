"""Tests for reply_router.ghl_client — GHL CRM API wrapper."""
from __future__ import annotations

import pytest
import responses

from reply_router.ghl_client import GHLClient, MultiContactResolution


GHL_BASE = "https://services.leadconnectorhq.com"


@pytest.fixture
def client():
    return GHLClient(api_key="test-key", sub_account_id="loc_abc", campaign_ids=["c1"])


@responses.activate
def test_get_contact_by_email_single_match(client):
    responses.add(
        responses.GET,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": [{"id": "ct_1", "email": "prospect@example.com"}]},
        status=200,
    )
    result = client.get_contacts_by_email("prospect@example.com")
    assert len(result) == 1
    assert result[0]["id"] == "ct_1"


@responses.activate
def test_get_contact_by_email_no_match_returns_empty(client):
    responses.add(
        responses.GET,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": []},
        status=200,
    )
    result = client.get_contacts_by_email("nobody@example.com")
    assert result == []


@responses.activate
def test_get_contact_by_email_network_error_raises(client):
    responses.add(
        responses.GET,
        f"{GHL_BASE}/contacts/search",
        status=500,
    )
    with pytest.raises(RuntimeError, match="GHL contact lookup failed"):
        client.get_contacts_by_email("prospect@example.com")
