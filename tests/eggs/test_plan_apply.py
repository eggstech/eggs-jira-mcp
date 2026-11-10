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
