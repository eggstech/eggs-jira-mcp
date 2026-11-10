from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from .config import EggsConfig
from .gateway import JiraGateway
from .plan import Plan, split_codes

logger = logging.getLogger(__name__)


@dataclass
class PlanContext:
    today: date
    components: dict[str, str]  # code -> full name
    part_options: list[str]
    epic_issue: dict[str, Any] | None
    epic_stories: list[dict[str, Any]]
    known_users: set[str]
    unknown_users: set[str]


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
    epic_stories: list[dict[str, Any]] = []
    epic_key = plan.epic.existing or plan.epic.key
    if epic_key:
        try:
            epic_issue = gateway.get_issue(epic_key)
        except Exception:  # noqa: BLE001
            epic_issue = None

        if epic_issue is not None:
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

            try:
                children = gateway.search(jql_epic, fields=search_fields)
            except Exception:  # noqa: BLE001
                children = []

            story_types = set(cfg.issue_types.get("story", ["Story"]))
            epic_stories = [
                child
                for child in children
                if not child.get("fields", {}).get("issuetype")
                or child.get("fields", {}).get("issuetype", {}).get("name")
                in story_types
            ]

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
        epic_stories=epic_stories,
        known_users=known_users,
        unknown_users=unknown_users,
    )


def validate_plan(plan: Plan, ctx: PlanContext, cfg: EggsConfig) -> ValidationReport:
    """Validate a plan against Jira context and business rules."""
    new_plan = copy.deepcopy(plan)
    findings: list[Finding] = []
    questions: list[str] = list(new_plan.open_questions)

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
    else:
        if not new_plan.epic.due:
            msg = "Epic mới bắt buộc phải có ngày hạn (due)"
            findings.append(Finding("error", "epic.due", msg))
        if not new_plan.epic.owner:
            msg = "Epic mới bắt buộc phải có người phụ trách (owner)"
            findings.append(Finding("error", "epic.owner", msg))

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

    # 2. Auto code generation for flows with code=None
    max_nn: dict[str, int] = {}
    for story in ctx.epic_stories:
        ext_codes, _ = split_codes(story.get("fields", {}).get("summary", ""))
        for c in ext_codes:
            if "-" in c:
                prefix, num = c.split("-", 1)
                if num.isdigit():
                    max_nn[prefix] = max(max_nn.get(prefix, 0), int(num))

    for flow in new_plan.flows:
        if flow.code and "-" in flow.code:
            prefix, num = flow.code.split("-", 1)
            if num.isdigit():
                max_nn[prefix] = max(max_nn.get(prefix, 0), int(num))

    for flow in new_plan.flows:
        if flow.code is None and flow.actor and flow.actor in ctx.components:
            curr = max_nn.get(flow.actor, 0) + 1
            max_nn[flow.actor] = curr
            flow.code = f"{flow.actor}-{curr:02d}"

    # 3. Flow rules
    seen_plan_codes: dict[str, int] = {}
    for i, flow in enumerate(new_plan.flows):
        label = flow.code or flow.summary or f"flows[{i}]"

        # Actor
        if not flow.actor or flow.actor not in ctx.components:
            allowed = ", ".join(sorted(ctx.components.keys()))
            msg = (
                f"Luồng '{label}': Actor '{flow.actor}' không hợp lệ. "
                f"Các Actor hợp lệ: {allowed}"
            )
            findings.append(Finding("error", f"flows[{i}].actor", msg))

        # Platform summary
        _, title = split_codes(flow.summary)
        if re.search(
            r"^(FE|BE|APP|API|WEB|CMS|POS|Backend|Frontend|Mobile)\b",
            title.strip(),
            re.IGNORECASE,
        ):
            lead_word = title.strip().split()[0] if title.strip() else ""
            msg = (
                f"Luồng '{label}': Tên luồng được đặt theo nền tảng kỹ thuật "
                f"({lead_word}), vui lòng đặt theo luồng nghiệp vụ"
            )
            findings.append(Finding("error", f"flows[{i}].summary", msg))

        # AC empty
        if not flow.ac and not flow.keep_description:
            msg = f"Luồng '{label}': Chưa có tiêu chí nghiệm thu (AC)"
            findings.append(Finding("error", f"flows[{i}].ac", msg))

        # Code format & actor prefix
        if flow.code:
            if not re.match(r"^[A-Z]+-\d{2,}$", flow.code):
                msg = f"Mã luồng '{flow.code}' không đúng định dạng ^[A-Z]+-\\d{{2,}}$"
                findings.append(Finding("error", f"flows[{i}].code", msg))
            else:
                prefix = flow.code.split("-")[0]
                if flow.actor and prefix != flow.actor:
                    msg = (
                        f"Mã luồng '{flow.code}' có tiền tố không khớp với Actor "
                        f"'{flow.actor}'"
                    )
                    findings.append(Finding("error", f"flows[{i}].code", msg))

            # Duplicate inside plan
            if flow.code in seen_plan_codes:
                prev_i = seen_plan_codes[flow.code]
                msg = (
                    f"Mã luồng '{flow.code}' bị trùng lặp trong plan "
                    f"(trùng với flows[{prev_i}])"
                )
                findings.append(Finding("error", f"flows[{i}].code", msg))
            else:
                seen_plan_codes[flow.code] = i

            # Duplicate against Epic stories
            for story in ctx.epic_stories:
                ext_codes, _ = split_codes(story.get("fields", {}).get("summary", ""))
                ext_key = story.get("key")
                if flow.code in ext_codes and flow.key != ext_key:
                    msg = (
                        f"Mã luồng '{flow.code}' bị trùng với Story đã có "
                        f"{ext_key} trong Epic"
                    )
                    findings.append(Finding("error", f"flows[{i}].code", msg))

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
    for story in ctx.epic_stories:
        c_list, _ = split_codes(story.get("fields", {}).get("summary", ""))
        valid_codes.update(c_list)

    for i, flow in enumerate(new_plan.flows):
        label = flow.code or flow.summary or f"flows[{i}]"
        for dep in flow.depends_on:
            if dep not in valid_codes:
                msg = (
                    f"Luồng '{label}': Phụ thuộc vào mã '{dep}' không tồn tại "
                    "trong plan hay Epic"
                )
                findings.append(Finding("error", f"flows[{i}].depends_on", msg))

    # 6. Warnings
    for i, flow in enumerate(new_plan.flows):
        label = flow.code or flow.summary or f"flows[{i}]"
        # TBD in AC
        for ac_item in flow.ac:
            if ac_item.strip().startswith("TBD"):
                msg = (
                    f"Luồng '{label}': AC có mục TBD ('{ac_item}'), "
                    "Story sẽ ở trạng thái Analyzing"
                )
                findings.append(Finding("warning", f"flows[{i}].ac", msg))
                q_text = f"[{label}]: {ac_item}"
                if q_text not in questions and ac_item not in questions:
                    questions.append(q_text)

        # QC missing
        if not flow.qc:
            msg = (
                f"Luồng '{label}': Chưa gán QC Owner, Story sẽ ở lại "
                "trạng thái Analyzing"
            )
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
            if "## Acceptance Criteria" not in desc:
                msg = (
                    f"Luồng '{label}': keep_description=true nhưng description "
                    f"của Story {flow.key} không có tiêu đề '## Acceptance Criteria'"
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
