from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from .actions import has_ac_heading
from .config import EggsConfig
from .gateway import JiraGateway
from .plan import Plan, split_codes, split_codes_exact

logger = logging.getLogger(__name__)

_PLATFORM_TITLE_RE = re.compile(r"^(FE|BE|APP|WEB|API)\b")


@dataclass
class PlanContext:
    today: date
    components: dict[str, str]  # code -> full name
    part_options: list[str]
    epic_issue: dict[str, Any] | None
    epic_stories: list[dict[str, Any]]  # every Epic child, with its type
    known_users: set[str]
    unknown_users: set[str]
    epic_children_error: str | None = None
    # flow index -> key of the existing Epic child the flow resolves to
    resolved_flow_keys: dict[int, str] = field(default_factory=dict)
    # existing child key -> eggs.flowCode read once per run
    flow_codes: dict[str, str] = field(default_factory=dict)
    # Story key -> Dev Task keys, only populated when a plan part carries a key
    story_dev_task_keys: dict[str, set[str]] = field(default_factory=dict)
    story_dev_task_errors: dict[str, str] = field(default_factory=dict)
    # Story key -> error message when its eggs.flowCode could not be read
    flow_code_errors: dict[str, str] = field(default_factory=dict)
    # flow index -> (Story key, its codes) for a title match with another code
    title_conflicts: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Finding:
    level: Literal["error", "warning"]
    path: str
    message: str


@dataclass
class ValidationReport:
    findings: list[Finding]
    questions: list[str]
    plan: Plan  # with auto codes filled
    tree: str

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)


def _issuetype(issue: dict[str, Any]) -> str | None:
    value = issue.get("fields", {}).get("issuetype", {}).get("name")
    return value if isinstance(value, str) else None


def _component_names(issue: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for comp in issue.get("fields", {}).get("components") or []:
        if isinstance(comp, dict):
            name = comp.get("name")
            if name:
                names.append(name)
    return names


def _story_children(ctx: PlanContext, cfg: EggsConfig) -> list[dict[str, Any]]:
    story_types = set(cfg.issue_types.get("story", ["Story"]))
    return [
        child
        for child in ctx.epic_stories
        if not _issuetype(child) or _issuetype(child) in story_types
    ]


def _adhoc_children(ctx: PlanContext, cfg: EggsConfig) -> list[dict[str, Any]]:
    adhoc_types = set(cfg.issue_types.get("adhoc", ["Ad-hoc Task"]))
    return [child for child in ctx.epic_stories if _issuetype(child) in adhoc_types]


def _epic_jql(cfg: EggsConfig, epic_key: str) -> str:
    epic_link_id = cfg.fields.get("epic_link")
    if epic_link_id:
        cf_match = re.search(r"\d+", epic_link_id)
        cf_id = cf_match.group(0) if cf_match else epic_link_id
        return f'cf[{cf_id}] = "{epic_key}"'
    return f'"Epic Link" = "{epic_key}"'


def _child_search_fields(cfg: EggsConfig) -> list[str]:
    fields = [
        "summary",
        "components",
        "duedate",
        "issuetype",
        "status",
        "description",
        "assignee",
        "project",
    ]
    if cfg.fields.get("qc_owner"):
        fields.append(cfg.fields["qc_owner"])
    if cfg.fields.get("epic_link"):
        fields.append(cfg.fields["epic_link"])
    return fields


def _read_flow_code(gateway: JiraGateway, key: str) -> tuple[str | None, str | None]:
    """Return (eggs.flowCode, read error) so errors are never read as "unset"."""
    getter = getattr(gateway, "get_issue_property", None)
    if getter is None:
        return None, None
    try:
        value = getter(key, "eggs.flowCode")
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    return (value if isinstance(value, str) and value else None), None


def _resolve_unkeyed_flows(
    plan: Plan,
    ctx_children: list[dict[str, Any]],
    components: dict[str, str],
    flow_codes: dict[str, str],
    cfg: EggsConfig,
) -> tuple[dict[int, str], dict[int, dict[str, Any]]]:
    """Match unkeyed flows to existing Epic Stories (item 5).

    Returns the resolved keys and any title matches rejected because the
    candidate carries a different flow code (I6).
    """
    story_children = [
        child
        for child in ctx_children
        if not _issuetype(child)
        or _issuetype(child) in set(cfg.issue_types.get("story", ["Story"]))
    ]
    claimed = {flow.key for flow in plan.flows if flow.key}
    resolved: dict[int, str] = {}
    conflicts: dict[int, dict[str, Any]] = {}

    for idx, flow in enumerate(plan.flows):
        if flow.key:
            continue
        child = None

        if flow.code:
            for candidate in story_children:
                key = candidate.get("key", "")
                if key in claimed:
                    continue
                summary_codes, _ = split_codes(
                    candidate.get("fields", {}).get("summary", "")
                )
                if flow_codes.get(key) == flow.code or flow.code in summary_codes:
                    child = candidate
                    break

        if child is None:
            _, flow_title = split_codes_exact(flow.summary)
            actor_component = components.get(flow.actor) if flow.actor else None
            for candidate in story_children:
                key = candidate.get("key", "")
                if key in claimed:
                    continue
                candidate_codes, candidate_title = split_codes_exact(
                    candidate.get("fields", {}).get("summary", "")
                )
                if candidate_title != flow_title:
                    continue
                if actor_component and actor_component not in _component_names(
                    candidate
                ):
                    continue
                if not actor_component and _component_names(candidate):
                    continue
                # Rule 5(c) must not re-identify a Story under another code.
                if flow.code and candidate_codes and flow.code not in candidate_codes:
                    conflicts.setdefault(idx, {"key": key, "codes": candidate_codes})
                    continue
                child = candidate
                break

        if child is not None:
            resolved[idx] = child.get("key", "")
            claimed.add(child.get("key", ""))

    return resolved, conflicts


def load_plan_context(
    gateway: JiraGateway, cfg: EggsConfig, plan: Plan, today: date
) -> PlanContext:
    """Load Jira context required to validate a plan."""
    components_list = gateway.components(plan.project)
    components: dict[str, str] = {}
    for c in components_list:
        if " - " in c:
            code, _ = c.split(" - ", 1)
            components[code.strip()] = c.strip()
        else:
            components[c.strip()] = c.strip()

    dev_task_types = cfg.issue_types.get("dev_task", ["Dev Task"])
    dev_task_type = (
        dev_task_types[0]
        if isinstance(dev_task_types, list) and dev_task_types
        else "Dev Task"
    )
    part_field = cfg.fields.get("part", "customfield_10701")
    part_options = gateway.field_allowed_values(plan.project, dev_task_type, part_field)

    epic_issue: dict[str, Any] | None = None
    epic_children: list[dict[str, Any]] = []
    children_error: str | None = None
    epic_key = plan.epic.existing or plan.epic.key
    if epic_key:
        try:
            epic_issue = gateway.get_issue(epic_key)
        except Exception:  # noqa: BLE001
            epic_issue = None

        if epic_issue is not None:
            try:
                epic_children = gateway.search(
                    _epic_jql(cfg, epic_key), fields=_child_search_fields(cfg)
                )
            except Exception as exc:  # noqa: BLE001
                children_error = str(exc)
                epic_children = []

    flow_codes: dict[str, str] = {}
    flow_code_errors: dict[str, str] = {}
    if any(not flow.key for flow in plan.flows):
        story_types = set(cfg.issue_types.get("story", ["Story"]))
        for child in epic_children:
            it_name = _issuetype(child)
            if it_name and it_name not in story_types:
                continue
            child_key = child.get("key")
            if not child_key:
                continue
            flow_code, read_error = _read_flow_code(gateway, child_key)
            if flow_code:
                flow_codes[child_key] = flow_code
            if read_error:
                flow_code_errors[child_key] = read_error

    resolved, title_conflicts = _resolve_unkeyed_flows(
        plan, epic_children, components, flow_codes, cfg
    )

    story_dev_task_keys: dict[str, set[str]] = {}
    story_dev_task_errors: dict[str, str] = {}
    for idx, flow in enumerate(plan.flows):
        effective_key = flow.key or resolved.get(idx)
        if not effective_key or not any(part.key for part in flow.parts):
            continue
        if effective_key in story_dev_task_keys:
            continue
        try:
            dev_tasks = gateway.search(
                f'parent = "{effective_key}"',
                fields=["key", "issuetype", "summary"],
            )
        except Exception as exc:  # noqa: BLE001
            story_dev_task_errors[effective_key] = str(exc)
            continue
        dev_task_type_names = set(cfg.issue_types.get("dev_task", ["Dev Task"]))
        story_dev_task_keys[effective_key] = {
            task.get("key", "")
            for task in dev_tasks
            if task.get("key")
            and (not _issuetype(task) or _issuetype(task) in dev_task_type_names)
        }

    candidates: set[str] = set()
    if plan.epic.owner:
        candidates.add(plan.epic.owner)
    for flow in plan.flows:
        if flow.ba:
            candidates.add(flow.ba)
        if flow.qc:
            candidates.add(flow.qc)
        for part in flow.parts:
            if part.assignee:
                candidates.add(part.assignee)
    for tt in plan.tech_tasks:
        if tt.assignee:
            candidates.add(tt.assignee)

    known_users: set[str] = set()
    unknown_users: set[str] = set()
    for u in candidates:
        if u:
            try:
                if gateway.user_exists(u):
                    known_users.add(u)
                else:
                    unknown_users.add(u)
            except Exception:  # noqa: BLE001
                unknown_users.add(u)

    return PlanContext(
        today=today,
        components=components,
        part_options=part_options,
        epic_issue=epic_issue,
        epic_stories=epic_children,
        known_users=known_users,
        unknown_users=unknown_users,
        epic_children_error=children_error,
        resolved_flow_keys=resolved,
        flow_codes=flow_codes,
        flow_code_errors=flow_code_errors,
        title_conflicts=title_conflicts,
        story_dev_task_keys=story_dev_task_keys,
        story_dev_task_errors=story_dev_task_errors,
    )


def _creates_cycle(flow_code: str, graph: dict[str, list[str]]) -> bool:
    """True when following depends_on from flow_code returns to flow_code."""
    stack = list(graph.get(flow_code, []))
    seen: set[str] = set()
    while stack:
        code = stack.pop()
        if code == flow_code:
            return True
        if code in seen:
            continue
        seen.add(code)
        stack.extend(graph.get(code, []))
    return False


def validate_plan(plan: Plan, ctx: PlanContext, cfg: EggsConfig) -> ValidationReport:
    """Validate a plan against Jira context and business rules."""
    new_plan = copy.deepcopy(plan)
    findings: list[Finding] = []
    questions: list[str] = list(new_plan.open_questions)

    story_children = _story_children(ctx, cfg)

    # 1. Epic validation
    if new_plan.epic.existing:
        if ctx.epic_issue is None:
            msg = f"Không tìm thấy Epic {new_plan.epic.existing} trên Jira"
            findings.append(Finding("error", "epic.existing", msg))
        else:
            it_name = ctx.epic_issue.get("fields", {}).get("issuetype", {}).get("name")
            epic_types = cfg.issue_types.get("epic", ["Epic"])
            if it_name and it_name not in epic_types:
                msg = (
                    f"Issue {new_plan.epic.existing} có loại '{it_name}', "
                    "không phải là Epic"
                )
                findings.append(Finding("error", "epic.existing", msg))

            project_key = (
                ctx.epic_issue.get("fields", {}).get("project", {}).get("key")
                if isinstance(ctx.epic_issue.get("fields", {}).get("project"), dict)
                else None
            )
            key_prefix = new_plan.epic.existing.split("-")[0]
            if key_prefix.upper() != new_plan.project.upper() or (
                project_key and project_key.upper() != new_plan.project.upper()
            ):
                msg = (
                    f"Epic {new_plan.epic.existing} không thuộc dự án "
                    f"{new_plan.project}"
                )
                findings.append(Finding("error", "epic.project", msg))

            if ctx.epic_children_error:
                msg = (
                    f"Không tải được danh sách Story của Epic "
                    f"{new_plan.epic.existing}: {ctx.epic_children_error}"
                )
                findings.append(Finding("error", "epic.children", msg))

            raw_due = ctx.epic_issue.get("fields", {}).get("duedate")
            raw_owner = ctx.epic_issue.get("fields", {}).get("assignee")
            owner = raw_owner.get("name") if isinstance(raw_owner, dict) else raw_owner
            if not new_plan.epic.due and not raw_due:
                findings.append(
                    Finding(
                        "warning",
                        "epic.due",
                        f"Epic {new_plan.epic.existing} chưa có ngày hạn (due)",
                    )
                )
            if not new_plan.epic.owner and not owner:
                findings.append(
                    Finding(
                        "warning",
                        "epic.owner",
                        f"Epic {new_plan.epic.existing} chưa có người phụ trách",
                    )
                )
    else:
        if not new_plan.epic.due:
            msg = "Epic mới bắt buộc phải có ngày hạn (due)"
            findings.append(Finding("error", "epic.due", msg))
        if not new_plan.epic.owner:
            msg = "Epic mới bắt buộc phải có người phụ trách (owner)"
            findings.append(Finding("error", "epic.owner", msg))

        # A plan that only carries epic.key still points at a real Epic (M2).
        if new_plan.epic.key and ctx.epic_issue is not None:
            it_name = ctx.epic_issue.get("fields", {}).get("issuetype", {}).get("name")
            epic_types = cfg.issue_types.get("epic", ["Epic"])
            if it_name and it_name not in epic_types:
                msg = (
                    f"Issue {new_plan.epic.key} có loại '{it_name}', không phải là Epic"
                )
                findings.append(Finding("error", "epic.key", msg))
            raw_project = ctx.epic_issue.get("fields", {}).get("project")
            project_key = (
                raw_project.get("key") if isinstance(raw_project, dict) else None
            )
            key_prefix = new_plan.epic.key.split("-")[0]
            if key_prefix.upper() != new_plan.project.upper() or (
                project_key and project_key.upper() != new_plan.project.upper()
            ):
                msg = f"Epic {new_plan.epic.key} không thuộc dự án {new_plan.project}"
                findings.append(Finding("error", "epic.project", msg))

    if new_plan.epic.owner and new_plan.epic.owner in ctx.unknown_users:
        msg = f"User '{new_plan.epic.owner}' không tồn tại trên Jira"
        findings.append(Finding("error", "epic.owner", msg))

    epic_due = new_plan.epic.due
    if not epic_due and ctx.epic_issue:
        raw_due = ctx.epic_issue.get("fields", {}).get("duedate")
        if raw_due:
            try:
                epic_due = date.fromisoformat(str(raw_due)[:10])
            except ValueError:
                pass

    # 2. Adopt existing Stories for unkeyed flows, then auto-code
    for i, flow in enumerate(new_plan.flows):
        if not flow.key and i in ctx.resolved_flow_keys:
            flow.key = ctx.resolved_flow_keys[i]
        if flow.key and not flow.code:
            for child in story_children:
                if child.get("key") != flow.key:
                    continue
                child_codes, _ = split_codes(child.get("fields", {}).get("summary", ""))
                if child_codes:
                    flow.codes = child_codes
                    flow.code = child_codes[0]
                break

    for i, flow in enumerate(new_plan.flows):
        if flow.key:
            continue
        conflict = ctx.title_conflicts.get(i)
        if conflict:
            codes_str = ", ".join(conflict["codes"])
            msg = (
                f"Luồng '{flow.code or flow.summary}': Story {conflict['key']} "
                f"có mã [{codes_str}] khác với mã luồng '{flow.code}'. "
                "Hãy sửa code hoặc đặt key cho luồng."
            )
            findings.append(Finding("error", f"flows[{i}].key", msg))
            continue
        if ctx.flow_code_errors:
            detail = "; ".join(
                f"{key}: {error}" for key, error in ctx.flow_code_errors.items()
            )
            msg = (
                f"Luồng '{flow.code or flow.summary}': không đọc được thuộc tính "
                f"eggs.flowCode của Story trong Epic ({detail}), không thể xác định "
                "luồng đã tồn tại hay chưa."
            )
            findings.append(Finding("error", f"flows[{i}].key", msg))

    max_nn: dict[str, int] = {}
    for story in story_children:
        ext_codes, _ = split_codes(story.get("fields", {}).get("summary", ""))
        for c in ext_codes:
            if "-" in c:
                prefix, num = c.split("-", 1)
                if num.isdigit():
                    max_nn[prefix] = max(max_nn.get(prefix, 0), int(num))

    for flow in new_plan.flows:
        for code in flow.codes or ([flow.code] if flow.code else []):
            if "-" in code:
                prefix, num = code.split("-", 1)
                if num.isdigit():
                    max_nn[prefix] = max(max_nn.get(prefix, 0), int(num))

    for flow in new_plan.flows:
        if flow.code is None and flow.actor and flow.actor in ctx.components:
            curr = max_nn.get(flow.actor, 0) + 1
            max_nn[flow.actor] = curr
            flow.code = f"{flow.actor}-{curr:02d}"
            flow.codes = [flow.code]

    # 3. Flow rules
    seen_plan_codes: dict[str, int] = {}
    epic_child_keys = {child.get("key") for child in ctx.epic_stories}
    for i, flow in enumerate(new_plan.flows):
        label = flow.code or flow.summary or f"flows[{i}]"

        # Actor
        if not flow.actor or flow.actor not in ctx.components:
            allowed = ", ".join(sorted(ctx.components.keys()))
            msg = (
                f"Chưa có Component {flow.actor} trong {plan.project}. "
                f"Chọn một Component có sẵn: {allowed} hoặc "
                "tạo mới bằng eggs_create_component."
            )
            findings.append(Finding("error", f"flows[{i}].actor", msg))

        # Platform summary: uppercase tokens only; legacy keyed Stories are kept
        _, title = split_codes_exact(flow.summary)
        if _PLATFORM_TITLE_RE.match(title.strip()):
            lead_word = title.strip().split()[0] if title.strip() else ""
            msg = (
                f"Luồng '{label}': Tên luồng được đặt theo nền tảng kỹ thuật "
                f"({lead_word}), vui lòng đặt theo luồng nghiệp vụ"
            )
            level: Literal["error", "warning"] = "warning" if flow.key else "error"
            findings.append(Finding(level, f"flows[{i}].summary", msg))

        # AC empty
        if not flow.ac and not flow.keep_description:
            msg = f"Luồng '{label}': Chưa có tiêu chí nghiệm thu (AC)"
            findings.append(Finding("error", f"flows[{i}].ac", msg))

        # Code format & actor prefix, duplicates
        codes_to_check = flow.codes or ([flow.code] if flow.code else [])
        if flow.code and not flow.codes:
            flow.codes = [flow.code]
        for code in codes_to_check:
            if not re.match(r"^[A-Z]+-\d{2,}$", code):
                msg = f"Mã luồng '{code}' không đúng định dạng ^[A-Z]+-\\d{{2,}}$"
                findings.append(Finding("error", f"flows[{i}].code", msg))
            else:
                prefix = code.split("-")[0]
                if flow.actor and prefix != flow.actor:
                    msg = (
                        f"Mã luồng '{code}' có tiền tố không khớp với Actor "
                        f"'{flow.actor}'"
                    )
                    findings.append(Finding("error", f"flows[{i}].code", msg))

            # Duplicate inside plan
            if code in seen_plan_codes:
                prev_i = seen_plan_codes[code]
                msg = (
                    f"Mã luồng '{code}' bị trùng lặp trong plan "
                    f"(trùng với flows[{prev_i}])"
                )
                findings.append(Finding("error", f"flows[{i}].code", msg))
            else:
                seen_plan_codes[code] = i

            # Duplicate against Epic Stories (same issue is not a duplicate)
            for story in story_children:
                ext_codes, _ = split_codes(story.get("fields", {}).get("summary", ""))
                ext_key = story.get("key")
                if code in ext_codes and flow.key != ext_key:
                    msg = (
                        f"Mã luồng '{code}' bị trùng với Story đã có "
                        f"{ext_key} trong Epic"
                    )
                    findings.append(Finding("error", f"flows[{i}].code", msg))

        # Key must belong to this Epic
        if (
            flow.key
            and ctx.epic_issue is not None
            and not ctx.epic_children_error
            and flow.key not in epic_child_keys
        ):
            msg = (
                f"Luồng '{label}': Story {flow.key} không thuộc Epic "
                f"{new_plan.epic.existing or new_plan.epic.key}"
            )
            findings.append(Finding("error", f"flows[{i}].key", msg))

        # Due before today for new flow
        if flow.key is None and flow.due is not None and flow.due < ctx.today:
            msg = (
                f"Luồng '{label}': Ngày hạn ({flow.due}) trước ngày hôm nay "
                f"({ctx.today})"
            )
            findings.append(Finding("error", f"flows[{i}].due", msg))

        # Unknown users
        if flow.ba and flow.ba in ctx.unknown_users:
            msg = f"Luồng '{label}': User BA '{flow.ba}' không tồn tại trên Jira"
            findings.append(Finding("error", f"flows[{i}].ba", msg))
        if flow.qc and flow.qc in ctx.unknown_users:
            msg = f"Luồng '{label}': User QC '{flow.qc}' không tồn tại trên Jira"
            findings.append(Finding("error", f"flows[{i}].qc", msg))

        # Parts
        for j, part in enumerate(flow.parts):
            if not part.part:
                msg = f"Luồng '{label}': Phần việc thứ {j + 1} thiếu Part"
                findings.append(Finding("error", f"flows[{i}].parts[{j}].part", msg))
            elif part.part not in ctx.part_options:
                opts = ", ".join(ctx.part_options)
                msg = (
                    f"Luồng '{label}': Part '{part.part}' không hợp lệ. "
                    f"Các lựa chọn: {opts}"
                )
                findings.append(Finding("error", f"flows[{i}].parts[{j}].part", msg))

            if not part.estimate:
                msg = f"Luồng '{label}': Phần việc thứ {j + 1} thiếu Estimate"
                findings.append(
                    Finding("error", f"flows[{i}].parts[{j}].estimate", msg)
                )
            elif not re.match(r"^(\d+[wdhm]\s*)+$", part.estimate.strip()):
                msg = (
                    f"Luồng '{label}': Estimate '{part.estimate}' không đúng "
                    "định dạng Jira duration (ví dụ: 1d, 4h, 30m)"
                )
                findings.append(
                    Finding("error", f"flows[{i}].parts[{j}].estimate", msg)
                )

            if part.assignee and part.assignee in ctx.unknown_users:
                msg = f"Luồng '{label}': User '{part.assignee}' không tồn tại trên Jira"
                findings.append(
                    Finding("error", f"flows[{i}].parts[{j}].assignee", msg)
                )

            if part.key:
                if not flow.key:
                    msg = (
                        f"Luồng '{label}': Part có key {part.key} nhưng luồng "
                        "chưa có Story key"
                    )
                    findings.append(Finding("error", f"flows[{i}].parts[{j}].key", msg))
                elif flow.key in ctx.story_dev_task_errors:
                    continue
                elif part.key not in ctx.story_dev_task_keys.get(flow.key, set()):
                    msg = (
                        f"Luồng '{label}': Dev Task {part.key} không thuộc "
                        f"Story {flow.key}"
                    )
                    findings.append(Finding("error", f"flows[{i}].parts[{j}].key", msg))

    # 4. Tech tasks
    for i, tt in enumerate(new_plan.tech_tasks):
        if tt.assignee and tt.assignee in ctx.unknown_users:
            msg = (
                f"Tech Task '{tt.summary}': User '{tt.assignee}' không "
                "tồn tại trên Jira"
            )
            findings.append(Finding("error", f"tech_tasks[{i}].assignee", msg))
        if tt.estimate and not re.match(r"^(\d+[wdhm]\s*)+$", tt.estimate.strip()):
            msg = (
                f"Tech Task '{tt.summary}': Estimate '{tt.estimate}' không đúng "
                "định dạng Jira duration"
            )
            findings.append(Finding("error", f"tech_tasks[{i}].estimate", msg))

    # 5. Depends on checking
    valid_codes = set()
    for f in new_plan.flows:
        if f.code:
            valid_codes.add(f.code)
        valid_codes.update(f.codes)
    for story in story_children:
        c_list, _ = split_codes(story.get("fields", {}).get("summary", ""))
        valid_codes.update(c_list)

    graph = {f.code: list(f.depends_on) for f in new_plan.flows if f.code}
    for i, flow in enumerate(new_plan.flows):
        label = flow.code or flow.summary or f"flows[{i}]"
        for dep in flow.depends_on:
            if dep in flow.codes or dep == flow.code:
                msg = f"Luồng '{label}': Không thể phụ thuộc vào chính nó ({dep})"
                findings.append(Finding("error", f"flows[{i}].depends_on", msg))
            elif dep not in valid_codes:
                msg = (
                    f"Luồng '{label}': Phụ thuộc vào mã '{dep}' không tồn tại "
                    "trong plan hay Epic"
                )
                findings.append(Finding("error", f"flows[{i}].depends_on", msg))
        if flow.code and _creates_cycle(flow.code, graph):
            msg = f"Luồng '{label}': Phụ thuộc tạo vòng (cycle) giữa các luồng"
            findings.append(Finding("error", f"flows[{i}].depends_on", msg))

    # 6. Warnings
    for i, flow in enumerate(new_plan.flows):
        label = flow.code or flow.summary or f"flows[{i}]"
        # TBD in AC (case-insensitive)
        for ac_item in flow.ac:
            if ac_item.strip().upper().startswith("TBD"):
                msg = (
                    f"Luồng '{label}': AC có mục TBD ('{ac_item}'), "
                    "Story sẽ chưa sang Ready For Dev"
                )
                findings.append(Finding("warning", f"flows[{i}].ac", msg))
                q_text = f"[{label}]: {ac_item}"
                if q_text not in questions and ac_item not in questions:
                    questions.append(q_text)

        # QC missing
        if not flow.qc:
            msg = f"Luồng '{label}': Chưa gán QC Owner, Story sẽ ở lại trạng thái Open"
            findings.append(Finding("warning", f"flows[{i}].qc", msg))

        # Flow due after Epic due
        if epic_due and flow.due and flow.due > epic_due:
            msg = (
                f"Luồng '{label}': Ngày hạn ({flow.due}) sau ngày hạn của "
                f"Epic ({epic_due})"
            )
            findings.append(Finding("warning", f"flows[{i}].due", msg))

        # keep_description true and no AC heading
        if flow.keep_description:
            desc = ""
            for story in ctx.epic_stories:
                if story.get("key") == flow.key:
                    desc = story.get("fields", {}).get("description") or ""
                    break
            if not has_ac_heading(desc):
                msg = (
                    f"Luồng '{label}': keep_description=true nhưng description "
                    f"của Story {flow.key} không có tiêu đề 'h2. Acceptance Criteria'"
                )
                findings.append(Finding("warning", f"flows[{i}].keep_description", msg))

        # Part assignee null
        for j, part in enumerate(flow.parts):
            if not part.assignee:
                msg = (
                    f"Luồng '{label}': Phần việc {part.part or j + 1} chưa có "
                    "người nhận (assignee)"
                )
                findings.append(
                    Finding("warning", f"flows[{i}].parts[{j}].assignee", msg)
                )

    # 7. Build tree
    lines: list[str] = []
    epic_label = new_plan.epic.existing or new_plan.epic.key or "NEW"
    epic_sum = new_plan.epic.summary or (
        ctx.epic_issue.get("fields", {}).get("summary") if ctx.epic_issue else ""
    )
    lines.append(f"Epic: [{epic_label}] {epic_sum}".strip())
    for flow in new_plan.flows:
        parts_repr = [
            f"{p.part or '?'} {p.estimate or '?'} {p.assignee or '?'}"
            for p in flow.parts
        ]
        parts_str = ", ".join(parts_repr)
        lines.append(
            f"  [{flow.code or '?'}] {flow.summary} · {flow.actor or '?'} · "
            f"{flow.due or '?'} · parts: {parts_str}"
        )
    for tt in new_plan.tech_tasks:
        lines.append(
            f"  [Tech Task] {tt.summary} · {tt.estimate or '?'} · {tt.assignee or '?'}"
        )
    tree = "\n".join(lines)

    return ValidationReport(
        findings=findings,
        questions=questions,
        plan=new_plan,
        tree=tree,
    )
