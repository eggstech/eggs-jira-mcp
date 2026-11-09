from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Thresholds:
    stale_working_days: int = 2
    unscheduled_window: int = 5
    interrupt_window: int = 5
    escalate_l1_after: int = 1
    escalate_l2_after: int = 2
    internal_block_escalate_after: int = 2
    qc_flow_buffer: int = 1
    triage_working_days: int = 1
    end_day_hour: int = 17


@dataclass(frozen=True)
class ProjectConfig:
    key: str
    default_lead_ba: str
    triage_owner: str
    chat_webhook: str | None = None
    frozen: bool = False


@dataclass(frozen=True)
class EggsConfig:
    lead_user: str
    jira_url: str
    timezone: str = "Asia/Ho_Chi_Minh"
    frozen_label: str = "legacy-frozen"
    flagged_impediment_value: str = "Impediment"
    reschedule_pending_value: str = "Chờ duyệt"
    reschedule_interrupt_value: str = "Chen ngang"
    blocked_internal_value: str = "Nội bộ"
    blocked_by_values: list[str] = field(
        default_factory=lambda: ["Nội bộ", "Khách hàng", "Bên thứ ba"]
    )
    ops_epic_prefix: str = "[OPS]"
    ops_epic_summary: str = "[OPS] Vận hành & Hotfix {month:02d}/{year}"
    hotfix_prefix: str = "[HOTFIX]"
    bot_user: str = "eggs-bot"
    fields: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    issue_types: dict[str, list[str]] = field(default_factory=dict)
    transitions: dict[str, str] = field(default_factory=dict)
    report_resolutions: dict[str, str] = field(default_factory=dict)
    link_types: dict[str, str] = field(
        default_factory=lambda: {
            "interrupts": "is interrupted by",
            "causes": "causes",
            "relates": "relates to",
            "duplicates": "duplicates",
        }
    )
    thresholds: Thresholds = field(default_factory=Thresholds)
    holidays: frozenset[date] = field(default_factory=frozenset)
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    lead_chat_webhook: str | None = None
    kit_min_version: str = "0.1.0"
    roles: dict[str, list[str]] = field(
        default_factory=lambda: {
            "lead_ba": ["Business Analysts", "Administrators"],
            "qc": ["Testers"],
            "dev": ["Developers"],
        }
    )

    def __post_init__(self) -> None:
        if self.roles:
            normalized = {}
            for k, v in self.roles.items():
                if isinstance(v, list):
                    normalized[k] = [str(x) for x in v]
                elif isinstance(v, (set, tuple)):
                    normalized[k] = [str(x) for x in v]
                elif isinstance(v, str):
                    normalized[k] = [v]
                else:
                    normalized[k] = [str(v)]
            object.__setattr__(self, "roles", normalized)

    def status_logical(self, jira_name: str) -> str | None:
        """Map Jira status name to logical status name."""
        cleaned = jira_name.strip().lower()
        for logical, name in self.statuses.items():
            if name and name.strip().lower() == cleaned:
                return logical
        return None

    def resolution_logical(self, jira_name: str | None) -> str | None:
        """Map Jira resolution name to logical resolution name."""
        if not jira_name:
            return None
        cleaned = jira_name.strip().lower()
        for logical, name in self.report_resolutions.items():
            if name and name.strip().lower() == cleaned:
                return logical
        return None

    def type_logical(self, jira_name: str, *, is_subtask: bool) -> str | None:
        """Map Jira issue type name and is_subtask to logical issue type."""
        cleaned = jira_name.strip().lower()

        # Candidate types strictly match is_subtask in v2
        candidate_types = (
            ("dev_task", "bug_sub")
            if is_subtask
            else ("epic", "story", "adhoc", "bug", "report")
        )
        for logical in candidate_types:
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
            val = os.getenv(var_name)
            if val is None:
                msg = f"Unresolved environment variable: {var_name}"
                raise ValueError(msg)
            return val

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
        msg = f"Config file not found: {path}"
        raise FileNotFoundError(msg)

    raw_text = config_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw_text) or {}
    data = _expand_env_vars(parsed)

    # Parse thresholds
    thresholds_data = data.get("thresholds", {})
    thresholds = Thresholds(
        stale_working_days=thresholds_data.get("stale_working_days", 2),
        unscheduled_window=thresholds_data.get("unscheduled_window", 5),
        interrupt_window=thresholds_data.get("interrupt_window", 5),
        escalate_l1_after=thresholds_data.get("escalate_l1_after", 1),
        escalate_l2_after=thresholds_data.get("escalate_l2_after", 2),
        internal_block_escalate_after=thresholds_data.get(
            "internal_block_escalate_after", 2
        ),
        qc_flow_buffer=thresholds_data.get("qc_flow_buffer", 1),
        triage_working_days=thresholds_data.get("triage_working_days", 1),
        end_day_hour=thresholds_data.get("end_day_hour", 17),
    )

    # Parse holidays
    holidays_raw = data.get("holidays", [])
    holidays_set: set[date] = set()
    for h in holidays_raw:
        if isinstance(h, date):
            holidays_set.add(h)
        elif isinstance(h, str):
            holidays_set.add(date.fromisoformat(h))

    if "lead_user" not in data or not data["lead_user"]:
        raise ValueError("Missing required configuration key: 'lead_user'")

    if "jira_url" not in data or not data["jira_url"]:
        raise ValueError("Missing required configuration key: 'jira_url'")

    link_types_data = data.get("link_types")
    if not isinstance(link_types_data, dict):
        raise ValueError("Missing required configuration key: 'link_types'")
    required_links = {"interrupts", "causes", "relates", "duplicates"}
    missing_links = required_links - set(link_types_data.keys())
    if missing_links:
        msg = f"Missing required link types in 'link_types': {sorted(missing_links)}"
        raise ValueError(msg)

    # Parse projects
    projects_data = data.get("projects", {})
    projects: dict[str, ProjectConfig] = {}
    for key, pdata in projects_data.items():
        if isinstance(pdata, dict):
            if "default_lead_ba" not in pdata or not pdata["default_lead_ba"]:
                msg = f"Missing required 'default_lead_ba' for project '{key}'"
                raise ValueError(msg)
            if "triage_owner" not in pdata or not pdata["triage_owner"]:
                msg = f"Missing required 'triage_owner' for project '{key}'"
                raise ValueError(msg)
            frozen = bool(pdata.get("frozen", False))
            projects[key] = ProjectConfig(
                key=pdata.get("key", key),
                default_lead_ba=pdata["default_lead_ba"],
                triage_owner=pdata["triage_owner"],
                chat_webhook=pdata.get("chat_webhook"),
                frozen=frozen,
            )

    roles_data = data.get("roles")
    if roles_data and isinstance(roles_data, dict):
        normalized_roles = {}
        for r_k, r_v in roles_data.items():
            if isinstance(r_v, list):
                normalized_roles[r_k] = [str(x) for x in r_v]
            elif isinstance(r_v, str):
                normalized_roles[r_k] = [r_v]
            else:
                normalized_roles[r_k] = [str(r_v)]
    else:
        normalized_roles = {
            "lead_ba": ["Business Analysts", "Administrators"],
            "qc": ["Testers"],
            "dev": ["Developers"],
        }

    return EggsConfig(
        lead_user=data["lead_user"],
        jira_url=data["jira_url"],
        timezone=data.get("timezone", "Asia/Ho_Chi_Minh"),
        frozen_label=data.get("frozen_label", "legacy-frozen"),
        flagged_impediment_value=data.get("flagged_impediment_value", "Impediment"),
        reschedule_pending_value=data.get("reschedule_pending_value", "Chờ duyệt"),
        reschedule_interrupt_value=data.get("reschedule_interrupt_value", "Chen ngang"),
        blocked_internal_value=data.get("blocked_internal_value", "Nội bộ"),
        blocked_by_values=data.get(
            "blocked_by_values", ["Nội bộ", "Khách hàng", "Bên thứ ba"]
        ),
        ops_epic_prefix=data.get("ops_epic_prefix", "[OPS]"),
        ops_epic_summary=data.get(
            "ops_epic_summary", "[OPS] Vận hành & Hotfix {month:02d}/{year}"
        ),
        hotfix_prefix=data.get("hotfix_prefix", "[HOTFIX]"),
        bot_user=data.get("bot_user", "eggs-bot"),
        fields=data.get("fields", {}),
        statuses=data.get("statuses", {}),
        issue_types=data.get("issue_types", {}),
        transitions=data.get("transitions", {}),
        report_resolutions=data.get("report_resolutions", {}),
        link_types=link_types_data,
        thresholds=thresholds,
        holidays=frozenset(holidays_set),
        projects=projects,
        lead_chat_webhook=data.get("lead_chat_webhook"),
        kit_min_version=data.get("kit_min_version", "0.1.0"),
        roles=normalized_roles,
    )
