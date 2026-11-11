from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastmcp import FastMCP

from mcp_atlassian.eggs.actions import Actions
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.identity import Identity
from mcp_atlassian.eggs.tools import register_eggs_tools
from tests.eggs.fakes import FakeGateway

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

BA = Identity(
    username="ba.rfid",
    display_name="BA RFID",
    roles={"RFID": {"lead_ba"}},
    is_lead=False,
)


@pytest.fixture
def base_cfg():
    return EggsConfig(
        jira_url="https://jira.eggstech.io.vn",
        lead_user="cuong.huynh",
        timezone="Asia/Ho_Chi_Minh",
        statuses={
            "open": "Open",
            "analyzing": "Analyzed",
            "ready_for_dev": "Ready For Dev",
            "todo": "DEV To Do",
            "in_progress": "Dev In Progress",
            "done": "Done",
            "cancelled": "Cancelled",
        },
        issue_types={
            "epic": ["Epic"],
            "story": ["Story"],
            "dev_task": ["Dev Task"],
            "bug": ["Bug stand-alone"],
            "bug_sub": ["Bug"],
            "adhoc": ["Ad-hoc Task"],
        },
        fields={
            "qc_owner": "customfield_qc_owner",
            "part": "customfield_part",
            "epic_link": "customfield_epic_link",
        },
        transitions={
            "analysis_done": "Analysis Done",
            "reopen_analysis": "Reopen Analysis",
            "ready_for_dev": "Ready For Dev",
            "close_epic": "Close Epic",
        },
        roles={"lead_ba": ["Lead-BA"], "dev": ["Developers"], "qc": ["QC"]},
        projects={
            "RFID": ProjectConfig(
                key="RFID",
                default_lead_ba="ba.rfid",
                triage_owner="dev1",
            ),
        },
    )


@pytest.fixture
def cal(base_cfg):
    return WorkCalendar(base_cfg.holidays)


def _make_story(
    key="RFID-1",
    status="Open",
    summary="Story 1",
    components=("POS - Web",),
    qc_owner="qc.user",
    description="h2. Acceptance Criteria\n- AC1",
    due_date="2026-11-20",
    subtasks=None,
) -> dict:
    comps = [{"name": c} for c in components]
    fields = {
        "summary": summary,
        "project": {"key": key.split("-")[0], "name": key.split("-")[0]},
        "issuetype": {"name": "Story"},
        "status": {"name": status},
        "components": comps,
        "customfield_qc_owner": {"name": qc_owner} if qc_owner else None,
        "description": description,
        "duedate": due_date,
        "subtasks": subtasks or [],
    }
    return {"key": key, "fields": fields}


def _make_dev_task(
    key="RFID-2",
    status="DEV To Do",
    summary="Dev Task 1",
    part="frontend",
    estimate_seconds=3600,
    parent_key="RFID-1",
) -> dict:
    fields = {
        "summary": summary,
        "project": {"key": key.split("-")[0], "name": key.split("-")[0]},
        "issuetype": {"name": "Dev Task", "subtask": True},
        "status": {"name": status},
        "customfield_part": {"value": part} if part else None,
        "timeoriginalestimate": estimate_seconds,
        "parent": {"key": parent_key},
    }
    return {"key": key, "fields": fields}


def _make_epic(
    key="RFID-100",
    status="Dev In Progress",
    summary="Epic 100",
) -> dict:
    fields = {
        "summary": summary,
        "project": {"key": key.split("-")[0], "name": key.split("-")[0]},
        "issuetype": {"name": "Epic"},
        "status": {"name": status},
    }
    return {"key": key, "fields": fields}


def test_unknown_step_refused(base_cfg, cal):
    gw = FakeGateway(
        issues={"RFID-1": _make_story(status="Open")},
        default_transitions=["Analysis Done"],
    )
    actions = Actions(gw, base_cfg, cal)
    res = actions.story_step(key="RFID-1", step="invalid_step", identity=BA)
    assert res.ok is False
    assert "Chỉ chấp nhận một trong 4 bước" in res.message
    assert "analysis_done" in res.message
    assert "reopen_analysis" in res.message
    assert "ready_for_dev" in res.message
    assert "close_epic" in res.message


def test_authorization_dev_and_qc_refused_lead_allowed(base_cfg, cal):
    gw = FakeGateway(
        issues={"RFID-1": _make_story(status="Open")},
        default_transitions=["Analysis Done"],
    )
    actions = Actions(gw, base_cfg, cal)

    dev_ident = Identity(
        username="dev1",
        display_name="Dev 1",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    qc_ident = Identity(
        username="qc1",
        display_name="QC 1",
        roles={"RFID": {"qc"}},
        is_lead=False,
    )
    lead_ident = Identity(
        username="lead1",
        display_name="Lead 1",
        roles={},
        is_lead=True,
    )

    # Dev refused
    res_dev = actions.story_step(key="RFID-1", step="analysis_done", identity=dev_ident)
    assert res_dev.ok is False
    assert res_dev.message == "Bước này do BA hoặc Lead làm."

    # QC refused
    res_qc = actions.story_step(key="RFID-1", step="analysis_done", identity=qc_ident)
    assert res_qc.ok is False
    assert res_qc.message == "Bước này do BA hoặc Lead làm."

    # Lead without lead_ba allowed
    res_lead = actions.story_step(
        key="RFID-1", step="analysis_done", identity=lead_ident
    )
    assert res_lead.ok is True
    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1][1] == "Analysis Done"


def test_wrong_status_message_names_fitting_step(base_cfg, cal):
    # Story in Analyzed asked analysis_done -> "đã Analyzed; bước tiếp là ready_for_dev hoặc reopen_analysis"
    gw = FakeGateway(
        issues={"RFID-1": _make_story(status="Analyzed")},
        default_transitions=["Ready For Dev", "Reopen Analysis"],
    )
    actions = Actions(gw, base_cfg, cal)
    res = actions.story_step(key="RFID-1", step="analysis_done", identity=BA)
    assert res.ok is False
    assert "đã Analyzed; bước tiếp là ready_for_dev hoặc reopen_analysis" in res.message

    # Story in Open asked ready_for_dev -> bước tiếp là analysis_done
    gw_open = FakeGateway(
        issues={"RFID-2": _make_story(status="Open")},
        default_transitions=["Analysis Done"],
    )
    actions_open = Actions(gw_open, base_cfg, cal)
    res_rfd = actions_open.story_step(key="RFID-2", step="ready_for_dev", identity=BA)
    assert res_rfd.ok is False
    assert "bước tiếp là analysis_done" in res_rfd.message

    # Story in Open asked reopen_analysis -> bước tiếp là analysis_done
    res_reopen = actions_open.story_step(
        key="RFID-2", step="reopen_analysis", identity=BA
    )
    assert res_reopen.ok is False
    assert "bước tiếp là analysis_done" in res_reopen.message

    # Epic in Done asked close_epic -> only in_progress allowed
    gw_epic = FakeGateway(
        issues={"RFID-100": _make_epic(status="Done")},
    )
    actions_epic = Actions(gw_epic, base_cfg, cal)
    res_close = actions_epic.story_step(key="RFID-100", step="close_epic", identity=BA)
    assert res_close.ok is False
    assert "chỉ áp dụng khi Epic ở trạng thái Dev In Progress" in res_close.message


def test_analysis_done_gaps_all_listed(base_cfg, cal):
    # Story with 0 Components, no QC Owner, empty description -> one message listing all gaps, no transition
    story_raw = _make_story(
        key="RFID-1",
        status="Open",
        components=(),
        qc_owner=None,
        description="",
    )
    gw = FakeGateway(
        issues={"RFID-1": story_raw},
        default_transitions=["Analysis Done"],
    )
    actions = Actions(gw, base_cfg, cal)
    res = actions.story_step(key="RFID-1", step="analysis_done", identity=BA)

    assert res.ok is False
    assert "component" in res.missing
    assert "qc_owner" in res.missing
    assert "description" in res.missing
    assert "Component" in res.message
    assert "QC Owner" in res.message
    assert "description" in res.message or "Mô tả" in res.message

    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 0


def test_analysis_done_with_two_components_refused(base_cfg, cal):
    story_raw = _make_story(
        key="RFID-1",
        status="Open",
        components=("POS - Web", "KT - App"),
        qc_owner="qc.user",
        description="h2. Acceptance Criteria\n- AC",
    )
    gw = FakeGateway(
        issues={"RFID-1": story_raw},
        default_transitions=["Analysis Done"],
    )
    actions = Actions(gw, base_cfg, cal)
    res = actions.story_step(key="RFID-1", step="analysis_done", identity=BA)

    assert res.ok is False
    assert "component" in res.missing
    assert "2 Components" in res.message
    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 0


def test_analysis_done_without_ac_heading_warns_and_proceeds(base_cfg, cal):
    story_raw = _make_story(
        key="RFID-1",
        status="Open",
        components=("POS - Web",),
        qc_owner="qc.user",
        description="Just description without any AC heading",
    )
    gw = FakeGateway(
        issues={"RFID-1": story_raw},
        default_transitions=["Analysis Done"],
    )
    actions = Actions(gw, base_cfg, cal)
    res = actions.story_step(key="RFID-1", step="analysis_done", identity=BA)

    assert res.ok is True
    assert "Open → Analyzed (Analysis Done)" in res.changes
    assert "Mô tả chưa có mục Acceptance Criteria" in res.changes
    assert res.next_actions == ["ready_for_dev"]

    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1][1] == "Analysis Done"


def test_reopen_analysis_requires_comment_and_passes_it(base_cfg, cal):
    story_raw = _make_story(
        key="RFID-1",
        status="Analyzed",
    )
    gw = FakeGateway(
        issues={"RFID-1": story_raw},
        default_transitions=["Reopen Analysis"],
    )
    actions = Actions(gw, base_cfg, cal)

    # Without comment
    res_no_com = actions.story_step(
        key="RFID-1", step="reopen_analysis", comment=None, identity=BA
    )
    assert res_no_com.ok is False
    assert "comment" in res_no_com.missing
    assert "Comment" in res_no_com.message
    assert len([c for c in gw.calls if c[0] == "transition"]) == 0

    # With comment
    reason = "Thiếu phân tích chi tiết API đối tác"
    res_with_com = actions.story_step(
        key="RFID-1", step="reopen_analysis", comment=reason, identity=BA
    )
    assert res_with_com.ok is True
    assert "Analyzed → Open (Reopen Analysis)" in res_with_com.changes

    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1][1] == "Reopen Analysis"
    assert trans_calls[0][2]["comment"] == reason


def test_ready_for_dev_checks_and_happy_path(base_cfg, cal):
    # Case 1: No Dev Task, no Due Date
    story_no_dt = _make_story(
        key="RFID-1",
        status="Analyzed",
        due_date=None,
        subtasks=[],
    )
    gw = FakeGateway(
        issues={"RFID-1": story_no_dt},
        default_transitions=["Ready For Dev"],
    )
    actions = Actions(gw, base_cfg, cal)
    res = actions.story_step(key="RFID-1", step="ready_for_dev", identity=BA)
    assert res.ok is False
    assert "due_date" in res.missing
    assert "dev_task" in res.missing

    # Case 2: Dev Task lacking Part, another lacking estimate, one Cancelled ignored
    dt_lacking_part = _make_dev_task(
        key="RFID-2",
        status="DEV To Do",
        part=None,
        estimate_seconds=3600,
    )
    dt_lacking_est = _make_dev_task(
        key="RFID-3",
        status="DEV To Do",
        part="backend",
        estimate_seconds=None,
    )
    dt_cancelled = _make_dev_task(
        key="RFID-4",
        status="Cancelled",
        part=None,
        estimate_seconds=None,
    )
    story_with_tasks = _make_story(
        key="RFID-1",
        status="Analyzed",
        due_date="2026-11-25",
        subtasks=[
            {"key": "RFID-2"},
            {"key": "RFID-3"},
            {"key": "RFID-4"},
        ],
    )
    gw2 = FakeGateway(
        issues={
            "RFID-1": story_with_tasks,
            "RFID-2": dt_lacking_part,
            "RFID-3": dt_lacking_est,
            "RFID-4": dt_cancelled,
        },
        default_transitions=["Ready For Dev"],
    )
    actions2 = Actions(gw2, base_cfg, cal)
    res2 = actions2.story_step(key="RFID-1", step="ready_for_dev", identity=BA)
    assert res2.ok is False
    assert "RFID-2" in res2.missing
    assert "RFID-3" in res2.missing
    # RFID-4 is Cancelled so it should be ignored and not offending
    assert "RFID-4" not in res2.missing
    assert "Dev Task RFID-2 thiếu Part" in res2.message
    assert "Dev Task RFID-3 thiếu Original Estimate" in res2.message

    # Case 3: Only Cancelled Dev Task -> counts as no Dev Task
    story_only_cancelled = _make_story(
        key="RFID-1",
        status="Analyzed",
        due_date="2026-11-25",
        subtasks=[{"key": "RFID-4"}],
    )
    gw3 = FakeGateway(
        issues={
            "RFID-1": story_only_cancelled,
            "RFID-4": dt_cancelled,
        },
        default_transitions=["Ready For Dev"],
    )
    actions3 = Actions(gw3, base_cfg, cal)
    res3 = actions3.story_step(key="RFID-1", step="ready_for_dev", identity=BA)
    assert res3.ok is False
    assert "dev_task" in res3.missing

    # Case 4: Happy path
    dt_valid = _make_dev_task(
        key="RFID-2",
        status="DEV To Do",
        part="frontend",
        estimate_seconds=7200,
    )
    gw_happy = FakeGateway(
        issues={
            "RFID-1": _make_story(
                key="RFID-1",
                status="Analyzed",
                due_date="2026-11-25",
                subtasks=[{"key": "RFID-2"}],
            ),
            "RFID-2": dt_valid,
        },
        default_transitions=["Ready For Dev"],
    )
    actions_happy = Actions(gw_happy, base_cfg, cal)
    res_happy = actions_happy.story_step(
        key="RFID-1", step="ready_for_dev", identity=BA
    )
    assert res_happy.ok is True
    assert "Analyzed → Ready For Dev (Ready For Dev)" in res_happy.changes
    assert res_happy.next_actions == []
    trans_calls = [c for c in gw_happy.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1][1] == "Ready For Dev"


def test_close_epic_open_issues_listed_cap_10_and_happy_path(base_cfg, cal):
    # 12 issues in Epic: 12 open (in_progress or open)
    epic = _make_epic(key="RFID-100", status="Dev In Progress")
    child_issues = {}
    for i in range(1, 13):
        k = f"RFID-{i}"
        child_issues[k] = {
            "key": k,
            "fields": {
                "summary": f"Child {i}",
                "status": {"name": "Dev In Progress"},
                "customfield_epic_link": "RFID-100",
            },
        }

    gw = FakeGateway(
        issues={"RFID-100": epic, **child_issues},
        default_transitions=["Close Epic"],
    )
    actions = Actions(gw, base_cfg, cal)
    res = actions.story_step(key="RFID-100", step="close_epic", identity=BA)

    assert res.ok is False
    assert "RFID-1 (Dev In Progress)" in res.message
    assert "RFID-10 (Dev In Progress)" in res.message
    assert "và 2 issue khác" in res.message
    assert len([c for c in gw.calls if c[0] == "transition"]) == 0

    # Happy path: all issues in Epic are Done or Cancelled
    for i in range(1, 13):
        k = f"RFID-{i}"
        st = "Done" if i % 2 == 0 else "Cancelled"
        child_issues[k]["fields"]["status"]["name"] = st

    gw_happy = FakeGateway(
        issues={"RFID-100": epic, **child_issues},
        default_transitions=["Close Epic"],
    )
    actions_happy = Actions(gw_happy, base_cfg, cal)
    res_happy = actions_happy.story_step(key="RFID-100", step="close_epic", identity=BA)
    assert res_happy.ok is True
    assert "Dev In Progress → Done (Close Epic)" in res_happy.changes
    assert res_happy.next_actions == []

    trans_calls = [c for c in gw_happy.calls if c[0] == "transition"]
    assert len(trans_calls) == 1
    assert trans_calls[0][1][1] == "Close Epic"


def test_dry_run_makes_no_write(base_cfg, cal):
    story = _make_story(
        key="RFID-1",
        status="Open",
        components=("POS - Web",),
        qc_owner="qc.user",
        description="h2. Acceptance Criteria\n- AC",
    )
    gw = FakeGateway(
        issues={"RFID-1": story},
        default_transitions=["Analysis Done"],
    )
    actions = Actions(gw, base_cfg, cal)
    res = actions.story_step(
        key="RFID-1", step="analysis_done", dry_run=True, identity=BA
    )

    assert res.ok is True
    assert "[DRY-RUN]" in res.message
    assert "Open → Analyzed (Analysis Done)" in res.changes
    assert res.next_actions == ["ready_for_dev"]

    # Verify no transitions recorded on gateway
    trans_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(trans_calls) == 0


@pytest.mark.asyncio
async def test_eggs_story_step_fastmcp_tool_execution(base_cfg):
    server = FastMCP("test_jira")
    register_eggs_tools(server, force=True)

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert "eggs_story_step" in tool_names

    user_gw = FakeGateway(
        user="ba.rfid",
        issues={"RFID-1": _make_story(key="RFID-1", status="Open")},
        default_transitions=["Analysis Done"],
    )
    bot_gw = FakeGateway(
        user="eggsbot",
        role_members={"RFID": {"Lead-BA": ["ba.rfid"]}},
    )

    with (
        patch("mcp_atlassian.eggs.tools.load_config", return_value=base_cfg),
        patch(
            "mcp_atlassian.eggs.tools.get_user_gateway",
            AsyncMock(return_value=user_gw),
        ),
        patch(
            "mcp_atlassian.eggs.tools.get_roles_reader",
            return_value=bot_gw,
        ),
    ):
        res = await server.call_tool(
            "eggs_story_step",
            {"key": "RFID-1", "step": "analysis_done", "dry_run": True},
        )
        data = json.loads(res.content[0].text)
        assert data["ok"] is True
        assert "[DRY-RUN]" in data["message"]
        assert "Open → Analyzed (Analysis Done)" in data["changes"]
        assert data["next_actions"] == ["ready_for_dev"]


def test_missing_identity_is_refused(base_cfg, cal):
    gw = FakeGateway(
        issues={"RFID-1": _make_story()}, default_transitions=["Analysis Done"]
    )
    res = Actions(gw, base_cfg, cal).story_step(key="RFID-1", step="analysis_done")
    assert res.ok is False
    assert res.message == "Bước này do BA hoặc Lead làm."
    assert not [c for c in gw.calls if c[0] == "transition"]


def test_status_with_no_ba_step_says_so(base_cfg, cal):
    gw = FakeGateway(issues={"RFID-1": _make_story(status="Done")})
    res = Actions(gw, base_cfg, cal).story_step(
        key="RFID-1", step="analysis_done", identity=BA
    )
    assert res.ok is False
    assert res.message.endswith("không có bước BA nào áp dụng ở trạng thái này.")
    assert "bước tiếp là" not in res.message
