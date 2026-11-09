from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from mcp_atlassian.eggs.gateway import JiraError, JiraGateway
from mcp_atlassian.eggs.gateway_atlassian import AtlassianGateway


@pytest.fixture
def mock_client():
    client = MagicMock()
    return client


def test_atlassian_gateway_implements_protocol(mock_client):
    gateway = AtlassianGateway(mock_client)
    assert isinstance(gateway, JiraGateway)


def test_update_fields_with_override_screen_security(mock_client):
    gateway = AtlassianGateway(mock_client)
    gateway.update_fields("PROJ-123", {"customfield_10108": "2026-11-20"}, override_screen_security=True)

    mock_client.put.assert_called_once_with(
        "rest/api/2/issue/PROJ-123",
        data={"fields": {"customfield_10108": "2026-11-20"}},
        params={"overrideScreenSecurity": "true", "notifyUsers": "false"},
    )


def test_update_fields_without_override(mock_client):
    gateway = AtlassianGateway(mock_client)
    gateway.update_fields("PROJ-123", {"summary": "Updated"})

    mock_client.put.assert_called_once_with(
        "rest/api/2/issue/PROJ-123",
        data={"fields": {"summary": "Updated"}},
        params=None,
    )


def test_transition_looks_up_id_by_name(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "transitions": [
            {"id": "11", "name": "Start"},
            {"id": "21", "name": "QC Pass"},
        ]
    }

    gateway.transition("PROJ-123", "QC Pass", fields={"customfield_1": "val"}, comment="passed")

    mock_client.get.assert_called_once_with("rest/api/2/issue/PROJ-123/transitions")
    mock_client.post.assert_called_once_with(
        "rest/api/2/issue/PROJ-123/transitions",
        data={
            "transition": {"id": "21"},
            "fields": {"customfield_1": "val"},
            "update": {"comment": [{"add": {"body": "passed"}}]},
        },
    )


def test_transition_not_found_raises_jira_error(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "transitions": [{"id": "11", "name": "Start"}]
    }

    with pytest.raises(JiraError, match="Transition 'Invalid' not available for PROJ-123"):
        gateway.transition("PROJ-123", "Invalid")


def test_worklogs_since_chains_endpoints(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "values": [
            {"worklogId": 101, "updatedTime": 1700000000000},
            {"worklogId": 102, "updatedTime": 1700000010000},
        ],
        "until": 1700000020000,
    }
    mock_client.post.return_value = [
        {
            "id": "101",
            "issueId": "10001",
            "author": {"name": "dev1"},
            "started": "2026-11-10T08:00:00.000+0700",
            "timeSpentSeconds": 3600,
        },
        {
            "id": "102",
            "issueId": "10002",
            "author": {"name": "dev2"},
            "started": "2026-11-10T09:00:00.000+0700",
            "timeSpentSeconds": 7200,
        },
    ]
    # For mapping issueId to issueKey
    mock_client.jql.return_value = {
        "startAt": 0,
        "maxResults": 50,
        "total": 2,
        "issues": [
            {"id": "10001", "key": "PROJ-1"},
            {"id": "10002", "key": "PROJ-2"},
        ]
    }

    since = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
    worklogs = gateway.worklogs_since(since)

    since_ms = int(since.timestamp() * 1000)
    mock_client.get.assert_called_once_with(f"rest/api/2/worklog/updated?since={since_ms}")
    mock_client.post.assert_called_once_with("rest/api/2/worklog/list", data={"ids": [101, 102]})
    assert len(worklogs) == 2
    assert worklogs[0].issue_key == "PROJ-1"
    assert worklogs[0].author == "dev1"
    assert worklogs[0].seconds == 3600
    assert worklogs[1].issue_key == "PROJ-2"
    assert worklogs[1].author == "dev2"
    assert worklogs[1].seconds == 7200


def test_search_handles_pagination(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.jql.side_effect = [
        {
            "startAt": 0,
            "maxResults": 2,
            "total": 3,
            "issues": [{"key": "P-1"}, {"key": "P-2"}]
        },
        {
            "startAt": 2,
            "maxResults": 2,
            "total": 3,
            "issues": [{"key": "P-3"}]
        }
    ]

    results = gateway.search("project = PROJ", fields=["key"])
    assert len(results) == 3
    assert [r["key"] for r in results] == ["P-1", "P-2", "P-3"]
    assert mock_client.jql.call_count == 2
