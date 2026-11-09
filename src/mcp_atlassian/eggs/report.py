from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import WorkCalendar
from .config import EggsConfig
from .escalation import Escalation, escalate
from .forecast import ActorRow, EpicMetrics, FlowMetrics, epic_metrics, flow_metrics
from .gateway import JiraGateway
from .health import compute_flags, detect_interruptions, people_not_updated
from .mapping import issue_from_json
from .model import Issue, Worklog


@dataclass(frozen=True)
class Snapshot:
    issues: dict[str, Issue]
    flags: dict[str, frozenset[str]]
    escalations: dict[str, Escalation]
    flows: dict[str, FlowMetrics]
    epics: dict[str, EpicMetrics]
    not_updated: list[str]
    today: date


def build_snapshot(
    gateway: JiraGateway,
    cfg: EggsConfig,
    today: date,
    cal: WorkCalendar,
    since: datetime,
) -> Snapshot:
    """Fetch issues and worklogs, run all domain engines, and produce Snapshot."""
    # Active projects (non-frozen)
    active_projects = [k for k, p in cfg.projects.items() if not p.frozen]
    if active_projects:
        jql = f"project in ({', '.join(sorted(active_projects))}) AND (resolution is EMPTY OR resolved >= -7d)"
    else:
        jql = "resolution is EMPTY OR resolved >= -7d"

    fields = list(dict.fromkeys([
        "summary", "project", "issuetype", "status", "assignee",
        "created", "duedate", "timeoriginalestimate", "aggregatetimeoriginalestimate",
        "labels", "parent", "components", "resolution"
    ] + [f for f in cfg.fields.values() if f]))

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

    # Worklogs for the last 10 working days
    since_wl_date = cal.add_working_days(today, -10)
    since_wl = datetime(
        since_wl_date.year, since_wl_date.month, since_wl_date.day, 0, 0,
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
        fm = flow_metrics(story, children_by_parent.get(story_key, []), cal=cal, cfg=cfg)
        flows[story_key] = fm
        if fm.at_risk:
            flags[story_key] = flags.get(story_key, frozenset()) | {"AT_RISK"}

    # Escalations
    escalations: dict[str, Escalation] = {}
    for key, iss in issues.items():
        item_flags = flags.get(key, frozenset())
        if item_flags:
            parent_story = stories.get(iss.parent_key) if iss.parent_key else None
            epic = issues.get(iss.epic_key) if iss.epic_key else None
            esc = escalate(iss, item_flags, story=parent_story, epic=epic, today=today, cal=cal, cfg=cfg)
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

    not_updated = people_not_updated(list(issues.values()), worklogs=worklogs, today=today, cal=cal)

    return Snapshot(
        issues=issues,
        flags=flags,
        escalations=escalations,
        flows=flows,
        epics=epics,
        not_updated=not_updated,
        today=today,
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
                s1_lines.append(
                    f"- {base_url}/browse/{key} | {iss.summary} ({item_flags}){owner_str}"
                )
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
            slip_str = "ETA không xác định" if em.unknown_eta else f"Trễ {em.slip_days:+d} ngày"
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
                        actor_segments.append(f"{code} {act.ready_for_uat}/{act.total} sẵn sàng UAT")
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
        else:
            on_track_count += 1

    s2 = _render_section("🟠 Workstream lệch cam kết", s2_lines, max_lines)
    if s2:
        sections.append(s2)

    # 3. 🟡 Chờ duyệt dời lịch
    s3_lines: list[str] = []
    for key, iss in sorted(snapshot.issues.items()):
        if not matches_project(iss):
            continue
        if "APPROVAL_PENDING" in snapshot.flags.get(key, frozenset()):
            req_str = f" (Người yêu cầu: {iss.reschedule_requester})" if iss.reschedule_requester else ""
            s3_lines.append(f"- {base_url}/browse/{key} | {iss.summary}{req_str}")
    s3 = _render_section("🟡 Chờ duyệt dời lịch", s3_lines, max_lines)
    if s3:
        sections.append(s3)

    # 4. ⛔ Đang block
    s4_lines: list[str] = []
    for key, iss in sorted(snapshot.issues.items()):
        if not matches_project(iss):
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
        if project is not None:
            # Check if user has active in_progress work items in this project
            has_in_proj = any(
                iss.assignee == user and iss.status == "in_progress" and iss.project == project
                for iss in snapshot.issues.values()
            )
            if not has_in_proj:
                continue
        s5_lines.append(f"- @{user}")
    s5 = _render_section("👤 Chưa cập nhật hôm qua", s5_lines, max_lines)
    if s5:
        sections.append(s5)

    # 6. 📨 Phản ánh hôm qua
    # per project with activity: N mới · B là lỗi · K không phải lỗi · P chờ xác minh > 1 ngày
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
            1 for r in proj_reports
            if r.created and r.created.date() == prev_working_day
        )
        b_loi = sum(
            1 for r in proj_reports
            if r.status in {"done", "cancelled"}
            and r.status_changed_at and r.status_changed_at.date() == prev_working_day
            and cfg.resolution_logical(r.resolution) == "bug"
        )
        k_khong_loi = sum(
            1 for r in proj_reports
            if r.status in {"done", "cancelled"}
            and r.status_changed_at and r.status_changed_at.date() == prev_working_day
            and cfg.resolution_logical(r.resolution) != "bug"
        )
        p_stale = sum(
            1 for r in proj_reports
            if "STALE" in snapshot.flags.get(r.key, frozenset())
        )

        if n_moi > 0 or b_loi > 0 or k_khong_loi > 0 or p_stale > 0:
            s6_lines.append(
                f"- {proj_key}: {n_moi} mới · {b_loi} là lỗi · {k_khong_loi} không phải lỗi · {p_stale} chờ xác minh > 1 ngày"
            )
    s6 = _render_section("📨 Phản ánh hôm qua", s6_lines, max_lines)
    if s6:
        sections.append(s6)

    # 7. ✅ Đúng tiến độ: N workstream
    if on_track_count > 0:
        sections.append([f"✅ Đúng tiến độ: {on_track_count} workstream"])

    return "\n\n".join("\n".join(sec) for sec in sections)


def render_workstream(snapshot: Snapshot, epic_key: str) -> str:
    """Render the actor table view for a single epic workstream."""
    epic = snapshot.issues.get(epic_key)
    em = snapshot.epics.get(epic_key)
    if not epic or not em:
        return f"Không tìm thấy workstream {epic_key}"

    due_str = epic.due_date.isoformat() if epic.due_date else "N/A"
    fc_str = em.forecast_end.isoformat() if em.forecast_end else "N/A"
    slip_str = "ETA không xác định" if em.unknown_eta else (
        f"{em.slip_days:+d} ngày" if em.slip_days is not None else "N/A"
    )

    header = (
        f"Workstream: {epic.key} - {epic.summary}\n"
        f"Due: {due_str} | Dự báo: {fc_str} | Lệch: {slip_str}\n"
    )

    lines = [
        header,
        f"{'Actor':<18} {'Luồng':>5} {'Dev':>5} {'QC':>5} {'Sẵn sàng UAT':>14} {'Đang UAT':>10} {'Đã ký':>7}",
    ]

    for act in em.actors:
        note = ""
        if act.ready_for_uat == act.total and act.total > 0:
            actor_display = act.actor.split(" - ")[-1].strip() if " - " in act.actor else act.actor
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


def render_my_day(snapshot: Snapshot, user: str, cfg: EggsConfig | None = None) -> str:
    """Render the day's tasks and pending items for a user."""
    base_url = cfg.jira_url.rstrip("/") if cfg else "https://jira.eggstech.io.vn"

    my_items = [
        iss for iss in snapshot.issues.values()
        if iss.assignee == user and not iss.is_resolved and not iss.frozen and iss.is_work_item
    ]

    lines = [f"📋 Kế hoạch ngày của {user} ({snapshot.today.isoformat()})\n"]

    if my_items:
        lines.append("Công việc cần làm:")
        for iss in sorted(my_items, key=lambda x: (x.planned_end or date.max, x.key)):
            flags_str = f" [{', '.join(sorted(snapshot.flags.get(iss.key, [])))}]" if snapshot.flags.get(iss.key) else ""
            end_str = f" (Hạn: {iss.planned_end})" if iss.planned_end else ""
            lines.append(f"- {base_url}/browse/{iss.key} | {iss.summary} [{iss.status}]{end_str}{flags_str}")
    else:
        lines.append("Không có công việc cần làm.")

    # QC items if user is qc_owner of stories
    qc_stories = [
        s for s in snapshot.issues.values()
        if s.type == "story" and s.qc_owner == user and s.status in {"ready_for_qc", "testing"}
    ]
    if qc_stories:
        lines.append("\nLuồng QC đang chờ:")
        for s in qc_stories:
            lines.append(f"- {base_url}/browse/{s.key} | {s.summary} [{s.status}]")

    # Approvals waiting
    pending_approvals = [
        iss for iss in snapshot.issues.values()
        if "APPROVAL_PENDING" in snapshot.flags.get(iss.key, frozenset())
        and iss.reschedule_requester != user
    ]
    if pending_approvals:
        lines.append("\nYêu cầu chờ duyệt:")
        for iss in pending_approvals:
            lines.append(f"- {base_url}/browse/{iss.key} | {iss.summary} (Người yêu cầu: {iss.reschedule_requester})")

    return "\n".join(lines)


def render_pending_approvals(snapshot: Snapshot, user: str, cfg: EggsConfig | None = None) -> str:
    """Render list of reschedule requests waiting for decision by user."""
    base_url = cfg.jira_url.rstrip("/") if cfg else "https://jira.eggstech.io.vn"

    pending = [
        iss for iss in snapshot.issues.values()
        if "APPROVAL_PENDING" in snapshot.flags.get(iss.key, frozenset())
        and iss.reschedule_requester != user
    ]

    if not pending:
        return "Không có yêu cầu dời lịch nào đang chờ bạn duyệt."

    lines = ["🟡 Yêu cầu dời lịch chờ duyệt:"]
    for iss in pending:
        req_str = f" (Người yêu cầu: {iss.reschedule_requester})" if iss.reschedule_requester else ""
        end_str = f" -> Đề xuất: {iss.proposed_end}" if iss.proposed_end else ""
        lines.append(f"- {base_url}/browse/{iss.key} | {iss.summary}{end_str}{req_str}")

    return "\n".join(lines)
