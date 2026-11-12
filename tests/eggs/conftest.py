from __future__ import annotations

import pytest

from mcp_atlassian.eggs import kit_version


@pytest.fixture(autouse=True)
def _no_kit_version_fetch(monkeypatch: pytest.MonkeyPatch):
    """Tests never call GitHub for the latest kit version."""
    kit_version.clear_cache()
    monkeypatch.setattr(kit_version, "_fetch", lambda: None)
    yield
    kit_version.clear_cache()
