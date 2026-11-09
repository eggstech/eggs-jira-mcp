from datetime import datetime, timezone
import pytest

from mcp_atlassian.eggs.gateway import JiraError, JiraGateway
from mcp_atlassian.eggs.model import Worklog
from tests.eggs.fakes import FakeGateway


def test_fake_gateway_implements_protocol():
    fake = FakeGateway(issues={}, worklogs=[], transitions={})
    assert isinstance(fake, JiraGateway)


def test_transition_not_available_raises_jira_error():
    fake = FakeGateway(
        issues={"PROJ-1": {"key": "PROJ-1", "fields": {"status": {"name": "Open"}}}},
        transitions={"PROJ-1": ["Start", "Cancel"]}
    )
    with pytest.raises(JiraError, match="Transition 'QC Pass' not available for PROJ-1"):
        fake.transition("PROJ-1", "QC Pass")

    # Happy path transition works
    fake.transition("PROJ-1", "Start")
    assert any(c[0] == "transition" and c[1] == ("PROJ-1", "Start") for c in fake.calls)


def test_update_fields_recorded_and_mutates():
    fake = FakeGateway(
        issues={"PROJ-1": {"key": "PROJ-1", "fields": {"summary": "Original", "customfield_10108": "2026-11-01"}}}
    )
    fake.update_fields("PROJ-1", {"customfield_10108": "2026-11-10"}, override_screen_security=True)

    # Assert record contains override_screen_security flag
    matching_calls = [
        c for c in fake.calls
        if c[0] == "update_fields" and c[2].get("override_screen_security") is True
    ]
    assert len(matching_calls) == 1
    assert fake.issues["PROJ-1"]["fields"]["customfield_10108"] == "2026-11-10"


def test_create_issue_and_get():
    fake = FakeGateway(issues={})
    key = fake.create_issue({"project": {"key": "PROJ"}, "summary": "New Issue"})
    assert key.startswith("PROJ-")
    fetched = fake.get_issue(key)
    assert fetched["fields"]["summary"] == "New Issue"


def test_worklogs_since():
    w1 = Worklog(issue_key="P-1", author="alice", started=datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc), seconds=3600)
    w2 = Worklog(issue_key="P-2", author="bob", started=datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc), seconds=7200)
    fake = FakeGateway(issues={}, worklogs=[w1, w2])

    logs = fake.worklogs_since(datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc))
    assert len(logs) == 1
    assert logs[0].author == "bob"
