"""MCP tool registration and handlers for the eggs toolset."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from mcp_atlassian import __version__
from mcp_atlassian.eggs.actions import (
    ActionResult,
    Actions,
    EndDayEntry,
    get_components_summary,
)
from mcp_atlassian.eggs.agenda import (
    AgendaItem,
    Intent,
    build_agenda,
    intents_for,
    mentioned_keys,
)
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, load_config
from mcp_atlassian.eggs.gateway import JiraGateway
from mcp_atlassian.eggs.gateway_atlassian import AtlassianGateway
from mcp_atlassian.eggs.identity import Identity, IdentityCache, resolve_identity
from mcp_atlassian.eggs.plan import PlanError, dump_plan, parse_plan
from mcp_atlassian.eggs.plan_apply import apply_plan, export_epic
from mcp_atlassian.eggs.plan_validate import load_plan_context, validate_plan
from mcp_atlassian.eggs.report import (
    Snapshot,
    build_snapshot,
    compute_worklog_report,
    render_pending_approvals,
    render_report,
    render_workstream,
)
from mcp_atlassian.jira import JiraConfig, JiraFetcher
from mcp_atlassian.utils.decorators import handle_tool_errors
from mcp_atlassian.utils.toolsets import (
    ALL_TOOLSETS,
    JIRA_TOOLSETS,
    ToolsetDefinition,
)

logger = logging.getLogger(__name__)

READ_EGGS_TOOL_NAMES = frozenset(
    {
        "eggs_version",
        "eggs_rules",
        "eggs_whoami",
        "eggs_next",
        "eggs_workstream_status",
        "eggs_daily_report",
        "eggs_worklog_report",
        "eggs_pending_approvals",
        "eggs_plan_validate",
        "eggs_migrate_epic",
        "eggs_components",
    }
)

WRITE_EGGS_TOOL_NAMES = frozenset(
    {
        "eggs_schedule",
        "eggs_start",
        "eggs_log_work",
        "eggs_submit_qc",
        "eggs_finish",
        "eggs_qc_start",
        "eggs_qc_pass",
        "eggs_qc_fail",
        "eggs_block",
        "eggs_unblock",
        "eggs_request_reschedule",
        "eggs_flow_reschedule",
        "eggs_story_step",
        "eggs_uat",
        "eggs_decide_reschedule",
        "eggs_cancel",
        "eggs_end_day",
        "eggs_triage_open",
        "eggs_triage_close",
        "eggs_plan_apply",
        "eggs_create_issue",
        "eggs_create_component",
        "eggs_update_description",
    }
)

ALL_EGGS_TOOL_NAMES = READ_EGGS_TOOL_NAMES | WRITE_EGGS_TOOL_NAMES

DEPLOY_RAW_SAFE_WRITE_TOOLS = frozenset(
    {
        "jira_add_comment",
        "jira_edit_comment",
        "jira_add_watcher",
        "jira_remove_watcher",
        "jira_create_issue_link",
        "jira_create_remote_issue_link",
        "jira_remove_issue_link",
        "jira_link_to_epic",
    }
)


def _compute_deploy_raw_read_tools_and_toolsets() -> tuple[
    frozenset[str], tuple[str, ...]
]:
    from mcp_atlassian.servers.jira import jira_mcp

    lp = getattr(jira_mcp, "_local_provider", None)
    read_tools: set[str] = set()
    toolsets: set[str] = {"eggs"}

    if lp and hasattr(lp, "_components"):
        for k, v in lp._components.items():
            if not k.startswith("tool:"):
                continue
            tags = getattr(v, "tags", set()) or set()
            name = v.name if v.name.startswith("jira_") else f"jira_{v.name}"
            if "read" in tags and "cloud_only" not in tags:
                read_tools.add(name)
            if name in read_tools or name in DEPLOY_RAW_SAFE_WRITE_TOOLS:
                for tag in tags:
                    if tag.startswith("toolset:"):
                        toolsets.add(tag.removeprefix("toolset:"))

    return frozenset(read_tools), tuple(sorted(toolsets))


def __getattr__(name: str) -> Any:
    if name in (
        "DEPLOY_RAW_READ_TOOLS",
        "DEPLOY_TOOLSETS",
        "DEPLOY_ENABLED_TOOLS",
        "READ_ONLY_RAW_JIRA_TOOLS",
    ):
        raw_read, toolsets = _compute_deploy_raw_read_tools_and_toolsets()
        globals()["DEPLOY_RAW_READ_TOOLS"] = raw_read
        globals()["DEPLOY_TOOLSETS"] = toolsets
        globals()["READ_ONLY_RAW_JIRA_TOOLS"] = raw_read
        globals()["DEPLOY_ENABLED_TOOLS"] = tuple(
            sorted(
                raw_read
                | DEPLOY_RAW_SAFE_WRITE_TOOLS
                | {f"jira_{n}" for n in ALL_EGGS_TOOL_NAMES}
                | ALL_EGGS_TOOL_NAMES
            )
        )
        return globals()[name]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


EGGS_TOOLSET_NAME = "eggs"
EGGS_TOOLSET_DEFINITION = ToolsetDefinition(
    name=EGGS_TOOLSET_NAME,
    description="Eggstech Jira workflow and guardian tools",
    default=False,
)

_identity_cache = IdentityCache(ttl_seconds=600)
_roles_reader: JiraGateway | None = None
_roles_reader_lock = threading.Lock()


def clear_identity_cache() -> None:
    _identity_cache.clear()


def _ensure_toolset_registered() -> None:
    if EGGS_TOOLSET_NAME not in ALL_TOOLSETS:
        JIRA_TOOLSETS[EGGS_TOOLSET_NAME] = EGGS_TOOLSET_DEFINITION
        ALL_TOOLSETS[EGGS_TOOLSET_NAME] = EGGS_TOOLSET_DEFINITION


def get_roles_reader(cfg: EggsConfig | None = None) -> JiraGateway:
    """Return the singleton gateway for role reading via EGGS_BOT_TOKEN."""
    global _roles_reader
    if _roles_reader is None:
        with _roles_reader_lock:
            if _roles_reader is None:
                c = cfg or load_config()
                url = os.getenv("JIRA_URL", c.jira_url if c else None)
                token = os.getenv("EGGS_BOT_TOKEN")
                if not url:
                    raise ValueError("Thiếu cấu hình JIRA_URL để kết nối Jira.")
                if not token:
                    raise ValueError(
                        "Thiếu biến môi trường EGGS_BOT_TOKEN để đọc vai trò dự án."
                    )
                fetcher = JiraFetcher(
                    JiraConfig(url=url, auth_type="pat", personal_token=token)
                )
                _roles_reader = AtlassianGateway(fetcher.jira, cfg=c)
    return _roles_reader


def set_roles_reader(gateway: JiraGateway | None) -> None:
    global _roles_reader
    with _roles_reader_lock:
        _roles_reader = gateway


async def get_user_gateway(ctx: Context, *, write: bool = False) -> JiraGateway:
    """Build the JiraGateway from the per-request user's Jira client."""
    from mcp_atlassian.servers.dependencies import get_jira_fetcher

    if write:
        req_ctx = getattr(ctx, "request_context", None)
        request = getattr(req_ctx, "request", None) if req_ctx else None
        if request is not None:
            state = getattr(request, "state", None)
            has_token = bool(
                state is not None and getattr(state, "user_atlassian_token", None)
            )
            if not has_token:
                raise ValueError(
                    "Thao tác ghi yêu cầu token cá nhân (PAT) của người dùng "
                    "trong Authorization header, không được dùng thông tin đăng "
                    "nhập dùng chung."
                )

    cfg = await asyncio.to_thread(load_config)
    fetcher = await get_jira_fetcher(ctx)
    return AtlassianGateway(fetcher.jira, cfg=cfg)


def eggs_check_write_access(func: Any) -> Any:
    """Check write access safely even if request_context is None."""
    tool_name = func.__name__

    @handle_tool_errors
    @wraps(func)
    async def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
        req_ctx = getattr(ctx, "request_context", None)
        if req_ctx is not None:
            lifespan_ctx_dict = getattr(req_ctx, "lifespan_context", None)
            app_lifespan_ctx = (
                lifespan_ctx_dict.get("app_lifespan_context")
                if isinstance(lifespan_ctx_dict, dict)
                else None
            )
            if app_lifespan_ctx is not None and getattr(
                app_lifespan_ctx, "read_only", False
            ):
                msg = f"Cannot execute '{tool_name}': server is in read-only mode."
                raise ToolError(msg)
        if os.getenv("READ_ONLY_MODE", "").lower() in ("true", "1", "yes"):
            msg = f"Cannot execute '{tool_name}': server is in read-only mode."
            raise ToolError(msg)
        return await func(ctx, *args, **kwargs)

    return wrapper


def identity_to_dict(ident: Identity) -> dict[str, Any]:
    return {
        "username": ident.username,
        "display_name": ident.display_name,
        "roles": {k: sorted(v) for k, v in sorted(ident.roles.items())},
        "is_lead": ident.is_lead,
        "errors": sorted(ident.errors),
    }


def agenda_item_to_dict(it: AgendaItem) -> dict[str, Any]:
    return {
        "priority": it.priority,
        "key": it.key,
        "project": it.project,
        "title": it.title,
        "say": it.say,
        "actions": [
            {
                "tool": a.tool,
                "args": a.args,
                "missing": a.missing,
            }
            for a in it.actions
        ],
    }


def intent_to_dict(intent: Intent) -> dict[str, Any]:
    return {
        "name": intent.name,
        "roles": sorted(intent.roles),
        "examples": intent.examples,
        "tool": intent.tool,
        "required": intent.required,
    }


def action_result_to_dict(res: ActionResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": res.ok,
        "message": res.message,
        "changes": res.changes,
        "next_actions": res.next_actions,
        "next": agenda_item_to_dict(res.next) if res.next else None,
    }
    if res.code is not None:
        out["code"] = res.code
    if res.missing is not None:
        out["missing"] = res.missing
    return out


def _check_project_role(
    identity: Identity,
    project_key: str,
    required_roles: set[str],
    role_label: str,
) -> ActionResult | None:
    if identity.is_lead:
        return None
    pk = project_key.upper()
    user_roles = identity.roles.get(pk, set())
    if not user_roles:
        return ActionResult(
            ok=False,
            message=f"Bạn chưa có vai trò nào trong dự án {pk}.",
            changes=[],
            next_actions=[],
        )
    if not (user_roles & required_roles):
        return ActionResult(
            ok=False,
            message=(
                f"Bạn không có vai trò {role_label} trong dự án {pk} để "
                "thực hiện thao tác này."
            ),
            changes=[],
            next_actions=[],
        )
    return None


def _make_snapshot(
    gateway: JiraGateway,
    cfg: EggsConfig,
    cal: WorkCalendar,
    today: date | None = None,
    now: datetime | None = None,
    worklog_since: date | None = None,
) -> Snapshot:
    tz = ZoneInfo(cfg.timezone)
    if today is None:
        current_dt = now or datetime.now(tz)
        today = current_dt.astimezone(tz).date()
    since = datetime(today.year, today.month, today.day, 0, 0, tzinfo=tz)
    return build_snapshot(gateway, cfg, today, cal, since, worklog_since=worklog_since)


def _get_next_agenda_item(
    user_gateway: JiraGateway,
    identity: Identity,
    cfg: EggsConfig,
    cal: WorkCalendar,
    now: datetime,
) -> tuple[AgendaItem | None, bool]:
    try:
        tz = ZoneInfo(cfg.timezone)
        today = now.astimezone(tz).date()
        snapshot = _make_snapshot(user_gateway, cfg, cal, today=today, now=now)
        agenda = build_agenda(snapshot, identity, cfg, now, cal)
        return (agenda[0] if agenda else None), False
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not compute next agenda item: %s", e)
        return None, True


def _attach_next_agenda_item(
    res: ActionResult,
    user_gateway: JiraGateway,
    identity: Identity,
    cfg: EggsConfig,
    cal: WorkCalendar,
    now: datetime,
) -> ActionResult:
    next_it, failed = _get_next_agenda_item(user_gateway, identity, cfg, cal, now)
    new_message = res.message
    if failed:
        suffix = "Không lấy được việc tiếp theo."
        if new_message:
            new_message = f"{new_message} {suffix}"
        else:
            new_message = suffix
    return dataclasses.replace(res, message=new_message, next=next_it)


def _is_eggs_enabled() -> bool:
    val = os.getenv("EGGS_ENABLED", "").strip().lower()
    return val in {"1", "true", "yes"}


def register_eggs_tools(server: FastMCP, *, force: bool = False) -> None:
    """Register all 25 egg tools on the given FastMCP server."""
    if not force and not _is_eggs_enabled():
        return

    _ensure_toolset_registered()

    read_tags = {"jira", "read", f"toolset:{EGGS_TOOLSET_NAME}"}
    write_tags = {"jira", "write", f"toolset:{EGGS_TOOLSET_NAME}"}

    # -------------------------------------------------------------------------
    # READ TOOLS (7)
    # -------------------------------------------------------------------------

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Version", "readOnlyHint": True},
    )
    async def eggs_version(ctx: Context) -> str:
        """Kiểm tra phiên bản server EGGS Jira MCP và phiên bản kit tối thiểu."""
        cfg = await asyncio.to_thread(load_config)
        data = {
            "server": __version__,
            "kit_min_version": cfg.kit_min_version,
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Rules", "readOnlyHint": True},
    )
    async def eggs_rules(ctx: Context) -> str:
        """Xem tóm tắt quy trình và quy tắc làm việc EGGS trên Jira."""
        return (
            "# Quy tắc làm việc EGGS Jira Workflow\n\n"
            "1. **Mọi thao tác ghi qua `eggs_*`**: Không dùng tool Jira thô.\n"
            "2. **Xác nhận 2 bước**: Gọi tool với `dry_run=true` để kiểm tra trước.\n"
            "3. **Phân quyền theo vai trò (Project Role)**:\n"
            "   - **Dev**: schedule, start, log_work, submit_qc, block, unblock.\n"
            "   - **QC**: qc_start, qc_pass, qc_fail, triage_open, triage_close.\n"
            "   - **Lead-BA**: flow_reschedule, uat, decide_reschedule, cancel.\n"
            "   - **Mọi người**: log_work, block, unblock, end_day (thường sau "
            "17:00; sớm hơn vẫn được nhưng có cảnh báo).\n"
            "4. **Chốt chặn Lead-BA**: Duyệt dời lịch, ký UAT, dời hạn Story.\n"
            "5. **Người duyệt khác người đề xuất**: Không tự duyệt dời lịch của mình.\n"
            "6. **Remaining Estimate**: Jira tự trừ theo giờ đã log (về 0 khi "
            "log vượt). EGGS chỉ dùng Original Estimate.\n"
        )

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Who Am I", "readOnlyHint": True},
    )
    async def eggs_whoami(ctx: Context) -> str:
        """Xác định danh tính người dùng hiện tại và vai trò theo từng dự án."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=False)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )
        return json.dumps(identity_to_dict(ident), indent=2, ensure_ascii=False)

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Next", "readOnlyHint": True},
    )
    async def eggs_next(
        ctx: Context,
        text: Annotated[
            str | None,
            Field(
                description="Câu nói hoặc yêu cầu tự nhiên của người dùng",
                default=None,
            ),
        ] = None,
    ) -> str:
        """Cửa vào duy nhất: Trả về agenda việc cần làm và danh mục ý định."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=False)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        cal = WorkCalendar(cfg.holidays)
        now = datetime.now(ZoneInfo(cfg.timezone))
        today = now.astimezone(ZoneInfo(cfg.timezone)).date()

        snapshot = await asyncio.to_thread(
            _make_snapshot, user_gw, cfg, cal, today=today, now=now
        )
        agenda = await asyncio.to_thread(build_agenda, snapshot, ident, cfg, now, cal)

        mentioned: list[str] = []
        if text:
            active_projects = set(cfg.projects.keys())
            mentioned = mentioned_keys(text, active_projects)
            mentioned_items = [it for it in agenda if it.key in mentioned]
            other_items = [it for it in agenda if it.key not in mentioned]
            agenda = (mentioned_items + other_items)[:10]
        else:
            agenda = agenda[:10]

        intents = [intent_to_dict(i) for i in intents_for(ident)]

        if not ident.roles and not ident.is_lead:
            hint = "Bạn chưa có vai trò trong dự án nào, báo Lead."
            agenda = []
        else:
            hint = (
                "Hiện agenda dạng danh sách đánh số; với ý định của người dùng, "
                "chọn một intent, gọi tool với dry_run=true, xác nhận, rồi gọi thật."
            )

        data = {
            "me": identity_to_dict(ident),
            "agenda": [agenda_item_to_dict(it) for it in agenda],
            "mentioned": mentioned,
            "intents": intents,
            "hint": hint,
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Workstream Status", "readOnlyHint": True},
    )
    async def eggs_workstream_status(
        ctx: Context,
        epic: Annotated[str | None, Field(description="Mã Epic", default=None)] = None,
        project: Annotated[
            str | None, Field(description="Mã dự án", default=None)
        ] = None,
    ) -> str:
        """Xem bảng theo dõi tiến độ Actor của Epic hoặc dự án."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=False)
        cal = WorkCalendar(cfg.holidays)
        now = datetime.now(ZoneInfo(cfg.timezone))
        today = now.astimezone(ZoneInfo(cfg.timezone)).date()
        snapshot = await asyncio.to_thread(
            _make_snapshot, user_gw, cfg, cal, today=today, now=now
        )

        if epic:
            return await asyncio.to_thread(render_workstream, snapshot, epic)

        target_epics: list[str] = []
        for ek, iss in snapshot.issues.items():
            if iss.type == "epic":
                if project and iss.project != project:
                    continue
                target_epics.append(ek)

        if not target_epics:
            return (
                f"Không tìm thấy workstream nào trong dự án {project}."
                if project
                else "Không tìm thấy workstream nào."
            )

        rendered = await asyncio.to_thread(
            lambda: "\n\n".join(
                render_workstream(snapshot, ek) for ek in sorted(target_epics)
            )
        )
        return rendered

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Daily Report", "readOnlyHint": True},
    )
    async def eggs_daily_report(
        ctx: Context,
        project: Annotated[
            str | None, Field(description="Mã dự án cần lọc", default=None)
        ] = None,
    ) -> str:
        """Báo cáo tổng hợp tình hình công việc trong ngày."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=False)
        cal = WorkCalendar(cfg.holidays)
        now = datetime.now(ZoneInfo(cfg.timezone))
        today = now.astimezone(ZoneInfo(cfg.timezone)).date()
        snapshot = await asyncio.to_thread(
            _make_snapshot, user_gw, cfg, cal, today=today, now=now
        )
        return await asyncio.to_thread(render_report, snapshot, cfg, project=project)

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Worklog Report", "readOnlyHint": True},
    )
    async def eggs_worklog_report(
        ctx: Context,
        project: Annotated[
            str | None, Field(description="Mã dự án (RFID, HH...)", default=None)
        ] = None,
        person: Annotated[
            str | None, Field(description="Tên người cần xem worklog", default=None)
        ] = None,
        date_from: Annotated[
            str | None, Field(description="Ngày bắt đầu (YYYY-MM-DD)", default=None)
        ] = None,
        date_to: Annotated[
            str | None, Field(description="Ngày kết thúc (YYYY-MM-DD)", default=None)
        ] = None,
    ) -> str:
        """Báo cáo tổng hợp worklog theo người và theo dự án."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=False)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        cal = WorkCalendar(cfg.holidays)
        now = datetime.now(ZoneInfo(cfg.timezone))
        today = now.astimezone(ZoneInfo(cfg.timezone)).date()

        d_from: date | None = None
        if date_from:
            try:
                d_from = date.fromisoformat(date_from)
            except ValueError:
                return json.dumps(
                    {
                        "ok": False,
                        "message": "date_from không đúng định dạng YYYY-MM-DD.",
                        "range": {"from": date_from, "to": str(date_to)},
                        "people": [],
                        "idle_with_work_in_progress": [],
                    },
                    ensure_ascii=False,
                )

        d_to: date | None = None
        if date_to:
            try:
                d_to = date.fromisoformat(date_to)
            except ValueError:
                return json.dumps(
                    {
                        "ok": False,
                        "message": "date_to không đúng định dạng YYYY-MM-DD.",
                        "range": {"from": str(date_from), "to": date_to},
                        "people": [],
                        "idle_with_work_in_progress": [],
                    },
                    ensure_ascii=False,
                )

        eff_from = (
            d_from if d_from is not None else today - timedelta(days=today.weekday())
        )
        eff_to = d_to if d_to is not None else today

        if eff_from > eff_to:
            return json.dumps(
                {
                    "ok": False,
                    "message": "date_from không được lớn hơn date_to.",
                    "range": {"from": eff_from.isoformat(), "to": eff_to.isoformat()},
                    "people": [],
                    "idle_with_work_in_progress": [],
                },
                ensure_ascii=False,
            )

        if (eff_to - eff_from).days + 1 > 31:
            return json.dumps(
                {
                    "ok": False,
                    "message": "Khoảng thời gian không được vượt quá 31 ngày.",
                    "range": {"from": eff_from.isoformat(), "to": eff_to.isoformat()},
                    "people": [],
                    "idle_with_work_in_progress": [],
                },
                ensure_ascii=False,
            )

        snapshot = await asyncio.to_thread(
            _make_snapshot,
            user_gw,
            cfg,
            cal,
            today=today,
            now=now,
            worklog_since=eff_from,
        )
        report = await asyncio.to_thread(
            compute_worklog_report,
            snapshot,
            cfg,
            cal,
            ident,
            project=project,
            person=person,
            date_from=d_from,
            date_to=d_to,
            now=now,
        )
        return json.dumps(report, ensure_ascii=False)

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Pending Approvals", "readOnlyHint": True},
    )
    async def eggs_pending_approvals(
        ctx: Context,
        project: Annotated[
            str | None, Field(description="Mã dự án", default=None)
        ] = None,
    ) -> str:
        """Xem danh sách các yêu cầu dời lịch đang chờ duyệt."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=False)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )
        cal = WorkCalendar(cfg.holidays)
        now = datetime.now(ZoneInfo(cfg.timezone))
        today = now.astimezone(ZoneInfo(cfg.timezone)).date()
        snapshot = await asyncio.to_thread(
            _make_snapshot, user_gw, cfg, cal, today=today, now=now
        )
        return await asyncio.to_thread(
            render_pending_approvals, snapshot, ident.username, cfg
        )

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Plan Validate", "readOnlyHint": True},
    )
    async def eggs_plan_validate(
        ctx: Context,
        plan_yaml: Annotated[
            str, Field(description="Nội dung kế hoạch plan.yaml định dạng eggs-plan/v2")
        ],
    ) -> str:
        """Kiểm tra và chuẩn hóa file plan.yaml trước khi đưa lên Jira."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=False)
        now = datetime.now(ZoneInfo(cfg.timezone))
        today = now.astimezone(ZoneInfo(cfg.timezone)).date()

        try:
            plan = parse_plan(plan_yaml)
        except PlanError as err:
            out = {
                "ok": False,
                "errors": [{"path": err.path, "message": str(err)}],
                "warnings": [],
                "questions": [],
                "tree": "",
                "plan_yaml": plan_yaml,
            }
            return json.dumps(out, indent=2, ensure_ascii=False)

        ctx_plan = await asyncio.to_thread(load_plan_context, user_gw, cfg, plan, today)
        report = await asyncio.to_thread(validate_plan, plan, ctx_plan, cfg)
        errors = [
            {"path": f.path, "message": f.message}
            for f in report.findings
            if f.level == "error"
        ]
        warnings = [f.message for f in report.findings if f.level == "warning"]
        updated_yaml = dump_plan(report.plan)
        out = {
            "ok": report.ok,
            "errors": errors,
            "warnings": warnings,
            "questions": report.questions,
            "tree": report.tree,
            "plan_yaml": updated_yaml,
        }
        return json.dumps(out, indent=2, ensure_ascii=False)

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Migrate Epic", "readOnlyHint": True},
    )
    async def eggs_migrate_epic(
        ctx: Context,
        epic: Annotated[
            str,
            Field(description="Mã Epic cần chuyển đổi (ví dụ: RFID-532, HH-317)"),
        ],
    ) -> str:
        """Trích xuất Epic hiện có thành plan.yaml để chuyển đổi sang mô hình EGGS."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=False)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = epic.split("-")[0].upper()
        refusal = _check_project_role(ident, proj, {"lead_ba"}, "Lead-BA")
        if refusal:
            out = {
                "ok": False,
                "message": refusal.message,
                "plan_yaml": "",
                "stories": 0,
                "missing": {"actor": 0, "due": 0, "qc": 0},
                "other_issues": 0,
            }
            return json.dumps(out, indent=2, ensure_ascii=False)

        exported_plan = await asyncio.to_thread(export_epic, user_gw, cfg, epic)
        missing_actor = sum(1 for f in exported_plan.flows if not f.actor)
        missing_due = sum(1 for f in exported_plan.flows if not f.due)
        missing_qc = sum(1 for f in exported_plan.flows if not f.qc)
        plan_yaml = dump_plan(exported_plan)
        out = {
            "plan_yaml": plan_yaml,
            "stories": len(exported_plan.flows),
            "missing": {
                "actor": missing_actor,
                "due": missing_due,
                "qc": missing_qc,
            },
            "other_issues": len(exported_plan.other_issues),
        }
        return json.dumps(out, indent=2, ensure_ascii=False)

    @server.tool(
        tags=read_tags,
        annotations={"title": "EGGS Components", "readOnlyHint": True},
    )
    async def eggs_components(
        ctx: Context,
        project: Annotated[str, Field(description="Mã dự án (ví dụ EGGSBOX)")],
    ) -> str:
        """Liệt kê các Component trong dự án kèm số lượng Story đang mở."""
        user_gw = await get_user_gateway(ctx, write=False)
        return await asyncio.to_thread(get_components_summary, user_gw, project.upper())

    # -------------------------------------------------------------------------
    # WRITE TOOLS (19)
    # -------------------------------------------------------------------------

    @server.tool(tags=write_tags, annotations={"title": "EGGS Schedule"})
    @eggs_check_write_access
    async def eggs_schedule(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue cần lên lịch")],
        start: Annotated[str, Field(description="Ngày bắt đầu (YYYY-MM-DD)")],
        end: Annotated[str, Field(description="Ngày kết thúc (YYYY-MM-DD)")],
        estimate: Annotated[
            str, Field(description="Thời lượng ước tính (ví dụ '2d', '4h')")
        ],
        assignee: Annotated[
            str | None, Field(description="Người nhận việc", default=None)
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm không ghi", default=False)
        ] = False,
    ) -> str:
        """Nhận việc và lên lịch thực hiện cho phần việc (Dev Task/Bug)."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"dev"}, "Developer (dev)")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.schedule,
            key=key,
            start=start,
            end=end,
            estimate=estimate,
            assignee=assignee,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Start"})
    @eggs_check_write_access
    async def eggs_start(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue cần bắt đầu")],
        start: Annotated[
            str | None, Field(description="Ngày bắt đầu", default=None)
        ] = None,
        end: Annotated[
            str | None, Field(description="Ngày kết thúc", default=None)
        ] = None,
        estimate: Annotated[
            str | None, Field(description="Thời lượng ước tính", default=None)
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm không ghi", default=False)
        ] = False,
    ) -> str:
        """Bắt đầu thực hiện công việc (chuyển sang Dev In Progress)."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"dev"}, "Developer (dev)")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.start,
            key=key,
            start=start,
            end=end,
            estimate=estimate,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Log Work"})
    @eggs_check_write_access
    async def eggs_log_work(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue ghi giờ")],
        time_spent: Annotated[
            str, Field(description="Thời gian làm việc (ví dụ '4h', '1d')")
        ],
        note: Annotated[str, Field(description="Nội dung công việc đã làm")],
        started: Annotated[
            str | None,
            Field(
                description=(
                    "Ngày làm việc YYYY-MM-DD (ghi lúc 09:00) hoặc "
                    "YYYY-MM-DDTHH:MM; bỏ trống = bây giờ. Không nhận ngày tương lai"
                ),
                default=None,
            ),
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Ghi nhận thời gian làm việc vào issue."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(
            ident, proj, {"dev", "qc", "lead_ba"}, "Dev, QC hoặc Lead-BA"
        )
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.log_work,
            key=key,
            time_spent=time_spent,
            note=note,
            started=started,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Submit QC"})
    @eggs_check_write_access
    async def eggs_submit_qc(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue chuyển QC")],
        note: Annotated[
            str, Field(description="Ghi chú bàn giao kiểm thử", default="")
        ] = "",
        qc_owner: Annotated[
            str | None,
            Field(
                description=(
                    "Jira username của QC Owner (chọn cho Ad-hoc Task khi cần QC "
                    "và Bug stand-alone; Dev Task/Bug (sub) kế thừa từ parent)"
                ),
                default=None,
            ),
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Hoàn thành phần việc và chuyển sang hàng đợi kiểm thử (QC TO DO).

        Đối với Dev Task: Dev lựa chọn dùng eggs_finish (không cần QC riêng, QC
        kiểm thử ở cấp Story) hoặc eggs_submit_qc (khi thuộc Story). Dev Task thuộc
        Ad-hoc Task hoặc Bug stand-alone không qua QC riêng, chỉ dùng eggs_finish.
        QC Owner (qc_owner) được chọn cho Ad-hoc Task (khi cần QC) và Bug stand-alone
        (bắt buộc, có thể đã được điền sẵn từ triage); Dev Task/Bug (sub) kế thừa từ
        Story hoặc Ad-hoc Task cha.
        Bug (sub dưới Story hoặc Ad-hoc Task, và Bug stand-alone) luôn qua QC bằng
        eggs_submit_qc (không có eggs_finish).
        Ad-hoc Task và Bug stand-alone chỉ nộp QC khi mọi sub-task đã hoàn thành.
        """
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"dev"}, "Developer (dev)")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.submit_qc,
            key=key,
            note=note,
            qc_owner=qc_owner,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Finish"})
    @eggs_check_write_access
    async def eggs_finish(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue hoàn thành")],
        note: Annotated[str, Field(description="Ghi chú hoàn thành", default="")] = "",
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Hoàn thành trực tiếp việc đang làm (Dev In Progress -> Done).

        Đối với Dev Task: Dev lựa chọn dùng eggs_finish (không cần QC riêng, QC
        kiểm thử ở cấp Story) hoặc eggs_submit_qc (khi thuộc Story). Dev Task thuộc
        Ad-hoc Task hoặc Bug stand-alone chỉ dùng eggs_finish. Đối với Ad-hoc Task: Dev
        lựa chọn dùng eggs_finish nếu không cần QC, hoặc eggs_submit_qc kèm qc_owner
        nếu cần QC. Bug (sub và stand-alone) không có finish, luôn qua QC
        (eggs_submit_qc).
        """
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"dev"}, "Developer (dev)")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.finish, key=key, note=note, dry_run=dry_run
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS QC Start"})
    @eggs_check_write_access
    async def eggs_qc_start(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue hoặc Story bắt đầu kiểm thử")],
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Bắt đầu kiểm thử phần việc hoặc luồng (Test In Progress)."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"qc"}, "QC (Tester)")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(actions.qc_start, key=key, dry_run=dry_run)
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS QC Pass"})
    @eggs_check_write_access
    async def eggs_qc_pass(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue hoặc Story đã kiểm thử đạt")],
        time_spent: Annotated[
            str | None,
            Field(description="Thời gian kiểm thử (nếu log work)", default=None),
        ] = None,
        note: Annotated[
            str, Field(description="Ghi chú kết quả kiểm thử", default="")
        ] = "",
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Xác nhận kiểm thử đạt (chuyển Done hoặc Ready For UAT)."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"qc"}, "QC (Tester)")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.qc_pass,
            key=key,
            time_spent=time_spent,
            note=note,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS QC Fail"})
    @eggs_check_write_access
    async def eggs_qc_fail(
        ctx: Context,
        key: Annotated[
            str, Field(description="Mã issue hoặc Story kiểm thử không đạt")
        ],
        bugs: Annotated[
            list[Any] | None,
            Field(description="Danh sách Bug cần tạo kèm", default=None),
        ] = None,
        time_spent: Annotated[
            str | None,
            Field(description="Thời gian làm việc cần log", default=None),
        ] = None,
        reason: Annotated[str, Field(description="Lý do không đạt", default="")] = "",
        note: Annotated[
            str, Field(description="Lý do/ghi chú không đạt", default="")
        ] = "",
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Xác nhận kiểm thử không đạt và tạo bug sub-task trả lại Dev."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"qc"}, "QC (Tester)")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        actual_reason = note or reason
        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.qc_fail,
            key=key,
            time_spent=time_spent,
            reason=actual_reason,
            bugs=bugs or [],
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Block"})
    @eggs_check_write_access
    async def eggs_block(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue bị tắc nghẽn")],
        reason: Annotated[str, Field(description="Lý do tắc nghẽn")],
        blocked_by: Annotated[
            str,
            Field(description="Bên gây tắc nghẽn (Nội bộ / Khách hàng / Bên thứ ba)"),
        ],
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Báo tắc nghẽn và cắm cờ Flagged trên issue."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(
            ident, proj, {"dev", "qc", "lead_ba"}, "Dev, QC hoặc Lead-BA"
        )
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.block,
            key=key,
            reason=reason,
            blocked_by=blocked_by,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Unblock"})
    @eggs_check_write_access
    async def eggs_unblock(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue được gỡ tắc nghẽn")],
        note: Annotated[str, Field(description="Ghi chú khi gỡ cờ", default="")] = "",
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Gỡ cờ tắc nghẽn trên issue sau khi vấn đề được giải quyết."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(
            ident, proj, {"dev", "qc", "lead_ba"}, "Dev, QC hoặc Lead-BA"
        )
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.unblock, key=key, note=note, dry_run=dry_run
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Request Reschedule"})
    @eggs_check_write_access
    async def eggs_request_reschedule(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue cần dời lịch")],
        proposed_end: Annotated[
            str | None,
            Field(
                description="Ngày kết thúc đề xuất mới (YYYY-MM-DD)",
                default=None,
            ),
        ] = None,
        reason: Annotated[
            str, Field(description="Lý do đề xuất dời lịch", default="")
        ] = "",
        interrupted_by: Annotated[
            str | None,
            Field(description="Mã issue chen ngang gây dời lịch", default=None),
        ] = None,
        *,
        no_impact: Annotated[
            bool,
            Field(
                description="Xác nhận việc không ảnh hưởng lịch tổng thể",
                default=False,
            ),
        ] = False,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Đề xuất dời lịch kết thúc phần việc hoặc xác nhận không ảnh hưởng."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        iss = await asyncio.to_thread(actions._get_issue, key)
        proj = iss.project

        # Permission check
        caller_user = (ident.username or "").lower()
        assignee_user = (iss.assignee or "").lower()
        if no_impact:
            if caller_user != assignee_user:
                refusal = ActionResult(
                    ok=False,
                    message=(
                        f"Chỉ người được giao việc (assignee: {iss.assignee}) "
                        "mới có thể xác nhận không ảnh hưởng."
                    ),
                    changes=[],
                    next_actions=[],
                )
                return json.dumps(
                    action_result_to_dict(refusal), indent=2, ensure_ascii=False
                )
            res = await asyncio.to_thread(
                actions.confirm_no_impact,
                key=key,
                note=reason,
                dry_run=dry_run,
            )
        else:
            is_assignee = caller_user == assignee_user
            is_lead_ba = "lead_ba" in ident.roles.get(proj, set())
            if not (is_assignee or is_lead_ba or ident.is_lead):
                refusal = ActionResult(
                    ok=False,
                    message=(
                        f"Bạn không phải người thực hiện ({iss.assignee}) "
                        f"hoặc Lead-BA trong dự án {proj}."
                    ),
                    changes=[],
                    next_actions=[],
                )
                return json.dumps(
                    action_result_to_dict(refusal), indent=2, ensure_ascii=False
                )
            res = await asyncio.to_thread(
                actions.request_reschedule,
                key=key,
                proposed_end=proposed_end or "",
                reason=reason,
                interrupted_by=interrupted_by,
                dry_run=dry_run,
            )

        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Flow Reschedule"})
    @eggs_check_write_access
    async def eggs_flow_reschedule(
        ctx: Context,
        story_key: Annotated[str, Field(description="Mã Story cần dời Due Date")],
        new_due: Annotated[str, Field(description="Hạn bàn giao mới (YYYY-MM-DD)")],
        reason: Annotated[str, Field(description="Lý do điều chỉnh hạn")],
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Lead-BA điều chỉnh hạn bàn giao (Due Date) cho Story cấp luồng."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = story_key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"lead_ba"}, "Lead-BA")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        new_due_date = (
            date.fromisoformat(new_due) if isinstance(new_due, str) else new_due
        )
        res = await asyncio.to_thread(
            actions.flow_reschedule,
            story_key=story_key,
            new_due=new_due_date,
            reason=reason,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Story Step"})
    @eggs_check_write_access
    async def eggs_story_step(
        ctx: Context,
        key: Annotated[str, Field(description="Mã Story hoặc Epic")],
        step: Annotated[
            str,
            Field(
                description=(
                    "Bước nghiệp vụ BA: analysis_done, reopen_analysis, "
                    "ready_for_dev, close_epic"
                )
            ),
        ],
        comment: Annotated[
            str | None,
            Field(description="Ghi chú kèm theo bước", default=None),
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Thực hiện bước nghiệp vụ BA cho Story hoặc đóng Epic."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.story_step,
            key=key,
            step=step,
            comment=comment,
            dry_run=dry_run,
            identity=ident,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS UAT"})
    @eggs_check_write_access
    async def eggs_uat(
        ctx: Context,
        epic: Annotated[str, Field(description="Mã Epic")],
        actor: Annotated[str, Field(description="Mã Actor (Component)")],
        action: Annotated[
            str,
            Field(description="Hành động UAT: 'start', 'signoff', hoặc 'fail'"),
        ],
        note: Annotated[str, Field(description="Ghi chú UAT", default="")] = "",
        bugs: Annotated[
            list[dict[str, Any]] | None,
            Field(description="Danh sách bug khi UAT fail", default=None),
        ] = None,
        stories: Annotated[
            list[str] | None,
            Field(description="Danh sách Story chỉ định (nếu có)", default=None),
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Quản lý tiến trình UAT theo nhóm Actor trong Epic."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = epic.split("-")[0]
        refusal = _check_project_role(ident, proj, {"lead_ba"}, "Lead-BA")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.uat,
            epic=epic,
            actor=actor,
            action=action,
            note=note,
            bugs=bugs,
            stories=stories,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Decide Reschedule"})
    @eggs_check_write_access
    async def eggs_decide_reschedule(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue chờ duyệt dời lịch")],
        reason: Annotated[
            str, Field(description="Lý do duyệt hoặc từ chối", default="")
        ] = "",
        new_end: Annotated[
            str | None,
            Field(description="Ngày kết thúc duyệt mới (YYYY-MM-DD)", default=None),
        ] = None,
        *,
        approved: Annotated[
            bool, Field(description="Đồng ý duyệt dời lịch hay từ chối")
        ],
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Lead-BA quyết định duyệt hoặc từ chối đề xuất dời lịch."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"lead_ba"}, "Lead-BA")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        new_end_date = (
            date.fromisoformat(new_end)
            if isinstance(new_end, str) and new_end
            else None
        )
        res = await asyncio.to_thread(
            actions.decide_reschedule,
            key=key,
            approved=approved,
            reason=reason,
            new_end=new_end_date,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Cancel"})
    @eggs_check_write_access
    async def eggs_cancel(
        ctx: Context,
        key: Annotated[str, Field(description="Mã issue cần hủy")],
        reason: Annotated[str, Field(description="Lý do hủy công việc")],
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Hủy phần việc hoặc Story (chuyển Cancelled kèm resolution Won't Do)."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"lead_ba"}, "Lead-BA")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.cancel, key=key, reason=reason, dry_run=dry_run
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS End Day"})
    @eggs_check_write_access
    async def eggs_end_day(
        ctx: Context,
        entries: Annotated[
            list[Any],
            Field(
                description=(
                    "Danh sách nhật ký ghi giờ cuối ngày: "
                    "[{'key', 'time_spent', 'note', 'move_to'}]"
                )
            ),
        ],
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Cập nhật ghi nhận giờ và trạng thái công việc kết thúc ngày làm việc."""
        if not entries:
            refusal = ActionResult(
                ok=False,
                message="Danh sách công việc cuối ngày không được để trống.",
                changes=[],
                next_actions=[],
            )
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        allowed_entry_fields = {f.name for f in dataclasses.fields(EndDayEntry)}
        for idx, e in enumerate(entries):
            if not isinstance(e, dict):
                refusal = ActionResult(
                    ok=False,
                    message=(
                        f"Mục {idx}: công việc cuối ngày phải là một đối tượng (dict)."
                    ),
                    changes=[],
                    next_actions=[],
                )
                return json.dumps(
                    action_result_to_dict(refusal), indent=2, ensure_ascii=False
                )

            unknown_keys = sorted(set(e.keys()) - allowed_entry_fields)
            if unknown_keys:
                bad_fields = ", ".join(unknown_keys)
                refusal = ActionResult(
                    ok=False,
                    message=f"Mục {idx}: chứa trường không hợp lệ: {bad_fields}.",
                    changes=[],
                    next_actions=[],
                )
                return json.dumps(
                    action_result_to_dict(refusal), indent=2, ensure_ascii=False
                )

            if not e.get("key") or not str(e["key"]).strip():
                refusal = ActionResult(
                    ok=False,
                    message=f"Mục {idx}: thiếu trường bắt buộc 'key'.",
                    changes=[],
                    next_actions=[],
                )
                return json.dumps(
                    action_result_to_dict(refusal), indent=2, ensure_ascii=False
                )

            if not e.get("time_spent") or not str(e["time_spent"]).strip():
                refusal = ActionResult(
                    ok=False,
                    message=f"Mục {idx}: thiếu trường bắt buộc 'time_spent'.",
                    changes=[],
                    next_actions=[],
                )
                return json.dumps(
                    action_result_to_dict(refusal), indent=2, ensure_ascii=False
                )

            move_to = e.get("move_to")
            if move_to not in (None, "ready_for_qc", "done"):
                refusal = ActionResult(
                    ok=False,
                    message=(
                        f"Mục {idx}: giá trị 'move_to' không hợp lệ: {move_to}. "
                        "Chỉ chấp nhận 'ready_for_qc', 'done' hoặc để trống."
                    ),
                    changes=[],
                    next_actions=[],
                )
                return json.dumps(
                    action_result_to_dict(refusal), indent=2, ensure_ascii=False
                )

        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        for e in entries:
            pk = str(e["key"]).split("-")[0]
            refusal = _check_project_role(
                ident, pk, {"dev", "qc", "lead_ba"}, "Dev, QC hoặc Lead-BA"
            )
            if refusal:
                return json.dumps(
                    action_result_to_dict(refusal), indent=2, ensure_ascii=False
                )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        typed_entries = [
            EndDayEntry(
                key=str(e["key"]).strip(),
                time_spent=str(e["time_spent"]).strip(),
                note=str(e.get("note", "") or ""),
                move_to=e.get("move_to"),
            )
            for e in entries
        ]
        res = await asyncio.to_thread(
            actions.end_day,
            entries=typed_entries,
            dry_run=dry_run,
            now=datetime.now(ZoneInfo(cfg.timezone)),
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Triage Open"})
    @eggs_check_write_access
    async def eggs_triage_open(
        ctx: Context,
        project: Annotated[str, Field(description="Mã dự án tiếp nhận")],
        text: Annotated[str, Field(description="Nội dung phản ánh")],
        source_url: Annotated[
            str,
            Field(description="Đường link nguồn phản ánh (tin nhắn, tài liệu)"),
        ],
        reported_by: Annotated[str, Field(description="Tên người phản ánh")],
        reported_at: Annotated[
            str | None, Field(description="Thời điểm phản ánh", default=None)
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Ghi nhận User Report mới từ kênh hỗ trợ vào Epic OPS tháng của dự án."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        refusal = _check_project_role(
            ident, project, {"qc", "lead_ba"}, "QC hoặc Lead-BA"
        )
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.triage_open,
            project=project,
            text=text,
            source_url=source_url,
            reported_by=reported_by,
            reported_at=reported_at,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Triage Close"})
    @eggs_check_write_access
    async def eggs_triage_close(
        ctx: Context,
        key: Annotated[str, Field(description="Mã User Report cần đóng")],
        verdict: Annotated[
            str,
            Field(
                description=(
                    "Kết luận: 'is_bug_flow', 'is_bug_ops', "
                    "'is_known_issue', hoặc 'not_a_bug'"
                )
            ),
        ],
        note: Annotated[str, Field(description="Ghi chú kết luận phản ánh")],
        time_spent: Annotated[str, Field(description="Thời gian phân tích phản ánh")],
        bug_target: Annotated[
            str | None,
            Field(
                description="Mã Story hoặc Ad-hoc Task cho is_bug_flow",
                default=None,
            ),
        ] = None,
        link_key: Annotated[
            str | None,
            Field(
                description="Mã issue liên kết cho is_known_issue",
                default=None,
            ),
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm", default=False)
        ] = False,
    ) -> str:
        """Kết luận phân tích User Report và tạo/liên kết công việc xử lý."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        proj = key.split("-")[0]
        refusal = _check_project_role(ident, proj, {"qc", "lead_ba"}, "QC hoặc Lead-BA")
        if refusal:
            return json.dumps(
                action_result_to_dict(refusal), indent=2, ensure_ascii=False
            )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        res = await asyncio.to_thread(
            actions.triage_close,
            key=key,
            verdict=verdict,
            note=note,
            time_spent=time_spent,
            bug_target=bug_target,
            link_key=link_key,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Plan Apply"})
    @eggs_check_write_access
    async def eggs_plan_apply(
        ctx: Context,
        plan_yaml: Annotated[
            str, Field(description="Nội dung kế hoạch plan.yaml định dạng eggs-plan/v2")
        ],
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm không ghi", default=False)
        ] = False,
    ) -> str:
        """Tạo hoặc cập nhật Epic, Story, Dev Task theo kế hoạch plan.yaml."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        try:
            plan = parse_plan(plan_yaml)
        except PlanError as err:
            out = {
                "ok": False,
                "message": f"Lỗi cú pháp kế hoạch: {err}",
                "changes": [],
                "created": [],
                "updated": [],
                "warnings": [f"{err.path}: {err}"],
                "plan_yaml": plan_yaml,
            }
            return json.dumps(out, indent=2, ensure_ascii=False)

        proj = plan.project.upper()
        refusal = _check_project_role(ident, proj, {"lead_ba"}, "Lead-BA")
        if refusal:
            out = {
                "ok": False,
                "message": refusal.message,
                "changes": [],
                "created": [],
                "updated": [],
                "warnings": [],
                "plan_yaml": plan_yaml,
            }
            return json.dumps(out, indent=2, ensure_ascii=False)

        now = datetime.now(ZoneInfo(cfg.timezone))
        today = now.astimezone(ZoneInfo(cfg.timezone)).date()
        ctx_plan = await asyncio.to_thread(load_plan_context, user_gw, cfg, plan, today)
        res = await asyncio.to_thread(
            apply_plan, user_gw, cfg, plan, ctx_plan, dry_run=dry_run
        )
        out = {
            "ok": res.ok,
            "message": res.message,
            "changes": res.changes,
            "created": res.created,
            "updated": res.updated,
            "warnings": res.warnings,
            "plan_yaml": dump_plan(res.plan),
        }
        return json.dumps(out, indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Create Issue"})
    @eggs_check_write_access
    async def eggs_create_issue(
        ctx: Context,
        project: Annotated[str, Field(description="Mã dự án (ví dụ 'RFID')")],
        issue_type: Annotated[
            str,
            Field(description="Loại issue: 'bug', 'story', 'adhoc', hoặc 'bug_sub'"),
        ],
        summary: Annotated[str, Field(description="Tiêu đề tóm tắt issue")],
        description: Annotated[
            str | None, Field(description="Mô tả chi tiết", default=None)
        ] = None,
        related_to: Annotated[
            str | None,
            Field(
                description=(
                    "Mã issue liên quan cùng dự án để tạo liên kết Relates "
                    "(assignee mặc định theo issue này nếu không chỉ định)"
                ),
                default=None,
            ),
        ] = None,
        epic: Annotated[
            str | None,
            Field(
                description=(
                    "Mã Epic gắn kèm (bắt buộc với Story, tùy chọn với Ad-hoc Task)"
                ),
                default=None,
            ),
        ] = None,
        component: Annotated[
            str | None,
            Field(
                description="Tên Component (Actor) có sẵn trong dự án",
                default=None,
            ),
        ] = None,
        assignee: Annotated[
            str | None,
            Field(
                description=(
                    "Username người nhận việc. Bỏ trống: Dev tạo bug/adhoc và BA "
                    "tạo Story thì giao cho chính người tạo; còn lại phải nêu"
                ),
                default=None,
            ),
        ] = None,
        qc_owner: Annotated[
            str | None,
            Field(
                description=(
                    "Username QC Owner (mặc định là người tạo nếu có vai trò qc, "
                    "bắt buộc với Bug stand-alone)"
                ),
                default=None,
            ),
        ] = None,
        priority: Annotated[
            str | None,
            Field(description="Độ ưu tiên của issue", default=None),
        ] = None,
        parent: Annotated[
            str | None,
            Field(
                description="Mã Story hoặc Ad-hoc Task cha (dành riêng cho bug_sub)",
                default=None,
            ),
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm không ghi", default=False)
        ] = False,
    ) -> str:
        """Tạo mới Bug stand-alone, Story hoặc Ad-hoc Task trong dự án."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        creator_roles = ident.roles.get(project.upper(), set())
        res = await asyncio.to_thread(
            actions.create_issue,
            project=project.upper(),
            issue_type=issue_type,
            summary=summary,
            description=description,
            related_to=related_to,
            epic=epic,
            component=component,
            assignee=assignee,
            qc_owner=qc_owner,
            priority=priority,
            creator=ident.username,
            creator_roles=creator_roles,
            is_lead=ident.is_lead,
            parent=parent,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Create Component"})
    @eggs_check_write_access
    async def eggs_create_component(
        ctx: Context,
        project: Annotated[str, Field(description="Mã dự án (ví dụ EGGSBOX)")],
        code: Annotated[
            str,
            Field(
                description="Mã Actor/Component (2-5 chữ cái in hoa, ví dụ KT, CH, KH)"
            ),
        ],
        name: Annotated[
            str,
            Field(description="Tên Actor/Component (ví dụ KẾ TOÁN, CỬA HÀNG)"),
        ],
        description: Annotated[
            str | None,
            Field(description="Mô tả Component", default=None),
        ] = None,
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm không ghi", default=False)
        ] = False,
    ) -> str:
        """Tạo mới Component (Actor) cho dự án."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal, bot_gateway=roles_gw)
        creator_roles = ident.roles.get(project.upper(), set())
        res = await asyncio.to_thread(
            actions.create_component,
            project=project.upper(),
            code=code,
            name=name,
            description=description,
            creator=ident.username,
            creator_roles=creator_roles,
            is_lead=ident.is_lead,
            bot_gateway=roles_gw,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)

    @server.tool(tags=write_tags, annotations={"title": "EGGS Update Description"})
    @eggs_check_write_access
    async def eggs_update_description(
        ctx: Context,
        key: Annotated[
            str,
            Field(description="Mã issue cần cập nhật mô tả (ví dụ EGGSBOX-38)"),
        ],
        text: Annotated[str, Field(description="Nội dung mô tả mới")],
        mode: Annotated[
            Literal["replace", "append", "ac"],
            Field(
                description=(
                    "Chế độ cập nhật: replace (thay thế toàn bộ), "
                    "append (nối thêm vào cuối), "
                    "ac (chỉ sửa section Acceptance Criteria)"
                ),
                default="replace",
            ),
        ] = "replace",
        *,
        dry_run: Annotated[
            bool, Field(description="Chạy thử nghiệm không ghi", default=False)
        ] = False,
    ) -> str:
        """Cập nhật phần mô tả (description) của issue."""
        cfg = await asyncio.to_thread(load_config)
        user_gw = await get_user_gateway(ctx, write=True)
        roles_gw = await asyncio.to_thread(get_roles_reader, cfg)
        ident = await asyncio.to_thread(
            resolve_identity, user_gw, roles_gw, cfg, _identity_cache
        )

        cal = WorkCalendar(cfg.holidays)
        actions = Actions(user_gw, cfg, cal)
        proj = key.split("-")[0].upper()
        user_roles = ident.roles.get(proj, set())
        res = await asyncio.to_thread(
            actions.update_description,
            key=key,
            text=text,
            mode=mode,
            caller=ident.username,
            caller_roles=user_roles,
            is_lead=ident.is_lead,
            dry_run=dry_run,
        )
        if res.ok and not dry_run:
            now = datetime.now(ZoneInfo(cfg.timezone))
            res = await asyncio.to_thread(
                _attach_next_agenda_item, res, user_gw, ident, cfg, cal, now
            )
        return json.dumps(action_result_to_dict(res), indent=2, ensure_ascii=False)
