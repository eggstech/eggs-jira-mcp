from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from mcp_atlassian.eggs.actions import Actions
from mcp_atlassian.eggs.agenda import (
    INTENTS,
    allowed_actions,
    build_agenda,
    intents_for,
    mentioned_keys,
)
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig, Thresholds
from mcp_atlassian.eggs.escalation import Escalation
from mcp_atlassian.eggs.forecast import ActorRow, EpicMetrics
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
    parent_type: str | None = None,
    epic_key: str | None = None,
    qc_owner: str | None = None,
    reschedule_status: str | None = None,
    proposed_end: date | None = None,
    actor_code: str | None = None,
    part: str | None = None,
    estimate_seconds: int | None = None,
    *,
    blocked: bool = False,
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
        part=part,
        parent_type=parent_type,
        qc_owner=qc_owner,
        parent_key=parent_key,
        epic_key=epic_key,
        created=datetime(2026, 11, 1, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")),
        status_changed_at=None,
        planned_start=None,
        planned_end=None,
        baseline_end=None,
        due_date=None,
        estimate_seconds=estimate_seconds,
        reschedule_status=reschedule_status,
        reschedule_requester=None,
        proposed_end=proposed_end,
        blocked=blocked,
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
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(
                key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"
            ),
            "CP": ProjectConfig(
                key="CP", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"
            ),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
        thresholds=Thresholds(end_day_hour=17),
    )


def test_suggested_actions_by_flag_kind(cfg, cal):
    identity = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"dev", "lead_ba", "qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # 1. APPROVAL_PENDING (esc level 1) -> eggs_decide_reschedule (missing ["approve", "note"])
    iss_ap = _make_issue("RFID-1", summary="Approval pending", status="in_progress")
    snap_ap = Snapshot(
        issues={"RFID-1": iss_ap},
        flags={"RFID-1": frozenset({"APPROVAL_PENDING"})},
        escalations={
            "RFID-1": Escalation(level=1, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_ap = build_agenda(snap_ap, identity, cfg, now, cal)
    assert len(agenda_ap) == 1
    assert len(agenda_ap[0].actions) == 1
    assert agenda_ap[0].actions[0].tool == "eggs_decide_reschedule"
    assert agenda_ap[0].actions[0].args == {"key": "RFID-1"}
    assert agenda_ap[0].actions[0].missing == ["approve", "note"]

    # 2. Story flags (AT_RISK, UNSCHEDULED on a story) -> eggs_flow_reschedule (missing ["proposed_due", "reason"])
    iss_st_risk = _make_issue(
        "RFID-2", issue_type="story", summary="Story at risk", status="in_progress"
    )
    snap_st_risk = Snapshot(
        issues={"RFID-2": iss_st_risk},
        flags={"RFID-2": frozenset({"AT_RISK"})},
        escalations={
            "RFID-2": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_st = build_agenda(snap_st_risk, identity, cfg, now, cal)
    assert len(agenda_st) == 1
    assert agenda_st[0].actions[0].tool == "eggs_flow_reschedule"
    assert agenda_st[0].actions[0].args == {"key": "RFID-2"}
    assert agenda_st[0].actions[0].missing == ["proposed_due", "reason"]

    # 3. Work item OVERDUE (dev_task under story) -> eggs_submit_qc + eggs_finish + eggs_request_reschedule (missing ["proposed_end", "reason"])
    iss_wi_od = _make_issue(
        "RFID-3",
        issue_type="dev_task",
        summary="Overdue task",
        status="in_progress",
        parent_type="story",
    )
    snap_wi_od = Snapshot(
        issues={"RFID-3": iss_wi_od},
        flags={"RFID-3": frozenset({"OVERDUE"})},
        escalations={
            "RFID-3": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_od = build_agenda(snap_wi_od, identity, cfg, now, cal)
    assert len(agenda_od) == 1
    assert len(agenda_od[0].actions) == 3
    assert agenda_od[0].actions[0].tool == "eggs_submit_qc"
    assert agenda_od[0].actions[0].args == {"key": "RFID-3"}
    assert agenda_od[0].actions[0].missing == []
    assert agenda_od[0].actions[1].tool == "eggs_finish"
    assert agenda_od[0].actions[1].args == {"key": "RFID-3"}
    assert agenda_od[0].actions[1].missing == []
    assert agenda_od[0].actions[2].tool == "eggs_request_reschedule"
    assert agenda_od[0].actions[2].args == {"key": "RFID-3"}
    assert agenda_od[0].actions[2].missing == ["proposed_end", "reason"]

    # Bug sub under story OVERDUE -> eggs_submit_qc only + eggs_request_reschedule
    iss_bug_sub = _make_issue(
        "RFID-3B",
        issue_type="bug_sub",
        parent_type="story",
        summary="Bug sub",
        status="in_progress",
    )
    snap_bug_sub = Snapshot(
        issues={"RFID-3B": iss_bug_sub},
        flags={"RFID-3B": frozenset({"OVERDUE"})},
        escalations={
            "RFID-3B": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_bug_sub = build_agenda(snap_bug_sub, identity, cfg, now, cal)
    assert [a.tool for a in agenda_bug_sub[0].actions] == [
        "eggs_submit_qc",
        "eggs_request_reschedule",
    ]

    # Bug stand-alone OVERDUE -> eggs_submit_qc only + eggs_request_reschedule
    iss_bug_sa = _make_issue(
        "RFID-3SA",
        issue_type="bug",
        parent_type=None,
        summary="Bug standalone",
        status="in_progress",
    )
    snap_bug_sa = Snapshot(
        issues={"RFID-3SA": iss_bug_sa},
        flags={"RFID-3SA": frozenset({"OVERDUE"})},
        escalations={
            "RFID-3SA": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_bug_sa = build_agenda(snap_bug_sa, identity, cfg, now, cal)
    assert [a.tool for a in agenda_bug_sa[0].actions] == [
        "eggs_submit_qc",
        "eggs_request_reschedule",
    ]
    assert agenda_bug_sa[0].actions[0].missing == ["qc_owner"]

    iss_bug_sa_qc = _make_issue(
        "RFID-3SAQ",
        issue_type="bug",
        parent_type=None,
        summary="Bug standalone with QC",
        status="in_progress",
        qc_owner="bob",
    )
    snap_bug_sa_qc = Snapshot(
        issues={"RFID-3SAQ": iss_bug_sa_qc},
        flags={"RFID-3SAQ": frozenset({"OVERDUE"})},
        escalations={
            "RFID-3SAQ": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_bug_sa_qc = build_agenda(snap_bug_sa_qc, identity, cfg, now, cal)
    assert agenda_bug_sa_qc[0].actions[0].missing == []

    # Work item NEED_RESCHEDULE on adhoc item -> eggs_finish + eggs_submit_qc (missing qc_owner) + eggs_request_reschedule
    iss_adhoc = _make_issue(
        "RFID-4", issue_type="adhoc", summary="Adhoc task", status="in_progress"
    )
    snap_adhoc = Snapshot(
        issues={"RFID-4": iss_adhoc},
        flags={"RFID-4": frozenset({"NEED_RESCHEDULE"})},
        escalations={
            "RFID-4": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_adhoc = build_agenda(snap_adhoc, identity, cfg, now, cal)
    assert len(agenda_adhoc) == 1
    assert len(agenda_adhoc[0].actions) == 3
    assert agenda_adhoc[0].actions[0].tool == "eggs_finish"
    assert agenda_adhoc[0].actions[0].args == {"key": "RFID-4"}
    assert agenda_adhoc[0].actions[0].missing == []
    assert agenda_adhoc[0].actions[1].tool == "eggs_submit_qc"
    assert agenda_adhoc[0].actions[1].args == {"key": "RFID-4"}
    assert agenda_adhoc[0].actions[1].missing == ["qc_owner"]
    assert agenda_adhoc[0].actions[2].tool == "eggs_request_reschedule"
    assert agenda_adhoc[0].actions[2].args == {"key": "RFID-4"}
    assert agenda_adhoc[0].actions[2].missing == ["proposed_end", "reason"]

    # Adhoc with qc_owner already set -> eggs_submit_qc missing is empty
    iss_adhoc_qc = _make_issue(
        "RFID-4Q",
        issue_type="adhoc",
        summary="Adhoc task with QC",
        status="in_progress",
        qc_owner="bob",
    )
    snap_adhoc_qc = Snapshot(
        issues={"RFID-4Q": iss_adhoc_qc},
        flags={"RFID-4Q": frozenset({"NEED_RESCHEDULE"})},
        escalations={
            "RFID-4Q": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_adhoc_qc = build_agenda(snap_adhoc_qc, identity, cfg, now, cal)
    assert agenda_adhoc_qc[0].actions[1].missing == []

    # Dev task under adhoc keeps eggs_finish only (no submit_qc)
    iss_dev_under_adhoc = _make_issue(
        "RFID-4S",
        issue_type="dev_task",
        parent_type="adhoc",
        summary="Sub under adhoc",
        status="in_progress",
    )
    snap_dev_under_adhoc = Snapshot(
        issues={"RFID-4S": iss_dev_under_adhoc},
        flags={"RFID-4S": frozenset({"NEED_RESCHEDULE"})},
        escalations={
            "RFID-4S": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_sub = build_agenda(snap_dev_under_adhoc, identity, cfg, now, cal)
    assert [a.tool for a in agenda_sub[0].actions] == [
        "eggs_finish",
        "eggs_request_reschedule",
    ]

    # 4. Work item UNSCHEDULED -> eggs_schedule (missing ["start", "end", "estimate"])
    iss_unsch = _make_issue(
        "RFID-5", issue_type="dev_task", summary="Unscheduled task", status="dev_todo"
    )
    snap_unsch = Snapshot(
        issues={"RFID-5": iss_unsch},
        flags={"RFID-5": frozenset({"UNSCHEDULED"})},
        escalations={
            "RFID-5": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_unsch = build_agenda(snap_unsch, identity, cfg, now, cal)
    assert len(agenda_unsch) == 1
    assert agenda_unsch[0].actions[0].tool == "eggs_schedule"
    assert agenda_unsch[0].actions[0].args == {"key": "RFID-5"}
    assert agenda_unsch[0].actions[0].missing == ["start", "end", "estimate"]

    # 5. STALE -> eggs_log_work (missing ["time_spent"])
    iss_stale = _make_issue(
        "RFID-6", issue_type="dev_task", summary="Stale task", status="in_progress"
    )
    snap_stale = Snapshot(
        issues={"RFID-6": iss_stale},
        flags={"RFID-6": frozenset({"STALE"})},
        escalations={
            "RFID-6": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_stale = build_agenda(snap_stale, identity, cfg, now, cal)
    assert len(agenda_stale) == 1
    assert agenda_stale[0].actions[0].tool == "eggs_log_work"
    assert agenda_stale[0].actions[0].args == {"key": "RFID-6"}
    assert agenda_stale[0].actions[0].missing == ["time_spent"]

    # 6. BLOCKED -> eggs_unblock (missing [])
    iss_blk = _make_issue(
        "RFID-7",
        issue_type="dev_task",
        summary="Blocked task",
        status="in_progress",
        blocked=True,
    )
    snap_blk = Snapshot(
        issues={"RFID-7": iss_blk},
        flags={"RFID-7": frozenset({"BLOCKED"})},
        escalations={
            "RFID-7": Escalation(level=0, owner="alice", since=date(2026, 11, 8))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda_blk = build_agenda(snap_blk, identity, cfg, now, cal)
    assert len(agenda_blk) == 1
    assert agenda_blk[0].actions[0].tool == "eggs_unblock"
    assert agenda_blk[0].actions[0].args == {"key": "RFID-7"}
    assert agenda_blk[0].actions[0].missing == []


def test_every_intent_tool_is_a_real_action():
    known_read_tools = {
        "eggs_next",
        "eggs_workstream_status",
        "eggs_daily_report",
        "eggs_pending_approvals",
        "eggs_plan_validate",
        "eggs_migrate_epic",
    }
    for it in INTENTS:
        if it.tool is None or it.tool in known_read_tools:
            continue
        method_name = it.tool.removeprefix("eggs_")
        assert hasattr(Actions, method_name), f"Actions has no method {method_name}"
        sig = inspect.signature(getattr(Actions, method_name))
        for param in it.required:
            assert param in sig.parameters, (
                f"Parameter {param} not in Actions.{method_name} parameters: {list(sig.parameters.keys())}"
            )


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
    assert "plan_cards" not in names
    assert "migrate_epic" not in names


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
    assert "plan_cards" in names
    assert "migrate_epic" in names


def test_intents_visible_to_lead_ba_and_not_dev():
    ba_identity = Identity(
        username="ba1",
        display_name="BA 1",
        roles={"RFID": {"lead_ba"}},
        is_lead=False,
    )
    ba_intents = {it.name for it in intents_for(ba_identity)}
    assert "plan_cards" in ba_intents
    assert "migrate_epic" in ba_intents

    dev_identity = Identity(
        username="dev1",
        display_name="Dev 1",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    dev_intents = {it.name for it in intents_for(dev_identity)}
    assert "plan_cards" not in dev_intents
    assert "migrate_epic" not in dev_intents


def test_mentioned_keys_filters_unknown_projects():
    text = "xong RFID-12 và ABC-3, RFID-12"
    assert mentioned_keys(text, {"RFID"}) == ["RFID-12"]
    assert mentioned_keys("ABC-1 và XYZ-2", {"RFID"}) == []
    assert mentioned_keys("HMK-1, HMK-2, HMK-1", {"HMK"}) == ["HMK-1", "HMK-2"]
    # Case-insensitive (normalize to upper)
    assert mentioned_keys("xong rfid-12 và Rfid-9", {"RFID"}) == ["RFID-12", "RFID-9"]
    # Project keys may contain underscore
    assert mentioned_keys("xong proj_a-10 và PROJ_A-2", {"PROJ_A"}) == [
        "PROJ_A-10",
        "PROJ_A-2",
    ]


def test_agenda_sorts_keys_naturally(cfg, cal):
    identity = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # Two items in progress for alice: RFID-10 and RFID-9
    iss10 = _make_issue(
        "RFID-10", summary="Item 10", status="in_progress", assignee="alice"
    )
    iss9 = _make_issue(
        "RFID-9", summary="Item 9", status="in_progress", assignee="alice"
    )

    snapshot = Snapshot(
        issues={"RFID-10": iss10, "RFID-9": iss9},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )

    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    # Natural sort must place RFID-9 before RFID-10 (not alphabetical RFID-10 < RFID-9)
    assert [it.key for it in agenda] == ["RFID-9", "RFID-10"]


def test_agenda_orders_by_priority(cfg, cal):
    identity = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"dev", "qc", "lead_ba"}},
        is_lead=False,
    )
    # 17:30 ICT on a Monday -> end-day (priority 6) triggered because no worklog
    now = datetime(2026, 11, 9, 17, 30, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # Priority 1: Flagged item owned by alice (level 1)
    iss1 = _make_issue("RFID-1", summary="Flagged item", status="in_progress")
    # Priority 2: Reschedule pending awaiting alice
    iss2 = _make_issue(
        "RFID-2",
        summary="Reschedule item",
        reschedule_status="Chờ duyệt",
        proposed_end=date(2026, 11, 20),
    )
    # Priority 3: User report assigned to alice in analyzing
    iss3 = _make_issue(
        "RFID-3", issue_type="report", status="analyzing", summary="User report"
    )
    # Priority 4: QC queue item under story where alice is qc_owner
    iss4_story = _make_issue(
        "RFID-4S", issue_type="story", status="dev_in_progress", qc_owner="alice"
    )
    iss4 = _make_issue(
        "RFID-4",
        issue_type="dev_task",
        status="ready_for_qc",
        parent_key="RFID-4S",
        summary="Ready for QC",
    )
    # Priority 5: Epic ready for UAT for lead_ba
    iss5_epic = _make_issue("RFID-5E", issue_type="epic", status="in_progress")
    em5 = EpicMetrics(
        key="RFID-5E",
        due=None,
        forecast_end=None,
        slip_days=None,
        unscheduled=0,
        unknown_eta=False,
        actors=[
            ActorRow(
                actor="CH - Cửa hàng",
                actor_code="CH",
                total=2,
                dev=0,
                qc=0,
                ready_for_uat=2,
                in_uat=0,
                signed=0,
                at_risk=0,
            )
        ],
    )
    # Priority 7: Own item in progress
    iss7 = _make_issue(
        "RFID-7", summary="In progress item", status="in_progress", assignee="alice"
    )

    issues = {
        iss1.key: iss1,
        iss2.key: iss2,
        iss3.key: iss3,
        iss4_story.key: iss4_story,
        iss4.key: iss4,
        iss5_epic.key: iss5_epic,
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
        epics={"RFID-5E": em5},
        not_updated=[],
        today=date(2026, 11, 9),
    )

    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    priorities = [it.priority for it in agenda]
    assert priorities == [1, 2, 3, 4, 5, 6, 7]
    assert [it.key for it in agenda] == [
        "RFID-1",
        "RFID-2",
        "RFID-3",
        "RFID-4",
        "RFID-5E",
        None,
        "RFID-7",
    ]


def test_agenda_priority_5_uses_full_actor_string(cfg, cal):
    identity = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"lead_ba"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    epic = _make_issue("RFID-100", issue_type="epic", status="in_progress")
    em = EpicMetrics(
        key="RFID-100",
        due=None,
        forecast_end=None,
        slip_days=None,
        unscheduled=0,
        unknown_eta=False,
        actors=[
            ActorRow(
                actor="CH - Cửa hàng",
                actor_code="CH",
                total=2,
                dev=0,
                qc=0,
                ready_for_uat=2,
                in_uat=0,
                signed=0,
                at_risk=0,
            )
        ],
    )
    snapshot = Snapshot(
        issues={"RFID-100": epic},
        flags={},
        escalations={},
        flows={},
        epics={"RFID-100": em},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    assert agenda[0].priority == 5
    assert agenda[0].actions[0].args["actor"] == "CH - Cửa hàng"


def test_agenda_priority_1_orders_l2_before_l0(cfg, cal):
    identity = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # RFID-1 is level 0, RFID-2 is level 2
    iss1 = _make_issue("RFID-1", summary="L0 item", status="in_progress")
    iss2 = _make_issue("RFID-2", summary="L2 item", status="in_progress")
    snapshot = Snapshot(
        issues={"RFID-1": iss1, "RFID-2": iss2},
        flags={"RFID-1": frozenset({"STALE"}), "RFID-2": frozenset({"STALE"})},
        escalations={
            "RFID-1": Escalation(level=0, owner="alice", since=date(2026, 11, 9)),
            "RFID-2": Escalation(level=2, owner="alice", since=date(2026, 11, 7)),
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert [it.key for it in agenda] == ["RFID-2", "RFID-1"]


def test_agenda_l1_owner_sees_escalated_item(cfg, cal):
    identity = Identity(
        username="bob",
        display_name="Bob",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    iss = _make_issue(
        "RFID-10", summary="Overdue task", status="in_progress", assignee="alice"
    )
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
    escalations = {
        "RFID-20": Escalation(level=0, owner="lead_ba_user", since=date(2026, 11, 9))
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

    story1 = _make_issue(
        "RFID-S1", issue_type="story", status="dev_in_progress", qc_owner="qc_alice"
    )
    task1 = _make_issue(
        "RFID-T1",
        issue_type="dev_task",
        status="ready_for_qc",
        parent_key="RFID-S1",
        assignee="dev_bob",
    )

    story2 = _make_issue(
        "RFID-S2", issue_type="story", status="dev_in_progress", qc_owner="other_qc"
    )
    task2 = _make_issue(
        "RFID-T2",
        issue_type="dev_task",
        status="ready_for_qc",
        parent_key="RFID-S2",
        assignee="dev_bob",
    )

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


def test_agenda_qc_queue_bug_standalone_routed_to_own_qc_owner(cfg, cal):
    identity = Identity(
        username="qc_alice",
        display_name="QC Alice",
        roles={"RFID": {"qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    bug1 = _make_issue(
        "RFID-B1", issue_type="bug", status="ready_for_qc", qc_owner="qc_alice"
    )
    bug2 = _make_issue(
        "RFID-B2", issue_type="bug", status="ready_for_qc", qc_owner="other_qc"
    )

    issues = {bug1.key: bug1, bug2.key: bug2}
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
    assert agenda[0].key == "RFID-B1"
    assert agenda[0].priority == 4
    assert any(act.tool == "eggs_qc_start" for act in agenda[0].actions)


def test_agenda_qc_queue_bug_sub_missing_parent_not_routed(cfg, cal):
    identity = Identity(
        username="qc_alice",
        display_name="QC Alice",
        roles={"RFID": {"qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # Bug sub with parent not in snapshot has qc_owner=None, so it must not be routed silently
    bug_sub = _make_issue(
        "RFID-BS1",
        issue_type="bug_sub",
        status="ready_for_qc",
        parent_key="RFID-MISSING",
    )

    issues = {bug_sub.key: bug_sub}
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
    assert len(agenda) == 0


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
    snap_empty = Snapshot(
        issues={},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=today,
        worklogs=[],
    )
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
    snap_with_wl = Snapshot(
        issues={},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=today,
        worklogs=[worklog_today],
    )
    agenda_with_wl = build_agenda(snap_with_wl, identity, cfg, now_1700, cal)
    assert not any(it.priority == 6 for it in agenda_with_wl)


def test_agenda_end_day_timezone_conversion(cfg, cal):
    identity = Identity(
        username="dev1",
        display_name="Dev 1",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    snap = Snapshot(
        issues={},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 17),
        worklogs=[],
    )

    # 2026-11-17 10:00 UTC = 17:00 ICT (Tuesday) -> end-day item present
    now_utc_10 = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    agenda_10utc = build_agenda(snap, identity, cfg, now_utc_10, cal)
    assert any(it.priority == 6 for it in agenda_10utc)

    # 2026-11-17 03:00 UTC = 10:00 ICT (Tuesday) -> end-day item absent
    now_utc_03 = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)
    agenda_03utc = build_agenda(snap, identity, cfg, now_utc_03, cal)
    assert not any(it.priority == 6 for it in agenda_03utc)


def test_agenda_end_day_absent_on_saturday(cfg, cal):
    identity = Identity(
        username="dev1",
        display_name="Dev 1",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    snap = Snapshot(
        issues={},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 21),
        worklogs=[],
    )
    # Saturday 2026-11-21 18:00 ICT -> weekend, absent
    sat_now = datetime(2026, 11, 21, 18, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    agenda = build_agenda(snap, identity, cfg, sat_now, cal)
    assert not any(it.priority == 6 for it in agenda)


def test_agenda_excludes_projects_without_role(cal):
    cfg_multi = EggsConfig(
        lead_user="lead_boss",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(
                key="RFID", default_lead_ba="lead_boss", triage_owner="lead_boss"
            ),
            "OTHER": ProjectConfig(
                key="OTHER", default_lead_ba="lead_boss", triage_owner="lead_boss"
            ),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
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


def test_agenda_priority_7_adhoc_suggests_both_finish_and_submit_qc(cfg, cal):
    identity = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # 1. Adhoc without QC Owner: finish, submit_qc (missing qc_owner), log_work
    adhoc_no_qc = _make_issue(
        "RFID-1", issue_type="adhoc", status="in_progress", assignee="alice"
    )
    # 2. Adhoc with QC Owner: finish, submit_qc (missing=[]), log_work
    adhoc_with_qc = _make_issue(
        "RFID-2",
        issue_type="adhoc",
        status="in_progress",
        assignee="alice",
        qc_owner="bob",
    )
    # 3. Dev task under adhoc: finish, log_work (keeps eggs_finish only)
    sub_under_adhoc = _make_issue(
        "RFID-3",
        issue_type="dev_task",
        parent_type="adhoc",
        status="in_progress",
        assignee="alice",
    )
    # 4. Dev task under story: submit_qc, finish, log_work (suggests BOTH submit_qc and finish)
    dev_under_story = _make_issue(
        "RFID-4",
        issue_type="dev_task",
        parent_type="story",
        status="in_progress",
        assignee="alice",
    )
    # 5. Bug sub under story: submit_qc, log_work (keeps eggs_submit_qc only)
    bug_sub = _make_issue(
        "RFID-5",
        issue_type="bug_sub",
        parent_type="story",
        status="in_progress",
        assignee="alice",
    )
    # 6. Bug stand-alone without QC: submit_qc (missing qc_owner), log_work
    bug_sa = _make_issue(
        "RFID-6",
        issue_type="bug",
        parent_type=None,
        status="in_progress",
        assignee="alice",
    )
    # 7. Bug stand-alone with QC: submit_qc (missing=[]), log_work
    bug_sa_qc = _make_issue(
        "RFID-7",
        issue_type="bug",
        parent_type=None,
        status="in_progress",
        assignee="alice",
        qc_owner="bob",
    )

    issues = {
        adhoc_no_qc.key: adhoc_no_qc,
        adhoc_with_qc.key: adhoc_with_qc,
        sub_under_adhoc.key: sub_under_adhoc,
        dev_under_story.key: dev_under_story,
        bug_sub.key: bug_sub,
        bug_sa.key: bug_sa,
        bug_sa_qc.key: bug_sa_qc,
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
    by_key = {it.key: it for it in agenda}

    # Adhoc without QC Owner
    it1 = by_key["RFID-1"]
    tools1 = [(a.tool, a.missing) for a in it1.actions]
    assert tools1 == [
        ("eggs_finish", []),
        ("eggs_submit_qc", ["qc_owner"]),
        ("eggs_log_work", ["time_spent", "note"]),
    ]

    # Adhoc with QC Owner
    it2 = by_key["RFID-2"]
    tools2 = [(a.tool, a.missing) for a in it2.actions]
    assert tools2 == [
        ("eggs_finish", []),
        ("eggs_submit_qc", []),
        ("eggs_log_work", ["time_spent", "note"]),
    ]

    # Dev task under adhoc
    it3 = by_key["RFID-3"]
    tools3 = [(a.tool, a.missing) for a in it3.actions]
    assert tools3 == [
        ("eggs_finish", []),
        ("eggs_log_work", ["time_spent", "note"]),
    ]

    # Dev task under story
    it4 = by_key["RFID-4"]
    tools4 = [(a.tool, a.missing) for a in it4.actions]
    assert tools4 == [
        ("eggs_submit_qc", []),
        ("eggs_finish", []),
        ("eggs_log_work", ["time_spent", "note"]),
    ]

    # Bug sub under story
    it5 = by_key["RFID-5"]
    tools5 = [(a.tool, a.missing) for a in it5.actions]
    assert tools5 == [
        ("eggs_submit_qc", []),
        ("eggs_log_work", ["time_spent", "note"]),
    ]

    # Bug stand-alone without QC
    it6 = by_key["RFID-6"]
    tools6 = [(a.tool, a.missing) for a in it6.actions]
    assert tools6 == [
        ("eggs_submit_qc", ["qc_owner"]),
        ("eggs_log_work", ["time_spent", "note"]),
    ]

    # Bug stand-alone with QC
    it7 = by_key["RFID-7"]
    tools7 = [(a.tool, a.missing) for a in it7.actions]
    assert tools7 == [
        ("eggs_submit_qc", []),
        ("eggs_log_work", ["time_spent", "note"]),
    ]


def test_finish_intent_example():
    from mcp_atlassian.eggs.agenda import INTENTS

    intent = next(i for i in INTENTS if i.name == "finish")
    assert "xong RFID-130, phần này không cần QC riêng" in intent.examples


def test_agenda_qc_queue_adhoc_task(cfg, cal):
    identity = Identity(
        username="qc_alice",
        display_name="QC Alice",
        roles={"RFID": {"qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # An Ad-hoc Task in ready_for_qc whose own QC Owner is qc_alice
    adhoc_qc = _make_issue(
        "RFID-10",
        issue_type="adhoc",
        status="ready_for_qc",
        assignee="dev_user",
        qc_owner="qc_alice",
        summary="Adhoc needs test",
    )
    snapshot = Snapshot(
        issues={"RFID-10": adhoc_qc},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 9),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    assert agenda[0].priority == 4
    assert agenda[0].key == "RFID-10"
    assert agenda[0].actions[0].tool == "eggs_qc_start"


def test_agenda_intent_examples_for_adhoc():
    from mcp_atlassian.eggs.agenda import INTENTS

    finish_intent = next(i for i in INTENTS if i.name == "finish")
    assert "xong ADHOC-5, không cần QC" in finish_intent.examples

    submit_qc_intent = next(i for i in INTENTS if i.name == "submit_qc")
    assert "xong ADHOC-5, nhờ tuan.bui test" in submit_qc_intent.examples


def test_agenda_dev_task_under_bug_and_bug_sub_under_adhoc(cfg, cal):
    identity = Identity(
        username="alice",
        display_name="Alice Dev",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # Dev Task under Bug stand-alone -> eggs_finish only
    dev_under_bug = _make_issue(
        "RFID-20",
        issue_type="dev_task",
        parent_type="bug",
        status="in_progress",
        assignee="alice",
        summary="Dev Task under bug",
    )
    # Bug sub under Ad-hoc Task -> eggs_submit_qc only (never eggs_finish)
    bug_sub_under_adhoc = _make_issue(
        "RFID-21",
        issue_type="bug_sub",
        parent_type="adhoc",
        status="in_progress",
        assignee="alice",
        summary="Bug sub under adhoc",
    )

    issues = {
        dev_under_bug.key: dev_under_bug,
        bug_sub_under_adhoc.key: bug_sub_under_adhoc,
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
    by_key = {it.key: it for it in agenda}

    # Dev Task under bug: eggs_finish + eggs_log_work
    it_dev = by_key["RFID-20"]
    tools_dev = [a.tool for a in it_dev.actions]
    assert tools_dev == ["eggs_finish", "eggs_log_work"]

    # Bug sub under adhoc: eggs_submit_qc + eggs_log_work (no eggs_finish)
    it_bug = by_key["RFID-21"]
    tools_bug = [a.tool for a in it_bug.actions]
    assert tools_bug == ["eggs_submit_qc", "eggs_log_work"]


def test_agenda_qc_queue_parent_adhoc_or_story(cfg, cal):
    identity = Identity(
        username="qc_alice",
        display_name="QC Alice",
        roles={"RFID": {"qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # Parent Ad-hoc Task with qc_owner="qc_alice"
    adhoc_parent = _make_issue(
        "RFID-30",
        issue_type="adhoc",
        status="in_progress",
        assignee="dev_user",
        qc_owner="qc_alice",
        summary="Adhoc parent",
    )
    # Bug (sub) under Ad-hoc Task in ready_for_qc
    bug_under_adhoc = _make_issue(
        "RFID-31",
        issue_type="bug_sub",
        parent_type="adhoc",
        parent_key="RFID-30",
        status="ready_for_qc",
        assignee="dev_user",
        summary="Bug sub under adhoc ready for qc",
    )

    # Parent Story with qc_owner="qc_alice"
    story_parent = _make_issue(
        "RFID-40",
        issue_type="story",
        status="in_progress",
        assignee="ba_user",
        qc_owner="qc_alice",
        summary="Story parent",
    )
    # Dev Task under Story in ready_for_qc
    dev_under_story = _make_issue(
        "RFID-41",
        issue_type="dev_task",
        parent_type="story",
        parent_key="RFID-40",
        status="ready_for_qc",
        assignee="dev_user",
        summary="Dev task under story ready for qc",
    )

    # Parent Bug stand-alone
    bug_parent = _make_issue(
        "RFID-50",
        issue_type="bug",
        status="in_progress",
        assignee="dev_user",
        summary="Bug parent",
    )
    # Dev Task under Bug stand-alone in ready_for_qc
    dev_under_bug = _make_issue(
        "RFID-51",
        issue_type="dev_task",
        parent_type="bug",
        parent_key="RFID-50",
        status="ready_for_qc",
        assignee="dev_user",
        summary="Dev task under bug ready for qc",
    )

    issues = {
        adhoc_parent.key: adhoc_parent,
        bug_under_adhoc.key: bug_under_adhoc,
        story_parent.key: story_parent,
        dev_under_story.key: dev_under_story,
        bug_parent.key: bug_parent,
        dev_under_bug.key: dev_under_bug,
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
    qc_queue_keys = [it.key for it in agenda if it.priority == 4]
    assert "RFID-31" in qc_queue_keys
    assert "RFID-41" in qc_queue_keys
    assert "RFID-51" not in qc_queue_keys


def test_eggsbox_38_qc_sees_only_qc_start(cfg, cal):
    # Seen live: tuan.bui (QC) ran /eggs. EGGSBOX-38 is a Bug stand-alone in status QC TO DO,
    # assignee cuong.huynh, QC Owner tuan.bui, with a live NEED_RESCHEDULE flag at escalation level 0.
    issue = _make_issue(
        "EGGSBOX-38",
        issue_type="bug",
        status="ready_for_qc",
        assignee="cuong.huynh",
        qc_owner="tuan.bui",
        summary="Bug standalone live case",
        project="EGGSBOX",
    )
    identity = Identity(
        username="tuan.bui",
        display_name="Tuan Bui",
        roles={"EGGSBOX": {"qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    snapshot = Snapshot(
        issues={"EGGSBOX-38": issue},
        flags={"EGGSBOX-38": frozenset({"NEED_RESCHEDULE"})},
        escalations={
            "EGGSBOX-38": Escalation(level=0, owner="tuan.bui", since=date(2026, 11, 9))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    item = agenda[0]
    assert item.key == "EGGSBOX-38"
    assert [a.tool for a in item.actions] == ["eggs_qc_start"]
    assert "EGGSBOX-38 đang chờ bạn test (cờ NEED_RESCHEDULE)." in item.say


def test_eggsbox_38_assignee_in_dev_gets_submit_qc_and_reschedule(cfg, cal):
    # Same issue seen by assignee in Dev In Progress gets submit_qc + request_reschedule
    issue = _make_issue(
        "EGGSBOX-38",
        issue_type="bug",
        status="in_progress",
        assignee="cuong.huynh",
        qc_owner="tuan.bui",
        summary="Bug standalone live case",
        project="EGGSBOX",
    )
    identity = Identity(
        username="cuong.huynh",
        display_name="Cuong Huynh",
        roles={"EGGSBOX": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    snapshot = Snapshot(
        issues={"EGGSBOX-38": issue},
        flags={"EGGSBOX-38": frozenset({"NEED_RESCHEDULE"})},
        escalations={
            "EGGSBOX-38": Escalation(
                level=0, owner="cuong.huynh", since=date(2026, 11, 9)
            )
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    item = agenda[0]
    tools = [a.tool for a in item.actions]
    assert "eggs_submit_qc" in tools
    assert "eggs_request_reschedule" in tools
    assert "eggs_qc_start" not in tools


def test_lead_ba_not_assignee_gets_reschedule_not_submit_qc(cfg, cal):
    # A lead_ba who is not the assignee is offered request_reschedule but not submit_qc
    issue = _make_issue(
        "EGGSBOX-38",
        issue_type="bug",
        status="in_progress",
        assignee="cuong.huynh",
        qc_owner="tuan.bui",
        summary="Bug standalone live case",
        project="EGGSBOX",
    )
    identity = Identity(
        username="lead.ba",
        display_name="Lead BA",
        roles={"EGGSBOX": {"lead_ba"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    snapshot = Snapshot(
        issues={"EGGSBOX-38": issue},
        flags={"EGGSBOX-38": frozenset({"NEED_RESCHEDULE"})},
        escalations={
            "EGGSBOX-38": Escalation(level=0, owner="lead.ba", since=date(2026, 11, 9))
        },
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    item = agenda[0]
    tools = [a.tool for a in item.actions]
    assert "eggs_request_reschedule" in tools
    assert "eggs_submit_qc" not in tools


def test_property_every_suggested_action_in_allowed_actions(cfg, cal):
    # A property-style test that for a grid of (issue type × status × identity relation)
    # every suggested action is in allowed_actions.
    issue_types = ["dev_task", "bug_sub", "bug", "adhoc", "story", "report", "epic"]
    statuses = [
        "open",
        "analyzing",
        "ready_for_dev",
        "todo",
        "in_progress",
        "ready_for_qc",
        "testing",
        "ready_for_uat",
        "done",
        "cancelled",
    ]
    identity_roles_map = [
        ("dev_user", {"RFID": {"dev"}}),
        ("qc_user", {"RFID": {"qc"}}),
        ("ba_user", {"RFID": {"lead_ba"}}),
        ("stranger", {"OTHER": {"dev"}}),
    ]

    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    for itype in issue_types:
        for st in statuses:
            for username, roles in identity_roles_map:
                ident = Identity(
                    username=username,
                    display_name=username,
                    roles=roles,
                    is_lead=False,
                )
                iss = _make_issue(
                    "RFID-99",
                    issue_type=itype,
                    status=st,
                    assignee="dev_user",
                    qc_owner="qc_user",
                    project="RFID",
                )
                # Test with escalation to this user and NEED_RESCHEDULE flag
                snap = Snapshot(
                    issues={"RFID-99": iss},
                    flags={"RFID-99": frozenset({"NEED_RESCHEDULE"})},
                    escalations={
                        "RFID-99": Escalation(
                            level=0, owner=username, since=date(2026, 11, 9)
                        )
                    },
                    flows={},
                    epics={},
                    not_updated=[],
                    today=date(2026, 11, 10),
                )
                agenda = build_agenda(snap, ident, cfg, now, cal)
                allowed = allowed_actions(iss, ident, cfg)
                for item in agenda:
                    if item.key == "RFID-99":
                        for a in item.actions:
                            assert a.tool in allowed, (
                                f"Action {a.tool} on ({itype}, {st}, {username}) not in allowed_actions: {allowed}"
                            )


def test_agenda_story_in_testing_with_open_bug_subs_informational(cfg, cal):
    identity = Identity(
        username="tuan.bui",
        display_name="Tuan Bui",
        roles={"EGGSBOX": {"qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    story = _make_issue(
        "EGGSBOX-10",
        issue_type="story",
        status="testing",
        qc_owner="tuan.bui",
        project="EGGSBOX",
        summary="Story 10",
    )
    bug1 = _make_issue(
        "EGGSBOX-41",
        issue_type="bug_sub",
        status="in_progress",
        parent_key="EGGSBOX-10",
        project="EGGSBOX",
        summary="Bug 41",
    )
    bug2 = _make_issue(
        "EGGSBOX-42",
        issue_type="bug_sub",
        status="in_progress",
        parent_key="EGGSBOX-10",
        project="EGGSBOX",
        summary="Bug 42",
    )
    snapshot = Snapshot(
        issues={
            story.key: story,
            bug1.key: bug1,
            bug2.key: bug2,
        },
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    by_key = {it.key: it for it in agenda}
    assert "EGGSBOX-10" in by_key
    it = by_key["EGGSBOX-10"]
    assert it.say == "EGGSBOX-10 đang test, còn 2 bug đang sửa: EGGSBOX-41, EGGSBOX-42"
    assert it.actions == []


def test_agenda_bug_sub_in_qc_to_do_offered_qc_start(cfg, cal):
    identity = Identity(
        username="tuan.bui",
        display_name="Tuan Bui",
        roles={"EGGSBOX": {"qc"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    bug_sub = _make_issue(
        "EGGSBOX-41",
        issue_type="bug_sub",
        status="ready_for_qc",
        qc_owner="tuan.bui",
        project="EGGSBOX",
        summary="Bug 41 ready for test",
    )
    snapshot = Snapshot(
        issues={bug_sub.key: bug_sub},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    by_key = {it.key: it for it in agenda}
    assert "EGGSBOX-41" in by_key
    it = by_key["EGGSBOX-41"]
    tools = [a.tool for a in it.actions]
    assert "eggs_qc_start" in tools


def test_agenda_bug_sub_in_dev_to_do_prioritized(cfg, cal):
    identity = Identity(
        username="cuong.huynh",
        display_name="Cuong Huynh",
        roles={"EGGSBOX": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    # Regular Dev Task in progress
    dev_task = _make_issue(
        "EGGSBOX-20",
        issue_type="dev_task",
        parent_type="story",
        status="in_progress",
        assignee="cuong.huynh",
        project="EGGSBOX",
        summary="Dev Task in progress",
    )
    # Bug sub in DEV To Do
    bug_sub = _make_issue(
        "EGGSBOX-41",
        issue_type="bug_sub",
        parent_type="story",
        status="todo",
        assignee="cuong.huynh",
        project="EGGSBOX",
        summary="Bug sub needs fix",
    )
    snapshot = Snapshot(
        issues={
            dev_task.key: dev_task,
            bug_sub.key: bug_sub,
        },
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    keys = [it.key for it in agenda]
    assert keys == ["EGGSBOX-41", "EGGSBOX-20"]
    it_bug = agenda[0]
    assert it_bug.priority == 7
    tools_bug = [a.tool for a in it_bug.actions]
    assert tools_bug == ["eggs_start"]


def test_dev_sees_informational_item_for_unsplit_analyzed_stories(cfg, cal):
    identity = Identity(
        username="cuong.huynh",
        display_name="Cuong Huynh",
        roles={"EGGSBOX": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    s60 = _make_issue(
        "EGGSBOX-60",
        issue_type="story",
        status="analyzing",
        project="EGGSBOX",
        summary="Story 60",
    )
    s61 = _make_issue(
        "EGGSBOX-61",
        issue_type="story",
        status="analyzing",
        project="EGGSBOX",
        summary="Story 61",
    )
    s62 = _make_issue(
        "EGGSBOX-62",
        issue_type="story",
        status="analyzing",
        project="EGGSBOX",
        summary="Story 62",
    )
    snapshot = Snapshot(
        issues={
            s60.key: s60,
            s61.key: s61,
            s62.key: s62,
        },
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert len(agenda) == 1
    it = agenda[0]
    assert (
        it.say
        == "3 Story đã phân tích xong, chưa chia việc: EGGSBOX-60, EGGSBOX-61, EGGSBOX-62"
    )
    assert it.actions == []


def test_stories_in_open_not_shown_to_devs(cfg, cal):
    identity = Identity(
        username="cuong.huynh",
        display_name="Cuong Huynh",
        roles={"EGGSBOX": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    story_open = _make_issue(
        "EGGSBOX-70",
        issue_type="story",
        status="open",
        project="EGGSBOX",
        summary="Story still in Open",
    )
    snapshot = Snapshot(
        issues={story_open.key: story_open},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    assert agenda == []


def test_lead_ba_sees_prompt_to_schedule_and_ready_for_dev(cfg, cal):
    identity = Identity(
        username="tuan.bui",
        display_name="Tuan Bui",
        roles={"EGGSBOX": {"lead_ba"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    story = _make_issue(
        "EGGSBOX-60",
        issue_type="story",
        status="analyzing",
        project="EGGSBOX",
        summary="Story 60 with full tasks",
    )
    dt1 = _make_issue(
        "EGGSBOX-61",
        issue_type="dev_task",
        parent_type="story",
        parent_key="EGGSBOX-60",
        part="BE",
        estimate_seconds=7200,
        status="todo",
        project="EGGSBOX",
        summary="BE task",
    )
    dt2 = _make_issue(
        "EGGSBOX-62",
        issue_type="dev_task",
        parent_type="story",
        parent_key="EGGSBOX-60",
        part="FE APP",
        estimate_seconds=14400,
        status="todo",
        project="EGGSBOX",
        summary="FE task",
    )
    snapshot = Snapshot(
        issues={
            story.key: story,
            dt1.key: dt1,
            dt2.key: dt2,
        },
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 10),
    )
    agenda = build_agenda(snapshot, identity, cfg, now, cal)
    by_key = {it.key: it for it in agenda if it.key}
    assert "EGGSBOX-60" in by_key
    it = by_key["EGGSBOX-60"]
    assert (
        it.say
        == "EGGSBOX-60 đã đủ Dev Task và estimate: chốt Due Date rồi Ready For Dev"
    )
    assert it.actions == []
