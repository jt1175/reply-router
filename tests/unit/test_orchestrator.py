"""Unit tests for reply_router.orchestrator."""
from __future__ import annotations

import pytest

from reply_router.orchestrator import _normalize_email


@pytest.mark.parametrize("raw,expected", [
    ("sarah.jones@clearfacilitymn.com", "sarah.jones@clearfacilitymn.com"),
    ("Sarah.Jones@ClearFacilityMN.com", "sarah.jones@clearfacilitymn.com"),
    ("sarah.jones+test@clearfacilitymn.com", "sarah.jones@clearfacilitymn.com"),
    ('"Sarah Jones" <sarah.jones@clearfacilitymn.com>', "sarah.jones@clearfacilitymn.com"),
    ("Sarah Jones <SARAH.JONES@CLEARFACILITYMN.COM>", "sarah.jones@clearfacilitymn.com"),
    ("  sarah.jones@clearfacilitymn.com  ", "sarah.jones@clearfacilitymn.com"),
    ("", ""),
    ("not an email", ""),
])
def test_normalize_email_handles_all_documented_variants(raw, expected):
    assert _normalize_email(raw) == expected
