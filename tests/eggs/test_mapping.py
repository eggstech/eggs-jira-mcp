from datetime import date, datetime
import json
from pathlib import Path
import pytest

from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.mapping import (
    issue_fields_for_write,
    issue_from_json,
    status_changed_at,
)
from mcp_atlassian.eggs.model import Issue, Worklog


@pytest.fixture
def base_config():
    return EggsConfig(
        fields={
            "planned_start": "customfield_10108",
            "planned_end": "customfield_10109",
            "baseline_end": "customfield_baseline_end",
            "contributors": "customfield_contributors",
            "qc_owner": "customfield_qc_owner",
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
            "todo": "DEV To Do",
            "in_progress": "Dev In Progress",
            "ready_for_qc": "QC TO DO",
            "testing": "Test In Progress",
            "done": "Done",
            "cancelled": "Cancelled",
        },
        issue_types={
            "epic": ["Epic"],
            "story": ["Story"],
            "tech_task": ["Tech Task", "Ad-hoc Task"],
            "bug": ["Bug", "Bug stand-alone"],
            "flow": ["Flow", "Dev Task"],
            "flow_bug": ["Flow Bug", "Bug"],
            "tech_subtask": ["Tech Sub-task", "Sub-task"],
        },
        projects={
            "HMKLOYAL": ProjectConfig(key="HMKLOYAL", default_lead_ba="cuong.huynh", frozen=False),
            "BUL": ProjectConfig(key="BUL", default_lead_ba="cuong.huynh", frozen=True),
        },
    )


def test_mapping_issue_flow_fixture(base_config):
    fixture_path = Path(__file__).parent / "fixtures" / "issue_flow.json"
    raw = json.loads(fixture_path.read_text())

    issue = issue_from_json(raw, base_config)
    assert issue.key == "HMKLOYAL-123"
    assert issue.project == "HMKLOYAL"
    assert issue.type == "flow"
    assert issue.status == "ready_for_qc"
    assert issue.summary == "[CH-01] Dang nhap bang OTP"
    assert issue.assignee == "nguyen.van.a"
    assert issue.contributors == ("tran.thi.b", "le.van.c")
    assert issue.qc_owner == "qc.tester"
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
    assert issue.people == frozenset({"nguyen.van.a", "tran.thi.b", "le.van.c"})
    assert issue.status_changed_at is not None
    assert issue.status_changed_at.year == 2026
    assert issue.status_changed_at.month == 11
    assert issue.status_changed_at.day == 5


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
    assert issue.contributors == ()
    assert issue.qc_owner is None
    assert issue.parent_key is None
    assert issue.epic_key is None
    assert issue.planned_start is None
    assert issue.planned_end is None
    assert issue.baseline_end is None
    assert issue.due_date is None
    assert issue.estimate_seconds is None
    assert issue.health_flags == frozenset()
    assert issue.escalation_level == 0
    assert issue.escalation_owner is None
    assert issue.health_since is None
    assert issue.status_changed_at is None
    assert issue.people == frozenset()
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
        contributors=["user1", "user2"],
    )
    assert payload["customfield_health_flags"] == [{"value": "STALE"}]
    assert payload["customfield_escalation_owner"] == {"name": "a.b"}
    assert payload["customfield_escalation_level"] == 2
    assert payload["customfield_10109"] == "2026-11-22"
    assert payload["customfield_contributors"] == [{"name": "user1"}, {"name": "user2"}]
