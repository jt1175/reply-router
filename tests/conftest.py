"""Shared pytest fixtures for reply-router tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture(fixtures_dir):
    """Return a callable: load_fixture('webhooks/interested_warm.json') -> dict."""
    def _load(relpath: str):
        path = fixtures_dir / relpath
        if path.suffix == ".json":
            return json.loads(path.read_text())
        return path.read_text()
    return _load
