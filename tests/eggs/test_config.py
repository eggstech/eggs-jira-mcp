from datetime import date
from pathlib import Path
import pytest
from mcp_atlassian.eggs.config import (
    EggsConfig,
    ProjectConfig,
    Thresholds,
    load_config,
)


def test_load_config_expands_env_and_maps_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK", "https://chat.example/x")
    yaml_content = """
lead_user: cuong.huynh
lead_chat_webhook: ${HOOK}
kit_min_version: 0.1.0
thresholds:
  stale_working_days: 2
  epic_unscheduled_window: 5
  interrupt_window: 5
  escalate_l1_after: 1
  escalate_l2_after: 2
  internal_block_escalate_after: 2
holidays:
  - "2026-01-01"
  - "2026-04-30"
  - "2026-05-01"
  - "2026-09-02"
fields:
  planned_start: customfield_10108
  planned_end: customfield_10109
statuses:
  ready_for_qc: QC TO DO
  in_progress: Dev In Progress
  todo: DEV To Do
issue_types:
  epic:
    - Epic
  story:
    - Story
  tech_task:
    - Tech Task
    - Ad-hoc Task
  bug:
    - Bug
    - Bug stand-alone
  flow:
    - Flow
    - Dev Task
  flow_bug:
    - Flow Bug
    - Bug
  tech_subtask:
    - Tech Sub-task
    - Sub-task
transitions:
  schedule: Schedule
  start: Start
projects:
  HMKLOYAL:
    key: HMKLOYAL
    default_lead_ba: cuong.huynh
    chat_webhook: ${HOOK}/hmkloyal
    frozen: false
  BUL:
    key: BUL
    default_lead_ba: cuong.huynh
    chat_webhook: null
    frozen: true
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    cfg = load_config(str(cfg_file))
    assert cfg.lead_chat_webhook == "https://chat.example/x"
    assert cfg.status_logical("QC TO DO") == "ready_for_qc"
    assert cfg.type_logical("Bug", is_subtask=True) == "flow_bug"
    assert cfg.type_logical("Bug", is_subtask=False) == "bug"
    assert cfg.type_logical("Bug stand-alone", is_subtask=False) == "bug"
    assert cfg.type_logical("Sub-task", is_subtask=True) == "tech_subtask"
    assert cfg.type_logical("Dev Task", is_subtask=True) == "flow"
    assert cfg.type_logical("Dev Task", is_subtask=False) is None
    assert cfg.type_logical("Ad-hoc Task", is_subtask=False) == "tech_task"
    assert cfg.projects["HMKLOYAL"].chat_webhook == "https://chat.example/x/hmkloyal"
    assert cfg.projects["BUL"].frozen is True
    assert date(2026, 1, 1) in cfg.holidays


def test_load_example_config():
    # Verify that config.example.yaml loads cleanly and has all 27 projects
    example_path = Path(__file__).parent.parent.parent / "src" / "mcp_atlassian" / "eggs" / "config.example.yaml"
    cfg = load_config(str(example_path))
    assert cfg.lead_user == "cuong.huynh"
    assert len(cfg.projects) == 27
    for frozen_key in ["BUL", "HUE", "HMKCHAT", "SUP", "KP", "MAP", "MEW", "RS", "SL", "UW", "VL", "ZBS"]:
        assert cfg.projects[frozen_key].frozen is True, f"{frozen_key} should be frozen"
    for active_key in ["ROCKY", "HMKPOS", "HMKLOYAL", "HH", "LPPOS", "RFID", "CP", "CRM", "EGG", "AILA", "FNB", "HMKAI", "AEON", "GSH", "RIV"]:
        assert cfg.projects[active_key].frozen is False, f"{active_key} should not be frozen"
    assert cfg.fields["planned_start"] == "customfield_10108"
    assert cfg.fields["planned_end"] == "customfield_10109"
    assert cfg.timezone == "Asia/Ho_Chi_Minh"
    assert cfg.frozen_label == "legacy-frozen"
    assert cfg.flagged_impediment_value == "Impediment"
    assert "epic_link" in cfg.fields
    assert "flagged" in cfg.fields


def test_load_config_unresolved_env_var_raises(tmp_path):
    yaml_content = """
lead_user: cuong.huynh
lead_chat_webhook: ${UNSET_HOOK_VAR}
projects:
  PROJ:
    default_lead_ba: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with pytest.raises(ValueError, match="UNSET_HOOK_VAR"):
        load_config(str(cfg_file))


def test_load_config_from_eggs_config_env(tmp_path, monkeypatch):
    yaml_content = """
lead_user: cuong.huynh
projects:
  PROJ:
    default_lead_ba: cuong.huynh
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    monkeypatch.setenv("EGGS_CONFIG", str(cfg_file))
    cfg = load_config(None)
    assert cfg.lead_user == "cuong.huynh"
    assert "PROJ" in cfg.projects


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
projects:
  PROJ:
    key: PROJ
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)

    with pytest.raises(ValueError, match="default_lead_ba"):
        load_config(str(cfg_file))


