from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastmcp import FastMCP

from mcp_atlassian.eggs.actions import ActionResult
from mcp_atlassian.eggs.agenda import AgendaItem, SuggestedAction
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.tools import (
    ALL_EGGS_TOOL_NAMES,
    READ_EGGS_TOOL_NAMES,
    clear_identity_cache,
    register_eggs_tools,
    set_roles_reader,
)
from mcp_atlassian.utils.toolsets import (
    get_enabled_toolsets,
    should_include_tool_by_toolset,
)
from tests.eggs.fakes import FakeGateway

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TODAY = date(2026, 11, 18)


@pytest.fixture(autouse=True)
def _reset_tools_state():
    clear_identity_cache()
    set_roles_reader(None)
    yield
    clear_identity_cache()
    set_roles_reader(None)


@pytest.fixture
def cfg() -> EggsConfig:
    return EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        timezone="Asia/Ho_Chi_Minh",
        ops_epic_prefix="[OPS]",
        ops_epic_summary="[OPS] Vận hành & Hotfix {month:02d}/{year}",
        hotfix_prefix="[HOTFIX]",
        reschedule_interrupt_value="Chen ngang",
        link_types={
            "interrupts": "is interrupted by",
            "causes": "causes",
            "relates": "relates to",
            "duplicates": "duplicates",
        },
        fields={
            "planned_start": "customfield_10108",
            "planned_end": "customfield_10109",
            "baseline_end": "customfield_baseline_end",
            "proposed_end": "customfield_proposed_end",
            "reschedule_reason": "customfield_reschedule_reason",
            "reschedule_status": "customfield_reschedule_status",
            "reschedule_requester": "customfield_reschedule_requester",
            "blocked_by": "customfield_blocked_by",
            "block_reason": "customfield_block_reason",
            "blocked_since": "customfield_blocked_since",
            "flagged": "customfield_flagged",
            "epic_link": "customfield_epic_link",
            "report_source": "customfield_report_source",
            "reported_by": "customfield_reported_by",
            "reported_at": "customfield_reported_at",
            "actor": "customfield_actor",
        },
        statuses={
            "open": "Open",
            "analyzing": "Analyzing",
            "ready_for_dev": "Ready For Dev",
            "todo": "DEV To Do",
            "in_progress": "Dev In Progress",
            "ready_for_qc": "QC TO DO",
            "testing": "Test In Progress",
            "ready_for_uat": "Ready For UAT",
            "client_uat": "Wait For Review",
            "done": "Done",
            "cancelled": "Cancelled",
        },
        issue_types={
            "dev_task": "Dev Task",
            "bug": "Bug",
            "story": "Story",
            "epic": "Epic",
            "adhoc": "Ad-hoc Task",
            "report": "User Report",
        },
        projects={
            "RFID": ProjectConfig(
                key="RFID",
                default_lead_ba="ba.rfid",
                triage_owner="qc.rfid",
            ),
            "CP": ProjectConfig(
                key="CP",
                default_lead_ba="ba.cp",
                triage_owner="qc.cp",
            ),
        },
        roles={
            "lead_ba": ["Business Analysts", "Administrators"],
            "qc": ["Testers"],
            "dev": ["Developers"],
        },
    )


def make_dummy_context(token: str = "tok-1", user: str = "dev.user") -> Any:
    ctx = MagicMock()
    ctx.request_context.request.state.user_token = token
    return ctx


class TestEggsToolsRegistration:
    @pytest.mark.asyncio
    async def test_toolsets_eggs_lists_exactly_25_names(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TOOLSETS", "eggs")
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        enabled = get_enabled_toolsets()
        assert "eggs" in enabled

        all_tools = await server.list_tools()
        tools = [
            t
            for t in all_tools
            if should_include_tool_by_toolset(t.tags, enabled)
        ]
        tool_names = sorted(t.name for t in tools)
        assert len(tool_names) == 25
        assert tool_names == sorted(ALL_EGGS_TOOL_NAMES)

    @pytest.mark.asyncio
    async def test_read_only_mode_hides_write_tools(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TOOLSETS", "eggs")
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        enabled = get_enabled_toolsets()
        read_only = True

        all_tools = await server.list_tools()
        visible_tools = [
            t
            for t in all_tools
            if should_include_tool_by_toolset(t.tags, enabled)
            and not (read_only and "write" in t.tags)
        ]
        tool_names = sorted(t.name for t in visible_tools)
        assert len(tool_names) == 7
        assert tool_names == sorted(READ_EGGS_TOOL_NAMES)


class TestEggsToolBehavior:
    @pytest.mark.asyncio
    async def test_eggs_uat_reaches_actions_uat(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={"RFID": {"Business Analysts": ["ba.rfid"]}},
        )
        user_gateway = FakeGateway(
            user="ba.rfid",
            issues={
                "RFID-E1": {"key": "RFID-E1", "fields": {"issuetype": {"name": "Epic"}, "summary": "Epic 1"}},
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user_gateway)),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
            patch("mcp_atlassian.eggs.tools.Actions.uat") as mock_uat,
        ):
            mock_uat.return_value = ActionResult(
                ok=True, message="UAT start ok", changes=["RFID-S1 -> Wait For Review"], next_actions=[]
            )

            res_str = await server.call_tool(
                "eggs_uat",
                {"epic": "RFID-E1", "actor": "KH", "action": "start", "note": "All good", "dry_run": False},
            )
            # FastMCP call_tool returns CallToolResult with content
            res_json = json.loads(res_str.content[0].text)
            assert res_json["ok"] is True
            assert mock_uat.called
            mock_uat.assert_called_once_with(
                epic="RFID-E1",
                actor="KH",
                action="start",
                note="All good",
                bugs=None,
                stories=None,
                dry_run=False,
            )

    @pytest.mark.asyncio
    async def test_next_returns_agenda_and_role_filtered_intents(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )
        user_gateway = FakeGateway(
            user="dev1",
            issues={
                "RFID-10": {
                    "key": "RFID-10",
                    "fields": {
                        "issuetype": {"name": "Dev Task"},
                        "summary": "Task 1",
                        "status": {"name": "Dev In Progress"},
                        "assignee": {"name": "dev1", "displayName": "Dev One"},
                    },
                }
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user_gateway)),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
        ):
            res_str = await server.call_tool("eggs_next", {})
            data = json.loads(res_str.content[0].text)
            assert data["me"]["username"] == "dev1"
            assert "RFID" in data["me"]["roles"]
            assert data["me"]["roles"]["RFID"] == ["dev"]

            intent_names = [i["name"] for i in data["intents"]]
            assert "schedule" in intent_names
            assert "start" in intent_names
            assert "decide_reschedule" not in intent_names
            assert "uat" not in intent_names

    @pytest.mark.asyncio
    async def test_next_with_text_puts_mentioned_keys_first(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )
        user_gateway = FakeGateway(
            user="dev1",
            issues={},
        )

        mock_agenda = [
            AgendaItem(priority=7, key="RFID-20", project="RFID", title="T20", say="Doing 20", actions=[]),
            AgendaItem(priority=7, key="RFID-10", project="RFID", title="T10", say="Doing 10", actions=[]),
        ]

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user_gateway)),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
            patch("mcp_atlassian.eggs.tools.build_agenda", return_value=mock_agenda),
        ):
            res_str = await server.call_tool("eggs_next", {"text": "xong RFID-10 rồi"})
            data = json.loads(res_str.content[0].text)
            assert data["mentioned"] == ["RFID-10"]
            agenda_keys = [it["key"] for it in data["agenda"]]
            assert agenda_keys == ["RFID-10", "RFID-20"]

    @pytest.mark.asyncio
    async def test_write_tool_refuses_without_role(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        # dev1 has only dev role in RFID, not lead_ba
        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )
        user_gateway = FakeGateway(
            user="dev1",
            issues={
                "RFID-E1": {"key": "RFID-E1", "fields": {"issuetype": {"name": "Epic"}}},
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user_gateway)),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
            patch("mcp_atlassian.eggs.tools.Actions.uat") as mock_uat,
        ):
            res_str = await server.call_tool(
                "eggs_uat",
                {"epic": "RFID-E1", "actor": "KH", "action": "start", "note": ""},
            )
            data = json.loads(res_str.content[0].text)
            assert data["ok"] is False
            assert "Lead-BA" in data["message"]
            assert not mock_uat.called

    @pytest.mark.asyncio
    async def test_successful_write_returns_next_agenda_item(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )
        user_gateway = FakeGateway(
            user="dev1",
            issues={
                "RFID-10": {
                    "key": "RFID-10",
                    "fields": {
                        "issuetype": {"name": "Dev Task"},
                        "summary": "Task 1",
                        "status": {"name": "DEV To Do"},
                        "assignee": {"name": "dev1", "displayName": "Dev One"},
                    },
                }
            },
        )

        next_item = AgendaItem(
            priority=7,
            key="RFID-11",
            project="RFID",
            title="Next task",
            say="Tiếp theo",
            actions=[SuggestedAction(tool="eggs_start", args={"key": "RFID-11"}, missing=[])],
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user_gateway)),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
            patch(
                "mcp_atlassian.eggs.tools.Actions.start",
                return_value=ActionResult(ok=True, message="Start ok", changes=["DEV To Do -> Dev In Progress"], next_actions=[]),
            ),
            patch("mcp_atlassian.eggs.tools.build_agenda", return_value=[next_item]),
        ):
            res_str = await server.call_tool("eggs_start", {"key": "RFID-10", "dry_run": False})
            data = json.loads(res_str.content[0].text)
            assert data["ok"] is True
            assert data["next"] is not None
            assert data["next"]["key"] == "RFID-11"

    @pytest.mark.asyncio
    async def test_write_tools_use_per_request_user_gateway(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={
                "RFID": {"Developers": ["dev1", "dev2"]},
            },
        )
        user_gateway_1 = FakeGateway(
            user="dev1",
            issues={
                "RFID-10": {
                    "key": "RFID-10",
                    "fields": {
                        "issuetype": {"name": "Dev Task"},
                        "summary": "Task 1",
                        "status": {"name": "DEV To Do"},
                        "assignee": {"name": "dev1", "displayName": "Dev 1"},
                    },
                }
            },
        )
        user_gateway_2 = FakeGateway(
            user="dev2",
            issues={
                "RFID-20": {
                    "key": "RFID-20",
                    "fields": {
                        "issuetype": {"name": "Dev Task"},
                        "summary": "Task 2",
                        "status": {"name": "DEV To Do"},
                        "assignee": {"name": "dev2", "displayName": "Dev 2"},
                    },
                }
            },
        )

        user_gw_calls: list[str] = []

        async def dynamic_user_gateway(ctx: Any) -> FakeGateway:
            if not user_gw_calls:
                user_gw_calls.append("dev1")
                return user_gateway_1
            user_gw_calls.append("dev2")
            return user_gateway_2

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", side_effect=dynamic_user_gateway),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
            patch("mcp_atlassian.eggs.tools.Actions.start") as mock_start,
        ):
            mock_start.return_value = ActionResult(ok=True, message="start ok", changes=[], next_actions=[])

            # Call 1 for dev1
            await server.call_tool("eggs_start", {"key": "RFID-10", "dry_run": True})

            # Call 2 for dev2
            await server.call_tool("eggs_start", {"key": "RFID-20", "dry_run": True})

            # Verify two different user gateways were fetched across the two requests
            assert user_gw_calls == ["dev1", "dev2"]

            # Bot gateway was only called for roles, no transitions or worklogs
            assert not any(c[0] in {"transition", "add_worklog", "create_issue"} for c in bot_gateway.calls)

    @pytest.mark.asyncio
    async def test_version_and_whoami(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )
        user_gateway = FakeGateway(user="dev1")

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user_gateway)),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
        ):
            v_str = await server.call_tool("eggs_version", {})
            v_data = json.loads(v_str.content[0].text)
            assert "server" in v_data
            assert v_data["kit_min_version"] == cfg.kit_min_version

            w_str = await server.call_tool("eggs_whoami", {})
            w_data = json.loads(w_str.content[0].text)
            assert w_data["username"] == "dev1"
            assert w_data["roles"] == {"RFID": ["dev"]}
            assert w_data["is_lead"] is False

    @pytest.mark.asyncio
    async def test_rules_returns_vietnamese_text(self):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        res_str = await server.call_tool("eggs_rules", {})
        text = res_str.content[0].text
        assert "Quy tắc làm việc EGGS Jira Workflow" in text
        assert "eggs_*" in text
        assert "dry_run=true" in text

    @pytest.mark.asyncio
    async def test_reschedule_no_impact_assignee_only(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        # dev1 is NOT assignee of RFID-10 (dev2 is)
        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={"RFID": {"Developers": ["dev1", "dev2"], "Business Analysts": ["ba.rfid"]}},
        )
        user_gateway = FakeGateway(
            user="dev1",
            issues={
                "RFID-10": {
                    "key": "RFID-10",
                    "fields": {
                        "issuetype": {"name": "Dev Task"},
                        "summary": "Task 1",
                        "status": {"name": "Dev In Progress"},
                        "assignee": {"name": "dev2", "displayName": "Dev 2"},
                    },
                }
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user_gateway)),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
            patch("mcp_atlassian.eggs.tools.Actions.confirm_no_impact") as mock_confirm,
        ):
            # dev1 tries no_impact on dev2's task -> refused
            res_str = await server.call_tool(
                "eggs_request_reschedule",
                {"key": "RFID-10", "no_impact": True, "reason": "No delay"},
            )
            data = json.loads(res_str.content[0].text)
            assert data["ok"] is False
            assert "assignee: dev2" in data["message"]
            assert not mock_confirm.called

    @pytest.mark.asyncio
    async def test_lead_user_always_allowed(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        # cuong.huynh is cfg.lead_user, has no explicit project role in CP
        bot_gateway = FakeGateway(
            user="eggs-bot",
            role_members={"CP": {"Developers": ["other_dev"]}},
        )
        user_gateway = FakeGateway(
            user="cuong.huynh",
            issues={
                "CP-E1": {"key": "CP-E1", "fields": {"issuetype": {"name": "Epic"}, "summary": "CP Epic"}},
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch("mcp_atlassian.eggs.tools.get_user_gateway", AsyncMock(return_value=user_gateway)),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway),
            patch("mcp_atlassian.eggs.tools.Actions.uat") as mock_uat,
        ):
            mock_uat.return_value = ActionResult(ok=True, message="Lead UAT ok", changes=[], next_actions=[])

            res_str = await server.call_tool(
                "eggs_uat",
                {"epic": "CP-E1", "actor": "KT", "action": "start"},
            )
            data = json.loads(res_str.content[0].text)
            assert data["ok"] is True
            assert mock_uat.called
