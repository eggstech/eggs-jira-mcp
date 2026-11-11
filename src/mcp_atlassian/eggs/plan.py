from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yaml


class PlanError(ValueError):
    """Raised when a plan violates the schema or format rules."""

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


@dataclass
class PlanEpic:
    key: str | None = None
    existing: str | None = None
    summary: str | None = None
    due: date | None = None
    owner: str | None = None


@dataclass
class PlanPart:
    part: str | None = None
    assignee: str | None = None
    estimate: str | None = None
    key: str | None = None


@dataclass
class PlanFlow:
    code: str | None = None
    codes: list[str] = field(default_factory=list)
    actor: str | None = None
    summary: str = ""
    ac: list[str] = field(default_factory=list)
    due: date | None = None
    ba: str | None = None
    qc: str | None = None
    depends_on: list[str] = field(default_factory=list)
    parts: list[PlanPart] = field(default_factory=list)
    key: str | None = None
    keep_description: bool = False


@dataclass
class PlanTechTask:
    summary: str = ""
    assignee: str | None = None
    estimate: str | None = None
    key: str | None = None


@dataclass
class Plan:
    project: str = ""
    epic: PlanEpic = field(default_factory=PlanEpic)
    flows: list[PlanFlow] = field(default_factory=list)
    tech_tasks: list[PlanTechTask] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    other_issues: list[dict[str, Any]] = field(default_factory=list)


def _parse_date(val: Any, path: str) -> date | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        try:
            return date.fromisoformat(val_str)
        except ValueError as err:
            msg = f"Invalid date format (expected YYYY-MM-DD): {val!r}"
            raise PlanError(msg, path=path) from err
    msg = f"Expected date, got {type(val).__name__}"
    raise PlanError(msg, path=path)


def split_codes(summary: str) -> tuple[list[str], str]:
    """Extract code brackets matching [A-Z]+-\\d{2,} (comma-separated allowed).

    Non-code bracket prefixes (e.g. [DEMO], [HMKPOS]) are preserved in the title.
    """
    codes: list[str] = []

    def repl(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        if parts and all(re.match(r"^[A-Z]+-\d{2,}$", p) for p in parts):
            codes.extend(parts)
            return ""
        return m.group(0)

    # Replace bracketed tokens that contain valid codes
    cleaned = re.sub(r"\[([^\]]+)\]", repl, summary)
    # Clean up whitespace
    cleaned = re.sub(r" +", " ", cleaned).strip()
    return codes, cleaned


def split_codes_exact(summary: str) -> tuple[list[str], str]:
    """Extract codes but keep the title byte-for-byte (no whitespace rewrite)."""
    codes: list[str] = []

    def repl(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        if parts and all(re.match(r"^[A-Z]+-\d{2,}$", p) for p in parts):
            codes.extend(parts)
            return ""
        return m.group(0)

    cleaned = re.sub(r"\[([^\]]+)\]", repl, summary)
    return codes, cleaned.strip()


def format_summary(codes: list[str], title: str) -> str:
    """Format flow code(s) and title into Jira Story summary."""
    clean_title = title.strip()
    clean_codes = [c.strip() for c in codes if c and c.strip()]
    if not clean_codes:
        return clean_title
    code_str = f"[{','.join(clean_codes)}]"
    if clean_title:
        return f"{code_str} {clean_title}"
    return code_str


def parse_plan(text: str) -> Plan:
    """Parse YAML text into a Plan dataclass according to eggs-plan/v2 schema."""
    try:
        data = yaml.safe_load(text)
    except Exception as err:
        msg = f"YAML parsing error: {err}"
        raise PlanError(msg, path="") from err

    if not isinstance(data, dict):
        msg = "Plan root must be a YAML mapping"
        raise PlanError(msg, path="")

    schema = data.get("schema")
    if schema != "eggs-plan/v2":
        msg = f"Invalid schema: expected 'eggs-plan/v2', got {schema!r}"
        raise PlanError(msg, path="schema")

    project = str(data.get("project") or "").strip()

    epic_raw = data.get("epic")
    if epic_raw is None:
        epic = PlanEpic()
    elif isinstance(epic_raw, dict):
        epic = PlanEpic(
            key=epic_raw.get("key"),
            existing=epic_raw.get("existing"),
            summary=epic_raw.get("summary"),
            due=_parse_date(epic_raw.get("due"), "epic.due"),
            owner=epic_raw.get("owner"),
        )
    else:
        msg = "epic must be a mapping"
        raise PlanError(msg, path="epic")

    flows: list[PlanFlow] = []
    raw_flows = data.get("flows") or []
    if not isinstance(raw_flows, list):
        msg = "flows must be a list"
        raise PlanError(msg, path="flows")

    for i, raw_flow in enumerate(raw_flows):
        if not isinstance(raw_flow, dict):
            msg = "flow item must be a mapping"
            raise PlanError(msg, path=f"flows[{i}]")

        path_flow = f"flows[{i}]"
        raw_parts = raw_flow.get("parts") or []
        parts: list[PlanPart] = []
        if isinstance(raw_parts, list):
            for j, raw_part in enumerate(raw_parts):
                if isinstance(raw_part, dict):
                    est = raw_part.get("estimate")
                    parts.append(
                        PlanPart(
                            part=raw_part.get("part"),
                            assignee=raw_part.get("assignee"),
                            estimate=str(est) if est is not None else None,
                            key=raw_part.get("key"),
                        )
                    )
                else:
                    msg = "part item must be a mapping"
                    raise PlanError(msg, path=f"{path_flow}.parts[{j}]")
        else:
            msg = "parts must be a list"
            raise PlanError(msg, path=f"{path_flow}.parts")

        raw_ac = raw_flow.get("ac") or []
        ac = [str(item) for item in raw_ac] if isinstance(raw_ac, list) else []

        raw_depends = raw_flow.get("depends_on") or []
        depends_on = (
            [str(item) for item in raw_depends] if isinstance(raw_depends, list) else []
        )

        raw_codes = raw_flow.get("codes")
        codes = (
            [str(c).strip() for c in raw_codes if str(c).strip()]
            if isinstance(raw_codes, list)
            else []
        )
        raw_code = raw_flow.get("code")
        if not codes and raw_code:
            codes = [str(raw_code).strip()]
        elif codes and not raw_code:
            raw_code = codes[0]

        flow = PlanFlow(
            code=raw_code,
            codes=codes,
            actor=raw_flow.get("actor"),
            summary=str(raw_flow.get("summary") or ""),
            ac=ac,
            due=_parse_date(raw_flow.get("due"), f"{path_flow}.due"),
            ba=raw_flow.get("ba"),
            qc=raw_flow.get("qc"),
            depends_on=depends_on,
            parts=parts,
            key=raw_flow.get("key"),
            keep_description=bool(raw_flow.get("keep_description", False)),
        )
        flows.append(flow)

    tech_tasks: list[PlanTechTask] = []
    raw_tech_tasks = data.get("tech_tasks") or []
    if isinstance(raw_tech_tasks, list):
        for i, raw_tt in enumerate(raw_tech_tasks):
            if isinstance(raw_tt, dict):
                est = raw_tt.get("estimate")
                tech_tasks.append(
                    PlanTechTask(
                        summary=str(raw_tt.get("summary") or ""),
                        assignee=raw_tt.get("assignee"),
                        estimate=str(est) if est is not None else None,
                        key=raw_tt.get("key"),
                    )
                )
            else:
                msg = "tech_task item must be a mapping"
                raise PlanError(msg, path=f"tech_tasks[{i}]")
    else:
        msg = "tech_tasks must be a list"
        raise PlanError(msg, path="tech_tasks")

    raw_oq = data.get("open_questions") or []
    open_questions = [str(q) for q in raw_oq] if isinstance(raw_oq, list) else []

    raw_other = data.get("other_issues") or []
    other_issues = list(raw_other) if isinstance(raw_other, list) else []

    return Plan(
        project=project,
        epic=epic,
        flows=flows,
        tech_tasks=tech_tasks,
        open_questions=open_questions,
        other_issues=other_issues,
    )


def dump_plan(plan: Plan) -> str:
    """Serialize a Plan dataclass to YAML according to eggs-plan/v2 schema."""
    epic_dict: dict[str, Any] = {}
    if plan.epic.existing is not None:
        epic_dict["existing"] = plan.epic.existing
    if plan.epic.key is not None:
        epic_dict["key"] = plan.epic.key
    if plan.epic.summary is not None:
        epic_dict["summary"] = plan.epic.summary
    if plan.epic.due is not None:
        epic_dict["due"] = plan.epic.due
    if plan.epic.owner is not None:
        epic_dict["owner"] = plan.epic.owner

    flows_list: list[dict[str, Any]] = []
    for flow in plan.flows:
        parts_list: list[dict[str, Any]] = []
        for part in flow.parts:
            p_dict: dict[str, Any] = {
                "part": part.part,
                "assignee": part.assignee,
                "estimate": part.estimate,
            }
            if part.key is not None:
                p_dict["key"] = part.key
            parts_list.append(p_dict)

        f_dict: dict[str, Any] = {
            "code": flow.code,
        }
        if len(flow.codes) > 1:
            f_dict["codes"] = flow.codes
        f_dict |= {
            "actor": flow.actor,
            "summary": flow.summary,
            "ac": flow.ac,
            "due": flow.due,
            "ba": flow.ba,
            "qc": flow.qc,
            "depends_on": flow.depends_on,
            "parts": parts_list,
            "key": flow.key,
        }
        if flow.keep_description:
            f_dict["keep_description"] = True
        flows_list.append(f_dict)

    tech_tasks_list: list[dict[str, Any]] = []
    for tt in plan.tech_tasks:
        tt_dict: dict[str, Any] = {
            "summary": tt.summary,
            "assignee": tt.assignee,
            "estimate": tt.estimate,
        }
        if tt.key is not None:
            tt_dict["key"] = tt.key
        tech_tasks_list.append(tt_dict)

    out: dict[str, Any] = {
        "schema": "eggs-plan/v2",
        "project": plan.project,
        "epic": epic_dict,
        "flows": flows_list,
        "tech_tasks": tech_tasks_list,
        "open_questions": plan.open_questions,
    }
    if plan.other_issues:
        out["other_issues"] = plan.other_issues

    return yaml.safe_dump(out, allow_unicode=True, sort_keys=False)
