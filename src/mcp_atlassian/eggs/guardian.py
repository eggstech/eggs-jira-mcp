"""EGGS Guardian CLI and periodic background tasks.

Subcommands:
- evaluate: Tính toán cờ sức khỏe, leo thang, ETA và ghi ngược Jira
- report: Xuất báo cáo ngày và gửi Google Chat
- remind: Nhắc nhở người chưa log work hôm nay
- ops-epics: Đảm bảo tồn tại Epic [OPS] tháng hiện tại cho từng dự án
- resolve-fields: Đọc danh sách trường Jira và ánh xạ tên logic
- backfill-qc-owner: Back-fill trường QC Owner cho các Bug stand-alone chưa có
"""

from __future__ import annotations

import argparse
import calendar
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp_atlassian.jira import JiraConfig, JiraFetcher

from .calendar import WorkCalendar
from .chat import post_to_chat
from .config import EggsConfig, load_config
from .gateway import JiraGateway
from .gateway_atlassian import AtlassianGateway
from .mapping import CLEAR, issue_fields_for_write, issue_from_json
from .report import build_snapshot, render_report

logger = logging.getLogger(__name__)

GUARDIAN_LOGICAL_FIELDS: frozenset[str] = frozenset(
    {
        "health_flags",
        "health_since",
        "escalation_level",
        "escalation_owner",
        "forecast_end",
        "slip_days",
        "unscheduled_count",
    }
)

LOGICAL_FIELD_NAMES: dict[str, str] = {
    "planned_start": "Planned Start",
    "planned_end": "Planned End",
    "epic_link": "Epic Link",
    "epic_name": "Epic Name",
    "flagged": "Flagged",
    "qc_owner": "QC Owner",
    "part": "Part",
    "reschedule_requester": "Reschedule Requester",
    "baseline_end": "Baseline End",
    "proposed_end": "Proposed End",
    "reschedule_reason": "Reschedule Reason",
    "reschedule_status": "Reschedule Status",
    "reschedule_count": "Reschedule Count",
    "qc_fail_count": "QC Fail Count",
    "block_reason": "Block Reason",
    "blocked_by": "Blocked By",
    "blocked_since": "Blocked Since",
    "health_flags": "Health Flags",
    "escalation_level": "Escalation Level",
    "escalation_owner": "Escalation Owner",
    "health_since": "Health Since",
    "forecast_end": "Forecast End",
    "slip_days": "Slip Days",
    "unscheduled_count": "Unscheduled Count",
    "report_source": "Report Source",
    "reported_by": "Reported By",
    "reported_at": "Reported At",
}


class EvaluationResult(list):
    """Result of guardian evaluation, keeping backwards-compatibility as a list."""

    def __init__(
        self,
        changes: list[str],
        issue_count: int = 0,
        field_count: int = 0,
        has_errors: bool = False,  # noqa: FBT001, FBT002
    ) -> None:
        super().__init__(changes)
        self.issue_count = issue_count
        self.field_count = field_count
        self.has_errors = has_errors

    @property
    def exit_code(self) -> int:
        return 1 if self.has_errors else 0

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, int):
            return self.exit_code == other
        return super().__eq__(other)


def evaluate(
    gateway: JiraGateway,
    cfg: EggsConfig,
    now: datetime,
    cal: WorkCalendar,
    dry_run: bool = False,  # noqa: FBT001, FBT002
) -> EvaluationResult:
    """Evaluate health flags, escalations and forecasts, writing updates to Jira.

    - Guardian writes ONLY the 7 guardian fields with notify_users=False.
    - Adds comment [bot_user] Đã xử lý: <flag> when a flag is cleared.
    - Idempotent: writes only on change.
    - Never transitions issues.
    """
    # Auto-unfreeze runs before health evaluation so the issue is
    # evaluated normally in the same run
    unfrozen_keys = unfreeze_stale_issues(
        gateway, cfg, now, days=cfg.thresholds.freeze_days, dry_run=dry_run
    )

    tz = ZoneInfo(cfg.timezone)
    today = now.astimezone(tz).date()
    since = now - timedelta(minutes=70)
    snapshot = build_snapshot(gateway, cfg, today, cal, since=since)

    if dry_run and unfrozen_keys:
        for k in unfrozen_keys:
            if k in snapshot.issues:
                snapshot.issues[k].frozen = False

    allowed_guardian_field_ids = {
        cfg.fields[f] for f in GUARDIAN_LOGICAL_FIELDS if f in cfg.fields
    }
    changes: list[str] = []
    changed_issues_set: set[str] = set()
    total_fields_count = 0
    has_errors = False

    for key, iss in snapshot.issues.items():
        is_res = iss.is_resolved() if callable(iss.is_resolved) else iss.is_resolved
        if iss.type is None or iss.frozen or is_res:
            continue

        # Desired new values
        new_flags = snapshot.flags.get(key, frozenset())
        esc = snapshot.escalations.get(key) if new_flags else None
        desired_since = esc.since if esc else None
        desired_level = esc.level if esc else None
        desired_owner = esc.owner if esc else None

        desired_forecast: date | None = None
        desired_slip: int | None = None
        desired_unscheduled: int | None = None

        if iss.type == "story":
            flow = snapshot.flows.get(key)
            if flow:
                desired_forecast = flow.forecast_end
                desired_slip = flow.slip_days
                desired_unscheduled = flow.unscheduled
            else:
                desired_unscheduled = 0
        elif iss.type == "epic":
            em = snapshot.epics.get(key)
            if em:
                desired_forecast = em.forecast_end
                desired_slip = em.slip_days
                desired_unscheduled = em.unscheduled
            else:
                desired_unscheduled = 0

        # Change detection
        field_changes: dict[str, Any] = {}

        if new_flags != iss.health_flags:
            field_changes["health_flags"] = new_flags if new_flags else CLEAR

        if desired_since != iss.health_since:
            field_changes["health_since"] = (
                desired_since if desired_since is not None else CLEAR
            )

        if desired_level != iss.escalation_level:
            field_changes["escalation_level"] = (
                desired_level if desired_level is not None else CLEAR
            )

        if desired_owner != iss.escalation_owner:
            field_changes["escalation_owner"] = (
                desired_owner if desired_owner is not None else CLEAR
            )

        if iss.type in ("story", "epic"):
            if desired_forecast != iss.forecast_end:
                field_changes["forecast_end"] = (
                    desired_forecast if desired_forecast is not None else CLEAR
                )
            if desired_slip != iss.slip_days:
                field_changes["slip_days"] = (
                    desired_slip if desired_slip is not None else CLEAR
                )
            if desired_unscheduled != iss.unscheduled_count:
                field_changes["unscheduled_count"] = (
                    desired_unscheduled if desired_unscheduled is not None else CLEAR
                )

        # Write fields FIRST
        write_succeeded = True
        if field_changes:
            payload = issue_fields_for_write(cfg, **field_changes)
            # Strict safety check: ONLY guardian field ids written
            for field_id in payload:
                if field_id not in allowed_guardian_field_ids:
                    msg = (
                        f"Guardian attempted to write non-guardian field {field_id} "
                        f"on issue {key}"
                    )
                    raise ValueError(msg)

            if not dry_run:
                try:
                    gateway.update_fields(key, payload, notify_users=False)
                except Exception as e:  # noqa: BLE001
                    logger.error("%s: %s", key, e)
                    has_errors = True
                    write_succeeded = False

            if write_succeeded:
                changes.append(f"{key}: updated {list(field_changes.keys())}")
                changed_issues_set.add(key)
                total_fields_count += len(field_changes)

        # Comment on cleared flags ONLY after successful field write
        if write_succeeded:
            cleared_flags = sorted(iss.health_flags - new_flags)
            for flag in cleared_flags:
                comment_body = f"[{cfg.bot_user}] Đã xử lý: {flag}"
                if not dry_run:
                    try:
                        gateway.add_comment(key, comment_body)
                    except Exception as e:  # noqa: BLE001
                        logger.error("%s: %s", key, e)
                        has_errors = True
                        continue
                changes.append(f"{key}: cleared flag {flag}")
                changed_issues_set.add(key)

    return EvaluationResult(
        changes,
        issue_count=len(changed_issues_set),
        field_count=total_fields_count,
        has_errors=has_errors,
    )


class OpsEpicResult(list):
    """Result of ensure_ops_epics, keeping backwards-compatibility as a list."""

    def __init__(self, items: Any = (), exit_code: int = 0) -> None:
        super().__init__(items)
        self.exit_code = exit_code

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, int):
            return self.exit_code == other
        return super().__eq__(other)


def ensure_ops_epics(
    gateway: JiraGateway,
    cfg: EggsConfig,
    today: date,
    cal: WorkCalendar,
    dry_run: bool = False,  # noqa: FBT001, FBT002
) -> OpsEpicResult:
    """Ensure the current month's OPS epic exists per active project."""
    created: list[str] = []
    failed = False
    expected_summary = cfg.ops_epic_summary.format(month=today.month, year=today.year)
    epic_type_name = cfg.issue_types["epic"][0]
    epic_name_field = cfg.fields.get("epic_name", "customfield_10102")

    # Calculate last working day of the month
    last_day = calendar.monthrange(today.year, today.month)[1]
    last_working_day = date(today.year, today.month, last_day)
    while not cal.is_working_day(last_working_day):
        last_working_day -= timedelta(days=1)

    active_projects = {k: p for k, p in cfg.projects.items() if not p.frozen}

    for proj_key, p in sorted(active_projects.items()):
        # Search by project and issuetype first
        epic_types = ", ".join(f'"{t}"' for t in cfg.issue_types["epic"])
        jql = f'project = "{proj_key}" AND issuetype in ({epic_types})'
        raw_issues = gateway.search(jql, fields=["summary", "issuetype", "project"])

        found = False
        for raw in raw_issues:
            iss = issue_from_json(raw, cfg)
            if (
                iss.project == proj_key
                and iss.type == "epic"
                and iss.summary == expected_summary
            ):
                found = True
                break

        if not found:
            create_payload = {
                "project": {"key": proj_key},
                "issuetype": {"name": epic_type_name},
                "summary": expected_summary,
                "assignee": {"name": p.default_lead_ba},
                "duedate": last_working_day.isoformat(),
                epic_name_field: expected_summary,
            }
            if not dry_run:
                try:
                    key = gateway.create_issue(create_payload)
                    created.append(
                        f"Created OPS epic {key} for {proj_key}: {expected_summary}"
                    )
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"Lỗi tạo OPS epic cho {proj_key}: {e}\n")
                    failed = True
            else:
                created.append(
                    f"[dry-run] Would create OPS epic for {proj_key}: "
                    f"{expected_summary}"
                )

    return OpsEpicResult(created, exit_code=1 if failed else 0)


def run_report(
    gateway: JiraGateway,
    cfg: EggsConfig,
    now: datetime,
    cal: WorkCalendar,
    project: str | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002
    http_post: Any = None,
) -> int:
    """Render daily report and post to Google Chat webhooks."""
    tz = ZoneInfo(cfg.timezone)
    today = now.astimezone(tz).date()
    since = datetime.combine(cal.previous_working_day(today), time.min, tzinfo=tz)
    snapshot = build_snapshot(gateway, cfg, today, cal, since=since)
    failures: list[Exception] = []

    if project:
        if project not in cfg.projects:
            sys.stderr.write(f"Dự án không tồn tại trong cấu hình: {project}\n")
            return 1
        if cfg.projects[project].frozen:
            sys.stderr.write(f"Dự án đang bị đóng băng: {project}\n")
            return 1
        text = render_report(snapshot, cfg, project=project)
        if dry_run:
            sys.stdout.write(text + "\n")
        else:
            webhook = cfg.projects[project].chat_webhook
            if webhook:
                try:
                    post_to_chat(
                        webhook, text, http_post=http_post, project_key=project
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error("Gửi báo cáo dự án %s thất bại: %s", project, e)
                    failures.append(e)
    else:
        # Lead global report
        lead_text = render_report(snapshot, cfg, project=None)
        if dry_run:
            sys.stdout.write("=== BÁO CÁO TOÀN DIỆN (LEAD) ===\n")
            sys.stdout.write(lead_text + "\n")
        else:
            if cfg.lead_chat_webhook:
                try:
                    post_to_chat(
                        cfg.lead_chat_webhook,
                        lead_text,
                        http_post=http_post,
                        project_key=None,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error("Gửi báo cáo Lead thất bại: %s", e)
                    failures.append(e)

        # Per-project reports
        for proj_key, p in sorted(cfg.projects.items()):
            if not p.frozen and p.chat_webhook:
                proj_text = render_report(snapshot, cfg, project=proj_key)
                if dry_run:
                    sys.stdout.write(f"=== BÁO CÁO DỰ ÁN {proj_key} ===\n")
                    sys.stdout.write(proj_text + "\n")
                else:
                    try:
                        post_to_chat(
                            p.chat_webhook,
                            proj_text,
                            http_post=http_post,
                            project_key=proj_key,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.error("Gửi báo cáo dự án %s thất bại: %s", proj_key, e)
                        failures.append(e)

    return 1 if failures else 0


def run_remind(
    gateway: JiraGateway,
    cfg: EggsConfig,
    now: datetime,
    cal: WorkCalendar,
    dry_run: bool = False,  # noqa: FBT001, FBT002
    http_post: Any = None,
) -> int:
    """Send end-of-day reminder to people without worklogs today."""
    tz = ZoneInfo(cfg.timezone)
    today = now.astimezone(tz).date()
    since = datetime.combine(cal.add_working_days(today, -5), time.min, tzinfo=tz)
    snapshot = build_snapshot(gateway, cfg, today, cal, since=since)
    failures: list[Exception] = []

    norm_bot = cfg.bot_user.strip().lower()
    authors_today = {
        wl.author.strip().lower()
        for wl in snapshot.worklogs
        if wl.started.astimezone(tz).date() == today
    }

    missing_by_project: dict[str, list[str]] = {}
    for iss in snapshot.issues.values():
        if (
            iss.is_work_item
            and not iss.is_resolved
            and not iss.frozen
            and iss.status == "in_progress"
            and iss.assignee
        ):
            if iss.assignee.strip().lower() == norm_bot:
                continue
            if iss.assignee.strip().lower() not in authors_today:
                missing_by_project.setdefault(iss.project, []).append(iss.assignee)

    # Check users with worklog in last 5 working days but no project role
    start_5days = cal.add_working_days(today, -5)
    workers_without_roles: list[tuple[str, str]] = []
    project_roles_cache: dict[str, set[str]] = {}

    for wl in snapshot.worklogs:
        wl_date = wl.started.astimezone(tz).date()
        if wl_date >= start_5days and wl.author.strip().lower() != norm_bot:
            proj = wl.issue_key.split("-")[0] if "-" in wl.issue_key else None
            if proj and proj in cfg.projects and not cfg.projects[proj].frozen:
                if proj not in project_roles_cache:
                    try:
                        members = gateway.project_role_members(proj)
                        project_roles_cache[proj] = {
                            u.strip().lower()
                            for ulist in members.values()
                            for u in ulist
                        }
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "Could not check role members for project %s: %s", proj, e
                        )
                        project_roles_cache[proj] = set()

                if wl.author.strip().lower() not in project_roles_cache[proj]:
                    pair = (wl.author, proj)
                    if pair not in workers_without_roles:
                        workers_without_roles.append(pair)

    suffix = "Gõ `/eggs` để cập nhật."

    # Post to active projects with missing users
    for proj_key, p in sorted(cfg.projects.items()):
        if not p.frozen and p.chat_webhook:
            p_users = sorted(set(missing_by_project.get(proj_key, [])))
            if p_users:
                user_lines = "\n".join(f"- {u}" for u in p_users)
                msg = (
                    f"Nhắc nhở cập nhật công việc hôm nay ({proj_key}):\n"
                    f"{user_lines}\n\n{suffix}"
                )
                if dry_run:
                    sys.stdout.write(f"=== Remind {proj_key} ===\n{msg}\n")
                else:
                    try:
                        post_to_chat(
                            p.chat_webhook,
                            msg,
                            http_post=http_post,
                            project_key=proj_key,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.error("Gửi nhắc nhở dự án %s thất bại: %s", proj_key, e)
                        failures.append(e)

    # Post to lead webhook
    lead_sections: list[str] = []
    all_missing = sorted({u for ulist in missing_by_project.values() for u in ulist})
    if all_missing:
        lead_sections.append(
            "Chưa cập nhật công việc hôm nay:\n"
            + "\n".join(f"- {u}" for u in all_missing)
        )
    if workers_without_roles:
        lead_sections.append(
            "Người có worklog trong 5 ngày qua nhưng chưa có Project Role:\n"
            + "\n".join(
                f"- {author} ({proj})" for author, proj in workers_without_roles
            )
        )

    if lead_sections and cfg.lead_chat_webhook:
        lead_body = "\n\n".join(lead_sections)
        lead_msg = f"{lead_body}\n\n{suffix}"
        if dry_run:
            sys.stdout.write(f"=== Remind Lead ===\n{lead_msg}\n")
        else:
            try:
                post_to_chat(
                    cfg.lead_chat_webhook,
                    lead_msg,
                    http_post=http_post,
                    project_key=None,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("Gửi nhắc nhở Lead thất bại: %s", e)
                failures.append(e)

    return 1 if failures else 0


class BackfillSearchError(Exception):
    """Exception raised when searching for issues in backfill fails."""


@dataclass
class BackfillResult:
    found: int
    written: int
    failed: list[tuple[str, str]]
    user: str
    issues: list[tuple[str, str, str]] = field(default_factory=list)
    found_by_type: dict[str, int] = field(default_factory=dict)
    written_by_type: dict[str, int] = field(default_factory=dict)


def backfill_qc_owner(
    gateway: JiraGateway,
    cfg: EggsConfig,
    project: str,
    qc_owner: str | None = None,
    types: list[str] | tuple[str, ...] | str | None = None,
    *,
    dry_run: bool = False,
) -> BackfillResult:
    qc_field_id = cfg.fields.get("qc_owner")
    if not qc_field_id:
        raise ValueError("Cấu hình fields.qc_owner không tồn tại.")

    proj_cfg = cfg.projects.get(project)
    user = qc_owner or (proj_cfg.triage_owner if proj_cfg else None)
    if not user:
        msg = (
            f"Dự án '{project}' không có triage_owner trong cấu hình "
            "và không cung cấp qc_owner."
        )
        raise ValueError(msg)

    if types is None:
        type_keys = ["bug", "story", "adhoc"]
    elif isinstance(types, str):
        type_keys = [t.strip().lower() for t in types.split(",") if t.strip()]
    else:
        type_keys = [t.strip().lower() for t in types if t.strip()]

    valid_keys = {"bug", "story", "adhoc"}
    for tk in type_keys:
        if tk not in valid_keys:
            msg = f"Loại issue '{tk}' không hợp lệ. Chỉ chấp nhận: bug, story, adhoc."
            raise ValueError(msg)

    non_adhoc_jira_types: list[str] = []
    if "bug" in type_keys:
        non_adhoc_jira_types.extend(cfg.issue_types.get("bug", []))
    if "story" in type_keys:
        non_adhoc_jira_types.extend(cfg.issue_types.get("story", []))

    adhoc_jira_types: list[str] = []
    if "adhoc" in type_keys:
        adhoc_jira_types.extend(cfg.issue_types.get("adhoc", []))

    adhoc_status_names = [
        name
        for logical, name in cfg.statuses.items()
        if logical in {"ready_for_qc", "testing"} and name
    ]
    if not adhoc_status_names:
        adhoc_status_names = [
            cfg.statuses.get("ready_for_qc", "QC TO DO"),
            cfg.statuses.get("testing", "Testing"),
        ]

    if not non_adhoc_jira_types and not adhoc_jira_types:
        msg = (
            f"Các loại issue đã chọn {type_keys} không có tên Jira issue type nào "
            "được cấu hình trong issue_types."
        )
        raise ValueError(msg)

    if adhoc_jira_types and non_adhoc_jira_types:
        non_adhoc_str = ", ".join(f'"{t}"' for t in non_adhoc_jira_types)
        adhoc_str = ", ".join(f'"{t}"' for t in adhoc_jira_types)
        status_str = ", ".join(f'"{s}"' for s in adhoc_status_names)
        jql = (
            f"project = {project} AND statusCategory != Done AND "
            f"((issuetype in ({non_adhoc_str})) OR "
            f"(issuetype in ({adhoc_str}) AND status in ({status_str})))"
        )
    elif adhoc_jira_types:
        adhoc_str = ", ".join(f'"{t}"' for t in adhoc_jira_types)
        status_str = ", ".join(f'"{s}"' for s in adhoc_status_names)
        jql = (
            f"project = {project} AND issuetype in ({adhoc_str}) "
            f"AND statusCategory != Done AND status in ({status_str})"
        )
    else:
        types_str = ", ".join(f'"{t}"' for t in non_adhoc_jira_types)
        jql = (
            f"project = {project} AND issuetype in ({types_str}) "
            f"AND statusCategory != Done"
        )

    try:
        raw_issues = gateway.search(
            jql,
            fields=["key", "status", "issuetype", qc_field_id],
        )
    except Exception as exc:
        msg = f"Tìm kiếm issues thất bại: {exc}"
        raise BackfillSearchError(msg) from exc

    candidate_issues: list[dict[str, Any]] = []
    bug_set = set(cfg.issue_types.get("bug", []))
    story_set = set(cfg.issue_types.get("story", []))
    adhoc_set = set(cfg.issue_types.get("adhoc", []))

    for raw in raw_issues:
        f_dict = raw.get("fields", {})
        key = raw.get("key") or str(raw.get("id"))

        if f_dict.get(qc_field_id):
            continue

        itype_obj = f_dict.get("issuetype")
        itype_name = itype_obj.get("name") if isinstance(itype_obj, dict) else None

        logical_type: str | None = None
        if itype_name:
            if "bug" in type_keys and itype_name in bug_set:
                logical_type = "bug"
            elif "story" in type_keys and itype_name in story_set:
                logical_type = "story"
            elif "adhoc" in type_keys and itype_name in adhoc_set:
                logical_type = "adhoc"
        else:
            logger.warning("Issue %s không có tên issuetype, bỏ qua.", key)
            continue

        if not logical_type:
            continue

        status_obj = f_dict.get("status", {})
        status_name = (
            status_obj.get("name", "Unknown")
            if isinstance(status_obj, dict)
            else "Unknown"
        )

        if logical_type == "adhoc":
            st_logical = cfg.status_logical(status_name)
            if st_logical not in {"ready_for_qc", "testing"}:
                continue

        candidate_issues.append(
            {
                "key": key,
                "type": itype_name or logical_type,
                "logical_type": logical_type,
                "status": status_name,
            }
        )

    found_by_type: dict[str, int] = {}
    for item in candidate_issues:
        t_name = item["type"]
        found_by_type[t_name] = found_by_type.get(t_name, 0) + 1

    issue_tuples = [
        (item["key"], item["type"], item["status"]) for item in candidate_issues
    ]
    found = len(candidate_issues)

    if dry_run:
        return BackfillResult(
            found=found,
            written=0,
            failed=[],
            user=user,
            issues=issue_tuples,
            found_by_type=found_by_type,
            written_by_type={},
        )

    written = 0
    failed: list[tuple[str, str]] = []
    written_by_type: dict[str, int] = {}

    for i, item in enumerate(candidate_issues):
        key = item["key"]
        t_name = item["type"]
        try:
            gateway.update_fields(
                key,
                {qc_field_id: {"name": user}},
                notify_users=False,
            )
            written += 1
            written_by_type[t_name] = written_by_type.get(t_name, 0) + 1
        except Exception as exc:
            if i == 0:
                raise
            failed.append((key, str(exc)))

    return BackfillResult(
        found=found,
        written=written,
        failed=failed,
        user=user,
        issues=issue_tuples,
        found_by_type=found_by_type,
        written_by_type=written_by_type,
    )


class FreezeSearchError(Exception):
    """Exception raised when searching for issues in freeze fails."""


@dataclass
class FreezeResult:
    found: int
    written: int
    failed: list[tuple[str, str]]
    issues: list[tuple[str, str, str]] = field(default_factory=list)
    found_by_type: dict[str, int] = field(default_factory=dict)
    written_by_type: dict[str, int] = field(default_factory=dict)


def freeze_legacy(
    gateway: JiraGateway,
    cfg: EggsConfig,
    project: str,
    days: int | None = None,
    now: datetime | None = None,
    *,
    dry_run: bool = False,
) -> FreezeResult:
    if days is None:
        days = cfg.thresholds.freeze_days
    frozen_label = cfg.frozen_label
    jql = (
        f"project = {project} AND statusCategory != Done AND "
        f'updated < -{days}d AND (labels is EMPTY OR labels != "{frozen_label}")'
    )

    if now is None:
        tz = ZoneInfo(cfg.timezone)
        now = datetime.now(tz)

    cutoff = now - timedelta(days=days)

    try:
        raw_candidates = gateway.search(
            jql,
            fields=["key", "status", "issuetype", "labels", "updated", "parent"],
        )
    except Exception as exc:
        msg = f"Tìm kiếm candidates đóng băng thất bại: {exc}"
        raise FreezeSearchError(msg) from exc

    valid_candidates: list[dict[str, Any]] = []
    epic_types = set(cfg.issue_types.get("epic", []))

    for raw in raw_candidates:
        f_dict = raw.get("fields", {})
        key = raw.get("key") or str(raw.get("id"))

        labels = f_dict.get("labels", [])
        if frozen_label in labels:
            continue

        st_cat = f_dict.get("status", {}).get("statusCategory", {}).get("name")
        if st_cat == "Done":
            continue
        st_name = f_dict.get("status", {}).get("name", "Unknown")
        if cfg.status_logical(st_name) == "done":
            continue

        updated_str = f_dict.get("updated")
        if updated_str:
            try:
                up_dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                if up_dt >= cutoff:
                    continue
            except (ValueError, TypeError):
                pass

        itype_obj = f_dict.get("issuetype", {})
        itype_name = (
            itype_obj.get("name", "Unknown")
            if isinstance(itype_obj, dict)
            else "Unknown"
        )
        is_epic = itype_name in epic_types
        parent_obj = f_dict.get("parent")
        parent_key = parent_obj.get("key") if isinstance(parent_obj, dict) else None

        valid_candidates.append(
            {
                "key": key,
                "type": itype_name,
                "status": st_name,
                "is_epic": is_epic,
                "parent_key": parent_key,
            }
        )

    if not valid_candidates:
        return FreezeResult(found=0, written=0, failed=[])

    epic_keys = [c["key"] for c in valid_candidates if c["is_epic"]]
    non_epic_keys = [c["key"] for c in valid_candidates if not c["is_epic"]]

    keys_to_skip: set[str] = set()

    subtask_parent_keys = list(
        {c["parent_key"] for c in valid_candidates if c.get("parent_key")}
    )
    if subtask_parent_keys:
        batch_size = 50
        for i in range(0, len(subtask_parent_keys), batch_size):
            chunk = subtask_parent_keys[i : i + batch_size]
            chunk_str = ", ".join(chunk)
            parent_jql = f"key in ({chunk_str}) AND updated >= -{days}d"
            try:
                parent_issues = gateway.search(
                    parent_jql,
                    fields=["key", "updated", "labels"],
                )
            except Exception as exc:
                msg = f"Tìm kiếm active parents cho candidates thất bại: {exc}"
                raise FreezeSearchError(msg) from exc

            for raw in parent_issues:
                f_dict = raw.get("fields", {})
                labels = f_dict.get("labels", [])
                if frozen_label in labels:
                    continue
                p_key = raw.get("key") or str(raw.get("id"))
                if p_key in chunk:
                    u_str = f_dict.get("updated")
                    if u_str:
                        try:
                            u_dt = datetime.fromisoformat(u_str.replace("Z", "+00:00"))
                            if u_dt < cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass
                    for c in valid_candidates:
                        if c.get("parent_key") == p_key:
                            keys_to_skip.add(c["key"])

    if non_epic_keys:
        batch_size = 50
        for i in range(0, len(non_epic_keys), batch_size):
            chunk = non_epic_keys[i : i + batch_size]
            chunk_str = ", ".join(chunk)
            sub_jql = f"parent in ({chunk_str}) AND updated >= -{days}d"
            try:
                sub_issues = gateway.search(
                    sub_jql,
                    fields=["key", "parent", "updated", "labels"],
                )
            except Exception as exc:
                msg = f"Tìm kiếm subtasks cho candidates thất bại: {exc}"
                raise FreezeSearchError(msg) from exc

            for raw in sub_issues:
                f = raw.get("fields", {})
                labels = f.get("labels", [])
                if frozen_label in labels:
                    continue
                parent_key = f.get("parent", {}).get("key")
                if parent_key in chunk:
                    u_str = f.get("updated")
                    if u_str:
                        try:
                            u_dt = datetime.fromisoformat(u_str.replace("Z", "+00:00"))
                            if u_dt < cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass
                    keys_to_skip.add(parent_key)

    if epic_keys:
        epic_link_id = cfg.fields.get("epic_link")
        if epic_link_id:
            cf_match = re.search(r"\d+", epic_link_id)
            cf_id = cf_match.group(0) if cf_match else epic_link_id
            batch_size = 50
            for i in range(0, len(epic_keys), batch_size):
                chunk = epic_keys[i : i + batch_size]
                chunk_str = ", ".join(chunk)
                epic_child_jql = f"cf[{cf_id}] in ({chunk_str}) AND updated >= -{days}d"
                try:
                    child_issues = gateway.search(
                        epic_child_jql,
                        fields=["key", epic_link_id, "updated", "labels"],
                    )
                except Exception as exc:
                    msg = f"Tìm kiếm issues trong Epic thất bại: {exc}"
                    raise FreezeSearchError(msg) from exc

                for raw in child_issues:
                    f = raw.get("fields", {})
                    labels = f.get("labels", [])
                    if frozen_label in labels:
                        continue
                    e_val = f.get(epic_link_id)
                    parent_epic = (
                        e_val
                        if isinstance(e_val, str)
                        else (e_val.get("key") if isinstance(e_val, dict) else None)
                    )
                    if parent_epic in chunk:
                        u_str = f.get("updated")
                        if u_str:
                            try:
                                u_dt = datetime.fromisoformat(
                                    u_str.replace("Z", "+00:00")
                                )
                                if u_dt < cutoff:
                                    continue
                            except (ValueError, TypeError):
                                pass
                        keys_to_skip.add(parent_epic)

    final_candidates = [c for c in valid_candidates if c["key"] not in keys_to_skip]

    found_by_type: dict[str, int] = {}
    for c in final_candidates:
        t_name = c["type"]
        found_by_type[t_name] = found_by_type.get(t_name, 0) + 1

    issue_tuples = [(c["key"], c["type"], c["status"]) for c in final_candidates]
    found = len(final_candidates)

    if dry_run:
        return FreezeResult(
            found=found,
            written=0,
            failed=[],
            issues=issue_tuples,
            found_by_type=found_by_type,
            written_by_type={},
        )

    written = 0
    failed: list[tuple[str, str]] = []
    written_by_type: dict[str, int] = {}

    for i, c in enumerate(final_candidates):
        key = c["key"]
        t_name = c["type"]
        try:
            gateway.add_label(key, frozen_label, notify_users=False)
            written += 1
            written_by_type[t_name] = written_by_type.get(t_name, 0) + 1
        except Exception as exc:
            if i == 0:
                raise
            failed.append((key, str(exc)))

    return FreezeResult(
        found=found,
        written=written,
        failed=failed,
        issues=issue_tuples,
        found_by_type=found_by_type,
        written_by_type=written_by_type,
    )


def unfreeze_stale_issues(
    gateway: JiraGateway,
    cfg: EggsConfig,
    now: datetime,
    days: int | None = None,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Auto-unfreeze issues carrying legacy-frozen label if active after freeze."""
    cutoff_days = days if days is not None else cfg.thresholds.freeze_days
    frozen_label = cfg.frozen_label
    window_days = cfg.thresholds.unfreeze_window_days
    jql = f'labels = "{frozen_label}" AND updated >= -{window_days}d'

    try:
        raw_issues = gateway.search(
            jql,
            fields=["key", "labels", "updated", "comment", "worklog"],
            expand_changelog=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tìm kiếm issues để unfreeze thất bại: %s", exc)
        return []

    bot_user: str | None = None
    try:
        bot_info = gateway.myself() if hasattr(gateway, "myself") else {}
        bot_user = (
            (bot_info.get("name") if isinstance(bot_info, dict) else None)
            or (bot_info.get("key") if isinstance(bot_info, dict) else None)
            or (gateway.current_user() if hasattr(gateway, "current_user") else None)
            or cfg.bot_user
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Không thể xác định danh tính bot user: %s", exc)
        return []

    if not isinstance(bot_user, str) or not bot_user.strip():
        logger.warning(
            "Danh tính bot user trống, bỏ qua unfreeze để tránh gỡ nhầm nhãn."
        )
        return []

    bot_user_lower = bot_user.strip().lower()

    qualifying_parents: list[str] = []

    for raw in raw_issues:
        f_dict = raw.get("fields", {})
        key = raw.get("key") or str(raw.get("id"))

        labels = f_dict.get("labels", [])
        if frozen_label not in labels:
            continue

        changelog_obj = raw.get("changelog", {})
        changelog = changelog_obj.get("histories", [])
        changelog_total = changelog_obj.get("total", len(changelog))
        if changelog_total > len(changelog) and hasattr(gateway, "get_changelog"):
            try:
                full_changelog = gateway.get_changelog(key)
                if full_changelog:
                    changelog = full_changelog
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lấy full changelog cho issue %s thất bại: %s", key, exc)

        comment_obj = f_dict.get("comment", {})
        comments = comment_obj.get("comments", [])
        comment_total = comment_obj.get("total", len(comments))
        comment_max = comment_obj.get("maxResults", len(comments))
        if (comment_total > len(comments) or comment_max < comment_total) and hasattr(
            gateway, "get_comments"
        ):
            try:
                full_comments = gateway.get_comments(key)
                if full_comments:
                    comments = full_comments
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lấy full comments cho issue %s thất bại: %s", key, exc)

        worklog_obj = f_dict.get("worklog", {})
        worklogs = worklog_obj.get("worklogs", [])
        worklog_total = worklog_obj.get("total", len(worklogs))
        if worklog_total > len(worklogs) and hasattr(gateway, "get_worklogs"):
            try:
                full_worklogs = gateway.get_worklogs(key)
                if full_worklogs:
                    worklogs = full_worklogs
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lấy full worklogs cho issue %s thất bại: %s", key, exc)

        freeze_dt: datetime | None = None
        for h in changelog:
            h_time_str = h.get("created")
            if not h_time_str:
                continue
            try:
                h_dt = datetime.fromisoformat(h_time_str.replace("Z", "+00:00"))
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "Issue %s: không thể parse changelog created timestamp '%s': %s",
                    key,
                    h_time_str,
                    exc,
                )
                continue

            for item in h.get("items", []):
                if item.get("field") == "labels":
                    from_labels = set((item.get("fromString") or "").split())
                    to_labels = set((item.get("toString") or "").split())
                    if frozen_label in to_labels and frozen_label not in from_labels:
                        if freeze_dt is None or h_dt > freeze_dt:
                            freeze_dt = h_dt

        if freeze_dt is None:
            logger.warning(
                "Issue %s: không có history thêm nhãn %s, dùng cutoff now - %sd",
                key,
                frozen_label,
                cutoff_days,
            )
            tz = ZoneInfo(cfg.timezone)
            now_aware = now if now.tzinfo is not None else now.replace(tzinfo=tz)
            freeze_dt = now_aware - timedelta(days=cutoff_days)
            has_freeze_entry = False
        else:
            has_freeze_entry = True

        should_unfreeze = False

        # (a) Worklog or comment by someone other than bot user after freeze_dt
        for c in comments:
            c_time_str = c.get("created")
            if not c_time_str:
                continue
            try:
                c_dt = datetime.fromisoformat(c_time_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if c_dt > freeze_dt:
                author = c.get("author", {})
                author_name = (
                    author.get("name") if isinstance(author, dict) else str(author)
                ) or (author.get("key") if isinstance(author, dict) else "")
                if author_name and author_name.strip().lower() != bot_user_lower:
                    should_unfreeze = True
                    break

        if not should_unfreeze:
            for w in worklogs:
                w_time_str = w.get("created") or w.get("started")
                if not w_time_str:
                    continue
                try:
                    w_dt = datetime.fromisoformat(w_time_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if w_dt > freeze_dt:
                    author = w.get("author", {})
                    author_name = (
                        (
                            author.get("name")
                            if isinstance(author, dict)
                            else str(author)
                        )
                        or (author.get("key") if isinstance(author, dict) else "")
                        or (
                            w.get("updateAuthor", {}).get("name")
                            if isinstance(w.get("updateAuthor"), dict)
                            else ""
                        )
                    )
                    if author_name and author_name.strip().lower() != bot_user_lower:
                        should_unfreeze = True
                        break

        # (b) History entry by someone other than bot user without field "Workflow"
        if not should_unfreeze and has_freeze_entry:
            for h in changelog:
                h_time_str = h.get("created")
                if not h_time_str:
                    continue
                try:
                    h_dt = datetime.fromisoformat(h_time_str.replace("Z", "+00:00"))
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "Issue %s: không parse được changelog timestamp '%s': %s",
                        key,
                        h_time_str,
                        exc,
                    )
                    continue
                if h_dt > freeze_dt:
                    author = h.get("author", {})
                    author_name = (
                        author.get("name") if isinstance(author, dict) else str(author)
                    ) or (author.get("key") if isinstance(author, dict) else "")
                    if author_name and author_name.strip().lower() != bot_user_lower:
                        items = h.get("items", [])
                        has_workflow = any(
                            item.get("field") == "Workflow" for item in items
                        )
                        if not has_workflow and items:
                            should_unfreeze = True
                            break

        if should_unfreeze:
            qualifying_parents.append(key)

    if not qualifying_parents:
        return []

    unfrozen_keys: list[str] = []
    already_unfrozen: set[str] = set()
    batch_size = 50

    for i in range(0, len(qualifying_parents), batch_size):
        chunk = qualifying_parents[i : i + batch_size]
        chunk_str = ", ".join(chunk)
        sub_jql = f'parent in ({chunk_str}) AND labels = "{frozen_label}"'
        try:
            frozen_subtasks = gateway.search(
                sub_jql, fields=["key", "labels", "parent"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tìm kiếm subtasks bị đóng băng cho chunk thất bại: %s", exc)
            continue

        subtasks_by_parent: dict[str, list[str]] = {p: [] for p in chunk}
        for st in frozen_subtasks:
            st_key = st.get("key") or str(st.get("id"))
            if not st_key:
                continue
            f_dict = st.get("fields", {}) if isinstance(st.get("fields"), dict) else {}
            p_obj = f_dict.get("parent") or st.get("parent")
            p_key = None
            if isinstance(p_obj, dict):
                p_key = p_obj.get("key")
            elif isinstance(p_obj, str):
                p_key = p_obj
            if p_key and p_key in subtasks_by_parent:
                if st_key not in subtasks_by_parent[p_key]:
                    subtasks_by_parent[p_key].append(st_key)

        for p_key in chunk:
            sub_keys = subtasks_by_parent.get(p_key, [])
            subtasks_success = True
            subtasks_removed_for_parent: list[str] = []

            for st_key in sub_keys:
                if st_key in already_unfrozen:
                    continue
                if dry_run:
                    subtasks_removed_for_parent.append(st_key)
                    already_unfrozen.add(st_key)
                else:
                    try:
                        gateway.remove_label(st_key, frozen_label, notify_users=False)
                        subtasks_removed_for_parent.append(st_key)
                        already_unfrozen.add(st_key)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Gỡ nhãn %s cho sub-task %s thất bại: %s",
                            frozen_label,
                            st_key,
                            exc,
                        )
                        subtasks_success = False
                        break

            if not subtasks_success:
                unfrozen_keys.extend(subtasks_removed_for_parent)
                continue

            unfrozen_keys.extend(subtasks_removed_for_parent)

            if p_key in already_unfrozen:
                continue

            if dry_run:
                unfrozen_keys.append(p_key)
                already_unfrozen.add(p_key)
            else:
                try:
                    gateway.remove_label(p_key, frozen_label, notify_users=False)
                    unfrozen_keys.append(p_key)
                    already_unfrozen.add(p_key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Gỡ nhãn %s cho issue %s thất bại: %s",
                        frozen_label,
                        p_key,
                        exc,
                    )

    return unfrozen_keys


def resolve_fields_cli(gateway: JiraGateway) -> None:
    """Print logical field -> id by matching Jira field names."""
    fields = gateway.get_fields()
    name_to_id = {
        f["name"].strip().lower(): f["id"] for f in fields if "name" in f and "id" in f
    }

    sys.stdout.write("# Resolved Jira custom fields:\nfields:\n")
    for logical, jira_name in LOGICAL_FIELD_NAMES.items():
        field_id = name_to_id.get(jira_name.lower())
        id_str = field_id if field_id else "null  # not found"
        sys.stdout.write(f"  {logical}: {id_str}\n")


def _build_gateway_from_env(cfg: EggsConfig | None = None) -> JiraGateway:
    url = os.environ.get("JIRA_URL") or (cfg.jira_url if cfg else None)
    token = os.environ.get("EGGS_BOT_TOKEN")
    fetcher = JiraFetcher(JiraConfig(url=url, auth_type="pat", personal_token=token))
    return AtlassianGateway(fetcher.jira, cfg=cfg)


def run_chat_test(
    cfg: EggsConfig,
    now: datetime,
    project: str | None = None,
    http_post: Any = None,
) -> int:
    """Send one test message to each configured Google Chat group.

    Prints one line per group (never the URL). Returns 1 if any send failed or
    the requested project has no webhook.
    """
    targets: list[tuple[str, str | None, str]] = []
    if project:
        p = cfg.projects.get(project)
        if not p or not p.chat_webhook:
            sys.stdout.write(f"{project}: chưa có webhook trong webhooks.yaml\n")
            return 1
        targets.append((project, project, p.chat_webhook))
    else:
        if cfg.lead_chat_webhook:
            targets.append(("lead (nhóm chung)", None, cfg.lead_chat_webhook))
        for key, p in sorted(cfg.projects.items()):
            if p.chat_webhook and not p.frozen:
                targets.append((key, key, p.chat_webhook))
    if not targets:
        sys.stdout.write("Chưa khai báo webhook nào (webhooks.yaml).\n")
        return 1

    failed = 0
    for label, key, url in targets:
        text = f"✅ EGGS guardian: kết nối nhóm chat {label} OK ({now:%H:%M %d/%m/%Y})"
        try:
            post_to_chat(url, text, http_post=http_post, project_key=key)
            sys.stdout.write(f"{label}: OK\n")
        except Exception as e:  # noqa: BLE001
            failed += 1
            sys.stdout.write(f"{label}: LỖI {e}\n")
    return 1 if failed else 0


def main(
    argv: list[str] | None = None,
    gateway: JiraGateway | None = None,
    http_post: Any = None,
) -> int:
    """Main CLI entrypoint for eggs-guardian."""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="eggs-guardian", description="EGGS Guardian CLI"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # evaluate
    p_eval = subparsers.add_parser(
        "evaluate",
        help="Tính toán cờ sức khỏe, leo thang, ETA và ghi ngược Jira",
    )
    p_eval.add_argument("--dry-run", action="store_true", help="Không ghi vào Jira")

    # report
    p_rep = subparsers.add_parser("report", help="Xuất báo cáo ngày và gửi Google Chat")
    p_rep.add_argument("--project", help="Chỉ xuất báo cáo cho dự án cụ thể")
    p_rep.add_argument(
        "--dry-run", action="store_true", help="In ra màn hình thay vì gửi"
    )

    # remind
    p_remind = subparsers.add_parser(
        "remind", help="Nhắc nhở người chưa log work hôm nay"
    )
    p_remind.add_argument("--dry-run", action="store_true", help="In ra màn hình")

    # ops-epics
    p_ops = subparsers.add_parser(
        "ops-epics", help="Đảm bảo tồn tại Epic [OPS] tháng hiện tại cho từng dự án"
    )
    p_ops.add_argument("--dry-run", action="store_true", help="Không tạo mới")

    # chat-test
    p_chat = subparsers.add_parser(
        "chat-test",
        help="Gửi một tin thử vào nhóm Lead và nhóm của từng dự án (webhooks.yaml)",
    )
    p_chat.add_argument("--project", help="Chỉ gửi thử cho một dự án")

    # resolve-fields
    subparsers.add_parser(
        "resolve-fields", help="Đọc danh sách trường Jira và ánh xạ tên logic"
    )

    # backfill-qc-owner
    p_backfill = subparsers.add_parser(
        "backfill-qc-owner",
        help="Back-fill trường QC Owner cho các Bug stand-alone, Story và Ad-hoc Task",
    )
    p_backfill.add_argument("--project", required=True, help="Mã dự án (project key)")
    p_backfill.add_argument(
        "--qc-owner",
        help="Jira username của QC Owner (mặc định lấy triage_owner của project)",
    )
    p_backfill.add_argument(
        "--types",
        default="bug,story,adhoc",
        help="Danh sách loại issue cần backfill (bug,story,adhoc; mặc định tất cả)",
    )
    p_backfill.add_argument("--dry-run", action="store_true", help="Không ghi vào Jira")

    # freeze-legacy
    p_freeze = subparsers.add_parser(
        "freeze-legacy",
        help="Gán nhãn legacy-frozen cho các issue cũ không còn cập nhật",
    )
    p_freeze.add_argument("--project", required=True, help="Mã dự án (project key)")
    p_freeze.add_argument(
        "--days",
        type=int,
        default=None,
        help="Số ngày không cập nhật để coi là stale (mặc định: freeze_days)",
    )
    p_freeze.add_argument("--dry-run", action="store_true", help="Không ghi vào Jira")

    args = parser.parse_args(argv)

    # Validate required environment variables after argparse so --help works
    if args.subcommand == "resolve-fields":
        required_env = ("JIRA_URL", "EGGS_BOT_TOKEN")
    else:
        required_env = ("JIRA_URL", "EGGS_BOT_TOKEN", "EGGS_CONFIG")

    missing_env = [var for var in required_env if not os.environ.get(var)]
    if missing_env:
        sys.stderr.write(
            f"Lỗi: Thiếu biến môi trường bắt buộc: {', '.join(missing_env)}\n"
        )
        sys.exit(2)

    if args.subcommand == "resolve-fields":
        cfg = None
        if os.environ.get("EGGS_CONFIG"):
            cfg = load_config(os.environ["EGGS_CONFIG"])
        gw = gateway or _build_gateway_from_env(cfg)
        resolve_fields_cli(gw)
        return 0

    cfg = load_config(os.environ["EGGS_CONFIG"])
    cal = WorkCalendar(cfg.holidays)
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    today = now.date()

    if args.subcommand == "chat-test":
        return run_chat_test(cfg, now, project=args.project, http_post=http_post)

    # Every subcommand exits 0 on a non-working day without calling Jira,
    # except resolve-fields, backfill-qc-owner, and freeze-legacy
    if args.subcommand not in (
        "resolve-fields",
        "backfill-qc-owner",
        "freeze-legacy",
    ) and not cal.is_working_day(today):
        logger.info("Ngày nghỉ (%s), bỏ qua chạy %s.", today, args.subcommand)
        return 0

    if args.subcommand == "backfill-qc-owner":
        if not cfg.fields.get("qc_owner"):
            sys.stderr.write("Lỗi: Cấu hình fields.qc_owner không tồn tại.\n")
            sys.exit(2)
        proj_cfg = cfg.projects.get(args.project)
        user = args.qc_owner or (proj_cfg.triage_owner if proj_cfg else None)
        if not user:
            sys.stderr.write(
                f"Lỗi: Dự án '{args.project}' không có triage_owner "
                "trong cấu hình và không cung cấp --qc-owner.\n"
            )
            sys.exit(2)
        raw_types = [t.strip().lower() for t in args.types.split(",") if t.strip()]
        valid_types = {"bug", "story", "adhoc"}
        invalid = [t for t in raw_types if t not in valid_types]
        if invalid or not raw_types:
            invalid_str = ", ".join(invalid) if invalid else "trống"
            sys.stderr.write(
                f"Lỗi: Loại issue '--types' không hợp lệ: {invalid_str}. "
                "Chỉ chấp nhận: bug, story, adhoc.\n"
            )
            sys.exit(2)

    gw = gateway or _build_gateway_from_env(cfg)

    if args.subcommand == "evaluate":
        res = evaluate(gw, cfg, now, cal, dry_run=args.dry_run)
        issue_count = getattr(res, "issue_count", len({c.split(":")[0] for c in res}))
        field_count = getattr(res, "field_count", 0)
        logger.info(
            "Đã đánh giá xong: %d issues, %d fields thay đổi.",
            issue_count,
            field_count,
        )
        return getattr(res, "exit_code", 0)
    elif args.subcommand == "report":
        return run_report(
            gw,
            cfg,
            now,
            cal,
            project=args.project,
            dry_run=args.dry_run,
            http_post=http_post,
        )
    elif args.subcommand == "remind":
        return run_remind(gw, cfg, now, cal, dry_run=args.dry_run, http_post=http_post)
    elif args.subcommand == "ops-epics":
        res = ensure_ops_epics(gw, cfg, today, cal, dry_run=args.dry_run)
        return getattr(res, "exit_code", 0)
    elif args.subcommand == "backfill-qc-owner":
        try:
            res = backfill_qc_owner(
                gw,
                cfg,
                args.project,
                qc_owner=args.qc_owner,
                types=raw_types,
                dry_run=args.dry_run,
            )
        except BackfillSearchError as e:
            sys.stderr.write(f"Lỗi khi tìm kiếm issues: {e}\n")
            return 1
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"Lỗi khi cập nhật issue đầu tiên: {e}\n")
            return 1

        if args.dry_run:
            sys.stdout.write(
                f"[Dry-run] Tìm thấy {res.found} issue(s) "
                f"cần gán QC Owner '{res.user}':\n"
            )
            for t_name, count in sorted(res.found_by_type.items()):
                sys.stdout.write(f"- {t_name}: {count}\n")
            sys.stdout.write("Danh sách issues:\n")
            for k, t, st in res.issues:
                sys.stdout.write(f"- {k} [{t}]: {st}\n")
            return 0

        sys.stdout.write(
            f"Hoàn thành backfill QC Owner '{res.user}' cho dự án {args.project}:\n"
            f"- Tìm thấy: {res.found}\n"
        )
        for t_name, count in sorted(res.found_by_type.items()):
            sys.stdout.write(f"  * {t_name}: {count}\n")
        sys.stdout.write(f"- Đã ghi: {res.written}\n")
        for t_name, count in sorted(res.written_by_type.items()):
            sys.stdout.write(f"  * {t_name}: {count}\n")
        sys.stdout.write(f"- Thất bại: {len(res.failed)}\n")
        if res.failed:
            for k, err in res.failed:
                sys.stdout.write(f"  * {k}: {err}\n")
            return 1
        return 0
    elif args.subcommand == "freeze-legacy":
        try:
            res = freeze_legacy(
                gw,
                cfg,
                args.project,
                days=args.days if args.days is not None else cfg.thresholds.freeze_days,
                now=now,
                dry_run=args.dry_run,
            )
        except FreezeSearchError as e:
            sys.stderr.write(f"Lỗi khi tìm kiếm issues: {e}\n")
            return 1
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"Lỗi khi cập nhật issue đầu tiên: {e}\n")
            return 1

        label = cfg.frozen_label
        if args.dry_run:
            sys.stdout.write(
                f"[Dry-run] Tìm thấy {res.found} issue(s) cần gán nhãn '{label}':\n"
            )
            for t_name, count in sorted(res.found_by_type.items()):
                sys.stdout.write(f"- {t_name}: {count}\n")
            sys.stdout.write("Danh sách issues:\n")
            for k, t, st in res.issues:
                sys.stdout.write(f"- {k} [{t}]: {st}\n")
            return 0

        sys.stdout.write(
            f"Hoàn thành gắn nhãn '{label}' cho dự án {args.project}:\n"
            f"- Tìm thấy: {res.found}\n"
        )
        for t_name, count in sorted(res.found_by_type.items()):
            sys.stdout.write(f"  * {t_name}: {count}\n")
        sys.stdout.write(f"- Đã ghi: {res.written}\n")
        for t_name, count in sorted(res.written_by_type.items()):
            sys.stdout.write(f"  * {t_name}: {count}\n")
        sys.stdout.write(f"- Thất bại: {len(res.failed)}\n")
        if res.failed:
            for k, err in res.failed:
                sys.stdout.write(f"  * {k}: {err}\n")
            return 1
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
