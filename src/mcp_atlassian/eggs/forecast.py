from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from .calendar import WorkCalendar
from .config import EggsConfig
from .escalation import Escalation
from .model import Issue


@dataclass(frozen=True)
class FlowMetrics:
    key: str
    due: date | None
    forecast_end: date | None
    slip_days: int | None
    unscheduled: int
    at_risk: bool


@dataclass(frozen=True)
class ActorRow:
    actor: str
    actor_code: str | None
    total: int
    dev: int
    qc: int
    ready_for_uat: int
    in_uat: int
    signed: int
    at_risk: int


@dataclass(frozen=True)
class EpicMetrics:
    key: str
    due: date | None
    forecast_end: date | None
    slip_days: int | None
    unscheduled: int
    unknown_eta: bool
    actors: list[ActorRow]


def _calculate_slip_days(
    forecast_end: date | None, due: date | None, cal: WorkCalendar
) -> int | None:
    """Calculate signed working days slip: positive = late, negative = early."""
    if forecast_end is None or due is None:
        return None
    if forecast_end > due:
        return cal.working_days_between(due, forecast_end)
    elif forecast_end < due:
        return -cal.working_days_between(forecast_end, due)
    else:
        return 0


def flow_metrics(
    story: Issue,
    children: list[Issue],
    cal: WorkCalendar,
    cfg: EggsConfig,
) -> FlowMetrics:
    """Calculate metrics and forecast for a single flow (Story)."""
    due = story.due_date

    unresolved_children = [c for c in children if not c.is_resolved and not c.frozen]

    unscheduled = sum(1 for c in unresolved_children if c.planned_end is None)

    planned_ends = [
        c.planned_end for c in unresolved_children if c.planned_end is not None
    ]

    if planned_ends:
        max_end = max(planned_ends)
        forecast_end = cal.add_working_days(max_end, cfg.thresholds.qc_flow_buffer)
    else:
        forecast_end = None

    at_risk = bool(forecast_end and due and forecast_end > due)
    slip_days = _calculate_slip_days(forecast_end, due, cal)

    return FlowMetrics(
        key=story.key,
        due=due,
        forecast_end=forecast_end,
        slip_days=slip_days,
        unscheduled=unscheduled,
        at_risk=at_risk,
    )


def epic_metrics(
    epic: Issue,
    stories: list[Issue],
    flow: dict[str, FlowMetrics],
    other_items: list[Issue],
    escalations: dict[str, Escalation],
    cal: WorkCalendar,
    children: Sequence[Issue] = (),
) -> EpicMetrics:
    """Calculate metrics and forecast for an epic."""
    due = epic.due_date

    # Forecast end = max(flow forecasts,
    # planned_end of unresolved adhoc/bug in the epic)
    dates_to_compare: list[date] = []
    for s in stories:
        f = flow.get(s.key)
        if f and f.forecast_end is not None:
            dates_to_compare.append(f.forecast_end)

    for item in other_items:
        if not item.is_resolved and not item.frozen and item.planned_end is not None:
            dates_to_compare.append(item.planned_end)

    if dates_to_compare:
        forecast_end = max(dates_to_compare)
    else:
        forecast_end = None

    slip_days = _calculate_slip_days(forecast_end, due, cal)

    # Unscheduled count
    unscheduled = 0
    for s in stories:
        f = flow.get(s.key)
        if f:
            unscheduled += f.unscheduled
    for item in other_items:
        if not item.is_resolved and not item.frozen and item.planned_end is None:
            unscheduled += 1

    # unknown_eta if any issue in the epic has an L2 escalation
    known_epic_keys = (
        {epic.key}
        | {s.key for s in stories}
        | {item.key for item in other_items}
        | {c.key for c in children}
    )
    unknown_eta = any(
        esc.level == 2 for k, esc in escalations.items() if k in known_epic_keys
    )

    # Group stories by Actor
    actors_map: dict[str, dict[str, int]] = {}
    actor_code_map: dict[str, str | None] = {}

    for s in stories:
        if s.status == "cancelled":
            continue

        if s.actor_code:
            actor_name = s.components[0]
            actor_code = s.actor_code
        else:
            actor_name = "(chưa gán Actor)"
            actor_code = None

        actor_code_map[actor_name] = actor_code

        if actor_name not in actors_map:
            actors_map[actor_name] = {
                "total": 0,
                "dev": 0,
                "qc": 0,
                "ready_for_uat": 0,
                "in_uat": 0,
                "signed": 0,
                "at_risk": 0,
            }

        counts = actors_map[actor_name]
        counts["total"] += 1

        if s.status in {"open", "analyzing", "ready_for_dev", "todo", "in_progress"}:
            counts["dev"] += 1
        elif s.status in {"ready_for_qc", "testing"}:
            counts["qc"] += 1
        elif s.status == "ready_for_uat":
            counts["ready_for_uat"] += 1
        elif s.status == "client_uat":
            counts["in_uat"] += 1
        elif s.status == "done":
            counts["signed"] += 1

        f = flow.get(s.key)
        if f and f.at_risk:
            counts["at_risk"] += 1

    # Sort actors: by actor code, with the unassigned bucket last
    sorted_actor_names = sorted(
        actors_map.keys(),
        key=lambda name: (
            1 if actor_code_map.get(name) is None else 0,
            actor_code_map.get(name) or "",
        ),
    )

    actor_rows = [
        ActorRow(
            actor=name,
            actor_code=actor_code_map.get(name),
            total=actors_map[name]["total"],
            dev=actors_map[name]["dev"],
            qc=actors_map[name]["qc"],
            ready_for_uat=actors_map[name]["ready_for_uat"],
            in_uat=actors_map[name]["in_uat"],
            signed=actors_map[name]["signed"],
            at_risk=actors_map[name]["at_risk"],
        )
        for name in sorted_actor_names
    ]

    return EpicMetrics(
        key=epic.key,
        due=due,
        forecast_end=forecast_end,
        slip_days=slip_days,
        unscheduled=unscheduled,
        unknown_eta=unknown_eta,
        actors=actor_rows,
    )
