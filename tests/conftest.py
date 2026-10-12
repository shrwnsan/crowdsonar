"""Shared test fixtures.

The storage module binds DATA_DIR (and its per-kind children) at import time,
so every test that touches the parquet store points those module globals at a
per-test tmp_path instead. No network access is ever required: Arctic Shift
responses come from JSON fixtures and the HTTP layer is faked.
"""

import json
from pathlib import Path

import pytest

import crowdsonar.storage as storage

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirect the parquet store into a per-test directory."""
    d = tmp_path / "data"
    monkeypatch.setattr(storage, "DATA_DIR", d)
    monkeypatch.setattr(storage, "SIGNALS_DIR", d / "signals")
    monkeypatch.setattr(storage, "RAW_DIR", d / "raw")
    monkeypatch.setattr(storage, "TOPICS_DIR", d / "topics")
    return d


@pytest.fixture
def arctic_posts_page():
    return json.loads((FIXTURES / "arctic_posts_page.json").read_text(encoding="utf-8"))


@pytest.fixture
def arctic_comments_page():
    return json.loads((FIXTURES / "arctic_comments_page.json").read_text(encoding="utf-8"))
