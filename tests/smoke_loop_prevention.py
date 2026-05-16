"""Live loop-prevention smoke — covers §7.3 critical scenario #5.

Parameterized over `sending_inboxes × NORMALIZATION_VARIANTS`. As inboxes are added to
clients/clear_facility.json's sending_inboxes list, this test auto-extends.

NOT live in the API sense — it runs against the orchestrator directly (no Vercel),
but uses the production client config so it catches drift between code and config.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reply_router.config import load_client_config
from reply_router.orchestrator import _loop_check


NORMALIZATION_VARIANTS = [
    lambda e: e,                                            # bare
    lambda e: e.upper(),                                    # mixed case
    lambda e: e.replace("@", "+test@"),                     # plus-tag
    lambda e: f'"Test User" <{e}>',                         # RFC 5322 display name
    lambda e: f"Test User <{e.upper()}>",                   # display name + uppercase
    lambda e: f"  {e}  ",                                   # whitespace
]


def _load_cfs():
    """Load the production CFS config — this is the source of truth for sending_inboxes."""
    path = Path(__file__).parent.parent / "clients" / "clear_facility.json"
    return load_client_config(path)


@pytest.fixture(scope="module")
def cfs_config():
    return _load_cfs()


def _all_pairs(cfg):
    for inbox in cfg.sending_inboxes:
        for variant in NORMALIZATION_VARIANTS:
            yield inbox, variant(inbox)


def test_loop_check_auto_extends_to_full_config(cfs_config):
    """Sanity: the parameter set is 12+ inboxes × 6 variants (auto-extends with config)."""
    pairs = list(_all_pairs(cfs_config))
    assert len(pairs) == len(cfs_config.sending_inboxes) * len(NORMALIZATION_VARIANTS)
    # Catch the 5th-domain rollout: when sending_inboxes grows past 12, fail loudly so
    # the developer notices the test set is also growing.
    assert len(cfs_config.sending_inboxes) >= 12, (
        f"sending_inboxes shrunk to {len(cfs_config.sending_inboxes)} — "
        f"check that inboxes weren't accidentally removed"
    )


@pytest.mark.parametrize(
    "inbox,variant",
    list(_all_pairs(_load_cfs())),
    ids=lambda v: v if isinstance(v, str) else "—",
)
def test_loop_check_ignores_every_normalization_variant(inbox, variant, cfs_config):
    """For each sending inbox, every variant of that address must trigger loop ignore."""
    assert _loop_check(variant, cfs_config.sending_inboxes) is True, (
        f"\n  inbox: {inbox}"
        f"\n  variant: {variant!r}"
        f"\n  did NOT trigger loop ignore — normalization missed a case"
    )


def test_loop_check_does_not_match_actual_prospect(cfs_config):
    """Sanity-negative: a real prospect email is NOT in sending_inboxes."""
    assert _loop_check("pat@hennepinlogistics.com", cfs_config.sending_inboxes) is False


def test_loop_prevention_all_inboxes_normalized(cfs_config):
    """§7.3 #5 — sanity wrapper that asserts the full inbox × variant matrix passes.

    The actual matrix checks live in `test_loop_check_ignores_every_normalization_variant`
    (parameterized) — this function names the §7.3 scenario explicitly so the scenario
    grep finds it.
    """
    failed = []
    for inbox in cfs_config.sending_inboxes:
        for variant in NORMALIZATION_VARIANTS:
            v = variant(inbox)
            if not _loop_check(v, cfs_config.sending_inboxes):
                failed.append((inbox, v))
    assert not failed, f"loop_check missed normalization variants: {failed}"
