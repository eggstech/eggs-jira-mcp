from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mcp_atlassian.eggs.actions import Actions, EndDayEntry
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.gateway import JiraError
from mcp_atlassian.eggs.model import Worklog
from tests.eggs.fakes import FakeGateway

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TODAY = date(2026, 11, 18)


@pytest.fixture
def cfg():
    return EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        timezone="Asia/Ho_Chi_Minh",
        ops_epic_prefix="[OPS]",
        ops_epic_summary="[OPS] Vận hành & Hotfix {month:02d}/{year}",
        hotfix_prefix="[HOTFIX]",
        reschedule_interrupt_value="Chen ngang",
        link_types={
            "interrupts": "Interrupts",
            "causes": "Causes",
            "relates": "Relates",
            "duplicates": "Duplicate",
            "blocks": "Blocks",
        },
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
            "qc_owner": "customfield_qc_owner",
            "report_source": "customfield_report_source",
            "reported_by": "customfield_reported_by",
            "reported_at": "customfield_reported_at",
            "health_flags": "customfield_health_flags",
            "qc_fail_count": "customfield_qc_fail_count",
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
            "finish": "Done",
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
            "set_due": "Set Due Date",
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


def _make_actions(gateway, cfg, cal, bot_gateway=None):
    return Actions(gateway, cfg, cal, today_fn=lambda: TODAY, bot_gateway=bot_gateway)


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
    res = actions.submit_qc("HMKPOS-102", time_spent="1h", note="Testing worklog")
    assert res.ok is False
    assert (
        "ad-hoc" in res.message.lower()
        or "adhoc" in res.message.lower()
        or "finish" in res.message.lower()
    )
    assert res.next_actions == ["finish"]
    assert not any(
        c[0] in ("add_worklog", "transition", "transition_issue") for c in gateway.calls
    )


def test_submit_qc_refused_under_bug_standalone(cfg, cal):
    # dev_task whose parent is bug cannot submit QC (point to finish, no Jira call)
    raw_bug = {
        "key": "HMKPOS-80",
        "fields": {
            "summary": "Standalone Bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_subtask = {
        "key": "HMKPOS-81",
        "fields": {
            "summary": "Dev task under bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-80",
                "fields": {"issuetype": {"name": "Bug stand-alone", "subtask": False}},
            },
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-80": raw_bug, "HMKPOS-81": raw_subtask},
        transitions={"HMKPOS-81": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.submit_qc("HMKPOS-81", time_spent="1h", note="Testing worklog")
    assert res.ok is False
    assert "finish" in res.message.lower()
    assert res.next_actions == ["finish"]
    # Verify no transition Jira calls were made
    assert not any(
        call[0] in ("add_worklog", "transition", "transition_issue")
        for call in gateway.calls
    )


def test_submit_qc_allowed_for_bug_sub_under_adhoc(cfg, cal):
    # Bug (sub) under an Ad-hoc Task is allowed to submit QC
    raw_adhoc = {
        "key": "HMKPOS-90",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_bug_sub = {
        "key": "HMKPOS-91",
        "fields": {
            "summary": "Bug sub under adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-90",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-90": raw_adhoc, "HMKPOS-91": raw_bug_sub},
        transitions={"HMKPOS-91": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.submit_qc("HMKPOS-91")
    assert res.ok is True
    assert any(call[0] == "transition" for call in gateway.calls)


def test_submit_qc_adhoc_without_owner_refused(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc},
        transitions={"HMKPOS-50": ["Submit QC", "Done"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.submit_qc("HMKPOS-50", time_spent="1h", note="Testing worklog")
    assert res.ok is False
    assert "finish" in res.message.lower()
    assert (
        "ai" in res.message.lower()
        or "qc_owner" in res.message.lower()
        or "test" in res.message.lower()
    )
    assert res.next_actions == ["submit_qc", "finish"]
    # No transition or write calls to Jira
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    actual_writes = [c for c in gateway.calls if c[0] in write_methods]
    assert actual_writes == []
    # No get_transitions call either
    trans_lookup = [c for c in gateway.calls if c[0] == "get_transitions"]
    assert trans_lookup == []


def test_submit_qc_adhoc_with_owner_sends_field(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc},
        transitions={"HMKPOS-50": ["Submit QC", "Done"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    # Dry-run lists QC Owner in changes
    res_dry = actions.submit_qc("HMKPOS-50", qc_owner="tuan.bui", dry_run=True)
    assert res_dry.ok is True
    assert any("tuan.bui" in c for c in res_dry.changes)
    assert any("QC Owner" in c for c in res_dry.changes)
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    assert [c for c in gateway.calls if c[0] in write_methods] == []

    # Real call sends {<qc_owner field id>: {"name": qc_owner}}
    res = actions.submit_qc("HMKPOS-50", qc_owner="tuan.bui")
    assert res.ok is True
    trans_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1] == ("HMKPOS-50", "Submit QC")
    assert trans_calls[0][2]["fields"] == {"customfield_qc_owner": {"name": "tuan.bui"}}


def test_submit_qc_adhoc_existing_owner_needs_no_param(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc with owner",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            "customfield_qc_owner": {"name": "existing.qc"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc},
        transitions={"HMKPOS-50": ["Submit QC", "Done"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    # Calling submit_qc without qc_owner parameter succeeds
    res = actions.submit_qc("HMKPOS-50")
    assert res.ok is True
    trans_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1] == ("HMKPOS-50", "Submit QC")


def test_submit_qc_missing_fields_qc_owner_refused(cfg, cal):
    cfg_no_qc = replace(
        cfg, fields={k: v for k, v in cfg.fields.items() if k != "qc_owner"}
    )
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc},
        transitions={"HMKPOS-50": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg_no_qc, cal)
    res = actions.submit_qc("HMKPOS-50", time_spent="1h", qc_owner="tuan.bui")
    assert res.ok is False
    assert res.message == "Thiếu cấu hình fields.qc_owner"
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    assert [c for c in gateway.calls if c[0] in write_methods] == []


def test_submit_qc_with_qc_owner_on_dev_task_refused(cfg, cal):
    data = _base_fixtures(cfg)
    gateway = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
    )
    actions = _make_actions(gateway, cfg, cal)
    # HMKPOS-101 is a dev_task under Story HMKPOS-10
    res = actions.submit_qc("HMKPOS-101", time_spent="1h", qc_owner="qc.tester")
    assert res.ok is False
    assert (
        res.message
        == "QC Owner của Dev Task/Bug con lấy từ Story hoặc Ad-hoc Task cha."
    )
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    assert [c for c in gateway.calls if c[0] in write_methods] == []


def test_submit_qc_with_qc_owner_replaces_different_owner_on_adhoc(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            "customfield_qc_owner": {"name": "old.qc"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc},
        transitions={"HMKPOS-50": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res_dry = actions.submit_qc("HMKPOS-50", qc_owner="new.qc", dry_run=True)
    assert res_dry.ok is True
    assert "Đổi QC Owner: old.qc → new.qc" in res_dry.changes

    res = actions.submit_qc("HMKPOS-50", qc_owner="new.qc")
    assert res.ok is True
    assert "Đổi QC Owner: old.qc → new.qc" in res.changes


def test_submit_qc_bug_without_owner_refused(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-60": raw_bug},
        transitions={"HMKPOS-60": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.submit_qc("HMKPOS-60", time_spent="1h", note="Testing bug submit")
    assert res.ok is False
    assert "Bug stand-alone HMKPOS-60 cần có QC Owner" in res.message
    assert "Ai sẽ kiểm thử" in res.message
    assert "finish" not in res.message.lower()
    assert res.next_actions == ["submit_qc"]
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    assert [c for c in gateway.calls if c[0] in write_methods] == []


def test_submit_qc_bug_with_owner_sends_field(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-60": raw_bug},
        transitions={"HMKPOS-60": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    # Dry-run
    res_dry = actions.submit_qc("HMKPOS-60", qc_owner="tuan.bui", dry_run=True)
    assert res_dry.ok is True
    assert any("tuan.bui" in c for c in res_dry.changes)
    assert any("QC Owner" in c for c in res_dry.changes)
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    assert [c for c in gateway.calls if c[0] in write_methods] == []

    # Real call
    res = actions.submit_qc("HMKPOS-60", qc_owner="tuan.bui")
    assert res.ok is True
    trans_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1] == ("HMKPOS-60", "Submit QC")
    assert trans_calls[0][2]["fields"] == {"customfield_qc_owner": {"name": "tuan.bui"}}


def test_submit_qc_bug_existing_owner_needs_no_param(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug with owner",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            "customfield_qc_owner": {"name": "existing.qc"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-60": raw_bug},
        transitions={"HMKPOS-60": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.submit_qc("HMKPOS-60")
    assert res.ok is True
    assert any("QC Owner: existing.qc" in c for c in res.changes)


def test_submit_qc_bug_replaces_owner(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug with owner",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            "customfield_qc_owner": {"name": "old.qc"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-60": raw_bug},
        transitions={"HMKPOS-60": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.submit_qc("HMKPOS-60", qc_owner="new.qc")
    assert res.ok is True
    assert "Đổi QC Owner: old.qc → new.qc" in res.changes


def test_submit_qc_with_qc_owner_on_bug_sub_refused(cfg, cal):
    raw_sub = {
        "key": "HMKPOS-61",
        "fields": {
            "summary": "Sub bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            "parent": {
                "key": "HMKPOS-10",
                "fields": {"issuetype": {"name": "Story", "subtask": False}},
            },
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-61": raw_sub},
        transitions={"HMKPOS-61": ["Submit QC"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.submit_qc("HMKPOS-61", qc_owner="qc.tester")
    assert res.ok is False
    assert (
        res.message
        == "QC Owner của Dev Task/Bug con lấy từ Story hoặc Ad-hoc Task cha."
    )


def test_request_reschedule_past_date_refused(cfg, cal):
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "customfield_10109": "2026-11-20",
            "customfield_baseline_end": "2026-11-20",
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Request Reschedule"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    # Date in the past relative to TODAY (2026-11-18)
    res = actions.request_reschedule(
        "HMKPOS-101", new_end=date(2026, 11, 10), reason="Chậm tiến độ"
    )
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
            "customfield_baseline_end": "2026-11-20",
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Request Reschedule"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    # Reason matches reschedule_interrupt_value ("Chen ngang") but interrupted_by is None
    res = actions.request_reschedule(
        "HMKPOS-101",
        new_end=date(2026, 11, 25),
        reason=cfg.reschedule_interrupt_value,
        interrupted_by=None,
    )
    assert res.ok is False
    assert (
        "chen ngang" in res.message.lower() or "interrupted_by" in res.message.lower()
    )


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
    res_empty = actions.qc_fail(
        "HMKPOS-100", time_spent="1h", reason="Bug found", bugs=[]
    )
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
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year),
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


def test_triage_close_bug_under_adhoc_allowed(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP2",
        "fields": {
            "summary": "Database timeout report",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    raw_adhoc = {
        "key": "HMKPOS-200",
        "fields": {
            "summary": "Database optimization",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "customfield_qc_owner": {"name": "qc.tester"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-REP2": raw_report, "HMKPOS-200": raw_adhoc},
        transitions={"HMKPOS-REP2": ["Close Report"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.triage_close(
        key="HMKPOS-REP2",
        verdict="bug",
        note="Bug found during database adhoc work",
        time_spent="30m",
        bug_story_key="HMKPOS-200",
    )
    assert res.ok is True
    cr_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(cr_calls) == 1
    assert cr_calls[0][1][0]["parent"]["key"] == "HMKPOS-200"
    assert (
        cr_calls[0][1][0]["summary"]
        == "[BUG][BUG] Bug found during database adhoc work"
    )


def test_triage_close_bug_under_adhoc_with_actor_code_matches_qc_fail(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP2",
        "fields": {
            "summary": "Timeout report",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    raw_adhoc = {
        "key": "HMKPOS-200",
        "fields": {
            "summary": "Order optimization",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Test In Progress"},
            "customfield_qc_owner": {"name": "qc.tester"},
            "components": [{"name": "ORDER - Đơn hàng"}],
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-REP2": raw_report, "HMKPOS-200": raw_adhoc},
        transitions={"HMKPOS-REP2": ["Close Report"], "HMKPOS-200": ["QC Fail"]},
    )
    actions = _make_actions(gw, cfg, cal)

    # triage_close creates bug sub-task under adhoc
    res_triage = actions.triage_close(
        key="HMKPOS-REP2",
        verdict="bug",
        note="Timeout error",
        time_spent="30m",
        bug_story_key="HMKPOS-200",
    )
    assert res_triage.ok is True
    cr_triage = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(cr_triage) == 1
    assert cr_triage[0][1][0]["summary"] == "[ORDER][BUG] Timeout error"

    # qc_fail on same adhoc produces the same prefix
    res_qc = actions.qc_fail("HMKPOS-200", reason="Failed QC", bugs=["Timeout error"])
    assert res_qc.ok is True
    cr_qc = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(cr_qc) == 2
    assert cr_qc[1][1][0]["summary"] == "[ORDER][BUG] Timeout error"


def test_triage_close_refuses_bug_under_bug_standalone(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP3",
        "fields": {
            "summary": "Payment crash report",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    raw_bug = {
        "key": "HMKPOS-300",
        "fields": {
            "summary": "Payment crash bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-REP3": raw_report, "HMKPOS-300": raw_bug},
        transitions={"HMKPOS-REP3": ["Close Report"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.triage_close(
        key="HMKPOS-REP3",
        verdict="bug",
        note="Another issue in payment crash",
        time_spent="30m",
        bug_story_key="HMKPOS-300",
    )
    assert res.ok is False
    assert (
        res.message
        == 'Bug stand-alone không có Bug con: chưa đạt thì QC Fail để trả về Dev; lỗi khác thì tạo Bug stand-alone mới bằng eggs_create_issue(issue_type="bug", related_to="HMKPOS-300").'
    )
    assert not any(c[0] == "create_issue" for c in gateway.calls)


def test_qc_fail_on_bug_standalone_with_bugs_refused(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-300",
        "fields": {
            "summary": "Payment crash bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Test In Progress"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-300": raw_bug},
        transitions={"HMKPOS-300": ["QC Fail"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.qc_fail(
        "HMKPOS-300", time_spent="1h", reason="Still broken", bugs=["Crash on retry"]
    )
    assert res.ok is False
    assert (
        res.message
        == 'Bug stand-alone không có Bug con: chưa đạt thì QC Fail để trả về Dev; lỗi khác thì tạo Bug stand-alone mới bằng eggs_create_issue(issue_type="bug", related_to="HMKPOS-300").'
    )
    assert not any(
        c[0] in ("create_issue", "add_worklog", "transition") for c in gateway.calls
    )


def test_qc_fail_on_dev_task_under_bug_with_bugs_refused(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-300",
        "fields": {
            "summary": "Payment crash bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_dev = {
        "key": "HMKPOS-301",
        "fields": {
            "summary": "Dev task for bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Test In Progress"},
            "parent": {
                "key": "HMKPOS-300",
                "fields": {"issuetype": {"name": "Bug stand-alone", "subtask": False}},
            },
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-300": raw_bug, "HMKPOS-301": raw_dev},
        transitions={"HMKPOS-301": ["QC Fail"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.qc_fail(
        "HMKPOS-301", time_spent="1h", reason="Still broken", bugs=["Crash on retry"]
    )
    assert res.ok is False
    assert (
        res.message
        == 'Bug stand-alone không có Bug con: chưa đạt thì QC Fail để trả về Dev; lỗi khác thì tạo Bug stand-alone mới bằng eggs_create_issue(issue_type="bug", related_to="HMKPOS-300").'
    )
    assert not any(
        c[0] in ("create_issue", "add_worklog", "transition") for c in gateway.calls
    )


def test_qc_fail_on_adhoc_with_bugs_creates_subtask(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-200",
        "fields": {
            "summary": "Adhoc task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Test In Progress"},
            "customfield_qc_owner": {"name": "qc.tester"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-200": raw_adhoc},
        transitions={"HMKPOS-200": ["QC Fail"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.qc_fail("HMKPOS-200", reason="Failed QC", bugs=["New error in logs"])
    assert res.ok is True
    cr_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(cr_calls) == 1
    assert cr_calls[0][1][0]["parent"]["key"] == "HMKPOS-200"


def test_triage_close_bug_production_goes_to_ops(cfg, cal):
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year),
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


def test_triage_close_bug_production_sets_qc_owner_from_report_assignee(cfg, cal):
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year),
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
            "assignee": {"name": "qc.triage_user"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-OPS": ops_epic, "HMKPOS-REP1": raw_report},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    # Dry-run
    res_dry = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Production hotfix needed",
        time_spent="1h",
        dry_run=True,
    )
    assert res_dry.ok is True
    assert "QC Owner: qc.triage_user" in res_dry.changes

    # Real run
    res = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Production hotfix needed",
        time_spent="1h",
    )
    assert res.ok is True
    assert "QC Owner: qc.triage_user" in res.changes
    cr_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(cr_calls) == 1
    bug_fields = cr_calls[0][1][0]
    assert bug_fields["customfield_qc_owner"] == {"name": "qc.triage_user"}


def test_triage_close_bug_production_sets_qc_owner_from_config_if_no_assignee(cfg, cal):
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year),
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
            "assignee": None,
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-OPS": ops_epic, "HMKPOS-REP1": raw_report},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Production hotfix needed",
        time_spent="1h",
    )
    assert res.ok is True
    assert "QC Owner: triage.qc" in res.changes
    cr_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(cr_calls) == 1
    bug_fields = cr_calls[0][1][0]
    assert bug_fields["customfield_qc_owner"] == {"name": "triage.qc"}


def test_triage_close_bug_production_without_fields_qc_owner_creates_issue_and_reports_in_changes(
    cfg, cal
):
    cfg_no_qc = replace(
        cfg, fields={k: v for k, v in cfg.fields.items() if k != "qc_owner"}
    )
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year),
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
            "assignee": {"name": "qc.triage_user"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-OPS": ops_epic, "HMKPOS-REP1": raw_report},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gateway, cfg_no_qc, cal)
    # Dry run
    res_dry = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Production hotfix needed",
        time_spent="1h",
        dry_run=True,
    )
    assert res_dry.ok is True
    assert any("fields.qc_owner" in c for c in res_dry.changes)

    # Real run
    res = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Production hotfix needed",
        time_spent="1h",
    )
    assert res.ok is True
    assert any("fields.qc_owner" in c for c in res.changes)
    cr_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(cr_calls) == 1
    bug_fields = cr_calls[0][1][0]
    assert "customfield_qc_owner" not in bug_fields


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
        (
            "schedule",
            {"key": "HMKPOS-102", "start": TODAY, "end": TODAY, "estimate": "1d"},
        ),
        ("start", {"key": "HMKPOS-101"}),
        ("log_work", {"key": "HMKPOS-101", "time_spent": "1h", "note": "Dev note"}),
        ("submit_qc", {"key": "HMKPOS-101", "time_spent": "1h", "note": "Ready"}),
        ("finish", {"key": "HMKPOS-101", "time_spent": "1h", "note": "Finished"}),
        (
            "block",
            {"key": "HMKPOS-101", "reason": "Blocked by API", "blocked_by": "Nội bộ"},
        ),
        ("unblock", {"key": "HMKPOS-101", "note": "Unblocked"}),
        (
            "request_reschedule",
            {
                "key": "HMKPOS-101",
                "new_end": date(2026, 11, 25),
                "reason": "Other work",
            },
        ),
        ("end_day", {"entries": [EndDayEntry("HMKPOS-101", "1h", "Done today", None)]}),
        ("qc_start", {"key": "HMKPOS-101"}),
        ("qc_pass", {"key": "HMKPOS-101", "time_spent": "1h", "note": "Pass"}),
        (
            "qc_fail",
            {"key": "HMKPOS-101", "time_spent": "1h", "reason": "Bug", "bugs": []},
        ),
        (
            "flow_reschedule",
            {
                "story_key": "HMKPOS-100",
                "new_due": date(2026, 11, 25),
                "reason": "Delay",
            },
        ),
        (
            "uat",
            {
                "epic_key": "HMKPOS-EPIC",
                "actor": "CH - CỬA HÀNG",
                "action": "start",
                "note": "UAT",
            },
        ),
        ("decide_reschedule", {"key": "HMKPOS-101", "approve": True, "note": "OK"}),
        ("cancel", {"key": "HMKPOS-101", "reason": "Cancelled"}),
        (
            "triage_open",
            {
                "project": "HMKPOS",
                "text": "Report",
                "source_url": "http://x",
                "reported_by": "A",
            },
        ),
        (
            "triage_close",
            {
                "key": "HMKPOS-REP1",
                "verdict": "guidance",
                "note": "Guide",
                "time_spent": "15m",
            },
        ),
    ],
)
def test_dry_run_makes_no_writes(cfg, cal, method_name, kwargs):
    # Setup test fixtures
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year),
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
            "customfield_10108": "2026-11-18",
            "customfield_10109": "2026-11-20",
            "customfield_baseline_end": "2026-11-20",
            "customfield_reschedule_status": "Chờ duyệt",
            "customfield_reschedule_requester": "requester.user",
            "parent": {
                "key": "HMKPOS-100",
                "fields": {"issuetype": {"name": "Story", "subtask": False}},
            },
        },
    }
    raw_task_unscheduled = {
        "key": "HMKPOS-102",
        "fields": {
            "summary": "Task Unscheduled",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
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

    all_task_transitions = [
        "Schedule",
        "Start",
        "Submit QC",
        "QC Pass",
        "QC Fail",
        "Block",
        "Unblock",
        "Request Reschedule",
        "Approve Reschedule",
        "Reject Reschedule",
        "Cancel",
        "Start Test",
        "Done",
    ]
    gateway = FakeGateway(
        issues={
            "HMKPOS-OPS": ops_epic,
            "HMKPOS-100": raw_story,
            "HMKPOS-101": raw_task,
            "HMKPOS-102": raw_task_unscheduled,
            "HMKPOS-REP1": raw_rep,
        },
        transitions={
            "HMKPOS-101": all_task_transitions,
            "HMKPOS-102": all_task_transitions,
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
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    actual_write_calls = [c for c in gateway.calls if c[0] in write_methods]
    assert actual_write_calls == [], (
        f"dry_run on {method_name} wrote to gateway: {actual_write_calls}"
    )


def test_actions_today_fn_uses_cfg_timezone(cfg, cal, monkeypatch):
    from datetime import timezone

    fixed_utc = datetime(2026, 11, 16, 18, 30, 0, tzinfo=timezone.utc)

    class MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fixed_utc.astimezone(tz)
            return fixed_utc

    monkeypatch.setattr("mcp_atlassian.eggs.actions.datetime", MockDatetime)
    gateway = FakeGateway()
    actions = Actions(gateway, cfg, cal)
    # At 18:30 UTC, Vietnam is UTC+7 -> 01:30 on Nov 17 -> date(2026, 11, 17)
    assert actions._today() == date(2026, 11, 17)


def test_schedule_refused_when_baseline_exists(cfg, cal):
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task with baseline",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
            "customfield_baseline_end": "2026-11-20",
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Schedule"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.schedule(
        "HMKPOS-101", start=TODAY, end=date(2026, 11, 25), estimate="2d"
    )
    assert res.ok is False
    assert "baseline" in res.message.lower() or "dời lịch" in res.message.lower()


def test_schedule_requires_estimate_and_future_end(cfg, cal):
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
        transitions={"HMKPOS-101": ["Schedule"]},
    )
    actions = _make_actions(gateway, cfg, cal)

    # Empty estimate
    res_no_est = actions.schedule(
        "HMKPOS-101", start=TODAY, end=date(2026, 11, 25), estimate=""
    )
    assert res_no_est.ok is False
    assert "estimate" in res_no_est.message.lower()

    # Past end date
    res_past = actions.schedule(
        "HMKPOS-101", start=date(2026, 11, 10), end=date(2026, 11, 12), estimate="1d"
    )
    assert res_past.ok is False

    # End before start
    res_order = actions.schedule(
        "HMKPOS-101", start=date(2026, 11, 25), end=date(2026, 11, 20), estimate="1d"
    )
    assert res_order.ok is False


def test_schedule_sends_dates_and_estimate_in_transition(cfg, cal):
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
        transitions={"HMKPOS-101": ["Schedule"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.schedule(
        "HMKPOS-101", start=TODAY, end=date(2026, 11, 25), estimate="2d"
    )
    assert res.ok is True

    # Exactly get_issue, get_transitions, transition
    trans_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    t_call = trans_calls[0]
    assert t_call[1][0] == "HMKPOS-101"
    assert t_call[1][1] == "Schedule"
    fields = t_call[2]["fields"]
    assert fields["customfield_10108"] == "2026-11-18"
    assert fields["customfield_10109"] == "2026-11-25"
    assert fields["timetracking"] == {"originalEstimate": "2d"}
    assert "customfield_baseline_end" not in fields
    assert not any(c[0] == "update_fields" for c in gateway.calls)


def test_start_with_dates_schedules_then_starts(cfg, cal):
    # When start is called with dates on an item without baseline_end:
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task without baseline",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Schedule", "Start"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.start(
        "HMKPOS-101", start=TODAY, end=date(2026, 11, 25), estimate="2d"
    )
    assert res.ok is True

    # Transition sequence: Schedule then Start
    trans_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert len(trans_calls) == 2
    assert trans_calls[0][1][1] == "Schedule"
    assert trans_calls[1][1][1] == "Start"
    assert trans_calls[1][2]["fields"] is None or trans_calls[1][2]["fields"] == {}

    # When start is called with dates on an item WITH baseline_end:
    raw_task_with_baseline = {
        "key": "HMKPOS-102",
        "fields": {
            "summary": "Task with baseline",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
            "customfield_baseline_end": "2026-11-20",
        },
    }
    gateway2 = FakeGateway(
        issues={"HMKPOS-102": raw_task_with_baseline},
        transitions={"HMKPOS-102": ["Start"]},
    )
    actions2 = _make_actions(gateway2, cfg, cal)
    res2 = actions2.start(
        "HMKPOS-102", start=TODAY, end=date(2026, 11, 25), estimate="2d"
    )
    assert res2.ok is False
    assert (
        "dời lịch" in res2.message.lower()
        or "request_reschedule" in res2.message.lower()
    )


def test_unavailable_transition_writes_nothing(cfg, cal):
    # Attempting to block a Done item where Block transition is unavailable
    done_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Done Task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Done"},
        },
    }
    gateway = FakeGateway(
        issues={"HMKPOS-101": done_task},
        transitions={"HMKPOS-101": ["Reopen"]},  # Block not available!
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.block("HMKPOS-101", reason="Blocked", blocked_by="Nội bộ")
    assert res.ok is False
    assert "Reopen" in res.message  # Lists available transition names
    # Zero mutating calls
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    actual_write_calls = [c for c in gateway.calls if c[0] in write_methods]
    assert actual_write_calls == []

    # Same check in dry-run
    res_dry = actions.block(
        "HMKPOS-101", reason="Blocked", blocked_by="Nội bộ", dry_run=True
    )
    assert res_dry.ok is False
    assert "Reopen" in res_dry.message


def test_actions_never_call_update_fields(cfg, cal):
    # Setup happy path fixtures and run ALL write methods
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Wait For Review"
    gw = FakeGateway(
        issues=data["issues"], transitions=data["transitions"], user="approver.user"
    )
    actions = _make_actions(gw, cfg, cal)

    # 1. schedule
    actions.schedule("HMKPOS-102", start=TODAY, end=date(2026, 11, 20), estimate="2d")
    # 2. start
    actions.start("HMKPOS-101")
    # 3. log_work
    actions.log_work("HMKPOS-101", time_spent="1h", note="Work note")
    # 4. submit_qc
    actions.submit_qc("HMKPOS-101", time_spent="1h", note="Ready for QC")
    # 5. finish
    actions.finish("HMKPOS-101", time_spent="1h", note="Done task")
    # 6. block
    actions.block("HMKPOS-101", reason="Blocked", blocked_by="Nội bộ")
    # 7. unblock
    actions.unblock("HMKPOS-101", note="Unblocked")
    # 8. request_reschedule
    actions.request_reschedule("HMKPOS-101", new_end=date(2026, 11, 25), reason="Delay")
    # 9. confirm_no_impact
    actions.confirm_no_impact("HMKPOS-101", note="No impact")
    # 10. end_day
    actions.end_day(
        [EndDayEntry(key="HMKPOS-101", time_spent="1h", note="Done", move_to=None)]
    )
    # 11. qc_start
    actions.qc_start("HMKPOS-101")
    # 12. qc_pass
    actions.qc_pass("HMKPOS-101", time_spent="1h", note="Pass")
    # 13. qc_fail
    actions.qc_fail("HMKPOS-101", time_spent="1h", reason="Bug")
    # 14. flow_reschedule
    actions.flow_reschedule("HMKPOS-100", new_due=date(2026, 11, 25), reason="Delay")
    # 15. uat start
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Ready For UAT"
    actions.uat("HMKPOS-EPIC", actor="CH - CỬA HÀNG", action="start", note="Start")
    # 16. uat signoff
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Wait For Review"
    actions.uat("HMKPOS-EPIC", actor="CH - CỬA HÀNG", action="signoff", note="Signoff")
    # 17. uat fail
    actions.uat(
        "HMKPOS-EPIC",
        actor="CH - CỬA HÀNG",
        action="fail",
        note="Fail",
        story_keys=["HMKPOS-100"],
        bugs={"HMKPOS-100": ["Bug"]},
    )
    # 18. decide_reschedule approve
    actions.decide_reschedule("HMKPOS-101", approve=True, note="Approved")
    # 19. decide_reschedule reject
    actions.decide_reschedule("HMKPOS-101", approve=False, note="Rejected")
    # 20. cancel
    actions.cancel("HMKPOS-101", reason="Cancelled")
    # 21. triage_open
    actions.triage_open(
        "HMKPOS", text="Bug report", source_url="url", reported_by="User"
    )
    # 22. triage_close
    actions.triage_close(
        "HMKPOS-REP1", verdict="guidance", note="Guide", time_spent="15m"
    )

    # Assert FakeGateway recorded ZERO update_fields across EVERY happy path
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert update_calls == []


# ---------------------------------------------------------------------------
# Exact Call Sequence Tests for ALL Public Actions Methods (Item 3)
# ---------------------------------------------------------------------------


def _base_fixtures(cfg):
    ops_summary = cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year)
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": ops_summary,
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    feature_epic = {
        "key": "HMKPOS-EPIC",
        "fields": {
            "summary": "Feature Epic",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_story = {
        "key": "HMKPOS-100",
        "fields": {
            "summary": "Story 100",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Ready For UAT"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            "customfield_epic_link": "HMKPOS-EPIC",
        },
    }
    raw_task_scheduled = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task 101 Scheduled",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
            "customfield_10108": "2026-11-18",
            "customfield_10109": "2026-11-20",
            "customfield_baseline_end": "2026-11-20",
            "customfield_proposed_end": "2026-11-25",
            "customfield_reschedule_reason": "Extra requirement",
            "customfield_reschedule_status": "Chờ duyệt",
            "customfield_reschedule_requester": "requester.user",
            "parent": {
                "key": "HMKPOS-100",
                "fields": {"issuetype": {"name": "Story", "subtask": False}},
            },
        },
    }
    raw_task_unscheduled = {
        "key": "HMKPOS-102",
        "fields": {
            "summary": "Task 102 Unscheduled",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
            "parent": {
                "key": "HMKPOS-100",
                "fields": {"issuetype": {"name": "Story", "subtask": False}},
            },
        },
    }
    raw_report = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "User Report 1",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    all_transitions = [
        "Schedule",
        "Start",
        "Submit QC",
        "Done",
        "Start Test",
        "QC Pass",
        "QC Fail",
        "Cancel",
        "Request Reschedule",
        "Approve Reschedule",
        "Reject Reschedule",
        "Block",
        "Unblock",
        "Confirm No Impact",
        "Client UAT",
        "UAT Sign-off",
        "UAT Fail",
        "Start Triage",
        "Close Report",
        "Set Due Date",
    ]
    return {
        "issues": {
            "HMKPOS-OPS": ops_epic,
            "HMKPOS-EPIC": feature_epic,
            "HMKPOS-100": raw_story,
            "HMKPOS-101": raw_task_scheduled,
            "HMKPOS-102": raw_task_unscheduled,
            "HMKPOS-REP1": raw_report,
        },
        "transitions": {
            "HMKPOS-OPS": all_transitions,
            "HMKPOS-EPIC": all_transitions,
            "HMKPOS-100": all_transitions,
            "HMKPOS-101": all_transitions,
            "HMKPOS-102": all_transitions,
            "HMKPOS-1000": all_transitions,
            "HMKPOS-1001": all_transitions,
            "HMKPOS-REP1": all_transitions,
        },
    }


def test_happy_path_exact_call_sequence_schedule(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.schedule(
        "HMKPOS-102", start=TODAY, end=date(2026, 11, 20), estimate="2d"
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-102", "Schedule")
    assert gw.calls[2][2]["fields"] == {
        "customfield_10108": "2026-11-18",
        "customfield_10109": "2026-11-20",
        "timetracking": {"originalEstimate": "2d"},
    }


def test_happy_path_exact_call_sequence_start(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.start("HMKPOS-101")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-101", "Start")
    assert gw.calls[2][2]["fields"] is None


def test_happy_path_exact_call_sequence_log_work(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.log_work("HMKPOS-101", time_spent="1h", note="Work note")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "add_worklog"]
    assert gw.calls[1][1] == ("HMKPOS-101", "1h", "Work note")


def test_happy_path_exact_call_sequence_submit_qc(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.submit_qc("HMKPOS-101", time_spent="1h", note="Ready for QC")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "get_transitions",
        "add_worklog",
        "transition",
    ]
    assert gw.calls[2][1] == ("HMKPOS-101", "1h", "Ready for QC")
    assert gw.calls[3][1] == ("HMKPOS-101", "Submit QC")


def test_happy_path_exact_call_sequence_finish(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.finish("HMKPOS-101", time_spent="1h", note="Completed task")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "get_transitions",
        "add_worklog",
        "transition",
    ]
    assert gw.calls[2][1] == ("HMKPOS-101", "1h", "Completed task")
    assert gw.calls[3][1] == ("HMKPOS-101", "Done")


def test_happy_path_exact_call_sequence_block(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.block("HMKPOS-101", reason="Wait for API", blocked_by="Nội bộ")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-101", "Block")
    assert gw.calls[2][2]["fields"]["customfield_block_reason"] == "Wait for API"
    assert gw.calls[2][2]["fields"]["customfield_blocked_by"] == {"value": "Nội bộ"}
    assert "customfield_flagged" not in gw.calls[2][2]["fields"]


def test_happy_path_exact_call_sequence_unblock(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.unblock("HMKPOS-101", note="Resolved API issue")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-101", "Unblock")
    assert gw.calls[2][2]["fields"] is None
    assert gw.calls[2][2]["comment"] == "Resolved API issue"


def test_happy_path_exact_call_sequence_request_reschedule(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.request_reschedule(
        "HMKPOS-101", new_end=date(2026, 11, 25), reason="Extra requirement"
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-101", "Request Reschedule")
    assert gw.calls[2][2]["fields"]["customfield_proposed_end"] == "2026-11-25"
    assert gw.calls[2][2]["fields"]["customfield_reschedule_reason"] == {
        "value": "Extra requirement"
    }
    assert "customfield_reschedule_requester" not in gw.calls[2][2]["fields"]


def test_happy_path_exact_call_sequence_confirm_no_impact(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-101"]["fields"]["assignee"] = {"name": "test_user"}
    data["issues"]["HMKPOS-101"]["fields"]["customfield_health_flags"] = [
        {"value": "NEED_RESCHEDULE"}
    ]
    gw = FakeGateway(
        issues=data["issues"], transitions=data["transitions"], user="test_user"
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.confirm_no_impact("HMKPOS-101", note="Overtime to keep schedule")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "current_user",
        "get_transitions",
        "transition",
    ]
    assert gw.calls[3][1] == ("HMKPOS-101", "Confirm No Impact")
    assert gw.calls[3][2]["fields"] is None
    assert gw.calls[3][2]["comment"] == "Overtime to keep schedule"


def test_happy_path_exact_call_sequence_end_day(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(
            key="HMKPOS-101", time_spent="2h", note="End day summary", move_to=None
        )
    ]
    res = actions.end_day(entries)
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_issue", "add_worklog"]
    assert gw.calls[2][1] == ("HMKPOS-101", "2h", "End day summary")


def test_happy_path_exact_call_sequence_qc_start(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_start("HMKPOS-101")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-101", "Start Test")


def test_happy_path_exact_call_sequence_qc_pass(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_pass("HMKPOS-101", time_spent="1h", note="All tests pass")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "get_transitions",
        "add_worklog",
        "transition",
    ]
    assert gw.calls[2][1] == ("HMKPOS-101", "1h", "All tests pass")
    assert gw.calls[3][1] == ("HMKPOS-101", "QC Pass")


def test_happy_path_exact_call_sequence_qc_fail_work_item(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_fail("HMKPOS-101", time_spent="1h", reason="Found regression")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "get_issue",
        "get_transitions",
        "add_worklog",
        "transition",
    ]
    assert gw.calls[3][1] == ("HMKPOS-101", "1h", "Found regression")
    assert gw.calls[4][1] == ("HMKPOS-101", "QC Fail")


def test_qc_start_adhoc_task(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "QC TO DO"},
            "assignee": {"name": "dev.user"},
            "customfield_qc_owner": {"name": "qc.tester"},
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc},
        transitions={"HMKPOS-50": ["Start Test"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_start("HMKPOS-50")
    assert res.ok is True
    assert res.next_actions == ["qc_pass", "qc_fail"]
    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1] == ("HMKPOS-50", "Start Test")


def test_qc_pass_adhoc_task_no_story_worklog_rule(cfg, cal):
    # For story, qc_pass without time_spent requires user worklog.
    # For adhoc, no Story worklog rule: note is required, but time_spent is optional even without prior worklog.
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Test In Progress"},
            "assignee": {"name": "dev.user"},
            "customfield_qc_owner": {"name": "qc.tester"},
            "worklog": {"worklogs": [], "total": 0},
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc},
        transitions={"HMKPOS-50": ["QC Pass", "QC Fail"]},
        user="qc.tester",
    )
    actions = _make_actions(gw, cfg, cal)
    # Calling qc_pass with note only (no time_spent) succeeds
    res = actions.qc_pass("HMKPOS-50", note="Verified fix on staging")
    assert res.ok is True
    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1] == ("HMKPOS-50", "QC Pass")


def test_submit_qc_adhoc_with_open_subtasks_refused(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "customfield_qc_owner": {"name": "qc.tester"},
        },
    }
    raw_sub = {
        "key": "HMKPOS-51",
        "fields": {
            "summary": "Dev task for adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-50",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc, "HMKPOS-51": raw_sub},
        transitions={"HMKPOS-50": ["Submit QC"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.submit_qc("HMKPOS-50", time_spent="1h")
    assert res.ok is False
    assert "HMKPOS-51" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_qc_pass_adhoc_with_open_subtasks_refused(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Test In Progress"},
            "customfield_qc_owner": {"name": "qc.tester"},
        },
    }
    raw_sub = {
        "key": "HMKPOS-51",
        "fields": {
            "summary": "Bug sub for adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug", "subtask": True},
            "status": {"name": "DEV To Do"},
            "parent": {
                "key": "HMKPOS-50",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc, "HMKPOS-51": raw_sub},
        transitions={"HMKPOS-50": ["QC Pass"]},
        user="qc.tester",
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_pass("HMKPOS-50", time_spent="1h", note="Testing done")
    assert res.ok is False
    assert "HMKPOS-51" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_submit_qc_bug_standalone_with_open_subtasks_refused(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_sub = {
        "key": "HMKPOS-61",
        "fields": {
            "summary": "Dev task for bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-60",
                "fields": {"issuetype": {"name": "Bug stand-alone", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-60": raw_bug, "HMKPOS-61": raw_sub},
        transitions={"HMKPOS-60": ["Submit QC"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.submit_qc("HMKPOS-60", time_spent="1h")
    assert res.ok is False
    assert "HMKPOS-61" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_qc_pass_bug_standalone_with_open_subtasks_refused(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Test In Progress"},
        },
    }
    raw_sub = {
        "key": "HMKPOS-61",
        "fields": {
            "summary": "Dev task for bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
            "parent": {
                "key": "HMKPOS-60",
                "fields": {"issuetype": {"name": "Bug stand-alone", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-60": raw_bug, "HMKPOS-61": raw_sub},
        transitions={"HMKPOS-60": ["QC Pass"]},
        user="qc.tester",
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_pass("HMKPOS-60", time_spent="1h", note="Testing done")
    assert res.ok is False
    assert "HMKPOS-61" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_finish_adhoc_with_open_subtasks_refused(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "customfield_qc_owner": {"name": "qc.tester"},
        },
    }
    raw_sub = {
        "key": "HMKPOS-51",
        "fields": {
            "summary": "Dev task for adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-50",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc, "HMKPOS-51": raw_sub},
        transitions={"HMKPOS-50": ["Done"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.finish("HMKPOS-50", time_spent="1h", note="Finish note")
    assert res.ok is False
    assert "HMKPOS-51" in res.message
    assert "sub-task chưa hoàn thành" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_finish_bug_standalone_with_open_subtasks_refused(cfg, cal):
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_sub = {
        "key": "HMKPOS-61",
        "fields": {
            "summary": "Dev task for bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-60",
                "fields": {"issuetype": {"name": "Bug stand-alone", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-60": raw_bug, "HMKPOS-61": raw_sub},
        transitions={"HMKPOS-60": ["Done"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.finish("HMKPOS-60", time_spent="1h", note="Finish note")
    assert res.ok is False
    assert "HMKPOS-61" in res.message
    assert "sub-task chưa hoàn thành" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_end_day_done_with_open_subtasks_refused_in_dry_run(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    raw_sub = {
        "key": "HMKPOS-51",
        "fields": {
            "summary": "Dev task for adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-50",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc, "HMKPOS-51": raw_sub},
        transitions={"HMKPOS-50": ["Done"]},
    )
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(key="HMKPOS-50", time_spent="1h", note="Done adhoc", move_to="done")
    ]
    res = actions.end_day(entries, dry_run=True)
    assert res.ok is False
    assert "HMKPOS-51" in res.message
    assert "sub-task chưa hoàn thành" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_submit_qc_and_qc_pass_with_closed_subtasks_allowed(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-70",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "customfield_qc_owner": {"name": "qc.tester"},
        },
    }
    raw_sub_done = {
        "key": "HMKPOS-71",
        "fields": {
            "summary": "Dev task for adhoc done",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Done"},
            "parent": {
                "key": "HMKPOS-70",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    raw_sub_cancelled = {
        "key": "HMKPOS-72",
        "fields": {
            "summary": "Dev task for adhoc cancelled",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Cancelled"},
            "parent": {
                "key": "HMKPOS-70",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={
            "HMKPOS-70": raw_adhoc,
            "HMKPOS-71": raw_sub_done,
            "HMKPOS-72": raw_sub_cancelled,
        },
        transitions={"HMKPOS-70": ["Submit QC"]},
        user="qc.tester",
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.submit_qc("HMKPOS-70")
    assert res.ok is True
    assert any(c[0] == "transition" for c in gw.calls)


def test_qc_fail_adhoc_task_no_parent_story_or_open_bug_rule(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Test In Progress"},
            "assignee": {"name": "dev.user"},
            "customfield_qc_owner": {"name": "qc.tester"},
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc},
        transitions={"HMKPOS-50": ["QC Pass", "QC Fail"]},
        user="qc.tester",
    )
    actions = _make_actions(gw, cfg, cal)

    # 1. qc_fail without bugs succeeds (no open-Bug rule, no parent Story needed)
    res = actions.qc_fail("HMKPOS-50", reason="Still reproduces on staging")
    assert res.ok is True
    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1] == ("HMKPOS-50", "QC Fail")

    # 2. qc_fail with bugs creates bug subtasks under adhoc task
    res_bugs = actions.qc_fail(
        "HMKPOS-50", reason="Found sub-bug", bugs=["Null pointer on login"]
    )
    assert res_bugs.ok is True
    create_calls = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(create_calls) == 1
    assert create_calls[0][1][0]["parent"]["key"] == "HMKPOS-50"
    assert "Null pointer on login" in create_calls[0][1][0]["summary"]


def test_end_day_adhoc_ready_for_qc_without_owner_fails_entry_and_runs_others(cfg, cal):
    raw_dev_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Dev task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            # no qc_owner
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-101": raw_dev_task, "HMKPOS-50": raw_adhoc},
        transitions={
            "HMKPOS-101": ["Submit QC", "Done"],
            "HMKPOS-50": ["Submit QC", "Done"],
        },
    )
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(key="HMKPOS-101", time_spent="2h", note="Work done", move_to=None),
        EndDayEntry(
            key="HMKPOS-50", time_spent="1h", note="Done adhoc", move_to="ready_for_qc"
        ),
    ]
    res = actions.end_day(entries)

    # Whole batch result is not ok because one entry failed
    assert res.ok is False
    # HMKPOS-101 succeeded
    assert "HMKPOS-101: Thành công" in res.message
    # HMKPOS-50 failed with item-2 message
    assert "HMKPOS-50: Thất bại" in res.message
    assert "finish" in res.message.lower()
    assert (
        "ai" in res.message.lower()
        or "qc_owner" in res.message.lower()
        or "test" in res.message.lower()
    )

    # HMKPOS-101 worklog was actually written (still ran)
    wl_calls = [
        c for c in gw.calls if c[0] == "add_worklog" and c[1][0] == "HMKPOS-101"
    ]
    assert len(wl_calls) == 1
    assert wl_calls[0][1] == ("HMKPOS-101", "2h", "Work done")

    # HMKPOS-50 transition was not executed
    trans_calls = [
        c for c in gw.calls if c[0] == "transition" and c[1][0] == "HMKPOS-50"
    ]
    assert trans_calls == []
    # HMKPOS-50 worklog was not executed
    assert not any(c[0] == "add_worklog" and c[1][0] == "HMKPOS-50" for c in gw.calls)
    assert "chưa log time" in res.message.lower() or "thời gian" in res.message.lower()


def test_end_day_adhoc_ready_for_qc_without_owner_dry_run_shows_failure(cfg, cal):
    raw_dev_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Dev task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            # no qc_owner
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-101": raw_dev_task, "HMKPOS-50": raw_adhoc},
        transitions={
            "HMKPOS-101": ["Submit QC", "Done"],
            "HMKPOS-50": ["Submit QC", "Done"],
        },
    )
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(key="HMKPOS-101", time_spent="2h", note="Work done", move_to=None),
        EndDayEntry(
            key="HMKPOS-50", time_spent="1h", note="Done adhoc", move_to="ready_for_qc"
        ),
    ]
    res = actions.end_day(entries, dry_run=True)
    assert res.ok is False
    assert "HMKPOS-50: Thất bại" in res.message
    assert "chưa log time" in res.message.lower() or "thời gian" in res.message.lower()
    assert "qc_owner" in res.message.lower()
    assert "HMKPOS-101: Thành công" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_end_day_bug_ready_for_qc_without_owner_fails_entry_and_runs_others(cfg, cal):
    raw_dev_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Dev task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            # no qc_owner
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-101": raw_dev_task, "HMKPOS-60": raw_bug},
        transitions={
            "HMKPOS-101": ["Submit QC", "Done"],
            "HMKPOS-60": ["Submit QC", "Done"],
        },
    )
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(key="HMKPOS-101", time_spent="2h", note="Work done", move_to=None),
        EndDayEntry(
            key="HMKPOS-60", time_spent="1h", note="Done bug", move_to="ready_for_qc"
        ),
    ]
    res = actions.end_day(entries)

    assert res.ok is False
    assert "HMKPOS-101: Thành công" in res.message
    assert "HMKPOS-60: Thất bại" in res.message
    assert "Bug stand-alone" in res.message
    assert "cần có QC Owner" in res.message
    assert "finish" not in res.message.lower()

    # HMKPOS-101 worklog written
    wl_calls = [
        c for c in gw.calls if c[0] == "add_worklog" and c[1][0] == "HMKPOS-101"
    ]
    assert len(wl_calls) == 1
    assert wl_calls[0][1] == ("HMKPOS-101", "2h", "Work done")

    # HMKPOS-60 worklog and transition not executed
    trans_calls = [
        c for c in gw.calls if c[0] == "transition" and c[1][0] == "HMKPOS-60"
    ]
    assert trans_calls == []
    assert not any(c[0] == "add_worklog" and c[1][0] == "HMKPOS-60" for c in gw.calls)


def test_end_day_bug_ready_for_qc_without_owner_dry_run_shows_failure(cfg, cal):
    raw_dev_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Dev task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    raw_bug = {
        "key": "HMKPOS-60",
        "fields": {
            "summary": "Standalone bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
            # no qc_owner
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-101": raw_dev_task, "HMKPOS-60": raw_bug},
        transitions={
            "HMKPOS-101": ["Submit QC", "Done"],
            "HMKPOS-60": ["Submit QC", "Done"],
        },
    )
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(key="HMKPOS-101", time_spent="2h", note="Work done", move_to=None),
        EndDayEntry(
            key="HMKPOS-60", time_spent="1h", note="Done bug", move_to="ready_for_qc"
        ),
    ]
    res = actions.end_day(entries, dry_run=True)
    assert res.ok is False
    assert "HMKPOS-60: Thất bại" in res.message
    assert "Bug stand-alone" in res.message
    assert "cần có QC Owner" in res.message
    assert "finish" not in res.message.lower()
    assert "HMKPOS-101: Thành công" in res.message
    assert not any(c[0] in ("add_worklog", "transition") for c in gw.calls)


def test_happy_path_exact_call_sequence_flow_reschedule(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.flow_reschedule(
        "HMKPOS-100", new_due=date(2026, 11, 25), reason="Customer delay"
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-100", "Request Reschedule")
    assert gw.calls[2][2]["fields"]["customfield_proposed_end"] == "2026-11-25"
    assert gw.calls[2][2]["fields"]["customfield_reschedule_reason"] == {
        "value": "Customer delay"
    }
    assert "customfield_reschedule_requester" not in gw.calls[2][2]["fields"]


def test_happy_path_exact_call_sequence_uat_start(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.uat(
        "HMKPOS-EPIC", actor="CH - CỬA HÀNG", action="start", note="Starting UAT"
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["search", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-100", "Client UAT")


def test_happy_path_exact_call_sequence_uat_signoff(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Wait For Review"
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.uat(
        "HMKPOS-EPIC",
        actor="CH - CỬA HÀNG",
        action="signoff",
        note="Client signoff accepted",
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["search", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-100", "UAT Sign-off")
    assert gw.calls[2][2]["comment"] == "Client signoff accepted"


def test_happy_path_exact_call_sequence_uat_fail(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Wait For Review"
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.uat(
        "HMKPOS-EPIC",
        actor="CH - CỬA HÀNG",
        action="fail",
        note="UI bug on checkout",
        story_keys=["HMKPOS-100"],
        bugs={"HMKPOS-100": ["Button misalignment"]},
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "search",
        "get_transitions",
        "create_issue",
        "transition",
    ]
    assert gw.calls[2][1][0]["summary"] == "[CH][BUG] Button misalignment"
    assert gw.calls[3][1] == ("HMKPOS-100", "UAT Fail")


def test_happy_path_exact_call_sequence_decide_reschedule_approve(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(
        issues=data["issues"], transitions=data["transitions"], user="approver.user"
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.decide_reschedule("HMKPOS-101", approve=True, note="Approved")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "current_user",
        "get_transitions",
        "transition",
    ]
    assert gw.calls[3][1] == ("HMKPOS-101", "Approve Reschedule")
    assert gw.calls[3][2]["fields"] is None
    assert gw.calls[3][2]["comment"] == "Phê duyệt dời lịch: Approved"


def test_happy_path_exact_call_sequence_decide_reschedule_reject(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(
        issues=data["issues"], transitions=data["transitions"], user="approver.user"
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.decide_reschedule(
        "HMKPOS-101", approve=False, note="Rejected deadline cannot be moved"
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "current_user",
        "get_transitions",
        "transition",
    ]
    assert gw.calls[3][1] == ("HMKPOS-101", "Reject Reschedule")
    assert gw.calls[3][2]["fields"] is None
    assert (
        gw.calls[3][2]["comment"]
        == "Từ chối dời lịch: Rejected deadline cannot be moved"
    )


def test_happy_path_exact_call_sequence_cancel(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.cancel("HMKPOS-101", reason="Project scope changed")
    assert res.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-101", "Cancel")
    assert gw.calls[2][2]["comment"] == "Hủy: Project scope changed"


def test_happy_path_exact_call_sequence_triage_open(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.triage_open(
        project="HMKPOS",
        text="Cannot checkout error 500",
        source_url="https://chat.google.com/room/1",
        reported_by="Store Manager",
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "search",
        "create_issue",
        "get_transitions",
        "transition",
    ]
    assert gw.calls[1][1][0]["summary"] == "Cannot checkout error 500"
    assert gw.calls[3][1] == ("HMKPOS-1001", "Start Triage")


def test_happy_path_exact_call_sequence_triage_close(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="guidance",
        note="Explained how to login",
        time_spent="15m",
    )
    assert res.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "get_transitions",
        "add_worklog",
        "transition",
    ]
    assert gw.calls[2][1] == ("HMKPOS-REP1", "15m", "Explained how to login")
    assert gw.calls[3][1] == ("HMKPOS-REP1", "Close Report")
    assert gw.calls[3][2]["fields"]["resolution"] == {"name": "User Guidance"}


# ---------------------------------------------------------------------------
# Pre-checks & Validation Tests (Items 4, 5, 6, 10)
# ---------------------------------------------------------------------------


def test_end_day_fails_if_any_sub_result_fails(cfg, cal):
    data = _base_fixtures(cfg)
    # HMKPOS-102 has no "Submit QC" transition
    data["transitions"]["HMKPOS-102"] = ["Schedule"]
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(key="HMKPOS-101", time_spent="1h", note="Entry 1", move_to=None),
        EndDayEntry(
            key="HMKPOS-102", time_spent="1h", note="Entry 2", move_to="ready_for_qc"
        ),
    ]
    res = actions.end_day(entries)
    assert res.ok is False
    assert "HMKPOS-102" in res.message
    # Pre-validation fails, so zero writes occurred
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    actual_writes = [c for c in gw.calls if c[0] in write_methods]
    assert actual_writes == []


def test_empty_or_whitespace_comment_refused(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(
        issues=data["issues"], transitions=data["transitions"], user="approver.user"
    )
    actions = _make_actions(gw, cfg, cal)

    # cancel requires non-empty reason
    assert actions.cancel("HMKPOS-101", reason="   ").ok is False
    assert actions.cancel("HMKPOS-101", reason="").ok is False

    # qc_pass requires non-empty note
    assert actions.qc_pass("HMKPOS-101", time_spent="1h", note="   ").ok is False
    assert actions.qc_pass("HMKPOS-101", time_spent="1h", note="").ok is False

    # qc_fail requires non-empty reason
    assert actions.qc_fail("HMKPOS-101", time_spent="1h", reason="   ").ok is False
    assert actions.qc_fail("HMKPOS-101", time_spent="1h", reason="").ok is False

    # uat signoff requires non-empty note
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Wait For Review"
    assert (
        actions.uat(
            "HMKPOS-EPIC", actor="CH - CỬA HÀNG", action="signoff", note="   "
        ).ok
        is False
    )

    # uat fail requires non-empty note
    assert (
        actions.uat(
            "HMKPOS-EPIC",
            actor="CH - CỬA HÀNG",
            action="fail",
            note="   ",
            story_keys=["HMKPOS-100"],
            bugs={"HMKPOS-100": ["Bug"]},
        ).ok
        is False
    )

    # decide_reschedule reject requires non-empty note
    assert (
        actions.decide_reschedule("HMKPOS-101", approve=False, note="   ").ok is False
    )

    # confirm_no_impact requires non-empty note
    assert actions.confirm_no_impact("HMKPOS-101", note="   ").ok is False


def test_block_requires_reason_and_blocked_by(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)

    res1 = actions.block("HMKPOS-101", reason="", blocked_by="Nội bộ")
    assert res1.ok is False
    assert "lý do" in res1.message.lower() or "reason" in res1.message.lower()

    res2 = actions.block("HMKPOS-101", reason="Wait for API", blocked_by="")
    assert res2.ok is False
    assert "blocked_by" in res2.message.lower() or "đối tượng" in res2.message.lower()


def test_decide_reschedule_requires_pending_status(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-101"]["fields"]["customfield_reschedule_status"] = None
    gw = FakeGateway(
        issues=data["issues"], transitions=data["transitions"], user="approver.user"
    )
    actions = _make_actions(gw, cfg, cal)

    res = actions.decide_reschedule("HMKPOS-101", approve=True, note="OK")
    assert res.ok is False
    assert "trạng thái" in res.message.lower() or "pending" in res.message.lower()


def test_qc_fail_work_item_parent_not_story_refused(cfg, cal):
    data = _base_fixtures(cfg)
    # HMKPOS-101 parent is an Ad-hoc Task instead of Story
    data["issues"]["HMKPOS-100"]["fields"]["issuetype"]["name"] = "Ad-hoc Task"
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)

    res = actions.qc_fail("HMKPOS-101", time_spent="1h", reason="Bug found")
    assert res.ok is False
    assert "story" in res.message.lower()


def test_searches_narrowed_by_epic_and_type(cfg, cal):
    data = _base_fixtures(cfg)

    def search_validator(jql: str, issue: dict) -> bool:
        # Search must explicitly filter project and issuetype
        assert "project =" in jql, f"JQL must filter project: {jql}"
        assert "issuetype in" in jql, f"JQL must narrow issuetype: {jql}"
        return True

    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        search_filter=search_validator,
    )
    actions = _make_actions(gw, cfg, cal)

    # triage_open searches for current OPS epic
    res1 = actions.triage_open(
        "HMKPOS", text="Bug report", source_url="url", reported_by="User"
    )
    assert res1.ok is True

    # uat searches for stories under epic
    res2 = actions.uat(
        "HMKPOS-EPIC", actor="CH - CỬA HÀNG", action="start", note="Start UAT"
    )
    assert res2.ok is True


def test_triage_close_second_call_refused_no_second_worklog(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        status_after={"Close Report": "Done"},
    )
    actions = _make_actions(gw, cfg, cal)

    # First call succeeds
    res1 = actions.triage_close(
        "HMKPOS-REP1", verdict="guidance", note="Guide 1", time_spent="15m"
    )
    assert res1.ok is True
    wl_calls_1 = [c for c in gw.calls if c[0] == "add_worklog"]
    assert len(wl_calls_1) == 1

    # Second call is refused because issue is now resolved
    res2 = actions.triage_close(
        "HMKPOS-REP1", verdict="guidance", note="Guide 2", time_spent="15m"
    )
    assert res2.ok is False
    assert "đã được xử lý" in res2.message.lower() or "resolved" in res2.message.lower()
    # Still only 1 worklog call
    wl_calls_2 = [c for c in gw.calls if c[0] == "add_worklog"]
    assert len(wl_calls_2) == 1


def test_triage_close_prod_bug_missing_ops_epic_refused_even_in_dry_run(cfg, cal):
    data = _base_fixtures(cfg)
    # Delete OPS epic
    del data["issues"]["HMKPOS-OPS"]
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)

    # Production bug with bug_story_key=None needs OPS epic
    res_dry = actions.triage_close(
        "HMKPOS-REP1",
        verdict="bug",
        note="Prod bug",
        time_spent="15m",
        bug_story_key=None,
        dry_run=True,
    )
    assert res_dry.ok is False
    assert "ops" in res_dry.message.lower()

    res_live = actions.triage_close(
        "HMKPOS-REP1",
        verdict="bug",
        note="Prod bug",
        time_spent="15m",
        bug_story_key=None,
        dry_run=False,
    )
    assert res_live.ok is False
    assert "ops" in res_live.message.lower()


def test_triage_close_link_target_type_validation(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)

    # Feature request linked to dev_task (instead of Story) is refused
    res_feat = actions.triage_close(
        "HMKPOS-REP1",
        verdict="feature",
        note="Feat",
        time_spent="15m",
        link_key="HMKPOS-101",
    )
    assert res_feat.ok is False
    assert "story" in res_feat.message.lower()

    # Data fix linked to Story (instead of Ad-hoc Task) is refused
    res_data = actions.triage_close(
        "HMKPOS-REP1",
        verdict="data_fix",
        note="Data fix",
        time_spent="15m",
        link_key="HMKPOS-100",
    )
    assert res_data.ok is False
    assert "ad-hoc" in res_data.message.lower()

    # Duplicate linked to Story (instead of Report) is refused
    res_dup = actions.triage_close(
        "HMKPOS-REP1",
        verdict="duplicate",
        note="Duplicate",
        time_spent="15m",
        link_key="HMKPOS-100",
    )
    assert res_dup.ok is False
    assert "report" in res_dup.message.lower()


def test_uat_actor_matching_and_story_keys_batch_refusal(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)

    # Actor code prefix: "CH - KHO" matches component "CH - CỬA HÀNG" (prefix CH)
    res_match = actions.uat(
        "HMKPOS-EPIC", actor="CH - KHO", action="start", note="Start UAT", dry_run=True
    )
    assert res_match.ok is True

    # Actor code prefix: "POS - THU NGÂN" does NOT match component "CH - CỬA HÀNG"
    res_no_match = actions.uat(
        "HMKPOS-EPIC",
        actor="POS - THU NGÂN",
        action="start",
        note="Start UAT",
        dry_run=True,
    )
    assert res_no_match.ok is False

    # story_keys with invalid story refuses entire batch before any write
    res_batch = actions.uat(
        "HMKPOS-EPIC",
        actor="CH - CỬA HÀNG",
        action="start",
        note="Start UAT",
        story_keys=["HMKPOS-100", "HMKPOS-999"],
    )
    assert res_batch.ok is False
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    actual_writes = [c for c in gw.calls if c[0] in write_methods]
    assert actual_writes == []


def test_start_with_dates_lacking_start_transition_makes_zero_writes(cfg, cal):
    raw_task = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Task without baseline",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
        },
    }
    # Gateway has Schedule, but LACKS Start
    gateway = FakeGateway(
        issues={"HMKPOS-101": raw_task},
        transitions={"HMKPOS-101": ["Schedule"]},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.start(
        "HMKPOS-101", start=TODAY, end=date(2026, 11, 25), estimate="2d"
    )
    assert res.ok is False
    assert "start" in res.message.lower()
    trans_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert trans_calls == []


def test_end_day_resolved_key_refused_in_prevalidation_zero_writes(cfg, cal):
    data = _base_fixtures(cfg)
    # HMKPOS-103 is resolved (Done)
    data["issues"]["HMKPOS-103"] = {
        "key": "HMKPOS-103",
        "fields": {
            "summary": "Done Task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Done"},
            "resolution": {"name": "Done"},
        },
    }
    data["transitions"]["HMKPOS-103"] = []
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(key="HMKPOS-101", time_spent="1h", note="Entry 1", move_to=None),
        EndDayEntry(key="HMKPOS-103", time_spent="1h", note="Entry 2", move_to=None),
    ]
    res = actions.end_day(entries)
    assert res.ok is False
    assert "HMKPOS-103" in res.message
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    actual_writes = [c for c in gw.calls if c[0] in write_methods]
    assert actual_writes == []


def test_end_day_write_phase_failure_collects_subresults(cfg, cal):
    from mcp_atlassian.eggs.gateway import JiraError

    data = _base_fixtures(cfg)

    class FailingTransitionGateway(FakeGateway):
        def transition(self, key, transition_name, fields=None, comment=None):
            if key == "HMKPOS-102":
                raise JiraError("Jira transition failure on HMKPOS-102")
            super().transition(key, transition_name, fields=fields, comment=comment)

    gw = FailingTransitionGateway(
        issues=data["issues"], transitions=data["transitions"]
    )
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(
            key="HMKPOS-101", time_spent="1h", note="Work 1", move_to="ready_for_qc"
        ),
        EndDayEntry(
            key="HMKPOS-102", time_spent="1h", note="Work 2", move_to="ready_for_qc"
        ),
    ]
    res = actions.end_day(entries)
    assert res.ok is False
    assert "HMKPOS-101" in res.message
    assert "HMKPOS-102" in res.message


def test_end_day_success_message_lists_per_key_results(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(
            key="HMKPOS-101", time_spent="1h", note="Work 1", move_to="ready_for_qc"
        ),
        EndDayEntry(key="HMKPOS-102", time_spent="2h", note="Work 2", move_to=None),
    ]
    res = actions.end_day(entries)
    assert res.ok is True
    assert "HMKPOS-101" in res.message
    assert "HMKPOS-102" in res.message


def test_triage_open_lacking_start_triage_returns_ok_with_warning(cfg, cal):
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year),
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Dev In Progress"},
        },
    }
    # Transitions for the new report (HMKPOS-1001) does NOT include "Start Triage"
    gateway = FakeGateway(
        issues={"HMKPOS-OPS": ops_epic},
        transitions={"HMKPOS-1001": []},
    )
    actions = _make_actions(gateway, cfg, cal)
    res = actions.triage_open(
        project="HMKPOS",
        text="Cannot login with OTP",
        source_url="https://chat.google.com/room/123",
        reported_by="Store Manager",
    )
    assert res.ok is True
    assert "HMKPOS-1001" in res.message
    assert "Open" in res.message
    # Issue was created, but no transition was called
    created_calls = [c for c in gateway.calls if c[0] == "create_issue"]
    assert len(created_calls) == 1
    trans_calls = [c for c in gateway.calls if c[0] == "transition"]
    assert trans_calls == []


def test_request_reschedule_interrupted_by_nonexistent_refused(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.request_reschedule(
        "HMKPOS-101",
        new_end=date(2026, 11, 25),
        reason=cfg.reschedule_interrupt_value,
        interrupted_by="NONEXISTENT-999",
    )
    assert res.ok is False
    assert "NONEXISTENT-999" in res.message
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    actual_writes = [c for c in gw.calls if c[0] in write_methods]
    assert actual_writes == []


def test_request_reschedule_links_before_transition(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.request_reschedule(
        "HMKPOS-101",
        new_end=date(2026, 11, 25),
        reason=cfg.reschedule_interrupt_value,
        interrupted_by="HMKPOS-102",
    )
    assert res.ok is True
    call_names = [c[0] for c in gw.calls]
    assert "link" in call_names
    assert "transition" in call_names
    assert call_names.index("link") < call_names.index("transition")


def test_triage_close_validates_note_time_spent_and_link_targets_before_write(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)

    # Empty note
    res_no_note = actions.triage_close(
        "HMKPOS-REP1", verdict="guidance", note="", time_spent="15m"
    )
    assert res_no_note.ok is False
    assert (
        "note" in res_no_note.message.lower()
        or "ghi chú" in res_no_note.message.lower()
    )

    # Empty time_spent
    res_no_time = actions.triage_close(
        "HMKPOS-REP1", verdict="guidance", note="Guidance note", time_spent=""
    )
    assert res_no_time.ok is False
    assert (
        "time_spent" in res_no_time.message.lower()
        or "thời gian" in res_no_time.message.lower()
    )

    # Non-existent link target for feature
    res_no_target = actions.triage_close(
        "HMKPOS-REP1",
        verdict="feature",
        note="Feature request",
        time_spent="15m",
        link_key="NONEXIST-1",
    )
    assert res_no_target.ok is False
    assert "NONEXIST-1" in res_no_target.message

    # Non-existent bug_story_key
    res_no_story = actions.triage_close(
        "HMKPOS-REP1",
        verdict="bug",
        note="Bug report",
        time_spent="15m",
        bug_story_key="NONEXIST-2",
    )
    assert res_no_story.ok is False
    assert "NONEXIST-2" in res_no_story.message

    # Assert zero writes occurred
    write_methods = {
        "transition",
        "update_fields",
        "add_comment",
        "add_worklog",
        "create_issue",
        "link",
    }
    actual_writes = [c for c in gw.calls if c[0] in write_methods]
    assert actual_writes == []


def test_block_allowed_values_from_config(cal):
    data = _base_fixtures(
        EggsConfig(
            lead_user="cuong.huynh",
            jira_url="https://jira.eggstech.io.vn",
            blocked_by_values=["Internal", "Client", "Third-party"],
            link_types={
                "interrupts": "is interrupted by",
                "causes": "causes",
                "relates": "relates to",
                "duplicates": "duplicates",
            },
            projects={
                "HMKPOS": ProjectConfig(
                    key="HMKPOS",
                    default_lead_ba="cuong.huynh",
                    triage_owner="triage.qc",
                )
            },
        )
    )
    cfg_custom = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        blocked_by_values=["Internal", "Client", "Third-party"],
        link_types={
            "interrupts": "Interrupts",
            "causes": "Causes",
            "relates": "Relates",
            "duplicates": "Duplicate",
            "blocks": "Blocks",
        },
        projects={
            "HMKPOS": ProjectConfig(
                key="HMKPOS", default_lead_ba="cuong.huynh", triage_owner="triage.qc"
            )
        },
        statuses={
            "open": "Open",
            "todo": "DEV To Do",
            "in_progress": "Dev In Progress",
            "done": "Done",
        },
        transitions={"block": "Block"},
        fields={
            "flagged": "customfield_flagged",
            "blocked_by": "customfield_blocked_by",
            "block_reason": "customfield_block_reason",
            "blocked_since": "customfield_blocked_since",
        },
    )
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg_custom, cal)

    # Allowed value from config succeeds
    res_ok = actions.block(
        "HMKPOS-101", reason="Waiting for internal team", blocked_by="Internal"
    )
    assert res_ok.ok is True

    # Old Vietnamese string refused when config specifies custom values
    res_fail = actions.block(
        "HMKPOS-101", reason="Waiting for internal team", blocked_by="Nội bộ"
    )
    assert res_fail.ok is False
    assert "blocked_by" in res_fail.message


def test_flow_reschedule_open_and_analyzing_uses_set_due(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Open"
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)

    # 1. Story in Open uses set_due transition
    res_open = actions.flow_reschedule(
        "HMKPOS-100", new_due=date(2026, 11, 28), reason="Initial scope estimation"
    )
    assert res_open.ok is True
    assert [c[0] for c in gw.calls] == ["get_issue", "get_transitions", "transition"]
    assert gw.calls[2][1] == ("HMKPOS-100", "Set Due Date")
    assert gw.calls[2][2]["fields"] == {"duedate": "2026-11-28"}

    # 2. Story in Analyzing uses set_due transition
    gw.calls.clear()
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Analyzing"
    res_analyzing = actions.flow_reschedule(
        "HMKPOS-100", new_due=date(2026, 11, 29), reason="BA analysis updated"
    )
    assert res_analyzing.ok is True
    assert gw.calls[2][1] == ("HMKPOS-100", "Set Due Date")
    assert gw.calls[2][2]["fields"] == {"duedate": "2026-11-29"}

    # 3. Story in Ready For Dev uses request_reschedule
    gw.calls.clear()
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Ready For Dev"
    res_ready = actions.flow_reschedule(
        "HMKPOS-100", new_due=date(2026, 11, 30), reason="Need more time"
    )
    assert res_ready.ok is True
    assert gw.calls[2][1] == ("HMKPOS-100", "Request Reschedule")
    assert gw.calls[2][2]["fields"]["customfield_proposed_end"] == "2026-11-30"


def test_story_qc_pass_worklog_requirement(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Test In Progress"
    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        user="qc_user",
    )
    actions = _make_actions(gw, cfg, cal)

    # Case A: No time_spent, and caller has NO existing worklog -> refused
    res_no_wl = actions.qc_pass("HMKPOS-100", note="QC Pass without time")
    assert res_no_wl.ok is False
    assert "worklog trên Story" in res_no_wl.message

    # Case B: time_spent provided -> logs work first, then transitions
    gw.calls.clear()
    res_with_time = actions.qc_pass(
        "HMKPOS-100", time_spent="2h", note="Tested all scenarios"
    )
    assert res_with_time.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "get_transitions",
        "add_worklog",
        "transition",
    ]
    assert gw.calls[2][1] == ("HMKPOS-100", "2h", "Tested all scenarios")
    assert gw.calls[3][1] == ("HMKPOS-100", "QC Pass")

    # Case C: No time_spent, but caller already has a worklog on the Story -> allowed
    gw.calls.clear()
    res_existing_wl = actions.qc_pass("HMKPOS-100", note="QC Pass existing worklog")
    assert res_existing_wl.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "current_user",
        "get_issue",
        "get_transitions",
        "transition",
    ]
    assert gw.calls[4][1] == ("HMKPOS-100", "QC Pass")


def test_story_qc_pass_fetches_full_worklogs_when_total_exceeds_len(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Test In Progress"
    # 20 worklogs inline by others, but total is 25
    data["issues"]["HMKPOS-100"]["fields"]["worklog"] = {
        "total": 25,
        "worklogs": [{"author": {"name": f"dev_{i}"}} for i in range(20)],
    }
    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        user="qc_user",
    )
    # Full worklog list includes qc_user
    gw.worklogs = [
        Worklog(
            issue_key="HMKPOS-100",
            author="qc_user",
            started=datetime.now(timezone.utc),
            seconds=3600,
        )
    ]

    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_pass("HMKPOS-100", note="QC Pass with paginated worklogs")
    assert res.ok is True
    assert any(c[0] == "get_worklogs" for c in gw.calls)


def test_no_hasattr_test_fake_branches_in_actions():
    actions_path = (
        Path(__file__).parents[2] / "src" / "mcp_atlassian" / "eggs" / "actions.py"
    )
    code = actions_path.read_text(encoding="utf-8")
    assert 'hasattr(self.gateway, "issues")' not in code
    assert 'hasattr(self.gateway, "worklogs")' not in code


def test_story_qc_fail_bug_subtask_requirement(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Test In Progress"
    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        user="qc_user",
    )
    actions = _make_actions(gw, cfg, cal)

    # Case A: No bugs provided and no existing bug sub-task -> refused
    res_no_bug = actions.qc_fail("HMKPOS-100", reason="Found issues")
    assert res_no_bug.ok is False
    assert "ít nhất 1 Bug sub-task đang mở" in res_no_bug.message

    # Case B: Bugs provided -> creates bug subtask then transitions
    gw.calls.clear()
    res_with_bug = actions.qc_fail(
        "HMKPOS-100", reason="Found broken UI", bugs=["Fix checkout button"]
    )
    assert res_with_bug.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "get_transitions",
        "create_issue",
        "transition",
    ]
    assert gw.calls[2][1][0]["summary"] == "[CH][BUG] Fix checkout button"
    assert gw.calls[3][1] == ("HMKPOS-100", "QC Fail")

    # Case C: No bugs provided, but an open Bug sub-task already exists on the Story
    gw.calls.clear()
    data["issues"]["HMKPOS-100"]["fields"]["subtasks"] = [
        {
            "key": "HMKPOS-199",
            "fields": {
                "issuetype": {"name": "Bug", "subtask": True},
                "status": {"name": "DEV To Do"},
            },
        }
    ]
    res_existing_bug = actions.qc_fail("HMKPOS-100", reason="Already has bug sub-task")
    assert res_existing_bug.ok is True
    assert [c[0] for c in gw.calls] == [
        "get_issue",
        "get_subtasks",
        "get_transitions",
        "transition",
    ]
    assert gw.calls[3][1] == ("HMKPOS-100", "QC Fail")


def test_qc_fail_story_transition_fails_lists_created_bugs(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Test In Progress"
    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        errors_on_transition={
            ("HMKPOS-100", "QC Fail"): JiraError(
                "Transition failed: Field customfield_101 is required",
                status_code=400,
                error_messages=["Trường bắt buộc chưa nhập."],
            )
        },
        user="qc_user",
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_fail(
        "HMKPOS-100",
        reason="Found broken UI",
        bugs=["Fix checkout button", "Fix payment screen"],
    )
    assert res.ok is False
    cr_calls = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(cr_calls) == 2
    assert "HMKPOS-1001" in res.message
    assert "HMKPOS-1002" in res.message
    assert "huỷ" in res.message.lower() or "tái sử dụng" in res.message.lower()


def test_decide_reschedule_self_approval_refusal(cfg, cal):
    data = _base_fixtures(cfg)
    # HMKPOS-101 has reschedule_requester = "requester.user"
    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        user="requester.user",  # same as requester
    )
    actions = _make_actions(gw, cfg, cal)

    # Attempt self-approval
    res = actions.decide_reschedule("HMKPOS-101", approve=True, note="Self approval")
    assert res.ok is False
    assert "Người yêu cầu không được tự duyệt" in res.message


def test_transition_jira_400_error_messages(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    # Simulate Jira 400 error from validator
    gw.errors_on_transition = {
        ("HMKPOS-101", "Cancel"): JiraError(
            "Lỗi từ Jira (400 Bad Request)",
            status_code=400,
            error_messages=["Lý do không hợp lệ."],
            errors={"customfield_101": "Trường này là bắt buộc."},
        )
    }
    actions = _make_actions(gw, cfg, cal)

    res = actions.cancel("HMKPOS-101", reason="Cancelled reason")
    assert res.ok is False
    assert "Lý do không hợp lệ." in res.message
    assert "Trường này là bắt buộc." in res.message


def test_end_day_adhoc_with_open_subtasks_and_no_qc_owner_reports_open_subtasks(
    cfg, cal
):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    raw_sub = {
        "key": "HMKPOS-51",
        "fields": {
            "summary": "Dev task for adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-50",
                "fields": {"issuetype": {"name": "Ad-hoc Task", "subtask": False}},
            },
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc, "HMKPOS-51": raw_sub},
        transitions={"HMKPOS-50": ["Submit QC", "Done"]},
    )
    actions = _make_actions(gw, cfg, cal)
    entries = [
        EndDayEntry(
            key="HMKPOS-50",
            time_spent="1h",
            note="Done adhoc",
            move_to="ready_for_qc",
        ),
    ]
    res = actions.end_day(entries)
    assert res.ok is False
    assert "sub-task chưa hoàn thành" in res.message
    assert "HMKPOS-51" in res.message
    assert "cần có QC Owner" not in res.message


def test_submit_qc_sets_code_missing_qc_owner(cfg, cal):
    raw_adhoc = {
        "key": "HMKPOS-50",
        "fields": {
            "summary": "Tech adhoc",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Ad-hoc Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    raw_bug = {
        "key": "HMKPOS-80",
        "fields": {
            "summary": "Prod bug",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Bug", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "assignee": {"name": "dev.user"},
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-50": raw_adhoc, "HMKPOS-80": raw_bug},
        transitions={"HMKPOS-50": ["Submit QC"], "HMKPOS-80": ["Submit QC"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res_adhoc = actions.submit_qc("HMKPOS-50")
    assert res_adhoc.ok is False
    assert res_adhoc.code == "missing_qc_owner"

    res_bug = actions.submit_qc("HMKPOS-80")
    assert res_bug.ok is False
    assert res_bug.code == "missing_qc_owner"


def test_triage_close_bug_production_no_qc_owner_determined_adds_dev_will_pick_note(
    cfg, cal
):
    ops_epic = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": cfg.ops_epic_summary.format(month=TODAY.month, year=TODAY.year),
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
            "assignee": None,
        },
    }
    cfg_no_triage_owner = replace(
        cfg,
        projects={k: replace(v, triage_owner=None) for k, v in cfg.projects.items()},
    )
    gateway = FakeGateway(
        issues={"HMKPOS-OPS": ops_epic, "HMKPOS-REP1": raw_report},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gateway, cfg_no_triage_owner, cal)
    expected_line = "Không xác định được QC Owner: Dev sẽ chọn khi Submit QC"
    res_dry = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Production hotfix needed",
        time_spent="1h",
        dry_run=True,
    )
    assert res_dry.ok is True
    assert expected_line in res_dry.changes

    res_live = actions.triage_close(
        key="HMKPOS-REP1",
        verdict="bug",
        note="Production hotfix needed",
        time_spent="1h",
        dry_run=False,
    )
    assert res_live.ok is True
    assert expected_line in res_live.changes


def test_qc_fail_non_400_jira_error_without_bugs_returns_not_ok(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-101"]["fields"]["status"]["name"] = "Testing"
    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        errors_on_transition={
            ("HMKPOS-101", "QC Fail"): JiraError(
                "Internal Server Error",
                status_code=500,
            )
        },
        user="qc_user",
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_fail("HMKPOS-101", reason="Failed QC")
    assert res.ok is False
    assert "Internal Server Error" in res.message


def test_qc_fail_transition_fails_after_worklog_reports_time_logged(cfg, cal):
    data = _base_fixtures(cfg)
    data["issues"]["HMKPOS-100"]["fields"]["status"]["name"] = "Test In Progress"
    gw = FakeGateway(
        issues=data["issues"],
        transitions=data["transitions"],
        errors_on_transition={
            ("HMKPOS-100", "QC Fail"): JiraError(
                "Transition failed",
                status_code=400,
                error_messages=["Validation failed."],
            )
        },
        user="qc_user",
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.qc_fail(
        "HMKPOS-100",
        reason="Found broken UI",
        bugs=["Fix checkout button"],
        time_spent="1h",
    )
    assert res.ok is False
    wl_calls = [c for c in gw.calls if c[0] == "add_worklog"]
    assert len(wl_calls) == 1
    assert wl_calls[0][1][:2] == ("HMKPOS-100", "1h")
    assert "Đã log 1h trên HMKPOS-100" in res.message
    assert "HMKPOS-1001" in res.message


def test_link_types_defaults_in_eggs_config():
    c = EggsConfig(lead_user="alice", jira_url="http://jira")
    assert c.link_types == {
        "interrupts": "Interrupts",
        "causes": "Causes",
        "relates": "Relates",
        "duplicates": "Duplicate",
        "blocks": "Blocks",
    }


def test_request_reschedule_interrupted_by_link_direction_and_name(cfg, cal):
    data = _base_fixtures(cfg)
    gw = FakeGateway(issues=data["issues"], transitions=data["transitions"])
    actions = _make_actions(gw, cfg, cal)
    res = actions.request_reschedule(
        "HMKPOS-101",
        new_end=date(2026, 11, 25),
        reason=cfg.reschedule_interrupt_value,
        interrupted_by="HMKPOS-102",
    )
    assert res.ok is True
    link_calls = [c for c in gw.calls if c[0] == "link"]
    assert len(link_calls) == 1
    # <interrupter> interrupts <delayed issue> -> inward_key=interrupted_by, outward_key=key
    assert link_calls[0][1] == ("Interrupts", "HMKPOS-102", "HMKPOS-101")


def test_triage_close_bug_sub_link_direction_and_name(cfg, cal):
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
    gw = FakeGateway(
        issues={"HMKPOS-REP1": raw_report, "HMKPOS-100": raw_story},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.triage_close(
        "HMKPOS-REP1",
        verdict="bug",
        bug_story_key="HMKPOS-100",
        note="Bug in story",
        time_spent="30m",
    )
    assert res.ok is True
    link_calls = [c for c in gw.calls if c[0] == "link"]
    assert len(link_calls) == 1
    # <bug> causes <User Report> -> inward_key=bug_key, outward_key=report key
    assert link_calls[0][1] == ("Causes", "HMKPOS-1001", "HMKPOS-REP1")


def test_triage_close_bug_standalone_link_direction_and_name(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "Standalone bug report",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    raw_ops = {
        "key": "HMKPOS-OPS",
        "fields": {
            "summary": "[OPS] Vận hành & Hotfix 11/2026",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Open"},
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-REP1": raw_report, "HMKPOS-OPS": raw_ops},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.triage_close(
        "HMKPOS-REP1",
        verdict="bug",
        note="Stand-alone hotfix bug",
        time_spent="30m",
    )
    assert res.ok is True
    link_calls = [c for c in gw.calls if c[0] == "link"]
    assert len(link_calls) == 1
    # <bug> causes <User Report> -> inward_key=bug_key, outward_key=report key
    assert link_calls[0][1] == ("Causes", "HMKPOS-1001", "HMKPOS-REP1")


def test_triage_close_duplicate_link_direction_and_name(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "Duplicate report",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    raw_rep2 = {
        "key": "HMKPOS-REP2",
        "fields": {
            "summary": "Original report",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
        },
    }
    gw = FakeGateway(
        issues={"HMKPOS-REP1": raw_report, "HMKPOS-REP2": raw_rep2},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.triage_close(
        "HMKPOS-REP1",
        verdict="duplicate",
        link_key="HMKPOS-REP2",
        note="Duplicate of REP2",
        time_spent="30m",
    )
    assert res.ok is True
    link_calls = [c for c in gw.calls if c[0] == "link"]
    assert len(link_calls) == 1
    # <report> duplicates <other report> -> inward_key=key, outward_key=link_key
    assert link_calls[0][1] == ("Duplicate", "HMKPOS-REP1", "HMKPOS-REP2")


def test_triage_close_relates_link_direction_and_name(cfg, cal):
    raw_report = {
        "key": "HMKPOS-REP1",
        "fields": {
            "summary": "Feature request report",
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
    gw = FakeGateway(
        issues={"HMKPOS-REP1": raw_report, "HMKPOS-100": raw_story},
        transitions={"HMKPOS-REP1": ["Close Report"]},
    )
    actions = _make_actions(gw, cfg, cal)
    res = actions.triage_close(
        "HMKPOS-REP1",
        verdict="feature",
        link_key="HMKPOS-100",
        note="Related feature story",
        time_spent="30m",
    )
    assert res.ok is True
    link_calls = [c for c in gw.calls if c[0] == "link"]
    assert len(link_calls) == 1
    assert link_calls[0][1] == ("Relates", "HMKPOS-REP1", "HMKPOS-100")


def test_create_issue_qc_creates_bug_related_to_eggsbox_38(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_bug = {
        "key": "EGGSBOX-38",
        "fields": {
            "summary": "Existing bug",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "QC TO DO"},
            "assignee": {"name": "cuong.huynh"},
        },
    }
    gw = FakeGateway(issues={"EGGSBOX-38": raw_bug})
    actions = _make_actions(gw, cfg, cal)

    # QC creates a Bug stand-alone related to EGGSBOX-38
    res = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug",
        summary="New bug found in testing",
        related_to="EGGSBOX-38",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res.ok is True
    created_calls = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(created_calls) == 1
    created_fields = created_calls[0][1][0]
    # Assignee defaults to related issue's assignee
    assert created_fields["assignee"] == {"name": "cuong.huynh"}
    # qc_owner defaults to creator when creator has qc role
    assert created_fields[cfg.fields["qc_owner"]] == {"name": "tuan.bui"}

    # Relates link inward/outward as gateway.link expects
    link_calls = [c for c in gw.calls if c[0] == "link"]
    assert len(link_calls) == 1
    new_key = [k for k in gw.issues if k != "EGGSBOX-38"][0]
    assert link_calls[0][1] == ("Relates", new_key, "EGGSBOX-38")


def test_create_issue_dev_creates_bug_and_adhoc(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    gw = FakeGateway()
    actions = _make_actions(gw, cfg, cal)

    # Dev creates Bug stand-alone (must supply qc_owner since dev does not have qc role)
    res_bug_no_qc = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug",
        summary="Dev found bug",
        creator="cuong.huynh",
        creator_roles={"dev"},
    )
    assert res_bug_no_qc.ok is False
    assert res_bug_no_qc.missing == ["qc_owner"]

    res_bug = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug",
        summary="Dev found bug",
        qc_owner="tuan.bui",
        creator="cuong.huynh",
        creator_roles={"dev"},
    )
    assert res_bug.ok is True

    # Dev creates Ad-hoc Task
    res_adhoc = actions.create_issue(
        project="EGGSBOX",
        issue_type="adhoc",
        summary="Ad-hoc task from dev",
        creator="cuong.huynh",
        creator_roles={"dev"},
    )
    assert res_adhoc.ok is True


def test_create_issue_dev_cannot_create_story_and_unauthorized_refused(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    gw = FakeGateway()
    actions = _make_actions(gw, cfg, cal)

    # Dev asking for Story is refused
    res_story = actions.create_issue(
        project="EGGSBOX",
        issue_type="story",
        summary="Dev story",
        creator="cuong.huynh",
        creator_roles={"dev"},
    )
    assert res_story.ok is False
    assert "Bạn không có quyền tạo story" in res_story.message

    # User with no roles is refused
    res_stranger = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug",
        summary="Stranger bug",
        creator="stranger",
        creator_roles=set(),
    )
    assert res_stranger.ok is False
    assert "Bạn không có quyền tạo bug" in res_stranger.message


def test_create_issue_validations(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    gw = FakeGateway()
    gw._components = ["CH - CỬA HÀNG"]
    actions = _make_actions(gw, cfg, cal)

    # Story without epic -> missing=["epic"]
    res_no_epic = actions.create_issue(
        project="EGGSBOX",
        issue_type="story",
        summary="Story without epic",
        creator="lead.ba",
        creator_roles={"lead_ba"},
    )
    assert res_no_epic.ok is False
    assert res_no_epic.missing == ["epic"]

    # Component not in project -> refused before writing
    res_bad_comp = actions.create_issue(
        project="EGGSBOX",
        issue_type="adhoc",
        summary="Adhoc with bad component",
        component="UNKNOWN - COMPONENT",
        creator="lead.ba",
        creator_roles={"lead_ba"},
    )
    assert res_bad_comp.ok is False
    assert "Chưa có Component UNKNOWN - COMPONENT" in res_bad_comp.message
    assert not any(c[0] == "create_issue" for c in gw.calls)

    # Dry run writes nothing
    res_dry = actions.create_issue(
        project="EGGSBOX",
        issue_type="adhoc",
        summary="Adhoc dry run",
        creator="lead.ba",
        creator_roles={"lead_ba"},
        dry_run=True,
    )
    assert res_dry.ok is True
    assert "[Dry-run]" in res_dry.message
    assert not any(c[0] == "create_issue" for c in gw.calls)


def test_create_issue_link_failure_and_retry_idempotency(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_bug = {
        "key": "EGGSBOX-38",
        "fields": {
            "summary": "Existing bug",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "QC TO DO"},
            "assignee": {"name": "cuong.huynh"},
        },
    }
    gw = FakeGateway(issues={"EGGSBOX-38": raw_bug})
    actions = _make_actions(gw, cfg, cal)

    # Force link to fail on first attempt
    def fail_link(link_type, inward_key, outward_key):
        raise RuntimeError("Connection error linking")

    gw.link = fail_link

    res1 = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug",
        summary="Bug to retry",
        related_to="EGGSBOX-38",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res1.ok is False
    assert "chưa tạo được liên kết Relates tới EGGSBOX-38" in res1.message
    created_key = [k for k in gw.issues if k != "EGGSBOX-38"][0]
    assert created_key in res1.message

    # Restore normal link
    original_links = []

    def good_link(link_type, inward_key, outward_key):
        gw._record("link", link_type, inward_key, outward_key)
        original_links.append((link_type, inward_key, outward_key))

    gw.link = good_link

    # Retry with same summary + related_to
    res2 = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug",
        summary="Bug to retry",
        related_to="EGGSBOX-38",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res2.ok is True
    assert f"Đã tạo thành công {created_key}." in res2.message
    # Gateway create_issue was called only ONCE across both attempts
    create_calls = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(create_calls) == 1
    # Link was called
    assert len(original_links) == 1
    assert original_links[0] == ("Relates", created_key, "EGGSBOX-38")


def test_create_bug_sub_under_story_in_test_in_progress(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_story = {
        "key": "EGGSBOX-10",
        "fields": {
            "summary": "Order flow",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Test In Progress"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            cfg.fields["qc_owner"]: {"name": "tuan.bui"},
            cfg.fields["qc_fail_count"]: 0,
        },
    }
    raw_dev_task = {
        "key": "EGGSBOX-11",
        "fields": {
            "summary": "Dev task for order flow",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Dev Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "parent": {"key": "EGGSBOX-10"},
            "assignee": {"name": "cuong.huynh"},
        },
    }
    gw = FakeGateway(issues={"EGGSBOX-10": raw_story, "EGGSBOX-11": raw_dev_task})
    actions = _make_actions(gw, cfg, cal)

    # Dry run
    res_dry = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-10",
        summary="nút Lưu không phản hồi",
        creator="tuan.bui",
        creator_roles={"qc"},
        dry_run=True,
    )
    assert res_dry.ok is True
    assert "[Dry-run]" in res_dry.message
    assert any("Parent: EGGSBOX-10" in c for c in res_dry.changes)
    assert any("[CH][BUG] nút Lưu không phản hồi" in c for c in res_dry.changes)
    assert any("Assignee: cuong.huynh" in c for c in res_dry.changes)
    assert any("QC Owner: tuan.bui" in c for c in res_dry.changes)
    assert not any(c[0] == "create_issue" for c in gw.calls)

    # Actual create
    res = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-10",
        summary="nút Lưu không phản hồi",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res.ok is True
    created_calls = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(created_calls) == 1
    created_fields = created_calls[0][1][0]
    assert created_fields["summary"] == "[CH][BUG] nút Lưu không phản hồi"
    assert created_fields["assignee"] == {"name": "cuong.huynh"}
    assert created_fields[cfg.fields["qc_owner"]] == {"name": "tuan.bui"}
    assert created_fields["parent"] == {"key": "EGGSBOX-10"}

    # Parent Story is not transitioned and QC Fail Count untouched
    story_after = gw.issues["EGGSBOX-10"]
    assert story_after["fields"]["status"]["name"] == "Test In Progress"
    assert story_after["fields"][cfg.fields["qc_fail_count"]] == 0
    assert not any(c[0] == "transition" and c[1][0] == "EGGSBOX-10" for c in gw.calls)


def test_create_bug_sub_story_with_multiple_devs_missing(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_story = {
        "key": "EGGSBOX-10",
        "fields": {
            "summary": "Order flow",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Test In Progress"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            cfg.fields["qc_owner"]: {"name": "tuan.bui"},
        },
    }
    raw_dev1 = {
        "key": "EGGSBOX-11",
        "fields": {
            "summary": "Dev task 1",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Dev Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "parent": {"key": "EGGSBOX-10"},
            "assignee": {"name": "dev_a"},
        },
    }
    raw_dev2 = {
        "key": "EGGSBOX-12",
        "fields": {
            "summary": "Dev task 2",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Dev Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "parent": {"key": "EGGSBOX-10"},
            "assignee": {"name": "dev_b"},
        },
    }
    gw = FakeGateway(
        issues={
            "EGGSBOX-10": raw_story,
            "EGGSBOX-11": raw_dev1,
            "EGGSBOX-12": raw_dev2,
        }
    )
    actions = _make_actions(gw, cfg, cal)

    res = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-10",
        summary="Lỗi thanh toán",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res.ok is False
    assert res.missing == ["dev_a", "dev_b"]


def test_create_bug_sub_parent_qc_to_do_refused(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_story = {
        "key": "EGGSBOX-10",
        "fields": {
            "summary": "Order flow",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "QC TO DO"},
        },
    }
    gw = FakeGateway(issues={"EGGSBOX-10": raw_story})
    actions = _make_actions(gw, cfg, cal)

    res = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-10",
        summary="Lỗi",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res.ok is False
    assert "Bắt đầu test trước (eggs_qc_start EGGSBOX-10)" in res.message


def test_create_bug_sub_parent_bug_standalone_refused(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_bug = {
        "key": "EGGSBOX-38",
        "fields": {
            "summary": "Standalone bug",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Bug stand-alone", "subtask": False},
            "status": {"name": "Test In Progress"},
        },
    }
    gw = FakeGateway(issues={"EGGSBOX-38": raw_bug})
    actions = _make_actions(gw, cfg, cal)

    res = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-38",
        summary="Bug under bug",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res.ok is False
    assert "Bug stand-alone không có Bug con" in res.message


def test_create_bug_sub_dev_without_role_and_not_qc_owner_refused(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_story = {
        "key": "EGGSBOX-10",
        "fields": {
            "summary": "Order flow",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Test In Progress"},
            cfg.fields["qc_owner"]: {"name": "tuan.bui"},
        },
    }
    raw_dev = {
        "key": "EGGSBOX-11",
        "fields": {
            "summary": "Dev task",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Dev Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "parent": {"key": "EGGSBOX-10"},
            "assignee": {"name": "cuong.huynh"},
        },
    }
    gw = FakeGateway(issues={"EGGSBOX-10": raw_story, "EGGSBOX-11": raw_dev})
    actions = _make_actions(gw, cfg, cal)

    # Dev who is not qc_owner and lacks qc role is refused
    res_dev = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-10",
        summary="Dev bug",
        creator="cuong.huynh",
        creator_roles={"dev"},
    )
    assert res_dev.ok is False
    assert "Bạn không có quyền tạo bug_sub" in res_dev.message

    # Parent's QC Owner without qc role is allowed
    res_qc_owner = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-10",
        summary="Owner bug",
        creator="tuan.bui",
        creator_roles=set(),
    )
    assert res_qc_owner.ok is True


def test_qc_pass_story_refused_while_bug_sub_open_and_allowed_once_done(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_story = {
        "key": "EGGSBOX-26",
        "fields": {
            "summary": "Story 26",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Test In Progress"},
            cfg.fields["qc_owner"]: {"name": "tuan.bui"},
            "subtasks": [
                {
                    "key": "EGGSBOX-50",
                    "fields": {
                        "issuetype": {"name": "Bug", "subtask": True},
                        "status": {"name": "Dev In Progress"},
                    },
                }
            ],
            "worklog": {
                "worklogs": [{"author": {"name": "tuan.bui"}}],
                "total": 1,
            },
        },
    }
    gw = FakeGateway(
        issues={"EGGSBOX-26": raw_story},
        transitions={"EGGSBOX-26": ["QC Pass", "QC Fail"]},
        user="tuan.bui",
    )
    actions = _make_actions(gw, cfg, cal)

    # QC Pass refused while subtask EGGSBOX-50 is in Dev In Progress
    res_fail = actions.qc_pass("EGGSBOX-26", note="Tested all")
    assert res_fail.ok is False
    assert (
        res_fail.message
        == "EGGSBOX-26 còn bug chưa xong: EGGSBOX-50 (Dev In Progress). QC Pass được khi mọi sub-task Done/Cancelled."
    )

    # Subtask becomes Done
    raw_story["fields"]["subtasks"][0]["fields"]["status"] = {"name": "Done"}
    res_pass = actions.qc_pass("EGGSBOX-26", note="Tested all")
    assert res_pass.ok is True
    assert "Đã xác nhận QC Pass cho EGGSBOX-26." in res_pass.message


def test_create_bug_sub_retry_returns_same_key(cfg, cal):
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    raw_story = {
        "key": "EGGSBOX-10",
        "fields": {
            "summary": "Order flow",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Test In Progress"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            cfg.fields["qc_owner"]: {"name": "tuan.bui"},
        },
    }
    raw_dev = {
        "key": "EGGSBOX-11",
        "fields": {
            "summary": "Dev task",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Dev Task", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "parent": {"key": "EGGSBOX-10"},
            "assignee": {"name": "cuong.huynh"},
        },
    }
    gw = FakeGateway(issues={"EGGSBOX-10": raw_story, "EGGSBOX-11": raw_dev})
    actions = _make_actions(gw, cfg, cal)

    res1 = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-10",
        summary="nút Lưu không phản hồi",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res1.ok is True
    created_key = [k for k in gw.issues if k not in ("EGGSBOX-10", "EGGSBOX-11")][0]

    # Retry with same parent and summary
    res2 = actions.create_issue(
        project="EGGSBOX",
        issue_type="bug_sub",
        parent="EGGSBOX-10",
        summary="nút Lưu không phản hồi",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res2.ok is True
    assert f"Đã tạo thành công {created_key}." in res2.message
    # Only 1 create_issue call recorded
    create_calls = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(create_calls) == 1


def test_create_component_and_components_summary(cfg, cal):
    from mcp_atlassian.eggs.config import ProjectConfig

    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="cuong.huynh",
        triage_owner="tuan.bui",
    )
    user_gw = FakeGateway(components_map={"EGGSBOX": ["CH - CỬA HÀNG"]})
    bot_gw = FakeGateway(components_map={"EGGSBOX": ["CH - CỬA HÀNG"]})
    actions = _make_actions(user_gw, cfg, cal, bot_gateway=bot_gw)

    # 1. QC creates KT - KẾ TOÁN
    res = actions.create_component(
        project="EGGSBOX",
        code="KT",
        name="Kế toán",
        description="Bộ phận kế toán",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res.ok is True
    assert "KT - KẾ TOÁN" in res.message
    # bot gateway used for the write
    bot_create_calls = [c for c in bot_gw.calls if c[0] == "create_component"]
    assert len(bot_create_calls) == 1
    args = bot_create_calls[0][1]
    assert args[0] == "EGGSBOX"
    assert args[1] == "KT - KẾ TOÁN"
    # description records caller
    assert "Tạo qua /eggs bởi tuan.bui, 2026-11-18." in args[2]
    # user gateway was not used for write
    assert not any(c[0] == "create_component" for c in user_gw.calls)

    # 2. dev refused
    res_dev = actions.create_component(
        project="EGGSBOX",
        code="NV",
        name="Nhân viên",
        creator="cuong.huynh",
        creator_roles={"dev"},
    )
    assert res_dev.ok is False
    assert "Bạn không có quyền tạo component trong dự án EGGSBOX" in res_dev.message

    # 3. duplicate code refused
    res_dup = actions.create_component(
        project="EGGSBOX",
        code="CH",
        name="Cửa hàng phụ",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res_dup.ok is False
    assert "Mã Component 'CH' đã được sử dụng bởi CH - CỬA HÀNG" in res_dup.message

    # 4. 'cua hang' vs existing 'CH - CỬA HÀNG' refused as similar
    res_similar = actions.create_component(
        project="EGGSBOX",
        code="CHM",
        name="cua hang",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res_similar.ok is False
    assert res_similar.message == (
        "Đã có Component gần giống: CH - CỬA HÀNG. "
        "Dùng Component này, hoặc đặt tên khác nếu thật sự là Actor mới."
    )

    # 5. invalid code 'kt1' refused
    res_invalid_code = actions.create_component(
        project="EGGSBOX",
        code="kt1",
        name="Kiểm toán",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res_invalid_code.ok is False
    assert "Mã Component 'kt1' không hợp lệ" in res_invalid_code.message

    # 6. dry run writes nothing
    res_dry = actions.create_component(
        project="EGGSBOX",
        code="KTM",
        name="Kế toán mới",
        creator="tuan.bui",
        creator_roles={"qc"},
        dry_run=True,
    )
    assert res_dry.ok is True
    assert res_dry.message == (
        'Sẽ làm: tạo Component "KTM - KẾ TOÁN MỚI" trong EGGSBOX '
        "(bot eggsbot, ghi người yêu cầu tuan.bui)."
    )
    assert len([c for c in bot_gw.calls if c[0] == "create_component"]) == 1

    # 7. eggs_create_issue with unknown component refused with hint
    res_unknown = actions.create_issue(
        project="EGGSBOX",
        issue_type="adhoc",
        summary="Test Adhoc",
        component="UNKNOWN",
        creator="tuan.bui",
        creator_roles={"qc"},
    )
    assert res_unknown.ok is False
    assert (
        "Chưa có Component UNKNOWN trong EGGSBOX. "
        "Chọn một Component có sẵn: CH - CỬA HÀNG hoặc tạo mới bằng eggs_create_component."
        in res_unknown.message
    )

    # 8. eggs_components counts open Stories
    gw_comp = FakeGateway(
        components_map={"EGGSBOX": ["CH - CỬA HÀNG", "KH - KHÁCH HÀNG"]},
        issues={
            "EGGSBOX-1": {
                "key": "EGGSBOX-1",
                "fields": {
                    "project": {"key": "EGGSBOX"},
                    "issuetype": {"name": "Story"},
                    "status": {"name": "Open", "statusCategory": {"key": "new"}},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                },
            },
            "EGGSBOX-2": {
                "key": "EGGSBOX-2",
                "fields": {
                    "project": {"key": "EGGSBOX"},
                    "issuetype": {"name": "Story"},
                    "status": {
                        "name": "In Progress",
                        "statusCategory": {"key": "indeterminate"},
                    },
                    "components": [{"name": "CH - CỬA HÀNG"}],
                },
            },
            "EGGSBOX-3": {
                "key": "EGGSBOX-3",
                "fields": {
                    "project": {"key": "EGGSBOX"},
                    "issuetype": {"name": "Story"},
                    "status": {"name": "Done", "statusCategory": {"key": "done"}},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                },
            },
            "EGGSBOX-4": {
                "key": "EGGSBOX-4",
                "fields": {
                    "project": {"key": "EGGSBOX"},
                    "issuetype": {"name": "Story"},
                    "status": {"name": "Open", "statusCategory": {"key": "new"}},
                    "components": [{"name": "KH - KHÁCH HÀNG"}],
                },
            },
        },
    )
    actions_comp = _make_actions(gw_comp, cfg, cal)
    summary_text = actions_comp.components("EGGSBOX")
    assert "CH - CỬA HÀNG: 2 open Stories" in summary_text
    assert "KH - KHÁCH HÀNG: 1 open Stories" in summary_text


def test_update_description(cfg, cal):
    from mcp_atlassian.eggs.config import ProjectConfig

    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX",
        default_lead_ba="ba.user",
        triage_owner="qc.user",
    )

    story_desc_orig = (
        "Title line\n\n"
        "## Acceptance Criteria\n"
        "- Old AC 1\n"
        "- Old AC 2\n\n"
        "## Technical Notes\n"
        "Keep this exactly."
    )
    issues = {
        "EGGSBOX-1": {
            "key": "EGGSBOX-1",
            "fields": {
                "project": {"key": "EGGSBOX"},
                "issuetype": {"name": "Story"},
                "status": {"name": "Open"},
                "description": "Old Story description",
            },
        },
        "EGGSBOX-2": {
            "key": "EGGSBOX-2",
            "fields": {
                "project": {"key": "EGGSBOX"},
                "issuetype": {"name": "Story"},
                "status": {"name": "Open"},
                "description": story_desc_orig,
            },
        },
        "EGGSBOX-11": {
            "key": "EGGSBOX-11",
            "fields": {
                "project": {"key": "EGGSBOX"},
                "issuetype": {"name": "Dev Task"},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "cuong.huynh"},
                "description": "Initial dev task note.",
            },
        },
    }
    gw = FakeGateway(issues=issues)
    actions = _make_actions(gw, cfg, cal)

    # 1. BA replaces a Story description
    res_ba = actions.update_description(
        key="EGGSBOX-1",
        text="New Story description",
        mode="replace",
        caller="ba.user",
        caller_roles={"lead_ba"},
    )
    assert res_ba.ok is True
    assert "Đã cập nhật mô tả cho EGGSBOX-1." in res_ba.message
    assert gw.issues["EGGSBOX-1"]["fields"]["description"] == "New Story description"

    # 2. QC updates only the AC section of a Story and the rest is byte-identical
    res_qc = actions.update_description(
        key="EGGSBOX-2",
        text="- New AC 1\n- New AC 2",
        mode="ac",
        caller="qc.user",
        caller_roles={"qc"},
    )
    assert res_qc.ok is True
    new_desc = gw.issues["EGGSBOX-2"]["fields"]["description"]
    assert (
        new_desc.split("## Acceptance Criteria")[0]
        == story_desc_orig.split("## Acceptance Criteria")[0]
    )
    assert (
        new_desc.split("## Technical Notes")[1]
        == story_desc_orig.split("## Technical Notes")[1]
    )
    assert "- New AC 1\n- New AC 2" in new_desc

    # 3. dev who is assignee appends to a Dev Task
    res_dev = actions.update_description(
        key="EGGSBOX-11",
        text="Additional implementation detail.",
        mode="append",
        caller="cuong.huynh",
        caller_roles={"dev"},
    )
    assert res_dev.ok is True
    assert (
        gw.issues["EGGSBOX-11"]["fields"]["description"]
        == "Initial dev task note.\n\nAdditional implementation detail."
    )

    # 4. dev not related to a Story is refused
    res_refused = actions.update_description(
        key="EGGSBOX-1",
        text="Dev trying to edit story",
        mode="replace",
        caller="cuong.huynh",
        caller_roles={"dev"},
    )
    assert res_refused.ok is False
    assert "Bạn không có quyền sửa mô tả" in res_refused.message

    # 5. same text -> no write
    write_calls_before = [
        c for c in gw.calls if c[0] in ("update_fields", "update_issue")
    ]
    res_same = actions.update_description(
        key="EGGSBOX-11",
        text="Initial dev task note.\n\nAdditional implementation detail.",
        mode="replace",
        caller="cuong.huynh",
        caller_roles={"dev"},
    )
    assert res_same.ok is True
    assert res_same.message == "Mô tả không đổi."
    assert [
        c for c in gw.calls if c[0] in ("update_fields", "update_issue")
    ] == write_calls_before

    # 6. dry run writes nothing
    res_dry = actions.update_description(
        key="EGGSBOX-11",
        text="Dry run new text",
        mode="replace",
        caller="cuong.huynh",
        caller_roles={"dev"},
        dry_run=True,
    )
    assert res_dry.ok is True
    assert "Sẽ làm: cập nhật mô tả EGGSBOX-11 (replace)" in res_dry.message
    assert "-Initial dev task note." in res_dry.message
    assert "+Dry run new text" in res_dry.message
    assert [
        c for c in gw.calls if c[0] in ("update_fields", "update_issue")
    ] == write_calls_before

    # 7. the gateway call carries only description
    res_field = actions.update_description(
        key="EGGSBOX-11",
        text="Final text check",
        mode="replace",
        caller="cuong.huynh",
        caller_roles={"dev"},
    )
    assert res_field.ok is True
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) > 0
    last_call = update_calls[-1]
    args = last_call[1]
    assert args[0] == "EGGSBOX-11"
    fields_payload = args[1]
    assert list(fields_payload.keys()) == ["description"]
    assert fields_payload["description"] == "Final text check"
