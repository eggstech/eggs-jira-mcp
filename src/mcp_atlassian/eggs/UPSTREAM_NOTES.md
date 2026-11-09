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

## Deviations from plan

- **macOS Symlink in test_stdio_keeps_wide_cwd:**
  - `tests/unit/utils/test_io.py:221` asserted `assert validate_safe_path("/etc/hostname") == Path("/etc/hostname")`.
  - On macOS Darwin, `/etc` is a symlink to `/private/etc`, causing `validate_safe_path` to return `PosixPath('/private/etc/hostname')`.
  - Adapted line 221 to `assert validate_safe_path("/etc/hostname") == Path("/etc/hostname").resolve()` so all 4543 upstream unit tests pass on macOS.

- **Unignore `eggs/` directory in `.gitignore`:**
  - Upstream `.gitignore` contained unanchored `eggs/` which ignored `src/mcp_atlassian/eggs/` and `tests/eggs/`.
  - Changed to `/eggs/` with explicit whitelists `!src/mcp_atlassian/eggs/` and `!tests/eggs/`.
