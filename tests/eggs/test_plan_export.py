from datetime import date

from mcp_atlassian.eggs.config import EggsConfig
from mcp_atlassian.eggs.plan_apply import (
    apply_plan,
    export_epic,
)
from mcp_atlassian.eggs.plan_validate import load_plan_context, validate_plan
from tests.eggs.fakes import FakeGateway


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
            "bug_sub": ["Bug"],
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


def test_export_maps_story_fields():
    cfg = make_cfg()
    gateway = FakeGateway(
        issues={
            "HMK-1": {
                "key": "HMK-1",
                "fields": {
                    "summary": "Epic 1",
                    "project": {"key": "HMK"},
                    "issuetype": {"name": "Epic"},
                },
            },
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] Check-in",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "duedate": "2026-11-20",
                    "assignee": {"name": "ba.user"},
                    "customfield_10110": {"name": "qc.user"},
                    "description": "Original spec text",
                    "subtasks": [
                        {
                            "key": "HMK-101",
                            "fields": {
                                "issuetype": {"name": "Dev Task"},
                                "customfield_10701": {"value": "BE"},
                                "assignee": {"name": "dev.user"},
                                "timetracking": {"originalEstimate": "1d"},
                            },
                        },
                        {
                            "key": "HMK-102",
                            "fields": {
                                "issuetype": {
                                    "name": "Bug"
                                },  # Bug sub-task -> must NOT be part
                                "summary": "Bug in check-in",
                            },
                        },
                    ],
                },
            },
            "HMK-20": {
                "key": "HMK-20",
                "fields": {
                    "summary": "Fix database migration",
                    "issuetype": {"name": "Ad-hoc Task"},
                    "status": {"name": "Dev In Progress"},
                },
            },
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
            "HMK-102": {
                "key": "HMK-102",
                "fields": {
                    "summary": "Bug in check-in",
                    "issuetype": {"name": "Bug"},
                    "parent": {"key": "HMK-10"},
                },
            },
        }
    )

    plan = export_epic(gateway, cfg, "HMK-1")

    assert plan.project == "HMK"
    assert plan.epic.existing == "HMK-1"
    assert len(plan.flows) == 1

    flow = plan.flows[0]
    assert flow.key == "HMK-10"
    assert flow.code == "CH-01"
    assert flow.actor == "CH"
    assert flow.summary == "Check-in"
    assert flow.ac == []
    assert flow.keep_description is True
    assert flow.due == date(2026, 11, 20)
    assert flow.ba == "ba.user"
    assert flow.qc == "qc.user"

    # Only Dev Task is exported as part
    assert len(flow.parts) == 1
    assert flow.parts[0].key == "HMK-101"
    assert flow.parts[0].part == "BE"
    assert flow.parts[0].assignee == "dev.user"
    assert flow.parts[0].estimate == "1d"

    # Ad-hoc Task is in other_issues
    assert len(plan.other_issues) == 1
    assert plan.other_issues[0]["key"] == "HMK-20"
    assert plan.other_issues[0]["type"] == "Ad-hoc Task"
    assert plan.other_issues[0]["status"] == "Dev In Progress"
    assert plan.other_issues[0]["summary"] == "Fix database migration"


def test_export_paginates():
    cfg = make_cfg()
    gateway = FakeGateway()

    # Epic issue
    gateway.issues["HMK-1"] = {
        "key": "HMK-1",
        "fields": {
            "summary": "Big Epic",
            "project": {"key": "HMK"},
            "issuetype": {"name": "Epic"},
        },
    }

    # Generate 250 stories across multiple pages
    all_children = []
    for i in range(1, 251):
        key = f"HMK-{100 + i}"
        iss = {
            "key": key,
            "fields": {
                "summary": f"[CH-{i:02d}] Story {i}",
                "issuetype": {"name": "Story"},
                "components": [{"name": "CH - CỬA HÀNG"}],
                "duedate": "2026-11-20",
                "assignee": {"name": "ba.user"},
                "customfield_10110": {"name": "qc.user"},
                "subtasks": [],
            },
        }
        all_children.append(iss)

    search_calls = 0

    def paginating_search(
        jql,
        fields,
        expand_changelog: bool = False,  # noqa: FBT001, FBT002
    ):
        nonlocal search_calls
        search_calls += 1
        return all_children

    gateway.search = paginating_search

    plan = export_epic(gateway, cfg, "HMK-1")
    assert len(plan.flows) == 250
    assert plan.flows[0].code == "CH-01"
    assert plan.flows[249].code == "CH-250"


def test_export_keeps_non_code_brackets():
    cfg = make_cfg()
    gateway = FakeGateway(
        issues={
            "HMK-1": {
                "key": "HMK-1",
                "fields": {
                    "summary": "Epic 1",
                    "project": {"key": "HMK"},
                    "issuetype": {"name": "Epic"},
                },
            },
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[DEMO][CH-01] Feature X",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "duedate": "2026-11-20",
                    "subtasks": [],
                },
            },
            "HMK-11": {
                "key": "HMK-11",
                "fields": {
                    "summary": "[HMKPOS] Plain Title",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "duedate": "2026-11-20",
                    "subtasks": [],
                },
            },
        }
    )

    plan = export_epic(gateway, cfg, "HMK-1")
    assert len(plan.flows) == 2
    assert plan.flows[0].code == "CH-01"
    assert plan.flows[0].summary == "[DEMO] Feature X"

    assert plan.flows[1].code is None
    assert plan.flows[1].summary == "[HMKPOS] Plain Title"


def test_export_then_validate_flags_missing_actor():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": {
                "key": "HMK-1",
                "fields": {
                    "summary": "Epic 1",
                    "project": {"key": "HMK"},
                    "issuetype": {"name": "Epic"},
                    "duedate": "2026-11-30",
                },
            },
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "Story without component",
                    "issuetype": {"name": "Story"},
                    "components": [],  # missing actor
                    "duedate": None,  # missing due
                    "subtasks": [],
                },
            },
        },
    )

    plan = export_epic(gateway, cfg, "HMK-1")
    assert plan.flows[0].actor is None
    # open_questions should list missing fields
    assert any("Actor" in q for q in plan.open_questions)

    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(f.path == "flows[0].actor" for f in report.findings)


def test_export_apply_round_trip_noop():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": {
                "key": "HMK-1",
                "fields": {
                    "summary": "Epic 1",
                    "project": {"key": "HMK"},
                    "issuetype": {"name": "Epic"},
                    "duedate": "2026-11-30",
                },
            },
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] Check-in",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "duedate": "2026-11-20",
                    "assignee": {"name": "ba.user"},
                    "customfield_10110": {"name": "qc.user"},
                    "description": "## Acceptance Criteria\n- Step 1",
                    "subtasks": [
                        {
                            "key": "HMK-101",
                            "fields": {
                                "summary": "[BE] Check-in",
                                "issuetype": {"name": "Dev Task"},
                                "status": {"name": "DEV To Do"},
                            },
                        }
                    ],
                },
            },
            "HMK-101": {
                "key": "HMK-101",
                "fields": {
                    "summary": "[BE] Check-in",
                    "issuetype": {"name": "Dev Task"},
                    "status": {"name": "DEV To Do"},
                    "parent": {"key": "HMK-10"},
                    "customfield_10701": {"value": "BE"},
                    "assignee": {"name": "dev.user"},
                    "timetracking": {"originalEstimate": "1d"},
                },
            },
        },
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    gateway.issue_properties[("HMK-10", "eggs.flowCode")] = "CH-01"

    plan = export_epic(gateway, cfg, "HMK-1")
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, plan, ctx, dry_run=True)

    assert res.ok
    # Dry run should report no changes because fields, property and subtasks match
    assert res.changes == []


# --- Batch R review fixes (items 1, 6) ---


def test_export_reads_dev_tasks_with_a_parent_search():
    cfg = make_cfg()
    gateway = FakeGateway(
        issues={
            "HMK-1": {
                "key": "HMK-1",
                "fields": {
                    "summary": "Epic",
                    "project": {"key": "HMK"},
                    "issuetype": {"name": "Epic"},
                },
            },
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] Check-in",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "duedate": "2026-11-20",
                    "subtasks": [
                        {
                            "key": "HMK-101",
                            "id": "101",
                            "fields": {
                                "summary": "[BE] Check-in",
                                "issuetype": {"name": "Dev Task"},
                                "status": {"name": "DEV To Do"},
                            },
                        }
                    ],
                },
            },
            "HMK-101": {
                "key": "HMK-101",
                "fields": {
                    "summary": "[BE] Check-in",
                    "issuetype": {"name": "Dev Task"},
                    "status": {"name": "DEV To Do"},
                    "parent": {"key": "HMK-10"},
                    "customfield_10701": {"value": "BE"},
                    "assignee": {"name": "dev.user"},
                    "timetracking": {"originalEstimate": "1d"},
                },
            },
        }
    )

    plan = export_epic(gateway, cfg, "HMK-1")

    assert 'parent = "HMK-10"' in gateway.jql_queries
    assert len(plan.flows[0].parts) == 1
    part = plan.flows[0].parts[0]
    assert part.key == "HMK-101"
    assert part.part == "BE"
    assert part.assignee == "dev.user"
    assert part.estimate == "1d"


def test_export_round_trip_keeps_multicode_multicomponent_and_spacing():
    from mcp_atlassian.eggs.plan import dump_plan, parse_plan

    cfg = make_cfg()
    summary = "[CH-04,CH-05]  Double  space"
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG", "OPS - KHÁC"]},
        issues={
            "HMK-1": {
                "key": "HMK-1",
                "fields": {
                    "summary": "Epic",
                    "project": {"key": "HMK"},
                    "issuetype": {"name": "Epic"},
                    "duedate": "2026-11-30",
                    "assignee": {"name": "ba.user"},
                },
            },
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": summary,
                    "issuetype": {"name": "Story"},
                    "status": {"name": "Analyzing"},
                    "components": [
                        {"name": "CH - CỬA HÀNG"},
                        {"name": "OPS - KHÁC"},
                    ],
                    "duedate": "2026-11-20",
                    "assignee": {"name": "ba.user"},
                    "customfield_10110": {"name": "qc.user"},
                    "description": "h2. Acceptance Criteria\n- old\n\nTail",
                },
            },
        },
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    gateway.issue_properties[("HMK-10", "eggs.flowCode")] = "CH-04"

    plan = export_epic(gateway, cfg, "HMK-1")
    flow = plan.flows[0]
    assert flow.code == "CH-04"
    assert flow.codes == ["CH-04", "CH-05"]
    assert flow.summary == "Double  space"

    dumped = dump_plan(plan)
    assert "codes:" in dumped
    reloaded = parse_plan(dumped)
    assert reloaded.flows[0].codes == ["CH-04", "CH-05"]

    ctx = load_plan_context(gateway, cfg, reloaded, today=date(2026, 11, 10))
    res = apply_plan(gateway, cfg, reloaded, ctx, dry_run=True)
    assert res.ok, res.warnings
    assert res.changes == []
    assert gateway.issues["HMK-10"]["fields"]["summary"] == summary
