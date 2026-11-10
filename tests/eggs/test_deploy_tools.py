from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from mcp_atlassian.eggs.tools import (
    ALL_EGGS_TOOL_NAMES,
    DEPLOY_ENABLED_TOOLS,
    DEPLOY_TOOLSETS,
    READ_ONLY_RAW_JIRA_TOOLS,
    register_eggs_tools,
)
from mcp_atlassian.jira import JiraConfig
from mcp_atlassian.servers.jira import jira_mcp
from mcp_atlassian.servers.main import MainAppContext, main_mcp
from mcp_atlassian.utils.tools import get_enabled_tools
from mcp_atlassian.utils.toolsets import get_enabled_toolsets


class TestDeployToolFiltering:
    """Test suite ensuring deployment configuration guarantees tool allow-list boundaries."""

    def test_env_parsing_yields_exact_deploy_allow_list(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        register_eggs_tools(jira_mcp, force=True)
        monkeypatch.setenv("ENABLED_TOOLS", ",".join(DEPLOY_ENABLED_TOOLS))
        monkeypatch.setenv("TOOLSETS", ",".join(DEPLOY_TOOLSETS))

        parsed_tools = get_enabled_tools()
        assert parsed_tools is not None
        assert sorted(parsed_tools) == sorted(DEPLOY_ENABLED_TOOLS)

        parsed_toolsets = get_enabled_toolsets()
        assert parsed_toolsets == set(DEPLOY_TOOLSETS)

    @pytest.mark.asyncio
    async def test_deploy_tool_list_enforces_read_only_and_eggs_present(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        register_eggs_tools(jira_mcp, force=True)
        monkeypatch.setenv("EGGS_ENABLED", "true")
        monkeypatch.setenv("JIRA_URL", "https://jira.eggstech.io.vn")
        monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "placeholder")
        monkeypatch.setenv("ALLOW_GLOBAL_CRED_FALLBACK", "false")
        monkeypatch.setenv("MCP_ALLOWED_URL_DOMAINS", "jira.eggstech.io.vn")
        monkeypatch.setenv("TOOLSETS", ",".join(DEPLOY_TOOLSETS))
        monkeypatch.setenv("ENABLED_TOOLS", ",".join(DEPLOY_ENABLED_TOOLS))

        jira_config = JiraConfig.from_env()
        app_context = MainAppContext(
            full_jira_config=jira_config,
            enabled_tools=get_enabled_tools(),
            enabled_toolsets=get_enabled_toolsets(),
        )
        request_context = MagicMock()
        request_context.request = None
        request_context.lifespan_context = {"app_lifespan_context": app_context}

        with patch.object(main_mcp, "_mcp_server") as mcp_server:
            mcp_server.request_context = request_context
            exposed_tools = await main_mcp._list_tools_mcp()
            exposed_names = {t.name for t in exposed_tools}

        # 1. Every exposed raw jira_* tool MUST be in the read-only allow-list
        raw_jira_exposed = {
            name
            for name in exposed_names
            if name.startswith("jira_") and not name.startswith("jira_eggs_")
        }
        assert raw_jira_exposed.issubset(READ_ONLY_RAW_JIRA_TOOLS), (
            f"Unauthorized raw Jira tools exposed: {raw_jira_exposed - READ_ONLY_RAW_JIRA_TOOLS}"
        )

        # 2. All 25 EGGS tools MUST be present
        eggs_exposed = {
            name
            for name in exposed_names
            if name.startswith("eggs_") or name.startswith("jira_eggs_")
        }
        assert len(eggs_exposed) == len(ALL_EGGS_TOOL_NAMES)
        for base_name in ALL_EGGS_TOOL_NAMES:
            assert f"jira_{base_name}" in exposed_names or base_name in exposed_names

        # 3. No raw write tools are exposed
        forbidden_raw_writes = {
            "jira_create_issue",
            "jira_update_issue",
            "jira_delete_issue",
            "jira_assign_issue",
            "jira_create_worklog",
            "jira_perform_bulk_issue_transitions",
            "jira_add_attachment",
            "jira_create_version",
            "jira_create_component",
        }
        assert not (exposed_names & forbidden_raw_writes)

    @pytest.mark.asyncio
    async def test_raw_jira_write_tool_is_not_authorized(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        register_eggs_tools(jira_mcp, force=True)
        monkeypatch.setenv("EGGS_ENABLED", "true")
        monkeypatch.setenv("JIRA_URL", "https://jira.eggstech.io.vn")
        monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "placeholder")
        monkeypatch.setenv("TOOLSETS", ",".join(DEPLOY_TOOLSETS))
        monkeypatch.setenv("ENABLED_TOOLS", ",".join(DEPLOY_ENABLED_TOOLS))

        jira_config = JiraConfig.from_env()
        app_context = MainAppContext(
            full_jira_config=jira_config,
            enabled_tools=get_enabled_tools(),
            enabled_toolsets=get_enabled_toolsets(),
        )
        ctx = {
            "read_only": False,
            "enabled_tools_filter": get_enabled_tools(),
            "enabled_toolsets_filter": get_enabled_toolsets(),
            "app_lifespan_state": app_context,
            "header_based_services": {"jira": False, "confluence": False},
            "jira_is_cloud": False,
        }

        # Create mock write tool
        mock_write_tool = MagicMock()
        mock_write_tool.tags = {"jira", "write", "toolset:jira_issues"}

        assert not main_mcp._is_tool_authorized(
            "jira_create_issue", mock_write_tool, ctx
        )
        assert not main_mcp._is_tool_authorized(
            "jira_delete_issue", mock_write_tool, ctx
        )

    @pytest.mark.asyncio
    async def test_read_only_raw_tools_contain_no_write_tags(self):
        register_eggs_tools(jira_mcp, force=True)
        tools = await main_mcp.list_tools()
        tools_by_name = {t.name: t for t in tools}

        for raw_tool in READ_ONLY_RAW_JIRA_TOOLS:
            if raw_tool in tools_by_name:
                tags = tools_by_name[raw_tool].tags
                assert "write" not in tags, f"Tool {raw_tool} has write tag!"
                assert "read" in tags, f"Tool {raw_tool} missing read tag!"

    def test_docker_compose_environment_matches_deploy_constants(self):
        compose_path = (
            Path(__file__).resolve().parent.parent.parent
            / "deploy"
            / "eggs"
            / "docker-compose.yml"
        )
        with compose_path.open() as f:
            data = yaml.safe_load(f)

        mcp_env = data["services"]["mcp"]["environment"]
        toolsets_env = [t.strip() for t in mcp_env["TOOLSETS"].split(",") if t.strip()]
        enabled_tools_env = [
            t.strip() for t in mcp_env["ENABLED_TOOLS"].split(",") if t.strip()
        ]

        assert tuple(enabled_tools_env) == DEPLOY_ENABLED_TOOLS
        assert tuple(toolsets_env) == DEPLOY_TOOLSETS
