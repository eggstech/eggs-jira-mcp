from __future__ import annotations

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastmcp import FastMCP

from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig, Thresholds
from mcp_atlassian.eggs.identity import Identity
from mcp_atlassian.eggs.model import Issue, Worklog
from mcp_atlassian.eggs.report import Snapshot, compute_worklog_report
from mcp_atlassian.eggs.tools import register_eggs_tools
from tests.eggs.fakes import FakeGateway

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _make_issue(
    key="DEV-1",
    issue_type="dev_task",
    status="in_progress",
    summary="Task Summary",
    assignee="henry",
    parent_key=None,
    epic_key=None,
    components=(),
    due_date=None,
    planned_end=None,
    created=None,
    resolution=None,
    labels=(),
    blocked: bool = False,  # noqa: FBT001, FBT002
    blocked_by=None,
    reschedule_status=None,
    reschedule_requester=None,
    health_flags=frozenset(),
    escalation_level=0,
    escalation_owner=None,
    qc_owner=None,
    part=None,
    parent_type=None,
    estimate_seconds=None,
    frozen: bool = False,  # noqa: FBT001, FBT002
) -> Issue:
    project = key.split("-")[0]
    return Issue(
        key=key,
        project=project,
        type=issue_type,
        status=status,
        summary=summary,
        assignee=assignee,
        components=tuple(components),
        part=part,
        parent_type=parent_type,
        qc_owner=qc_owner,
        parent_key=parent_key,
        epic_key=epic_key,
        created=created,
        status_changed_at=created,
        planned_start=None,
        planned_end=planned_end,
        baseline_end=None,
        due_date=due_date,
        estimate_seconds=estimate_seconds,
        reschedule_status=reschedule_status,
        reschedule_requester=reschedule_requester,
        proposed_end=None,
        blocked=blocked,
        blocked_by=blocked_by,
        blocked_since=None,
        health_flags=health_flags,
        health_since=None,
        escalation_level=escalation_level,
        escalation_owner=escalation_owner,
        frozen=frozen,
        labels=tuple(labels),
        resolution=resolution,
    )


@pytest.fixture
def base_cfg():
    return EggsConfig(
        jira_url="https://jira.eggstech.io.vn",
        lead_user="cuong.huynh",
        timezone="Asia/Ho_Chi_Minh",
        holidays=frozenset(
            {date(2026, 11, 13)}
        ),  # Friday 13th is a holiday for testing
        projects={
            "RFID": ProjectConfig(
                key="RFID", default_lead_ba="ba.rfid", triage_owner="dev1"
            ),
            "HH": ProjectConfig(key="HH", default_lead_ba="ba.hh", triage_owner="dev2"),
        },
        roles={"lead_ba": ["Lead-BA"], "dev": ["Developers"], "qc": ["QC"]},
        thresholds=Thresholds(
            daily_hours_target=8.0,
            end_day_hour=17,
        ),
    )


@pytest.fixture
def cal(base_cfg):
    return WorkCalendar(base_cfg.holidays)


def test_default_range_monday_through_today(base_cfg, cal):
    # Suppose today is Wednesday 2026-11-11 10:00:00 (Asia/Ho_Chi_Minh)
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime(2026, 11, 11, 10, 0, tzinfo=tz)
    today = now.date()

    ident = Identity(
        username="henry",
        display_name="Henry",
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
        today=today,
        worklogs=[],
    )

    result = compute_worklog_report(snap, base_cfg, cal, ident, project="RFID", now=now)
    assert result["ok"] is True
    # Monday of this week (2026-11-09) to Wednesday (2026-11-11)
    assert result["range"] == {"from": "2026-11-09", "to": "2026-11-11"}


def test_holiday_and_weekend_handling(base_cfg, cal):
    # Range covers Thursday 2026-11-12 to Sunday 2026-11-15
    # Friday 2026-11-13 is a holiday. Saturday 14, Sunday 15 are weekends.
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime(2026, 11, 15, 18, 0, tzinfo=tz)
    d_from = date(2026, 11, 12)
    d_to = date(2026, 11, 15)

    # Henry logged 4h on Thursday (working day), 3h on Friday (holiday), 0h on Saturday, 2h on Sunday (weekend)
    iss = _make_issue(
        key="RFID-1",
        summary="Task 1",
        assignee="henry",
    )
    wls = [
        Worklog(
            issue_key="RFID-1",
            author="henry",
            started=datetime(2026, 11, 12, 9, 0, tzinfo=tz),
            seconds=4 * 3600,
        ),
        Worklog(
            issue_key="RFID-1",
            author="henry",
            started=datetime(2026, 11, 13, 9, 0, tzinfo=tz),
            seconds=3 * 3600,
        ),
        Worklog(
            issue_key="RFID-1",
            author="henry",
            started=datetime(2026, 11, 15, 10, 0, tzinfo=tz),
            seconds=2 * 3600,
        ),
    ]
    snap = Snapshot(
        issues={"RFID-1": iss},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=d_to,
        worklogs=wls,
    )
    ident = Identity(
        username="henry",
        display_name="Henry",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )

    result = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        date_from=d_from,
        date_to=d_to,
        now=now,
    )
    assert result["ok"] is True
    person = result["people"][0]
    days = person["days"]

    # Saturday 2026-11-14 had 0h so it MUST NOT be listed.
    day_dates = [d["date"] for d in days]
    assert "2026-11-14" not in day_dates
    # Thursday (working day) -> holiday=False
    assert any(
        d["date"] == "2026-11-12"
        and d["holiday"] is False
        and d["below_target"] is True
        for d in days
    )
    # Friday (holiday with 3h) -> holiday=True, below_target=False
    assert any(
        d["date"] == "2026-11-13"
        and d["holiday"] is True
        and d["below_target"] is False
        and d["hours"] == 3.0
        for d in days
    )
    # Sunday (weekend with 2h) -> holiday=True, below_target=False
    assert any(
        d["date"] == "2026-11-15"
        and d["holiday"] is True
        and d["below_target"] is False
        and d["hours"] == 2.0
        for d in days
    )


def test_timezone_edge(base_cfg, cal):
    # A worklog started 23:30 on the 11th, local time (Asia/Ho_Chi_Minh, +07:00)
    # In UTC, this is 16:30 on the 11th.
    # Group by Worklog.started converted to cfg.timezone.
    tz_vn = ZoneInfo("Asia/Ho_Chi_Minh")
    # started in UTC
    dt_utc = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)
    # Local time is 2026-11-11 23:30:00

    iss = _make_issue(
        key="RFID-1",
        summary="Task 1",
        assignee="henry",
    )
    wls = [
        Worklog(
            issue_key="RFID-1",
            author="henry",
            started=dt_utc,
            seconds=2 * 3600,
        ),
    ]
    snap = Snapshot(
        issues={"RFID-1": iss},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 11),
        worklogs=wls,
    )
    ident = Identity(
        username="henry",
        display_name="Henry",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )
    now = datetime(2026, 11, 11, 23, 45, tzinfo=tz_vn)

    result = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        date_from=date(2026, 11, 11),
        date_to=date(2026, 11, 11),
        now=now,
    )
    assert result["ok"] is True
    person = result["people"][0]
    # Counts on the 11th
    assert person["days"][0]["date"] == "2026-11-11"
    assert person["days"][0]["hours"] == 2.0


def test_dev_asking_for_someone_else_is_refused(base_cfg, cal):
    ident = Identity(
        username="dev_alice",
        display_name="Alice",
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
        today=date(2026, 11, 11),
        worklogs=[],
    )
    result = compute_worklog_report(
        snap, base_cfg, cal, ident, project="RFID", person="henry"
    )
    assert result["ok"] is False
    assert (
        result["message"]
        == "Bạn chỉ xem được giờ của mình; giờ của người khác do BA hoặc Lead xem."
    )


def test_dev_without_person_gets_only_their_own(base_cfg, cal):
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime(2026, 11, 11, 10, 0, tzinfo=tz)
    iss = _make_issue(
        key="RFID-1",
        summary="Task 1",
        assignee="alice",
    )
    wls = [
        Worklog(
            issue_key="RFID-1",
            author="alice",
            started=datetime(2026, 11, 11, 8, 0, tzinfo=tz),
            seconds=4 * 3600,
        ),
        Worklog(
            issue_key="RFID-1",
            author="bob",
            started=datetime(2026, 11, 11, 8, 0, tzinfo=tz),
            seconds=6 * 3600,
        ),
    ]
    snap = Snapshot(
        issues={"RFID-1": iss},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 11),
        worklogs=wls,
    )
    ident = Identity(
        username="alice",
        display_name="Alice",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )

    result = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        person=None,
        date_from=date(2026, 11, 11),
        date_to=date(2026, 11, 11),
        now=now,
    )
    assert result["ok"] is True
    assert len(result["people"]) == 1
    assert result["people"][0]["username"] == "alice"
    assert result["people"][0]["total_hours"] == 4.0


def test_lead_ba_sees_team_of_their_project_only(base_cfg, cal):
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime(2026, 11, 11, 10, 0, tzinfo=tz)
    iss_rfid = _make_issue(
        key="RFID-1",
        summary="RFID Task",
        assignee="alice",
    )
    iss_hh = _make_issue(
        key="HH-1",
        summary="HH Task",
        assignee="charlie",
    )
    wls = [
        Worklog(
            issue_key="RFID-1",
            author="alice",
            started=datetime(2026, 11, 11, 8, 0, tzinfo=tz),
            seconds=4 * 3600,
        ),
        Worklog(
            issue_key="RFID-1",
            author="bob",
            started=datetime(2026, 11, 11, 8, 0, tzinfo=tz),
            seconds=5 * 3600,
        ),
        Worklog(
            issue_key="HH-1",
            author="charlie",
            started=datetime(2026, 11, 11, 8, 0, tzinfo=tz),
            seconds=8 * 3600,
        ),
    ]
    snap = Snapshot(
        issues={"RFID-1": iss_rfid, "HH-1": iss_hh},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 11),
        worklogs=wls,
    )
    # BA has lead_ba in RFID only
    ident = Identity(
        username="ba_rfid",
        display_name="BA RFID",
        roles={"RFID": {"lead_ba"}},
        is_lead=False,
    )

    result = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project=None,  # omitted -> defaults to their lead_ba projects
        date_from=date(2026, 11, 11),
        date_to=date(2026, 11, 11),
        now=now,
    )
    assert result["ok"] is True
    usernames = [p["username"] for p in result["people"]]
    assert usernames == ["alice", "bob"]
    assert "charlie" not in usernames


def test_below_target_today_only_flagged_after_end_day_hour(base_cfg, cal):
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    # Today is Wednesday 2026-11-11
    today = date(2026, 11, 11)
    iss = _make_issue(
        key="RFID-1",
        summary="Task 1",
        assignee="henry",
    )
    # 4h logged today (target is 8h)
    wls = [
        Worklog(
            issue_key="RFID-1",
            author="henry",
            started=datetime(2026, 11, 11, 9, 0, tzinfo=tz),
            seconds=4 * 3600,
        ),
    ]
    snap = Snapshot(
        issues={"RFID-1": iss},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=today,
        worklogs=wls,
    )
    ident = Identity(
        username="henry",
        display_name="Henry",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )

    # 1. At 14:00 (before end_day_hour 17): below_target should be False
    now_before = datetime(2026, 11, 11, 14, 0, tzinfo=tz)
    res_before = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        date_from=today,
        date_to=today,
        now=now_before,
    )
    assert res_before["ok"] is True
    day_before = res_before["people"][0]["days"][0]
    assert day_before["hours"] == 4.0
    assert day_before["below_target"] is False

    # 2. At 17:30 (after end_day_hour 17): below_target should be True
    now_after = datetime(2026, 11, 11, 17, 30, tzinfo=tz)
    res_after = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        date_from=today,
        date_to=today,
        now=now_after,
    )
    assert res_after["ok"] is True
    day_after = res_after["people"][0]["days"][0]
    assert day_after["hours"] == 4.0
    assert day_after["below_target"] is True
    assert res_after["people"][0]["missing_hours"] == 4.0


def test_range_longer_than_31_days_and_reversed_refused(base_cfg, cal):
    ident = Identity(
        username="henry",
        display_name="Henry",
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
        today=date(2026, 11, 11),
        worklogs=[],
    )

    # Reversed dates: date_from > date_to
    res_rev = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        date_from=date(2026, 11, 12),
        date_to=date(2026, 11, 10),
    )
    assert res_rev["ok"] is False
    assert "date_from không được lớn hơn date_to" in res_rev["message"]

    # Range > 31 days
    res_long = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        date_from=date(2026, 10, 1),
        date_to=date(2026, 11, 2),  # 33 days inclusive
    )
    assert res_long["ok"] is False
    assert "31 ngày" in res_long["message"]


def test_idle_with_work_in_progress(base_cfg, cal):
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime(2026, 11, 11, 10, 0, tzinfo=tz)
    today = date(2026, 11, 11)

    # carol has in_progress issue in RFID, 0h logged
    iss_carol = _make_issue(
        key="RFID-10",
        summary="Carol Task",
        assignee="carol",
    )
    # bob has in_progress issue in RFID, 4h logged
    iss_bob = _make_issue(
        key="RFID-11",
        summary="Bob Task",
        assignee="bob",
    )
    # dave has done issue in RFID, 0h logged
    iss_dave = _make_issue(
        key="RFID-12",
        status="done",
        summary="Dave Task",
        assignee="dave",
        resolution="Done",
    )
    wls = [
        Worklog(
            issue_key="RFID-11",
            author="bob",
            started=datetime(2026, 11, 11, 9, 0, tzinfo=tz),
            seconds=4 * 3600,
        ),
    ]
    snap = Snapshot(
        issues={
            "RFID-10": iss_carol,
            "RFID-11": iss_bob,
            "RFID-12": iss_dave,
        },
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=today,
        worklogs=wls,
    )
    # Lead viewing project RFID
    ident = Identity(
        username="cuong.huynh",
        display_name="Cuong Huynh",
        roles={"RFID": {"lead_ba"}},
        is_lead=True,
    )

    result = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        date_from=today,
        date_to=today,
        now=now,
    )
    assert result["ok"] is True
    assert result["idle_with_work_in_progress"] == ["carol"]


def test_rounding(base_cfg, cal):
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime(2026, 11, 11, 10, 0, tzinfo=tz)
    today = date(2026, 11, 11)

    # 4500 seconds = 1.25 hours
    # 5400 seconds = 1.5 hours
    # 3000 seconds = 0.833h -> rounds to 0.75h
    iss = _make_issue(
        key="RFID-1",
        summary="Task 1",
        assignee="henry",
    )
    wls = [
        Worklog(
            issue_key="RFID-1",
            author="henry",
            started=datetime(2026, 11, 11, 9, 0, tzinfo=tz),
            seconds=4500,
        ),
        Worklog(
            issue_key="RFID-1",
            author="henry",
            started=datetime(2026, 11, 11, 11, 0, tzinfo=tz),
            seconds=3000,
        ),
    ]
    snap = Snapshot(
        issues={"RFID-1": iss},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=today,
        worklogs=wls,
    )
    ident = Identity(
        username="henry",
        display_name="Henry",
        roles={"RFID": {"dev"}},
        is_lead=False,
    )

    result = compute_worklog_report(
        snap,
        base_cfg,
        cal,
        ident,
        project="RFID",
        date_from=today,
        date_to=today,
        now=now,
    )
    assert result["ok"] is True
    # 4500s + 3000s = 7500s = 2.083h -> rounds to 2.0h
    p = result["people"][0]
    assert p["total_hours"] == 2.0
    assert p["days"][0]["hours"] == 2.0


def test_worklog_window_widened_when_date_from_older_than_10_working_days(
    base_cfg, cal
):
    from mcp_atlassian.eggs.report import build_snapshot

    today = date(2026, 11, 11)
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    since = datetime(today.year, today.month, today.day, 0, 0, tzinfo=tz)

    gateway = FakeGateway()
    # 20 working days ago:
    old_date = cal.add_working_days(today, -20)

    # Calling build_snapshot with worklog_since=old_date
    build_snapshot(gateway, base_cfg, today, cal, since, worklog_since=old_date)

    wl_calls = [c for c in gateway.calls if c[0] == "worklogs_since"]
    assert len(wl_calls) == 1
    called_since = wl_calls[0][1][0]
    assert called_since.date() == old_date


@pytest.mark.asyncio
async def test_eggs_worklog_report_fastmcp_tool_execution(base_cfg):
    server = FastMCP("test_jira")
    register_eggs_tools(server, force=True)

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert "eggs_worklog_report" in tool_names

    # Test tool invocation via server.call_tool
    bot_gateway = FakeGateway(user="eggsbot")
    user_gateway = FakeGateway(
        user="henry",
        issues={
            "RFID-1": {
                "key": "RFID-1",
                "fields": {
                    "summary": "Task 1",
                    "project": {"key": "RFID", "name": "RFID"},
                    "issuetype": {"name": "Dev Task"},
                    "status": {"name": "In Progress"},
                    "assignee": {"name": "henry", "displayName": "Henry"},
                },
            }
        },
        worklogs=[
            Worklog(
                issue_key="RFID-1",
                author="henry",
                started=datetime(
                    2026, 11, 9, 9, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")
                ),
                seconds=31 * 3600,
            )
        ],
    )

    with (
        patch("mcp_atlassian.eggs.tools.load_config", return_value=base_cfg),
        patch(
            "mcp_atlassian.eggs.tools.get_user_gateway",
            AsyncMock(return_value=user_gateway),
        ),
        patch(
            "mcp_atlassian.eggs.tools.get_roles_reader",
            return_value=bot_gateway,
        ),
    ):
        res = await server.call_tool("eggs_worklog_report", {"project": "RFID"})
        data = json.loads(res.content[0].text)
        assert data["ok"] is True
        assert "range" in data
        assert "people" in data
        assert "idle_with_work_in_progress" in data
        assert "message" in data
        assert len(data["people"]) == 1
        assert data["people"][0]["username"] == "henry"


def _snap(issues: dict[str, Issue], wls: list[Worklog]) -> Snapshot:
    return Snapshot(
        issues=issues,
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=date(2026, 11, 11),
        worklogs=wls,
    )


def _wl(key: str, author: str, hours: float) -> Worklog:
    return Worklog(
        issue_key=key,
        author=author,
        started=datetime(2026, 11, 11, 9, 0, tzinfo=TZ),
        seconds=int(hours * 3600),
    )


_BA_RFID = Identity(
    username="ba_rfid",
    display_name="BA RFID",
    roles={"RFID": {"lead_ba"}},
    is_lead=False,
)


def _report(snap, cfg, cal, ident, **kw):
    return compute_worklog_report(
        snap,
        cfg,
        cal,
        ident,
        date_from=date(2026, 11, 11),
        date_to=date(2026, 11, 11),
        now=datetime(2026, 11, 11, 10, 0, tzinfo=TZ),
        **kw,
    )


def test_ba_viewing_person_sees_only_projects_they_lead(base_cfg, cal):
    snap = _snap(
        {
            "RFID-1": _make_issue(key="RFID-1", assignee="charlie"),
            "HH-1": _make_issue(key="HH-1", assignee="charlie"),
        },
        [
            _wl("RFID-1", "charlie", 2),
            _wl("HH-1", "charlie", 5),
            _wl("OLD-9", "charlie", 1),  # issue outside the snapshot
        ],
    )
    result = _report(snap, base_cfg, cal, _BA_RFID, person="charlie")
    assert result["ok"] is True
    assert [p["username"] for p in result["people"]] == ["charlie"]
    assert result["people"][0]["total_hours"] == 2.0
    assert [i["key"] for i in result["people"][0]["top_issues"]] == ["RFID-1"]


def test_own_view_and_lead_count_hours_outside_snapshot(base_cfg, cal):
    snap = _snap(
        {"HH-1": _make_issue(key="HH-1", assignee="charlie")},
        [_wl("HH-1", "charlie", 5), _wl("OLD-9", "charlie", 1)],
    )
    me = Identity(
        username="charlie", display_name="C", roles={"HH": {"dev"}}, is_lead=False
    )
    lead = Identity(username="cuong.huynh", display_name="L", roles={}, is_lead=True)
    assert _report(snap, base_cfg, cal, me)["people"][0]["total_hours"] == 6.0
    by_lead = _report(snap, base_cfg, cal, lead, person="charlie")
    assert by_lead["people"][0]["total_hours"] == 6.0
    # With a project, only that project's hours.
    in_hh = _report(snap, base_cfg, cal, me, project="HH")
    assert in_hh["people"][0]["total_hours"] == 5.0


def test_idle_ignores_case_and_non_work_items_and_respects_person(base_cfg, cal):
    snap = _snap(
        {
            "RFID-1": _make_issue(key="RFID-1", assignee="Bob"),
            "RFID-2": _make_issue(key="RFID-2", assignee="carol"),
            "RFID-3": _make_issue(key="RFID-3", issue_type="story", assignee="erin"),
        },
        [_wl("RFID-1", "bob", 3)],
    )
    team = _report(snap, base_cfg, cal, _BA_RFID, project="RFID")
    assert team["idle_with_work_in_progress"] == ["carol"]
    one = _report(snap, base_cfg, cal, _BA_RFID, project="RFID", person="carol")
    assert one["idle_with_work_in_progress"] == ["carol"]
    other = _report(snap, base_cfg, cal, _BA_RFID, project="RFID", person="bob")
    assert other["idle_with_work_in_progress"] == []


def test_fill_worklog_issues_fetches_issues_outside_snapshot(base_cfg):
    from mcp_atlassian.eggs.report import fill_worklog_issues

    raw = {
        "key": "HH-9",
        "fields": {
            "summary": "Closed long ago",
            "project": {"key": "HH", "name": "HH"},
            "issuetype": {"name": "Dev Task"},
            "status": {"name": "Done"},
        },
    }
    gw = FakeGateway(issues={"HH-9": raw})
    snap = _snap(
        {},
        [
            _wl("HH-9", "charlie", 2),
            Worklog(
                issue_key="HH-8",
                author="charlie",
                started=datetime(2026, 10, 1, 9, 0, tzinfo=TZ),
                seconds=3600,
            ),
        ],
    )
    out = fill_worklog_issues(gw, base_cfg, snap, date(2026, 11, 9), date(2026, 11, 11))
    assert out.issues["HH-9"].project == "HH"
    assert gw.jql_queries == ["key in (HH-9)"]
    # Nothing missing → no search.
    gw2 = FakeGateway()
    assert (
        fill_worklog_issues(gw2, base_cfg, out, date(2026, 11, 9), date(2026, 11, 11))
        is out
    )
    assert gw2.jql_queries == []
