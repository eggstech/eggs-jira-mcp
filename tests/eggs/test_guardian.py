from __future__ import annotations

import dataclasses
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.guardian import evaluate, main
from mcp_atlassian.eggs.model import Worklog
from tests.eggs.fakes import FakeGateway

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
NOW_WED = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
TODAY_WED = date(2026, 11, 18)


@pytest.fixture
def cfg() -> EggsConfig:
    return EggsConfig(
        lead_user="cuong.huynh",
        bot_user="eggsbot",
        jira_url="https://jira.eggstech.io.vn",
        timezone="Asia/Ho_Chi_Minh",
        lead_chat_webhook="https://chat.googleapis.com/v1/spaces/LEAD/messages?key=lead_key",
        ops_epic_summary="[OPS] Vận hành & Hotfix {month:02d}/{year}",
        holidays=["2026-01-01", "2026-09-02"],
        projects={
            "RFID": ProjectConfig(
                key="RFID",
                default_lead_ba="cuong.huynh",
                triage_owner="cuong.huynh",
                chat_webhook="https://chat.googleapis.com/v1/spaces/RFID/messages?key=rfid_key",
            ),
            "CP": ProjectConfig(
                key="CP",
                default_lead_ba="cuong.huynh",
                triage_owner="cuong.huynh",
                chat_webhook="https://chat.googleapis.com/v1/spaces/CP/messages?key=cp_key",
            ),
        },
        issue_types={
            "epic": ["Epic"],
            "story": ["Story"],
            "dev_task": ["Dev Task"],
            "bug_sub": ["Bug (subtask)"],
            "bug": ["Bug stand-alone"],
            "adhoc": ["Ad-hoc Task"],
            "report": ["User Report"],
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
        fields={
            "planned_start": "customfield_10108",
            "planned_end": "customfield_10109",
            "epic_link": "customfield_10100",
            "flagged": "customfield_10409",
            "qc_owner": "customfield_10702",
            "part": "customfield_10701",
            "reschedule_requester": "customfield_10706",
            "baseline_end": "customfield_10700",
            "proposed_end": "customfield_10703",
            "reschedule_reason": "customfield_10704",
            "reschedule_status": "customfield_10705",
            "reschedule_count": "customfield_10707",
            "qc_fail_count": "customfield_10708",
            "block_reason": "customfield_10709",
            "blocked_by": "customfield_10710",
            "blocked_since": "customfield_10711",
            "health_flags": "customfield_10712",
            "health_since": "customfield_10713",
            "escalation_level": "customfield_10714",
            "escalation_owner": "customfield_10715",
            "forecast_end": "customfield_10716",
            "slip_days": "customfield_10717",
            "unscheduled_count": "customfield_10718",
            "report_source": "customfield_10719",
            "reported_by": "customfield_10720",
            "reported_at": "customfield_10721",
        },
    )


@pytest.fixture
def cal(cfg: EggsConfig) -> WorkCalendar:
    return WorkCalendar(cfg.holidays)


def test_evaluate_writes_only_guardian_fields_with_override(cfg: EggsConfig, cal: WorkCalendar):
    # Overdue task -> health flag OVERDUE, level 0
    issues = {
        "RFID-1": {
            "key": "RFID-1",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "alice"},
                "customfield_10109": "2026-11-16",  # planned_end past today
                "customfield_10700": "2026-11-16",  # baseline_end
                "customfield_10712": None,  # health_flags empty
            },
        }
    }
    gw = FakeGateway(issues=issues)
    changes = evaluate(gw, cfg, NOW_WED, cal)

    assert len(changes) > 0
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) == 1
    call_name, args, kwargs = update_calls[0]
    key, fields_payload = args[0], args[1]
    assert key == "RFID-1"
    assert kwargs.get("override_screen_security") is True

    # Exact guardian field IDs allowed
    guardian_field_ids = {
        cfg.fields["health_flags"],
        cfg.fields["health_since"],
        cfg.fields["escalation_level"],
        cfg.fields["escalation_owner"],
        cfg.fields["forecast_end"],
        cfg.fields["slip_days"],
        cfg.fields["unscheduled_count"],
    }
    for field_id in fields_payload:
        assert field_id in guardian_field_ids

    # Guardian never transitions issues
    transition_calls = [c for c in gw.calls if c[0] == "transition"]
    assert len(transition_calls) == 0


def test_evaluate_is_idempotent(cfg: EggsConfig, cal: WorkCalendar):
    issues = {
        "RFID-1": {
            "key": "RFID-1",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "alice"},
                "customfield_10109": "2026-11-16",
                "customfield_10700": "2026-11-16",
                "customfield_10712": None,
            },
        }
    }
    gw = FakeGateway(issues=issues)

    # First run: writes fields
    changes1 = evaluate(gw, cfg, NOW_WED, cal)
    assert len(changes1) > 0
    writes_count_1 = len([c for c in gw.calls if c[0] == "update_fields"])
    assert writes_count_1 == 1

    # Second run without any external change -> zero writes, zero comments
    gw.calls.clear()
    changes2 = evaluate(gw, cfg, NOW_WED, cal)
    assert len(changes2) == 0
    assert len([c for c in gw.calls if c[0] == "update_fields"]) == 0
    assert len([c for c in gw.calls if c[0] == "add_comment"]) == 0


def test_cleared_flag_adds_comment(cfg: EggsConfig, cal: WorkCalendar):
    # Issue had STALE flag, but now has recent worklog today -> flag clears
    issues = {
        "RFID-1": {
            "key": "RFID-1",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "status_category_change_date": "2026-11-10T08:00:00.000+0700",
                "assignee": {"name": "alice"},
                "customfield_10108": "2026-11-18",
                "customfield_10109": "2026-11-19",
                "customfield_10700": "2026-11-19",
                "customfield_10712": [{"value": "STALE"}],  # previously STALE
                "customfield_10713": "2026-11-16",
                "customfield_10714": 0,
                "customfield_10715": {"name": "alice"},
            },
        }
    }
    worklogs = [
        Worklog(issue_key="RFID-1", author="alice", started=NOW_WED, seconds=3600),
    ]
    gw = FakeGateway(issues=issues, worklogs=worklogs)
    changes = evaluate(gw, cfg, NOW_WED, cal)

    assert len(changes) > 0
    # Added comment noting cleared flag
    comment_calls = [c for c in gw.calls if c[0] == "add_comment"]
    assert len(comment_calls) == 1
    _, args, _ = comment_calls[0]
    assert args[0] == "RFID-1"
    assert f"[{cfg.bot_user}] Đã xử lý: STALE" in args[1]

    # Flags cleared in Jira update payload
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) == 1
    payload = update_calls[0][1][1]
    assert payload[cfg.fields["health_flags"]] is None


def test_story_gets_forecast_and_at_risk(cfg: EggsConfig, cal: WorkCalendar):
    # Story due 2026-11-20, child dev task planned_end 2026-11-23 -> forecast = 2026-11-24 (due + buffer) -> AT_RISK
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Story", "subtask": False},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "carol"},
                "duedate": "2026-11-20",
                "components": [{"name": "CH - CỬA HÀNG"}],
                "subtasks": [{"key": "RFID-11"}],
            },
        },
        "RFID-11": {
            "key": "RFID-11",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "alice"},
                "parent": {"key": "RFID-10"},
                "customfield_10108": "2026-11-18",
                "customfield_10109": "2026-11-23",  # forecast = 23 + 1 = 24th
                "customfield_10700": "2026-11-23",
            },
        },
    }
    gw = FakeGateway(issues=issues)
    evaluate(gw, cfg, NOW_WED, cal)

    story_updates = [c for c in gw.calls if c[0] == "update_fields" and c[1][0] == "RFID-10"]
    assert len(story_updates) == 1
    story_payload = story_updates[0][1][1]

    # forecast_end = 2026-11-24
    assert story_payload[cfg.fields["forecast_end"]] == "2026-11-24"
    # AT_RISK flag present
    assert {"value": "AT_RISK"} in story_payload[cfg.fields["health_flags"]]
    # Slip days > 0
    assert story_payload[cfg.fields["slip_days"]] == 2


def test_report_posts_lead_and_project_webhooks(cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    gw = FakeGateway(issues={})
    posted: list[tuple[str, str, str | None]] = []

    def mock_post_to_chat(webhook: str, text: str, http_post: Any = None, project_key: str | None = None) -> None:
        posted.append((webhook, text, project_key))

    monkeypatch.setattr("mcp_atlassian.eggs.guardian.post_to_chat", mock_post_to_chat)
    monkeypatch.setattr("mcp_atlassian.eggs.guardian.datetime", type("MockDT", (), {
        "now": lambda tz=None: NOW_WED,
        "combine": datetime.combine,
    }))

    code = main(["report"], gateway=gw)
    assert code == 0

    # Posted to lead webhook and active projects with chat_webhook (RFID and CP)
    webhooks_called = [p[0] for p in posted]
    assert cfg.lead_chat_webhook in webhooks_called
    assert cfg.projects["RFID"].chat_webhook in webhooks_called
    assert cfg.projects["CP"].chat_webhook in webhooks_called


def test_remind_lists_workers_without_role(cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    # User 'stranger_bob' has worklog in RFID within last 5 working days, but no role in RFID
    worklogs = [
        Worklog(issue_key="RFID-1", author="stranger_bob", started=NOW_WED, seconds=3600),
    ]
    # Project roles: only alice in Developers
    gw = FakeGateway(
        issues={},
        worklogs=worklogs,
        role_members={"RFID": {"Developers": ["alice"]}, "CP": {}},
    )
    posted: list[tuple[str, str, str | None]] = []

    def mock_post_to_chat(webhook: str, text: str, http_post: Any = None, project_key: str | None = None) -> None:
        posted.append((webhook, text, project_key))

    monkeypatch.setattr("mcp_atlassian.eggs.guardian.post_to_chat", mock_post_to_chat)
    monkeypatch.setattr("mcp_atlassian.eggs.guardian.datetime", type("MockDT", (), {
        "now": lambda tz=None: NOW_WED,
        "combine": datetime.combine,
    }))

    code = main(["remind"], gateway=gw)
    assert code == 0

    # Lead webhook message should mention stranger_bob and RFID, and end with "Gõ `/eggs` để cập nhật."
    lead_messages = [p[1] for p in posted if p[0] == cfg.lead_chat_webhook]
    assert len(lead_messages) == 1
    assert "stranger_bob" in lead_messages[0]
    assert "RFID" in lead_messages[0]
    assert lead_messages[0].strip().endswith("Gõ `/eggs` để cập nhật.")


def test_skip_on_non_working_day(cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    # Saturday 2026-11-21
    saturday = datetime(2026, 11, 21, 10, 0, tzinfo=TZ)
    monkeypatch.setattr("mcp_atlassian.eggs.guardian.datetime", type("MockDT", (), {
        "now": lambda tz=None: saturday,
        "combine": datetime.combine,
    }))

    gw = FakeGateway()

    # evaluate, report, remind, ops-epics must exit 0 without calling Jira
    for sub in ["evaluate", "report", "remind", "ops-epics"]:
        gw.calls.clear()
        code = main([sub], gateway=gw)
        assert code == 0
        assert len(gw.calls) == 0

    # resolve-fields does NOT skip on non-working day
    gw.calls.clear()
    code = main(["resolve-fields"], gateway=gw)
    assert code == 0
    assert any(c[0] == "get_fields" for c in gw.calls)


def test_ops_epics_creates_missing_only(cfg: EggsConfig, cal: WorkCalendar):
    # RFID already has current month OPS epic; CP does not
    summary_rfid = cfg.ops_epic_summary.format(month=TODAY_WED.month, year=TODAY_WED.year)
    issues = {
        "RFID-OPS": {
            "key": "RFID-OPS",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Epic", "subtask": False},
                "summary": summary_rfid,
                "status": {"name": "Dev In Progress"},
            },
        }
    }
    gw = FakeGateway(issues=issues)

    from mcp_atlassian.eggs.guardian import ensure_ops_epics

    created = ensure_ops_epics(gw, cfg, TODAY_WED, cal)
    assert len(created) == 1
    assert "CP" in created[0]

    # Check create_issue called only for CP
    create_calls = [c for c in gw.calls if c[0] == "create_issue"]
    assert len(create_calls) == 1
    create_payload = create_calls[0][1][0]
    assert create_payload["project"]["key"] == "CP"
    assert create_payload["summary"] == summary_rfid
    assert create_payload["assignee"]["name"] == cfg.projects["CP"].default_lead_ba
    # Due date = last working day of Nov 2026 (Nov 30 is Mon)
    assert create_payload["duedate"] == "2026-11-30"

    # Second run is idempotent: creates nothing
    gw.calls.clear()
    created2 = ensure_ops_epics(gw, cfg, TODAY_WED, cal)
    assert len(created2) == 0
    assert len([c for c in gw.calls if c[0] == "create_issue"]) == 0


def test_missing_env_exits_2(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.delenv("EGGS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("EGGS_CONFIG", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["evaluate"])
    assert exc.value.code == 2
