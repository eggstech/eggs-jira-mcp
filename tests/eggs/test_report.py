from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig, Thresholds
from mcp_atlassian.eggs.forecast import ActorRow, EpicMetrics
from mcp_atlassian.eggs.model import Issue
from mcp_atlassian.eggs.report import (
    Snapshot,
    build_snapshot,
    render_my_day,
    render_pending_approvals,
    render_report,
    render_workstream,
)
from tests.eggs.fakes import FakeGateway

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TODAY = date(2026, 11, 18)


def _make_issue(
    key="DEV-1",
    issue_type="dev_task",
    status="todo",
    summary="Task Summary",
    assignee="dev.a",
    parent_key=None,
    epic_key="EPIC-1",
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
        created=created or datetime(2026, 11, 10, 8, 0, tzinfo=TZ),
        status_changed_at=created or datetime(2026, 11, 10, 8, 0, tzinfo=TZ),
        planned_start=None,
        planned_end=planned_end,
        baseline_end=None,
        due_date=due_date,
        estimate_seconds=3600,
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
        frozen=False,
        labels=tuple(labels),
        resolution=resolution,
    )


@pytest.fixture
def cfg():
    return EggsConfig(
        lead_user="cuong.huynh",
        timezone="Asia/Ho_Chi_Minh",
        jira_url="https://jira.eggstech.io.vn",
        ops_epic_prefix="[OPS]",
        fields={
            "epic_link": "customfield_epic_link",
            "health_flags": "customfield_health_flags",
            "health_since": "customfield_health_since",
            "escalation_level": "customfield_escalation_level",
            "escalation_owner": "customfield_escalation_owner",
            "blocked_by": "customfield_blocked_by",
            "flagged": "customfield_flagged",
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
        thresholds=Thresholds(
            stale_working_days=2,
            unscheduled_window=5,
            interrupt_window=5,
            escalate_l1_after=1,
            escalate_l2_after=2,
            internal_block_escalate_after=2,
            qc_flow_buffer=1,
            triage_working_days=1,
        ),
        projects={
            "HMKPOS": ProjectConfig(
                key="HMKPOS",
                default_lead_ba="cuong.huynh",
                triage_owner="triage.qc",
            ),
            "HMKLOYAL": ProjectConfig(
                key="HMKLOYAL",
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


def test_build_snapshot_fills_subtask_epic_from_parent(cfg, cal):
    # Tests that subtasks without epic_link get epic_key populated from parent Story
    raw_parent_story = {
        "key": "HMKPOS-100",
        "fields": {
            "summary": "Customer Login Flow",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "DEV To Do"},
            "customfield_epic_link": "HMKPOS-10",
            "components": [{"name": "CH - CỬA HÀNG"}],
            "duedate": "2026-11-25",
        },
    }
    raw_subtask = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Implement Login API",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "DEV To Do"},
            "parent": {
                "key": "HMKPOS-100",
                "fields": {"issuetype": {"name": "Story", "subtask": False}},
            },
            # Note: No epic link field on subtask in Jira!
        },
    }
    raw_epic = {
        "key": "HMKPOS-10",
        "fields": {
            "summary": "Q4 POS Revamp",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "duedate": "2026-11-30",
        },
    }

    # Pass search_filter to verify search filter works
    def s_filter(jql: str, item: dict) -> bool:
        return True

    fake = FakeGateway(
        issues={
            "HMKPOS-10": raw_epic,
            "HMKPOS-100": raw_parent_story,
            "HMKPOS-101": raw_subtask,
        },
        search_filter=s_filter,
    )

    since = datetime(2026, 11, 18, 0, 0, tzinfo=TZ)
    snapshot = build_snapshot(fake, cfg, TODAY, cal, since)

    assert "HMKPOS-101" in snapshot.issues
    subtask = snapshot.issues["HMKPOS-101"]
    assert subtask.parent_key == "HMKPOS-100"
    assert subtask.epic_key == "HMKPOS-10"


def test_report_section_counts_user_reports(cfg, cal):
    # Previous working day before TODAY (2026-11-18) is 2026-11-17 (Tuesday)
    # 7 mới: created on 2026-11-17
    # 3 là lỗi: resolved on 2026-11-17 with resolution="Is a Bug"
    # 4 không phải lỗi: resolved on 2026-11-17 with resolution="User Guidance"
    # 1 chờ xác minh > 1 ngày: STALE report
    issues = {}

    # 7 created yesterday
    for i in range(1, 8):
        k = f"HMKPOS-R{i}"
        issues[k] = _make_issue(
            key=k,
            issue_type="report",
            status="open",
            created=datetime(2026, 11, 17, 10, 0, tzinfo=TZ),
        )

    # 3 resolved yesterday with bug
    for i in range(8, 11):
        k = f"HMKPOS-R{i}"
        issues[k] = _make_issue(
            key=k,
            issue_type="report",
            status="done",
            created=datetime(2026, 11, 16, 10, 0, tzinfo=TZ),
            resolution="Is a Bug",
        )
        # changelog / status_changed_at on yesterday
        object.__setattr__(issues[k], "status_changed_at", datetime(2026, 11, 17, 15, 0, tzinfo=TZ))

    # 4 resolved yesterday not a bug
    for i in range(11, 15):
        k = f"HMKPOS-R{i}"
        issues[k] = _make_issue(
            key=k,
            issue_type="report",
            status="done",
            created=datetime(2026, 11, 16, 10, 0, tzinfo=TZ),
            resolution="User Guidance",
        )
        object.__setattr__(issues[k], "status_changed_at", datetime(2026, 11, 17, 16, 0, tzinfo=TZ))

    # 1 stale report (created earlier, open, STALE flag)
    stale_k = "HMKPOS-R99"
    issues[stale_k] = _make_issue(
        key=stale_k,
        issue_type="report",
        status="open",
        created=datetime(2026, 11, 10, 9, 0, tzinfo=TZ),
    )
    flags = {stale_k: frozenset({"STALE"})}

    snapshot = Snapshot(
        issues=issues,
        flags=flags,
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=TODAY,
    )

    report_text = render_report(snapshot, cfg)
    assert "📨 Phản ánh hôm qua" in report_text
    # Line should match: 7 mới · 3 là lỗi · 4 không phải lỗi · 1 chờ xác minh > 1 ngày
    assert "7 mới · 3 là lỗi · 4 không phải lỗi · 1 chờ xác minh > 1 ngày" in report_text


def test_sections_in_order_and_empty_omitted(cfg, cal):
    # Only have an escalation (L2) and an on-track epic
    epic_on_track = _make_issue(key="HMKPOS-1", issue_type="epic", status="in_progress", summary="On track Epic")
    task_l2 = _make_issue(key="HMKPOS-2", issue_type="dev_task", summary="L2 Task", escalation_level=2)

    from mcp_atlassian.eggs.escalation import Escalation
    esc = {task_l2.key: Escalation(level=2, owner="cuong.huynh", since=TODAY)}
    epics_metrics = {
        epic_on_track.key: EpicMetrics(
            key=epic_on_track.key,
            due=date(2026, 11, 30),
            forecast_end=date(2026, 11, 28),
            slip_days=-2,
            unscheduled=0,
            unknown_eta=False,
            actors=[],
        )
    }

    snapshot = Snapshot(
        issues={epic_on_track.key: epic_on_track, task_l2.key: task_l2},
        flags={task_l2.key: frozenset({"OVERDUE"})},
        escalations=esc,
        flows={},
        epics=epics_metrics,
        not_updated=[],
        today=TODAY,
    )

    text = render_report(snapshot, cfg)

    # Section 1 should be present
    assert "🔴 Leo thang" in text
    # Section 7 should be present
    assert "✅ Đúng tiến độ: 1 workstream" in text

    # Empty sections omitted
    assert "🟠 Workstream lệch cam kết" not in text
    assert "🟡 Chờ duyệt dời lịch" not in text
    assert "⛔ Đang block" not in text
    assert "👤 Chưa cập nhật hôm qua" not in text
    assert "📨 Phản ánh hôm qua" not in text

    # Verify order: Leo thang comes before Đúng tiến độ
    pos_leo_thang = text.index("🔴 Leo thang")
    pos_dung_tien_do = text.index("✅ Đúng tiến độ")
    assert pos_leo_thang < pos_dung_tien_do


def test_on_track_collapsed(cfg, cal):
    epics_metrics = {}
    issues = {}
    for i in range(3):
        k = f"HMKPOS-{i+1}"
        issues[k] = _make_issue(key=k, issue_type="epic", summary=f"Epic {i+1}")
        epics_metrics[k] = EpicMetrics(
            key=k,
            due=date(2026, 11, 30),
            forecast_end=date(2026, 11, 25),
            slip_days=-3,
            unscheduled=0,
            unknown_eta=False,
            actors=[],
        )

    snapshot = Snapshot(
        issues=issues,
        flags={},
        escalations={},
        flows={},
        epics=epics_metrics,
        not_updated=[],
        today=TODAY,
    )

    text = render_report(snapshot, cfg)
    assert "✅ Đúng tiến độ: 3 workstream" in text
    assert "Epic 1" not in text  # Collapsed, individual summaries not listed


def test_truncation_message(cfg, cal):
    # More than max_lines items in blocked section
    issues = {}
    flags = {}
    for i in range(15):
        k = f"HMKPOS-B{i+1}"
        issues[k] = _make_issue(key=k, summary=f"Blocked item {i+1}", blocked=True)
        flags[k] = frozenset({"BLOCKED"})

    snapshot = Snapshot(
        issues=issues,
        flags=flags,
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=TODAY,
    )

    text = render_report(snapshot, cfg, max_lines=10)
    assert "⛔ Đang block" in text
    assert "… và 5 mục khác" in text


def test_ops_epic_not_in_slip_section(cfg, cal):
    # [OPS] epic that is slipping should NOT appear in section 2
    ops_epic = _make_issue(key="HMKPOS-OPS", issue_type="epic", summary="[OPS] Operations November")
    normal_epic = _make_issue(key="HMKPOS-SLIP", issue_type="epic", summary="Normal Slipped Epic")

    epics_metrics = {
        ops_epic.key: EpicMetrics(
            key=ops_epic.key,
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 25),
            slip_days=3,
            unscheduled=0,
            unknown_eta=False,
            actors=[],
        ),
        normal_epic.key: EpicMetrics(
            key=normal_epic.key,
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 25),
            slip_days=3,
            unscheduled=0,
            unknown_eta=False,
            actors=[],
        ),
    }

    snapshot = Snapshot(
        issues={ops_epic.key: ops_epic, normal_epic.key: normal_epic},
        flags={},
        escalations={},
        flows={},
        epics=epics_metrics,
        not_updated=[],
        today=TODAY,
    )

    text = render_report(snapshot, cfg)
    assert "🟠 Workstream lệch cam kết" in text
    assert "Normal Slipped Epic" in text
    assert "[OPS] Operations November" not in text


def test_actor_line_in_slip_section(cfg, cal):
    # Slipped epic with actor breakdown
    epic = _make_issue(key="HMKPOS-10", issue_type="epic", summary="Revamp Epic")
    actors = [
        ActorRow(
            actor="KT - KẾ TOÁN",
            actor_code="KT",
            total=3,
            dev=0,
            qc=0,
            ready_for_uat=3,
            in_uat=0,
            signed=0,
            at_risk=0,
        ),
        ActorRow(
            actor="CH - CỬA HÀNG",
            actor_code="CH",
            total=10,
            dev=2,
            qc=1,
            ready_for_uat=4,
            in_uat=2,
            signed=1,
            at_risk=0,
        ),
        ActorRow(
            actor="KH - KHÁCH HÀNG",
            actor_code="KH",
            total=6,
            dev=6,
            qc=0,
            ready_for_uat=0,
            in_uat=0,
            signed=0,
            at_risk=2,
        ),
    ]

    epics_metrics = {
        epic.key: EpicMetrics(
            key=epic.key,
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 25),
            slip_days=3,
            unscheduled=0,
            unknown_eta=False,
            actors=actors,
        )
    }

    snapshot = Snapshot(
        issues={epic.key: epic},
        flags={},
        escalations={},
        flows={},
        epics=epics_metrics,
        not_updated=[],
        today=TODAY,
    )

    text = render_report(snapshot, cfg)
    assert "🟠 Workstream lệch cam kết" in text
    assert "KT 3/3 sẵn sàng UAT" in text
    assert "CH 4/10" in text
    assert "KH 0/6 (AT_RISK 2)" in text


def test_legacy_actor_model_epic_level_only(cfg, cal):
    # Epic labelled "legacy-actor-model" should not render the actor breakdown line
    epic = _make_issue(
        key="HMKPOS-10",
        issue_type="epic",
        summary="Legacy Epic",
        labels=["legacy-actor-model"],
    )
    actors = [
        ActorRow(
            actor="CH - CỬA HÀNG",
            actor_code="CH",
            total=10,
            dev=2,
            qc=1,
            ready_for_uat=4,
            in_uat=2,
            signed=1,
            at_risk=0,
        ),
    ]

    epics_metrics = {
        epic.key: EpicMetrics(
            key=epic.key,
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 25),
            slip_days=3,
            unscheduled=0,
            unknown_eta=False,
            actors=actors,
        )
    }

    snapshot = Snapshot(
        issues={epic.key: epic},
        flags={},
        escalations={},
        flows={},
        epics=epics_metrics,
        not_updated=[],
        today=TODAY,
    )

    text = render_report(snapshot, cfg)
    assert "🟠 Workstream lệch cam kết" in text
    assert "HMKPOS-10" in text
    assert "CH 4/10" not in text


def test_project_filter(cfg, cal):
    # Filters by project
    pos_epic = _make_issue(key="HMKPOS-1", issue_type="epic", summary="POS Epic")
    loyal_epic = _make_issue(key="HMKLOYAL-1", issue_type="epic", summary="Loyalty Epic")

    epics_metrics = {
        pos_epic.key: EpicMetrics(
            key=pos_epic.key,
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 25),
            slip_days=3,
            unscheduled=0,
            unknown_eta=False,
            actors=[],
        ),
        loyal_epic.key: EpicMetrics(
            key=loyal_epic.key,
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 25),
            slip_days=3,
            unscheduled=0,
            unknown_eta=False,
            actors=[],
        ),
    }

    snapshot = Snapshot(
        issues={pos_epic.key: pos_epic, loyal_epic.key: loyal_epic},
        flags={},
        escalations={},
        flows={},
        epics=epics_metrics,
        not_updated=[],
        today=TODAY,
    )

    text = render_report(snapshot, cfg, project="HMKPOS")
    assert "HMKPOS-1" in text
    assert "HMKLOYAL-1" not in text


def test_workstream_and_report_agree(cfg, cal):
    # Tests that render_workstream and render_report agree on slip days and actor breakdown
    epic = _make_issue(key="HMKPOS-10", issue_type="epic", summary="Revamp Epic")
    actors = [
        ActorRow(
            actor="KT - KẾ TOÁN",
            actor_code="KT",
            total=3,
            dev=0,
            qc=0,
            ready_for_uat=3,
            in_uat=0,
            signed=0,
            at_risk=0,
        ),
        ActorRow(
            actor="CH - CỬA HÀNG",
            actor_code="CH",
            total=10,
            dev=2,
            qc=1,
            ready_for_uat=4,
            in_uat=2,
            signed=1,
            at_risk=0,
        ),
    ]

    epics_metrics = {
        epic.key: EpicMetrics(
            key=epic.key,
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 25),
            slip_days=3,
            unscheduled=0,
            unknown_eta=False,
            actors=actors,
        )
    }

    snapshot = Snapshot(
        issues={epic.key: epic},
        flags={},
        escalations={},
        flows={},
        epics=epics_metrics,
        not_updated=[],
        today=TODAY,
    )

    report_text = render_report(snapshot, cfg)
    ws_text = render_workstream(snapshot, "HMKPOS-10")

    # Both show slip days (3) without + sign
    assert "Trễ 3 ngày" in report_text
    assert "3 ngày" in ws_text
    # Table columns in render_workstream
    assert "Actor" in ws_text
    assert "Luồng" in ws_text
    assert "Sẵn sàng UAT" in ws_text
    assert "CH - CỬA HÀNG" in ws_text
    assert "KT - KẾ TOÁN" in ws_text


def test_done_item_with_impediment_flag_not_listed(cfg, cal):
    # Done item with blocked=True / Impediment should NOT be listed in Section 4 (Đang block)
    done_blocked = _make_issue(
        key="HMKPOS-99",
        issue_type="dev_task",
        status="done",
        blocked=True,
        blocked_by="Nội bộ",
        health_flags=frozenset({"BLOCKED"}),
    )
    unresolved_blocked = _make_issue(
        key="HMKPOS-98",
        issue_type="dev_task",
        status="in_progress",
        blocked=True,
        blocked_by="Nội bộ",
        health_flags=frozenset({"BLOCKED"}),
    )
    snapshot = Snapshot(
        issues={done_blocked.key: done_blocked, unresolved_blocked.key: unresolved_blocked},
        flags={done_blocked.key: frozenset({"BLOCKED"}), unresolved_blocked.key: frozenset({"BLOCKED"})},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=TODAY,
    )
    text = render_report(snapshot, cfg)
    assert "HMKPOS-98" in text
    assert "HMKPOS-99" not in text


def test_resolved_epic_not_counted_on_track(cfg, cal):
    # Resolved epic should not count towards on-track count
    done_epic = _make_issue(
        key="HMKPOS-10",
        issue_type="epic",
        status="done",
        summary="Done Epic",
    )
    epics_metrics = {
        done_epic.key: EpicMetrics(
            key=done_epic.key,
            due=date(2026, 11, 20),
            forecast_end=date(2026, 11, 20),
            slip_days=0,
            unscheduled=0,
            unknown_eta=False,
            actors=[],
        )
    }
    snapshot = Snapshot(
        issues={done_epic.key: done_epic},
        flags={},
        escalations={},
        flows={},
        epics=epics_metrics,
        not_updated=[],
        today=TODAY,
    )
    text = render_report(snapshot, cfg)
    assert "✅ Đúng tiến độ" not in text


def test_render_my_day_and_pending_approvals_require_cfg(cfg, cal):
    snapshot = Snapshot(
        issues={},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=TODAY,
    )
    with pytest.raises(TypeError):
        render_my_day(snapshot, "cuong.huynh")  # missing required cfg

    with pytest.raises(TypeError):
        render_pending_approvals(snapshot, "cuong.huynh")  # missing required cfg

    # With cfg it works
    assert "Kế hoạch ngày" in render_my_day(snapshot, "cuong.huynh", cfg)
    assert "Không có yêu cầu dời lịch" in render_pending_approvals(snapshot, "cuong.huynh", cfg)


def test_monday_report_yesterday_is_friday(cfg, cal):
    # On Monday 2026-11-16, previous working day is Friday 2026-11-13
    monday = date(2026, 11, 16)
    fri_report = _make_issue(
        key="HMKPOS-R1",
        issue_type="report",
        status="open",
        created=datetime(2026, 11, 13, 10, 0, tzinfo=TZ),
    )
    sat_report = _make_issue(
        key="HMKPOS-R2",
        issue_type="report",
        status="open",
        created=datetime(2026, 11, 14, 10, 0, tzinfo=TZ),
    )
    snapshot = Snapshot(
        issues={fri_report.key: fri_report, sat_report.key: sat_report},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=[],
        today=monday,
    )
    text = render_report(snapshot, cfg)
    # 1 mới (Friday report counted, Saturday report not counted)
    assert "1 mới" in text


def test_snapshot_unknown_eta_from_child_l2(cfg, cal):
    raw_epic = {
        "key": "HMKPOS-10",
        "fields": {
            "summary": "Epic 10",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "duedate": "2026-11-30",
        },
    }
    raw_parent_story = {
        "key": "HMKPOS-100",
        "fields": {
            "summary": "Parent Story",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "Dev In Progress"},
            "customfield_epic_link": "HMKPOS-10",
            "components": [{"name": "CH - CỬA HÀNG"}],
            "duedate": "2026-11-25",
        },
    }
    # Child subtask of the story has an L2 flag (health_since was 5 working days ago)
    raw_child = {
        "key": "HMKPOS-101",
        "fields": {
            "summary": "Child Dev Task",
            "project": {"key": "HMKPOS"},
            "issuetype": {"name": "Dev Task", "subtask": True},
            "status": {"name": "Dev In Progress"},
            "parent": {
                "key": "HMKPOS-100",
                "fields": {"issuetype": {"name": "Story", "subtask": False}},
            },
            "created": "2026-11-01T08:00:00.000+0700",
            "customfield_health_flags": [{"value": "STALE"}],
            "customfield_health_since": "2026-11-10",  # > 2 working days before 2026-11-18 -> L2
        },
    }
    fake = FakeGateway(
        issues={
            "HMKPOS-10": raw_epic,
            "HMKPOS-100": raw_parent_story,
            "HMKPOS-101": raw_child,
        },
    )
    since = datetime(2026, 11, 18, 0, 0, tzinfo=TZ)
    snapshot = build_snapshot(fake, cfg, TODAY, cal, since)

    # Check child escalation is L2
    assert snapshot.escalations["HMKPOS-101"].level == 2
    # Check epic unknown_eta derived from child L2
    assert snapshot.epics["HMKPOS-10"].unknown_eta is True


def test_bot_user_excluded_from_not_updated_section(cfg, cal):
    # eggsbot has an in-progress issue without yesterday worklog, but must be excluded from not_updated
    snapshot = Snapshot(
        issues={},
        flags={},
        escalations={},
        flows={},
        epics={},
        not_updated=["alice", cfg.bot_user],
        today=TODAY,
    )
    text = render_report(snapshot, cfg)
    assert "@alice" in text
    assert f"@{cfg.bot_user}" not in text

