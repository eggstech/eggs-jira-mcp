from __future__ import annotations

from datetime import date, datetime

from .calendar import WorkCalendar
from .config import EggsConfig
from .model import Issue, Worklog


def detect_interruptions(
    issues: list[Issue],
    since: datetime,
    today: date,
    cal: WorkCalendar,
    cfg: EggsConfig,
) -> dict[str, str]:
    """Detect interruptions across work items.

    Returns dict mapping affected work-item key -> interrupting key.
    A work item that entered in_progress after `since` interrupts the same assignee's
    other unresolved work items that are in_progress or have planned_end within
    interrupt_window working days, when it is bug/adhoc, or was created after those items'
    status_changed_at, or has no baseline_end;
    a bug_sub never interrupts if the same assignee owns a dev_task under the same parent Story.
    """
    interruptions: dict[str, str] = {}

    # Index dev_tasks by (assignee, parent_key) to detect own-flow bugs
    owned_dev_tasks: set[tuple[str, str]] = set()
    for iss in issues:
        if iss.type == "dev_task" and iss.assignee and iss.parent_key:
            owned_dev_tasks.add((iss.assignee, iss.parent_key))

    interrupting_candidates: list[Issue] = []
    for iss in issues:
        if (
            iss.is_work_item
            and not iss.is_resolved
            and not iss.frozen
            and iss.status == "in_progress"
            and iss.assignee
            and iss.status_changed_at
            and iss.status_changed_at >= since
        ):
            # A bug_sub never interrupts if the same assignee owns a dev_task under the same parent Story
            if iss.type == "bug_sub" and iss.parent_key and (iss.assignee, iss.parent_key) in owned_dev_tasks:
                continue
            interrupting_candidates.append(iss)

    if not interrupting_candidates:
        return interruptions

    # Group unresolved work items by assignee
    by_assignee: dict[str, list[Issue]] = {}
    for iss in issues:
        if iss.is_work_item and not iss.is_resolved and not iss.frozen and iss.assignee:
            by_assignee.setdefault(iss.assignee, []).append(iss)

    for x in interrupting_candidates:
        peer_issues = by_assignee.get(x.assignee, [])
        for y in peer_issues:
            if y.key == x.key:
                continue

            # Items that themselves entered in_progress after since never get flagged by each other
            if (
                y.status == "in_progress"
                and y.status_changed_at is not None
                and y.status_changed_at >= since
            ):
                continue

            # Check if Y is active/scheduled within window
            is_y_active = False
            if y.status == "in_progress":
                is_y_active = True
            elif y.planned_end is not None:
                days = cal.working_days_between(today, y.planned_end)
                if days <= cfg.thresholds.interrupt_window:
                    is_y_active = True

            if not is_y_active:
                continue

            # Check interruption criteria
            is_interrupting = False
            if x.type in {"bug", "adhoc"}:
                is_interrupting = True
            elif x.baseline_end is None:
                is_interrupting = True
            elif x.created and y.status_changed_at and x.created > y.status_changed_at:
                is_interrupting = True

            if is_interrupting:
                interruptions[y.key] = x.key

    return interruptions


def compute_flags(
    issues: list[Issue],
    worklogs: list[Worklog],
    stories: dict[str, Issue],
    today: date,
    cal: WorkCalendar,
    cfg: EggsConfig,
    interruptions: dict[str, str],
) -> dict[str, frozenset[str]]:
    """Compute health flags for work items and Stories.

    Resolved, frozen, or type is None -> empty.
    Work items:
      - OVERDUE: planned_end < today, unresolved, not 'Chờ duyệt'
      - STALE: status in (in_progress, ready_for_qc, testing) and
               working_days_between(max(status_changed_at, last worklog), today) >= stale_working_days
      - UNSCHEDULED: (status todo and no planned_end and parent Story due within unscheduled_window)
                     or (status past todo and no baseline_end)
      - APPROVAL_PENDING: reschedule_status == 'Chờ duyệt'
      - BLOCKED: issue.blocked is True
      - NEED_RESCHEDULE: key in interruptions or 'NEED_RESCHEDULE' in issue.health_flags
    Stories:
      - UNSCHEDULED: status in ready_for_dev or later, unresolved, no due_date
      - APPROVAL_PENDING: reschedule_status == 'Chờ duyệt'
      - BLOCKED: issue.blocked is True
    """
    flags_by_key: dict[str, frozenset[str]] = {}

    worklogs_by_key: dict[str, list[Worklog]] = {}
    for wl in worklogs:
        worklogs_by_key.setdefault(wl.issue_key, []).append(wl)

    for issue in issues:
        if issue.is_resolved or issue.frozen or not issue.type:
            flags_by_key[issue.key] = frozenset()
            continue

        item_flags: set[str] = set()

        if issue.blocked:
            item_flags.add("BLOCKED")

        if issue.reschedule_status == cfg.reschedule_pending_value:
            item_flags.add("APPROVAL_PENDING")

        if issue.is_work_item:
            # OVERDUE
            is_approval_pending = issue.reschedule_status == cfg.reschedule_pending_value
            if issue.planned_end is not None and issue.planned_end < today and not is_approval_pending:
                item_flags.add("OVERDUE")

            # STALE
            if issue.status in {"in_progress", "ready_for_qc", "testing"}:
                timestamps: list[date] = []
                if issue.status_changed_at:
                    timestamps.append(issue.status_changed_at.date())
                issue_wls = worklogs_by_key.get(issue.key, [])
                if issue_wls:
                    timestamps.append(max(w.started.date() for w in issue_wls))
                if not timestamps and issue.created:
                    timestamps.append(issue.created.date())

                if timestamps:
                    last_activity = max(timestamps)
                    days_inactive = cal.working_days_between(last_activity, today)
                    if days_inactive >= cfg.thresholds.stale_working_days:
                        item_flags.add("STALE")

            # UNSCHEDULED
            if issue.type == "dev_task" and issue.status == "todo" and issue.planned_end is None:
                parent_story = stories.get(issue.parent_key) if issue.parent_key else None
                if parent_story and parent_story.due_date is not None:
                    days_to_due = cal.working_days_between(today, parent_story.due_date)
                    if days_to_due <= cfg.thresholds.unscheduled_window:
                        item_flags.add("UNSCHEDULED")
            elif issue.status in {"in_progress", "ready_for_qc", "testing"} and issue.baseline_end is None:
                item_flags.add("UNSCHEDULED")

            # NEED_RESCHEDULE
            if issue.key in interruptions or "NEED_RESCHEDULE" in issue.health_flags:
                item_flags.add("NEED_RESCHEDULE")

        elif issue.type == "story":
            # UNSCHEDULED: ready_for_dev or later, unresolved, no due_date
            if issue.status not in {"open", "analyzing"} and issue.due_date is None:
                item_flags.add("UNSCHEDULED")

        flags_by_key[issue.key] = frozenset(item_flags)

    return flags_by_key


def people_not_updated(
    issues: list[Issue],
    worklogs: list[Worklog],
    today: date,
    cal: WorkCalendar,
) -> list[str]:
    """Find people with in_progress work items who logged no work on the previous working day."""
    prev_working_day = cal.previous_working_day(today)

    active_assignees: set[str] = set()
    for iss in issues:
        if (
            iss.is_work_item
            and not iss.is_resolved
            and not iss.frozen
            and iss.status == "in_progress"
            and iss.assignee
        ):
            active_assignees.add(iss.assignee)

    if not active_assignees:
        return []

    updated_authors: set[str] = set()
    for wl in worklogs:
        if wl.started.date() == prev_working_day:
            updated_authors.add(wl.author)

    not_updated = active_assignees - updated_authors
    return sorted(not_updated)
