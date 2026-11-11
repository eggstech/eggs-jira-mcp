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


def test_update_fields_with_notify_users_false(mock_client):
    gateway = AtlassianGateway(mock_client)
    gateway.update_fields(
        "PROJ-123", {"customfield_10108": "2026-11-20"}, notify_users=False
    )

    mock_client.put.assert_called_once_with(
        "rest/api/2/issue/PROJ-123",
        data={"fields": {"customfield_10108": "2026-11-20"}},
        params={"notifyUsers": "false"},
    )


def test_update_fields_http_error_wrapped_into_jira_error(mock_client):
    from requests.exceptions import HTTPError

    gateway = AtlassianGateway(mock_client)
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = {
        "errorMessages": ["Field 'part' cannot be set"],
        "errors": {"planned_start": "Date is required"},
    }
    mock_client.put.side_effect = HTTPError("400 Client Error", response=resp)

    with pytest.raises(JiraError) as exc_info:
        gateway.update_fields("PROJ-123", {"part": "INVALID"})

    err = exc_info.value
    assert err.status_code == 400
    assert err.error_messages == ["Field 'part' cannot be set"]
    assert err.errors == {"planned_start": "Date is required"}
    assert "Field 'part' cannot be set" in str(err)
    assert "Date is required" in str(err)


def test_get_subtasks(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "fields": {"subtasks": [{"id": "1", "key": "PROJ-124"}]}
    }
    subtasks = gateway.get_subtasks("PROJ-123")
    assert subtasks == [{"id": "1", "key": "PROJ-124"}]
    mock_client.get.assert_called_once_with("rest/api/2/issue/PROJ-123")


def test_get_worklogs(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "total": 1,
        "worklogs": [{"id": "w1", "author": {"name": "alice"}}],
    }
    worklogs = gateway.get_worklogs("PROJ-123")
    assert worklogs == [{"id": "w1", "author": {"name": "alice"}}]
    mock_client.get.assert_called_once_with("rest/api/2/issue/PROJ-123/worklog")


def test_get_comments(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "total": 1,
        "comments": [{"id": "c1", "body": "hello"}],
    }
    comments = gateway.get_comments("PROJ-123")
    assert comments == [{"id": "c1", "body": "hello"}]
    mock_client.get.assert_called_once_with("rest/api/2/issue/PROJ-123/comment")


def test_get_changelog(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "changelog": {
            "total": 1,
            "histories": [{"id": "h1", "items": [{"field": "status"}]}],
        }
    }
    changelog = gateway.get_changelog("PROJ-123")
    assert changelog == [{"id": "h1", "items": [{"field": "status"}]}]
    mock_client.get.assert_called_once_with(
        "rest/api/2/issue/PROJ-123?expand=changelog"
    )


def test_get_changelog_warns_when_total_greater_than_returned(mock_client, caplog):
    import logging

    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "changelog": {
            "total": 45,
            "histories": [{"id": "h1"}, {"id": "h2"}],
        }
    }
    with caplog.at_level(logging.WARNING):
        res = gateway.get_changelog("PROJ-123")

    assert len(res) == 2
    assert any(
        "PROJ-123" in r.message and "45" in r.message and "2" in r.message
        for r in caplog.records
    )


def test_update_fields_default_notify_users(mock_client):
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

    gateway.transition(
        "PROJ-123", "QC Pass", fields={"customfield_1": "val"}, comment="passed"
    )

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
    mock_client.get.return_value = {"transitions": [{"id": "11", "name": "Start"}]}

    with pytest.raises(
        JiraError, match="Transition 'Invalid' not available for PROJ-123"
    ):
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
        ],
    }

    since = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
    worklogs = gateway.worklogs_since(since)

    since_ms = int(since.timestamp() * 1000)
    mock_client.get.assert_called_once_with(
        f"rest/api/2/worklog/updated?since={since_ms}"
    )
    mock_client.post.assert_called_once_with(
        "rest/api/2/worklog/list", data={"ids": [101, 102]}
    )
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
            "issues": [{"key": "P-1"}, {"key": "P-2"}],
        },
        {"startAt": 2, "maxResults": 2, "total": 3, "issues": [{"key": "P-3"}]},
    ]

    results = gateway.search("project = PROJ", fields=["key"])
    assert len(results) == 3
    assert [r["key"] for r in results] == ["P-1", "P-2", "P-3"]
    assert mock_client.jql.call_count == 2
    assert mock_client.jql.call_args_list[0].kwargs["start"] == 0
    assert mock_client.jql.call_args_list[1].kwargs["start"] == 2


def test_search_expand_changelog_passes_changelog(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.jql.return_value = {
        "startAt": 0,
        "maxResults": 50,
        "total": 1,
        "issues": [{"key": "P-1"}],
    }

    gateway.search("project = PROJ", fields=["key"], expand_changelog=True)
    mock_client.jql.assert_called_once_with(
        "project = PROJ",
        start=0,
        limit=50,
        fields=["key"],
        expand="changelog",
    )


def test_worklogs_since_requires_aware_datetime(mock_client):
    gateway = AtlassianGateway(mock_client)
    naive_dt = datetime(2026, 11, 10, 8, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="aware"):
        gateway.worklogs_since(naive_dt)


def test_add_worklog_requires_aware_datetime(mock_client):
    gateway = AtlassianGateway(mock_client)
    naive_dt = datetime(2026, 11, 10, 8, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="aware"):
        gateway.add_worklog("PROJ-1", "1h", "did work", started=naive_dt)


def test_worklogs_since_pagination_and_chunking(mock_client):
    gateway = AtlassianGateway(mock_client)

    # First page: lastPage=False, 1000 ids (1..1000)
    page1_values = [{"worklogId": i} for i in range(1, 1001)]
    # Second page: lastPage=True, 500 ids (1001..1500)
    page2_values = [{"worklogId": i} for i in range(1001, 1501)]

    mock_client.get.side_effect = [
        {
            "values": page1_values,
            "lastPage": False,
            "nextPage": "rest/api/2/worklog/updated?since=1700000010000",
        },
        {"values": page2_values, "lastPage": True},
    ]

    # Two list calls: chunk 1 (1000 items), chunk 2 (500 items)
    chunk1_worklogs = [
        {
            "id": str(i),
            "issueKey": "PROJ-1",
            "author": {"name": "alice"},
            "started": "2026-11-10T08:00:00.000+0700",
            "timeSpentSeconds": 3600,
        }
        for i in range(1, 1001)
    ]
    chunk2_worklogs = [
        {
            "id": str(i),
            "issueKey": "PROJ-2",
            "author": {"name": "bob"},
            "started": "2026-11-10T09:00:00.000+0700",
            "timeSpentSeconds": 1800,
        }
        for i in range(1001, 1501)
    ]

    mock_client.post.side_effect = [
        chunk1_worklogs,
        chunk2_worklogs,
    ]

    since = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
    results = gateway.worklogs_since(since)

    # Assert 2 get calls (pagination)
    assert mock_client.get.call_count == 2
    # Assert 2 post calls (chunking 1500 ids into 1000 + 500)
    assert mock_client.post.call_count == 2
    assert len(mock_client.post.call_args_list[0].kwargs["data"]["ids"]) == 1000
    assert len(mock_client.post.call_args_list[1].kwargs["data"]["ids"]) == 500
    # Both pages' worklogs returned
    assert len(results) == 1500


def test_worklogs_since_skips_unresolvable_issue_id(mock_client, caplog):
    import logging

    gateway = AtlassianGateway(mock_client)
    mock_client.get.return_value = {
        "values": [{"worklogId": 999}],
        "lastPage": True,
    }
    mock_client.post.return_value = [
        {
            "id": "999",
            "issueId": "99999",
            "author": {"name": "dev"},
            "started": "2026-11-10T08:00:00.000+0700",
            "timeSpentSeconds": 3600,
        }
    ]
    # JQL search fails to find issueId 99999
    mock_client.jql.return_value = {"issues": [], "total": 0}

    since = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING):
        results = gateway.worklogs_since(since)

    # Worklog must be skipped and never use numeric ID as key
    assert len(results) == 0
    assert any("could not be resolved" in record.message for record in caplog.records)


def test_project_role_members_pagination_and_skips_inactive(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.side_effect = [
        # 1. Project roles list
        {
            "Developers": "http://jira/rest/api/2/project/PROJ/role/10001",
            "QA": "http://jira/rest/api/2/project/PROJ/role/10002",
        },
        # 2. Role 10001 details
        {
            "actors": [
                {"type": "atlassian-user-role-actor", "name": "alice", "active": True},
                {
                    "type": "atlassian-user-role-actor",
                    "name": "inactive_dev",
                    "active": False,
                },
                {"type": "atlassian-group-role-actor", "name": "dev-group"},
            ]
        },
        # 3. Group page 1
        {
            "isLast": False,
            "values": [
                {"name": "bob", "active": True},
                {"name": "inactive_grp_user", "active": False},
            ],
        },
        # 4. Group page 2
        {
            "isLast": True,
            "values": [
                {"name": "charlie", "active": True},
            ],
        },
    ]

    res = gateway.project_role_members("PROJ", role_names=["Developers"])
    assert res == {"Developers": ["alice", "bob", "charlie"]}

    # Verify group/member calls with params
    group_calls = [
        call
        for call in mock_client.get.call_args_list
        if "rest/api/2/group/member" in call.args[0]
    ]
    assert len(group_calls) == 2
    assert group_calls[0].kwargs.get("params") == {
        "groupname": "dev-group",
        "startAt": 0,
        "maxResults": 50,
    }
    assert group_calls[1].kwargs.get("params") == {
        "groupname": "dev-group",
        "startAt": 50,
        "maxResults": 50,
    }


def test_project_role_members_group_name_with_ampersand(mock_client):
    gateway = AtlassianGateway(mock_client)
    group_name = "devs & testers"
    mock_client.get.side_effect = [
        {"Developers": "rest/api/2/project/PROJ/role/10001"},
        {"actors": [{"type": "atlassian-group-role-actor", "name": group_name}]},
        {"isLast": True, "values": [{"name": "alice", "active": True}]},
    ]

    res = gateway.project_role_members("PROJ", role_names=["Developers"])
    assert res == {"Developers": ["alice"]}
    group_calls = [
        call
        for call in mock_client.get.call_args_list
        if "rest/api/2/group/member" in call.args[0]
    ]
    assert len(group_calls) == 1
    assert group_calls[0].kwargs.get("params") == {
        "groupname": group_name,
        "startAt": 0,
        "maxResults": 50,
    }


def test_project_role_members_group_expansion_failure_raises(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.side_effect = [
        {"Developers": "rest/api/2/project/PROJ/role/10001"},
        {"actors": [{"type": "atlassian-group-role-actor", "name": "failing-group"}]},
        Exception("Failed to reach group API"),
    ]

    with pytest.raises(Exception) as exc_info:
        gateway.project_role_members("PROJ", role_names=["Developers"])
    assert "Failed to reach group API" in str(exc_info.value) or "failing-group" in str(
        exc_info.value
    )


def test_project_role_members_project_list_failure_raises(mock_client):
    gateway = AtlassianGateway(mock_client)
    mock_client.get.side_effect = Exception("403 Forbidden")

    with pytest.raises(Exception, match="403 Forbidden"):
        gateway.project_role_members("PROJ", role_names=["Developers"])
