# Upstream Notes: mcp-atlassian

## Architecture Findings

1. **Server Structure:**
   - `src/mcp_atlassian/servers/jira.py` defines `jira_mcp: ErrorPreservingFastMCP` where Jira tools are registered with `@jira_mcp.tool(tags={...})`.
   - `src/mcp_atlassian/servers/main.py` defines `AtlassianMCP` (subclassing `ErrorPreservingFastMCP[MainAppContext]`), serving Starlette HTTP/SSE apps with `main_lifespan`.

2. **Tool Filtering (Toolsets & ENABLED_TOOLS):**
   - Enforced at both listing (`_list_tools_mcp`) and invocation (`_call_tool_mcp`) via `_is_tool_authorized()`.
   - Checks `should_include_tool_by_toolset(tool.tags, enabled_toolsets)` against `TOOLSETS` env var.
   - Checks `should_include_tool(name, enabled_tools)` against `ENABLED_TOOLS` env var.
   - If `read_only` is true, any tool tagged with `"write"` is excluded.

3. **Per-User PAT Authentication:**
   - `UserTokenMiddleware` in `servers/main.py` parses `Authorization: Token <PAT>` or headers into `request.state`.
   - `get_jira_fetcher(ctx)` in `servers/dependencies.py` extracts credentials from `ctx.request_context.request.state`, instantiating a per-user `JiraConfig` and `JiraFetcher` (with `fetcher.jira` as the underlying `atlassian.Jira` client).

4. **EGGS Toolset Registration Hook:**
   - Location: `src/mcp_atlassian/servers/jira.py` lines 4894-4897.
   - Code:
     ```python
     # Register EGGS toolset
     from mcp_atlassian.eggs.tools import register_eggs_tools  # noqa: E402

     register_eggs_tools(jira_mcp)
     ```
   - Dynamically registers toolset `"eggs"` in `ALL_TOOLSETS` and `JIRA_TOOLSETS`.
   - Guarded by the `EGGS_ENABLED` environment variable (truthy: `"1"`, `"true"`, `"yes"`; default off). When `EGGS_ENABLED` is unset or falsy, `register_eggs_tools()` returns immediately without registering the `eggs` toolset or its 25 tools.
   - Upstream unit tests and `scripts/generate_tool_docs.py --check` run with `EGGS_ENABLED` unset, keeping EGGS tools out of upstream generated documentation and preserving exact upstream assertions.

5. **Deployment & Credential Isolation:**
   - Deployment sets `TOOLSETS=eggs` (or appropriate toolset list) and must **never** set `ALLOW_GLOBAL_CRED_FALLBACK`.
   - `get_user_gateway(ctx, write=True)` strictly enforces that write operations carry a per-user Personal Access Token (PAT) via `Authorization` header populating `request.state.user_atlassian_token`. Requests lacking a per-user PAT are rejected early with a clear Vietnamese error message, ensuring zero write fallback to global/operator credentials.

## Deviations from plan

- **macOS Symlink in test_stdio_keeps_wide_cwd:**
  - `tests/unit/utils/test_io.py:221` asserted `assert validate_safe_path("/etc/hostname") == Path("/etc/hostname")`.
  - On macOS Darwin, `/etc` is a symlink to `/private/etc`, causing `validate_safe_path` to return `PosixPath('/private/etc/hostname')`.
  - Adapted line 221 to `assert validate_safe_path("/etc/hostname") == Path("/etc/hostname").resolve()` so all 4543 upstream unit tests pass on macOS.

- **Unignore `eggs/` directory in `.gitignore`:**
  - Upstream `.gitignore` contained unanchored `eggs/` which ignored `src/mcp_atlassian/eggs/` and `tests/eggs/`.
  - Changed to `/eggs/` with explicit whitelists `!src/mcp_atlassian/eggs/` and `!tests/eggs/`.

- **Ruff line length (E501) in test files:**
  - In `pyproject.toml` under `[tool.ruff.lint.per-file-ignores]`, reverted `E501` from `"tests/**/*.py"` and instead added a separate entry `"tests/eggs/**/*.py" = ["S", "ANN", "B017", "E501"]` to preserve upstream test linting settings.

- **Guardian CLI entrypoint in pyproject.toml:**
  - Added `eggs-guardian = "mcp_atlassian.eggs.guardian:main"` to `[project.scripts]` in `pyproject.toml` to provide the standalone cron/CLI binary.

## Behavior notes

- **Past-due Story within unscheduled window:**
  A past-due Story still counts as within the UNSCHEDULED window for its todo dev_tasks.
- **Overdue items count as within interrupt window:**
  When checking if an unresolved work item has its planned end date within `interrupt_window` working days, overdue items (`planned_end < today`) return 0 working days from `cal.working_days_between(today, y.planned_end)` and thus count as within the window.
- **Resolved owned dev_tasks count for exemption:**
  In `detect_interruptions`, the assignee's owned dev_tasks under a parent Story are indexed without filtering out resolved tasks, so an assignee whose dev_task is already Done is still exempt from having their `bug_sub` under that same Story flagged as an interruption.
- **Reschedule action parameter aliases:**
  `Actions.request_reschedule` gained `proposed_end` as an alias alongside `new_end`. `Actions.flow_reschedule` gained `key` and `proposed_due` as aliases alongside `story_key` and `new_due`. Old parameter names are preserved for backwards compatibility.
- **JiraGateway protocol methods for subtasks & worklogs:**
  `get_subtasks` and `get_worklogs` methods added to `JiraGateway` protocol, `AtlassianGateway`, and test fakes to avoid `hasattr` duck-typing checks in production code (`_has_open_bug_subtask` and `qc_pass`).
- **Jira DC Epic Name requirement:**
  `customfield_10102` (`epic_name`) added to default fields configuration and create payload for `ensure_ops_epics` on Jira Data Center.
- **Guardian writes through edit screen:**
  Guardian fields live on the EGGS Edit/Story Edit screens (Jira DC rejects overrideScreenSecurity, verified 10/11/2026); writes use notifyUsers=false (eggsbot is jira-administrators); resolved issues are skipped.


