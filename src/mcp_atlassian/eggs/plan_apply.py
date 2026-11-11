from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .actions import update_ac_section
from .config import EggsConfig
from .gateway import JiraError, JiraGateway
from .plan import (
    Plan,
    PlanEpic,
    PlanFlow,
    PlanPart,
    format_summary,
    split_codes,
    split_codes_exact,
)
from .plan_validate import PlanContext, validate_plan


@dataclass
class ApplyResult:
    ok: bool
    message: str
    changes: list[str] = field(default_factory=list)
    plan: Plan = field(default_factory=Plan)
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    would_create: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


def _fields(issue: dict[str, Any] | None) -> dict[str, Any]:
    return issue.get("fields", {}) if issue else {}


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return value if isinstance(value, str) else None


def _status_name(issue: dict[str, Any] | None) -> str:
    raw = _fields(issue).get("status")
    if isinstance(raw, dict):
        return str(raw.get("name") or "")
    return str(raw or "")


def _norm_newlines(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n")


def _desc_equal(current: str | None, desired: str) -> bool:
    return _norm_newlines(current).rstrip("\n") == _norm_newlines(desired).rstrip("\n")


def _ac_text(flow: PlanFlow) -> str:
    return "\n".join(f"- {ac}" for ac in flow.ac)


def _tbd_in_ac(flow: PlanFlow) -> bool:
    return any(ac.strip().upper().startswith("TBD") for ac in flow.ac)


def _flow_reasons(flow: PlanFlow, ctx: PlanContext) -> list[str]:
    """Why a flow cannot go to Ready For Dev (same rule as before)."""
    reasons: list[str] = []
    if not (flow.actor and flow.actor in ctx.components):
        reasons.append("thiếu Component")
    if not flow.due:
        reasons.append("thiếu Due Date")
    if not flow.qc:
        reasons.append("thiếu QC Owner")
    if not flow.ac or _tbd_in_ac(flow):
        reasons.append("AC chưa đầy đủ hoặc có TBD")
    if not (flow.parts and all(p.part and p.estimate for p in flow.parts)):
        reasons.append("phần việc chưa đủ Part/Estimate")
    return reasons


def _story_update_fields(
    flow: PlanFlow,
    story_fields: dict[str, Any],
    ctx: PlanContext,
    cfg: EggsConfig,
) -> dict[str, Any]:
    """Fields to change on an existing Story: never drop a code or Component."""
    upd: dict[str, Any] = {}

    current_summary = story_fields.get("summary") or ""
    current_codes, current_title = split_codes_exact(current_summary)
    wanted_codes = list(current_codes)
    for code in [*(flow.codes or []), flow.code]:
        if code and code not in wanted_codes:
            wanted_codes.append(code)
    if wanted_codes == current_codes:
        # Codes unchanged: keep the summary byte-for-byte.
        desired_summary = current_summary
    else:
        desired_summary = format_summary(wanted_codes, current_title or flow.summary)
    if desired_summary != current_summary:
        upd["summary"] = desired_summary

    if flow.actor and flow.actor in ctx.components:
        component_name = ctx.components[flow.actor]
        current_components = [
            comp.get("name")
            for comp in story_fields.get("components") or []
            if isinstance(comp, dict) and comp.get("name")
        ]
        if component_name not in current_components:
            upd["components"] = [
                *({"name": name} for name in current_components),
                {"name": component_name},
            ]

    qc_fid = cfg.fields.get("qc_owner")
    if qc_fid and flow.qc and _name(story_fields.get(qc_fid)) != flow.qc:
        upd[qc_fid] = {"name": flow.qc}

    if flow.ac:
        current_desc = _norm_newlines(story_fields.get("description"))
        desired_desc = update_ac_section(current_desc, _ac_text(flow))
        if not _desc_equal(current_desc, desired_desc):
            upd["description"] = desired_desc

    return upd


_DURATION_UNITS = {"w": 5 * 8 * 3600, "d": 8 * 3600, "h": 3600, "m": 60}


def _duration_seconds(value: str | None) -> int | None:
    """Parse a Jira duration ("1d 4h") with 8h/day and 5d/week."""
    if not value:
        return None
    matches = re.findall(r"(\d+)\s*([wdhm])", value.lower())
    if not matches:
        return None
    return sum(int(num) * _DURATION_UNITS[unit] for num, unit in matches)


def _jira_seconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _dev_task_part(candidate: dict[str, Any], cfg: EggsConfig, part_field: str) -> Any:
    fields = _fields(candidate)
    candidate_type = fields.get("issuetype", {}).get("name")
    dev_task_types = set(cfg.issue_types.get("dev_task", ["Dev Task"]))
    if candidate_type and candidate_type not in dev_task_types:
        return None
    raw_part = fields.get(part_field)
    return raw_part.get("value") if isinstance(raw_part, dict) else raw_part


def _match_dev_task(
    part: PlanPart,
    candidates: list[dict[str, Any]],
    claimed: set[str],
    cfg: EggsConfig,
    part_field: str,
) -> dict[str, Any] | None:
    if part.key:
        match = next((c for c in candidates if c.get("key") == part.key), None)
        return match
    dev_task_types = set(cfg.issue_types.get("dev_task", ["Dev Task"]))
    for candidate in candidates:
        key = candidate.get("key") or ""
        if not key or key in claimed:
            continue
        candidate_fields = _fields(candidate)
        candidate_type = candidate_fields.get("issuetype", {}).get("name")
        if candidate_type and candidate_type not in dev_task_types:
            continue
        raw_part = candidate_fields.get(part_field)
        candidate_part = (
            raw_part.get("value") if isinstance(raw_part, dict) else raw_part
        )
        candidate_assignee = _name(candidate_fields.get("assignee"))
        if candidate_part == part.part and candidate_assignee == part.assignee:
            return candidate
    return None


def apply_plan(
    gateway: JiraGateway,
    cfg: EggsConfig,
    plan: Plan,
    ctx: PlanContext,
    *,
    dry_run: bool = False,
) -> ApplyResult:
    """Apply a plan to Jira idempotently, creating/updating issues as needed."""
    # 1. Validate plan before any writes
    report = validate_plan(plan, ctx, cfg)
    if not report.ok:
        err_msgs = [f.message for f in report.findings if f.level == "error"]
        return ApplyResult(
            ok=False,
            message="Kế hoạch không hợp lệ, vui lòng sửa các lỗi trước khi thực hiện.",
            changes=[],
            plan=report.plan,
            created=[],
            updated=[],
            warnings=err_msgs,
            errors=[{"code": "", "key": "", "error": message} for message in err_msgs],
        )

    working_plan = copy.deepcopy(report.plan)
    changes: list[str] = []
    created: list[str] = []
    updated: list[str] = []
    would_create: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    warnings: list[str] = [f.message for f in report.findings if f.level == "warning"]

    def record_error(flow: PlanFlow | None, key: str | None, exc: Any) -> None:
        errors.append(
            {
                "code": (flow.code if flow else "") or "",
                "key": key or "",
                "error": str(exc),
            }
        )

    # 2. Epic creation or resolution
    epic_key = working_plan.epic.existing or working_plan.epic.key
    if not epic_key:
        if dry_run:
            would_create.append({"type": "Epic", "summary": working_plan.epic.summary})
            changes.append(f"[Dry-run] Tạo Epic mới: [NEW] {working_plan.epic.summary}")
        else:
            create_payload: dict[str, Any] = {
                "project": {"key": working_plan.project},
                "issuetype": {"name": cfg.issue_types.get("epic", ["Epic"])[0]},
                "summary": working_plan.epic.summary,
            }
            epic_name_fid = cfg.fields.get("epic_name", "customfield_10102")
            create_payload[epic_name_fid] = working_plan.epic.summary
            if working_plan.epic.due:
                create_payload["duedate"] = str(working_plan.epic.due)
            if working_plan.epic.owner:
                create_payload["assignee"] = {"name": working_plan.epic.owner}

            try:
                epic_key = gateway.create_issue(create_payload)
            except Exception as exc:  # noqa: BLE001
                record_error(None, None, exc)
                return ApplyResult(
                    ok=False,
                    message=f"Không tạo được Epic: {exc}",
                    changes=changes,
                    plan=working_plan,
                    created=created,
                    updated=updated,
                    warnings=warnings,
                    would_create=would_create,
                    errors=errors,
                )
            working_plan.epic.key = epic_key
            created.append(epic_key)
            changes.append(f"Tạo Epic mới {epic_key}: {working_plan.epic.summary}")
    else:
        working_plan.epic.key = epic_key

    # 3. Map codes to existing Story keys
    story_types = set(cfg.issue_types.get("story", ["Story"]))
    code_to_key: dict[str, str] = {}
    for child in ctx.epic_stories:
        child_type = _fields(child).get("issuetype", {}).get("name")
        if child_type and child_type not in story_types:
            continue
        child_codes, _ = split_codes(_fields(child).get("summary", ""))
        for code in child_codes:
            if child.get("key"):
                code_to_key[code] = child["key"]

    part_field_id = cfg.fields.get("part", "customfield_10701")
    claimed_dev_tasks: set[str] = set()
    property_cache: dict[str, str] = dict(ctx.flow_codes)
    flow_states: dict[int, dict[str, Any]] = {}

    def current_property_code(key: str) -> tuple[str, str | None]:
        """Return (flowCode, read error); never turn a failure into "unset"."""
        if key in property_cache:
            return property_cache[key], None
        getter = getattr(gateway, "get_issue_property", None)
        value = ""
        if getter is not None:
            try:
                raw = getter(key, "eggs.flowCode")
            except Exception as exc:  # noqa: BLE001
                return value, str(exc)
            if isinstance(raw, str):
                value = raw
        property_cache[key] = value
        return value, None

    # 4. Flows
    for flow_index, flow in enumerate(working_plan.flows):
        flow_errors_start = len(errors)
        story_key = flow.key
        matched_story: dict[str, Any] | None = None
        if story_key:
            matched_story = next(
                (s for s in ctx.epic_stories if s.get("key") == story_key),
                None,
            )
        elif flow.code and flow.code in code_to_key:
            story_key = code_to_key[flow.code]
            matched_story = next(
                (s for s in ctx.epic_stories if s.get("key") == story_key),
                None,
            )

        created_now = False
        analyzed = False
        current_status = "Open"
        raw_status = ""

        try:
            if story_key and matched_story is None and not dry_run:
                matched_story = gateway.get_issue(story_key)
        except Exception as exc:  # noqa: BLE001
            record_error(flow, story_key, exc)
            continue

        if story_key:
            story_key = str(story_key)
            flow.key = story_key
            if flow.code:
                code_to_key[flow.code] = story_key
            raw_status = _status_name(matched_story)
            current_status = raw_status or "Open"
        else:
            # Create the Story
            if dry_run:
                created_now = True
                would_create.append(
                    {
                        "type": "Story",
                        "code": flow.code,
                        "summary": flow.summary,
                    }
                )
                changes.append(f"[Dry-run] Tạo Story mới: [{flow.code}] {flow.summary}")
                if flow.code:
                    code_to_key.setdefault(flow.code, flow.code)
            else:
                desc = update_ac_section("", _ac_text(flow)) if flow.ac else ""
                comp_name = ctx.components.get(flow.actor) if flow.actor else None
                story_payload: dict[str, Any] = {
                    "project": {"key": working_plan.project},
                    "issuetype": {"name": cfg.issue_types.get("story", ["Story"])[0]},
                    "summary": format_summary(
                        flow.codes or ([flow.code] if flow.code else []),
                        flow.summary,
                    ),
                    "description": desc,
                }
                if comp_name:
                    story_payload["components"] = [{"name": comp_name}]
                if flow.ba:
                    story_payload["assignee"] = {"name": flow.ba}
                if flow.qc and cfg.fields.get("qc_owner"):
                    story_payload[cfg.fields["qc_owner"]] = {"name": flow.qc}
                if epic_key and cfg.fields.get("epic_link"):
                    story_payload[cfg.fields["epic_link"]] = epic_key

                try:
                    story_key = gateway.create_issue(story_payload)
                except Exception as exc:  # noqa: BLE001
                    record_error(flow, None, exc)
                    continue
                flow.key = story_key
                created_now = True
                created.append(story_key)
                if flow.code:
                    code_to_key[flow.code] = story_key
                changes.append(
                    f"Tạo Story mới {story_key}: [{flow.code}] {flow.summary}"
                )

        # 4a. Story fields (existing Stories only; new ones got them at create)
        if not created_now and matched_story is not None:
            try:
                upd_fields = _story_update_fields(
                    flow, _fields(matched_story), ctx, cfg
                )
                if dry_run:
                    if upd_fields:
                        changes.append(
                            f"[Dry-run] Cập nhật trường cho Story {story_key}: "
                            f"{list(upd_fields.keys())}"
                        )
                elif upd_fields:
                    gateway.update_fields(
                        str(story_key), upd_fields, notify_users=False
                    )
                    updated.append(str(story_key))
                    changes.append(
                        f"Cập nhật trường cho Story {story_key}: "
                        f"{list(upd_fields.keys())}"
                    )
            except Exception as exc:  # noqa: BLE001
                record_error(flow, story_key, exc)

        # 4b. eggs.flowCode property (skip when already equal)
        if flow.code and story_key:
            try:
                current_code, read_error = current_property_code(story_key)
                if read_error:
                    msg = f"Không đọc được eggs.flowCode của {story_key}: {read_error}"
                    raise JiraError(msg)
                if current_code != flow.code:
                    if dry_run:
                        changes.append(
                            f"[Dry-run] Ghi eggs.flowCode cho {story_key} = {flow.code}"
                        )
                    else:
                        gateway.set_issue_property(
                            story_key, "eggs.flowCode", flow.code
                        )
                        property_cache[story_key] = flow.code
                        changes.append(
                            f"Ghi eggs.flowCode cho {story_key} = {flow.code}"
                        )
            except Exception as exc:  # noqa: BLE001
                record_error(flow, story_key, exc)

        # 4c. Start Analysis for a new Story or an existing Open Story
        if created_now or (matched_story is not None and raw_status.lower() == "open"):
            has_comp = bool(flow.actor and flow.actor in ctx.components)
            has_qc = bool(flow.qc)
            has_ac = bool(flow.ac)
            missing_parts: list[str] = []
            if not has_comp:
                missing_parts.append("Component")
            if not has_qc:
                missing_parts.append("QC Owner")
            if not has_ac:
                missing_parts.append("AC")

            if missing_parts:
                missing_str = "/".join(missing_parts)
                changes.append(
                    f"{story_key or flow.code} giữ nguyên Open: thiếu {missing_str}"
                )
            else:
                analyzed = True
                current_status = "Analyzing"
                if dry_run:
                    changes.append(f"[Dry-run] Chuyển Analysis Done cho {flow.code}")
                else:
                    try:
                        analysis_trans = cfg.transitions.get(
                            "analysis_done", "Analysis Done"
                        )
                        gateway.transition(str(story_key), analysis_trans)
                        changes.append(f"{story_key} chuyển trạng thái -> Analyzed")
                    except Exception as exc:  # noqa: BLE001
                        analyzed = False
                        current_status = "Open"
                        record_error(flow, story_key, exc)

        # 4d. Due Date
        if flow.due:
            current_due = _fields(matched_story).get("duedate")
            try:
                if current_due:
                    current_due_str = str(current_due)[:10]
                    if current_due_str != str(flow.due):
                        warnings.append(
                            f"{story_key}: Due Date đang là {current_due_str}, "
                            f"plan là {flow.due}; muốn đổi thì dùng dời lịch "
                            "(eggs_request_reschedule)"
                        )
                elif dry_run:
                    changes.append(
                        f"[Dry-run] Chuyển Set Due Date cho "
                        f"{story_key or flow.code} = {flow.due}"
                    )
                else:
                    set_due_trans = cfg.transitions.get("set_due", "Set Due Date")
                    gateway.transition(
                        str(story_key),
                        set_due_trans,
                        fields={"duedate": str(flow.due)},
                        comment=f"Đặt Due Date: {flow.due}",
                    )
                    changes.append(f"Đặt Due Date cho {story_key} = {flow.due}")
            except JiraError as exc:
                warnings.append(
                    f"{story_key}: chưa đặt được Due Date {flow.due} "
                    f"(Set Due Date không khả dụng: {exc})"
                )
            except Exception as exc:  # noqa: BLE001
                record_error(flow, story_key, exc)

        # 4e. Dev Tasks under this Story
        try:
            if not story_key and not dry_run:
                raise JiraError("Story chưa có key để tạo Dev Task")
            candidates: list[dict[str, Any]] = []
            if story_key:
                candidates = gateway.search(
                    f'parent = "{story_key}"',
                    fields=[
                        "key",
                        "issuetype",
                        "summary",
                        "status",
                        "assignee",
                        part_field_id,
                        "timetracking",
                        "timeoriginalestimate",
                    ],
                )
            for part in flow.parts:
                matched_dt = _match_dev_task(
                    part, candidates, claimed_dev_tasks, cfg, part_field_id
                )
                if part.key and matched_dt is None:
                    msg = (
                        f"Dev Task {part.key} không thuộc Story {story_key}; "
                        "không tạo Dev Task mới cho part có key"
                    )
                    raise JiraError(msg)
                if matched_dt is None and part.part and not part.assignee:
                    part_only = next(
                        (
                            candidate
                            for candidate in candidates
                            if _dev_task_part(candidate, cfg, part_field_id)
                            == part.part
                            and candidate.get("key") not in claimed_dev_tasks
                        ),
                        None,
                    )
                    if part_only is not None:
                        warnings.append(
                            f"Luồng '{flow.code or flow.summary}' ({story_key}): "
                            f"phần việc {part.part} thiếu assignee nên không ghép "
                            f"được Dev Task {part_only.get('key')}; sẽ tạo Dev Task mới"
                        )
                if matched_dt is not None:
                    part_key = matched_dt.get("key")
                    if part_key and part_key in claimed_dev_tasks:
                        msg = f"Dev Task {part_key} đã được nhận bởi phần việc khác"
                        raise JiraError(msg)
                    if part_key:
                        claimed_dev_tasks.add(part_key)
                    part.key = part_key
                    dt_fields = _fields(matched_dt)
                    raw_part = dt_fields.get(part_field_id)
                    dt_part = (
                        raw_part.get("value")
                        if isinstance(raw_part, dict)
                        else raw_part
                    )
                    dt_assignee = _name(dt_fields.get("assignee"))
                    tt = dt_fields.get("timetracking")
                    dt_est = (
                        str(tt.get("originalEstimate") or "")
                        if isinstance(tt, dict)
                        else ""
                    )
                    dt_seconds = _jira_seconds(dt_fields.get("timeoriginalestimate"))
                    if dt_seconds is None and dt_est:
                        dt_seconds = _duration_seconds(dt_est)
                    plan_seconds = (
                        _duration_seconds(part.estimate) if part.estimate else None
                    )

                    upd_part: dict[str, Any] = {}
                    if part.part and dt_part != part.part:
                        upd_part[part_field_id] = {"value": part.part}
                    if part.assignee and dt_assignee != part.assignee:
                        upd_part["assignee"] = {"name": part.assignee}
                    estimate_differs = False
                    plan_estimate = part.estimate
                    if plan_estimate:
                        if plan_seconds is not None and dt_seconds is not None:
                            estimate_differs = plan_seconds != dt_seconds
                        else:
                            estimate_differs = dt_est.strip() != plan_estimate.strip()
                    if plan_estimate and estimate_differs:
                        upd_part["timetracking"] = {
                            "originalEstimate": plan_estimate.strip()
                        }
                    if not upd_part:
                        continue
                    if dry_run:
                        changes.append(
                            f"[Dry-run] Cập nhật Dev Task {part_key}: "
                            f"{list(upd_part.keys())}"
                        )
                    elif part_key:
                        gateway.update_fields(part_key, upd_part, notify_users=False)
                        changes.append(
                            f"Cập nhật Dev Task {part_key}: {list(upd_part.keys())}"
                        )
                else:
                    if dry_run:
                        would_create.append(
                            {
                                "type": "Dev Task",
                                "part": part.part,
                                "story": flow.code or story_key,
                                "summary": f"[{part.part}] {flow.summary}",
                            }
                        )
                        changes.append(
                            f"[Dry-run] Tạo Dev Task: [{part.part}] {flow.summary}"
                        )
                        continue
                    dt_payload: dict[str, Any] = {
                        "project": {"key": working_plan.project},
                        "parent": {"key": story_key},
                        "issuetype": {
                            "name": cfg.issue_types.get("dev_task", ["Dev Task"])[0]
                        },
                        "summary": f"[{part.part}] {flow.summary}",
                    }
                    if part.part:
                        dt_payload[part_field_id] = {"value": part.part}
                    if part.assignee:
                        dt_payload["assignee"] = {"name": part.assignee}
                    if part.estimate:
                        dt_payload["timetracking"] = {
                            "originalEstimate": part.estimate.strip()
                        }

                    part_key = gateway.create_issue(dt_payload)
                    part.key = part_key
                    claimed_dev_tasks.add(part_key)
                    created.append(part_key)
                    changes.append(
                        f"Tạo Dev Task mới {part_key}: [{part.part}] {flow.summary}"
                    )
        except Exception as exc:  # noqa: BLE001
            record_error(flow, story_key, exc)

        flow_states[flow_index] = {
            "status": current_status,
            "created": created_now,
            "analyzed": analyzed,
            "story_ready": True,
            "ready_ok": len(errors) == flow_errors_start,
        }

    # 5. Tech tasks (Ad-hoc Tasks in the Epic)
    adhoc_types = set(cfg.issue_types.get("adhoc", ["Ad-hoc Task"]))
    adhoc_children = [
        child
        for child in ctx.epic_stories
        if _fields(child).get("issuetype", {}).get("name") in adhoc_types
    ]
    for tt in working_plan.tech_tasks:
        try:
            matched_tt = None
            if tt.key:
                matched_tt = next(
                    (c for c in adhoc_children if c.get("key") == tt.key), None
                )
                if matched_tt is None:
                    msg = (
                        f"Ad-hoc Task {tt.key} không thuộc Epic "
                        f"{working_plan.epic.existing or working_plan.epic.key}"
                    )
                    raise JiraError(msg)
            else:
                matched_tt = next(
                    (
                        c
                        for c in adhoc_children
                        if _fields(c).get("summary") == tt.summary
                    ),
                    None,
                )
            if matched_tt is not None:
                tt.key = matched_tt.get("key")
                continue
            if dry_run:
                would_create.append({"type": "Ad-hoc Task", "summary": tt.summary})
                changes.append(f"[Dry-run] Tạo Ad-hoc Task: {tt.summary}")
                continue
            adhoc_payload: dict[str, Any] = {
                "project": {"key": working_plan.project},
                "issuetype": {"name": cfg.issue_types.get("adhoc", ["Ad-hoc Task"])[0]},
                "summary": tt.summary,
            }
            if epic_key and cfg.fields.get("epic_link"):
                adhoc_payload[cfg.fields["epic_link"]] = epic_key
            if tt.assignee:
                adhoc_payload["assignee"] = {"name": tt.assignee}
            if tt.estimate:
                adhoc_payload["timetracking"] = {
                    "originalEstimate": tt.estimate.strip()
                }
            tt_key = gateway.create_issue(adhoc_payload)
            tt.key = tt_key
            created.append(tt_key)
            changes.append(f"Tạo Ad-hoc Task {tt_key}: {tt.summary}")
        except Exception as exc:  # noqa: BLE001
            errors.append({"code": "", "key": tt.key or "", "error": str(exc)})

    # 6. Links for depends_on
    for flow_index, flow in enumerate(working_plan.flows):
        state = flow_states.get(flow_index)
        if not state or not state.get("story_ready") or not flow.depends_on:
            continue
        story_key = flow.key or flow.code or ""
        try:
            dep_pairs: list[tuple[str, str]] = []
            for dep_code in flow.depends_on:
                dep_key = code_to_key.get(dep_code)
                if not dep_key:
                    msg = f"Không tìm thấy Story cho mã phụ thuộc {dep_code}"
                    raise JiraError(msg)
                dep_pairs.append((dep_code, dep_key))

            if dry_run:
                for dep_code, dep_key in dep_pairs:
                    would_create.append(
                        {
                            "type": "Link",
                            "from": dep_code,
                            "to": flow.code or story_key,
                        }
                    )
                    changes.append(
                        f"[Dry-run] Tạo liên kết {dep_key} blocks {story_key}"
                    )
                continue

            link_type = cfg.link_types.get("blocks", "Blocks")
            try:
                jql_links = f'issue in linkedIssues("{story_key}", "is blocked by")'
                existing_blocking = gateway.search(jql_links, fields=["key"])
            except Exception as exc:  # noqa: BLE001
                msg = f"Không đọc được liên kết hiện có của {story_key}: {exc}"
                raise JiraError(msg) from exc
            blocking_keys = {
                issue.get("key") for issue in existing_blocking if issue.get("key")
            }
            for _dep_code, dep_key in dep_pairs:
                if dep_key in blocking_keys:
                    continue
                gateway.link(link_type, inward_key=dep_key, outward_key=story_key)
                changes.append(
                    f"Tạo liên kết {dep_key} blocks {story_key} (type: {link_type})"
                )
        except Exception as exc:  # noqa: BLE001
            record_error(flow, flow.key, exc)

    # 7. Ready For Dev for every Story currently Analyzing
    for flow_index, flow in enumerate(working_plan.flows):
        state = flow_states.get(flow_index)
        if not state or not state.get("story_ready") or not state.get("ready_ok"):
            continue
        status = str(state.get("status") or "")
        if status.lower() != "analyzing":
            if state.get("created") and status.lower() == "open":
                reasons = _flow_reasons(flow, ctx)
                reason_str = ", ".join(reasons)
                warnings.append(
                    f"Luồng '{flow.code or flow.summary}' ({flow.key or flow.code}) "
                    f"ở lại trạng thái Open vì {reason_str}"
                )
            continue

        reasons = _flow_reasons(flow, ctx)
        story_key = flow.key or flow.code
        if reasons:
            warnings.append(
                f"Luồng '{flow.code or flow.summary}' ({story_key}) "
                f"ở lại trạng thái Analyzed vì {', '.join(reasons)}"
            )
            continue

        if dry_run:
            changes.append(f"[Dry-run] Chuyển Ready For Dev cho {story_key}")
            continue
        try:
            ready_trans = cfg.transitions.get("ready_for_dev", "Ready For Dev")
            gateway.transition(str(story_key), ready_trans)
            changes.append(f"{story_key} chuyển trạng thái -> Ready For Dev")
        except JiraError as err:
            warnings.append(
                f"Luồng '{flow.code or flow.summary}' ({story_key}) "
                f"không thể chuyển Ready For Dev: {err}"
            )

    ok = not errors
    if errors:
        msg = (
            f"Áp dụng kế hoạch chưa hoàn tất: {len(errors)} luồng/bước lỗi. "
            "Chạy lại cùng plan sau khi sửa."
        )
    else:
        msg = (
            "Kiểm tra kế hoạch thành công (dry-run)."
            if dry_run
            else "Áp dụng kế hoạch thành công."
        )
    return ApplyResult(
        ok=ok,
        message=msg,
        changes=changes,
        plan=working_plan,
        created=created,
        updated=updated,
        warnings=warnings,
        would_create=would_create,
        errors=errors,
    )


def export_epic(gateway: JiraGateway, cfg: EggsConfig, epic_key: str) -> Plan:
    """Export an existing Epic and its children to an eggs-plan/v2 Plan."""
    epic_issue = gateway.get_issue(epic_key)
    project = (
        epic_issue.get("fields", {}).get("project", {}).get("key")
        or epic_key.split("-")[0]
    )

    epic_link_id = cfg.fields.get("epic_link")
    if epic_link_id:
        cf_match = re.search(r"\d+", epic_link_id)
        cf_id = cf_match.group(0) if cf_match else epic_link_id
        jql_epic = f'cf[{cf_id}] = "{epic_key}"'
    else:
        jql_epic = f'"Epic Link" = "{epic_key}"'

    search_fields = [
        "summary",
        "components",
        "duedate",
        "issuetype",
        "status",
        "description",
        "assignee",
        "project",
        "parent",
    ]
    if cfg.fields.get("qc_owner"):
        search_fields.append(cfg.fields["qc_owner"])
    if epic_link_id:
        search_fields.append(epic_link_id)

    children = gateway.search(jql_epic, fields=search_fields)

    story_types = set(cfg.issue_types.get("story", ["Story"]))
    dev_task_types = set(cfg.issue_types.get("dev_task", ["Dev Task"]))
    bug_sub_types = set(cfg.issue_types.get("bug_sub", ["Bug"]))
    qc_fid = cfg.fields.get("qc_owner")
    part_field_id = cfg.fields.get("part", "customfield_10701")

    flows: list[PlanFlow] = []
    other_issues: list[dict[str, Any]] = []
    open_questions: list[str] = []

    for child in children:
        if child.get("key") == epic_key:
            continue
        c_fields = child.get("fields", {})
        it_name = c_fields.get("issuetype", {}).get("name")
        if c_fields.get("parent"):
            # A sub-task is never a direct child of the Epic.
            continue

        if not it_name or it_name in story_types:
            key = child.get("key", "")
            summary_raw = c_fields.get("summary", "")
            codes, title = split_codes_exact(summary_raw)

            components = c_fields.get("components") or []
            component_names = [
                comp.get("name", "") if isinstance(comp, dict) else str(comp)
                for comp in components
            ]

            def _actor_of(comp_name: str) -> str:
                if " - " in comp_name:
                    return comp_name.split(" - ", 1)[0].strip()
                return comp_name.strip()

            actor = None
            code_prefixes = {code.split("-")[0] for code in codes}
            for comp_name in component_names:
                if _actor_of(comp_name) in code_prefixes:
                    actor = _actor_of(comp_name)
                    break
            if actor is None and len(component_names) == 1:
                actor = _actor_of(component_names[0])

            # Due Date
            raw_due = c_fields.get("duedate")
            due = None
            if raw_due:
                try:
                    due = date.fromisoformat(str(raw_due)[:10])
                except ValueError:
                    due = None

            # Assignee (BA)
            ba = _name(c_fields.get("assignee"))

            # QC Owner
            raw_qc = c_fields.get(qc_fid) if qc_fid else None
            qc = _name(raw_qc)

            # Parts = Dev Task sub-tasks, read with a parent search (item 1)
            parts: list[PlanPart] = []
            if key:
                dev_tasks = gateway.search(
                    f'parent = "{key}"',
                    fields=[
                        "key",
                        "issuetype",
                        "summary",
                        "status",
                        "assignee",
                        part_field_id,
                        "timetracking",
                    ],
                )
                for st in dev_tasks:
                    st_fields = _fields(st)
                    st_type = st_fields.get("issuetype", {}).get("name")
                    if st_type in bug_sub_types:
                        continue
                    if dev_task_types and st_type and st_type not in dev_task_types:
                        continue

                    raw_part = st_fields.get(part_field_id)
                    part_val = (
                        raw_part.get("value")
                        if isinstance(raw_part, dict)
                        else raw_part
                    )

                    st_assignee = _name(st_fields.get("assignee"))

                    # Estimate as Jira duration
                    est_val = None
                    tt = st_fields.get("timetracking")
                    if isinstance(tt, dict) and tt.get("originalEstimate"):
                        est_val = str(tt["originalEstimate"])
                    elif st_fields.get("timeoriginalestimate") is not None:
                        secs = int(st_fields["timeoriginalestimate"])
                        days = secs // 28800
                        rem = secs % 28800
                        hours = rem // 3600
                        mins = (rem % 3600) // 60
                        dur_parts = []
                        if days > 0:
                            dur_parts.append(f"{days}d")
                        if hours > 0:
                            dur_parts.append(f"{hours}h")
                        if mins > 0:
                            dur_parts.append(f"{mins}m")
                        est_val = " ".join(dur_parts) if dur_parts else "0m"

                    parts.append(
                        PlanPart(
                            part=part_val,
                            assignee=st_assignee,
                            estimate=est_val,
                            key=st.get("key"),
                        )
                    )

            flows.append(
                PlanFlow(
                    code=codes[0] if codes else None,
                    codes=codes,
                    actor=actor,
                    summary=title,
                    ac=[],
                    due=due,
                    ba=ba,
                    qc=qc,
                    depends_on=[],
                    parts=parts,
                    key=key,
                    keep_description=True,
                )
            )

            # Check missing actor / due / qc for open_questions
            missing: list[str] = []
            if not actor:
                missing.append("Actor")
            if not due:
                missing.append("Due Date")
            if not qc:
                missing.append("QC Owner")
            if missing:
                missing_str = ", ".join(missing)
                open_questions.append(f"Story {key} ({title}): thiếu {missing_str}")

        else:
            # Other issue under Epic
            other_issues.append(
                {
                    "key": child.get("key"),
                    "type": it_name,
                    "status": c_fields.get("status", {}).get("name"),
                    "summary": c_fields.get("summary", ""),
                }
            )

    return Plan(
        project=project,
        epic=PlanEpic(existing=epic_key),
        flows=flows,
        tech_tasks=[],
        open_questions=open_questions,
        other_issues=other_issues,
    )
