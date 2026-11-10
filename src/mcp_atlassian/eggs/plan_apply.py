from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import EggsConfig
from .gateway import JiraError, JiraGateway
from .plan import Plan, PlanEpic, PlanFlow, PlanPart, format_summary, split_codes
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
        )

    working_plan = copy.deepcopy(report.plan)
    changes: list[str] = []
    created: list[str] = []
    updated: list[str] = []
    warnings: list[str] = [f.message for f in report.findings if f.level == "warning"]

    try:
        # 2. Epic creation or resolution
        epic_key = working_plan.epic.existing or working_plan.epic.key
        if not epic_key:
            if dry_run:
                epic_key = "DRYRUN-EPIC"
                working_plan.epic.key = epic_key
                changes.append(
                    f"[Dry-run] Tạo Epic mới: [NEW] {working_plan.epic.summary}"
                )
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

                epic_key = gateway.create_issue(create_payload)
                working_plan.epic.key = epic_key
                created.append(epic_key)
                changes.append(f"Tạo Epic mới {epic_key}: {working_plan.epic.summary}")
        else:
            working_plan.epic.key = epic_key

        # 3. Map codes to story keys
        code_to_key: dict[str, str] = {}
        for story in ctx.epic_stories:
            c_list, _ = split_codes(story.get("fields", {}).get("summary", ""))
            s_key = story.get("key", "")
            for c in c_list:
                code_to_key[c] = s_key

        # 4. Flows creation and update
        newly_created_story_keys: set[str] = set()
        part_field_id = cfg.fields.get("part", "customfield_10701")
        dev_task_types = set(cfg.issue_types.get("dev_task", ["Dev Task"]))

        for flow in working_plan.flows:
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

            if matched_story or (story_key and not story_key.startswith("DRYRUN-")):
                # Existing Story
                story_key = matched_story.get("key") if matched_story else story_key
                flow.key = story_key
                if flow.code:
                    code_to_key[flow.code] = story_key

                st_fields: dict[str, Any] = (
                    matched_story.get("fields", {})
                    if matched_story
                    else gateway.get_issue(story_key).get("fields", {})
                )

                upd_fields: dict[str, Any] = {}

                # Summary code
                curr_summary = st_fields.get("summary", "")
                _, curr_title = split_codes(curr_summary)
                desired_summary = format_summary(
                    [flow.code] if flow.code else [], curr_title or flow.summary
                )
                if curr_summary != desired_summary:
                    upd_fields["summary"] = desired_summary

                # Component
                if flow.actor and flow.actor in ctx.components:
                    comp_name = ctx.components[flow.actor]
                    existing_comps = [
                        c.get("name")
                        for c in st_fields.get("components", [])
                        if isinstance(c, dict)
                    ]
                    if [comp_name] != existing_comps:
                        upd_fields["components"] = [{"name": comp_name}]

                # QC Owner
                qc_fid = cfg.fields.get("qc_owner")
                if qc_fid and flow.qc:
                    curr_qc = st_fields.get(qc_fid)
                    curr_qc_name = (
                        curr_qc.get("name") if isinstance(curr_qc, dict) else curr_qc
                    )
                    if curr_qc_name != flow.qc:
                        upd_fields[qc_fid] = {"name": flow.qc}

                # Description
                if not flow.keep_description and flow.ac:
                    desired_desc = "## Acceptance Criteria\n" + "\n".join(
                        f"- {ac}" for ac in flow.ac
                    )
                    curr_desc = st_fields.get("description") or ""
                    if curr_desc.strip() != desired_desc.strip():
                        upd_fields["description"] = desired_desc

                if dry_run:
                    if upd_fields:
                        changes.append(
                            f"[Dry-run] Cập nhật trường cho Story {story_key}: "
                            f"{list(upd_fields.keys())}"
                        )
                else:
                    if upd_fields:
                        gateway.update_fields(story_key, upd_fields, notify_users=False)
                        updated.append(story_key)
                        changes.append(
                            f"Cập nhật trường cho Story {story_key}: "
                            f"{list(upd_fields.keys())}"
                        )
                    if flow.code:
                        gateway.set_issue_property(
                            story_key, "eggs.flowCode", flow.code
                        )

                # Due Date for existing Story: only when empty
                curr_due = st_fields.get("duedate")
                if not curr_due and flow.due:
                    if dry_run:
                        changes.append(
                            f"[Dry-run] Chuyển Set Due Date cho "
                            f"Story {story_key} = {flow.due}"
                        )
                    else:
                        set_due_trans = cfg.transitions.get("set_due", "Set Due Date")
                        gateway.transition(
                            story_key,
                            set_due_trans,
                            fields={"duedate": str(flow.due)},
                            comment=f"Đặt Due Date: {flow.due}",
                        )
                        changes.append(f"Đặt Due Date cho {story_key} = {flow.due}")

            else:
                # Create new Story
                if dry_run:
                    story_key = f"DRYRUN-STORY-{flow.code or 'NEW'}"
                    flow.key = story_key
                    if flow.code:
                        code_to_key[flow.code] = story_key
                    changes.append(
                        f"[Dry-run] Tạo Story mới: [{flow.code}] {flow.summary}"
                    )
                else:
                    desc = "## Acceptance Criteria\n" + "\n".join(
                        f"- {ac}" for ac in flow.ac
                    )
                    comp_name = ctx.components.get(flow.actor) if flow.actor else None
                    story_payload: dict[str, Any] = {
                        "project": {"key": working_plan.project},
                        "issuetype": {
                            "name": cfg.issue_types.get("story", ["Story"])[0]
                        },
                        "summary": format_summary(
                            [flow.code] if flow.code else [], flow.summary
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

                    story_key = gateway.create_issue(story_payload)
                    flow.key = story_key
                    if flow.code:
                        code_to_key[flow.code] = story_key
                    created.append(story_key)
                    newly_created_story_keys.add(story_key)
                    changes.append(
                        f"Tạo Story mới {story_key}: [{flow.code}] {flow.summary}"
                    )

                    if flow.code:
                        gateway.set_issue_property(
                            story_key, "eggs.flowCode", flow.code
                        )

                    # Transitions for new Story
                    start_trans = cfg.transitions.get(
                        "start_analysis", "Start Analysis"
                    )
                    gateway.transition(story_key, start_trans)
                    changes.append(f"{story_key} chuyển trạng thái -> Analyzing")

                    if flow.due:
                        set_due_trans = cfg.transitions.get("set_due", "Set Due Date")
                        gateway.transition(
                            story_key,
                            set_due_trans,
                            fields={"duedate": str(flow.due)},
                            comment=f"Đặt Due Date: {flow.due}",
                        )
                        changes.append(f"Đặt Due Date cho {story_key} = {flow.due}")

            # 5. Parts under this Story
            existing_dev_tasks: list[dict[str, Any]] = []
            if story_key and not story_key.startswith("DRYRUN-"):
                if matched_story and matched_story.get("fields", {}).get("subtasks"):
                    existing_dev_tasks = matched_story["fields"]["subtasks"]
                else:
                    try:
                        existing_dev_tasks = gateway.get_subtasks(story_key)
                    except Exception:  # noqa: BLE001
                        existing_dev_tasks = []

            for part in flow.parts:
                matched_dt: dict[str, Any] | None = None
                if part.key:
                    matched_dt = next(
                        (dt for dt in existing_dev_tasks if dt.get("key") == part.key),
                        None,
                    )
                if not matched_dt:
                    for dt in existing_dev_tasks:
                        dt_fields = dt.get("fields", {})
                        dt_type = dt_fields.get("issuetype", {}).get("name")
                        if dt_type and dt_type not in dev_task_types:
                            continue
                        raw_part = dt_fields.get(part_field_id)
                        dt_part = (
                            raw_part.get("value")
                            if isinstance(raw_part, dict)
                            else raw_part
                        )
                        raw_assignee = dt_fields.get("assignee")
                        dt_assignee = (
                            raw_assignee.get("name")
                            if isinstance(raw_assignee, dict)
                            else raw_assignee
                        )
                        if dt_part == part.part and dt_assignee == part.assignee:
                            matched_dt = dt
                            break

                if matched_dt:
                    part_key = matched_dt.get("key")
                    part.key = part_key
                    dt_fields = matched_dt.get("fields", {})
                    raw_part = dt_fields.get(part_field_id)
                    dt_part = (
                        raw_part.get("value")
                        if isinstance(raw_part, dict)
                        else raw_part
                    )
                    dt_est = dt_fields.get("timetracking", {}).get("originalEstimate")
                    upd_part: dict[str, Any] = {}
                    if not dt_part and part.part:
                        upd_part[part_field_id] = {"value": part.part}
                    if not dt_est and part.estimate:
                        upd_part["timetracking"] = {
                            "originalEstimate": part.estimate.strip()
                        }
                    if upd_part and not dry_run and part_key:
                        gateway.update_fields(part_key, upd_part, notify_users=False)
                        changes.append(
                            f"Cập nhật Dev Task {part_key}: {list(upd_part.keys())}"
                        )
                else:
                    if dry_run:
                        part.key = f"DRYRUN-PART-{part.part}"
                        changes.append(
                            f"[Dry-run] Tạo Dev Task: [{part.part}] {flow.summary}"
                        )
                    else:
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
                        created.append(part_key)
                        changes.append(
                            f"Tạo Dev Task mới {part_key}: [{part.part}] {flow.summary}"
                        )

        # 6. Tech tasks
        for tt in working_plan.tech_tasks:
            if not tt.key:
                found_adhoc = next(
                    (
                        s
                        for s in ctx.epic_stories
                        if s.get("fields", {}).get("summary") == tt.summary
                    ),
                    None,
                )
                if found_adhoc:
                    tt.key = found_adhoc.get("key")
                else:
                    if dry_run:
                        tt.key = "DRYRUN-TECH"
                        changes.append(f"[Dry-run] Tạo Ad-hoc Task: {tt.summary}")
                    else:
                        adhoc_payload: dict[str, Any] = {
                            "project": {"key": working_plan.project},
                            "issuetype": {
                                "name": cfg.issue_types.get("adhoc", ["Ad-hoc Task"])[0]
                            },
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

        # 7. Links for depends_on
        for flow in working_plan.flows:
            story_key = flow.key
            if not story_key or story_key.startswith("DRYRUN-"):
                continue
            for dep_code in flow.depends_on:
                dep_key = code_to_key.get(dep_code)
                if not dep_key:
                    continue

                link_type = cfg.link_types.get("blocks", "Blocks")
                blocking_keys: set[str] = set()
                try:
                    jql_links = f'issue in linkedIssues("{story_key}", "is blocked by")'
                    existing_blocking = gateway.search(jql_links, fields=["key"])
                    blocking_keys = {
                        iss.get("key") for iss in existing_blocking if iss.get("key")
                    }
                except Exception:  # noqa: BLE001
                    blocking_keys = set()

                if dep_key not in blocking_keys:
                    if dry_run:
                        changes.append(
                            f"[Dry-run] Tạo liên kết {dep_key} blocks {story_key}"
                        )
                    else:
                        gateway.link(
                            link_type,
                            inward_key=dep_key,
                            outward_key=story_key,
                        )
                        changes.append(
                            f"Tạo liên kết {dep_key} blocks {story_key} "
                            f"(type: {link_type})"
                        )

        # 8. Ready For Dev transition for new stories
        for flow in working_plan.flows:
            story_key = flow.key
            if story_key and story_key in newly_created_story_keys:
                has_comp = bool(flow.actor and flow.actor in ctx.components)
                has_due = bool(flow.due)
                has_qc = bool(flow.qc)
                has_ac_clean = bool(
                    flow.ac and not any(a.strip().startswith("TBD") for a in flow.ac)
                )
                parts_complete = bool(
                    flow.parts and all(p.part and p.estimate for p in flow.parts)
                )

                if has_comp and has_due and has_qc and has_ac_clean and parts_complete:
                    if dry_run:
                        changes.append(
                            f"[Dry-run] Chuyển Ready For Dev cho {story_key}"
                        )
                    else:
                        ready_trans = cfg.transitions.get(
                            "ready_for_dev", "Ready For Dev"
                        )
                        try:
                            gateway.transition(story_key, ready_trans)
                            changes.append(
                                f"{story_key} chuyển trạng thái -> Ready For Dev"
                            )
                        except JiraError as err:
                            msg = (
                                f"Luồng '{flow.code or flow.summary}' ({story_key}) "
                                f"không thể chuyển Ready For Dev: {err}"
                            )
                            warnings.append(msg)
                else:
                    reasons: list[str] = []
                    if not has_due:
                        reasons.append("thiếu Due Date")
                    if not has_qc:
                        reasons.append("thiếu QC Owner")
                    if not has_ac_clean:
                        reasons.append("AC chưa đầy đủ hoặc có TBD")
                    if not parts_complete:
                        reasons.append("phần việc chưa đủ Part/Estimate")
                    reason_str = ", ".join(reasons)
                    msg = (
                        f"Luồng '{flow.code or flow.summary}' ({story_key}) "
                        f"ở lại trạng thái Analyzing vì {reason_str}"
                    )
                    warnings.append(msg)

    except Exception as exc:  # noqa: BLE001
        return ApplyResult(
            ok=False,
            message=f"Áp dụng plan thất bại: {exc}",
            changes=changes,
            plan=working_plan,
            created=created,
            updated=updated,
            warnings=warnings,
        )

    msg = (
        "Kiểm tra kế hoạch thành công (dry-run)."
        if dry_run
        else "Áp dụng kế hoạch thành công."
    )
    return ApplyResult(
        ok=True,
        message=msg,
        changes=changes,
        plan=working_plan,
        created=created,
        updated=updated,
        warnings=warnings,
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
        "subtasks",
        "issuetype",
        "status",
        "description",
        "assignee",
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

        if not it_name or it_name in story_types:
            key = child.get("key", "")
            summary_raw = c_fields.get("summary", "")
            codes, title = split_codes(summary_raw)
            code = codes[0] if codes else None

            # Actor = code part of the single Component (else null)
            components = c_fields.get("components") or []
            actor = None
            if len(components) == 1:
                comp_raw = components[0]
                comp_name = (
                    comp_raw.get("name", "")
                    if isinstance(comp_raw, dict)
                    else str(comp_raw)
                )
                if " - " in comp_name:
                    actor = comp_name.split(" - ", 1)[0].strip()
                else:
                    actor = comp_name.strip()

            # Due Date
            raw_due = c_fields.get("duedate")
            due = None
            if raw_due:
                try:
                    due = date.fromisoformat(str(raw_due)[:10])
                except ValueError:
                    due = None

            # Assignee (BA)
            raw_assignee = c_fields.get("assignee")
            ba = (
                raw_assignee.get("name")
                if isinstance(raw_assignee, dict)
                else raw_assignee
            )

            # QC Owner
            raw_qc = c_fields.get(qc_fid) if qc_fid else None
            qc = raw_qc.get("name") if isinstance(raw_qc, dict) else raw_qc

            # Parts (Dev Task sub-tasks; Bug sub-tasks ignored)
            parts: list[PlanPart] = []
            for st in c_fields.get("subtasks", []):
                st_fields = st.get("fields", {})
                st_type = st_fields.get("issuetype", {}).get("name")
                if st_type in bug_sub_types:
                    continue
                if dev_task_types and st_type and st_type not in dev_task_types:
                    continue

                raw_part = st_fields.get(part_field_id)
                part_val = (
                    raw_part.get("value") if isinstance(raw_part, dict) else raw_part
                )

                raw_st_assignee = st_fields.get("assignee")
                st_assignee = (
                    raw_st_assignee.get("name")
                    if isinstance(raw_st_assignee, dict)
                    else raw_st_assignee
                )

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
                    code=code,
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
