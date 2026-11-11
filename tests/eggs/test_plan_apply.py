from datetime import date

from mcp_atlassian.eggs.config import EggsConfig
from mcp_atlassian.eggs.gateway import JiraError
from mcp_atlassian.eggs.plan import (
    Plan,
    PlanEpic,
    PlanFlow,
    PlanPart,
    PlanTechTask,
)
from mcp_atlassian.eggs.plan_apply import (
    apply_plan,
)
from mcp_atlassian.eggs.plan_validate import load_plan_context
from tests.eggs.fakes import FakeGateway

DEFAULT_FLOW_TRANSITIONS = ["Analysis Done", "Set Due Date", "Ready For Dev"]


def make_cfg() -> EggsConfig:
    return EggsConfig(
        lead_user="lead.user",
        jira_url="https://jira.example.com",
        fields={
            "part": "customfield_10701",
            "epic_link": "customfield_10100",
            "epic_name": "customfield_10102",
            "qc_owner": "customfield_10110",
        },
        issue_types={
            "epic": ["Epic"],
            "story": ["Story"],
            "dev_task": ["Dev Task"],
            "adhoc": ["Ad-hoc Task"],
        },
        transitions={
            "analysis_done": "Analysis Done",
            "set_due": "Set Due Date",
            "ready_for_dev": "Ready For Dev",
        },
        link_types={
            "blocks": "Blocks",
        },
    )


def test_creates_new_epic_story_parts():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {
        ("HMK", "Dev Task", "customfield_10701"): ["BE", "FE APP"]
    }

    plan = Plan(
        project="HMK",
        epic=PlanEpic(
            summary="New Workstream", due=date(2026, 11, 30), owner="ba.user"
        ),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in flow",
                ac=["AC 1 valid"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[
                    PlanPart(part="BE", assignee="dev.user", estimate="1d"),
                ],
            )
        ],
        tech_tasks=[
            PlanTechTask(summary="Setup DB", assignee="dev.user", estimate="4h"),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    assert len(res.created) >= 3  # Epic, Story, Dev Task, Tech Task
    assert res.plan.epic.key is not None
    assert res.plan.flows[0].key is not None
    assert res.plan.flows[0].parts[0].key is not None
    assert res.plan.tech_tasks[0].key is not None

    # Check transitions called on the new Story
    trans_calls = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == res.plan.flows[0].key
    ]
    assert "Analysis Done" in trans_calls
    assert "Set Due Date" in trans_calls
    assert "Ready For Dev" in trans_calls


def test_rerun_is_noop():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(
            summary="New Workstream", due=date(2026, 11, 30), owner="ba.user"
        ),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in flow",
                ac=["AC 1 valid"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[
                    PlanPart(part="BE", assignee="dev.user", estimate="1d"),
                ],
            )
        ],
    )
    ctx1 = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res1 = apply_plan(gateway, cfg, plan, ctx1, dry_run=False)
    assert res1.ok

    calls_before = len(gateway.calls)
    ctx2 = load_plan_context(gateway, cfg, res1.plan, today=date(2026, 11, 10))
    res2 = apply_plan(gateway, cfg, res1.plan, ctx2, dry_run=False)

    assert res2.ok
    assert res2.created == []
    # No new issue created
    create_calls = [c for c in gateway.calls[calls_before:] if c[0] == "create_issue"]
    assert create_calls == []


def test_partial_failure_returns_keys_and_rerun_continues():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}

    # Custom create_issue behavior: succeed on Epic, Story 1, Dev Task 1, fail on Story 2
    original_create = gateway.create_issue
    count = 0

    def fail_on_fourth_create(fields):
        nonlocal count
        count += 1
        if count == 4:  # 1=Epic, 2=Story 1, 3=Dev Task 1, 4=Story 2 -> FAIL
            raise JiraError("Jira temporarily unavailable")
        return original_create(fields)

    gateway.create_issue = fail_on_fourth_create

    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            ),
            PlanFlow(
                code="CH-02",
                actor="CH",
                summary="Flow 2",
                ac=["AC 2"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            ),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res1 = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert not res1.ok
    # Keys for Epic and Story 1 must be present in the returned plan!
    assert res1.plan.epic.key is not None
    assert res1.plan.flows[0].key is not None
    assert res1.plan.flows[0].parts[0].key is not None
    # Story 2 not created yet
    assert res1.plan.flows[1].key is None

    # Now restore create_issue and rerun with res1.plan
    gateway.create_issue = original_create
    ctx2 = load_plan_context(gateway, cfg, res1.plan, today=date(2026, 11, 10))
    res2 = apply_plan(gateway, cfg, res1.plan, ctx2, dry_run=False)

    assert res2.ok
    assert res2.plan.flows[1].key is not None
    # No duplicate for Story 1
    assert res2.plan.flows[0].key == res1.plan.flows[0].key


def test_existing_story_no_status_change():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "Old summary without code",
                    "status": {"name": "Dev In Progress"},
                    "issuetype": {"name": "Story"},
                    "components": [],
                    "duedate": None,
                    "description": "Original description",
                },
            }
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}

    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1", summary="Existing Epic"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in flow",
                ac=["AC 1"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                key="HMK-10",
                keep_description=True,
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            )
        ],
    )
    # Mock get_issue for HMK-1 as Epic
    gateway.issues["HMK-1"] = {
        "key": "HMK-1",
        "fields": {
            "summary": "Existing Epic",
            "issuetype": {"name": "Epic"},
            "duedate": "2026-11-30",
        },
    }

    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    trans_calls = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == "HMK-10"
    ]
    # Set Due Date is called because duedate was empty
    assert "Set Due Date" in trans_calls
    # Analysis Done and Ready For Dev must NOT be called on existing Story!
    assert "Analysis Done" not in trans_calls
    assert "Ready For Dev" not in trans_calls


def test_existing_due_not_reset():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": {
                "key": "HMK-1",
                "fields": {
                    "summary": "Existing Epic",
                    "issuetype": {"name": "Epic"},
                    "duedate": "2026-11-30",
                },
            },
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] Already has due",
                    "status": {"name": "Analyzing"},
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "duedate": "2026-11-20",
                    "customfield_10110": {"name": "qc.user"},
                },
            },
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Already has due",
                ac=["AC 1"],
                due=date(2026, 11, 25),  # different due date in plan
                ba="ba.user",
                qc="qc.user",
                key="HMK-10",
                keep_description=True,
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    trans_calls = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == "HMK-10"
    ]
    assert "Set Due Date" not in trans_calls


def test_tbd_stays_analyzing():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow with TBD",
                ac=["AC 1", "TBD: Chưa chốt logic"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    story_key = res.plan.flows[0].key
    trans_calls = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == story_key
    ]
    assert "Analysis Done" in trans_calls
    assert "Set Due Date" in trans_calls
    # Ready For Dev must NOT be called!
    assert "Ready For Dev" not in trans_calls
    assert any("Analyzed" in w for w in res.warnings)


def test_ready_for_dev_validator_error_reported_per_flow():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}

    first_ready = True

    def transition_with_validator_error(
        key, transition_name, fields=None, comment=None
    ):
        nonlocal first_ready
        if transition_name == "Ready For Dev" and first_ready:
            first_ready = False
            raise JiraError("JMWE validator: sub-tasks incomplete")
        gateway.calls.append(
            (
                "transition",
                (key, transition_name),
                {"fields": fields, "comment": comment},
            )
        )

    gateway.transition = transition_with_validator_error

    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            ),
            PlanFlow(
                code="CH-02",
                actor="CH",
                summary="Flow 2",
                ac=["AC 2"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            ),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok  # non-fatal for other flows
    assert any("CH-01" in w and "Ready For Dev" in w for w in res.warnings)
    # Story 2 Ready For Dev was called
    story2_key = res.plan.flows[1].key
    trans2_calls = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == story2_key
    ]
    assert "Ready For Dev" in trans2_calls


def test_depends_on_link_direction():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            ),
            PlanFlow(
                code="CH-02",
                actor="CH",
                summary="Flow 2",
                ac=["AC 2"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                depends_on=["CH-01"],  # CH-02 depends on CH-01 (CH-01 blocks CH-02)
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            ),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    story1_key = res.plan.flows[0].key
    story2_key = res.plan.flows[1].key

    link_calls = [
        (args, kwargs) for name, args, kwargs in gateway.calls if name == "link"
    ]
    assert len(link_calls) == 1
    args, _ = link_calls[0]
    # link_type, inward_key, outward_key
    assert args[0] == "Blocks"
    assert args[1] == story1_key  # inward = dependency (CH-01)
    assert args[2] == story2_key  # outward = dependent flow (CH-02)


def test_dry_run_writes_nothing():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                due=date(2026, 11, 20),
                ba="ba.user",
                qc="qc.user",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=True)

    assert res.ok
    assert len(res.changes) > 0
    assert res.created == []
    # No mutation calls to gateway
    write_methods = {
        "create_issue",
        "update_fields",
        "transition",
        "link",
        "set_issue_property",
    }
    executed_writes = [c[0] for c in gateway.calls if c[0] in write_methods]
    assert executed_writes == []


def test_story_with_full_fields_transitions_open_to_analyzed():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in flow",
                ac=["AC 1 valid"],
                ba="ba.user",
                qc="qc.user",
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    story_key = res.plan.flows[0].key
    trans_calls = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == story_key
    ]
    assert "Analysis Done" in trans_calls
    assert any(f"{story_key} chuyển trạng thái -> Analyzed" in c for c in res.changes)


def test_story_missing_ac_stays_open_with_message():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-02",
                actor="CH",
                summary="Flow missing AC",
                ac=[],  # Missing AC!
                keep_description=True,
                ba="ba.user",
                qc="qc.user",
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    story_key = res.plan.flows[0].key
    trans_calls = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == story_key
    ]
    # Analysis Done must NOT be called
    assert "Analysis Done" not in trans_calls
    assert any(f"{story_key} giữ nguyên Open: thiếu AC" in c for c in res.changes)


def test_story_without_qc_stays_open_but_gets_due_date_and_open_warning():
    """Review U: Due Date is set even when Analysis Done is skipped; message says Open."""
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-03",
                actor="CH",
                summary="Flow without QC",
                ac=["AC 1 valid"],
                ba="ba.user",
                due=date(2026, 11, 25),
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    story_key = res.plan.flows[0].key
    trans = [
        (args[1], kwargs)
        for name, args, kwargs in gateway.calls
        if name == "transition" and args[0] == story_key
    ]
    names = [t[0] for t in trans]
    assert "Analysis Done" not in names
    assert "Set Due Date" in names
    due_kwargs = next(k for n, k in trans if n == "Set Due Date")
    assert due_kwargs["fields"] == {"duedate": "2026-11-25"}
    warn = [w for w in res.warnings if story_key in w]
    assert warn, res.warnings
    assert "ở lại trạng thái Open" in warn[0]
    assert "Analyzed" not in warn[0]
    assert "thiếu QC Owner" in warn[0]


# --- Batch R review fixes (items 1, 2, 4, 5, 7, 8, 9, 11) ---

READY_STATUSES = {"Analysis Done": "Analyzing", "Ready For Dev": "Ready For Dev"}


def _epic(key: str = "HMK-1", **fields) -> dict:
    base = {
        "summary": "Epic",
        "issuetype": {"name": "Epic"},
        "project": {"key": "HMK"},
        "duedate": "2026-11-30",
        "assignee": {"name": "ba.user"},
    }
    base.update(fields)
    return {"key": key, "fields": base}


def _story(key: str, summary: str, **fields) -> dict:
    base = {
        "summary": summary,
        "issuetype": {"name": "Story"},
        "status": {"name": "Dev In Progress"},
        "components": [{"name": "CH - CỬA HÀNG"}],
    }
    base.update(fields)
    return {"key": key, "fields": base}


def _flow(code: str, summary: str = "Flow", **kwargs) -> PlanFlow:
    base: dict = {
        "code": code,
        "actor": "CH",
        "summary": summary,
        "ac": ["AC 1"],
        "due": date(2026, 11, 25),
        "ba": "ba.user",
        "qc": "qc.user",
        "parts": [PlanPart(part="BE", assignee="dev.user", estimate="1d")],
    }
    base.update(kwargs)
    return PlanFlow(**base)


def test_dev_task_matching_uses_parent_search_and_writes_only_differences():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] Check-in",
                customfield_10100="HMK-1",
                customfield_10110={"name": "qc.user"},
                duedate="2026-11-25",
                description="h2. Acceptance Criteria\n- AC 1",
                subtasks=[
                    {
                        "id": "101",
                        "key": "HMK-101",
                        "fields": {"summary": "[BE] Check-in"},
                    }
                ],
            ),
            "HMK-101": {
                "key": "HMK-101",
                "fields": {
                    "summary": "[BE] Check-in",
                    "issuetype": {"name": "Dev Task"},
                    "parent": {"key": "HMK-10"},
                    "customfield_10701": {"value": "BE"},
                    "assignee": {"name": "dev.user"},
                    "timetracking": {"originalEstimate": "1d"},
                },
            },
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow(
                "CH-01",
                "Check-in",
                key="HMK-10",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="2d")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    assert 'parent = "HMK-10"' in gateway.jql_queries
    updates = [
        c for c in gateway.calls if c[0] == "update_fields" and c[1][0] == "HMK-101"
    ]
    assert len(updates) == 1
    assert updates[0][1][1] == {"timetracking": {"originalEstimate": "2d"}}
    dev_creates = [
        c
        for c in gateway.calls
        if c[0] == "create_issue"
        and c[1][0].get("issuetype", {}).get("name") == "Dev Task"
    ]
    assert dev_creates == []


def test_one_existing_dev_task_is_claimed_by_at_most_one_part():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story("HMK-10", "[CH-01] Check-in", customfield_10100="HMK-1"),
            "HMK-101": {
                "key": "HMK-101",
                "fields": {
                    "summary": "[BE] Check-in",
                    "issuetype": {"name": "Dev Task"},
                    "parent": {"key": "HMK-10"},
                    "customfield_10701": {"value": "BE"},
                    "assignee": {"name": "dev.user"},
                    "timetracking": {"originalEstimate": "1d"},
                },
            },
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow(
                "CH-01",
                "Check-in",
                key="HMK-10",
                parts=[
                    PlanPart(part="BE", assignee="dev.user", estimate="1d"),
                    PlanPart(part="BE", assignee="dev.user", estimate="1d"),
                ],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    created = [
        c
        for c in gateway.calls
        if c[0] == "create_issue"
        and c[1][0].get("issuetype", {}).get("name") == "Dev Task"
    ]
    assert len(created) == 1
    assert res.plan.flows[0].parts[0].key == "HMK-101"
    assert res.plan.flows[0].parts[1].key != "HMK-101"


def test_adhoc_tech_task_matches_existing_by_exact_summary():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-20": {
                "key": "HMK-20",
                "fields": {
                    "summary": "Setup DB",
                    "issuetype": {"name": "Ad-hoc Task"},
                    "status": {"name": "Dev In Progress"},
                },
            },
        },
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[],
        tech_tasks=[PlanTechTask(summary="Setup DB")],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    pre_count = len(gateway.issues)
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    assert res.created == []
    assert len(gateway.issues) == pre_count
    assert res.plan.tech_tasks[0].key == "HMK-20"


def test_epic_children_search_failure_writes_nothing():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={"HMK-1": _epic()},
        errors_on_search={"cf[10100]": JiraError("JQL unavailable")},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01")],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert not res.ok
    assert res.created == []
    write_methods = {
        "create_issue",
        "update_fields",
        "transition",
        "link",
        "set_issue_property",
    }
    assert [c[0] for c in gateway.calls if c[0] in write_methods] == []


def test_flow_error_isolated_and_links_ready_for_dev_run_for_successful_flows():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
        status_after=READY_STATUSES,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    original_create = gateway.create_issue

    def fail_flow_three(fields):
        summary = fields.get("summary", "")
        if "[CH-03] Flow 3" in summary:
            raise JiraError("Jira temporarily unavailable")
        return original_create(fields)

    gateway.create_issue = fail_flow_three

    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            _flow("CH-01", "Flow 1"),
            _flow("CH-02", "Flow 2", depends_on=["CH-01"]),
            _flow("CH-03", "Flow 3"),
            _flow("CH-04", "Flow 4", depends_on=["CH-01"]),
            _flow("CH-05", "Flow 5", depends_on=["CH-01"]),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res1 = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert not res1.ok
    assert [e["code"] for e in res1.errors] == ["CH-03"]
    assert res1.errors[0]["error"] == "Jira temporarily unavailable"
    keys = {f.code: f.key for f in res1.plan.flows}
    assert keys["CH-03"] is None
    for code in ("CH-01", "CH-02", "CH-04", "CH-05"):
        key = keys[code]
        assert key
        transitions = [
            args[1]
            for name, args, _ in gateway.calls
            if name == "transition" and args[0] == key
        ]
        assert "Ready For Dev" in transitions
    linked = {(inward, outward) for _t, inward, outward in gateway.links}
    for code in ("CH-02", "CH-04", "CH-05"):
        assert (keys["CH-01"], keys[code]) in linked

    gateway.create_issue = original_create
    mark = len(gateway.calls)
    ctx2 = load_plan_context(gateway, cfg, res1.plan, today=date(2026, 11, 10))
    res2 = apply_plan(gateway, cfg, res1.plan, ctx2, dry_run=False)

    assert res2.ok
    flow_three_key = res2.plan.flows[2].key
    assert flow_three_key is not None
    later_writes = [
        c
        for c in gateway.calls[mark:]
        if c[0] in {"update_fields", "transition", "link", "set_issue_property"}
        and c[1][0] != flow_three_key
    ]
    assert later_writes == []
    later_creates = [c[0] for c in gateway.calls[mark:] if c[0] == "create_issue"]
    assert len(later_creates) == 2  # flow 3: Story + Dev Task


def test_applying_original_unkeyed_plan_twice_creates_nothing():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
        status_after=READY_STATUSES,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    gateway.issues["HMK-1"] = _epic()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "Check-in")],
    )
    ctx1 = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res1 = apply_plan(gateway, cfg, plan, ctx1, dry_run=False)
    assert res1.ok

    mark = len(gateway.calls)
    ctx2 = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res2 = apply_plan(gateway, cfg, plan, ctx2, dry_run=False)

    assert res2.ok
    assert res2.created == []
    assert res2.plan.flows[0].key == res1.plan.flows[0].key
    assert [
        c
        for c in gateway.calls[mark:]
        if c[0]
        in {"create_issue", "update_fields", "transition", "link", "set_issue_property"}
    ] == []


def test_existing_due_date_difference_warns_and_writes_nothing():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] Check-in",
                customfield_10100="HMK-1",
                duedate="2026-11-20",
                customfield_10110={"name": "qc.user"},
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "Check-in", key="HMK-10", due=date(2026, 11, 25))],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    assert any(
        "HMK-10: Due Date đang là 2026-11-20, plan là 2026-11-25" in w
        and "eggs_request_reschedule" in w
        for w in res.warnings
    )
    due_calls = [
        args
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == "HMK-10" and args[1] == "Set Due Date"
    ]
    assert due_calls == []
    assert not any(
        c[0] == "update_fields" and "duedate" in c[1][1] for c in gateway.calls
    )


def test_missing_set_due_date_transition_is_a_warning_not_an_error():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] Check-in",
                customfield_10100="HMK-1",
                customfield_10110={"name": "qc.user"},
            ),
        },
        default_transitions=["Analysis Done", "Ready For Dev"],
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "Check-in", key="HMK-10")],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    assert any("Set Due Date" in w and "HMK-10" in w for w in res.warnings)


def test_dry_run_returns_plan_without_placeholder_keys_and_lists_would_create():
    from mcp_atlassian.eggs.plan import dump_plan

    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
        status_after=READY_STATUSES,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Workstream", due=date(2026, 11, 30), owner="ba.user"),
        flows=[
            _flow("CH-01", "Flow 1"),
            _flow("CH-02", "Flow 2", depends_on=["CH-01"]),
        ],
        tech_tasks=[PlanTechTask(summary="Setup DB")],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=True)

    assert res.ok
    dumped = dump_plan(res.plan)
    assert "DRYRUN" not in dumped
    assert res.plan.epic.key is None
    assert all(f.key is None for f in res.plan.flows)
    assert all(p.key is None for f in res.plan.flows for p in f.parts)
    assert res.plan.tech_tasks[0].key is None
    types = {item["type"] for item in res.would_create}
    assert {"Epic", "Story", "Dev Task", "Ad-hoc Task", "Link"} <= types
    assert any("CH-01 blocks CH-02" in c for c in res.changes)


def test_link_search_failure_is_an_error_for_that_flow():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story("HMK-10", "[CH-01] One", customfield_10100="HMK-1"),
            "HMK-11": _story("HMK-11", "[CH-02] Two", customfield_10100="HMK-1"),
        },
        errors_on_search={"linkedIssues(": JiraError("link search down")},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow("CH-01", "One", key="HMK-10"),
            _flow("CH-02", "Two", key="HMK-11", depends_on=["CH-01"]),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert not res.ok
    assert any(e["code"] == "CH-02" for e in res.errors)
    assert gateway.links == []


def test_property_is_skipped_when_current_code_matches():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] Check-in",
                customfield_10100="HMK-1",
                customfield_10110={"name": "qc.user"},
                description="h2. Acceptance Criteria\n- AC 1",
                duedate="2026-11-25",
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.issue_properties[("HMK-10", "eggs.flowCode")] = "CH-01"
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "Check-in", key="HMK-10")],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert [c for c in gateway.calls if c[0] == "set_issue_property"] == []


def test_existing_description_only_its_ac_section_is_replaced():
    cfg = make_cfg()
    original = "Intro paragraph\n\nh2. Acceptance Criteria\n- old item\n\nh2. Notes\nFooter notes"
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] Check-in",
                customfield_10100="HMK-1",
                description=original,
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "Check-in", key="HMK-10", ac=["new item"])],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    desc = gateway.issues["HMK-10"]["fields"]["description"]
    assert desc.startswith("Intro paragraph")
    assert "Footer notes" in desc
    assert "- new item" in desc
    assert "- old item" not in desc
    assert "h2. Acceptance Criteria" in desc


def test_existing_summary_keeps_all_codes_and_extra_components():
    cfg = make_cfg()
    summary = "[CH-04,CH-05]  Double  space"
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG", "OPS - KHÁC"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                summary,
                customfield_10100="HMK-1",
                components=[
                    {"name": "CH - CỬA HÀNG"},
                    {"name": "OPS - KHÁC"},
                ],
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow(
                "CH-05",
                "Double space",
                key="HMK-10",
                ac=[],
                keep_description=True,
                due=None,
                qc=None,
                ba=None,
                parts=[],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    assert gateway.issues["HMK-10"]["fields"]["summary"] == summary
    assert gateway.issues["HMK-10"]["fields"]["components"] == [
        {"name": "CH - CỬA HÀNG"},
        {"name": "OPS - KHÁC"},
    ]
    assert not any(
        c[0] == "update_fields" and ({"summary", "components"} & set(c[1][1]))
        for c in gateway.calls
    )


def test_existing_ac_section_compare_ignores_crlf_and_trailing_newline():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] Check-in",
                customfield_10100="HMK-1",
                customfield_10110={"name": "qc.user"},
                components=[{"name": "CH - CỬA HÀNG"}],
                description=("Intro\r\n\r\nh2. Acceptance Criteria\r\n- AC 1\r\n"),
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "Check-in", key="HMK-10", due=None, parts=[])],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    desc_updates = [
        c for c in gateway.calls if c[0] == "update_fields" and "description" in c[1][1]
    ]
    assert desc_updates == []


def test_original_plan_without_codes_applied_twice_creates_nothing():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={"HMK-1": _epic()},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
        status_after=READY_STATUSES,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    uncoded_flow = _flow(
        "CH-01",
        "Check-in",
        parts=[PlanPart(part="BE", assignee="dev.user", estimate="1d")],
    )
    uncoded_flow.code = None
    uncoded_flow.codes = []
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[uncoded_flow],
    )
    ctx1 = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res1 = apply_plan(gateway, cfg, plan, ctx1, dry_run=False)
    assert res1.ok
    assert res1.plan.flows[0].code == "CH-01"

    mark = len(gateway.calls)
    ctx2 = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res2 = apply_plan(gateway, cfg, plan, ctx2, dry_run=False)

    assert res2.ok
    assert res2.created == []
    assert res2.plan.flows[0].code == "CH-01"
    assert [
        c
        for c in gateway.calls[mark:]
        if c[0]
        in {"create_issue", "update_fields", "transition", "link", "set_issue_property"}
    ] == []


# --- Batch T review fixes (C1, I1-I5, M3, M4) ---


def test_dev_task_error_keeps_that_story_out_of_ready_for_dev():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] One",
                customfield_10100="HMK-1",
                customfield_10110={"name": "qc.user"},
                duedate="2026-11-25",
                description="h2. Acceptance Criteria\n- AC 1",
                status={"name": "Analyzing"},
            ),
            "HMK-11": _story(
                "HMK-11",
                "[CH-02] Two",
                customfield_10100="HMK-1",
                customfield_10110={"name": "qc.user"},
                duedate="2026-11-25",
                description="h2. Acceptance Criteria\n- AC 1",
                status={"name": "Analyzing"},
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
        status_after=READY_STATUSES,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    original_create = gateway.create_issue

    def fail_dev_task_for_first_story(fields):
        if (
            fields.get("issuetype", {}).get("name") == "Dev Task"
            and fields.get("parent", {}).get("key") == "HMK-10"
        ):
            raise JiraError("Dev Task create failed")
        return original_create(fields)

    gateway.create_issue = fail_dev_task_for_first_story

    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow("CH-01", "One", key="HMK-10"),
            _flow("CH-02", "Two", key="HMK-11"),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert not res.ok
    assert any(e["code"] == "CH-01" for e in res.errors)
    trans10 = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == "HMK-10"
    ]
    trans11 = [
        args[1]
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == "HMK-11"
    ]
    assert "Ready For Dev" not in trans10
    assert "Ready For Dev" in trans11


def test_existing_open_story_is_analyzed_on_rerun_after_transition_failure():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] One",
                customfield_10100="HMK-1",
                customfield_10110={"name": "qc.user"},
                duedate="2026-11-25",
                description="h2. Acceptance Criteria\n- AC 1",
                components=[{"name": "CH - CỬA HÀNG"}],
                status={"name": "Open"},
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
        status_after=READY_STATUSES,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    original_transition = gateway.transition
    failed = False

    def flaky_analysis(key, transition_name, fields=None, comment=None):
        nonlocal failed
        if key == "HMK-10" and transition_name == "Analysis Done" and not failed:
            failed = True
            raise JiraError("validator blocked")
        return original_transition(key, transition_name, fields=fields, comment=comment)

    gateway.transition = flaky_analysis
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "One", key="HMK-10")],
    )
    ctx1 = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res1 = apply_plan(gateway, cfg, plan, ctx1, dry_run=False)

    assert not res1.ok
    assert any(e["code"] == "CH-01" for e in res1.errors)
    assert gateway.issues["HMK-10"]["fields"]["status"]["name"] == "Open"
    assert not any(
        args[1] == "Ready For Dev"
        for name, args, _ in gateway.calls
        if name == "transition" and args[0] == "HMK-10"
    )

    gateway.transition = original_transition
    mark = len(gateway.calls)
    ctx2 = load_plan_context(gateway, cfg, res1.plan, today=date(2026, 11, 10))
    res2 = apply_plan(gateway, cfg, res1.plan, ctx2, dry_run=False)

    assert res2.ok
    transitions = [
        args[1]
        for name, args, _ in gateway.calls[mark:]
        if name == "transition" and args[0] == "HMK-10"
    ]
    assert "Analysis Done" in transitions
    assert "Ready For Dev" in transitions


def test_dev_task_estimate_compares_seconds_not_strings():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] One",
                customfield_10100="HMK-1",
                status={"name": "Dev In Progress"},
            ),
            "HMK-101": {
                "key": "HMK-101",
                "fields": {
                    "summary": "[BE] One",
                    "issuetype": {"name": "Dev Task"},
                    "parent": {"key": "HMK-10"},
                    "customfield_10701": {"value": "BE"},
                    "assignee": {"name": "dev.user"},
                    "timeoriginalestimate": 28800,
                },
            },
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow(
                "CH-01",
                "One",
                key="HMK-10",
                parts=[PlanPart(part="BE", assignee="dev.user", estimate="8h")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    assert not any(
        c[0] == "update_fields" and c[1][0] == "HMK-101" for c in gateway.calls
    )


def test_part_key_not_found_in_story_dev_tasks_is_an_error():
    from mcp_atlassian.eggs.plan_validate import PlanContext

    cfg = make_cfg()
    story = _story(
        "HMK-10",
        "[CH-01] One",
        status={"name": "Dev In Progress"},
    )
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={"HMK-1": _epic(), "HMK-10": story},
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow(
                "CH-01",
                "One",
                key="HMK-10",
                parts=[
                    PlanPart(
                        part="BE", assignee="dev.user", estimate="1d", key="HMK-999"
                    )
                ],
            )
        ],
    )
    ctx = PlanContext(
        today=date(2026, 11, 10),
        components={"CH": "CH - CỬA HÀNG"},
        part_options=["BE"],
        epic_issue=_epic(),
        epic_stories=[_epic(), story],
        known_users={"ba.user", "qc.user", "dev.user"},
        unknown_users=set(),
        story_dev_task_keys={"HMK-10": {"HMK-999"}},
    )
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert not res.ok
    assert any(e["code"] == "CH-01" and "HMK-999" in e["error"] for e in res.errors)
    assert [c for c in gateway.calls if c[0] == "create_issue"] == []


def test_property_read_error_in_apply_is_recorded_and_not_written():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] One",
                customfield_10100="HMK-1",
                customfield_10110={"name": "qc.user"},
                duedate="2026-11-25",
                status={"name": "Analyzing"},
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "One", key="HMK-10", parts=[])],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    gateway.errors_on_get_property[("HMK-10", "eggs.flowCode")] = JiraError(
        "property read failed"
    )
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert not res.ok
    assert any("property read failed" in e["error"] for e in res.errors)
    assert [c for c in gateway.calls if c[0] == "set_issue_property"] == []


def test_property_write_is_listed_in_dry_run_changes():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] One",
                customfield_10100="HMK-1",
                status={"name": "Dev In Progress"},
            ),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[_flow("CH-01", "One", key="HMK-10", parts=[])],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=True)

    assert res.ok
    assert any("eggs.flowCode" in change for change in res.changes)
    assert [c for c in gateway.calls if c[0] == "set_issue_property"] == []


def test_part_without_assignee_warns_when_only_part_could_match():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story(
                "HMK-10",
                "[CH-01] One",
                customfield_10100="HMK-1",
                status={"name": "Dev In Progress"},
            ),
            "HMK-101": {
                "key": "HMK-101",
                "fields": {
                    "summary": "[BE] One",
                    "issuetype": {"name": "Dev Task"},
                    "parent": {"key": "HMK-10"},
                    "customfield_10701": {"value": "BE"},
                    "assignee": {"name": "dev.user"},
                    "timetracking": {"originalEstimate": "1d"},
                },
            },
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow(
                "CH-01",
                "One",
                key="HMK-10",
                parts=[PlanPart(part="BE", assignee=None, estimate="1d")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=False)

    assert res.ok
    assert any("HMK-101" in w and "assignee" in w and "BE" in w for w in res.warnings)


def test_dry_run_does_not_list_a_link_that_already_exists():
    cfg = make_cfg()

    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic(),
            "HMK-10": _story("HMK-10", "[CH-01] One", customfield_10100="HMK-1"),
            "HMK-11": _story("HMK-11", "[CH-02] Two", customfield_10100="HMK-1"),
        },
        default_transitions=DEFAULT_FLOW_TRANSITIONS,
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    gateway.links.append(("Blocks", "HMK-10", "HMK-11"))  # HMK-10 blocks HMK-11
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            _flow("CH-01", "One", key="HMK-10"),
            _flow("CH-02", "Two", key="HMK-11", depends_on=["CH-01"]),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=True)

    assert not any("liên kết" in c for c in res.changes)
    assert not any(w.get("type") == "Link" for w in res.would_create)
