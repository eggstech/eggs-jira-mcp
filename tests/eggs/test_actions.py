from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo
import pytest

from mcp_atlassian.eggs.actions import ActionResult, Actions, EndDayEntry
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig, Thresholds
from mcp_atlassian.eggs.model import Issue
from tests.eggs.fakes import FakeGateway

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TODAY = date(2026, 11, 18)


@pytest.fixture
def cfg():
    return EggsConfig(
        lead_user="cuong.huynh",
        timezone="Asia/Ho_Chi_Minh",
        ops_epic_prefix="[OPS]",
        fields={
            "planned_start": "customfield_10108",
            "planned_end": "customfield_10109",
            "baseline_end": "customfield_baseline_end",
            "proposed_end": "customfield_proposed_end",
            "reschedule_reason": "customfield_reschedule_reason",
            "reschedule_status": "customfield_reschedule_status",
            "reschedule_requester": "customfield_reschedule_requester",
            "blocked_by": "customfield_blocked_by",
            "block_reason": "customfield_block_reason",
            "blocked_since": "customfield_blocked_since",
            "flagged": "customfield_flagged",
            "epic_link": "customfield_epic_link",
            "report_source": "customfield_report_source",
            "reported_by": "customfield_reported_by",
            "reported_at": "customfield_reported_at",
        },
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
            "report": ["User Report"],
        },
        transitions={
            "schedule": "Schedule",
            "start": "Start",
            "submit_qc": "Submit QC",
            "start_test": "Start Test",
            "qc_pass": "QC Pass",
            "qc_fail": "QC Fail",
            "cancel": "Cancel",
            "request_reschedule": "Request Reschedule",
            "approve_reschedule": "Approve Reschedule",
            "reject_reschedule": "Reject Reschedule",
            "block": "Block",
            "unblock": "Unblock",
            "confirm_no_impact": "Confirm No Impact",
            "client_uat": "Client UAT",
            "uat_signoff": "UAT Sign-off",
            "uat_fail": "UAT Fail",
            "start_triage": "Start Triage",
            "close_report": "Close Report",
        },
        projects={
            "HMKPOS": ProjectConfig(
                key="HMKPOS",
                default_lead_ba="cuong.huynh",
                triage_owner="triage.qc",
            ),
        },
        report_resolutions={
            "bug": "Is a Bug",
            "guidance": "User Guidance",
            "feature": "Feature Request",
            "data_fix": "Data/Config Fix",
            "duplicate": "Duplicate",
            "cannot_reproduce": "Cannot Reproduce",
            "third_party": "Third-party Issue",
        },
    )


@pytest.fixture
def cal():
    return WorkCalendar(holidays=frozenset())


def _make_actions(gateway, cfg, cal):
    return Actions(gateway, cfg, cal, today_fn=lambda: TODAY)


def test_no_delete_method_exists():
    # Hard rule: actions never delete anything
    action_methods = [m for m in dir(Actions) if not m.startswith("_")]
    for m in action_methods:
        assert "delete" not in m.lower(), f"Method {m} violates no-delete rule"
        assert "remove" not in m.lower(), f"Method {m} violates no-delete rule"


def test_start_without_dates_and_no_baseline_explains(cfg, cal):
    # Starting a task without schedule and without baseline should fail with explanation
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task without dates",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Start"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.start("HMKPOS-101")
    assert res.ok is False
    assert "lịch" in res.message.lower() or "planned" in res.message.lower()


def test_submit_qc_refused_under_adhoc(cfg, cal):
    # dev_task whose parent is adhoc cannot submit QC
    raw_adhoc = {
        "key": "HMKPOS-99",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_subtask = {
        "key": "HMKPOS-102",
        "fields": {
            "summary": "Sub under adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-99",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-99": raw_adhoc, "HMKPOS-102": raw_subtask},
        transitions={"HMKPOS-102": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.submit_qc("HMKPOS-102")
    assert res.ok is False
    assert "ad-hoc" in res.message.lower() or "adhoc" in res.message.lower() or "finish" in res.message.lower()


def test_request_reschedule_past_date_refused(cfg, cal):
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "customfield_10109": "2026-11-20",
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Request Reschedule"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    # Date in the past relative to TODAY (2026-11-18)
    res = actions.request_reschedule("HMKPOS-101", new_end=date(2026, 11, 10), reason="Chậm tiến độ")
    assert res.ok is False
    assert "quá khứ" in res.message.lower() or "past" in res.message.lower()


def test_chen_ngang_requires_interrupted_by(cfg, cal):
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Request Reschedule"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    # Reason mentions chen ngang but interrupted_by is None
    res = actions.request_reschedule(
        "HMKPOS-101",
        new_end=date(2026, 11, 25),
        reason="Việc chen ngang",
        interrupted_by=None,
    )
    assert res.ok is False
    assert "chen ngang" in res.message.lower() or "interrupted_by" in res.message.lower()


def test_self_approval_refused(cfg, cal):
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "customfield_reschedule_requester": {"name": "cuong.huynh"},
            "customfield_reschedule_status": {"value": "Chờ duyệt"},
        },
    }
    # Current user is cuong.huynh who is also the requester
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Approve Reschedule"]},
        user="cuong.huynh",
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.decide_reschedule("HMKPOS-101", approve=True, note="Approve")
    assert res.ok is False
    assert "tự duyệt" in res.message.lower() or "self" in res.message.lower()


def test_story_qc_fail_requires_bugs_and_creates_bug_sub_first(cfg, cal):
    raw_story = {
        "key": "HMKPOS-100",
        "fields": {
            "summary": "Login Flow",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Test In Progress"},
            "components": [{"name": "CH - CỬA HÀNG"}],
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-100": raw_story},
        transitions={"HMKPOS-100": ["QC Fail"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    # 1. Fails if bugs list is empty
    res_empty = actions.qc_fail("HMKPOS-100", time_spent="1h", reason="Bug found", bugs=[])
    assert res_empty.ok is False
    assert "bug" in res_empty.message.lower()

    # 2. Succeeds with bugs and creates bug_sub first
    res = actions.qc_fail(
        "HMKPOS-100",
        time_spent="1h",
        reason="Login button not clickable",
        bugs=["Login button broken on iOS"],
    )
    assert res.ok is True

    # Assert gateway calls order: create_issue first, then transition
    call_names = [call[0] for call in gateway.calls]
    assert "create_issue" in call_names
    assert "transition" in call_names
    idx_create = call_names.index("create_issue")
    idx_trans = call_names.index("transition")
    assert idx_create < idx_trans

    # Check created issue summary: [<CODE>][BUG] <text>
    created_call = [c for c in gateway.calls if c[0] == "create_issue"][0]
    created_fields = created_call[1][0]
    assert created_fields["summary"] == "[CH][BUG] Login button broken on iOS"
    assert created_fields["parent"]["key"] == "HMKPOS-100"


def test_uat_start_moves_only_actor_stories_in_ready_for_uat(cfg, cal):
    # 2 stories of actor CH: one at ready_for_uat, one at in_progress
    # 1 story of actor KT: at ready_for_uat
    raw_ch1 = {
        "key": "HMKPOS-1",
        "fields": {
            "summary": "CH 1",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Ready For UAT"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            "customfield_epic_link": "HMKPOS-EPIC",
        },
    }
    raw_ch2 = {
        "key": "HMKPOS-2",
        "fields": {
            "summary": "CH 2",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            "customfield_epic_link": "HMKPOS-EPIC",
        },
    }
    raw_kt1 = {
        "key": "HMKPOS-3",
        "fields": {
            "summary": "KT 1",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Ready For UAT"},
            "components": [{"name": "KT - KẾ TOÁN"}],
            "customfield_epic_link": "HMKPOS-EPIC",
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-1": raw_ch1, "HMKPOS-2": raw_ch2, "HMKPOS-3": raw_kt1},
        transitions={"HMKPOS-1": ["Client UAT"], "HMKPOS-3": ["Client UAT"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    res = actions.uat(
        epic_key="HMKPOS-EPIC",
        actor="CH - CỬA HÀNG",
        action="start",
        note="Start CH UAT",
    )
    assert res.ok is True
    # Only HMKPOS-1 transitioned
    transitioned_keys = [c[1][0] for c in gateway.calls if c[0] == "transition"]
    assert transitioned_keys == ["HMKPOS-1"]


def test_uat_signoff_refuses_when_actor_story_not_in_uat(cfg, cal):
    # Actor CH has 2 stories: one at client_uat, one still in Dev
    raw_ch1 = {
        "key": "HMKPOS-1",
        "fields": {
            "summary": "CH 1",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Wait For Review"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            "customfield_epic_link": "HMKPOS-EPIC",
        },
    }
    raw_ch2 = {
        "key": "HMKPOS-2",
        "fields": {
            "summary": "CH 2",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "DEV To Do"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            "customfield_epic_link": "HMKPOS-EPIC",
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-1": raw_ch1, "HMKPOS-2": raw_ch2},
        transitions={"HMKPOS-1": ["UAT Sign-off"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    # Without story_keys, refuses because HMKPOS-2 is not in UAT
    res = actions.uat(
        epic_key="HMKPOS-EPIC",
        actor="CH - CỬA HÀNG",
        action="signoff",
        note="Signoff CH",
    )
    assert res.ok is False
    assert "chưa" in res.message.lower() or "wait for review" in res.message.lower()


def test_triage_open_creates_report_in_ops_epic_assigned_to_triage_owner(cfg, cal):
    # Current month OPS epic
    month_str = TODAY.strftime("%m/%Y")
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": f"[OPS] Operations {month_str}",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-OPS": ops_epic},
        transitions={"HMKPOS-1001": ["Start Triage"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    res = actions.triage_open(
        project="HMKPOS",
        text="Cannot login with OTP",
        source_url="https://chat.google.com/room/123",
        reported_by="Store Manager",
    )
    assert res.ok is True

    # Assert created issue
    created_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(created_calls) == 1
    fields = created_calls[0][1][0]
    assert fields["summary"] == "Cannot login with OTP"
    assert fields["assignee"]["name"] == "triage.qc"
    assert fields["customfield_epic_link"] == "HMKPOS-OPS"
    assert fields["customfield_report_source"] == "https://chat.google.com/room/123"
    assert fields["customfield_reported_by"] == "Store Manager"

    # Assert transition Start Triage
    trans_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1][1] == "Start Triage"


def test_triage_close_bug_under_story_links_and_resolves(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "Login issue",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    raw_story = {
        "key": "HMKPOS-100",
        "fields": {
            "summary": "Login Story",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Ready For Dev"},
            "components": [{"name": "CH - CỬA HÀNG"}],
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-REP1": raw_report, "HMKPOS-100": raw_story},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    res = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Confirmed bug in login flow",
        time_spent="30m",
        bug_story_key="HMKPOS-100",
        bug_summary="Fix OTP login timeout",
    )
    assert res.ok is True
    # Assert worklog added
    wl_calls = [c for c in gateway.calls if c[0] == "add_worklog"]
    assert len(wl_calls) == 1
    # Assert bug created under story
    cr_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(cr_calls) == 1
    bug_fields = cr_calls[0][1][0]
    assert bug_fields["parent"]["key"] == "HMKPOS-100"
    # Assert link causes
    link_calls = [c for c in gateway.calls if c[0] == "link"]
    assert len(link_calls) == 1
    assert link_calls[0][1][0].lower() == "causes"
    # Assert transition Close Report with resolution "Is a Bug"
    tr_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert len(tr_calls) == 1
    assert tr_calls[0][1][1] == "Close Report"
    assert tr_calls[0][2]["fields"]["resolution"]["name"] == "Is a Bug"
    # Assert reply suggestion in message
    assert "gợi ý" in res.message.lower() or "reply" in res.message.lower()


def test_triage_close_bug_production_goes_to_ops(cfg, cal):
    month_str = TODAY.strftime("%m/%Y")
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": f"[OPS] Operations {month_str}",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_report = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "Prod login failure",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-OPS": ops_epic, "HMKPOS-REP1": raw_report},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    # bug_story_key is None -> goes to OPS epic as Bug stand-alone
    res = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Production hotfix needed",
        time_spent="1h",
        bug_story_key=None,
        bug_summary="Hotfix prod login",
    )
    assert res.ok is True
    cr_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(cr_calls) == 1
    bug_fields = cr_calls[0][1][0]
    assert bug_fields["customfield_epic_link"] == "HMKPOS-OPS"
    assert bug_fields["issuetype"]["name"] == "Bug stand-alone"


def test_triage_close_feature_requires_link(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "Request dark mode",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    gateway = FakeGateway(issues={"HMKPOS-REP1": raw_report})
    actions = _make_actions(gateway, cfg, cal)

    # Missing link_key should be refused
    res = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="feature",
        note="Dark mode is a feature request",
        time_spent="15m",
        link_key=None,
    )
    assert res.ok is False
    assert "link_key" in res.message.lower() or "liên kết" in res.message.lower()


def test_triage_close_refuses_bug_under_done_story(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "Report",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    done_story = {
        "key": "HMKPOS-100",
        "fields": {
            "summary": "Done Story",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Done"},
        },
    }
    gateway = FakeGateway(issues={"HMKPOS-REP1": raw_report, "HMKPOS-100": done_story})
    actions = _make_actions(gateway, cfg, cal)

    res = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Bug in done story",
        time_spent="30m",
        bug_story_key="HMKPOS-100",
    )
    assert res.ok is False
    assert "done" in res.message.lower() or "hoàn thành" in res.message.lower()


@pytest.mark.parametrize(
    "method_name,kwargs",
    [
        ("schedule", {"key": "HMKPOS-101", "start": TODAY, "end": TODAY, "estimate": "1d"}),
        ("start", {"key": "HMKPOS-101"}),
        ("log_work", {"key": "HMKPOS-101", "time_spent": "1h", "note": "Dev note"}),
        ("submit_qc", {"key": "HMKPOS-101", "time_spent": "1h", "note": "Ready"}),
        ("finish", {"key": "HMKPOS-101", "time_spent": "1h", "note": "Finished"}),
        ("block", {"key": "HMKPOS-101", "reason": "Blocked by API", "blocked_by": "Nội bộ"}),
        ("unblock", {"key": "HMKPOS-101", "note": "Unblocked"}),
        ("request_reschedule", {"key": "HMKPOS-101", "new_end": date(2026, 11, 25), "reason": "Other work"}),
        ("end_day", {"entries": [EndDayEntry("HMKPOS-101", "1h", "Done today", None)]}),
        ("qc_start", {"key": "HMKPOS-101"}),
        ("qc_pass", {"key": "HMKPOS-101", "time_spent": "1h", "note": "Pass"}),
        ("qc_fail", {"key": "HMKPOS-101", "time_spent": "1h", "reason": "Bug", "bugs": []}),
        ("flow_reschedule", {"story_key": "HMKPOS-100", "new_due": date(2026, 11, 25), "reason": "Delay"}),
        ("uat", {"epic_key": "HMKPOS-EPIC", "actor": "CH - CỬA HÀNG", "action": "start", "note": "UAT"}),
        ("decide_reschedule", {"key": "HMKPOS-101", "approve": True, "note": "OK"}),
        ("cancel", {"key": "HMKPOS-101", "reason": "Cancelled"}),
        ("triage_open", {"project": "HMKPOS", "text": "Report", "source_url": "http://x", "reported_by": "A"}),
        ("triage_close", {"key": "HMKPOS-REP1", "verdict": "guidance", "note": "Guide", "time_spent": "15m"}),
    ],
)
def test_dry_run_makes_no_writes(cfg, cal, method_name, kwargs):
    # Setup test fixtures
    month_str = TODAY.strftime("%m/%Y")
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": f"[OPS] Operations {month_str}",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
            "customfield_10109": "2026-11-20",
            "parent": {
                "key": "HMKPOS-100",
                "fields": {"issuetype": {"name": "Story", "subtask": False}},
            },
        },
    }
    raw_story = {
        "key": "HMKPOS-100",
        "fields": {
            "summary": "Story",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Ready For UAT"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            "customfield_epic_link": "HMKPOS-EPIC",
        },
    }
    raw_rep = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "Report",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }

    gateway = FakeGateway(
        issues={
            "HMKPOS-OPS": ops_epic,
            "HMKPOS-100": raw_story,
            "HMKPOS-101": raw_task,
            "HMKPOS-REP1": raw_rep,
        },
        transitions={
            "HMKPOS-101": ["Schedule", "Start", "Submit QC", "QC Pass", "QC Fail", "Block", "Unblock", "Request Reschedule", "Approve Reschedule", "Cancel"],
            "HMKPOS-100": ["Client UAT", "Request Reschedule"],
            "HMKPOS-REP1": ["Close Report", "Start Triage"],
        },
        user="other.approver",
    )
    actions = _make_actions(gateway, cfg, cal)
    fn = getattr(actions, method_name)

    # Call with dry_run=True
    res = fn(**kwargs, dry_run=True)
    assert res.ok is True
    # Assert zero mutating calls to gateway
    write_methods = {"transition", "update_fields", "add_comment", "add_worklog", "create_issue", "link"}
    actual_write_calls = [c for c in gateway.calls if c[0] in write_methods]
    assert actual_write_calls == [], f"dry_run on {method_name} wrote to gateway: {actual_write_calls}"
