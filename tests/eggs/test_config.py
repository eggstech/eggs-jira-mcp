from datetime import date
from pathlib import Path

import pytest

from mcp_atlassian.eggs.config import (
    EggsConfig,
    load_config,
)


def test_load_config_expands_env_and_maps_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK", "https://chat.example/x")
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
lead_chat_webhook: ${HOOK}
kit_min_version: 0.1.0
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
thresholds:
  stale_working_days: 2
  unscheduled_window: 5
  interrupt_window: 5
  escalate_l1_after: 1
  escalate_l2_after: 2
  internal_block_escalate_after: 2
  qc_flow_buffer: 1
holidays:
  - "2026-01-01"
  - "2026-04-30"
  - "2026-05-01"
  - "2026-09-02"
fields:
  planned_start: customfield_10108
  planned_end: customfield_10109
  part: customfield_part
  qc_owner: customfield_qc_owner
  unscheduled_count: customfield_unscheduled_count
statuses:
  open: Open
  analyzing: Analyzing
  ready_for_dev: Ready For Dev
  todo: DEV To Do
  in_progress: Dev In Progress
  ready_for_qc: QC TO DO
  testing: Test In Progress
  ready_for_uat: Ready For UAT
  client_uat: Wait For Review
  done: Done
  cancelled: Cancelled
issue_types:
  epic:
    - Epic
  story:
    - Story
  dev_task:
    - Dev Task
  bug_sub:
    - Bug
  adhoc:
    - Ad-hoc Task
  bug:
    - Bug stand-alone
transitions:
  schedule: Schedule
  start: Start
projects:
  HMKLOYAL:
    key: HMKLOYAL
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
    chat_webhook: ${HOOK}/hmkloyal
    frozen: false
  BUL:
    key: BUL
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
    chat_webhook: null
    frozen: true
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    cfg = load_config(str(cfg_file))
    assert cfg.lead_chat_webhook == "https://chat.example/x"
    assert cfg.status_logical("QC TO DO") == "ready_for_qc"
    assert cfg.status_logical("Ready For Dev") == "ready_for_dev"
    assert cfg.status_logical("Wait For Review") == "client_uat"
    assert cfg.type_logical("Bug", is_subtask=True) == "bug_sub"
    assert cfg.type_logical("Bug", is_subtask=False) is None
    assert cfg.type_logical("Bug stand-alone", is_subtask=False) == "bug"
    assert cfg.type_logical("Sub-task", is_subtask=True) is None
    assert cfg.type_logical("Dev Task", is_subtask=True) == "dev_task"
    assert cfg.type_logical("Dev Task", is_subtask=False) is None
    assert cfg.type_logical("Ad-hoc Task", is_subtask=False) == "adhoc"
    assert cfg.projects["HMKLOYAL"].chat_webhook == "https://chat.example/x/hmkloyal"
    assert cfg.projects["BUL"].frozen is True
    assert date(2026, 1, 1) in cfg.holidays
    assert cfg.thresholds.qc_flow_buffer == 1
    assert cfg.thresholds.unscheduled_window == 5
    assert cfg.thresholds.unfreeze_window_days == 4


@pytest.mark.parametrize(
    ("jira_name", "is_subtask", "expected"),
    [
        ("Epic", False, "epic"),
        ("Story", False, "story"),
        ("Dev Task", True, "dev_task"),
        ("Dev Task", False, None),
        ("Bug", True, "bug_sub"),
        ("Bug", False, None),
        ("Bug stand-alone", False, "bug"),
        ("Bug stand-alone", True, None),
        ("Ad-hoc Task", False, "adhoc"),
        ("Ad-hoc Task", True, None),
        ("User Report", False, "report"),
        ("User Report", True, None),
        # Retired types -> None
        ("Testing Task", True, None),
        ("Testing Task", False, None),
        ("Sub-task", True, None),
        ("Sub-task", False, None),
        ("Improvement", False, None),
        ("Task", False, None),
        ("New Feature", False, None),
        ("Unknown Type", False, None),
    ],
)
def test_type_logical_table(jira_name, is_subtask, expected):
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        issue_types={
            "epic": ["Epic"],
            "story": ["Story"],
            "dev_task": ["Dev Task"],
            "bug_sub": ["Bug"],
            "adhoc": ["Ad-hoc Task"],
            "bug": ["Bug stand-alone"],
            "report": ["User Report"],
        },
    )
    assert cfg.type_logical(jira_name, is_subtask=is_subtask) == expected


def test_resolution_logical():
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
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
    assert cfg.resolution_logical("Is a Bug") == "bug"
    assert cfg.resolution_logical("User Guidance") == "guidance"
    assert cfg.resolution_logical("Feature Request") == "feature"
    assert cfg.resolution_logical("Data/Config Fix") == "data_fix"
    assert cfg.resolution_logical("Duplicate") == "duplicate"
    assert cfg.resolution_logical("Cannot Reproduce") == "cannot_reproduce"
    assert cfg.resolution_logical("Third-party Issue") == "third_party"
    assert cfg.resolution_logical("Unknown Resolution") is None
    assert cfg.resolution_logical("") is None
    assert cfg.resolution_logical(None) is None


def test_load_example_config():
    # Verify that config.example.yaml loads cleanly and has all 27 projects
    example_path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "mcp_atlassian"
        / "eggs"
        / "config.example.yaml"
    )
    cfg = load_config(str(example_path))
    assert cfg.lead_user == "cuong.huynh"
    assert len(cfg.projects) == 27
    for frozen_key in [
        "BUL",
        "HUE",
        "HMKCHAT",
        "SUP",
        "KP",
        "MAP",
        "MEW",
        "RS",
        "SL",
        "UW",
        "VL",
        "ZBS",
    ]:
        assert cfg.projects[frozen_key].frozen is True, f"{frozen_key} should be frozen"
    for active_key in [
        "ROCKY",
        "HMKPOS",
        "HMKLOYAL",
        "HH",
        "LPPOS",
        "RFID",
        "CP",
        "CRM",
        "EGG",
        "AILA",
        "FNB",
        "HMKAI",
        "AEON",
        "GSH",
        "RIV",
    ]:
        assert cfg.projects[active_key].frozen is False, (
            f"{active_key} should not be frozen"
        )
    assert cfg.fields["planned_start"] == "customfield_10108"
    assert cfg.fields["planned_end"] == "customfield_10109"
    assert cfg.fields["epic_name"] == "customfield_10102"
    assert cfg.timezone == "Asia/Ho_Chi_Minh"
    assert cfg.frozen_label == "legacy-frozen"
    assert cfg.flagged_impediment_value == "Impediment"
    assert "epic_link" in cfg.fields
    assert "flagged" in cfg.fields
    assert cfg.reschedule_pending_value == "Chờ duyệt"
    assert cfg.blocked_internal_value == "Nội bộ"


def test_load_config_custom_pending_and_internal_values(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
reschedule_pending_value: "Custom Pending"
blocked_internal_value: "Custom Internal"
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = load_config(str(cfg_file))
    assert cfg.reschedule_pending_value == "Custom Pending"
    assert cfg.blocked_internal_value == "Custom Internal"


def test_load_config_roles_and_end_day_hour(tmp_path):
    # Default roles and end_day_hour
    cfg_default = EggsConfig(
        lead_user="cuong.huynh", jira_url="https://jira.eggstech.io.vn"
    )
    assert cfg_default.thresholds.end_day_hour == 17
    assert cfg_default.roles == {
        "lead_ba": ["Business Analysts", "Administrators"],
        "qc": ["Testers"],
        "dev": ["Developers"],
    }

    # Custom roles and end_day_hour loaded from YAML
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
thresholds:
  end_day_hour: 18
roles:
  lead_ba:
    - "Lead BA Custom"
  qc:
    - "Quality Control"
  dev:
    - "Software Engineers"
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = load_config(str(cfg_file))
    assert cfg.thresholds.end_day_hour == 18
    assert cfg.roles["lead_ba"] == ["Lead BA Custom"]
    assert cfg.roles["qc"] == ["Quality Control"]
    assert cfg.roles["dev"] == ["Software Engineers"]


def test_load_config_legacy_epic_unscheduled_window_shim_removed(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
thresholds:
  epic_unscheduled_window: 10
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = load_config(str(cfg_file))
    # Since legacy shim is removed, unscheduled_window defaults to 5, ignoring epic_unscheduled_window
    assert cfg.thresholds.unscheduled_window == 5


def test_load_config_unresolved_env_var_raises(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
lead_chat_webhook: ${UNSET_HOOK_VAR}
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with pytest.raises(ValueError, match="UNSET_HOOK_VAR"):
        load_config(str(cfg_file))


def test_load_config_from_eggs_config_env(tmp_path, monkeypatch):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    monkeypatch.setenv("EGGS_CONFIG", str(cfg_file))
    cfg = load_config(None)
    assert cfg.lead_user == "cuong.huynh"
    assert "PROJ" in cfg.projects


def test_load_config_logs_warning_for_non_standard_link_types(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    import logging

    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "Interrupts"
  causes: "causes"  # Lowercase / non-standard!
  relates: "Relates"
  duplicates: "Duplicate"
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(cfg_file))

    assert cfg.link_types["causes"] == "causes"
    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "link_types" in r.message
        and "causes" in r.message
    ]
    assert len(warning_records) >= 1


def test_load_config_requires_lead_user(tmp_path):
    yaml_content = """
projects:
  PROJ:
    default_lead_ba: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with pytest.raises(ValueError, match="lead_user"):
        load_config(str(cfg_file))


def test_load_config_requires_project_default_lead_ba(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
projects:
  PROJ:
    key: PROJ
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with pytest.raises(ValueError, match="default_lead_ba"):
        load_config(str(cfg_file))


def test_load_config_requires_jira_url(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with pytest.raises(ValueError, match="jira_url"):
        load_config(str(cfg_file))


def test_load_config_requires_project_triage_owner(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
projects:
  PROJ:
    default_lead_ba: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with pytest.raises(ValueError, match="triage_owner"):
        load_config(str(cfg_file))


def test_load_config_requires_link_types(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with pytest.raises(ValueError, match="link_types"):
        load_config(str(cfg_file))


def test_load_config_defaults_for_new_fields(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    cfg = load_config(str(cfg_file))
    assert cfg.reschedule_interrupt_value == "Chen ngang"
    assert cfg.ops_epic_summary == "[OPS] Vận hành & Hotfix {month:02d}/{year}"
    assert cfg.hotfix_prefix == "[HOTFIX]"
    assert cfg.bot_user == "eggsbot"
    assert cfg.roles["lead_ba"] == ["Business Analysts", "Administrators"]
    assert cfg.roles["qc"] == ["Testers"]
    assert cfg.roles["dev"] == ["Developers"]
    assert cfg.fields["epic_name"] == "customfield_10102"


def test_webhook_repr_redaction():
    from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig

    proj = ProjectConfig(
        key="PROJ",
        default_lead_ba="ba",
        triage_owner="qc",
        chat_webhook="https://chat.googleapis.com/secret_proj_hook",
    )
    assert "secret_proj_hook" not in repr(proj)
    assert proj.chat_webhook == "https://chat.googleapis.com/secret_proj_hook"

    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        lead_chat_webhook="https://chat.googleapis.com/secret_lead_hook",
    )
    assert "secret_lead_hook" not in repr(cfg)
    assert cfg.lead_chat_webhook == "https://chat.googleapis.com/secret_lead_hook"


def test_load_config_fields_none_dropped_keeps_default_fields(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
jira_url: https://jira.eggstech.io.vn
link_types:
  interrupts: "is interrupted by"
  causes: "causes"
  relates: "relates to"
  duplicates: "duplicates"
fields:
  epic_name: null
  planned_start: customfield_99999
projects:
  PROJ:
    default_lead_ba: cuong.huynh
    triage_owner: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    cfg = load_config(str(cfg_file))
    assert cfg.fields["epic_name"] == "customfield_10102"
    assert cfg.fields["planned_start"] == "customfield_99999"
