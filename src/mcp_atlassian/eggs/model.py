from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Issue:
    key: str
    project: str
    type: str
    status: str
    summary: str
    assignee: str | None
    components: tuple[str, ...]
    part: str | None
    parent_type: str | None
    qc_owner: str | None
    parent_key: str | None
    epic_key: str | None
    created: datetime | None
    status_changed_at: datetime | None
    planned_start: date | None
    planned_end: date | None
    baseline_end: date | None
    due_date: date | None
    estimate_seconds: int | None
    reschedule_status: str | None
    reschedule_requester: str | None
    proposed_end: date | None
    blocked: bool
    blocked_by: str | None
    blocked_since: date | None
    health_flags: frozenset[str]
    health_since: date | None
    escalation_level: int
    escalation_owner: str | None
    frozen: bool
    report_source: str | None = None
    reported_by: str | None = None
    reported_at: datetime | None = None
    resolution: str | None = None

    @property
    def is_resolved(self) -> bool:
        """Return True if issue is resolved (done or cancelled)."""
        return self.status in {"done", "cancelled"}

    @property
    def is_work_item(self) -> bool:
        """Return True if issue is a work item (dev_task, bug_sub, bug, adhoc)."""
        return self.type in {"dev_task", "bug_sub", "bug", "adhoc"}

    @property
    def actor_code(self) -> str | None:
        """Prefix before ' - ' of the single component, else None."""
        if len(self.components) != 1:
            return None
        comp = self.components[0]
        if " - " in comp:
            return comp.split(" - ", 1)[0].strip()
        return None


@dataclass(frozen=True)
class Worklog:
    issue_key: str
    author: str
    started: datetime
    seconds: int
