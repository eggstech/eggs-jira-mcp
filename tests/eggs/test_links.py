from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from mcp_atlassian.eggs import links
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.gateway import JiraError
from mcp_atlassian.eggs.tools import (
    clear_identity_cache,
    register_eggs_tools,
    set_roles_reader,
)
from tests.eggs.fakes import FakeGateway

URL = "https://jira.example"


def _issue(key: str, itype: str) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "issuetype": {"name": itype},
            "summary": f"S {key}",
            "status": {"name": "DEV To Do"},
        },
    }


@pytest.fixture(autouse=True)
def _reset():
    links.clear_type_cache()
    clear_identity_cache()
    set_roles_reader(None)
    yield
    links.clear_type_cache()
    set_roles_reader(None)


class TestFindKeys:
    def test_only_configured_projects_in_order(self):
        text = "EGG-2 then HH-10, UTF-8, ABC-1, EGG-2 again, x/EGG-9, EGG-3."
        assert links.find_issue_keys(text, ["EGG", "HH"]) == ["EGG-2", "HH-10", "EGG-3"]

    def test_key_inside_browse_url_is_skipped_but_plain_one_found(self):
        text = f"{URL}/browse/EGG-4 và EGG-5"
        assert links.find_issue_keys(text, ["EGG"]) == ["EGG-5"]


class TestEnrich:
    def test_adds_type_and_url_for_every_key(self):
        gw = FakeGateway(
            issues={"EGG-1": _issue("EGG-1", "Story"), "EGG-2": _issue("EGG-2", "Bug")}
        )
        src = json.dumps(
            {"ok": True, "message": "Đã chuyển EGG-1", "next": {"key": "EGG-2"}}
        )
        out = json.loads(
            links.enrich_result(
                src, jira_url=URL, project_keys=["EGG"], gateway_getter=lambda: gw
            )
        )
        assert out["message"] == "Đã chuyển EGG-1"
        assert out["issue_links"] == {
            "EGG-1": {"type": "Story", "url": f"{URL}/browse/EGG-1"},
            "EGG-2": {"type": "Bug", "url": f"{URL}/browse/EGG-2"},
        }

    def test_no_keys_leaves_result_untouched(self):
        src = '{"ok": true, "message": "xong"}'
        assert (
            links.enrich_result(
                src, jira_url=URL, project_keys=["EGG"], gateway_getter=None
            )
            == src
        )

    def test_non_json_untouched(self):
        assert (
            links.enrich_result(
                "EGG-1 text", jira_url=URL, project_keys=["EGG"], gateway_getter=None
            )
            == "EGG-1 text"
        )

    def test_lookup_failure_still_gives_links(self):
        def broken() -> FakeGateway:
            raise ValueError("no bot token")

        out = json.loads(
            links.enrich_result(
                '{"ok": true, "message": "EGG-7"}',
                jira_url=URL,
                project_keys=["EGG"],
                gateway_getter=broken,
            )
        )
        assert out["issue_links"]["EGG-7"] == {
            "type": None,
            "url": f"{URL}/browse/EGG-7",
        }

    def test_bad_jql_falls_back_to_single_reads(self):
        gw = FakeGateway(issues={"EGG-1": _issue("EGG-1", "Dev Task")})

        def bad_search(*_a: Any, **_k: Any) -> list[dict]:
            raise JiraError("An issue with key 'EGG-99' does not exist", 400)

        gw.search = bad_search  # type: ignore[method-assign]
        types = links.lookup_types(gw, ["EGG-1", "EGG-99"])
        assert types == {"EGG-1": "Dev Task"}

    def test_types_are_cached(self):
        gw = FakeGateway(issues={"EGG-1": _issue("EGG-1", "Bug")})
        links.lookup_types(gw, ["EGG-1"], now=0.0)
        gw.issues.clear()
        assert links.lookup_types(gw, ["EGG-1"], now=10.0) == {"EGG-1": "Bug"}


class TestToolsGetLinks:
    @pytest.mark.asyncio
    async def test_eggs_comment_result_has_issue_links(self):
        cfg = EggsConfig(
            lead_user="cuong.huynh",
            jira_url=URL,
            projects={"EGG": ProjectConfig(key="EGG", default_lead_ba="cuong.huynh")},
        )
        server = FastMCP("t")
        register_eggs_tools(server, force=True)
        gw = FakeGateway(issues={"EGG-1": _issue("EGG-1", "Bug")})
        set_roles_reader(gw)
        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=gw),
            ),
        ):
            res = await server.call_tool(
                "eggs_comment", {"key": "EGG-1", "text": "ok", "dry_run": True}
            )
        data = json.loads(res.content[0].text)
        assert data["issue_links"]["EGG-1"] == {
            "type": "Bug",
            "url": f"{URL}/browse/EGG-1",
        }
