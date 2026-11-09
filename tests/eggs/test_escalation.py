from datetime import date, datetime
from zoneinfo import ZoneInfo
import pytest

from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig, Thresholds
from mcp_atlassian.eggs.escalation import Escalation, escalate
from mcp_atlassian.eggs.model import Issue

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TODAY = date(2026, 11, 18)  # Wednesday Nov 18


@pytest.fixture
def cal():
    return WorkCalendar(holidays=frozenset())


@pytest.fixture
def cfg():
    return EggsConfig(
        lead_user="cuong.huynh",
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
        projects={
            "PROJ": ProjectConfig(
                key="PROJ",
                default_lead_ba="default.lead.ba",
            )
        },
    )


def _make_issue(
    key="PROJ-1",
    issue_type="dev_task",
    status="todo",
    assignee="dev.worker",
    qc_owner=None,
    health_since=None,
    blocked=False,
    blocked_by=None,
    blocked_since=None,
    reschedule_status=None,
    reschedule_requester=None,
) -> Issue:
    return Issue(
        key=key,
        project=key.split("-")[0],
        type=issue_type,
        status=status,
        summary=f"Summary for {key}",
        assignee=assignee,
        components=(),
        part="BE",
        parent_type="story",
        qc_owner=qc_owner,
        parent_key="PROJ-STORY",
        epic_key="PROJ-EPIC",
        created=datetime(2026, 11, 1, 8, 0, tzinfo=TZ),
        status_changed_at=None,
        planned_start=date(2026, 11, 10),
        planned_end=date(2026, 11, 20),
        baseline_end=date(2026, 11, 20),
        due_date=None,
        estimate_seconds=3600,
        reschedule_status=reschedule_status,
        reschedule_requester=reschedule_requester,
        proposed_end=None,
        blocked=blocked,
        blocked_by=blocked_by,
        blocked_since=blocked_since,
        health_flags=frozenset(),
        health_since=health_since,
        escalation_level=0,
        escalation_owner=None,
        frozen=False,
    )


def test_level_progression_over_working_days(cal, cfg):
    # since = Fri 2026-11-13
    # Mon Nov 16 -> L1
    # Tue Nov 17 -> L2 (owner = cuong.huynh)
    since_date = date(2026, 11, 13)
    epic = _make_issue(key="PROJ-EPIC", issue_type="epic", assignee="epic.owner")
    story = _make_issue(key="PROJ-STORY", issue_type="story", assignee="story.ba")
    dev_task = _make_issue(
        key="PROJ-1",
        issue_type="dev_task",
        status="in_progress",
        assignee="dev.worker",
        health_since=since_date,
    )
    flags = frozenset({"OVERDUE"})

    # Friday 2026-11-13: L0
    esc_fri = escalate(dev_task, flags, story, epic, date(2026, 11, 13), cal, cfg)
    assert esc_fri is not None
    assert esc_fri.level == 0
    assert esc_fri.owner == "dev.worker"
    assert esc_fri.since == since_date

    # Monday 2026-11-16 (1 working day): L1 -> epic assignee
    esc_mon = escalate(dev_task, flags, story, epic, date(2026, 11, 16), cal, cfg)
    assert esc_mon is not None
    assert esc_mon.level == 1
    assert esc_mon.owner == "epic.owner"

    # Tuesday 2026-11-17 (2 working days): L2 -> cuong.huynh
    esc_tue = escalate(dev_task, flags, story, epic, date(2026, 11, 17), cal, cfg)
    assert esc_tue is not None
    assert esc_tue.level == 2
    assert esc_tue.owner == "cuong.huynh"


def test_dev_task_in_qc_owned_by_story_qc_owner(cal, cfg):
    # Dev task in ready_for_qc or testing -> L0 owner is story.qc_owner
    epic = _make_issue(key="PROJ-EPIC", issue_type="epic", assignee="epic.owner")
    story = _make_issue(key="PROJ-STORY", issue_type="story", assignee="story.ba", qc_owner="qc.specialist")
    task_ready_qc = _make_issue(key="PROJ-1", issue_type="dev_task", status="ready_for_qc", assignee="dev.worker")
    task_testing = _make_issue(key="PROJ-2", issue_type="dev_task", status="testing", assignee="dev.worker")

    flags = frozenset({"STALE"})
    esc1 = escalate(task_ready_qc, flags, story, epic, TODAY, cal, cfg)
    assert esc1 is not None
    assert esc1.level == 0
    assert esc1.owner == "qc.specialist"

    esc2 = escalate(task_testing, flags, story, epic, TODAY, cal, cfg)
    assert esc2 is not None
    assert esc2.level == 0
    assert esc2.owner == "qc.specialist"


def test_story_flag_owned_by_story_assignee(cal, cfg):
    # Story flag -> L0 owner is story assignee (BA)
    epic = _make_issue(key="PROJ-EPIC", issue_type="epic", assignee="epic.owner")
    story = _make_issue(key="PROJ-STORY", issue_type="story", status="ready_for_dev", assignee="story.ba")

    flags = frozenset({"UNSCHEDULED"})
    esc = escalate(story, flags, story=None, epic=epic, today=TODAY, cal=cal, cfg=cfg)
    assert esc is not None
    assert esc.level == 0
    assert esc.owner == "story.ba"


def test_self_requested_approval_goes_to_default_lead_ba(cal, cfg):
    # When epic assignee is the reschedule requester, APPROVAL_PENDING goes to default_lead_ba
    epic = _make_issue(key="PROJ-EPIC", issue_type="epic", assignee="ba.lead")
    task = _make_issue(
        key="PROJ-1",
        issue_type="dev_task",
        status="in_progress",
        reschedule_status="Chờ duyệt",
        reschedule_requester="ba.lead",  # same as epic.assignee
    )
    flags = frozenset({"APPROVAL_PENDING"})

    esc = escalate(task, flags, story=None, epic=epic, today=TODAY, cal=cal, cfg=cfg)
    assert esc is not None
    assert esc.level == 0
    assert esc.owner == "default.lead.ba"


def test_missing_owner_falls_back_chain(cal, cfg):
    # Missing assignee falls back to epic assignee -> default_lead_ba -> lead_user
    task_no_assignee = _make_issue(key="PROJ-1", issue_type="dev_task", status="todo", assignee=None)
    epic_no_assignee = _make_issue(key="PROJ-EPIC", issue_type="epic", assignee=None)
    flags = frozenset({"UNSCHEDULED"})

    # Case 1: falls back to default_lead_ba
    esc = escalate(task_no_assignee, flags, story=None, epic=epic_no_assignee, today=TODAY, cal=cal, cfg=cfg)
    assert esc is not None
    assert esc.owner == "default.lead.ba"

    # Case 2: default_lead_ba also None -> falls back to lead_user
    cfg_no_default_ba = EggsConfig(
        lead_user="cuong.huynh",
        thresholds=cfg.thresholds,
        projects={"PROJ": ProjectConfig(key="PROJ", default_lead_ba="")},
    )
    esc2 = escalate(task_no_assignee, flags, story=None, epic=epic_no_assignee, today=TODAY, cal=cal, cfg=cfg_no_default_ba)
    assert esc2 is not None
    assert esc2.owner == "cuong.huynh"


def test_external_block_does_not_escalate(cal, cfg):
    # BLOCKED alone by external party (Khách hàng or Bên thứ ba) stays L0 regardless of days
    epic = _make_issue(key="PROJ-EPIC", issue_type="epic", assignee="epic.owner")
    task = _make_issue(
        key="PROJ-1",
        issue_type="dev_task",
        status="in_progress",
        assignee="dev.worker",
        blocked=True,
        blocked_by="Khách hàng",
        blocked_since=date(2026, 11, 1),
        health_since=date(2026, 11, 1),
    )
    flags = frozenset({"BLOCKED"})

    esc = escalate(task, flags, story=None, epic=epic, today=TODAY, cal=cal, cfg=cfg)
    assert esc is not None
    assert esc.level == 0
    assert esc.owner == "dev.worker"


def test_internal_block_escalates_after_two_days(cal, cfg):
    # BLOCKED alone with blocked_by == "Nội bộ":
    # since Fri 2026-11-13
    # Mon 2026-11-16 (1 working day) -> L0
    # Tue 2026-11-17 (2 working days) -> L1
    # Wed 2026-11-18 (3 working days) -> L1 (caps at L1)
    since_date = date(2026, 11, 13)
    epic = _make_issue(key="PROJ-EPIC", issue_type="epic", assignee="epic.owner")
    task = _make_issue(
        key="PROJ-1",
        issue_type="dev_task",
        status="in_progress",
        assignee="dev.worker",
        blocked=True,
        blocked_by="Nội bộ",
        blocked_since=since_date,
        health_since=since_date,
    )
    flags = frozenset({"BLOCKED"})

    esc_mon = escalate(task, flags, story=None, epic=epic, today=date(2026, 11, 16), cal=cal, cfg=cfg)
    assert esc_mon is not None
    assert esc_mon.level == 0

    esc_tue = escalate(task, flags, story=None, epic=epic, today=date(2026, 11, 17), cal=cal, cfg=cfg)
    assert esc_tue is not None
    assert esc_tue.level == 1
    assert esc_tue.owner == "epic.owner"

    esc_wed = escalate(task, flags, story=None, epic=epic, today=date(2026, 11, 18), cal=cal, cfg=cfg)
    assert esc_wed is not None
    assert esc_wed.level == 1
    assert esc_wed.owner == "epic.owner"


def test_new_flag_uses_today_as_since(cal, cfg):
    # When health_since is None, since should be today and level is 0
    task = _make_issue(
        key="PROJ-1",
        issue_type="dev_task",
        status="in_progress",
        assignee="dev.worker",
        health_since=None,
    )
    flags = frozenset({"OVERDUE"})

    esc = escalate(task, flags, story=None, epic=None, today=TODAY, cal=cal, cfg=cfg)
    assert esc is not None
    assert esc.since == TODAY
    assert esc.level == 0
