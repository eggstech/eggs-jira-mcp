from datetime import date, datetime
from zoneinfo import ZoneInfo
import pytest

from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, Thresholds
from mcp_atlassian.eggs.escalation import Escalation
from mcp_atlassian.eggs.forecast import (
    ActorRow,
    EpicMetrics,
    FlowMetrics,
    epic_metrics,
    flow_metrics,
)
from mcp_atlassian.eggs.model import Issue

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@pytest.fixture
def cal():
    return WorkCalendar(holidays=frozenset())


@pytest.fixture
def cfg():
    return EggsConfig(
        lead_user="cuong.huynh",
        thresholds=Thresholds(
            qc_flow_buffer=1,
            unscheduled_window=5,
        ),
    )


def _make_issue(
    key="KEY-1",
    issue_type="dev_task",
    status="in_progress",
    planned_end=None,
    due_date=None,
    components=(),
    parent_key=None,
) -> Issue:
    return Issue(
        key=key,
        project=key.split("-")[0],
        type=issue_type,
        status=status,
        summary=f"Summary for {key}",
        assignee="worker",
        components=components,
        part="BE",
        parent_type="story" if issue_type == "dev_task" else None,
        qc_owner=None,
        parent_key=parent_key,
        epic_key="EPIC-1",
        created=datetime(2026, 11, 1, 8, 0, tzinfo=TZ),
        status_changed_at=None,
        planned_start=None,
        planned_end=planned_end,
        baseline_end=planned_end,
        due_date=due_date,
        estimate_seconds=3600,
        reschedule_status=None,
        reschedule_requester=None,
        proposed_end=None,
        blocked=False,
        blocked_by=None,
        blocked_since=None,
        health_flags=frozenset(),
        health_since=None,
        escalation_level=0,
        escalation_owner=None,
        frozen=False,
    )


def test_flow_forecast_adds_qc_buffer(cal, cfg):
    # Story with two unresolved dev tasks.
    # Max planned_end is Friday Nov 20, 2026.
    # qc_flow_buffer = 1 working day -> next working day is Monday Nov 23, 2026.
    story = _make_issue(
        key="STORY-1",
        issue_type="story",
        status="in_progress",
        due_date=date(2026, 11, 25),
        components=("CH - CỬA HÀNG",),
    )
    task1 = _make_issue(key="DEV-1", issue_type="dev_task", status="in_progress", planned_end=date(2026, 11, 18), parent_key="STORY-1")
    task2 = _make_issue(key="DEV-2", issue_type="dev_task", status="in_progress", planned_end=date(2026, 11, 20), parent_key="STORY-1")

    fm = flow_metrics(story, [task1, task2], cal, cfg)
    assert fm.key == "STORY-1"
    assert fm.due == date(2026, 11, 25)
    assert fm.forecast_end == date(2026, 11, 23)
    assert fm.at_risk is False
    assert fm.slip_days == -2  # 2 days early (Nov 23 vs Nov 25)
    assert fm.unscheduled == 0


def test_flow_at_risk_when_forecast_after_due(cal, cfg):
    # Story due on Friday Nov 20, 2026.
    # Max planned_end is Friday Nov 20 + 1 buffer day = Monday Nov 23.
    # forecast_end (Nov 23) > due (Nov 20) -> at_risk is True, slip_days = +1
    story = _make_issue(
        key="STORY-1",
        issue_type="story",
        status="in_progress",
        due_date=date(2026, 11, 20),
        components=("CH - CỬA HÀNG",),
    )
    task = _make_issue(key="DEV-1", issue_type="dev_task", status="in_progress", planned_end=date(2026, 11, 20), parent_key="STORY-1")

    fm = flow_metrics(story, [task], cal, cfg)
    assert fm.at_risk is True
    assert fm.forecast_end == date(2026, 11, 23)
    assert fm.slip_days == 1


def test_cancelled_children_excluded(cal, cfg):
    # Cancelled child has planned_end Nov 30, but is cancelled -> should be excluded.
    # Active child has planned_end Nov 18 + 1 buffer day = Nov 19.
    story = _make_issue(key="STORY-1", issue_type="story", status="in_progress", due_date=date(2026, 11, 25))
    cancelled_task = _make_issue(key="DEV-CANCEL", issue_type="dev_task", status="cancelled", planned_end=date(2026, 11, 30))
    active_task = _make_issue(key="DEV-ACT", issue_type="dev_task", status="in_progress", planned_end=date(2026, 11, 18))

    fm = flow_metrics(story, [cancelled_task, active_task], cal, cfg)
    assert fm.forecast_end == date(2026, 11, 19)


def test_epic_slip_positive_when_late_negative_when_early(cal, cfg):
    epic_late = _make_issue(key="EPIC-LATE", issue_type="epic", due_date=date(2026, 11, 20))
    # Story forecast is Tuesday Nov 24 -> late by 2 working days (Mon 23, Tue 24)
    story_late = _make_issue(key="STORY-1", issue_type="story")
    flow_late = {
        "STORY-1": FlowMetrics(
            key="STORY-1",
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 24),
            slip_days=2,
            unscheduled=0,
            at_risk=True,
        )
    }

    em_late = epic_metrics(epic_late, [story_late], flow_late, other_items=[], escalations={}, cal=cal)
    assert em_late.forecast_end == date(2026, 11, 24)
    assert em_late.slip_days == 2

    # Early case: epic due Nov 25, forecast Nov 20 -> early by 3 working days (Nov 23, 24, 25)
    epic_early = _make_issue(key="EPIC-EARLY", issue_type="epic", due_date=date(2026, 11, 25))
    story_early = _make_issue(key="STORY-2", issue_type="story")
    flow_early = {
        "STORY-2": FlowMetrics(
            key="STORY-2",
            due=date(2026, 11, 25),
            forecast_end=date(2026, 11, 20),
            slip_days=-3,
            unscheduled=0,
            at_risk=False,
        )
    }
    em_early = epic_metrics(epic_early, [story_early], flow_early, other_items=[], escalations={}, cal=cal)
    assert em_early.forecast_end == date(2026, 11, 20)
    assert em_early.slip_days == -3


def test_actor_rows_bucket_by_status(cal, cfg):
    # Component "CH - CỬA HÀNG":
    # dev: open..in_progress (e.g. open, ready_for_dev, in_progress) -> 3
    # qc: ready_for_qc, testing -> 2
    # ready_for_uat: 2
    # in_uat: client_uat -> 1
    # signed: done -> 1
    # cancelled: 1 (excluded!)
    # Total = 9
    comp = ("CH - CỬA HÀNG",)
    stories = [
        _make_issue(key="S-1", issue_type="story", status="open", components=comp),
        _make_issue(key="S-2", issue_type="story", status="ready_for_dev", components=comp),
        _make_issue(key="S-3", issue_type="story", status="in_progress", components=comp),
        _make_issue(key="S-4", issue_type="story", status="ready_for_qc", components=comp),
        _make_issue(key="S-5", issue_type="story", status="testing", components=comp),
        _make_issue(key="S-6", issue_type="story", status="ready_for_uat", components=comp),
        _make_issue(key="S-7", issue_type="story", status="ready_for_uat", components=comp),
        _make_issue(key="S-8", issue_type="story", status="client_uat", components=comp),
        _make_issue(key="S-9", issue_type="story", status="done", components=comp),
        _make_issue(key="S-10", issue_type="story", status="cancelled", components=comp),
    ]

    flows = {
        f"S-{i}": FlowMetrics(
            key=f"S-{i}",
            due=date(2026, 11, 25),
            forecast_end=date(2026, 11, 26) if i == 3 else date(2026, 11, 20),
            slip_days=1 if i == 3 else 0,
            unscheduled=0,
            at_risk=(i == 3),  # S-3 is at risk
        )
        for i in range(1, 11)
    }

    epic = _make_issue(key="EPIC-1", issue_type="epic", due_date=date(2026, 11, 30))
    em = epic_metrics(epic, stories, flows, other_items=[], escalations={}, cal=cal)

    assert len(em.actors) == 1
    row = em.actors[0]
    assert row.actor == "CH - CỬA HÀNG"
    assert row.actor_code == "CH"
    assert row.total == 9
    assert row.dev == 3
    assert row.qc == 2
    assert row.ready_for_uat == 2
    assert row.in_uat == 1
    assert row.signed == 1
    assert row.at_risk == 1


def test_story_without_component_grouped_separately(cal, cfg):
    # Story without component or with invalid component -> under "(chưa gán Actor)"
    story_no_comp = _make_issue(key="S-NO-COMP", issue_type="story", status="in_progress", components=())
    story_ch = _make_issue(key="S-CH", issue_type="story", status="in_progress", components=("CH - CỬA HÀNG",))
    story_kt = _make_issue(key="S-KT", issue_type="story", status="in_progress", components=("KT - KỸ THUẬT",))

    flows = {
        "S-NO-COMP": FlowMetrics(key="S-NO-COMP", due=None, forecast_end=None, slip_days=None, unscheduled=0, at_risk=False),
        "S-CH": FlowMetrics(key="S-CH", due=None, forecast_end=None, slip_days=None, unscheduled=0, at_risk=False),
        "S-KT": FlowMetrics(key="S-KT", due=None, forecast_end=None, slip_days=None, unscheduled=0, at_risk=False),
    }
    epic = _make_issue(key="EPIC-1", issue_type="epic")
    em = epic_metrics(epic, [story_no_comp, story_kt, story_ch], flows, other_items=[], escalations={}, cal=cal)

    assert len(em.actors) == 3
    assert em.actors[0].actor == "CH - CỬA HÀNG"
    assert em.actors[0].actor_code == "CH"
    assert em.actors[1].actor == "KT - KỸ THUẬT"
    assert em.actors[1].actor_code == "KT"
    assert em.actors[2].actor == "(chưa gán Actor)"
    assert em.actors[2].actor_code is None


def test_unknown_eta_when_l2(cal, cfg):
    epic = _make_issue(key="EPIC-1", issue_type="epic")
    story = _make_issue(key="S-1", issue_type="story", components=("CH - CỬA HÀNG",))
    flows = {"S-1": FlowMetrics(key="S-1", due=None, forecast_end=None, slip_days=None, unscheduled=0, at_risk=False)}

    # Without L2 escalation -> unknown_eta is False
    em_clean = epic_metrics(epic, [story], flows, other_items=[], escalations={}, cal=cal)
    assert em_clean.unknown_eta is False

    # With L2 escalation on an issue in the epic -> unknown_eta is True
    l2_esc = {"S-1": Escalation(level=2, owner="cuong.huynh", since=date(2026, 11, 10))}
    em_l2 = epic_metrics(epic, [story], flows, other_items=[], escalations=l2_esc, cal=cal)
    assert em_l2.unknown_eta is True

    # With L2 escalation on a child passed in children parameter -> unknown_eta is True
    child = _make_issue(key="DEV-CHILD-1", issue_type="dev_task")
    l2_esc_child = {"DEV-CHILD-1": Escalation(level=2, owner="cuong.huynh", since=date(2026, 11, 10))}
    em_child_l2 = epic_metrics(epic, [story], flows, other_items=[], escalations=l2_esc_child, cal=cal, children=[child])
    assert em_child_l2.unknown_eta is True

    # NEGATIVE test: an L2 escalation on an issue of ANOTHER epic in the same project -> unknown_eta is False
    # (Project is "EPIC", another issue is "EPIC-999")
    l2_esc_other = {"EPIC-999": Escalation(level=2, owner="cuong.huynh", since=date(2026, 11, 10))}
    em_other = epic_metrics(epic, [story], flows, other_items=[], escalations=l2_esc_other, cal=cal, children=[child])
    assert em_other.unknown_eta is False

