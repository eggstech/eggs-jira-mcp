from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any
from zoneinfo import ZoneInfo

from .config import EggsConfig
from .model import Issue


def parse_jira_dt(value: Any, cfg: EggsConfig | None = None) -> datetime | None:
    """Parse a Jira datetime string or object into an aware datetime in cfg.timezone."""
    if value is None:
        return None

    tz_name = cfg.timezone if (cfg is not None and hasattr(cfg, "timezone")) else "Asia/Ho_Chi_Minh"
    target_tz = ZoneInfo(tz_name)

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=target_tz)
        return value.astimezone(target_tz)

    if not isinstance(value, str):
        return None

    val = value.strip()
    if not val:
        return None

    # Normalize trailing 'Z' or 'z' -> '+00:00'
    if val.endswith(("Z", "z")):
        val = val[:-1] + "+00:00"
    else:
        # Normalize +HHMM or -HHMM (no colon) to +HH:MM or -HH:MM
        val = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", val)

    try:
        dt = datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=target_tz)
    return dt.astimezone(target_tz)


def status_changed_at(raw_changelog: dict | None, cfg: EggsConfig | None = None) -> datetime | None:
    """Extract the timestamp of the latest status change from issue changelog."""
    if not raw_changelog or not isinstance(raw_changelog, dict):
        return None

    histories = raw_changelog.get("histories", [])
    if not histories:
        return None

    status_changes: list[datetime] = []
    for history in histories:
        items = history.get("items", [])
        has_status_change = any(item.get("field") == "status" for item in items)
        if has_status_change and history.get("created"):
            dt = parse_jira_dt(history["created"], cfg)
            if dt:
                status_changes.append(dt)

    if not status_changes:
        return None

    return max(status_changes)


def _parse_date(val: Any) -> date | None:
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str) and val.strip():
        try:
            return date.fromisoformat(val.strip().split("T")[0])
        except ValueError:
            return None
    return None


def _parse_user(val: Any) -> str | None:
    if isinstance(val, dict):
        return val.get("name") or val.get("key") or val.get("displayName")
    if isinstance(val, str):
        return val
    return None


def _parse_users(val: Any) -> tuple[str, ...]:
    if isinstance(val, list):
        users = [_parse_user(u) for u in val]
        return tuple(u for u in users if u)
    if isinstance(val, tuple):
        return val
    return ()


def _parse_option(val: Any) -> str | None:
    if isinstance(val, dict):
        return val.get("value") or val.get("name")
    if isinstance(val, str):
        return val
    return None


def _parse_options(val: Any) -> frozenset[str]:
    if isinstance(val, list):
        opts = [_parse_option(item) for item in val]
        return frozenset(o for o in opts if o)
    if isinstance(val, (set, frozenset)):
        return frozenset(val)
    return frozenset()


def issue_from_json(raw: dict, cfg: EggsConfig) -> Issue:
    """Map Jira REST v2 issue JSON to an immutable Issue model."""
    key = raw.get("key", "")
    fields = raw.get("fields") or {}

    project_data = fields.get("project") or {}
    project = project_data.get("key") or (key.split("-")[0] if "-" in key else "")

    issuetype_data = fields.get("issuetype") or {}
    raw_type_name = issuetype_data.get("name") or ""
    is_subtask = bool(issuetype_data.get("subtask", False))
    issue_type = cfg.type_logical(raw_type_name, is_subtask=is_subtask) or raw_type_name.lower()

    status_data = fields.get("status") or {}
    raw_status_name = status_data.get("name") or ""
    status = cfg.status_logical(raw_status_name) or raw_status_name.lower()

    assignee = _parse_user(fields.get("assignee"))

    created_raw = fields.get("created")
    created = parse_jira_dt(created_raw, cfg)

    due_date = _parse_date(fields.get("duedate"))
    estimate_seconds = fields.get("timeoriginalestimate") or fields.get("aggregatetimeoriginalestimate")

    labels = fields.get("labels") or []
    is_project_frozen = bool(cfg.projects.get(project).frozen) if project in cfg.projects else False
    frozen = (cfg.frozen_label in labels) or is_project_frozen

    parent_data = fields.get("parent") or {}
    parent_key = parent_data.get("key")
    parent_fields = parent_data.get("fields") or {}
    parent_issuetype = parent_fields.get("issuetype") or {}
    raw_parent_type = parent_issuetype.get("name")
    parent_is_subtask = bool(parent_issuetype.get("subtask", False))
    parent_type = (
        cfg.type_logical(raw_parent_type, is_subtask=parent_is_subtask)
        if raw_parent_type
        else None
    )

    # Helper to retrieve custom field value
    def get_cf(logical_name: str) -> Any:
        fid = cfg.fields.get(logical_name)
        return fields.get(fid) if fid else None

    # Epic key resolution
    epic_link_fid = cfg.fields.get("epic_link")
    epic_key = None
    if epic_link_fid and fields.get(epic_link_fid):
        val = fields[epic_link_fid]
        epic_key = val.get("key") if isinstance(val, dict) else str(val)
    elif fields.get("epic"):
        epic_key = fields["epic"].get("key")

    planned_start = _parse_date(get_cf("planned_start"))
    planned_end = _parse_date(get_cf("planned_end"))
    baseline_end = _parse_date(get_cf("baseline_end"))
    proposed_end = _parse_date(get_cf("proposed_end"))
    health_since = _parse_date(get_cf("health_since"))
    blocked_since = _parse_date(get_cf("blocked_since"))

    raw_components = fields.get("components") or []
    components = tuple(
        c.get("name") if isinstance(c, dict) else str(c)
        for c in raw_components
        if (c.get("name") if isinstance(c, dict) else str(c))
    )
    part = _parse_option(get_cf("part"))
    qc_owner = _parse_user(get_cf("qc_owner")) if issue_type == "story" else None
    reschedule_requester = _parse_user(get_cf("reschedule_requester"))
    escalation_owner = _parse_user(get_cf("escalation_owner"))

    reschedule_status = _parse_option(get_cf("reschedule_status"))
    blocked_by = _parse_option(get_cf("blocked_by"))
    health_flags = _parse_options(get_cf("health_flags"))

    esc_lvl = get_cf("escalation_level")
    escalation_level = int(esc_lvl) if esc_lvl is not None else 0

    # Blocked status
    flagged_raw = fields.get("flagged") or get_cf("flagged")
    impediment_target = cfg.flagged_impediment_value.lower()
    is_flagged = bool(flagged_raw and any(
        (_parse_option(f) or "").lower() == impediment_target
        for f in (flagged_raw if isinstance(flagged_raw, list) else [flagged_raw])
    ))
    blocked = is_flagged or (blocked_by is not None)

    status_chg = status_changed_at(raw.get("changelog"), cfg)

    resolution = _parse_option(fields.get("resolution"))
    report_source_raw = get_cf("report_source")
    report_source = _parse_option(report_source_raw) if isinstance(report_source_raw, dict) else (str(report_source_raw) if report_source_raw is not None else None)
    reported_by_raw = get_cf("reported_by")
    reported_by = _parse_user(reported_by_raw) if isinstance(reported_by_raw, dict) else (str(reported_by_raw) if reported_by_raw is not None else None)
    reported_at = parse_jira_dt(get_cf("reported_at"), cfg)

    return Issue(
        key=key,
        project=project,
        type=issue_type,
        status=status,
        summary=fields.get("summary") or "",
        assignee=assignee,
        components=components,
        part=part,
        parent_type=parent_type,
        qc_owner=qc_owner,
        parent_key=parent_key,
        epic_key=epic_key,
        created=created,
        status_changed_at=status_chg,
        planned_start=planned_start,
        planned_end=planned_end,
        baseline_end=baseline_end,
        due_date=due_date,
        estimate_seconds=estimate_seconds,
        reschedule_status=reschedule_status,
        reschedule_requester=reschedule_requester,
        proposed_end=proposed_end,
        blocked=blocked,
        blocked_by=blocked_by,
        blocked_since=blocked_since,
        health_flags=health_flags,
        health_since=health_since,
        escalation_level=escalation_level,
        escalation_owner=escalation_owner,
        frozen=frozen,
        labels=tuple(labels),
        report_source=report_source,
        reported_by=reported_by,
        reported_at=reported_at,
        resolution=resolution,
    )


# Field types categorization for serialization
_DATE_FIELDS = frozenset({
    "planned_start", "planned_end", "baseline_end", "proposed_end",
    "health_since", "blocked_since", "forecast_end", "due_date",
    "reported_at",
})
_USER_FIELDS = frozenset({
    "escalation_owner", "qc_owner", "reschedule_requester", "assignee"
})
_MULTI_USER_FIELDS = frozenset()
_MULTI_SELECT_FIELDS = frozenset({
    "health_flags"
})
_SELECT_FIELDS = frozenset({
    "part", "reschedule_reason", "reschedule_status", "blocked_by"
})


CLEAR = object()


def issue_fields_for_write(cfg: EggsConfig, **logical_values: Any) -> dict[str, Any]:
    """Convert logical field names and values to Jira REST API write payload."""
    payload: dict[str, Any] = {}

    for logical_name, value in logical_values.items():
        if value is None:
            continue

        # Determine target Jira field ID
        field_id = cfg.fields.get(logical_name)
        if field_id is None:
            # Handle standard fields fallback if not explicitly in cfg.fields
            if logical_name == "due_date" and "due_date" not in cfg.fields:
                field_id = "duedate"
            elif logical_name in ("summary", "description", "assignee") and logical_name not in cfg.fields:
                field_id = logical_name
            else:
                raise ValueError(
                    f"Logical field '{logical_name}' has no configured field ID in cfg.fields"
                )

        if value is CLEAR:
            payload[field_id] = None
            continue

        # Format value according to field type
        if logical_name in _DATE_FIELDS:
            if isinstance(value, date):
                payload[field_id] = value.isoformat()
            else:
                payload[field_id] = str(value)

        elif logical_name in _USER_FIELDS:
            if isinstance(value, dict):
                payload[field_id] = value
            else:
                payload[field_id] = {"name": str(value)}

        elif logical_name in _MULTI_USER_FIELDS:
            if isinstance(value, (list, tuple, set, frozenset)):
                payload[field_id] = [{"name": u} if isinstance(u, str) else u for u in value]
            else:
                payload[field_id] = [{"name": str(value)}]

        elif logical_name in _MULTI_SELECT_FIELDS:
            if isinstance(value, (list, tuple, set, frozenset)):
                payload[field_id] = [{"value": v} for v in sorted(value)]
            else:
                payload[field_id] = [{"value": str(value)}]

        elif logical_name in _SELECT_FIELDS:
            if isinstance(value, dict):
                payload[field_id] = value
            else:
                payload[field_id] = {"value": str(value)}

        else:
            # Scalar values (number, string, etc.)
            payload[field_id] = value

    return payload
