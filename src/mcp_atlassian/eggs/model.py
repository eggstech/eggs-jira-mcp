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
    contributors: tuple[str, ...]
    qc_owner: str | None
    parent_key: str | None
    epic_key: str | None
    created: datetime
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

    @property
    def is_resolved(self) -> bool:
        """Return True if issue is resolved (done or cancelled)."""
        return self.status in {"done", "cancelled"}

    @property
    def people(self) -> frozenset[str]:
        """Return set of people involved: assignee ∪ contributors."""
        result = set(self.contributors)
        if self.assignee:
            result.add(self.assignee)
        return frozenset(result)


@dataclass(frozen=True)
class Worklog:
    issue_key: str
    author: str
    started: datetime
    seconds: int
