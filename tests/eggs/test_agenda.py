from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo
import pytest

from mcp_atlassian.eggs.agenda import (
    AgendaItem,
    INTENTS,
    Intent,
    SuggestedAction,
    build_agenda,
    intents_for,
    mentioned_keys,
)
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig, Thresholds
from mcp_atlassian.eggs.escalation import Escalation
from mcp_atlassian.eggs.forecast import ActorRow, EpicMetrics, FlowMetrics
from mcp_atlassian.eggs.identity import Identity
from mcp_atlassian.eggs.model import Issue, Worklog
from mcp_atlassian.eggs.report import Snapshot


def _make_issue(
    key: str,
    issue_type: str = "dev_task",
    status: str = "in_progress",
    assignee: str | None = "alice",
    summary: str = "Test issue",
    project: str | None = None,
    parent_key: str | None = None,
    epic_key: str | None = None,
    qc_owner: str | None = None,
    reschedule_status: str | None = None,
    proposed_end: date | None = None,
    actor_code: str | None = None,
) -> Issue:
    proj = project or key.split("-")[0]
    comps = (f"{actor_code} - Component",) if actor_code else ()
    return Issue(
        key=key,
        project=proj,
        type=issue_type,
        status=status,
        summary=summary,
        assignee=assignee,
        components=comps,
        part=None,
        parent_type=None,
        qc_owner=qc_owner,
        parent_key=parent_key,
        epic_key=epic_key,
        created=datetime(2026, 11, 1, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")),
        status_changed_at=None,
        planned_start=None,
        planned_end=None,
        baseline_end=None,
        due_date=None,
        estimate_seconds=None,
        reschedule_status=reschedule_status,
        reschedule_requester=None,
        proposed_end=proposed_end,
        blocked=False,
        blocked_by=None,
        blocked_since=None,
        health_flags=frozenset(),
        health_since=None,
        escalation_level=0,
        escalation_owner=None,
        frozen=False,
    )


@pytest.fixture
def cal():
    return WorkCalendar(holidays=frozenset())


@pytest.fixture
def cfg():
    return EggsConfig(
        lead_user="cuong.huynh",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh"),
            "CP": ProjectConfig(key="CP", default_lead_ba="cuong.huynh"),
        },
        roles={"lead_ba": "Lead-BA", "qc": "QC", "dev": "Developers"},
        thresholds=Thresholds(end_day_hour=17),
    )


def test_suggested_action_prefills_key_and_lists_missing():
    action = SuggestedAction(
        tool="eggs_schedule",
        args={"key": "RFID-10", "project": "RFID"},
        missing=["planned_start", "planned_end"],
    )
    assert action.tool == "eggs_schedule"
    assert action.args["key"] == "RFID-10"
    assert action.args["project"] == "RFID"
    assert action.missing == ["planned_start", "planned_end"]


def test_intents_for_dev_excludes_decide_reschedule():
    identity = Identity(
        username="dev1",
        display_name="Dev 1",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    intents = intents_for(identity)
    names = {it.name for it in intents}
    assert "schedule" in names
    assert "start" in names
    assert "submit_qc" in names
    assert "log_work" in names
    assert "decide_reschedule" not in names
    assert "flow_reschedule" not in names
    assert "uat" not in names


def test_intents_for_lead_includes_status_and_report():
    identity = Identity(
        username="cuong.huynh",
        display_name="Cuong Huynh",
        roles={"RFID": {"lead_ba"}},
        is_lead=True,
    )
    intents = intents_for(identity)
    names = {it.name for it in intents}
    assert "workstream_status" in names
    assert "daily_report" in names
    assert "pending_approvals" in names
    assert "decide_reschedule" in names


def test_mentioned_keys_filters_unknown_projects():
    text = "xong RFID-12 và ABC-3, RFID-12"
    assert mentioned_keys(text, {"RFID"}) == ["RFID-12"]
    assert mentioned_keys("ABC-1 và XYZ-2", {"RFID"}) == []
    assert mentioned_keys("HMK-1, HMK-2, HMK-1", {"HMK"}) == ["HMK-1", "HMK-2"]


def test_agenda_orders_by_priority(cfg, cal):
    identity = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"dev", "qc", "lead_ba"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # Priority 1: Flagged item owned by alice (level 1)
    iss1 = _make_issue("RFID-1", summary="Flagged item", status="in_progress")
    # Priority 2: Reschedule pending awaiting alice
    iss2 = _make_issue("RFID-2", summary="Reschedule item", reschedule_status="Chờ duyệt", proposed_end=date(2026, 11, 20))
    # Priority 3: User report assigned to alice in analyzing
    iss3 = _make_issue("RFID-3", issue_type="report", status="analyzing", summary="User report")
    # Priority 4: QC queue item under story where alice is qc_owner
    iss4_story = _make_issue("RFID-4S", issue_type="story", status="dev_in_progress", qc_owner="alice")
    iss4 = _make_issue("RFID-4", issue_type="dev_task", status="ready_for_qc", parent_key="RFID-4S", summary="Ready for QC")
    # Priority 7: Own item in progress
    iss7 = _make_issue("RFID-7", summary="In progress item", status="in_progress", assignee="alice")

    issues = {
        iss1.key: iss1,
        iss2.key: iss2,
        iss3.key: iss3,
        iss4_story.key: iss4_story,
        iss4.key: iss4,
        iss7.key: iss7,
    }
    flags = {
        "RFID-1": frozenset({"STALE"}),
        "RFID-2": frozenset({"APPROVAL_PENDING"}),
    }
    escalations = {
        "RFID-1": Escalation(level=1, owner="alice", since=date(2026, 11, 8)),
        "RFID-2": Escalation(level=0, owner="alice", since=date(2026, 11, 9)),
    }

    snapshot = Snapshot(
        issues=issues,
        flags=flags,
        escalations=escalations,
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )

    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    priorities = [it.priority for it in agenda]
    assert priorities == [1, 2, 3, 4, 7]
    assert [it.key for it in agenda] == ["RFID-1", "RFID-2", "RFID-3", "RFID-4", "RFID-7"]


def test_agenda_l1_owner_sees_escalated_item(cfg, cal):
    identity = Identity(
        username="bob",
        display_name="Bob",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    iss = _make_issue("RFID-10", summary="Overdue task", status="in_progress", assignee="alice")
    issues = {iss.key: iss}
    flags = {"RFID-10": frozenset({"OVERDUE"})}
    escalations = {"RFID-10": Escalation(level=1, owner="bob", since=date(2026, 11, 8))}

    snapshot = Snapshot(
        issues=issues,
        flags=flags,
        escalations=escalations,
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )

    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    assert agenda[0].key == "RFID-10"
    assert agenda[0].priority == 1


def test_agenda_reschedule_waiting_for_me(cfg, cal):
    identity = Identity(
        username="lead_ba_user",
        display_name="Lead BA",
        roles={"RFID": {"lead_ba"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    iss = _make_issue(
        "RFID-20",
        summary="Task needing reschedule",
        reschedule_status="Chờ duyệt",
        proposed_end=date(2026, 11, 25),
    )
    issues = {iss.key: iss}
    flags = {"RFID-20": frozenset({"APPROVAL_PENDING"})}
    escalations = {"RFID-20": Escalation(level=0, owner="lead_ba_user", since=date(2026, 11, 9))}

    snapshot = Snapshot(
        issues=issues,
        flags=flags,
        escalations=escalations,
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )

    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    assert agenda[0].key == "RFID-20"
    assert agenda[0].priority == 2
    assert any(act.tool == "eggs_decide_reschedule" for act in agenda[0].actions)


def test_agenda_qc_queue_uses_story_qc_owner(cfg, cal):
    identity = Identity(
        username="qc_alice",
        display_name="QC Alice",
        roles={"RFID": {"qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    story1 = _make_issue("RFID-S1", issue_type="story", status="dev_in_progress", qc_owner="qc_alice")
    task1 = _make_issue("RFID-T1", issue_type="dev_task", status="ready_for_qc", parent_key="RFID-S1", assignee="dev_bob")

    story2 = _make_issue("RFID-S2", issue_type="story", status="dev_in_progress", qc_owner="other_qc")
    task2 = _make_issue("RFID-T2", issue_type="dev_task", status="ready_for_qc", parent_key="RFID-S2", assignee="dev_bob")

    issues = {
        story1.key: story1,
        task1.key: task1,
        story2.key: story2,
        task2.key: task2,
    }

    snapshot = Snapshot(
        issues=issues,
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )

    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    assert agenda[0].key == "RFID-T1"
    assert agenda[0].priority == 4
    assert any(act.tool == "eggs_qc_start" for act in agenda[0].actions)


def test_agenda_end_day_after_17_without_worklog(cfg, cal):
    identity = Identity(
        username="dev1",
        display_name="Dev 1",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    today = date(2026, 11, 9)

    # 16:59 -> absent
    now_1659 = datetime(2026, 11, 9, 16, 59, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    snap_empty = Snapshot(issues={}, flags={}, escalations={}, flows={}, epics={}, not_updated=[], today=today, worklogs=[])
    agenda_1659 = build_agenda(snap_empty, identity, cfg, now_1659, cal)
    assert not any(it.priority == 6 for it in agenda_1659)

    # 17:00 without worklog -> present
    now_1700 = datetime(2026, 11, 9, 17, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    agenda_1700 = build_agenda(snap_empty, identity, cfg, now_1700, cal)
    assert any(it.priority == 6 for it in agenda_1700)

    # 17:00 with worklog today -> absent
    worklog_today = Worklog(
        issue_key="RFID-1",
        author="dev1",
        started=datetime(2026, 11, 9, 14, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")),
        seconds=3600,
    )
    snap_with_wl = Snapshot(issues={}, flags={}, escalations={}, flows={}, epics={}, not_updated=[], today=today, worklogs=[worklog_today])
    agenda_with_wl = build_agenda(snap_with_wl, identity, cfg, now_1700, cal)
    assert not any(it.priority == 6 for it in agenda_with_wl)


def test_agenda_excludes_projects_without_role(cal):
    cfg_multi = EggsConfig(
        lead_user="lead_boss",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="lead_boss"),
            "OTHER": ProjectConfig(key="OTHER", default_lead_ba="lead_boss"),
        },
        roles={"lead_ba": "Lead-BA", "qc": "QC", "dev": "Developers"},
    )
    # Non-lead user has role only in RFID
    identity = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    rfid_task = _make_issue("RFID-1", status="in_progress", assignee="alice")
    other_task = _make_issue("OTHER-1", status="in_progress", assignee="alice")
    issues = {rfid_task.key: rfid_task, other_task.key: other_task}

    snapshot = Snapshot(
        issues=issues,
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )

    agenda = build_agenda(snapshot, identity, cfg_multi, now, cal)
    keys = [it.key for it in agenda]
    assert "RFID-1" in keys
    assert "OTHER-1" not in keys

    # Lead user sees priority 1-2 items escalated to them even in OTHER
    lead_identity = Identity(
        username="lead_boss",
        display_name="Boss",
        roles={"RFID": {"lead_ba"}},  # no role in OTHER
        is_lead=True,
    )
    esc_other = Escalation(level=2, owner="lead_boss", since=date(2026, 11, 9))
    snapshot_lead = Snapshot(
        issues=issues,
        flags={"OTHER-1": frozenset({"STALE"})},
        escalations={"OTHER-1": esc_other},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    lead_agenda = build_agenda(snapshot_lead, lead_identity, cfg_multi, now, cal)
    lead_keys = [it.key for it in lead_agenda]
    assert "OTHER-1" in lead_keys
