"""Dev tự tạo Dev Task dưới Story / Ad-hoc Task / Bug stand-alone."""

import pytest

from mcp_atlassian.eggs.actions import Actions
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import ProjectConfig
from tests.eggs.fakes import FakeGateway
from tests.eggs.test_actions import cfg  # noqa: F401 - fixture

PART_FIELD = "customfield_10701"


def _parent(key, type_name, status):
    return {
        "key": key,
        "fields": {
            "summary": f"{type_name} cha",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": type_name, "subtask": False},
            "status": {"name": status},
        },
    }


def _gw(*issues, existing=None):
    gw = FakeGateway(
        issues={i["key"]: i for i in issues},
        allowed_field_values={("EGGSBOX", "Dev Task", PART_FIELD): ["BE", "FE APP"]},
    )
    if existing:
        gw.issues.update({i["key"]: i for i in existing})
    return gw


def _actions(gw, cfg):  # noqa: F811
    cfg.projects["EGGSBOX"] = ProjectConfig(
        key="EGGSBOX", default_lead_ba="cuong.huynh", triage_owner="tuan.bui"
    )
    cfg.fields.setdefault("part", PART_FIELD)
    cfg.statuses["analyzing"] = "Analyzed"  # tên status thật trên Jira
    return Actions(gw, cfg, WorkCalendar([]))


def _create(actions, parent="EGGSBOX-1", **kw):
    args = {
        "project": "EGGSBOX",
        "issue_type": "dev_task",
        "summary": "API duyệt ticket",
        "parent": parent,
        "part": "be",
        "estimate": "2h",
        "creator": "henry",
        "creator_roles": {"dev"},
    }
    args.update(kw)
    return actions.create_issue(**args)


def _created(gw):
    return [c[1][0] for c in gw.calls if c[0] == "create_issue"]


@pytest.mark.parametrize(
    "status", ["Open", "Analyzed", "Ready For Dev", "Dev In Progress"]
)
def test_dev_creates_dev_task_under_story(cfg, status):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", "Story", status))
    res = _create(_actions(gw, cfg))
    assert res.ok, res.message
    (fields,) = _created(gw)
    assert fields["parent"] == {"key": "EGGSBOX-1"}
    assert fields["issuetype"] == {"name": "Dev Task"}
    assert fields["summary"] == "[BE] API duyệt ticket"
    assert fields[PART_FIELD] == {"value": "BE"}
    assert fields["timetracking"] == {"originalEstimate": "2h"}
    assert fields["assignee"] == {"name": "henry"}
    assert "epic" not in str(fields).lower()
    if status in ("Open", "Analyzed"):
        assert "Ready For Dev" in res.message
    assert res.next_actions == ["schedule"]


@pytest.mark.parametrize("status", ["QC TO DO", "Test In Progress", "Ready For UAT"])
def test_story_in_qc_needs_confirmation(cfg, status):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", "Story", status))
    actions = _actions(gw, cfg)
    res = _create(actions)
    assert not res.ok
    assert res.missing == ["confirm_reopen"]
    assert "bug_sub" in res.message
    assert not _created(gw)
    res = _create(actions, confirm_reopen=True)
    assert res.ok
    assert "Dev In Progress" in res.message
    assert len(_created(gw)) == 1


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        ("Wait For Review", "Story mới"),
        ("Done", "relates"),
        ("Cancelled", "relates"),
    ],
)
def test_story_closed_or_in_uat_refused(cfg, status, hint):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", "Story", status))
    res = _create(_actions(gw, cfg), confirm_reopen=True)
    assert not res.ok
    assert hint in res.message
    assert not _created(gw)


@pytest.mark.parametrize("type_name", ["Ad-hoc Task", "Bug stand-alone"])
@pytest.mark.parametrize(
    ("status", "ok"),
    [
        ("DEV To Do", True),
        ("Dev In Progress", True),
        ("QC TO DO", False),
        ("Test In Progress", False),
        ("Done", False),
        ("Cancelled", False),
    ],
)
def test_adhoc_and_bug_parent_rules(cfg, type_name, status, ok):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", type_name, status))
    res = _create(_actions(gw, cfg), confirm_reopen=True)
    assert res.ok is ok, res.message
    assert len(_created(gw)) == (1 if ok else 0)
    if status == "QC TO DO":
        assert "QC Fail" in res.message
        assert ("bug_sub" in res.message) is (type_name == "Ad-hoc Task")


def test_bad_parent_and_missing_fields(cfg):  # noqa: F811
    gw = _gw(
        _parent("EGGSBOX-1", "Story", "Dev In Progress"),
        _parent("EGGSBOX-2", "Epic", "Open"),
    )
    actions = _actions(gw, cfg)
    assert _create(actions, parent=None).missing == ["parent"]
    assert not _create(actions, parent="EGGSBOX-2").ok
    assert not _create(actions, parent="EGGSBOX-404").ok
    res = _create(actions, part=None)
    assert res.missing == ["part"]
    assert "BE, FE APP" in res.message
    assert _create(actions, part="Mobile").missing == ["part"]
    assert _create(actions, estimate=None).missing == ["estimate"]
    assert _create(actions, estimate="hai giờ").missing == ["estimate"]
    assert not _created(gw)


def test_only_dev_or_ba_can_create(cfg):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", "Story", "Dev In Progress"))
    actions = _actions(gw, cfg)
    res = _create(actions, creator="tuan.bui", creator_roles={"qc"})
    assert not res.ok
    assert "quyền" in res.message
    res = _create(
        actions, creator="quang.nguyen", creator_roles={"lead_ba"}, assignee="henry"
    )
    assert res.ok
    assert _created(gw)[0]["assignee"] == {"name": "henry"}


def test_dry_run_writes_nothing(cfg):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", "Story", "Analyzed"))
    res = _create(_actions(gw, cfg), dry_run=True)
    assert res.ok
    assert res.message.startswith("[Dry-run]")
    assert "Part: BE" in res.changes
    assert not _created(gw)


def test_duplicate_open_dev_task_is_a_warning(cfg):  # noqa: F811
    existing = {
        "key": "EGGSBOX-5",
        "fields": {
            "summary": "[BE] cũ",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {"key": "EGGSBOX-1"},
            "assignee": {"name": "henry"},
            PART_FIELD: {"value": "BE"},
        },
    }
    gw = _gw(_parent("EGGSBOX-1", "Story", "Dev In Progress"), existing=[existing])
    res = _create(_actions(gw, cfg))
    assert res.ok
    assert "EGGSBOX-5" in res.message
    assert len(_created(gw)) == 1


def test_retry_returns_same_key(cfg):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", "Story", "Dev In Progress"))
    actions = _actions(gw, cfg)
    first = _create(actions)
    second = _create(actions)
    assert first.ok and second.ok
    assert len(_created(gw)) == 1


def test_messages_show_jira_status_names(cfg):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", "Story", "QC TO DO"))
    res = _create(_actions(gw, cfg))
    assert "QC TO DO" in res.message
    assert "ready_for_qc" not in res.message


def test_same_summary_for_another_dev_is_created(cfg):  # noqa: F811
    gw = _gw(_parent("EGGSBOX-1", "Story", "Dev In Progress"))
    actions = _actions(gw, cfg)
    assert _create(actions).ok
    assert _create(actions, assignee="viet.le").ok
    assert len(_created(gw)) == 2


def test_duplicate_warning_ignores_part_case_without_options(cfg):  # noqa: F811
    existing = {
        "key": "EGGSBOX-5",
        "fields": {
            "summary": "[BE] cũ",
            "project": {"key": "EGGSBOX"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {"key": "EGGSBOX-1"},
            "assignee": {"name": "henry"},
            PART_FIELD: {"value": "BE"},
        },
    }
    gw = _gw(_parent("EGGSBOX-1", "Story", "Dev In Progress"), existing=[existing])
    gw.allowed_field_values = {}
    res = _create(_actions(gw, cfg), part="be")
    assert res.ok
    assert "EGGSBOX-5" in res.message
