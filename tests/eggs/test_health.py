from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, Thresholds
from mcp_atlassian.eggs.health import (
    compute_flags,
    detect_interruptions,
    people_not_updated,
)
from mcp_atlassian.eggs.mapping import issue_from_json
from mcp_atlassian.eggs.model import Issue, Worklog

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TODAY = date(2026, 11, 18)  # Wednesday Nov 18, 2026 per plan


@pytest.fixture
def cal():
    return WorkCalendar(holidays=frozenset())


@pytest.fixture
def cfg():
    return EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        timezone="Asia/Ho_Chi_Minh",
        thresholds=Thresholds(
            stale_working_days=2,
            unscheduled_window=5,
            interrupt_window=5,
            escalate_l1_after=1,
            escalate_l2_after=2,
            internal_block_escalate_after=2,
            qc_flow_buffer=1,
        ),
        statuses={
            "open": "Open",
            "analyzing": "Analyzing",
            "ready_for_dev": "Ready For Dev",
            "todo": "DEV To Do",
            "in_progress": "Dev In Progress",
            "ready_for_qc": "QC TO DO",
            "testing": "Test In Progress",
            "ready_for_uat": "Ready For UAT",
            "client_uat": "Wait For Review",
            "done": "Done",
            "cancelled": "Cancelled",
        },
        issue_types={
            "epic": ["Epic"],
            "story": ["Story"],
            "dev_task": ["Dev Task"],
            "bug_sub": ["Bug"],
            "adhoc": ["Ad-hoc Task"],
            "bug": ["Bug stand-alone"],
        },
    )


def _make_issue(
    key="DEV-1",
    issue_type="dev_task",
    status="todo",
    assignee="dev.a",
    planned_end=None,
    baseline_end=None,
    due_date=None,
    parent_key=None,
    status_changed_at=None,
    reschedule_status=None,
    blocked: bool = False,  # noqa: FBT001, FBT002
    blocked_by=None,
    health_flags=frozenset(),
    frozen: bool = False,  # noqa: FBT001, FBT002
    created=None,
    components=(),
    part="BE",
    parent_type="story",
) -> Issue:
    return Issue(
        key=key,
        project=key.split("-")[0],
        type=issue_type,
        status=status,
        summary=f"Summary for {key}",
        assignee=assignee,
        components=components,
        part=part,
        parent_type=parent_type,
        qc_owner=None,
        parent_key=parent_key,
        epic_key="EPIC-1",
        created=created or datetime(2026, 11, 1, 8, 0, tzinfo=TZ),
        status_changed_at=status_changed_at,
        planned_start=date(2026, 11, 10),
        planned_end=planned_end,
        baseline_end=baseline_end,
        due_date=due_date,
        estimate_seconds=3600,
        reschedule_status=reschedule_status,
        reschedule_requester=None,
        proposed_end=None,
        blocked=blocked,
        blocked_by=blocked_by,
        blocked_since=None,
        health_flags=health_flags,
        health_since=None,
        escalation_level=0,
        escalation_owner=None,
        frozen=frozen,
    )


def test_overdue_not_when_reschedule_pending(cal, cfg):
    # planned_end < today (Nov 17 < Nov 18), but reschedule_status == "Chờ duyệt"
    # -> Should have APPROVAL_PENDING, but NOT OVERDUE
    pending_issue = _make_issue(
        key="DEV-1",
        status="in_progress",
        planned_end=date(2026, 11, 17),
        reschedule_status="Chờ duyệt",
    )
    # Another issue planned_end < today without pending reschedule -> OVERDUE
    overdue_issue = _make_issue(
        key="DEV-2",
        status="in_progress",
        planned_end=date(2026, 11, 17),
        reschedule_status=None,
    )

    flags = compute_flags(
        issues=[pending_issue, overdue_issue],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "APPROVAL_PENDING" in flags["DEV-1"]
    assert "OVERDUE" not in flags["DEV-1"]

    assert "OVERDUE" in flags["DEV-2"]
    assert "APPROVAL_PENDING" not in flags["DEV-2"]


def test_reschedule_pending_uses_configured_value(cal):
    custom_cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        reschedule_pending_value="Pending Approval",
    )
    # Issue with custom value should be recognized as APPROVAL_PENDING
    custom_issue = _make_issue(
        key="DEV-CUSTOM",
        status="in_progress",
        planned_end=date(2026, 11, 17),
        reschedule_status="Pending Approval",
    )
    # Issue with old hard-coded value should NOT be recognized as APPROVAL_PENDING
    old_issue = _make_issue(
        key="DEV-OLD",
        status="in_progress",
        planned_end=date(2026, 11, 17),
        reschedule_status="Chờ duyệt",
    )
    flags = compute_flags(
        issues=[custom_issue, old_issue],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=custom_cfg,
        interruptions={},
    )
    assert "APPROVAL_PENDING" in flags["DEV-CUSTOM"]
    assert "OVERDUE" not in flags["DEV-CUSTOM"]
    assert "APPROVAL_PENDING" not in flags["DEV-OLD"]
    assert "OVERDUE" in flags["DEV-OLD"]


def test_stale_after_two_working_days(cal, cfg):
    # Status changed to in_progress on Monday Nov 16 at 09:00.
    # Today is Wednesday Nov 18.
    # working_days_between(Nov 16, Nov 18) == 2 >= stale_working_days (2)
    # No worklogs -> STALE
    issue = _make_issue(
        key="DEV-1",
        status="in_progress",
        status_changed_at=datetime(2026, 11, 16, 9, 0, tzinfo=TZ),
        planned_end=date(2026, 11, 25),
    )
    flags = compute_flags(
        issues=[issue],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "STALE" in flags["DEV-1"]

    # If there is a recent worklog on Tuesday Nov 17, working_days_between(Nov 17, Nov 18) == 1 < 2 -> NOT STALE
    wl = Worklog(
        issue_key="DEV-1",
        author="dev.a",
        started=datetime(2026, 11, 17, 10, 0, tzinfo=TZ),
        seconds=3600,
    )
    flags_with_wl = compute_flags(
        issues=[issue],
        worklogs=[wl],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "STALE" not in flags_with_wl["DEV-1"]


def test_stale_ignores_weekend(cal, cfg):
    # Status changed to in_progress on Friday Nov 13.
    # Over the weekend (Nov 14, 15), working days do not advance.
    # On Monday Nov 16: working_days_between(Nov 13, Nov 16) == 1 (< 2) -> NOT STALE
    issue = _make_issue(
        key="DEV-1",
        status="in_progress",
        status_changed_at=datetime(2026, 11, 13, 17, 0, tzinfo=TZ),
        planned_end=date(2026, 11, 25),
    )
    flags_monday = compute_flags(
        issues=[issue],
        worklogs=[],
        stories={},
        today=date(2026, 11, 16),
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "STALE" not in flags_monday["DEV-1"]

    # On Tuesday Nov 17: working_days_between(Nov 13, Nov 17) == 2 (>= 2) -> STALE
    flags_tuesday = compute_flags(
        issues=[issue],
        worklogs=[],
        stories={},
        today=date(2026, 11, 17),
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "STALE" in flags_tuesday["DEV-1"]


def test_unscheduled_dev_task_story_due_soon(cal, cfg):
    # Dev task in todo with no planned_end.
    # Parent story has due_date on Nov 20 (within 5 working days of Nov 18).
    story = _make_issue(
        key="STORY-1",
        issue_type="story",
        status="ready_for_dev",
        due_date=date(2026, 11, 20),
    )
    dev_task = _make_issue(
        key="DEV-1",
        issue_type="dev_task",
        status="todo",
        planned_end=None,
        parent_key="STORY-1",
    )
    flags = compute_flags(
        issues=[dev_task],
        worklogs=[],
        stories={"STORY-1": story},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "UNSCHEDULED" in flags["DEV-1"]

    # If story due date is far away (> 5 working days, e.g. Dec 10), not unscheduled yet
    story_far = _make_issue(
        key="STORY-2",
        issue_type="story",
        status="ready_for_dev",
        due_date=date(2026, 12, 10),
    )
    dev_task_far = _make_issue(
        key="DEV-2",
        issue_type="dev_task",
        status="todo",
        planned_end=None,
        parent_key="STORY-2",
    )
    flags_far = compute_flags(
        issues=[dev_task_far],
        worklogs=[],
        stories={"STORY-2": story_far},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "UNSCHEDULED" not in flags_far["DEV-2"]


def test_unscheduled_todo_branch_applies_only_to_dev_task(cal, cfg):
    story = _make_issue(
        key="STORY-1",
        issue_type="story",
        status="ready_for_dev",
        due_date=date(2026, 11, 20),
    )
    adhoc_task = _make_issue(
        key="ADHOC-1",
        issue_type="adhoc",
        status="todo",
        planned_end=None,
        parent_key="STORY-1",
    )
    bug_task = _make_issue(
        key="BUG-1",
        issue_type="bug",
        status="todo",
        planned_end=None,
        parent_key="STORY-1",
    )
    flags = compute_flags(
        issues=[adhoc_task, bug_task],
        worklogs=[],
        stories={"STORY-1": story},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "UNSCHEDULED" not in flags["ADHOC-1"]
    assert "UNSCHEDULED" not in flags["BUG-1"]


def test_unscheduled_dev_task_when_story_due_in_past(cal, cfg):
    # Story due date in the past still counts as within the window for its todo dev_tasks
    story = _make_issue(
        key="STORY-PAST",
        issue_type="story",
        status="ready_for_dev",
        due_date=date(2026, 11, 10),  # In the past relative to TODAY (Nov 18)
    )
    dev_task = _make_issue(
        key="DEV-PAST",
        issue_type="dev_task",
        status="todo",
        planned_end=None,
        parent_key="STORY-PAST",
    )
    flags = compute_flags(
        issues=[dev_task],
        worklogs=[],
        stories={"STORY-PAST": story},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "UNSCHEDULED" in flags["DEV-PAST"]


def test_unscheduled_migrated_in_progress(cal, cfg):
    # An issue that is past todo (e.g. in_progress) without baseline_end -> UNSCHEDULED
    migrated_task = _make_issue(
        key="DEV-1",
        issue_type="dev_task",
        status="in_progress",
        planned_end=date(2026, 11, 25),
        baseline_end=None,  # No baseline
    )
    flags = compute_flags(
        issues=[migrated_task],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "UNSCHEDULED" in flags["DEV-1"]


def test_story_without_due_after_ready_is_unscheduled(cal, cfg):
    # Story in ready_for_dev without due_date -> UNSCHEDULED
    story_ready = _make_issue(
        key="STORY-1",
        issue_type="story",
        status="ready_for_dev",
        due_date=None,
    )
    # Story in open or analyzing without due_date -> NOT UNSCHEDULED
    story_open = _make_issue(
        key="STORY-2",
        issue_type="story",
        status="open",
        due_date=None,
    )
    story_analyzing = _make_issue(
        key="STORY-3",
        issue_type="story",
        status="analyzing",
        due_date=None,
    )
    flags = compute_flags(
        issues=[story_ready, story_open, story_analyzing],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "UNSCHEDULED" in flags["STORY-1"]
    assert "UNSCHEDULED" not in flags["STORY-2"]
    assert "UNSCHEDULED" not in flags["STORY-3"]


def test_story_in_analyzed_status_without_due_date_not_unscheduled(cal, cfg):
    # Live Jira status name is 'Analyzed'
    cfg.statuses["analyzing"] = "Analyzed"
    raw_story = {
        "key": "STORY-ANALYZED",
        "fields": {
            "summary": "Story Analyzed",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Analyzed"},
            "duedate": None,
        },
    }
    story = issue_from_json(raw_story, cfg)
    assert story.status == "analyzing"
    flags = compute_flags(
        issues=[story],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert "UNSCHEDULED" not in flags["STORY-ANALYZED"]


def test_own_flow_bug_is_not_interruption(cal, cfg):
    # dev.a has a dev_task under STORY-1
    # dev.a also has a bug_sub under STORY-1 that entered in_progress
    # Because dev.a owns a dev_task under the same Story, the bug_sub is NOT an interruption
    dev_task = _make_issue(
        key="DEV-1",
        issue_type="dev_task",
        status="in_progress",
        assignee="dev.a",
        parent_key="STORY-1",
        status_changed_at=datetime(2026, 11, 10, 9, 0, tzinfo=TZ),
        planned_end=date(2026, 11, 20),
    )
    flow_bug = _make_issue(
        key="BUG-1",
        issue_type="bug_sub",
        status="in_progress",
        assignee="dev.a",
        parent_key="STORY-1",
        status_changed_at=datetime(2026, 11, 17, 10, 0, tzinfo=TZ),
    )

    interruptions = detect_interruptions(
        issues=[dev_task, flow_bug],
        since=datetime(2026, 11, 16, 0, 0, tzinfo=TZ),
        today=TODAY,
        cal=cal,
        cfg=cfg,
    )
    assert "DEV-1" not in interruptions
    assert len(interruptions) == 0


def test_adhoc_interrupts_other_projects_same_person(cal, cfg):
    # dev.a is working on PROJA-1 (in_progress, planned_end Nov 20)
    # dev.a starts an adhoc task PROJB-99 (in_progress after `since`)
    # PROJB-99 interrupts PROJA-1 across projects
    proja_task = _make_issue(
        key="PROJA-1",
        issue_type="dev_task",
        status="in_progress",
        assignee="dev.a",
        status_changed_at=datetime(2026, 11, 10, 9, 0, tzinfo=TZ),
        planned_end=date(2026, 11, 20),
        baseline_end=date(2026, 11, 20),
    )
    projb_adhoc = _make_issue(
        key="PROJB-99",
        issue_type="adhoc",
        status="in_progress",
        assignee="dev.a",
        status_changed_at=datetime(2026, 11, 17, 14, 0, tzinfo=TZ),
    )

    interruptions = detect_interruptions(
        issues=[proja_task, projb_adhoc],
        since=datetime(2026, 11, 16, 0, 0, tzinfo=TZ),
        today=TODAY,
        cal=cal,
        cfg=cfg,
    )
    assert interruptions.get("PROJA-1") == "PROJB-99"

    # And compute_flags applies NEED_RESCHEDULE to PROJA-1
    flags = compute_flags(
        issues=[proja_task, projb_adhoc],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions=interruptions,
    )
    assert "NEED_RESCHEDULE" in flags["PROJA-1"]


def test_detect_interruptions_both_entered_after_since_do_not_flag(cal, cfg):
    # Two tasks for same assignee both entered in_progress after since.
    # Neither should flag the other as an interruption.
    since = datetime(2026, 11, 16, 0, 0, tzinfo=TZ)
    task1 = _make_issue(
        key="ADHOC-1",
        issue_type="adhoc",
        status="in_progress",
        assignee="dev.a",
        status_changed_at=datetime(2026, 11, 17, 10, 0, tzinfo=TZ),
    )
    task2 = _make_issue(
        key="ADHOC-2",
        issue_type="adhoc",
        status="in_progress",
        assignee="dev.a",
        status_changed_at=datetime(2026, 11, 17, 14, 0, tzinfo=TZ),
    )
    interruptions = detect_interruptions([task1, task2], since, TODAY, cal, cfg)
    assert interruptions == {}


def test_own_flow_bug_resolved_owned_dev_task_exempts(cal, cfg):
    # Owner's dev_task Done, bug_sub under same story started -> no interruption
    since = datetime(2026, 11, 16, 0, 0, tzinfo=TZ)
    dev_task_done = _make_issue(
        key="DEV-1",
        issue_type="dev_task",
        status="done",
        assignee="dev.a",
        parent_key="STORY-1",
        planned_end=date(2026, 11, 15),
    )
    other_active_task = _make_issue(
        key="DEV-OTHER",
        issue_type="dev_task",
        status="in_progress",
        assignee="dev.a",
        parent_key="STORY-2",
        status_changed_at=datetime(2026, 11, 10, 9, 0, tzinfo=TZ),
        planned_end=date(2026, 11, 20),
    )
    flow_bug = _make_issue(
        key="BUG-1",
        issue_type="bug_sub",
        status="in_progress",
        assignee="dev.a",
        parent_key="STORY-1",
        status_changed_at=datetime(2026, 11, 17, 10, 0, tzinfo=TZ),
    )
    interruptions = detect_interruptions(
        [dev_task_done, other_active_task, flow_bug], since, TODAY, cal, cfg
    )
    assert interruptions == {}


def test_detect_interruptions_today_required(cal, cfg):
    since = datetime(2026, 11, 16, 0, 0, tzinfo=TZ)
    with pytest.raises(TypeError):
        # Missing required argument today
        detect_interruptions(issues=[], since=since, cal=cal, cfg=cfg)  # type: ignore


def test_need_reschedule_persists_until_cleared(cal, cfg):
    # An issue that previously had NEED_RESCHEDULE in its health_flags,
    # even if not in the current `interruptions` dict, must retain NEED_RESCHEDULE
    issue = _make_issue(
        key="DEV-1",
        status="in_progress",
        health_flags=frozenset({"NEED_RESCHEDULE"}),
        planned_end=date(2026, 11, 25),
        baseline_end=date(2026, 11, 25),
    )
    flags = compute_flags(
        issues=[issue],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},  # empty interruptions
    )
    assert "NEED_RESCHEDULE" in flags["DEV-1"]


def test_people_not_updated_uses_previous_working_day(cal):
    # dev.a and dev.b have tasks in in_progress
    issue_a = _make_issue(key="DEV-1", status="in_progress", assignee="dev.a")
    issue_b = _make_issue(key="DEV-2", status="in_progress", assignee="dev.b")

    # Yesterday was Tuesday Nov 17 (working day before Wednesday Nov 18).
    # dev.a logged work on Tuesday Nov 17.
    # dev.b did NOT log work on Tuesday Nov 17.
    wl_a = Worklog(
        issue_key="DEV-1",
        author="dev.a",
        started=datetime(2026, 11, 17, 16, 0, tzinfo=TZ),
        seconds=3600,
    )
    # dev.b only logged work on Monday Nov 16
    wl_b_old = Worklog(
        issue_key="DEV-2",
        author="dev.b",
        started=datetime(2026, 11, 16, 16, 0, tzinfo=TZ),
        seconds=3600,
    )

    not_updated = people_not_updated(
        issues=[issue_a, issue_b],
        worklogs=[wl_a, wl_b_old],
        today=TODAY,
        cal=cal,
    )
    assert "dev.b" in not_updated
    assert "dev.a" not in not_updated


def test_resolved_frozen_retired_have_no_flags(cal, cfg):
    done_issue = _make_issue(key="DEV-1", status="done", planned_end=date(2026, 11, 1))
    cancelled_issue = _make_issue(
        key="DEV-2", status="cancelled", planned_end=date(2026, 11, 1)
    )
    frozen_issue = _make_issue(
        key="DEV-3", status="in_progress", frozen=True, planned_end=date(2026, 11, 1)
    )
    retired_issue = _make_issue(key="DEV-4", issue_type=None, status="in_progress")

    flags = compute_flags(
        issues=[done_issue, cancelled_issue, frozen_issue, retired_issue],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert flags["DEV-1"] == frozenset()
    assert flags["DEV-2"] == frozenset()
    assert flags["DEV-3"] == frozenset()
    assert flags["DEV-4"] == frozenset()


def test_missing_assignee_and_dates_does_not_crash(cal, cfg):
    # Issue with all dates and assignee missing
    issue = Issue(
        key="DEV-1",
        project="DEV",
        type="dev_task",
        status="todo",
        summary="No dates issue",
        assignee=None,
        components=(),
        part=None,
        parent_type=None,
        qc_owner=None,
        parent_key=None,
        epic_key=None,
        created=None,
        status_changed_at=None,
        planned_start=None,
        planned_end=None,
        baseline_end=None,
        due_date=None,
        estimate_seconds=None,
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
    flags = compute_flags(
        issues=[issue],
        worklogs=[],
        stories={},
        today=TODAY,
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert isinstance(flags["DEV-1"], frozenset)

    # detect_interruptions also doesn't crash
    interruptions = detect_interruptions(
        issues=[issue],
        since=datetime(2026, 11, 16, 0, 0, tzinfo=TZ),
        today=TODAY,
        cal=cal,
        cfg=cfg,
    )
    assert interruptions == {}

    # people_not_updated also doesn't crash
    not_updated = people_not_updated(
        issues=[issue],
        worklogs=[],
        today=TODAY,
        cal=cal,
    )
    assert not_updated == []


def test_report_stale_after_one_working_day(cal, cfg):
    # Report created on Tuesday Nov 17, status is analyzing (or open).
    # On Wednesday Nov 18: working_days_between(Nov 17, Nov 18) == 1 >= triage_working_days (1) -> STALE
    report = _make_issue(
        key="REP-1",
        issue_type="report",
        status="analyzing",
        created=datetime(2026, 11, 17, 9, 0, tzinfo=TZ),
    )
    flags = compute_flags(
        issues=[report],
        worklogs=[],
        stories={},
        today=date(2026, 11, 18),
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert flags["REP-1"] == frozenset({"STALE"})

    # On Tuesday Nov 17 (same day): working_days_between == 0 -> NOT STALE
    flags_same_day = compute_flags(
        issues=[report],
        worklogs=[],
        stories={},
        today=date(2026, 11, 17),
        cal=cal,
        cfg=cfg,
        interruptions={},
    )
    assert flags_same_day["REP-1"] == frozenset()


def test_report_created_friday_not_stale_monday_morning_weekend(cfg):
    # Report created on Friday Nov 13, 2026.
    # Calendar has Monday Nov 16 as a holiday (Review focus 1: holiday Monday counts as zero).
    cal_holiday = WorkCalendar(frozenset({date(2026, 11, 16)}))
    report = _make_issue(
        key="REP-1",
        issue_type="report",
        status="open",
        created=datetime(2026, 11, 13, 17, 30, tzinfo=TZ),
    )

    # Over weekend: Saturday Nov 14 and Sunday Nov 15 -> not stale
    for d in [date(2026, 11, 14), date(2026, 11, 15)]:
        f = compute_flags(
            [report], [], {}, today=d, cal=cal_holiday, cfg=cfg, interruptions={}
        )
        assert "STALE" not in f["REP-1"]

    # Monday Nov 16 (holiday morning): 0 working days elapsed -> not stale
    f_mon = compute_flags(
        [report],
        [],
        {},
        today=date(2026, 11, 16),
        cal=cal_holiday,
        cfg=cfg,
        interruptions={},
    )
    assert "STALE" not in f_mon["REP-1"]

    # Tuesday Nov 17: 1 working day elapsed -> STALE
    f_tue = compute_flags(
        [report],
        [],
        {},
        today=date(2026, 11, 17),
        cal=cal_holiday,
        cfg=cfg,
        interruptions={},
    )
    assert "STALE" in f_tue["REP-1"]
