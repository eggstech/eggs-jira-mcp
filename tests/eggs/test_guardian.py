from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.gateway import JiraError
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


def test_evaluate_writes_only_guardian_fields(cfg: EggsConfig, cal: WorkCalendar):
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
    assert kwargs.get("notify_users") is False

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


def test_evaluate_skips_resolved_issues_zero_gateway_writes(
    cfg: EggsConfig, cal: WorkCalendar
):
    # Done and Cancelled issues that still carry flags -> zero gateway writes (no update_fields, no add_comment)
    issues = {
        "RFID-DONE": {
            "key": "RFID-DONE",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Done"},
                "status_category_change_date": "2026-11-10T08:00:00.000+0700",
                "assignee": {"name": "alice"},
                "customfield_10108": "2026-11-18",
                "customfield_10109": "2026-11-19",
                "customfield_10700": "2026-11-19",
                "customfield_10712": [{"value": "STALE"}],  # carries flags
                "customfield_10713": "2026-11-16",
                "customfield_10714": 0,
                "customfield_10715": {"name": "alice"},
            },
        },
        "RFID-CANCELLED": {
            "key": "RFID-CANCELLED",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Cancelled"},
                "status_category_change_date": "2026-11-10T08:00:00.000+0700",
                "assignee": {"name": "alice"},
                "customfield_10108": "2026-11-18",
                "customfield_10109": "2026-11-19",
                "customfield_10700": "2026-11-19",
                "customfield_10712": [{"value": "OVERDUE"}],  # carries flags
                "customfield_10713": "2026-11-16",
                "customfield_10714": 0,
                "customfield_10715": {"name": "alice"},
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = evaluate(gw, cfg, NOW_WED, cal)

    write_calls = [c for c in gw.calls if c[0] in ("update_fields", "add_comment")]
    assert len(write_calls) == 0
    assert len(res) == 0


def test_evaluate_rewrites_manually_tampered_health_flags_to_computed_value(
    cfg: EggsConfig, cal: WorkCalendar
):
    # A user manually set Health Flags to OVERDUE on an issue whose snapshot
    # does NOT justify it (active dev task with future planned_end and recent worklog).
    # evaluate must rewrite the flags to the computed value (cleared/None).
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
                "customfield_10109": "2026-11-20",  # future date, not overdue
                "customfield_10700": "2026-11-20",
                "customfield_10712": [{"value": "OVERDUE"}],  # manually set by user
                "customfield_10713": "2026-11-16",
                "customfield_10714": 1,
                "customfield_10715": {"name": "alice"},
            },
        }
    }
    worklogs = [
        Worklog(issue_key="RFID-1", author="alice", started=NOW_WED, seconds=3600),
    ]
    gw = FakeGateway(issues=issues, worklogs=worklogs)
    res = evaluate(gw, cfg, NOW_WED, cal)

    assert res.has_errors is False
    assert len(res) > 0

    # Health Flags must be rewritten to computed value (None / cleared)
    update_calls = [
        c for c in gw.calls if c[0] == "update_fields" and c[1][0] == "RFID-1"
    ]
    assert len(update_calls) == 1
    _, args, kwargs = update_calls[0]
    payload = args[1]
    assert payload[cfg.fields["health_flags"]] is None
    assert payload[cfg.fields["escalation_level"]] is None
    assert kwargs.get("notify_users") is False

    # Issue state in gateway mutated to computed value
    assert gw.issues["RFID-1"]["fields"][cfg.fields["health_flags"]] is None
    assert gw.issues["RFID-1"]["fields"][cfg.fields["escalation_level"]] is None

    # Comment recorded for cleared flag
    comment_calls = [
        c for c in gw.calls if c[0] == "add_comment" and c[1][0] == "RFID-1"
    ]
    assert len(comment_calls) == 1
    assert "Đã xử lý: OVERDUE" in comment_calls[0][1][1]


def test_evaluate_update_fields_error_adds_no_comment(
    cfg: EggsConfig, cal: WorkCalendar, caplog
):
    # If update_fields raises, it must not stop the run: log key and error, skip comment
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

    def failing_update_fields(
        key: str, fields: dict, *, notify_users: bool = True
    ) -> None:
        raise JiraError("Jira write permission denied")

    gw.update_fields = failing_update_fields

    with caplog.at_level(logging.ERROR):
        res = evaluate(gw, cfg, NOW_WED, cal)

    # Must NOT raise, but record error
    assert res.has_errors is True
    assert res.exit_code == 1

    # Log only issue key and error text
    error_logs = [rec.message for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(error_logs) == 1
    assert "RFID-1" in error_logs[0]
    assert "Jira write permission denied" in error_logs[0]
    assert "customfield_" not in error_logs[0]

    # Cleared flag comment skipped
    comment_calls = [c for c in gw.calls if c[0] == "add_comment"]
    assert len(comment_calls) == 0


def test_evaluate_update_fields_error_continues_with_other_issues(
    cfg: EggsConfig, cal: WorkCalendar, caplog
):
    issues = {
        "RFID-FAIL": {
            "key": "RFID-FAIL",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "status_category_change_date": "2026-11-10T08:00:00.000+0700",
                "assignee": {"name": "alice"},
                "customfield_10108": "2026-11-18",
                "customfield_10109": "2026-11-19",
                "customfield_10700": "2026-11-19",
                "customfield_10712": [{"value": "STALE"}],
                "customfield_10713": "2026-11-16",
                "customfield_10714": 0,
                "customfield_10715": {"name": "alice"},
            },
        },
        "RFID-OK": {
            "key": "RFID-OK",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "bob"},
                "customfield_10109": "2026-11-16",  # overdue
                "customfield_10700": "2026-11-16",
                "customfield_10712": None,
            },
        },
    }
    worklogs = [
        Worklog(issue_key="RFID-FAIL", author="alice", started=NOW_WED, seconds=3600),
    ]
    gw = FakeGateway(issues=issues, worklogs=worklogs)
    orig_update_fields = gw.update_fields

    def selective_update_fields(
        key: str, fields: dict, *, notify_users: bool = True
    ) -> None:
        if key == "RFID-FAIL":
            raise JiraError("Field customfield_10712 cannot be set")
        orig_update_fields(key, fields, notify_users=notify_users)

    gw.update_fields = selective_update_fields

    with caplog.at_level(logging.ERROR):
        res = evaluate(gw, cfg, NOW_WED, cal)

    assert res.has_errors is True
    assert res.exit_code == 1

    # RFID-FAIL logged
    error_logs = [rec.message for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(error_logs) == 1
    assert "RFID-FAIL" in error_logs[0]
    assert "Field customfield_10712 cannot be set" in error_logs[0]

    # RFID-FAIL comments skipped
    fail_comments = [
        c for c in gw.calls if c[0] == "add_comment" and c[1][0] == "RFID-FAIL"
    ]
    assert len(fail_comments) == 0

    # RFID-OK update succeeded
    ok_updates = [
        c for c in gw.calls if c[0] == "update_fields" and c[1][0] == "RFID-OK"
    ]
    assert len(ok_updates) == 1


def test_main_evaluate_exits_1_on_write_failure(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    issues = {
        "RFID-FAIL": {
            "key": "RFID-FAIL",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "bob"},
                "customfield_10109": "2026-11-16",  # overdue
                "customfield_10700": "2026-11-16",
                "customfield_10712": None,
            },
        },
    }
    gw = FakeGateway(issues=issues)

    def failing_update_fields(
        key: str, fields: dict, *, notify_users: bool = True
    ) -> None:
        raise JiraError("Internal Jira error")

    gw.update_fields = failing_update_fields

    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    code = main(["evaluate"], gateway=gw)
    assert code == 1


def test_guardian_main_evaluate_logs_only_counts_no_issue_content(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    # Issue with clearable flag
    issues = {
        "RFID-1": {
            "key": "RFID-1",
            "fields": {
                "summary": "Secret issue summary that must never be logged",
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "status_category_change_date": "2026-11-10T08:00:00.000+0700",
                "assignee": {"name": "alice"},
                "customfield_10108": "2026-11-18",
                "customfield_10109": "2026-11-19",
                "customfield_10700": "2026-11-19",
                "customfield_10712": [{"value": "STALE"}],
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
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    with caplog.at_level(logging.INFO):
        code = main(["evaluate"], gateway=gw)

    assert code == 0
    # Must log count of changed issues and fields
    eval_logs = [rec.message for rec in caplog.records if "1" in rec.message]
    assert len(eval_logs) > 0
    # Must NOT log issue key, summary, or field content
    for rec in caplog.records:
        assert "RFID-1" not in rec.message
        assert "Secret issue summary" not in rec.message
        assert "customfield_" not in rec.message
        assert "health_flags" not in rec.message


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

    story_updates = [
        c for c in gw.calls if c[0] == "update_fields" and c[1][0] == "RFID-10"
    ]
    assert len(story_updates) == 1
    story_payload = story_updates[0][1][1]

    # forecast_end = 2026-11-24
    assert story_payload[cfg.fields["forecast_end"]] == "2026-11-24"
    # AT_RISK flag present
    assert {"value": "AT_RISK"} in story_payload[cfg.fields["health_flags"]]
    # Slip days > 0
    assert story_payload[cfg.fields["slip_days"]] == 2


def test_report_posts_lead_and_project_webhooks(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    gw = FakeGateway(issues={})
    posted: list[tuple[str, str, str | None]] = []

    def mock_post_to_chat(
        webhook: str, text: str, http_post: Any = None, project_key: str | None = None
    ) -> None:
        posted.append((webhook, text, project_key))

    monkeypatch.setattr("mcp_atlassian.eggs.guardian.post_to_chat", mock_post_to_chat)
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    code = main(["report"], gateway=gw)
    assert code == 0

    # Posted to lead webhook and active projects with chat_webhook (RFID and CP)
    webhooks_called = [p[0] for p in posted]
    assert cfg.lead_chat_webhook in webhooks_called
    assert cfg.projects["RFID"].chat_webhook in webhooks_called
    assert cfg.projects["CP"].chat_webhook in webhooks_called


def test_report_continues_on_failure_and_returns_1(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    gw = FakeGateway(issues={})
    posted: list[str] = []

    def mock_post_to_chat(
        webhook: str, text: str, http_post: Any = None, project_key: str | None = None
    ) -> None:
        if project_key == "CP":
            raise RuntimeError("Gửi Google Chat thất bại cho CP (HTTP 500 | HTTPError)")
        posted.append(webhook)

    monkeypatch.setattr("mcp_atlassian.eggs.guardian.post_to_chat", mock_post_to_chat)
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    code = main(["report"], gateway=gw)
    assert code == 1
    # Lead and RFID still posted despite CP failure (1 failing among 3)
    assert cfg.lead_chat_webhook in posted
    assert cfg.projects["RFID"].chat_webhook in posted


def test_report_unknown_project_stderr_and_exit_1(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    gw = FakeGateway(issues={})
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    code = main(["report", "--project", "UNKNOWN"], gateway=gw)
    assert code == 1
    captured = capsys.readouterr()
    assert "Dự án không tồn tại" in captured.err


def test_report_frozen_project_stderr_and_exit_1(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    # Create config with a frozen project
    cfg_data = dataclasses.asdict(cfg)
    cfg_data["projects"]["RFID"]["frozen"] = True
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(cfg_data, allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    gw = FakeGateway(issues={})
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    code = main(["report", "--project", "RFID"], gateway=gw)
    assert code == 1
    captured = capsys.readouterr()
    assert "đang bị đóng băng" in captured.err


def test_remind_continues_on_failure_and_returns_1(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    # In-progress issues missing worklogs for both RFID and CP
    issues = {
        "RFID-1": {
            "key": "RFID-1",
            "fields": {
                "project": {"key": "RFID"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "alice"},
            },
        },
        "CP-1": {
            "key": "CP-1",
            "fields": {
                "project": {"key": "CP"},
                "issuetype": {"name": "Dev Task", "subtask": True},
                "status": {"name": "Dev In Progress"},
                "assignee": {"name": "bob"},
            },
        },
    }
    gw = FakeGateway(issues=issues, worklogs=[])
    posted: list[str] = []

    def mock_post_to_chat(
        webhook: str, text: str, http_post: Any = None, project_key: str | None = None
    ) -> None:
        if project_key == "RFID":
            raise RuntimeError(
                "Gửi Google Chat thất bại cho RFID (HTTP 500 | HTTPError)"
            )
        posted.append(webhook)

    monkeypatch.setattr("mcp_atlassian.eggs.guardian.post_to_chat", mock_post_to_chat)
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    code = main(["remind"], gateway=gw)
    assert code == 1
    # CP and lead still posted despite RFID failure
    assert cfg.projects["CP"].chat_webhook in posted
    assert cfg.lead_chat_webhook in posted


def test_remind_does_not_send_lead_message_when_nothing_missing(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    gw = FakeGateway(issues={}, worklogs=[])
    posted: list[str] = []

    def mock_post_to_chat(
        webhook: str, text: str, http_post: Any = None, project_key: str | None = None
    ) -> None:
        posted.append(webhook)

    monkeypatch.setattr("mcp_atlassian.eggs.guardian.post_to_chat", mock_post_to_chat)
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    code = main(["remind"], gateway=gw)
    assert code == 0
    # Nothing missing -> lead message MUST NOT be sent
    assert len(posted) == 0


def test_remind_lists_workers_without_role(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    # User 'stranger_bob' has worklog in RFID within last 5 working days, but no role in RFID
    worklogs = [
        Worklog(
            issue_key="RFID-1", author="stranger_bob", started=NOW_WED, seconds=3600
        ),
    ]
    # Project roles: only alice in Developers
    gw = FakeGateway(
        issues={},
        worklogs=worklogs,
        role_members={"RFID": {"Developers": ["alice"]}, "CP": {}},
    )
    posted: list[tuple[str, str, str | None]] = []

    def mock_post_to_chat(
        webhook: str, text: str, http_post: Any = None, project_key: str | None = None
    ) -> None:
        posted.append((webhook, text, project_key))

    monkeypatch.setattr("mcp_atlassian.eggs.guardian.post_to_chat", mock_post_to_chat)
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: NOW_WED,
                "combine": datetime.combine,
            },
        ),
    )

    code = main(["remind"], gateway=gw)
    assert code == 0

    # Lead webhook message should mention stranger_bob and RFID, and end with "Gõ `/eggs` để cập nhật."
    lead_messages = [p[1] for p in posted if p[0] == cfg.lead_chat_webhook]
    assert len(lead_messages) == 1
    assert "stranger_bob" in lead_messages[0]
    assert "RFID" in lead_messages[0]
    assert lead_messages[0].strip().endswith("Gõ `/eggs` để cập nhật.")


def test_skip_on_non_working_day(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    # Saturday 2026-11-21
    saturday = datetime(2026, 11, 21, 10, 0, tzinfo=TZ)
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: saturday,
                "combine": datetime.combine,
            },
        ),
    )

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
    summary_rfid = cfg.ops_epic_summary.format(
        month=TODAY_WED.month, year=TODAY_WED.year
    )
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
    # Exact payload must include epic_name customfield_10102 = summary
    assert create_payload["customfield_10102"] == summary_rfid
    assert set(create_payload.keys()) == {
        "project",
        "issuetype",
        "summary",
        "assignee",
        "duedate",
        "customfield_10102",
    }

    # Second run is idempotent: creates nothing
    gw.calls.clear()
    created2 = ensure_ops_epics(gw, cfg, TODAY_WED, cal)
    assert len(created2) == 0
    assert len([c for c in gw.calls if c[0] == "create_issue"]) == 0


def test_ensure_ops_epics_error_prints_stderr_continues_and_returns_1(
    cfg: EggsConfig, cal: WorkCalendar, capsys
):
    from mcp_atlassian.eggs.guardian import ensure_ops_epics

    # Neither RFID nor CP has OPS epic
    gw = FakeGateway(issues={})

    def mock_create_issue(fields: dict) -> str:
        if fields["project"]["key"] == "CP":
            raise JiraError("Jira DC error: Custom field validation failed")
        return f"{fields['project']['key']}-100"

    gw.create_issue = mock_create_issue

    res = ensure_ops_epics(gw, cfg, TODAY_WED, cal)
    assert res == 1

    captured = capsys.readouterr()
    # Prints the Jira error text (not a traceback)
    assert "Jira DC error: Custom field validation failed" in captured.err
    assert "Traceback" not in captured.err

    # Successfully continued with RFID despite CP failure
    assert any("RFID" in msg for msg in res)


def test_guardian_help_without_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.delenv("EGGS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("EGGS_CONFIG", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_resolve_fields_needs_only_jira_url_and_token(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.delenv("EGGS_CONFIG", raising=False)

    gw = FakeGateway(
        field_definitions=[
            {"id": "customfield_10102", "name": "Epic Name"},
            {"id": "customfield_10108", "name": "Planned Start"},
        ]
    )

    code = main(["resolve-fields"], gateway=gw)
    assert code == 0

    captured = capsys.readouterr()
    assert "customfield_10102" in captured.out
    assert "epic_name" in captured.out


def test_missing_env_exits_2(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.delenv("EGGS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("EGGS_CONFIG", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["evaluate"])
    assert exc.value.code == 2


def test_backfill_qc_owner_dry_run_writes_nothing(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {"status": {"name": "QC TO DO"}},
        },
        "RFID-11": {
            "key": "RFID-11",
            "fields": {"status": {"name": "Dev In Progress"}},
        },
    }
    gw = FakeGateway(issues=issues)
    res = backfill_qc_owner(gw, cfg, "RFID", dry_run=True)
    assert res.found == 2
    assert res.written == 0
    assert res.failed == []
    assert res.user == "cuong.huynh"
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) == 0


def test_backfill_qc_owner_default_user_is_triage_owner(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {"status": {"name": "QC TO DO"}},
        },
    }
    gw = FakeGateway(issues=issues)
    res = backfill_qc_owner(gw, cfg, "RFID")
    assert res.found == 1
    assert res.written == 1
    assert res.failed == []
    assert res.user == "cuong.huynh"
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) == 1
    assert update_calls[0][1] == (
        "RFID-10",
        {"customfield_10702": {"name": "cuong.huynh"}},
    )
    assert update_calls[0][2] == {"notify_users": False}


def test_backfill_qc_owner_qc_owner_flag_overrides(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {"status": {"name": "QC TO DO"}},
        },
    }
    gw = FakeGateway(issues=issues)
    res = backfill_qc_owner(gw, cfg, "RFID", qc_owner="override.qc")
    assert res.user == "override.qc"
    assert res.written == 1
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert update_calls[0][1] == (
        "RFID-10",
        {"customfield_10702": {"name": "override.qc"}},
    )


def test_backfill_qc_owner_first_write_failure_stops(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-10": {"key": "RFID-10", "fields": {"status": {"name": "Open"}}},
        "RFID-11": {"key": "RFID-11", "fields": {"status": {"name": "Open"}}},
    }
    gw = FakeGateway(issues=issues)

    def fail_first(key: str, fields: dict, *, notify_users: bool = True) -> None:
        raise JiraError("User does not exist")

    gw.update_fields = fail_first

    with pytest.raises(JiraError) as exc_info:
        backfill_qc_owner(gw, cfg, "RFID")
    assert "User does not exist" in str(exc_info.value)


def test_backfill_qc_owner_later_failure_continues_and_is_reported(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-10": {"key": "RFID-10", "fields": {"status": {"name": "Open"}}},
        "RFID-11": {"key": "RFID-11", "fields": {"status": {"name": "Open"}}},
        "RFID-12": {"key": "RFID-12", "fields": {"status": {"name": "Open"}}},
    }
    gw = FakeGateway(issues=issues)
    attempts: list[str] = []

    def fail_second(key: str, fields: dict, *, notify_users: bool = True) -> None:
        attempts.append(key)
        if key == "RFID-11":
            raise JiraError("Field not on screen for RFID-11")

    gw.update_fields = fail_second

    res = backfill_qc_owner(gw, cfg, "RFID")
    assert res.found == 3
    assert res.written == 2
    assert len(res.failed) == 1
    assert res.failed[0][0] == "RFID-11"
    assert "Field not on screen" in res.failed[0][1]
    assert attempts == ["RFID-10", "RFID-11", "RFID-12"]


def test_backfill_qc_owner_missing_config_or_project_exits_2(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    gw = FakeGateway()

    # 1. Project not in config and no --qc-owner
    with pytest.raises(SystemExit) as exc1:
        main(["backfill-qc-owner", "--project", "UNKNOWN_PROJ"], gateway=gw)
    assert exc1.value.code == 2
    assert len(gw.calls) == 0

    # 2. Config has no qc_owner field
    cfg_no_qc = dataclasses.replace(
        cfg, fields={k: v for k, v in cfg.fields.items() if k != "qc_owner"}
    )
    config_file.write_text(
        yaml.safe_dump(dataclasses.asdict(cfg_no_qc), allow_unicode=True)
    )
    with pytest.raises(SystemExit) as exc2:
        main(["backfill-qc-owner", "--project", "RFID"], gateway=gw)
    assert exc2.value.code == 2
    assert len(gw.calls) == 0


def test_backfill_qc_owner_runs_on_non_working_day(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    # Saturday 2026-11-21
    saturday = datetime(2026, 11, 21, 10, 0, tzinfo=TZ)
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: saturday,
                "combine": datetime.combine,
            },
        ),
    )

    gw = FakeGateway(
        issues={
            "RFID-10": {"key": "RFID-10", "fields": {"status": {"name": "QC TO DO"}}}
        }
    )

    code = main(["backfill-qc-owner", "--project", "RFID", "--dry-run"], gateway=gw)
    assert code == 0
    search_calls = [c for c in gw.calls if c[0] == "search"]
    assert len(search_calls) == 1


def test_backfill_qc_owner_jql_contains_no_cf_and_no_is_empty(
    cfg: EggsConfig,
):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    gw = FakeGateway()
    backfill_qc_owner(gw, cfg, "RFID", dry_run=True)

    search_calls = [c for c in gw.calls if c[0] == "search"]
    assert len(search_calls) == 1
    jql = search_calls[0][1][0]
    fields = search_calls[0][1][1]

    assert "status" in fields
    assert "issuetype" in fields
    assert "customfield_10702" in fields
    assert "project = RFID" in jql
    assert "statusCategory != Done" in jql
    assert "cf[" not in jql
    assert "is EMPTY" not in jql


def test_backfill_qc_owner_skips_issue_when_qc_owner_already_set(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "issuetype": {"name": "Bug stand-alone"},
                "status": {"name": "QC TO DO"},
                "customfield_10702": {"name": "existing.qc"},
            },
        },
        "RFID-11": {
            "key": "RFID-11",
            "fields": {
                "issuetype": {"name": "Bug stand-alone"},
                "status": {"name": "QC TO DO"},
                "customfield_10702": None,
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = backfill_qc_owner(gw, cfg, "RFID")
    assert res.found == 1
    assert res.written == 1
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) == 1
    assert update_calls[0][1][0] == "RFID-11"


def test_backfill_qc_owner_adhoc_status_filter(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-20": {
            "key": "RFID-20",
            "fields": {
                "issuetype": {"name": "Ad-hoc Task"},
                "status": {"name": "Dev In Progress"},
                "customfield_10702": None,
            },
        },
        "RFID-21": {
            "key": "RFID-21",
            "fields": {
                "issuetype": {"name": "Ad-hoc Task"},
                "status": {"name": "QC TO DO"},
                "customfield_10702": None,
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = backfill_qc_owner(gw, cfg, "RFID")
    assert res.found == 1
    assert res.written == 1
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) == 1
    assert update_calls[0][1][0] == "RFID-21"


def test_backfill_qc_owner_story_filled(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-30": {
            "key": "RFID-30",
            "fields": {
                "issuetype": {"name": "Story"},
                "status": {"name": "Dev In Progress"},
                "customfield_10702": None,
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = backfill_qc_owner(gw, cfg, "RFID")
    assert res.found == 1
    assert res.written == 1
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) == 1
    assert update_calls[0][1][0] == "RFID-30"


def test_backfill_qc_owner_types_bug_only_touches_bug_standalone(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import backfill_qc_owner

    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "issuetype": {"name": "Bug stand-alone"},
                "status": {"name": "Open"},
                "customfield_10702": None,
            },
        },
        "RFID-20": {
            "key": "RFID-20",
            "fields": {
                "issuetype": {"name": "Story"},
                "status": {"name": "Open"},
                "customfield_10702": None,
            },
        },
        "RFID-30": {
            "key": "RFID-30",
            "fields": {
                "issuetype": {"name": "Ad-hoc Task"},
                "status": {"name": "QC TO DO"},
                "customfield_10702": None,
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = backfill_qc_owner(gw, cfg, "RFID", types=["bug"])
    assert res.found == 1
    assert res.written == 1
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) == 1
    assert update_calls[0][1][0] == "RFID-10"


def test_backfill_qc_owner_search_error_reported_separately(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    gw = FakeGateway()

    def fail_search(jql, fields, expand_changelog: bool = False):  # noqa: FBT001, FBT002
        raise JiraError("JQL syntax error or Jira down")

    gw.search = fail_search

    code = main(["backfill-qc-owner", "--project", "RFID"], gateway=gw)
    assert code == 1
    captured = capsys.readouterr()
    assert "Lỗi khi tìm kiếm issues" in captured.err
    assert "Lỗi khi cập nhật issue đầu tiên" not in captured.err


def test_freeze_legacy_skips_story_with_recently_updated_subtask(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import freeze_legacy

    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "issuetype": {"name": "Story"},
                "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
                "updated": "2026-09-01T10:00:00.000+0700",
                "labels": [],
            },
        },
        "RFID-11": {
            "key": "RFID-11",
            "fields": {
                "issuetype": {"name": "Sub-task"},
                "parent": {"key": "RFID-10"},
                "status": {"name": "Dev In Progress"},
                "updated": "2026-11-05T10:00:00.000+0700",
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = freeze_legacy(gw, cfg, "RFID", days=30, now=now)
    assert res.found == 0
    assert res.written == 0
    add_calls = [c for c in gw.calls if c[0] == "add_label"]
    assert len(add_calls) == 0


def test_freeze_legacy_skips_epic_with_recently_updated_child(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import freeze_legacy

    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-1": {
            "key": "RFID-1",
            "fields": {
                "issuetype": {"name": "Epic"},
                "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
                "updated": "2026-09-01T10:00:00.000+0700",
                "labels": [],
            },
        },
        "RFID-2": {
            "key": "RFID-2",
            "fields": {
                "issuetype": {"name": "Story"},
                "customfield_10100": "RFID-1",
                "status": {"name": "Dev In Progress"},
                "updated": "2026-11-05T10:00:00.000+0700",
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = freeze_legacy(gw, cfg, "RFID", days=30, now=now)
    assert res.found == 0
    assert res.written == 0
    add_calls = [c for c in gw.calls if c[0] == "add_label"]
    assert len(add_calls) == 0


def test_freeze_legacy_dry_run_writes_nothing(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import freeze_legacy

    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "issuetype": {"name": "Story"},
                "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
                "updated": "2026-09-01T10:00:00.000+0700",
                "labels": [],
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = freeze_legacy(gw, cfg, "RFID", days=30, now=now, dry_run=True)
    assert res.found == 1
    assert res.written == 0
    add_calls = [c for c in gw.calls if c[0] == "add_label"]
    assert len(add_calls) == 0


def test_freeze_legacy_real_run_adds_label(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import freeze_legacy

    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "issuetype": {"name": "Bug stand-alone"},
                "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
                "updated": "2026-09-01T10:00:00.000+0700",
                "labels": [],
            },
        },
    }
    gw = FakeGateway(issues=issues)
    res = freeze_legacy(gw, cfg, "RFID", days=30, now=now)
    assert res.found == 1
    assert res.written == 1
    add_calls = [c for c in gw.calls if c[0] == "add_label"]
    assert len(add_calls) == 1
    assert add_calls[0][1] == ("RFID-10", "legacy-frozen")
    assert add_calls[0][2] == {"notify_users": False}
    assert "legacy-frozen" in gw.issues["RFID-10"]["fields"]["labels"]


def test_unfreeze_on_human_worklog(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import evaluate

    cal = WorkCalendar(cfg.holidays)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "summary": "Stale story",
                "issuetype": {"name": "Story"},
                "status": {"name": "DEV To Do"},
                "labels": ["legacy-frozen"],
                "updated": "2026-11-10T09:30:00.000+0700",
                "worklog": {
                    "worklogs": [
                        {
                            "author": {"name": "alice"},
                            "created": "2026-11-10T09:25:00.000+0700",
                            "started": "2026-11-10T09:00:00.000+0700",
                            "timeSpentSeconds": 1800,
                        }
                    ]
                },
            },
            "changelog": {
                "histories": [
                    {
                        "created": "2026-11-09T08:00:00.000+0700",
                        "author": {"name": "bot"},
                        "items": [
                            {
                                "field": "labels",
                                "fromString": "",
                                "toString": "legacy-frozen",
                            }
                        ],
                    }
                ]
            },
        }
    }
    gw = FakeGateway(issues=issues, user="bot")
    evaluate(gw, cfg, now, cal)
    remove_calls = [c for c in gw.calls if c[0] == "remove_label"]
    assert len(remove_calls) == 1
    assert remove_calls[0][1] == ("RFID-10", "legacy-frozen")
    assert remove_calls[0][2] == {"notify_users": False}


def test_unfreeze_on_human_comment(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import evaluate

    cal = WorkCalendar(cfg.holidays)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "summary": "Stale story",
                "issuetype": {"name": "Story"},
                "status": {"name": "DEV To Do"},
                "labels": ["legacy-frozen"],
                "updated": "2026-11-10T09:30:00.000+0700",
                "comment": {
                    "comments": [
                        {
                            "author": {"name": "bob"},
                            "created": "2026-11-10T09:20:00.000+0700",
                            "body": "Resuming work on this issue.",
                        }
                    ]
                },
            },
            "changelog": {
                "histories": [
                    {
                        "created": "2026-11-09T08:00:00.000+0700",
                        "author": {"name": "bot"},
                        "items": [
                            {
                                "field": "labels",
                                "fromString": "",
                                "toString": "legacy-frozen",
                            }
                        ],
                    }
                ]
            },
        }
    }
    gw = FakeGateway(issues=issues, user="bot")
    evaluate(gw, cfg, now, cal)
    remove_calls = [c for c in gw.calls if c[0] == "remove_label"]
    assert len(remove_calls) == 1
    assert remove_calls[0][1] == ("RFID-10", "legacy-frozen")


def test_unfreeze_on_human_transition(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import evaluate

    cal = WorkCalendar(cfg.holidays)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "summary": "Stale story",
                "issuetype": {"name": "Story"},
                "status": {"name": "Dev In Progress"},
                "labels": ["legacy-frozen"],
                "updated": "2026-11-10T09:30:00.000+0700",
            },
            "changelog": {
                "histories": [
                    {
                        "created": "2026-11-09T08:00:00.000+0700",
                        "author": {"name": "bot"},
                        "items": [
                            {
                                "field": "labels",
                                "fromString": "",
                                "toString": "legacy-frozen",
                            }
                        ],
                    },
                    {
                        "created": "2026-11-10T09:15:00.000+0700",
                        "author": {"name": "charlie"},
                        "items": [
                            {
                                "field": "status",
                                "fromString": "DEV To Do",
                                "toString": "Dev In Progress",
                            }
                        ],
                    },
                ]
            },
        }
    }
    gw = FakeGateway(issues=issues, user="bot")
    evaluate(gw, cfg, now, cal)
    remove_calls = [c for c in gw.calls if c[0] == "remove_label"]
    assert len(remove_calls) == 1
    assert remove_calls[0][1] == ("RFID-10", "legacy-frozen")


def test_no_unfreeze_on_bot_entry(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import evaluate

    cal = WorkCalendar(cfg.holidays)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "summary": "Stale story",
                "issuetype": {"name": "Story"},
                "status": {"name": "DEV To Do"},
                "labels": ["legacy-frozen"],
                "updated": "2026-11-10T09:30:00.000+0700",
                "comment": {
                    "comments": [
                        {
                            "author": {"name": "bot"},
                            "created": "2026-11-10T09:20:00.000+0700",
                            "body": "Automated evaluation note.",
                        }
                    ]
                },
            },
            "changelog": {
                "histories": [
                    {
                        "created": "2026-11-09T08:00:00.000+0700",
                        "author": {"name": "bot"},
                        "items": [
                            {
                                "field": "labels",
                                "fromString": "",
                                "toString": "legacy-frozen",
                            }
                        ],
                    }
                ]
            },
        }
    }
    gw = FakeGateway(issues=issues, user="bot")
    evaluate(gw, cfg, now, cal)
    remove_calls = [c for c in gw.calls if c[0] == "remove_label"]
    assert len(remove_calls) == 0


def test_no_unfreeze_on_workflow_history_item(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import evaluate

    cal = WorkCalendar(cfg.holidays)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "summary": "Stale story",
                "issuetype": {"name": "Story"},
                "status": {"name": "DEV To Do"},
                "labels": ["legacy-frozen"],
                "updated": "2026-11-10T09:30:00.000+0700",
            },
            "changelog": {
                "histories": [
                    {
                        "created": "2026-11-09T08:00:00.000+0700",
                        "author": {"name": "bot"},
                        "items": [
                            {
                                "field": "labels",
                                "fromString": "",
                                "toString": "legacy-frozen",
                            }
                        ],
                    },
                    {
                        "created": "2026-11-10T09:15:00.000+0700",
                        "author": {"name": "migration_admin"},
                        "items": [
                            {
                                "field": "Workflow",
                                "fromString": "Old WF",
                                "toString": "EGGS WF",
                            }
                        ],
                    },
                ]
            },
        }
    }
    gw = FakeGateway(issues=issues, user="bot")
    evaluate(gw, cfg, now, cal)
    remove_calls = [c for c in gw.calls if c[0] == "remove_label"]
    assert len(remove_calls) == 0


def test_no_unfreeze_for_activity_before_freeze_entry(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import evaluate

    cal = WorkCalendar(cfg.holidays)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "summary": "Stale story",
                "issuetype": {"name": "Story"},
                "status": {"name": "DEV To Do"},
                "labels": ["legacy-frozen"],
                "updated": "2026-11-10T09:30:00.000+0700",
                "comment": {
                    "comments": [
                        {
                            "author": {"name": "human"},
                            "created": "2026-11-08T09:00:00.000+0700",
                            "body": "Comment before freeze.",
                        }
                    ]
                },
            },
            "changelog": {
                "histories": [
                    {
                        "created": "2026-11-09T08:00:00.000+0700",
                        "author": {"name": "bot"},
                        "items": [
                            {
                                "field": "labels",
                                "fromString": "",
                                "toString": "legacy-frozen",
                            }
                        ],
                    }
                ]
            },
        }
    }
    gw = FakeGateway(issues=issues, user="bot")
    evaluate(gw, cfg, now, cal)
    remove_calls = [c for c in gw.calls if c[0] == "remove_label"]
    assert len(remove_calls) == 0


def test_freeze_legacy_runs_on_non_working_day(
    cfg: EggsConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    config_file = tmp_path / "eggs.yaml"
    config_file.write_text(yaml.safe_dump(dataclasses.asdict(cfg), allow_unicode=True))

    monkeypatch.setenv("JIRA_URL", cfg.jira_url)
    monkeypatch.setenv("EGGS_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("EGGS_CONFIG", str(config_file))

    saturday = datetime(2026, 11, 21, 10, 0, tzinfo=TZ)
    monkeypatch.setattr(
        "mcp_atlassian.eggs.guardian.datetime",
        type(
            "MockDT",
            (),
            {
                "now": lambda tz=None: saturday,
                "combine": datetime.combine,
                "fromisoformat": datetime.fromisoformat,
            },
        ),
    )

    gw = FakeGateway(
        issues={
            "RFID-10": {
                "key": "RFID-10",
                "fields": {
                    "issuetype": {"name": "Bug stand-alone"},
                    "status": {"name": "Open"},
                    "updated": "2026-09-01T10:00:00.000+0700",
                    "labels": [],
                },
            }
        }
    )

    code = main(["freeze-legacy", "--project", "RFID", "--dry-run"], gateway=gw)
    assert code == 0
    search_calls = [c for c in gw.calls if c[0] == "search"]
    assert len(search_calls) >= 1


def test_unfreeze_evaluates_health_normally(cfg: EggsConfig):
    from mcp_atlassian.eggs.guardian import evaluate

    cal = WorkCalendar(cfg.holidays)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
    issues = {
        "RFID-10": {
            "key": "RFID-10",
            "fields": {
                "summary": "Stale story",
                "issuetype": {"name": "Story"},
                "status": {"name": "Dev In Progress"},
                "assignee": None,
                "labels": ["legacy-frozen"],
                "updated": "2026-11-10T09:30:00.000+0700",
                "comment": {
                    "comments": [
                        {
                            "author": {"name": "human"},
                            "created": "2026-11-10T09:20:00.000+0700",
                            "body": "Starting work on this.",
                        }
                    ]
                },
            },
            "changelog": {
                "histories": [
                    {
                        "created": "2026-11-09T08:00:00.000+0700",
                        "author": {"name": "bot"},
                        "items": [
                            {
                                "field": "labels",
                                "fromString": "",
                                "toString": "legacy-frozen",
                            }
                        ],
                    }
                ]
            },
        }
    }
    gw = FakeGateway(issues=issues, user="bot")
    evaluate(gw, cfg, now, cal)
    remove_calls = [c for c in gw.calls if c[0] == "remove_label"]
    assert len(remove_calls) == 1
    update_calls = [c for c in gw.calls if c[0] == "update_fields"]
    assert len(update_calls) >= 1
