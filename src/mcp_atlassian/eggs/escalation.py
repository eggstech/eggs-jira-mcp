from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .calendar import WorkCalendar
from .config import EggsConfig
from .model import Issue


@dataclass(frozen=True)
class Escalation:
    level: int
    owner: str | None
    since: date


def escalate(
    issue: Issue,
    flags: frozenset[str],
    story: Issue | None,
    epic: Issue | None,
    today: date,
    cal: WorkCalendar,
    cfg: EggsConfig,
) -> Escalation | None:
    """Compute escalation level, owner, and since date for an issue."""
    if not flags:
        return None

    since = issue.health_since if issue.health_since is not None else today
    days = cal.working_days_between(since, today)

    def resolve_owner(preferred: str | None) -> str:
        if preferred and preferred.strip():
            return preferred.strip()
        if epic and epic.assignee and epic.assignee.strip():
            return epic.assignee.strip()
        proj_cfg = cfg.projects.get(issue.project)
        if proj_cfg and proj_cfg.default_lead_ba and proj_cfg.default_lead_ba.strip():
            return proj_cfg.default_lead_ba.strip()
        return cfg.lead_user

    epic_assignee = epic.assignee.strip() if (epic and epic.assignee and epic.assignee.strip()) else None

    # L0 owner candidate
    if "APPROVAL_PENDING" in flags:
        if epic_assignee and issue.reschedule_requester and epic_assignee == issue.reschedule_requester:
            proj_cfg = cfg.projects.get(issue.project)
            default_lead = (
                proj_cfg.default_lead_ba.strip()
                if (proj_cfg and proj_cfg.default_lead_ba and proj_cfg.default_lead_ba.strip())
                else None
            )
            l0_candidate = default_lead or cfg.lead_user
        else:
            l0_candidate = epic_assignee
    elif issue.is_work_item:
        if issue.status in {"ready_for_qc", "testing"}:
            l0_candidate = story.qc_owner if story else None
        else:
            l0_candidate = issue.assignee
    elif issue.type == "story":
        l0_candidate = issue.assignee
    elif issue.type == "report":
        proj_cfg = cfg.projects.get(issue.project)
        l0_candidate = issue.assignee or (proj_cfg.triage_owner if proj_cfg else None)
    else:
        l0_candidate = issue.assignee

    l0_owner = resolve_owner(l0_candidate)

    # L1 owner candidate: epic assignee; if L0 owner was epic assignee -> default_lead_ba
    if epic_assignee and l0_owner == epic_assignee:
        proj_cfg = cfg.projects.get(issue.project)
        default_lead = (
            proj_cfg.default_lead_ba.strip()
            if (proj_cfg and proj_cfg.default_lead_ba and proj_cfg.default_lead_ba.strip())
            else None
        )
        l1_candidate = default_lead or cfg.lead_user
    else:
        l1_candidate = epic_assignee

    if (
        l1_candidate is not None
        and (
            l1_candidate == l0_owner
            or (issue.reschedule_requester and l1_candidate == issue.reschedule_requester)
        )
    ):
        l1_candidate = cfg.lead_user

    l1_owner = resolve_owner(l1_candidate)

    # L2 owner
    l2_owner = cfg.lead_user

    # Level determination
    if flags == frozenset({"BLOCKED"}):
        if issue.blocked_by == cfg.blocked_internal_value:
            block_since = issue.blocked_since if issue.blocked_since is not None else since
            block_days = cal.working_days_between(block_since, today)
            if block_days >= cfg.thresholds.internal_block_escalate_after:
                level = 1
            else:
                level = 0
        else:
            level = 0
    else:
        if days >= cfg.thresholds.escalate_l2_after:
            level = 2
        elif days >= cfg.thresholds.escalate_l1_after:
            level = 1
        else:
            level = 0

    if level == 0:
        owner = l0_owner
    elif level == 1:
        owner = l1_owner
    else:
        owner = l2_owner

    return Escalation(level=level, owner=owner, since=since)
