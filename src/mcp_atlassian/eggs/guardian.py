from __future__ import annotations

import argparse
import calendar
import logging
import os
import sys
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

GUARDIAN_LOGICAL_FIELDS: frozenset[str] = frozenset({
    "health_flags",
    "health_since",
    "escalation_level",
    "escalation_owner",
    "forecast_end",
    "slip_days",
    "unscheduled_count",
})

LOGICAL_FIELD_NAMES: dict[str, str] = {
    "planned_start": "Planned Start",
    "planned_end": "Planned End",
    "epic_link": "Epic Link",
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


def evaluate(
    gateway: JiraGateway,
    cfg: EggsConfig,
    now: datetime,
    cal: WorkCalendar,
    dry_run: bool = False,  # noqa: FBT001, FBT002
) -> list[str]:
    """Evaluate health flags, escalations and forecasts, writing updates to Jira.

    - Guardian writes ONLY the 7 guardian fields with override_screen_security=True.
    - Adds comment [bot_user] Đã xử lý: <flag> when a flag is cleared.
    - Idempotent: writes only on change.
    - Never transitions issues.
    """
    tz = ZoneInfo(cfg.timezone)
    today = now.astimezone(tz).date()
    since = now - timedelta(minutes=70)
    snapshot = build_snapshot(gateway, cfg, today, cal, since=since)

    allowed_guardian_field_ids = {
        cfg.fields[f] for f in GUARDIAN_LOGICAL_FIELDS if f in cfg.fields
    }
    changes: list[str] = []

    for key, iss in snapshot.issues.items():
        if iss.type is None or iss.frozen:
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

        # Comment on cleared flags
        cleared_flags = sorted(iss.health_flags - new_flags)
        for flag in cleared_flags:
            comment_body = f"[{cfg.bot_user}] Đã xử lý: {flag}"
            if not dry_run:
                gateway.add_comment(key, comment_body)
            changes.append(f"{key}: cleared flag {flag}")

        # Write fields
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
                gateway.update_fields(key, payload, override_screen_security=True)
            changes.append(f"{key}: updated {list(field_changes.keys())}")

    return changes


def ensure_ops_epics(
    gateway: JiraGateway,
    cfg: EggsConfig,
    today: date,
    cal: WorkCalendar,
    dry_run: bool = False,  # noqa: FBT001, FBT002
) -> list[str]:
    """Ensure the current month's OPS epic exists per active project."""
    created: list[str] = []
    expected_summary = cfg.ops_epic_summary.format(
        month=today.month, year=today.year
    )
    epic_type_name = cfg.issue_types["epic"][0]

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
            }
            if not dry_run:
                key = gateway.create_issue(create_payload)
                created.append(
                    f"Created OPS epic {key} for {proj_key}: {expected_summary}"
                )
            else:
                created.append(
                    f"[dry-run] Would create OPS epic for {proj_key}: "
                    f"{expected_summary}"
                )

    return created


def run_report(
    gateway: JiraGateway,
    cfg: EggsConfig,
    now: datetime,
    cal: WorkCalendar,
    project: str | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002
    http_post: Any = None,
) -> None:
    """Render daily report and post to Google Chat webhooks."""
    tz = ZoneInfo(cfg.timezone)
    today = now.astimezone(tz).date()
    since = datetime.combine(
        cal.previous_working_day(today), time.min, tzinfo=tz
    )
    snapshot = build_snapshot(gateway, cfg, today, cal, since=since)

    if project:
        if project not in cfg.projects:
            sys.stderr.write(f"Dự án không tồn tại trong cấu hình: {project}\n")
            return
        text = render_report(snapshot, cfg, project=project)
        if dry_run:
            sys.stdout.write(text + "\n")
        else:
            webhook = cfg.projects[project].chat_webhook
            if webhook:
                post_to_chat(
                    webhook, text, http_post=http_post, project_key=project
                )
    else:
        # Lead global report
        lead_text = render_report(snapshot, cfg, project=None)
        if dry_run:
            sys.stdout.write("=== BÁO CÁO TOÀN DIỆN (LEAD) ===\n")
            sys.stdout.write(lead_text + "\n")
        else:
            if cfg.lead_chat_webhook:
                post_to_chat(
                    cfg.lead_chat_webhook,
                    lead_text,
                    http_post=http_post,
                    project_key=None,
                )

        # Per-project reports
        for proj_key, p in sorted(cfg.projects.items()):
            if not p.frozen and p.chat_webhook:
                proj_text = render_report(snapshot, cfg, project=proj_key)
                if dry_run:
                    sys.stdout.write(f"=== BÁO CÁO DỰ ÁN {proj_key} ===\n")
                    sys.stdout.write(proj_text + "\n")
                else:
                    post_to_chat(
                        p.chat_webhook,
                        proj_text,
                        http_post=http_post,
                        project_key=proj_key,
                    )


def run_remind(
    gateway: JiraGateway,
    cfg: EggsConfig,
    now: datetime,
    cal: WorkCalendar,
    dry_run: bool = False,  # noqa: FBT001, FBT002
    http_post: Any = None,
) -> None:
    """Send end-of-day reminder to people without worklogs today."""
    tz = ZoneInfo(cfg.timezone)
    today = now.astimezone(tz).date()
    since = datetime.combine(
        cal.add_working_days(today, -5), time.min, tzinfo=tz
    )
    snapshot = build_snapshot(gateway, cfg, today, cal, since=since)

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
                    post_to_chat(
                        p.chat_webhook, msg, http_post=http_post, project_key=proj_key
                    )

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

    if lead_sections or cfg.lead_chat_webhook:
        lead_body = (
            "\n\n".join(lead_sections)
            if lead_sections
            else "Mọi người đã cập nhật công việc đầy đủ hôm nay."
        )
        lead_msg = f"{lead_body}\n\n{suffix}"
        if dry_run:
            sys.stdout.write(f"=== Remind Lead ===\n{lead_msg}\n")
        else:
            if cfg.lead_chat_webhook:
                post_to_chat(
                    cfg.lead_chat_webhook,
                    lead_msg,
                    http_post=http_post,
                    project_key=None,
                )


def resolve_fields_cli(gateway: JiraGateway) -> None:
    """Print logical field -> id by matching Jira field names."""
    fields = gateway.get_fields()
    name_to_id = {
        f["name"].strip().lower(): f["id"]
        for f in fields
        if "name" in f and "id" in f
    }

    sys.stdout.write("# Resolved Jira custom fields:\nfields:\n")
    for logical, jira_name in LOGICAL_FIELD_NAMES.items():
        field_id = name_to_id.get(jira_name.lower())
        id_str = field_id if field_id else "null  # not found"
        sys.stdout.write(f"  {logical}: {id_str}\n")


def _build_gateway_from_env(cfg: EggsConfig) -> JiraGateway:
    url = os.environ.get("JIRA_URL") or cfg.jira_url
    token = os.environ.get("EGGS_BOT_TOKEN")
    fetcher = JiraFetcher(
        JiraConfig(url=url, auth_type="pat", personal_token=token)
    )
    return AtlassianGateway(fetcher.jira, cfg=cfg)


def main(
    argv: list[str] | None = None,
    gateway: JiraGateway | None = None,
    http_post: Any = None,
) -> int:
    """Main CLI entrypoint for eggs-guardian."""
    if argv is None:
        argv = sys.argv[1:]

    # Validate required environment variables
    missing_env = [
        var for var in ("JIRA_URL", "EGGS_BOT_TOKEN", "EGGS_CONFIG")
        if not os.environ.get(var)
    ]
    if missing_env:
        sys.stderr.write(
            f"Lỗi: Thiếu biến môi trường bắt buộc: {', '.join(missing_env)}\n"
        )
        sys.exit(2)

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
    p_rep = subparsers.add_parser(
        "report", help="Xuất báo cáo ngày và gửi Google Chat"
    )
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

    # resolve-fields
    subparsers.add_parser(
        "resolve-fields", help="Đọc danh sách trường Jira và ánh xạ tên logic"
    )

    args = parser.parse_args(argv)

    cfg = load_config(os.environ["EGGS_CONFIG"])
    cal = WorkCalendar(cfg.holidays)
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    today = now.date()

    # Every subcommand exits 0 on a non-working day without calling Jira,
    # except resolve-fields
    if args.subcommand != "resolve-fields":
        if not cal.is_working_day(today):
            logger.info("Ngày nghỉ (%s), bỏ qua chạy %s.", today, args.subcommand)
            return 0

    gw = gateway or _build_gateway_from_env(cfg)

    if args.subcommand == "evaluate":
        evaluate(gw, cfg, now, cal, dry_run=args.dry_run)
    elif args.subcommand == "report":
        run_report(
            gw,
            cfg,
            now,
            cal,
            project=args.project,
            dry_run=args.dry_run,
            http_post=http_post,
        )
    elif args.subcommand == "remind":
        run_remind(gw, cfg, now, cal, dry_run=args.dry_run, http_post=http_post)
    elif args.subcommand == "ops-epics":
        ensure_ops_epics(gw, cfg, today, cal, dry_run=args.dry_run)
    elif args.subcommand == "resolve-fields":
        resolve_fields_cli(gw)

    return 0


if __name__ == "__main__":
    sys.exit(main())
