from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import os
from pathlib import Path
import re
from typing import Any
import yaml


@dataclass(frozen=True)
class Thresholds:
    stale_working_days: int = 2
    epic_unscheduled_window: int = 5
    interrupt_window: int = 5
    escalate_l1_after: int = 1
    escalate_l2_after: int = 2
    internal_block_escalate_after: int = 2


@dataclass(frozen=True)
class ProjectConfig:
    key: str
    default_lead_ba: str
    chat_webhook: str | None = None
    frozen: bool = False


@dataclass(frozen=True)
class EggsConfig:
    fields: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    issue_types: dict[str, list[str]] = field(default_factory=dict)
    transitions: dict[str, str] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)
    holidays: frozenset[date] = field(default_factory=frozenset)
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    lead_user: str = "cuong.huynh"
    lead_chat_webhook: str | None = None
    kit_min_version: str = "0.1.0"

    def status_logical(self, jira_name: str) -> str | None:
        """Map Jira status name to logical status name."""
        cleaned = jira_name.strip().lower()
        for logical, name in self.statuses.items():
            if name and name.strip().lower() == cleaned:
                return logical
        return None

    def type_logical(self, jira_name: str, is_subtask: bool) -> str | None:
        """Map Jira issue type name and is_subtask to logical issue type."""
        cleaned = jira_name.strip().lower()

        # Prioritize based on is_subtask
        subtask_types = ("flow_bug", "flow", "tech_subtask")
        standard_types = ("epic", "story", "tech_task", "bug")

        priority_order = subtask_types if is_subtask else standard_types
        for logical in priority_order:
            names = self.issue_types.get(logical, [])
            if any(name.strip().lower() == cleaned for name in names):
                return logical

        # Fallback to check other types if not resolved
        secondary_order = standard_types if is_subtask else subtask_types
        for logical in secondary_order:
            names = self.issue_types.get(logical, [])
            if any(name.strip().lower() == cleaned for name in names):
                return logical

        return None


def _expand_env_vars(data: Any) -> Any:
    """Recursively expand ${VAR} and $VAR in string values."""
    if isinstance(data, str):
        # Expand ${VAR} or $VAR using os.environ
        def replace_var(m: re.Match) -> str:
            var_name = m.group(1) or m.group(2)
            return os.getenv(var_name, m.group(0))

        pattern = re.compile(r"\$\{([^}]+)\}|\$([a-zA-Z_][a-zA-Z0-9_]*)")
        return pattern.sub(replace_var, data)
    elif isinstance(data, dict):
        return {k: _expand_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_expand_env_vars(v) for v in data]
    return data


def load_config(path: str | None = None) -> EggsConfig:
    """Load EggsConfig from a YAML file path or EGGS_CONFIG env var."""
    if not path:
        path = os.getenv("EGGS_CONFIG")
    if not path:
        raise ValueError("No configuration path provided and EGGS_CONFIG is not set.")

    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw_text = config_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw_text) or {}
    data = _expand_env_vars(parsed)

    # Parse thresholds
    thresholds_data = data.get("thresholds", {})
    thresholds = Thresholds(
        stale_working_days=thresholds_data.get("stale_working_days", 2),
        epic_unscheduled_window=thresholds_data.get("epic_unscheduled_window", 5),
        interrupt_window=thresholds_data.get("interrupt_window", 5),
        escalate_l1_after=thresholds_data.get("escalate_l1_after", 1),
        escalate_l2_after=thresholds_data.get("escalate_l2_after", 2),
        internal_block_escalate_after=thresholds_data.get("internal_block_escalate_after", 2),
    )

    # Parse holidays
    holidays_raw = data.get("holidays", [])
    holidays_set: set[date] = set()
    for h in holidays_raw:
        if isinstance(h, date):
            holidays_set.add(h)
        elif isinstance(h, str):
            holidays_set.add(date.fromisoformat(h))

    # Parse projects
    projects_data = data.get("projects", {})
    projects: dict[str, ProjectConfig] = {}
    for key, pdata in projects_data.items():
        if isinstance(pdata, dict):
            projects[key] = ProjectConfig(
                key=pdata.get("key", key),
                default_lead_ba=pdata.get("default_lead_ba", "cuong.huynh"),
                chat_webhook=pdata.get("chat_webhook"),
                frozen=bool(pdata.get("frozen", False)),
            )

    return EggsConfig(
        fields=data.get("fields", {}),
        statuses=data.get("statuses", {}),
        issue_types=data.get("issue_types", {}),
        transitions=data.get("transitions", {}),
        thresholds=thresholds,
        holidays=frozenset(holidays_set),
        projects=projects,
        lead_user=data.get("lead_user", "cuong.huynh"),
        lead_chat_webhook=data.get("lead_chat_webhook"),
        kit_min_version=data.get("kit_min_version", "0.1.0"),
    )
