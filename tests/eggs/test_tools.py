from __future__ import annotations

import dataclasses
import json
import os
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastmcp import FastMCP

from mcp_atlassian.eggs.actions import ActionResult
from mcp_atlassian.eggs.agenda import AgendaItem, SuggestedAction
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.tools import (
    ALL_EGGS_TOOL_NAMES,
    READ_EGGS_TOOL_NAMES,
    _attach_next_agenda_item,
    _make_snapshot,
    clear_identity_cache,
    get_roles_reader,
    get_user_gateway,
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
            "interrupts": "Interrupts",
            "causes": "Causes",
            "relates": "Relates",
            "duplicates": "Duplicate",
            "blocks": "Blocks",
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
            "health_flags": "customfield_10712",
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
        transitions={
            "set_due": "Set Due Date",
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
    async def test_toolsets_eggs_lists_exactly_25_names(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("TOOLSETS", "eggs")
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        enabled = get_enabled_toolsets()
        assert "eggs" in enabled

        all_tools = await server.list_tools()
        tools = [
            t for t in all_tools if should_include_tool_by_toolset(t.tags, enabled)
        ]
        tool_names = sorted(t.name for t in tools)
        assert len(tool_names) == len(ALL_EGGS_TOOL_NAMES)
        assert tool_names == sorted(ALL_EGGS_TOOL_NAMES)

    @pytest.mark.asyncio
    async def test_read_only_mode_hides_write_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ):
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
        assert len(tool_names) == len(READ_EGGS_TOOL_NAMES)
        assert tool_names == sorted(READ_EGGS_TOOL_NAMES)


class TestEggsToolBehavior:
    @pytest.mark.asyncio
    async def test_eggs_uat_reaches_actions_uat(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggsbot",
            role_members={"RFID": {"Business Analysts": ["ba.rfid"]}},
        )
        user_gateway = FakeGateway(
            user="ba.rfid",
            issues={
                "RFID-E1": {
                    "key": "RFID-E1",
                    "fields": {"issuetype": {"name": "Epic"}, "summary": "Epic 1"},
                },
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
            patch("mcp_atlassian.eggs.tools.Actions.uat") as mock_uat,
        ):
            mock_uat.return_value = ActionResult(
                ok=True,
                message="UAT start ok",
                changes=["RFID-S1 -> Wait For Review"],
                next_actions=[],
            )

            res_str = await server.call_tool(
                "eggs_uat",
                {
                    "epic": "RFID-E1",
                    "actor": "KH",
                    "action": "start",
                    "note": "All good",
                    "dry_run": False,
                },
            )
            # FastMCP call_tool returns CallToolResult with content
            res_json = json.loads(res_str.content[0].text)
            assert res_json["ok"] is True
            assert mock_uat.called
            mock_uat.assert_called_once_with(
                epic_key="RFID-E1",
                actor="KH",
                action="start",
                note="All good",
                story_keys=None,
                bugs=None,
                dry_run=False,
            )

    @pytest.mark.asyncio
    async def test_next_returns_agenda_and_role_filtered_intents(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggsbot",
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
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
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
            user="eggsbot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )
        user_gateway = FakeGateway(
            user="dev1",
            issues={},
        )

        mock_agenda = [
            AgendaItem(
                priority=7,
                key="RFID-20",
                project="RFID",
                title="T20",
                say="Doing 20",
                actions=[],
            ),
            AgendaItem(
                priority=7,
                key="RFID-10",
                project="RFID",
                title="T10",
                say="Doing 10",
                actions=[],
            ),
        ]

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
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
            user="eggsbot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )
        user_gateway = FakeGateway(
            user="dev1",
            issues={
                "RFID-E1": {
                    "key": "RFID-E1",
                    "fields": {"issuetype": {"name": "Epic"}},
                },
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
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
            user="eggsbot",
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
            actions=[
                SuggestedAction(tool="eggs_start", args={"key": "RFID-11"}, missing=[])
            ],
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
            patch(
                "mcp_atlassian.eggs.tools.Actions.start",
                return_value=ActionResult(
                    ok=True,
                    message="Start ok",
                    changes=["DEV To Do -> Dev In Progress"],
                    next_actions=[],
                ),
            ),
            patch("mcp_atlassian.eggs.tools.build_agenda", return_value=[next_item]),
        ):
            res_str = await server.call_tool(
                "eggs_start", {"key": "RFID-10", "dry_run": False}
            )
            data = json.loads(res_str.content[0].text)
            assert data["ok"] is True
            assert data["next"] is not None
            assert data["next"]["key"] == "RFID-11"

    @pytest.mark.asyncio
    async def test_write_tools_use_per_request_user_gateway(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggsbot",
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

        async def dynamic_user_gateway(
            ctx: Any, *args: Any, **kwargs: Any
        ) -> FakeGateway:
            if not user_gw_calls:
                user_gw_calls.append("dev1")
                return user_gateway_1
            user_gw_calls.append("dev2")
            return user_gateway_2

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                side_effect=dynamic_user_gateway,
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
            patch("mcp_atlassian.eggs.tools.Actions.start") as mock_start,
        ):
            mock_start.return_value = ActionResult(
                ok=True, message="start ok", changes=[], next_actions=[]
            )

            # Call 1 for dev1
            await server.call_tool("eggs_start", {"key": "RFID-10", "dry_run": True})

            # Call 2 for dev2
            await server.call_tool("eggs_start", {"key": "RFID-20", "dry_run": True})

            # Verify two different user gateways were fetched across the two requests
            assert user_gw_calls == ["dev1", "dev2"]

            # Bot gateway was only called for roles, no transitions or worklogs
            assert not any(
                c[0] in {"transition", "add_worklog", "create_issue"}
                for c in bot_gateway.calls
            )

    @pytest.mark.asyncio
    async def test_version_and_whoami(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        bot_gateway = FakeGateway(
            user="eggsbot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )
        user_gateway = FakeGateway(user="dev1")

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
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
            user="eggsbot",
            role_members={
                "RFID": {
                    "Developers": ["dev1", "dev2"],
                    "Business Analysts": ["ba.rfid"],
                }
            },
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
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
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
            user="eggsbot",
            role_members={"CP": {"Developers": ["other_dev"]}},
        )
        user_gateway = FakeGateway(
            user="cuong.huynh",
            issues={
                "CP-E1": {
                    "key": "CP-E1",
                    "fields": {"issuetype": {"name": "Epic"}, "summary": "CP Epic"},
                },
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
            patch("mcp_atlassian.eggs.tools.Actions.uat") as mock_uat,
        ):
            mock_uat.return_value = ActionResult(
                ok=True, message="Lead UAT ok", changes=[], next_actions=[]
            )

            res_str = await server.call_tool(
                "eggs_uat",
                {"epic": "CP-E1", "actor": "KT", "action": "start"},
            )
            data = json.loads(res_str.content[0].text)
            assert data["ok"] is True
            assert mock_uat.called

    def test_get_roles_reader_pat_auth_and_errors(self, cfg: EggsConfig):
        # 1. Success with PAT auth_type
        set_roles_reader(None)
        with (
            patch.dict(
                os.environ,
                {"JIRA_URL": "https://jira.test", "EGGS_BOT_TOKEN": "bot-tok-123"},
            ),
            patch("mcp_atlassian.eggs.tools.JiraFetcher") as mock_fetcher_cls,
        ):
            mock_fetcher = MagicMock()
            mock_fetcher_cls.return_value = mock_fetcher
            reader = get_roles_reader(cfg)
            assert reader is not None
            mock_fetcher_cls.assert_called_once()
            jira_config = mock_fetcher_cls.call_args[0][0]
            assert jira_config.auth_type == "pat"
            assert jira_config.url == "https://jira.test"
            assert jira_config.personal_token == "bot-tok-123"

        # 2. Missing JIRA_URL raises Vietnamese error
        set_roles_reader(None)
        cfg_no_url = EggsConfig(
            lead_user="cuong.huynh",
            jira_url="",
            link_types={
                "interrupts": "Interrupts",
                "causes": "Causes",
                "relates": "Relates",
                "duplicates": "Duplicate",
                "blocks": "Blocks",
            },
            projects={
                "RFID": ProjectConfig(
                    key="RFID", default_lead_ba="ba", triage_owner="qc"
                )
            },
        )
        with (
            patch.dict(os.environ, {"EGGS_BOT_TOKEN": "bot-tok-123"}, clear=True),
            pytest.raises(ValueError, match="Thiếu cấu hình JIRA_URL để kết nối Jira."),
        ):
            get_roles_reader(cfg_no_url)

        # 3. Missing EGGS_BOT_TOKEN raises Vietnamese error
        set_roles_reader(None)
        with (
            patch.dict(os.environ, {"JIRA_URL": "https://jira.test"}, clear=True),
            pytest.raises(
                ValueError,
                match="Thiếu biến môi trường EGGS_BOT_TOKEN để đọc vai trò dự án.",
            ),
        ):
            get_roles_reader(cfg)

    @pytest.mark.asyncio
    async def test_eggs_enabled_env_flag(self):
        # Default / unset -> not registered
        srv_off = FastMCP("test_off")
        with patch.dict(os.environ, {"EGGS_ENABLED": ""}, clear=True):
            register_eggs_tools(srv_off, force=False)
            assert len(await srv_off.list_tools()) == 0

        # Falsy values -> not registered
        for val in ("0", "false", "no", "off"):
            srv = FastMCP(f"test_{val}")
            with patch.dict(os.environ, {"EGGS_ENABLED": val}):
                register_eggs_tools(srv, force=False)
                assert len(await srv.list_tools()) == 0

        # Truthy values -> registered (all 28 tools)
        for val in ("1", "true", "yes", "TRUE", "Yes"):
            srv = FastMCP(f"test_{val}")
            with patch.dict(os.environ, {"EGGS_ENABLED": val}):
                register_eggs_tools(srv, force=False)
                assert len(await srv.list_tools()) == len(ALL_EGGS_TOOL_NAMES)

        # force=True overrides EGGS_ENABLED=False
        srv_forced = FastMCP("test_forced")
        with patch.dict(os.environ, {"EGGS_ENABLED": "false"}):
            register_eggs_tools(srv_forced, force=True)
            assert len(await srv_forced.list_tools()) == len(ALL_EGGS_TOOL_NAMES)

    def test_dates_asia_ho_chi_minh_timezone(self, cfg: EggsConfig):
        # 06:00 Asia/Ho_Chi_Minh = 23:00 UTC previous day
        dt_utc = datetime(2026, 11, 18, 23, 0, 0, tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))

        assert dt_utc.date() == date(2026, 11, 18)
        assert dt_local.date() == date(2026, 11, 19)

        gw = FakeGateway(
            issues={
                "RFID-10": {
                    "key": "RFID-10",
                    "fields": {
                        "issuetype": {"name": "Dev Task"},
                        "summary": "Task 1",
                        "status": {"name": "Dev In Progress"},
                        "project": {"key": "RFID"},
                    },
                }
            }
        )
        cal = WorkCalendar(cfg.holidays)
        snapshot = _make_snapshot(gw, cfg, cal, now=dt_utc)
        assert snapshot.today == date(2026, 11, 19)

    @pytest.mark.asyncio
    async def test_request_reschedule_no_impact_lead_not_assignee_refused(
        self, cfg: EggsConfig
    ):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        # cuong.huynh is lead_user, but task assignee is dev2
        user_gateway = FakeGateway(
            user="cuong.huynh",
            issues={
                "RFID-10": {
                    "key": "RFID-10",
                    "fields": {
                        "issuetype": {"name": "Dev Task"},
                        "summary": "Task 10",
                        "status": {"name": "Dev In Progress"},
                        "project": {"key": "RFID"},
                        "assignee": {"name": "dev2"},
                    },
                }
            },
        )
        bot_gateway = FakeGateway(
            user="eggsbot",
            role_members={"RFID": {"Developers": ["dev2"]}},
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
        ):
            res_str = await server.call_tool(
                "eggs_request_reschedule",
                {"key": "RFID-10", "no_impact": True, "reason": "Lead says no impact"},
            )
            data = json.loads(res_str.content[0].text)
            assert data["ok"] is False
            assert "assignee: dev2" in data["message"]

    @pytest.mark.asyncio
    async def test_username_comparison_case_insensitive_henry(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        # Issue assignee is lowercase 'henry'
        issues = {
            "RFID-10": {
                "key": "RFID-10",
                "fields": {
                    "project": {"key": "RFID"},
                    "issuetype": {"name": "Dev Task", "subtask": True},
                    "status": {"name": "Dev In Progress"},
                    "assignee": {"name": "henry"},
                    "customfield_10108": "2026-11-18",
                    "customfield_10109": "2026-11-19",
                    cfg.fields["baseline_end"]: "2026-11-19",
                    "customfield_10712": [{"value": "NEED_RESCHEDULE"}],
                },
            }
        }
        cfg_with_trans = dataclasses.replace(
            cfg,
            transitions={
                "confirm_no_impact": "Confirm No Impact",
                "request_reschedule": "Request Reschedule",
            },
        )
        # Caller is capitalized 'Henry'
        user_gateway = FakeGateway(
            issues=issues,
            user="Henry",
            transitions={"RFID-10": ["Confirm No Impact", "Request Reschedule"]},
        )
        bot_gateway = FakeGateway(
            user="eggsbot",
            role_members={"RFID": {"Developers": ["henry"]}},
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg_with_trans),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
        ):
            # 1. confirm_no_impact with Henry vs henry
            res_str = await server.call_tool(
                "eggs_request_reschedule",
                {"key": "RFID-10", "no_impact": True, "reason": "No impact confirm"},
            )
            data = json.loads(res_str.content[0].text)
            assert data["ok"] is True, f"Failed: {data.get('message')}"

            # 2. request_reschedule with Henry vs henry
            res_str2 = await server.call_tool(
                "eggs_request_reschedule",
                {
                    "key": "RFID-10",
                    "proposed_end": "2026-11-20",
                    "reason": "Need more time",
                },
            )
            data2 = json.loads(res_str2.content[0].text)
            assert data2["ok"] is True, f"Failed: {data2.get('message')}"

    @pytest.mark.asyncio
    async def test_workstream_lists_epics_with_logical_type_check(
        self, cfg: EggsConfig
    ):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        issues = {
            "RFID-EPIC": {
                "key": "RFID-EPIC",
                "fields": {
                    "project": {"key": "RFID"},
                    "issuetype": {"name": "Epic", "subtask": False},
                    "status": {"name": "DEV To Do"},
                    "summary": "RFID Epic 1",
                },
            }
        }
        user_gateway = FakeGateway(issues=issues, user="cuong.huynh")
        bot_gateway = FakeGateway(user="eggsbot")

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
        ):
            res = await server.call_tool("eggs_workstream_status", {"project": "RFID"})
            text = res.content[0].text
            assert "Không tìm thấy workstream nào" not in text
            assert "RFID-EPIC" in text

        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        cfg_with_other = EggsConfig(
            lead_user=cfg.lead_user,
            jira_url=cfg.jira_url,
            roles={
                "dev": ["Developers"],
                "qc": ["Testers"],
                "lead_ba": ["Business Analysts", "Administrators"],
                "other": ["Viewers"],
            },
            link_types=cfg.link_types,
            projects=cfg.projects,
        )

        user_gateway = FakeGateway(user="dev1")
        bot_gateway = FakeGateway(
            user="eggsbot",
            role_members={
                "RFID": {"Developers": ["dev1"]},
                "CP": {"Viewers": ["dev1"]},
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg_with_other),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
        ):
            # 1. Empty entries
            res_empty = await server.call_tool("eggs_end_day", {"entries": []})
            data_empty = json.loads(res_empty.content[0].text)
            assert data_empty["ok"] is False
            assert (
                "Danh sách công việc cuối ngày không được để trống."
                in data_empty["message"]
            )

            # 2. Non-dict entry
            res_nondict = await server.call_tool(
                "eggs_end_day", {"entries": ["invalid"]}
            )
            data_nondict = json.loads(res_nondict.content[0].text)
            assert data_nondict["ok"] is False
            assert "Mục 0" in data_nondict["message"]
            assert "phải là một đối tượng (dict)" in data_nondict["message"]

            # 3. Entry missing key
            res_nokey = await server.call_tool(
                "eggs_end_day", {"entries": [{"time_spent": "1h"}]}
            )
            data_nokey = json.loads(res_nokey.content[0].text)
            assert data_nokey["ok"] is False
            assert "Mục 0" in data_nokey["message"]
            assert "thiếu trường bắt buộc 'key'" in data_nokey["message"]

            # 3b. Entry missing time_spent
            res_notime = await server.call_tool(
                "eggs_end_day", {"entries": [{"key": "RFID-1"}, {"key": "RFID-2"}]}
            )
            data_notime = json.loads(res_notime.content[0].text)
            assert data_notime["ok"] is False
            assert "Mục 0" in data_notime["message"]
            assert "thiếu trường bắt buộc 'time_spent'" in data_notime["message"]

            # 3c. Entry with unknown keys
            res_unknown = await server.call_tool(
                "eggs_end_day",
                {
                    "entries": [
                        {"key": "RFID-1", "time_spent": "1h"},
                        {"key": "RFID-2", "time_spent": "1h", "unknown_field": 123},
                    ]
                },
            )
            data_unknown = json.loads(res_unknown.content[0].text)
            assert data_unknown["ok"] is False
            assert "Mục 1" in data_unknown["message"]
            assert (
                "không hợp lệ" in data_unknown["message"]
                or "unknown_field" in data_unknown["message"]
            )

            # 3d. Entry with invalid move_to
            res_moveto = await server.call_tool(
                "eggs_end_day",
                {
                    "entries": [
                        {"key": "RFID-1", "time_spent": "1h", "move_to": "in_progress"}
                    ]
                },
            )
            data_moveto = json.loads(res_moveto.content[0].text)
            assert data_moveto["ok"] is False
            assert "Mục 0" in data_moveto["message"]
            assert "move_to" in data_moveto["message"]

            # 4. Entry for project with missing role (e.g. CP-10)
            res_norole = await server.call_tool(
                "eggs_end_day",
                {"entries": [{"key": "CP-10", "time_spent": "1h", "note": "Worked"}]},
            )
            data_norole = json.loads(res_norole.content[0].text)
            assert data_norole["ok"] is False
            assert "Dev, QC hoặc Lead-BA" in data_norole["message"]

    @pytest.mark.asyncio
    async def test_role_refusal_label_dev_qc_lead_ba(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        cfg_with_other = EggsConfig(
            lead_user=cfg.lead_user,
            jira_url=cfg.jira_url,
            roles={
                "dev": ["Developers"],
                "qc": ["Testers"],
                "lead_ba": ["Business Analysts", "Administrators"],
                "other": ["Viewers"],
            },
            link_types=cfg.link_types,
            projects=cfg.projects,
        )

        # User has role in RFID, but in CP has only "other" role (Viewers)
        user_gateway = FakeGateway(user="dev1")
        bot_gateway = FakeGateway(
            user="eggsbot",
            role_members={
                "RFID": {"Developers": ["dev1"]},
                "CP": {"Viewers": ["dev1"]},
            },
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg_with_other),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
        ):
            # log_work on CP-10
            res_lw = await server.call_tool(
                "eggs_log_work", {"key": "CP-10", "time_spent": "1h", "note": "Log"}
            )
            data_lw = json.loads(res_lw.content[0].text)
            assert data_lw["ok"] is False
            assert "Dev, QC hoặc Lead-BA" in data_lw["message"]

            # block on CP-10
            res_b = await server.call_tool(
                "eggs_block", {"key": "CP-10", "reason": "Wait", "blocked_by": "Nội bộ"}
            )
            data_b = json.loads(res_b.content[0].text)
            assert data_b["ok"] is False
            assert "Dev, QC hoặc Lead-BA" in data_b["message"]

            # unblock on CP-10
            res_ub = await server.call_tool(
                "eggs_unblock", {"key": "CP-10", "note": "Resolved"}
            )
            data_ub = json.loads(res_ub.content[0].text)
            assert data_ub["ok"] is False
            assert "Dev, QC hoặc Lead-BA" in data_ub["message"]

    @pytest.mark.asyncio
    async def test_get_user_gateway_write_requires_user_token(self, cfg: EggsConfig):
        # 1. Request context with NO user token on write=True
        mock_ctx_no_token = MagicMock()
        mock_ctx_no_token.request_context.request.state.user_atlassian_token = None

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            pytest.raises(ValueError, match="Thao tác ghi yêu cầu token cá nhân"),
        ):
            await get_user_gateway(mock_ctx_no_token, write=True)

        # 2. Request context WITH user token on write=True succeeds
        mock_ctx_with_token = MagicMock()
        mock_ctx_with_token.request_context.request.state.user_atlassian_token = (
            "user-pat-xyz"
        )
        mock_fetcher = MagicMock()

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.servers.dependencies.get_jira_fetcher",
                AsyncMock(return_value=mock_fetcher),
            ),
        ):
            gw = await get_user_gateway(mock_ctx_with_token, write=True)
            assert gw is not None

    def test_attach_next_agenda_item_failure_adds_warning_message(
        self, cfg: EggsConfig
    ):
        res = ActionResult(
            ok=True, message="Thực hiện thành công.", changes=[], next_actions=[]
        )
        ident = MagicMock()
        cal = WorkCalendar(cfg.holidays)
        now = datetime.now(ZoneInfo(cfg.timezone))

        # Faulty gateway that raises when building snapshot
        faulty_gw = MagicMock()
        faulty_gw.search.side_effect = RuntimeError("Connection dropped")

        attached = _attach_next_agenda_item(res, faulty_gw, ident, cfg, cal, now)
        assert attached.ok is True
        assert "Thực hiện thành công." in attached.message
        assert "Không lấy được việc tiếp theo." in attached.message

    def test_get_roles_reader_thread_safe_singleton_with_lock(
        self, cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch
    ):
        import concurrent.futures
        import threading

        from mcp_atlassian.eggs import tools

        assert hasattr(tools, "_roles_reader_lock"), (
            "tools must define _roles_reader_lock"
        )
        assert isinstance(tools._roles_reader_lock, type(threading.Lock()))

        monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
        monkeypatch.setenv("EGGS_BOT_TOKEN", "bot-token-123")

        gateways = []
        with patch("mcp_atlassian.eggs.tools.JiraFetcher") as mock_fetcher:
            mock_jira = MagicMock()
            mock_fetcher.return_value.jira = mock_jira

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(get_roles_reader, cfg) for _ in range(16)]
                gateways = [f.result() for f in futures]

            assert len(gateways) == 16
            assert all(g is gateways[0] for g in gateways)
            # Fetcher should have been called only once despite 16 concurrent threads
            assert mock_fetcher.call_count == 1

    @pytest.mark.asyncio
    async def test_async_tools_use_to_thread_for_config_and_roles_reader(
        self, cfg: EggsConfig
    ):
        import asyncio

        from mcp_atlassian.eggs import tools

        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        mock_user_gw = FakeGateway(user="alice")
        mock_roles_gw = FakeGateway()

        real_to_thread = asyncio.to_thread
        to_thread_calls = []

        async def tracked_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func)
            return await real_to_thread(func, *args, **kwargs)

        with (
            patch(
                "mcp_atlassian.eggs.tools.asyncio.to_thread",
                side_effect=tracked_to_thread,
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=mock_user_gw),
            ),
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=mock_roles_gw
            ),
        ):
            await server.call_tool("eggs_whoami", {})

            assert tools.load_config in to_thread_calls, (
                "load_config must be called via asyncio.to_thread"
            )
            assert tools.get_roles_reader in to_thread_calls, (
                "get_roles_reader must be called via asyncio.to_thread"
            )

    @pytest.mark.asyncio
    async def test_eggs_submit_qc_param_and_docstrings(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        tool_submit = await server.get_tool("eggs_submit_qc")
        assert "qc_owner" in tool_submit.parameters["properties"]
        qc_prop_desc = tool_submit.parameters["properties"]["qc_owner"]["description"]
        assert "Ad-hoc Task" in qc_prop_desc
        assert "Bug stand-alone" in qc_prop_desc
        assert "eggs_finish" in tool_submit.description
        assert "qc_owner" in tool_submit.description
        assert "Dev Task" in tool_submit.description
        assert "Bug" in tool_submit.description

        tool_finish = await server.get_tool("eggs_finish")
        assert "eggs_finish" in tool_finish.description
        assert "eggs_submit_qc" in tool_finish.description
        assert "qc_owner" in tool_finish.description
        assert "Dev Task" in tool_finish.description
        assert "Bug" in tool_finish.description

        user_gw = FakeGateway(user="dev1")
        bot_gw = FakeGateway(role_members={"RFID": {"Developers": ["dev1"]}})
        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gw),
            ),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gw),
            patch("mcp_atlassian.eggs.tools.Actions.submit_qc") as mock_submit,
        ):
            mock_submit.return_value = ActionResult(
                ok=True, message="OK", changes=[], next_actions=[]
            )
            res = await server.call_tool(
                "eggs_submit_qc", {"key": "RFID-10", "qc_owner": "tuan.bui"}
            )
            data = json.loads(res.content[0].text)
            assert data["ok"] is True
            mock_submit.assert_called_once_with(
                key="RFID-10", note="", qc_owner="tuan.bui", dry_run=False
            )


def test_action_result_to_dict_code_field():
    from mcp_atlassian.eggs.tools import action_result_to_dict

    res_without_code = ActionResult(
        ok=False, message="err", changes=[], next_actions=[]
    )
    d1 = action_result_to_dict(res_without_code)
    assert "code" not in d1

    res_with_code = ActionResult(
        ok=False, message="err", changes=[], next_actions=[], code="missing_qc_owner"
    )
    d2 = action_result_to_dict(res_with_code)
    assert d2.get("code") == "missing_qc_owner"


class TestPlanTools:
    VALID_PLAN_YAML = """
schema: eggs-plan/v2
project: RFID
epic:
  summary: Epic Test
  due: 2026-12-01
  owner: ba1
flows:
  - code: CH-01
    actor: CH
    summary: Flow Check-in
    ac:
      - Scan RFID tag
    due: 2026-11-25
    ba: ba1
    qc: qc1
    depends_on: []
    parts:
      - part: BE
        assignee: dev1
        estimate: 1d
tech_tasks: []
open_questions: []
"""

    INVALID_PLAN_YAML = """
schema: eggs-plan/v2
project: RFID
epic:
  summary: Epic Test
  due: 2026-12-01
  owner: ba1
flows:
  - code: UNKNOWN-01
    actor: UNKNOWN
    summary: Flow Unknown Actor
    ac:
      - Scan RFID tag
    due: 2026-11-25
    ba: ba1
    qc: qc1
    depends_on: []
    parts:
      - part: BE
        assignee: dev1
        estimate: 1d
tech_tasks: []
open_questions: []
"""

    @pytest.mark.asyncio
    async def test_eggs_migrate_epic_refuses_dev(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        user_gw = FakeGateway(user="dev1")
        bot_gw = FakeGateway(
            user="eggsbot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gw),
            ),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gw),
        ):
            res_str = await server.call_tool("eggs_migrate_epic", {"epic": "RFID-532"})
            res_json = json.loads(res_str.content[0].text)
            assert res_json.get("ok") is False
            assert "Lead-BA" in res_json.get("message", "")

    @pytest.mark.asyncio
    async def test_eggs_plan_apply_refuses_dev(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        user_gw = FakeGateway(user="dev1")
        bot_gw = FakeGateway(
            user="eggsbot",
            role_members={"RFID": {"Developers": ["dev1"]}},
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gw),
            ),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gw),
        ):
            res_str = await server.call_tool(
                "eggs_plan_apply", {"plan_yaml": self.VALID_PLAN_YAML}
            )
            res_json = json.loads(res_str.content[0].text)
            assert res_json["ok"] is False
            assert "Lead-BA" in res_json["message"]

    @pytest.mark.asyncio
    async def test_eggs_plan_apply_refuses_invalid_plan(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        user_gw = FakeGateway(
            user="ba1",
            components_map={"RFID": ["CH - Check-in"]},
            allowed_field_values={
                ("RFID", "Dev Task", cfg.fields.get("part", "customfield_10701")): [
                    "BE",
                    "FE APP",
                ]
            },
            existing_users={"ba1", "qc1", "dev1"},
        )
        bot_gw = FakeGateway(
            user="eggsbot",
            role_members={"RFID": {"Business Analysts": ["ba1"]}},
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gw),
            ),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gw),
        ):
            initial_issue_count = len(user_gw.issues)
            res_str = await server.call_tool(
                "eggs_plan_apply", {"plan_yaml": self.INVALID_PLAN_YAML}
            )
            res_json = json.loads(res_str.content[0].text)
            assert res_json["ok"] is False
            assert len(res_json["warnings"]) > 0
            # Nothing created
            assert len(user_gw.issues) == initial_issue_count
            assert res_json["created"] == []

    @pytest.mark.asyncio
    async def test_plan_tools_json_shapes(self, cfg: EggsConfig):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)

        user_gw = FakeGateway(
            user="ba1",
            components_map={"RFID": ["CH - Check-in"]},
            allowed_field_values={
                ("RFID", "Dev Task", cfg.fields.get("part", "customfield_10701")): [
                    "BE",
                    "FE APP",
                ]
            },
            existing_users={"ba1", "qc1", "dev1"},
            issues={
                "RFID-532": {
                    "key": "RFID-532",
                    "fields": {
                        "issuetype": {"name": "Epic"},
                        "summary": "Existing Epic",
                    },
                }
            },
        )
        bot_gw = FakeGateway(
            user="eggsbot",
            role_members={"RFID": {"Business Analysts": ["ba1"]}},
        )

        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gw),
            ),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gw),
        ):
            # 1. eggs_plan_validate shape: {ok, errors, warnings, questions, tree, plan_yaml}
            val_res = await server.call_tool(
                "eggs_plan_validate", {"plan_yaml": self.VALID_PLAN_YAML}
            )
            val_json = json.loads(val_res.content[0].text)
            assert "ok" in val_json
            assert "errors" in val_json
            assert "warnings" in val_json
            assert "questions" in val_json
            assert "tree" in val_json
            assert "plan_yaml" in val_json
            assert val_json["ok"] is True

            # 2. eggs_plan_apply shape: {ok, message, changes, created, updated, warnings, plan_yaml}
            apply_res = await server.call_tool(
                "eggs_plan_apply",
                {"plan_yaml": self.VALID_PLAN_YAML, "dry_run": True},
            )
            apply_json = json.loads(apply_res.content[0].text)
            assert "ok" in apply_json
            assert "message" in apply_json
            assert "changes" in apply_json
            assert "created" in apply_json
            assert "updated" in apply_json
            assert "warnings" in apply_json
            assert "plan_yaml" in apply_json
            assert "errors" in apply_json
            assert "would_create" in apply_json
            assert apply_json["ok"] is True
            assert "DRYRUN" not in apply_json["plan_yaml"]
            assert apply_json["would_create"]

            # 3. eggs_migrate_epic shape: {ok, plan_yaml, stories, missing, other_issues}
            migrate_res = await server.call_tool(
                "eggs_migrate_epic", {"epic": "RFID-532"}
            )
            migrate_json = json.loads(migrate_res.content[0].text)
            assert migrate_json["ok"] is True
            assert "plan_yaml" in migrate_json
            assert "stories" in migrate_json
            assert "missing" in migrate_json
            assert "actor" in migrate_json["missing"]
            assert "due" in migrate_json["missing"]
            assert "qc" in migrate_json["missing"]
            assert "other_issues" in migrate_json


class TestLogWorkToolWiring:
    """W1 (11/11): eggs_log_work crashed on every call (`started` kwarg)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started", [None, "2026-11-10"])
    async def test_eggs_log_work_reaches_gateway_without_mocking_actions(
        self, cfg: EggsConfig, started: str | None
    ):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)
        bot_gateway = FakeGateway(
            user="eggsbot", role_members={"RFID": {"Developers": ["dev1"]}}
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
        args: dict[str, Any] = {
            "key": "RFID-10",
            "time_spent": "2h",
            "note": "code",
            "dry_run": False,
        }
        if started:
            args["started"] = started
        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user_gateway),
            ),
            patch(
                "mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot_gateway
            ),
            patch("mcp_atlassian.eggs.tools.build_agenda", return_value=[]),
        ):
            res = await server.call_tool("eggs_log_work", args)
        data = json.loads(res.content[0].text)
        assert data["ok"] is True, data
        logged = [c for c in user_gateway.calls if c[0] == "add_worklog"]
        assert len(logged) == 1
        if started:
            assert logged[0][2]["started"].date() == date(2026, 11, 10)


def _actions_call_mismatches() -> list[str]:
    """Every eggs tool calls Actions with keyword names Actions accepts."""
    import ast
    import inspect
    from pathlib import Path

    from mcp_atlassian.eggs import tools as tools_mod
    from mcp_atlassian.eggs.actions import Actions

    tree = ast.parse(Path(tools_mod.__file__).read_text(encoding="utf-8"))
    problems: list[str] = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.AsyncFunctionDef) and fn.name.startswith("eggs_")):
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            func, args = call.func, list(call.args)
            target: str | None = None
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "to_thread"
                and args
                and isinstance(args[0], ast.Attribute)
                and isinstance(args[0].value, ast.Name)
                and args[0].value.id == "actions"
            ):
                target, n_pos = args[0].attr, len(args) - 1
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "actions"
            ):
                target, n_pos = func.attr, len(args)
            if target is None:
                continue
            method = getattr(Actions, target, None)
            if method is None:
                problems.append(f"{fn.name}: Actions.{target} does not exist")
                continue
            sig = inspect.signature(method)
            kwargs = {k.arg: None for k in call.keywords if k.arg}
            try:
                sig.bind(None, *([None] * n_pos), **kwargs)
            except TypeError as e:
                problems.append(f"{fn.name} -> Actions.{target}: {e}")
    return problems


def test_every_tool_calls_actions_with_matching_arguments():
    assert _actions_call_mismatches() == []


class TestBrokenWrappersFixed:
    """B15–B17 (11/11): tools crashed on argument names before reaching Jira."""

    @staticmethod
    async def _call(cfg: EggsConfig, name: str, args: dict[str, Any], issues: dict):
        server = FastMCP("test_jira")
        register_eggs_tools(server, force=True)
        members = ["lead1"]
        bot = FakeGateway(
            user="eggsbot",
            role_members={
                "RFID": {
                    "Business Analysts": members,
                    "Testers": members,
                    "Developers": members,
                }
            },
        )
        user = FakeGateway(user="lead1", issues=issues)
        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=user),
            ),
            patch("mcp_atlassian.eggs.tools.get_roles_reader", return_value=bot),
            patch("mcp_atlassian.eggs.tools.build_agenda", return_value=[]),
        ):
            res = await server.call_tool(name, args)
        return json.loads(res.content[0].text)

    @pytest.mark.asyncio
    async def test_triage_close_accepts_bug_target_and_real_verdicts(self, cfg):
        report = {
            "key": "RFID-50",
            "fields": {
                "issuetype": {"name": "User Report"},
                "summary": "Khách báo lỗi",
                "status": {"name": "Analyzed"},
                "project": {"key": "RFID"},
            },
        }
        data = await self._call(
            cfg,
            "eggs_triage_close",
            {
                "key": "RFID-50",
                "verdict": "guidance",
                "note": "Hướng dẫn sử dụng",
                "time_spent": "10m",
                "dry_run": True,
            },
            {"RFID-50": report},
        )
        assert "ok" in data, data

    @pytest.mark.asyncio
    async def test_decide_reschedule_maps_approved_and_reason(self, cfg):
        data = await self._call(
            cfg,
            "eggs_decide_reschedule",
            {"key": "RFID-10", "approved": False, "reason": "", "dry_run": True},
            {
                "RFID-10": {
                    "key": "RFID-10",
                    "fields": {
                        "issuetype": {"name": "Dev Task"},
                        "summary": "T",
                        "status": {"name": "Dev In Progress"},
                        "project": {"key": "RFID"},
                    },
                }
            },
        )
        assert data["ok"] is False
        assert "chờ duyệt" in data["message"]

    @pytest.mark.asyncio
    async def test_uat_maps_epic_and_stories(self, cfg):
        data = await self._call(
            cfg,
            "eggs_uat",
            {
                "epic": "RFID-1",
                "actor": "KH",
                "action": "fail",
                "note": "",
                "stories": ["RFID-2"],
                "bugs": {"RFID-2": ["Sai tổng tiền"]},
                "dry_run": True,
            },
            {},
        )
        assert data["ok"] is False
        assert "bắt buộc" in data["message"]

    @pytest.mark.asyncio
    async def test_schedule_parses_dates_and_has_no_assignee(self, cfg):
        data = await self._call(
            cfg,
            "eggs_schedule",
            {
                "key": "RFID-10",
                "start": "11/11/2026",
                "end": "2026-11-12",
                "estimate": "4h",
                "dry_run": True,
            },
            {},
        )
        assert data["ok"] is False
        assert "YYYY-MM-DD" in data["message"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-11 09:30", "2026-11-11T09:30:00+07:00"),
        ("2026-11-11T09:30:00+07:00", "2026-11-11T09:30:00+07:00"),
        ("2026-11-11T09:30:00.000+0700", "2026-11-11T09:30:00+07:00"),
        ("11/11/2026 09:30", "2026-11-11T09:30:00+07:00"),
    ],
)
def test_parse_reported_at_formats(text: str, expected: str):
    from mcp_atlassian.eggs.tools import _parse_reported_at

    got = _parse_reported_at(text, "Asia/Ho_Chi_Minh")
    assert got is not None
    assert got.isoformat() == expected


def test_parse_reported_at_rejects_garbage_and_blank():
    from mcp_atlassian.eggs.tools import _parse_reported_at

    assert _parse_reported_at("  ", "Asia/Ho_Chi_Minh") is None
    with pytest.raises(ValueError):
        _parse_reported_at("hôm qua", "Asia/Ho_Chi_Minh")


def test_triage_open_refuses_without_source_or_reporter(cfg: EggsConfig):
    from mcp_atlassian.eggs.actions import Actions

    gw = FakeGateway(user="qc.rfid")
    res = Actions(gw, cfg, WorkCalendar(cfg.holidays)).triage_open(
        project="RFID", text="Lỗi in hóa đơn", source_url=" ", reported_by=""
    )
    assert res.ok is False
    assert res.missing == ["source_url", "reported_by"]
    assert not [c for c in gw.calls if c[0] in ("create_issue", "search")]
