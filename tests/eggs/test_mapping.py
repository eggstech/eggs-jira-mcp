from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.mapping import (
    CLEAR,
    issue_fields_for_write,
    issue_from_json,
    parse_jira_dt,
    status_changed_at,
)
from mcp_atlassian.eggs.model import Issue, Worklog


@pytest.fixture
def base_config():
    return EggsConfig(
        lead_user="cuong.huynh",
        fields={
            "planned_start": "customfield_10108",
            "planned_end": "customfield_10109",
            "baseline_end": "customfield_baseline_end",
            "part": "customfield_part",
            "qc_owner": "customfield_qc_owner",
            "unscheduled_count": "customfield_unscheduled_count",
            "health_flags": "customfield_health_flags",
            "escalation_level": "customfield_escalation_level",
            "escalation_owner": "customfield_escalation_owner",
            "health_since": "customfield_health_since",
            "epic_link": "customfield_epic_link",
            "blocked_by": "customfield_blocked_by",
            "blocked_since": "customfield_blocked_since",
            "reschedule_status": "customfield_reschedule_status",
            "reschedule_requester": "customfield_reschedule_requester",
            "proposed_end": "customfield_proposed_end",
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
        },
        projects={
            "HMKLOYAL": ProjectConfig(key="HMKLOYAL", default_lead_ba="cuong.huynh", frozen=False),
            "BUL": ProjectConfig(key="BUL", default_lead_ba="cuong.huynh", frozen=True),
        },
    )


def test_issue_actor_code_and_work_item():
    # 1 component with prefix
    i1 = Issue(
        key="T-1", project="T", type="dev_task", status="todo", summary="S",
        assignee="dev", components=("CH - CỬA HÀNG",), part="BE", parent_type="story",
        qc_owner=None, parent_key=None, epic_key=None, created=None,
        status_changed_at=None, planned_start=None, planned_end=None,
        baseline_end=None, due_date=None, estimate_seconds=None,
        reschedule_status=None, reschedule_requester=None, proposed_end=None,
        blocked=False, blocked_by=None, blocked_since=None,
        health_flags=frozenset(), health_since=None, escalation_level=0,
        escalation_owner=None, frozen=False,
    )
    assert i1.actor_code == "CH"
    assert i1.is_work_item is True

    # Zero components
    i0 = Issue(
        key="T-0", project="T", type="story", status="todo", summary="S",
        assignee="ba", components=(), part=None, parent_type=None,
        qc_owner="qc", parent_key=None, epic_key=None, created=None,
        status_changed_at=None, planned_start=None, planned_end=None,
        baseline_end=None, due_date=None, estimate_seconds=None,
        reschedule_status=None, reschedule_requester=None, proposed_end=None,
        blocked=False, blocked_by=None, blocked_since=None,
        health_flags=frozenset(), health_since=None, escalation_level=0,
        escalation_owner=None, frozen=False,
    )
    assert i0.actor_code is None
    assert i0.is_work_item is False

    # Two components
    i2 = Issue(
        key="T-2", project="T", type="dev_task", status="todo", summary="S",
        assignee="dev", components=("CH - CỬA HÀNG", "KT - KẾ TOÁN"), part="BE",
        parent_type="story", qc_owner=None, parent_key=None, epic_key=None,
        created=None, status_changed_at=None, planned_start=None, planned_end=None,
        baseline_end=None, due_date=None, estimate_seconds=None,
        reschedule_status=None, reschedule_requester=None, proposed_end=None,
        blocked=False, blocked_by=None, blocked_since=None,
        health_flags=frozenset(), health_since=None, escalation_level=0,
        escalation_owner=None, frozen=False,
    )
    assert i2.actor_code is None

    # Component without prefix
    i_no_sep = Issue(
        key="T-3", project="T", type="adhoc", status="todo", summary="S",
        assignee="dev", components=("CH",), part=None, parent_type=None,
        qc_owner=None, parent_key=None, epic_key=None, created=None,
        status_changed_at=None, planned_start=None, planned_end=None,
        baseline_end=None, due_date=None, estimate_seconds=None,
        reschedule_status=None, reschedule_requester=None, proposed_end=None,
        blocked=False, blocked_by=None, blocked_since=None,
        health_flags=frozenset(), health_since=None, escalation_level=0,
        escalation_owner=None, frozen=False,
    )
    assert i_no_sep.actor_code is None
    assert i_no_sep.is_work_item is True


def test_mapping_issue_flow_fixture(base_config):
    fixture_path = Path(__file__).parent / "fixtures" / "issue_flow.json"
    raw = json.loads(fixture_path.read_text())

    issue = issue_from_json(raw, base_config)
    assert issue.key == "HMKLOYAL-123"
    assert issue.project == "HMKLOYAL"
    assert issue.type == "dev_task"
    assert issue.status == "ready_for_qc"
    assert issue.summary == "[CH-01] Dang nhap bang OTP"
    assert issue.assignee == "nguyen.van.a"
    assert issue.components == ("CH - CỬA HÀNG",)
    assert issue.actor_code == "CH"
    assert issue.part == "BE"
    assert issue.parent_type == "story"
    assert issue.qc_owner is None  # work items keep None
    assert issue.is_work_item is True
    assert issue.parent_key == "HMKLOYAL-100"
    assert issue.epic_key == "HMKLOYAL-50"
    assert issue.planned_start == date(2026, 11, 10)
    assert issue.planned_end == date(2026, 11, 20)
    assert issue.baseline_end == date(2026, 11, 20)
    assert issue.due_date == date(2026, 11, 25)
    assert issue.estimate_seconds == 28800
    assert issue.health_flags == frozenset({"OVERDUE"})
    assert issue.escalation_level == 1
    assert issue.escalation_owner == "ba.lead"
    assert issue.health_since == date(2026, 11, 15)
    assert issue.frozen is False
    assert issue.is_resolved is False
    assert issue.status_changed_at is not None
    assert issue.status_changed_at.year == 2026
    assert issue.status_changed_at.month == 11
    assert issue.status_changed_at.day == 5


def test_mapping_story_reads_qc_owner(base_config):
    story_raw = {
        "id": "10010",
        "key": "HMKLOYAL-100",
        "fields": {
            "summary": "[CH-01] Dang nhap bang OTP Flow",
            "project": {"key": "HMKLOYAL"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Ready For Dev"},
            "assignee": {"name": "ba.lead"},
            "components": [{"name": "CH - CỬA HÀNG"}],
            "customfield_qc_owner": {"name": "qc.tester"},
            "duedate": "2026-11-25",
        },
    }
    issue = issue_from_json(story_raw, base_config)
    assert issue.type == "story"
    assert issue.components == ("CH - CỬA HÀNG",)
    assert issue.actor_code == "CH"
    assert issue.qc_owner == "qc.tester"
    assert issue.is_work_item is False
    assert issue.part is None


def test_status_changed_at_extracts_latest():
    changelog = {
        "histories": [
            {
                "created": "2026-11-02T09:00:00.000+0700",
                "items": [{"field": "status", "toString": "Dev In Progress"}]
            },
            {
                "created": "2026-11-04T10:00:00.000+0700",
                "items": [{"field": "assignee", "toString": "dev"}]
            },
            {
                "created": "2026-11-05T14:15:00.000+0700",
                "items": [{"field": "status", "toString": "QC TO DO"}]
            }
        ]
    }
    dt = status_changed_at(changelog)
    assert dt is not None
    assert dt.day == 5
    assert dt.hour == 14

    empty_changelog = {"histories": []}
    assert status_changed_at(empty_changelog) is None
    assert status_changed_at({}) is None


def test_unknown_or_none_custom_fields_map_gracefully(base_config):
    minimal_raw = {
        "id": "20001",
        "key": "MIN-1",
        "fields": {
            "summary": "Minimal issue",
            "project": {"key": "ROCKY"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Open"},
            "created": "2026-11-01T00:00:00.000+0700",
        }
    }
    issue = issue_from_json(minimal_raw, base_config)
    assert issue.key == "MIN-1"
    assert issue.project == "ROCKY"
    assert issue.type == "epic"
    assert issue.assignee is None
    assert issue.components == ()
    assert issue.actor_code is None
    assert issue.part is None
    assert issue.parent_type is None
    assert issue.qc_owner is None
    assert issue.is_work_item is False
    assert issue.parent_key is None
    assert issue.epic_key is None
    assert issue.planned_start is None
    assert issue.planned_end is None
    assert issue.baseline_end is None
    assert issue.due_date is None
    assert issue.estimate_seconds is None
    assert issue.health_flags == frozenset()


def test_mapping_retired_parent_type_maps_to_none(base_config):
    raw = {
        "key": "SUB-1",
        "fields": {
            "summary": "Sub under retired Task parent",
            "project": {"key": "ROCKY"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
            "parent": {
                "key": "TASK-1",
                "fields": {
                    "issuetype": {"name": "Task", "subtask": False},
                },
            },
        },
    }
    issue = issue_from_json(raw, base_config)
    assert issue.parent_type is None

    assert issue.escalation_level == 0
    assert issue.escalation_owner is None
    assert issue.health_since is None
    assert issue.status_changed_at is None
    assert issue.frozen is False


def test_legacy_frozen_label_or_frozen_project(base_config):
    # legacy-frozen in labels -> frozen is True
    raw_with_label = {
        "id": "20002",
        "key": "HMKLOYAL-999",
        "fields": {
            "summary": "Old issue",
            "project": {"key": "HMKLOYAL"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "created": "2026-01-01T00:00:00.000+0700",
            "labels": ["some-tag", "legacy-frozen"]
        }
    }
    issue1 = issue_from_json(raw_with_label, base_config)
    assert issue1.frozen is True

    # BUL project is configured as frozen -> frozen is True
    raw_in_frozen_project = {
        "id": "20003",
        "key": "BUL-10",
        "fields": {
            "summary": "Frozen project issue",
            "project": {"key": "BUL"},
            "issuetype": {"name": "Tech Task", "subtask": False},
            "status": {"name": "DEV To Do"},
            "created": "2026-01-01T00:00:00.000+0700",
            "labels": []
        }
    }
    issue2 = issue_from_json(raw_in_frozen_project, base_config)
    assert issue2.frozen is True


def test_issue_fields_for_write(base_config):
    payload = issue_fields_for_write(
        base_config,
        health_flags={"STALE"},
        escalation_owner="a.b",
        escalation_level=2,
        planned_end=date(2026, 11, 22),
        part="BE",
        unscheduled_count=3,
    )
    assert payload["customfield_health_flags"] == [{"value": "STALE"}]
    assert payload["customfield_escalation_owner"] == {"name": "a.b"}
    assert payload["customfield_escalation_level"] == 2
    assert payload["customfield_10109"] == "2026-11-22"
    assert payload["customfield_part"] == {"value": "BE"}
    assert payload["customfield_unscheduled_count"] == 3

    # Select field shape (reschedule_status -> {"value": ...})
    payload_select = issue_fields_for_write(base_config, reschedule_status="Chờ duyệt")
    assert payload_select["customfield_reschedule_status"] == {"value": "Chờ duyệt"}


def test_blocked_derives_only_from_source_signals(base_config):
    # health_flags={"BLOCKED"}, no flag, no blocked_by -> blocked is False
    raw_health_blocked = {
        "id": "1001",
        "key": "P-1",
        "fields": {
            "summary": "Health blocked issue",
            "project": {"key": "HMKLOYAL"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "created": "2026-11-01T08:00:00.000+0700",
            "customfield_health_flags": [{"value": "BLOCKED"}],
        }
    }
    issue1 = issue_from_json(raw_health_blocked, base_config)
    assert issue1.health_flags == frozenset({"BLOCKED"})
    assert issue1.blocked is False

    # Flagged Impediment -> blocked is True
    raw_flagged = {
        "id": "1002",
        "key": "P-2",
        "fields": {
            "summary": "Flagged issue",
            "project": {"key": "HMKLOYAL"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "created": "2026-11-01T08:00:00.000+0700",
            "flagged": [{"value": "Impediment"}],
        }
    }
    issue2 = issue_from_json(raw_flagged, base_config)
    assert issue2.blocked is True

    # blocked_by set -> blocked is True
    raw_blocked_by = {
        "id": "1003",
        "key": "P-3",
        "fields": {
            "summary": "Blocked by issue",
            "project": {"key": "HMKLOYAL"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "created": "2026-11-01T08:00:00.000+0700",
            "customfield_blocked_by": {"value": "Nội bộ"},
        }
    }
    issue3 = issue_from_json(raw_blocked_by, base_config)
    assert issue3.blocked is True


def test_parse_jira_dt(base_config):
    # +HHMM format
    dt1 = parse_jira_dt("2026-11-16T09:00:00.000+0700", base_config)
    assert dt1 is not None
    assert str(dt1.tzinfo) == "Asia/Ho_Chi_Minh"
    assert (dt1.year, dt1.month, dt1.day, dt1.hour, dt1.minute) == (2026, 11, 16, 9, 0)

    # +07:00 format
    dt2 = parse_jira_dt("2026-11-16T09:00:00.000+07:00", base_config)
    assert dt2 is not None
    assert str(dt2.tzinfo) == "Asia/Ho_Chi_Minh"
    assert (dt2.year, dt2.month, dt2.day, dt2.hour, dt2.minute) == (2026, 11, 16, 9, 0)

    # Z format (02:00 UTC == 09:00 Asia/Ho_Chi_Minh)
    dt3 = parse_jira_dt("2026-11-16T02:00:00.000Z", base_config)
    assert dt3 is not None
    assert str(dt3.tzinfo) == "Asia/Ho_Chi_Minh"
    assert (dt3.year, dt3.month, dt3.day, dt3.hour, dt3.minute) == (2026, 11, 16, 9, 0)

    # Missing / empty forms -> None
    assert parse_jira_dt(None, base_config) is None
    assert parse_jira_dt("", base_config) is None
    assert parse_jira_dt("   ", base_config) is None

    # Issue with missing created -> created is None, not datetime.min
    raw_no_created = {
        "id": "1004",
        "key": "P-4",
        "fields": {
            "summary": "No created issue",
            "project": {"key": "HMKLOYAL"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
        }
    }
    issue = issue_from_json(raw_no_created, base_config)
    assert issue.created is None


def test_issue_fields_for_write_clear_and_validation(base_config):
    # 1. CLEAR writes JSON null (None in dict)
    payload = issue_fields_for_write(base_config, escalation_owner=CLEAR)
    assert payload["customfield_escalation_owner"] is None

    # 2. None means skip
    payload_skip = issue_fields_for_write(base_config, escalation_owner=None)
    assert "customfield_escalation_owner" not in payload_skip

    # 3. Missing in cfg.fields raises ValueError naming the logical field
    with pytest.raises(ValueError, match="unknown_field"):
        issue_fields_for_write(base_config, unknown_field="foo")

    # 4. Field with None id in cfg.fields raises ValueError naming the logical field
    cfg_with_null = EggsConfig(
        lead_user="someone",
        fields={"unconfigured_cf": None},
        statuses={},
        issue_types={},
        projects={},
    )
    with pytest.raises(ValueError, match="unconfigured_cf"):
        issue_fields_for_write(cfg_with_null, unconfigured_cf="bar")

    with pytest.raises(ValueError, match="unconfigured_cf"):
        issue_fields_for_write(cfg_with_null, unconfigured_cf=CLEAR)


def test_frozen_label_and_impediment_value_from_config():
    custom_cfg = EggsConfig(
        lead_user="someone",
        frozen_label="custom-frozen",
        flagged_impediment_value="Blocker",
        fields={"flagged": "customfield_flagged"},
        statuses={},
        issue_types={},
        projects={"P": ProjectConfig(key="P", default_lead_ba="ba", frozen=False)},
    )
    raw_issue = {
        "id": "1",
        "key": "P-1",
        "fields": {
            "summary": "Custom issue",
            "project": {"key": "P"},
            "labels": ["custom-frozen"],
            "customfield_flagged": [{"value": "Blocker"}],
        }
    }
    issue = issue_from_json(raw_issue, custom_cfg)
    assert issue.frozen is True
    assert issue.blocked is True


def test_user_report_type_and_fields_mapped(base_config):
    cfg_with_report = EggsConfig(
        lead_user="cuong.huynh",
        fields={
            **base_config.fields,
            "report_source": "customfield_report_source",
            "reported_by": "customfield_reported_by",
            "reported_at": "customfield_reported_at",
        },
        issue_types={
            **base_config.issue_types,
            "report": ["User Report"],
        },
        statuses=base_config.statuses,
    )
    raw = {
        "key": "HMKPOS-500",
        "fields": {
            "summary": "Customer app cannot checkout",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "User Report", "subtask": False},
            "status": {"name": "Analyzing"},
            "created": "2026-11-17T08:00:00.000+0700",
            "customfield_report_source": "https://chat.google.com/room/ABC/msg/123",
            "customfield_reported_by": "Support Team",
            "customfield_reported_at": "2026-11-17T07:45:00.000+0700",
            "resolution": {"name": "Is a Bug"},
        },
    }
    issue = issue_from_json(raw, cfg_with_report)
    assert issue.type == "report"
    assert issue.is_work_item is False
    assert issue.report_source == "https://chat.google.com/room/ABC/msg/123"
    assert issue.reported_by == "Support Team"
    assert issue.reported_at == datetime(2026, 11, 17, 7, 45, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert issue.resolution == "Is a Bug"





