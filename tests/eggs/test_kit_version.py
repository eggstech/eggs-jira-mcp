from __future__ import annotations

import pytest

from mcp_atlassian.eggs import kit_version as kv


def test_version_in_skill_text():
    assert kv.version_in("# x\n\nKit version: `2026.11.12.1`\n") == "2026.11.12.1"
    assert kv.version_in("no version here") is None


@pytest.mark.parametrize(
    ("current", "latest", "outdated"),
    [
        ("2026.11.12.1", "2026.11.12.2", True),
        ("2026.11.12.2", "2026.11.12.2", False),
        ("2026.11.13.1", "2026.11.12.9", False),  # local newer (raw cache stale)
        ("2026.11.9.1", "2026.11.12.1", True),  # numeric, not text compare
        (None, "2026.11.12.1", True),  # old skill that sends no version
        ("2026.11.12.1", None, False),  # GitHub unreachable
        ("<Kit version above>", "2026.11.12.1", False),  # placeholder, not nag
    ],
)
def test_update_info(current: str | None, latest: str | None, outdated: bool):  # noqa: FBT001
    info = kv.update_info(current, latest)
    assert (info is not None) is outdated
    if info:
        assert info["latest"] == latest
        assert "get.sh" in info["commands"]["macos_linux"]
        assert "get.ps1" in info["commands"]["windows"]


def test_latest_is_cached_including_failures(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    def fetch() -> str | None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("offline")
        return "2026.11.12.3"

    monkeypatch.setattr(kv, "_fetch", fetch)
    assert kv.latest_version(now=1000.0) is None
    assert kv.latest_version(now=1100.0) is None  # failure cached
    assert kv.latest_version(now=1000.0 + kv.CACHE_SECONDS + 1) == "2026.11.12.3"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_eggs_next_reports_kit_update_only_when_older(
    monkeypatch: pytest.MonkeyPatch,
):
    import json
    from unittest.mock import AsyncMock, patch

    from fastmcp import FastMCP

    from mcp_atlassian.eggs.config import EggsConfig
    from mcp_atlassian.eggs.tools import (
        clear_identity_cache,
        register_eggs_tools,
        set_roles_reader,
    )
    from tests.eggs.fakes import FakeGateway

    clear_identity_cache()
    set_roles_reader(None)
    monkeypatch.setattr(kv, "_fetch", lambda: "2026.11.12.2")
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.example",
        roles={
            "lead_ba": ["Business Analysts"],
            "qc": ["Testers"],
            "dev": ["Developers"],
        },
    )
    server = FastMCP("t")
    register_eggs_tools(server, force=True)
    bot = FakeGateway(user="eggsbot", role_members={"RFID": {"Developers": ["dev1"]}})
    user = FakeGateway(user="dev1")
    with (
        patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
        patch(
            "mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user)
        ),
        patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot),
    ):
        old = await server.call_tool("eggs_next", {"kit": "2026.11.12.1"})
        new = await server.call_tool("eggs_next", {"kit": "2026.11.12.2"})
    old_data = json.loads(old.content[0].text)
    assert old_data["kit_update"]["latest"] == "2026.11.12.2"
    assert "kit_update" not in json.loads(new.content[0].text)
    set_roles_reader(None)
