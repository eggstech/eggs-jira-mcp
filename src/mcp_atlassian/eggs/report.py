from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import WorkCalendar
from .config import EggsConfig
from .escalation import Escalation, escalate
from .forecast import EpicMetrics, FlowMetrics, epic_metrics, flow_metrics
from .gateway import JiraGateway
from .health import compute_flags, detect_interruptions, people_not_updated
from .identity import Identity
from .mapping import issue_from_json
from .model import Issue, Worklog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    issues: dict[str, Issue]
    flags: dict[str, frozenset[str]]
    escalations: dict[str, Escalation]
    flows: dict[str, FlowMetrics]
    epics: dict[str, EpicMetrics]
    not_updated: list[str]
    today: date
    worklogs: Sequence[Worklog] = ()


def build_snapshot(
    gateway: JiraGateway,
    cfg: EggsConfig,
    today: date,
    cal: WorkCalendar,
    since: datetime,
    worklog_since: date | None = None,
) -> Snapshot:
    """Fetch issues and worklogs, run all domain engines, and produce Snapshot."""
    # Active projects (non-frozen)
    active_projects = [k for k, p in cfg.projects.items() if not p.frozen]
    if active_projects:
        proj_list = ", ".join(sorted(active_projects))
        jql = (
            f"project in ({proj_list}) AND (statusCategory != Done OR resolved >= -7d)"
        )
    else:
        jql = "statusCategory != Done OR resolved >= -7d"

    fields = list(
        dict.fromkeys(
            [
                "summary",
                "project",
                "issuetype",
                "status",
                "assignee",
                "created",
                "duedate",
                "timeoriginalestimate",
                "aggregatetimeoriginalestimate",
                "labels",
                "parent",
                "components",
                "resolution",
            ]
            + [f for f in cfg.fields.values() if f]
        )
    )

    raw_issues = gateway.search(jql, fields=fields, expand_changelog=True)
    issues: dict[str, Issue] = {}
    for raw in raw_issues:
        iss = issue_from_json(raw, cfg)
        issues[iss.key] = iss

    # Fill sub-task epic_key from parent
    for key, iss in list(issues.items()):
        if iss.epic_key is None and iss.parent_key and iss.parent_key in issues:
            parent = issues[iss.parent_key]
            if parent.epic_key:
                issues[key] = replace(iss, epic_key=parent.epic_key)

    # Worklogs for the last 10 working days or custom window covering date_from
    if worklog_since is not None:
        since_wl_date = worklog_since
    else:
        since_wl_date = cal.add_working_days(today, -10)
    since_wl = datetime(
        since_wl_date.year,
        since_wl_date.month,
        since_wl_date.day,
        0,
        0,
        tzinfo=ZoneInfo(cfg.timezone),
    )
    worklogs = gateway.worklogs_since(since_wl)

    # Health flags & interruptions
    interruptions = detect_interruptions(
        list(issues.values()), since=since, today=today, cal=cal, cfg=cfg
    )
    stories = {k: iss for k, iss in issues.items() if iss.type == "story"}
    flags = compute_flags(
        list(issues.values()),
        worklogs=worklogs,
        stories=stories,
        today=today,
        cal=cal,
        cfg=cfg,
        interruptions=interruptions,
    )

    # Flow metrics
    children_by_parent: dict[str, list[Issue]] = {}
    for iss in issues.values():
        if iss.parent_key:
            children_by_parent.setdefault(iss.parent_key, []).append(iss)

    flows: dict[str, FlowMetrics] = {}
    for story_key, story in stories.items():
        fm = flow_metrics(
            story, children_by_parent.get(story_key, []), cal=cal, cfg=cfg
        )
        flows[story_key] = fm
        if fm.at_risk:
            flags[story_key] = flags.get(story_key, frozenset()) | {"AT_RISK"}

    # Escalations
    escalations: dict[str, Escalation] = {}
    for key, iss in issues.items():
        item_flags = flags.get(key, frozenset())
        if item_flags:
            parent_issue = issues.get(iss.parent_key) if iss.parent_key else None
            epic = issues.get(iss.epic_key) if iss.epic_key else None
            esc = escalate(
                iss,
                item_flags,
                story=parent_issue,
                epic=epic,
                today=today,
                cal=cal,
                cfg=cfg,
            )
            if esc is not None:
                escalations[key] = esc

    # Epic metrics
    epics: dict[str, EpicMetrics] = {}
    epic_issues = [iss for iss in issues.values() if iss.type == "epic"]
    for epic in epic_issues:
        epic_children = [iss for iss in issues.values() if iss.epic_key == epic.key]
        epic_stories = [iss for iss in epic_children if iss.type == "story"]
        other_items = [iss for iss in epic_children if iss.type in {"adhoc", "bug"}]
        em = epic_metrics(
            epic=epic,
            stories=epic_stories,
            flow=flows,
            other_items=other_items,
            escalations=escalations,
            cal=cal,
            children=epic_children,
        )
        epics[epic.key] = em

    not_updated = [
        u
        for u in people_not_updated(
            list(issues.values()), worklogs=worklogs, today=today, cal=cal
        )
        if u != cfg.bot_user
    ]

    return Snapshot(
        issues=issues,
        flags=flags,
        escalations=escalations,
        flows=flows,
        epics=epics,
        not_updated=not_updated,
        today=today,
        worklogs=worklogs,
    )


def _render_section(
    title: str,
    lines: list[str],
    max_lines: int,
) -> list[str]:
    """Helper to render a titled section with truncation."""
    if not lines:
        return []
    res = [title]
    if len(lines) <= max_lines:
        res.extend(lines)
    else:
        res.extend(lines[:max_lines])
        res.append(f"… và {len(lines) - max_lines} mục khác")
    return res


def render_report(
    snapshot: Snapshot,
    cfg: EggsConfig,
    project: str | None = None,
    max_lines: int = 10,
) -> str:
    """Render the daily report string with sections in exact order."""
    cal = WorkCalendar(cfg.holidays)
    base_url = cfg.jira_url.rstrip("/")

    def matches_project(iss: Issue) -> bool:
        return project is None or iss.project == project

    sections: list[list[str]] = []

    # 1. 🔴 Leo thang (mức 2)
    s1_lines: list[str] = []
    for key, esc in sorted(snapshot.escalations.items()):
        if esc.level >= 2:
            iss = snapshot.issues.get(key)
            if iss and matches_project(iss):
                item_flags = ", ".join(sorted(snapshot.flags.get(key, [])))
                owner_str = f" - {esc.owner}" if esc.owner else ""
                url = f"{base_url}/browse/{key}"
                s1_lines.append(f"- {url} | {iss.summary} ({item_flags}){owner_str}")
    s1 = _render_section("🔴 Leo thang", s1_lines, max_lines)
    if s1:
        sections.append(s1)

    # 2. 🟠 Workstream lệch cam kết
    s2_lines: list[str] = []
    on_track_count = 0
    ops_prefix = cfg.ops_epic_prefix

    for epic_key, em in sorted(snapshot.epics.items()):
        epic = snapshot.issues.get(epic_key)
        if not epic or not matches_project(epic):
            continue

        # Exclude [OPS] epics from section 2
        is_ops = bool(ops_prefix and epic.summary.startswith(ops_prefix))
        if is_ops:
            continue

        is_slipped = (em.slip_days is not None and em.slip_days > 0) or em.unknown_eta
        if is_slipped:
            slip_str = (
                "ETA không xác định" if em.unknown_eta else f"Trễ {em.slip_days} ngày"
            )
            due_str = epic.due_date.isoformat() if epic.due_date else "N/A"
            fc_str = em.forecast_end.isoformat() if em.forecast_end else "N/A"
            epic_line = (
                f"- {base_url}/browse/{epic.key} | {epic.summary} "
                f"(Due: {due_str} · Dự báo: {fc_str} · {slip_str})"
            )

            # Actor line (unless legacy-actor-model)
            if "legacy-actor-model" in epic.labels:
                s2_lines.append(epic_line)
            else:
                actor_segments: list[str] = []
                for act in em.actors:
                    code = act.actor_code or act.actor
                    if act.ready_for_uat == act.total and act.total > 0:
                        actor_segments.append(
                            f"{code} {act.ready_for_uat}/{act.total} sẵn sàng UAT"
                        )
                    else:
                        seg = f"{code} {act.ready_for_uat}/{act.total}"
                        if act.at_risk > 0:
                            seg += f" (AT_RISK {act.at_risk})"
                        actor_segments.append(seg)
                if actor_segments:
                    actor_line = "  " + " · ".join(actor_segments)
                    s2_lines.append(f"{epic_line}\n{actor_line}")
                else:
                    s2_lines.append(epic_line)
        elif not epic.is_resolved:
            on_track_count += 1

    s2 = _render_section("🟠 Workstream lệch cam kết", s2_lines, max_lines)
    if s2:
        sections.append(s2)

    # 3. 🟡 Chờ duyệt dời lịch
    s3_lines: list[str] = []
    for key, iss in sorted(snapshot.issues.items()):
        if not matches_project(iss):
            continue
        if iss.is_resolved:
            continue
        if "APPROVAL_PENDING" in snapshot.flags.get(key, frozenset()):
            req_str = (
                f" (Người yêu cầu: {iss.reschedule_requester})"
                if iss.reschedule_requester
                else ""
            )
            s3_lines.append(f"- {base_url}/browse/{key} | {iss.summary}{req_str}")
    s3 = _render_section("🟡 Chờ duyệt dời lịch", s3_lines, max_lines)
    if s3:
        sections.append(s3)

    # 4. ⛔ Đang block
    s4_lines: list[str] = []
    for key, iss in sorted(snapshot.issues.items()):
        if not matches_project(iss):
            continue
        if iss.is_resolved:
            continue
        if "BLOCKED" in snapshot.flags.get(key, frozenset()) or iss.blocked:
            by_str = f" (Lý do: {iss.blocked_by})" if iss.blocked_by else ""
            s4_lines.append(f"- {base_url}/browse/{key} | {iss.summary}{by_str}")
    s4 = _render_section("⛔ Đang block", s4_lines, max_lines)
    if s4:
        sections.append(s4)

    # 5. 👤 Chưa cập nhật hôm qua
    s5_lines: list[str] = []
    for user in sorted(snapshot.not_updated):
        if user == cfg.bot_user:
            continue
        if project is not None:
            # Check if user has active in_progress work items in this project
            has_in_proj = any(
                iss.assignee == user
                and iss.status == "in_progress"
                and iss.project == project
                for iss in snapshot.issues.values()
            )
            if not has_in_proj:
                continue
        s5_lines.append(f"- @{user}")
    s5 = _render_section("👤 Chưa cập nhật hôm qua", s5_lines, max_lines)
    if s5:
        sections.append(s5)

    # 6. 📨 Phản ánh hôm qua
    # per project with activity:
    # N mới · B là lỗi · K không phải lỗi · P chờ xác minh > 1 ngày
    prev_working_day = cal.previous_working_day(snapshot.today)
    reports_by_proj: dict[str, list[Issue]] = {}
    for iss in snapshot.issues.values():
        if iss.type == "report":
            reports_by_proj.setdefault(iss.project, []).append(iss)

    s6_lines: list[str] = []
    for proj_key in sorted(reports_by_proj.keys()):
        if project is not None and proj_key != project:
            continue
        proj_reports = reports_by_proj[proj_key]
        n_moi = sum(
            1
            for r in proj_reports
            if r.created and r.created.date() == prev_working_day
        )
        b_loi = sum(
            1
            for r in proj_reports
            if r.status in {"done", "cancelled"}
            and r.status_changed_at
            and r.status_changed_at.date() == prev_working_day
            and cfg.resolution_logical(r.resolution) == "bug"
        )
        k_khong_loi = sum(
            1
            for r in proj_reports
            if r.status in {"done", "cancelled"}
            and r.status_changed_at
            and r.status_changed_at.date() == prev_working_day
            and cfg.resolution_logical(r.resolution) != "bug"
        )
        p_stale = sum(
            1 for r in proj_reports if "STALE" in snapshot.flags.get(r.key, frozenset())
        )

        if n_moi > 0 or b_loi > 0 or k_khong_loi > 0 or p_stale > 0:
            s6_lines.append(
                f"- {proj_key}: {n_moi} mới · {b_loi} là lỗi · "
                f"{k_khong_loi} không phải lỗi · {p_stale} chờ xác minh > 1 ngày"
            )
    s6 = _render_section("📨 Phản ánh hôm qua", s6_lines, max_lines)
    if s6:
        sections.append(s6)

    # 7. ✅ Đúng tiến độ: N workstream
    if on_track_count > 0:
        sections.append([f"✅ Đúng tiến độ: {on_track_count} workstream"])

    # 11/11: always say which day and scope, and say so when nothing needs
    # attention (a quiet day used to render as an empty string).
    scope = project or "Tất cả dự án"
    header = f"📋 Báo cáo {snapshot.today:%d/%m/%Y} · {scope}"
    has_issues = len(sections) > (1 if on_track_count > 0 else 0)
    if not has_issues:
        sections.insert(
            0,
            ["Không có leo thang, block, việc chờ duyệt hay người chưa cập nhật."],
        )
    return "\n\n".join([header] + ["\n".join(sec) for sec in sections])


def render_workstream(snapshot: Snapshot, epic_key: str) -> str:
    """Render the actor table view for a single epic workstream."""
    epic = snapshot.issues.get(epic_key)
    em = snapshot.epics.get(epic_key)
    if not epic or not em:
        return f"Không tìm thấy workstream {epic_key}"

    due_str = epic.due_date.isoformat() if epic.due_date else "N/A"
    fc_str = em.forecast_end.isoformat() if em.forecast_end else "N/A"
    slip_str = (
        "ETA không xác định"
        if em.unknown_eta
        else (f"{em.slip_days} ngày" if em.slip_days is not None else "N/A")
    )

    header = (
        f"Workstream: {epic.key} - {epic.summary}\n"
        f"Due: {due_str} | Dự báo: {fc_str} | Lệch: {slip_str}\n"
    )

    lines = [
        header,
        f"{'Actor':<18} {'Luồng':>5} {'Dev':>5} {'QC':>5} "
        f"{'Sẵn sàng UAT':>14} {'Đang UAT':>10} {'Đã ký':>7}",
    ]

    for act in em.actors:
        note = ""
        if act.ready_for_uat == act.total and act.total > 0:
            actor_display = (
                act.actor.split(" - ")[-1].strip() if " - " in act.actor else act.actor
            )
            note = f"   ← mời {actor_display} UAT được"

        row = (
            f"{act.actor:<18} "
            f"{act.total:>5} "
            f"{act.dev:>5} "
            f"{act.qc:>5} "
            f"{act.ready_for_uat:>14} "
            f"{act.in_uat:>10} "
            f"{act.signed:>7}"
            f"{note}"
        )
        lines.append(row)

    return "\n".join(lines)


def render_my_day(snapshot: Snapshot, user: str, cfg: EggsConfig) -> str:
    """Render the day's tasks and pending items for a user."""
    base_url = cfg.jira_url.rstrip("/")

    my_items = [
        iss
        for iss in snapshot.issues.values()
        if iss.assignee == user
        and not iss.is_resolved
        and not iss.frozen
        and iss.is_work_item
    ]

    lines = [f"📋 Kế hoạch ngày của {user} ({snapshot.today.isoformat()})\n"]

    if my_items:
        lines.append("Công việc cần làm:")
        for iss in sorted(my_items, key=lambda x: (x.planned_end or date.max, x.key)):
            flags_str = (
                f" [{', '.join(sorted(snapshot.flags.get(iss.key, [])))}]"
                if snapshot.flags.get(iss.key)
                else ""
            )
            end_str = f" (Hạn: {iss.planned_end})" if iss.planned_end else ""
            url = f"{base_url}/browse/{iss.key}"
            lines.append(f"- {url} | {iss.summary} [{iss.status}]{end_str}{flags_str}")
    else:
        lines.append("Không có công việc cần làm.")

    # QC items if user is qc_owner of stories
    qc_stories = [
        s
        for s in snapshot.issues.values()
        if s.type == "story"
        and s.qc_owner == user
        and s.status in {"ready_for_qc", "testing"}
    ]
    if qc_stories:
        lines.append("\nLuồng QC đang chờ:")
        for s in qc_stories:
            lines.append(f"- {base_url}/browse/{s.key} | {s.summary} [{s.status}]")

    # Approvals waiting
    pending_approvals = [
        iss
        for iss in snapshot.issues.values()
        if "APPROVAL_PENDING" in snapshot.flags.get(iss.key, frozenset())
        and (iss.reschedule_requester or "").lower() != (user or "").lower()
    ]
    if pending_approvals:
        lines.append("\nYêu cầu chờ duyệt:")
        for iss in pending_approvals:
            url = f"{base_url}/browse/{iss.key}"
            req = iss.reschedule_requester
            lines.append(f"- {url} | {iss.summary} (Người yêu cầu: {req})")

    return "\n".join(lines)


def render_pending_approvals(snapshot: Snapshot, user: str, cfg: EggsConfig) -> str:
    """Render list of reschedule requests waiting for decision by user."""
    base_url = cfg.jira_url.rstrip("/")

    pending = [
        iss
        for iss in snapshot.issues.values()
        if "APPROVAL_PENDING" in snapshot.flags.get(iss.key, frozenset())
        and (iss.reschedule_requester or "").lower() != (user or "").lower()
    ]

    if not pending:
        return "Không có yêu cầu dời lịch nào đang chờ bạn duyệt."

    lines = ["🟡 Yêu cầu dời lịch chờ duyệt:"]
    for iss in pending:
        req_str = (
            f" (Người yêu cầu: {iss.reschedule_requester})"
            if iss.reschedule_requester
            else ""
        )
        end_str = f" -> Đề xuất: {iss.proposed_end}" if iss.proposed_end else ""
        lines.append(f"- {base_url}/browse/{iss.key} | {iss.summary}{end_str}{req_str}")

    return "\n".join(lines)


_WEEKDAYS_VI = [
    "Thứ Hai",
    "Thứ Ba",
    "Thứ Tư",
    "Thứ Năm",
    "Thứ Sáu",
    "Thứ Bảy",
    "Chủ Nhật",
]


def _fmt_hours(h: float) -> str:
    if h.is_integer():
        return f"{int(h)}h"
    return f"{h:g}h"


_WORKLOG_ISSUE_CHUNK = 50


def fill_worklog_issues(
    gateway: JiraGateway,
    cfg: EggsConfig,
    snapshot: Snapshot,
    date_from: date,
    date_to: date,
) -> Snapshot:
    """Add issues that have worklogs in the range but are not in the snapshot.

    The snapshot only holds open issues and those resolved in the last 7 days,
    so hours logged on an issue closed earlier would lose their project.
    Fetch those issues by key; on a search error they stay unknown.
    """
    tz = ZoneInfo(cfg.timezone)
    missing = sorted(
        {
            wl.issue_key
            for wl in snapshot.worklogs
            if wl.issue_key not in snapshot.issues
            and date_from <= wl.started.astimezone(tz).date() <= date_to
        }
    )
    if not missing:
        return snapshot
    issues = dict(snapshot.issues)
    fields = ["summary", "project", "issuetype", "status", "assignee", "resolution"]
    for i in range(0, len(missing), _WORKLOG_ISSUE_CHUNK):
        chunk = missing[i : i + _WORKLOG_ISSUE_CHUNK]
        try:
            raws = gateway.search(f"key in ({', '.join(chunk)})", fields=fields)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not fetch %d worklog issues: %s", len(chunk), e)
            continue
        for raw in raws:
            try:
                iss = issue_from_json(raw, cfg)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not map worklog issue: %s", e)
                continue
            issues.setdefault(iss.key, iss)
    return replace(snapshot, issues=issues)


def compute_worklog_report(
    snapshot: Snapshot,
    cfg: EggsConfig,
    cal: WorkCalendar,
    identity: Identity,
    project: str | None = None,
    person: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    tz = ZoneInfo(cfg.timezone)
    current_now = now or datetime.now(tz)
    current_today = current_now.astimezone(tz).date()

    if date_from is None:
        d_from = current_today - timedelta(days=current_today.weekday())
    else:
        d_from = date_from

    if date_to is None:
        d_to = current_today
    else:
        d_to = date_to

    if d_from > d_to:
        return {
            "ok": False,
            "message": "date_from không được lớn hơn date_to.",
            "range": {"from": d_from.isoformat(), "to": d_to.isoformat()},
            "people": [],
            "idle_with_work_in_progress": [],
        }

    if (d_to - d_from).days + 1 > 31:
        return {
            "ok": False,
            "message": "Khoảng thời gian không được vượt quá 31 ngày.",
            "range": {"from": d_from.isoformat(), "to": d_to.isoformat()},
            "people": [],
            "idle_with_work_in_progress": [],
        }

    proj = project.upper() if project else None
    norm_caller = identity.username.strip().lower()
    norm_person = person.strip().lower() if person else None

    # Check team access:
    if proj:
        has_team_access = identity.is_lead or (
            "lead_ba" in identity.roles.get(proj, set())
        )
    else:
        ba_projects = {p for p, roles in identity.roles.items() if "lead_ba" in roles}
        has_team_access = identity.is_lead or bool(ba_projects)

    if not has_team_access:
        if norm_person is not None and norm_person != norm_caller:
            return {
                "ok": False,
                "message": (
                    "Bạn chỉ xem được giờ của mình; "
                    "giờ của người khác do BA hoặc Lead xem."
                ),
                "range": {"from": d_from.isoformat(), "to": d_to.isoformat()},
                "people": [],
                "idle_with_work_in_progress": [],
            }
        target_person: str | None = identity.username
        is_team_view = False
    elif norm_person is not None:
        target_person = person.strip() if person else None
        is_team_view = False
    else:
        target_person = None
        is_team_view = True

    own_view = not is_team_view and (
        target_person is None or target_person.lower() == norm_caller
    )

    # Allowed projects for issues. Own view: every project of the caller.
    # BA/Lead looking at a team or another person: only projects they lead.
    if proj:
        allowed_projects: set[str] | None = {proj}
    elif own_view or identity.is_lead:
        allowed_projects = None
    else:
        allowed_projects = {
            p for p, roles in identity.roles.items() if "lead_ba" in roles
        }
    # Worklogs on issues outside the snapshot ("khác") are counted only when
    # the caller may see every project: their own hours or the Lead.
    include_other = proj is None and not is_team_view and (own_view or identity.is_lead)

    def round_to_quarter(hours: float) -> float:
        return round(round(hours * 4.0) / 4.0, 2)

    # Filter worklogs
    matching_worklogs = []
    for wl in snapshot.worklogs:
        wl_started_local = wl.started.astimezone(tz)
        wl_date = wl_started_local.date()
        if not (d_from <= wl_date <= d_to):
            continue

        iss = snapshot.issues.get(wl.issue_key)
        if iss is not None:
            wl_project = iss.project.upper()
        else:
            wl_project = "khác"

        if wl_project == "khác":
            if not include_other:
                continue
        elif allowed_projects is not None and wl_project not in allowed_projects:
            continue
        if not is_team_view:
            wl_author_norm = wl.author.strip().lower()
            expected_user = target_person.lower() if target_person else norm_caller
            if wl_author_norm != expected_user:
                continue

        matching_worklogs.append((wl, wl_date, wl_project))

    if not is_team_view:
        people_usernames = [target_person] if target_person else [norm_caller]
    else:
        people_usernames = sorted({wl.author for wl, _, _ in matching_worklogs})

    people = []
    user_hours_map: dict[str, float] = {}

    all_days: list[date] = []
    cur = d_from
    while cur <= d_to:
        all_days.append(cur)
        cur += timedelta(days=1)

    working_days_count = sum(1 for d in all_days if cal.is_working_day(d))
    target_total = round_to_quarter(
        working_days_count * cfg.thresholds.daily_hours_target
    )

    person_missing_descriptions: dict[str, list[str]] = {}

    for username in people_usernames:
        user_wls = [
            (wl, dt)
            for wl, dt, _ in matching_worklogs
            if wl.author.strip().lower() == username.strip().lower()
        ]
        day_seconds: dict[date, int] = defaultdict(int)
        issue_seconds: dict[str, int] = defaultdict(int)
        for wl, dt in user_wls:
            day_seconds[dt] += wl.seconds
            issue_seconds[wl.issue_key] += wl.seconds

        total_secs = sum(day_seconds.values())
        user_total_hours = round_to_quarter(total_secs / 3600.0)
        user_hours_map[username] = user_total_hours

        days_list = []
        missing_hours_total = 0.0
        missing_day_strs = []

        for d in all_days:
            is_work = cal.is_working_day(d)
            logged_secs = day_seconds.get(d, 0)
            d_hours = round_to_quarter(logged_secs / 3600.0)

            if is_work:
                target = cfg.thresholds.daily_hours_target
                if d < current_today:
                    below_target = d_hours < target
                elif d == current_today:
                    if current_now.hour >= cfg.thresholds.end_day_hour:
                        below_target = d_hours < target
                    else:
                        below_target = False
                else:
                    below_target = False

                if below_target:
                    diff = round_to_quarter(target - d_hours)
                    missing_hours_total += diff
                    w_vn = _WEEKDAYS_VI[d.weekday()]
                    missing_day_strs.append(
                        f"{w_vn} {d.strftime('%d/%m')} ({_fmt_hours(diff)})"
                    )

                days_list.append(
                    {
                        "date": d.isoformat(),
                        "hours": d_hours,
                        "below_target": below_target,
                        "holiday": False,
                    }
                )
            else:
                if d_hours > 0:
                    days_list.append(
                        {
                            "date": d.isoformat(),
                            "hours": d_hours,
                            "below_target": False,
                            "holiday": True,
                        }
                    )

        top_issues = []
        for k, secs in sorted(issue_seconds.items(), key=lambda x: (-x[1], x[0]))[:3]:
            iss_obj = snapshot.issues.get(k)
            summary_str = iss_obj.summary if iss_obj else ""
            top_issues.append(
                {
                    "key": k,
                    "summary": summary_str,
                    "hours": round_to_quarter(secs / 3600.0),
                }
            )

        missing_hours_total = round_to_quarter(missing_hours_total)
        person_missing_descriptions[username] = missing_day_strs
        people.append(
            {
                "username": username,
                "total_hours": user_total_hours,
                "days": days_list,
                "top_issues": top_issues,
                "missing_hours": missing_hours_total,
            }
        )

    people.sort(key=lambda p: p["username"].lower())

    # Idle with work in progress:
    idle_people = []
    if has_team_access and not own_view:
        hours_by_user = {u.strip().lower(): h for u, h in user_hours_map.items()}
        target_norm = target_person.lower() if target_person else None
        in_progress_assignees: dict[str, str] = {}
        for iss in snapshot.issues.values():
            if (
                iss.status == "in_progress"
                and iss.is_work_item
                and iss.assignee
                and not iss.is_resolved
                and not iss.frozen
                and (
                    allowed_projects is None or iss.project.upper() in allowed_projects
                )
            ):
                a_norm = iss.assignee.strip().lower()
                if target_norm is None or a_norm == target_norm:
                    in_progress_assignees.setdefault(a_norm, iss.assignee)
        idle_people = sorted(
            name
            for a_norm, name in in_progress_assignees.items()
            if hours_by_user.get(a_norm, 0.0) == 0.0
        )

    message_lines = []
    for p in people:
        u = p["username"]
        missing_strs = person_missing_descriptions.get(u, [])
        if missing_strs:
            missing_detail = ", ".join(missing_strs)
            line = (
                f"{u}: {_fmt_hours(p['total_hours'])}/{_fmt_hours(target_total)}, "
                f"thiếu {missing_detail}"
            )
        else:
            line = f"{u}: {_fmt_hours(p['total_hours'])}/{_fmt_hours(target_total)}"
        message_lines.append(line)

    if idle_people:
        message_lines.append(f"Chưa log giờ: {', '.join(idle_people)}")

    if not message_lines:
        message_str = "Không có dữ liệu worklog."
    else:
        message_str = "\n".join(message_lines)

    return {
        "ok": True,
        "message": message_str,
        "range": {"from": d_from.isoformat(), "to": d_to.isoformat()},
        "people": people,
        "idle_with_work_in_progress": idle_people,
    }
