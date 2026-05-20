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
        responses.POST,
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
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": []},
        status=200,
    )
    result = client.get_contacts_by_email("nobody@example.com")
    assert result == []


@responses.activate
def test_get_contact_by_email_network_error_raises(client):
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        status=500,
    )
    with pytest.raises(RuntimeError, match="GHL contact lookup failed"):
        client.get_contacts_by_email("prospect@example.com")


@responses.activate
def test_update_custom_fields_patches_contact(client):
    responses.add(
        responses.PUT,
        f"{GHL_BASE}/contacts/ct_1",
        json={"contact": {"id": "ct_1"}},
        status=200,
    )
    client.update_contact(
        contact_id="ct_1",
        custom_fields={"cf_1": "interested", "cf_2": "2026-05-15"},
    )
    call = responses.calls[0]
    body = call.request.body
    # Just verify the call was made; payload shape gets tested via integration
    assert b"ct_1" not in body  # path-based, not body
    # GHL expects customFields as an array of {id, value} pairs
    import json
    parsed = json.loads(body)
    assert "customFields" in parsed
    field_map = {cf["id"]: cf["value"] for cf in parsed["customFields"]}
    assert field_map == {"cf_1": "interested", "cf_2": "2026-05-15"}


@responses.activate
def test_add_tags_calls_tags_endpoint(client):
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/ct_1/tags",
        json={"tags": ["replied", "interested"]},
        status=200,
    )
    client.add_tags(contact_id="ct_1", tags=["replied", "interested"])
    assert responses.calls[0].request.body is not None


@responses.activate
def test_add_note(client):
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/ct_1/notes",
        json={"note": {"id": "n_1"}},
        status=201,
    )
    client.add_note(contact_id="ct_1", body="Auto-response sent")


@responses.activate
def test_move_to_pipeline_stage(client):
    responses.add(
        responses.PUT,
        f"{GHL_BASE}/opportunities/op_1",
        json={"opportunity": {"id": "op_1"}},
        status=200,
    )
    # Pipeline-stage moves go via opportunities API; this assumes the contact
    # already has an opportunity. Implementation finds/creates it.
    responses.add(
        responses.GET,
        f"{GHL_BASE}/opportunities/search",
        json={"opportunities": [{"id": "op_1"}]},
        status=200,
    )
    client.move_to_pipeline_stage(
        contact_id="ct_1", pipeline_id="pipe_abc", stage_id="s2"
    )


@responses.activate
def test_move_to_pipeline_stage_creates_with_name_and_trailing_slash(client):
    """When no opportunity exists, POST to /opportunities/ (trailing slash required by GHL v2)
    with a non-empty name field — both missing causes a 404 in production."""
    responses.add(
        responses.GET,
        f"{GHL_BASE}/opportunities/search",
        json={"opportunities": []},
        status=200,
    )
    create_call = responses.add(
        responses.POST,
        f"{GHL_BASE}/opportunities/",
        json={"opportunity": {"id": "op_new"}},
        status=201,
    )
    client.move_to_pipeline_stage(
        contact_id="ct_1", pipeline_id="pipe_abc", stage_id="s2", name="Acme Inc"
    )
    import json as _json
    posted = _json.loads(create_call.calls[0].request.body)
    assert posted["name"] == "Acme Inc"
    assert posted["pipelineStageId"] == "s2"


@responses.activate
def test_add_to_dnc(client):
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/ct_1/dnd",
        status=200,
    )
    client.add_to_dnc(contact_id="ct_1")


@responses.activate
def test_resolve_contact_zero_matches_creates_skeleton(client):
    # First search returns nothing
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": []},
        status=200,
    )
    # Create skeleton
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts",
        json={"contact": {"id": "ct_new"}},
        status=201,
    )
    # Re-fetch to detect concurrent-creation race
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": [{"id": "ct_new", "email": "new@example.com", "dateAdded": "2026-05-15T00:00:00Z"}]},
        status=200,
    )
    contact, resolution = client.resolve_contact_by_email("new@example.com")
    assert contact["id"] == "ct_new"
    assert resolution == MultiContactResolution.CREATED_SKELETON


@responses.activate
def test_resolve_contact_handles_duplicate_email_via_search_index_lag(client):
    """Regression: GHL's contact-search index is eventually consistent. If a
    contact was created within seconds of the search, search returns 0 but
    POST /contacts then rejects with 400 + `meta.contactId` pointing at the
    existing record. We must recover by fetching that contact by ID rather
    than raising — otherwise every webhook fired in this race window 500s."""
    # Search returns 0 (stale index)
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": []},
        status=200,
    )
    # Create rejected as duplicate, but GHL hands us the existing id in meta
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts",
        json={
            "statusCode": 400,
            "message": "This location does not allow duplicated contacts.",
            "meta": {"contactName": "", "contactId": "ct_already_existed", "matchingField": "email"},
        },
        status=400,
    )
    # Direct fetch by id finds the contact (bypasses search index)
    responses.add(
        responses.GET,
        f"{GHL_BASE}/contacts/ct_already_existed",
        json={"contact": {"id": "ct_already_existed", "email": "race@example.com", "firstName": "Race"}},
        status=200,
    )
    contact, resolution = client.resolve_contact_by_email("race@example.com")
    assert contact["id"] == "ct_already_existed"
    assert contact["firstName"] == "Race"
    # Resolution is SINGLE (not CREATED_SKELETON) — the contact pre-existed
    assert resolution == MultiContactResolution.SINGLE


@responses.activate
def test_resolve_contact_non_duplicate_400_still_raises(client):
    """A 400 from POST /contacts that ISN'T a duplicate-email rejection should
    still raise — don't swallow generic validation errors."""
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": []},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts",
        json={"statusCode": 400, "message": "Invalid email format", "meta": {}},
        status=400,
    )
    with pytest.raises(RuntimeError, match="skeleton contact create failed"):
        client.resolve_contact_by_email("bad@example.com")


@responses.activate
def test_get_contact_by_id_returns_contact(client):
    responses.add(
        responses.GET,
        f"{GHL_BASE}/contacts/ct_abc",
        json={"contact": {"id": "ct_abc", "email": "x@y.com"}},
        status=200,
    )
    c = client.get_contact_by_id("ct_abc")
    assert c["id"] == "ct_abc"


@responses.activate
def test_get_contact_by_id_returns_none_on_404(client):
    responses.add(
        responses.GET,
        f"{GHL_BASE}/contacts/missing",
        json={"message": "not found"},
        status=404,
    )
    assert client.get_contact_by_id("missing") is None


@responses.activate
def test_resolve_contact_single_match(client):
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": [{"id": "ct_1", "email": "p@x.com"}]},
        status=200,
    )
    contact, resolution = client.resolve_contact_by_email("p@x.com")
    assert contact["id"] == "ct_1"
    assert resolution == MultiContactResolution.SINGLE


@responses.activate
def test_resolve_contact_multi_match_prefers_in_campaign(client):
    """When multiple contacts match, prefer one that's in the configured campaign_ids."""
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": [
            {"id": "ct_old", "email": "p@x.com", "dateAdded": "2026-01-01T00:00:00Z", "campaigns": []},
            {"id": "ct_active", "email": "p@x.com", "dateAdded": "2026-04-01T00:00:00Z", "campaigns": ["c1"]},
        ]},
        status=200,
    )
    contact, resolution = client.resolve_contact_by_email("p@x.com")
    assert contact["id"] == "ct_active"
    assert resolution == MultiContactResolution.RESOLVED_BY_CAMPAIGN


@responses.activate
def test_resolve_contact_multi_match_ambiguous_picks_most_recent(client):
    """When multi-match and none in campaign, fall back to most recently modified + flag ambiguous."""
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": [
            {"id": "ct_a", "email": "p@x.com", "dateAdded": "2026-01-01T00:00:00Z", "campaigns": []},
            {"id": "ct_b", "email": "p@x.com", "dateAdded": "2026-04-01T00:00:00Z", "campaigns": []},
        ]},
        status=200,
    )
    contact, resolution = client.resolve_contact_by_email("p@x.com")
    assert contact["id"] == "ct_b"
    assert resolution == MultiContactResolution.AMBIGUOUS


@responses.activate
def test_search_contacts_by_custom_field_returns_match(client):
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": [{"id": "ct_token", "customFields": [{"id": "cf_token", "value": "abc123"}]}]},
        status=200,
    )
    result = client.search_contacts_by_custom_field("cf_token", "abc123")
    assert len(result) == 1
    assert result[0]["id"] == "ct_token"


@responses.activate
def test_search_contacts_by_custom_field_no_match_returns_empty(client):
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": []},
        status=200,
    )
    assert client.search_contacts_by_custom_field("cf_token", "nonesuch") == []


@responses.activate
def test_search_contacts_by_custom_field_multi_match_raises(client):
    """Tokens are meant to be unique. If GHL returns >1, something's wrong with our token-gen
    or there's a stale duplicate — caller should investigate, not silently pick one."""
    responses.add(
        responses.POST,
        f"{GHL_BASE}/contacts/search",
        json={"contacts": [
            {"id": "ct_a", "customFields": [{"id": "cf_token", "value": "abc123"}]},
            {"id": "ct_b", "customFields": [{"id": "cf_token", "value": "abc123"}]},
        ]},
        status=200,
    )
    with pytest.raises(RuntimeError, match="multiple contacts.*same token"):
        client.search_contacts_by_custom_field("cf_token", "abc123", unique=True)


def test_list_contacts_with_field_is_stub_returns_empty(client):
    """v1 stub returns empty list with warning. Real implementation lands before launch."""
    assert client.list_contacts_with_field("cf_some_field") == []
