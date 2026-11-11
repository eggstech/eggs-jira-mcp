from datetime import date

from mcp_atlassian.eggs.config import EggsConfig
from mcp_atlassian.eggs.gateway import JiraError
from mcp_atlassian.eggs.plan import (
    Plan,
    PlanEpic,
    PlanFlow,
    PlanPart,
)
from mcp_atlassian.eggs.plan_validate import (
    PlanContext,
    load_plan_context,
    validate_plan,
)
from tests.eggs.fakes import FakeGateway


def make_cfg() -> EggsConfig:
    return EggsConfig(
        lead_user="lead.user",
        jira_url="https://jira.example.com",
        fields={
            "part": "customfield_10701",
            "epic_link": "customfield_10100",
            "qc_owner": "customfield_10110",
        },
        issue_types={
            "epic": ["Epic"],
            "story": ["Story"],
            "dev_task": ["Dev Task"],
            "adhoc": ["Ad-hoc Task"],
        },
    )


def make_context(
    today: date | None = None,
    components: dict[str, str] | None = None,
    part_options: list[str] | None = None,
    epic_issue: dict | None = None,
    epic_stories: list[dict] | None = None,
    known_users: set[str] | None = None,
    unknown_users: set[str] | None = None,
) -> PlanContext:
    return PlanContext(
        today=today or date(2026, 11, 10),
        components=(
            components
            if components is not None
            else {"CH": "CH - CỬA HÀNG", "KH": "KH - KHÁCH HÀNG"}
        ),
        part_options=(
            part_options
            if part_options is not None
            else ["FE APP", "BE", "FE WEB", "Fullstack"]
        ),
        epic_issue=epic_issue,
        epic_stories=epic_stories or [],
        known_users=(
            known_users
            if known_users is not None
            else {"ba.user", "qc.user", "dev.user", "lead.user"}
        ),
        unknown_users=unknown_users or set(),
    )


def test_context_reads_part_options_from_jira():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMKLOYAL": ["CH - CỬA HÀNG", "KH - KHÁCH HÀNG"]},
    )
    gateway.allowed_field_values = {
        ("HMKLOYAL", "Dev Task", "customfield_10701"): [
            "FE APP",
            "BE",
            "FE WEB",
            "Fullstack",
        ]
    }
    plan = Plan(
        project="HMKLOYAL",
        epic=PlanEpic(summary="Epic 1", due=date(2026, 11, 25), owner="ba.user"),
        flows=[],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    assert ctx.part_options == ["FE APP", "BE", "FE WEB", "Fullstack"]
    assert ctx.components == {"CH": "CH - CỬA HÀNG", "KH": "KH - KHÁCH HÀNG"}
    assert "ba.user" in ctx.known_users


def test_epic_missing_due_or_owner():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic without due or owner"),
        flows=[],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    paths = {f.path for f in report.findings if f.level == "error"}
    assert "epic.due" in paths
    assert "epic.owner" in paths


def test_epic_existing_not_found():
    cfg = make_cfg()
    ctx = make_context(epic_issue=None)
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-999"),
        flows=[],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(
        f.path == "epic.existing" and "HMK-999" in f.message for f in report.findings
    )


def test_epic_existing_not_an_epic():
    cfg = make_cfg()
    ctx = make_context(
        epic_issue={
            "key": "HMK-100",
            "fields": {"issuetype": {"name": "Story"}},
        }
    )
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-100"),
        flows=[],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(
        f.path == "epic.existing" and "không phải là Epic" in f.message
        for f in report.findings
    )


def test_actor_not_in_components():
    cfg = make_cfg()
    ctx = make_context(components={"CH": "CH - CỬA HÀNG"})
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="UNKNOWN-01",
                actor="UNKNOWN",
                summary="Some flow",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    finding = next(f for f in report.findings if f.path == "flows[0].actor")
    assert "CH" in finding.message


def test_summary_is_platform():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="[CH-01] FE viết màn hình check-in",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(
        f.path == "flows[0].summary" and "nền tảng" in f.message
        for f in report.findings
    )


def test_ac_empty_without_keep_description():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in tại quầy",
                ac=[],
                keep_description=False,
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(f.path == "flows[0].ac" for f in report.findings)


def test_part_missing_part_or_estimate():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in tại quầy",
                ac=["AC 1"],
                parts=[
                    PlanPart(part=None, estimate="1d"),
                    PlanPart(part="BE", estimate=None),
                ],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    paths = {f.path for f in report.findings}
    assert "flows[0].parts[0].part" in paths
    assert "flows[0].parts[1].estimate" in paths


def test_part_not_in_part_options():
    cfg = make_cfg()
    ctx = make_context(part_options=["FE APP", "BE"])
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in tại quầy",
                ac=["AC 1"],
                parts=[PlanPart(part="DEVOPS", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    finding = next(f for f in report.findings if f.path == "flows[0].parts[0].part")
    assert "DEVOPS" in finding.message
    assert "FE APP" in finding.message


def test_estimate_not_jira_duration():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in tại quầy",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="invalid_time")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(f.path == "flows[0].parts[0].estimate" for f in report.findings)


def test_code_invalid_format_or_mismatched_actor():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="ch_01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
            PlanFlow(
                code="KH-01",
                actor="CH",
                summary="Flow 2",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(f.path == "flows[0].code" for f in report.findings)
    assert any(
        f.path == "flows[1].code" and "khớp" in f.message for f in report.findings
    )


def test_duplicate_code_in_plan_or_epic():
    cfg = make_cfg()
    ctx = make_context(
        epic_stories=[
            {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-02] Existing story",
                    "issuetype": {"name": "Story"},
                },
            }
        ]
    )
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 2",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
            PlanFlow(
                code="CH-02",
                actor="CH",
                summary="Flow 3",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    code_errors = [
        f for f in report.findings if f.path in ("flows[1].code", "flows[2].code")
    ]
    assert len(code_errors) == 2


def test_depends_on_unknown():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                depends_on=["CH-99"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(
        f.path == "flows[0].depends_on" and "CH-99" in f.message
        for f in report.findings
    )


def test_user_unknown():
    cfg = make_cfg()
    ctx = make_context(unknown_users={"ghost.user"})
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ghost.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                ba="ghost.user",
                parts=[PlanPart(part="BE", estimate="1d", assignee="ghost.user")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    paths = {f.path for f in report.findings}
    assert "epic.owner" in paths
    assert "flows[0].ba" in paths
    assert "flows[0].parts[0].assignee" in paths


def test_due_before_today_for_new_flow():
    cfg = make_cfg()
    ctx = make_context(today=date(2026, 11, 10))
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                due=date(2026, 11, 5),
                key=None,
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(f.path == "flows[0].due" for f in report.findings)


def test_warning_tbd_in_ac():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["TBD: Có cho phép check-in quá giờ không?"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok  # warnings do not block ok
    assert any(
        f.path == "flows[0].ac" and f.level == "warning" for f in report.findings
    )
    assert any("TBD" in q for q in report.questions)


def test_warning_qc_missing():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                qc=None,
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert any(
        f.path == "flows[0].qc" and f.level == "warning" for f in report.findings
    )


def test_warning_flow_due_after_epic_due():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 15), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                due=date(2026, 11, 20),
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert any(
        f.path == "flows[0].due" and f.level == "warning" for f in report.findings
    )


def test_warning_keep_description_no_ac_heading():
    cfg = make_cfg()
    ctx = make_context(
        epic_stories=[
            {
                "key": "HMK-10",
                "fields": {
                    "summary": "Old story",
                    "description": "Just plain text without criteria heading",
                },
            }
        ]
    )
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                key="HMK-10",
                keep_description=True,
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert any(
        f.path == "flows[0].keep_description" and f.level == "warning"
        for f in report.findings
    )


def test_warning_part_assignee_null():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow 1",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d", assignee=None)],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert any(
        f.path == "flows[0].parts[0].assignee" and f.level == "warning"
        for f in report.findings
    )


def test_auto_code_continues_existing():
    cfg = make_cfg()
    ctx = make_context(
        epic_stories=[
            {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-07] Old flow",
                    "issuetype": {"name": "Story"},
                },
            }
        ]
    )
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code=None,
                actor="CH",
                summary="New flow without code",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert report.plan.flows[0].code == "CH-08"
    assert "[CH-08]" in report.tree


# --- Batch R review fixes (items 3, 5, 6, 10, 12) ---


def _epic_issue(key: str = "HMK-1", **fields) -> dict:
    base_fields = {
        "summary": "Epic",
        "issuetype": {"name": "Epic"},
        "project": {"key": "HMK"},
        "duedate": "2026-11-30",
        "assignee": {"name": "ba.user"},
    }
    base_fields.update(fields)
    return {"key": key, "fields": base_fields}


def test_epic_children_search_failure_is_an_error():
    cfg = make_cfg()
    gateway = FakeGateway(
        issues={"HMK-1": _epic_issue()},
        errors_on_search={"cf[10100]": JiraError("JQL unavailable")},
    )
    plan = Plan(project="HMK", epic=PlanEpic(existing="HMK-1"), flows=[])

    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    assert ctx.epic_children_error is not None

    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(
        f.path == "epic.children" and "JQL unavailable" in f.message
        for f in report.findings
        if f.level == "error"
    )


def test_epic_must_belong_to_plan_project():
    cfg = make_cfg()
    gateway = FakeGateway(issues={"HMK-1": _epic_issue(project={"key": "OTHER"})})
    plan = Plan(project="HMK", epic=PlanEpic(existing="HMK-1"), flows=[])

    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(f.path == "epic.project" for f in report.findings)

    # Key prefix is checked even when Jira does not return the project field.
    gateway2 = FakeGateway(issues={"OTHER-1": _epic_issue("OTHER-1", project=None)})
    plan2 = Plan(project="HMK", epic=PlanEpic(existing="OTHER-1"), flows=[])
    ctx2 = load_plan_context(gateway2, cfg, plan2, today=date(2026, 11, 10))
    report2 = validate_plan(plan2, ctx2, cfg)
    assert any(f.path == "epic.project" for f in report2.findings)


def test_epic_existing_without_due_or_owner_warns():
    cfg = make_cfg()
    epic = _epic_issue()
    del epic["fields"]["duedate"]
    del epic["fields"]["assignee"]
    gateway = FakeGateway(issues={"HMK-1": epic})
    plan = Plan(project="HMK", epic=PlanEpic(existing="HMK-1"), flows=[])
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))

    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    warnings = {f.path for f in report.findings if f.level == "warning"}
    assert "epic.due" in warnings
    assert "epic.owner" in warnings


def test_platform_title_is_case_sensitive_and_warns_for_keyed_flow():
    cfg = make_cfg()
    ctx = make_context(
        epic_stories=[
            {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] FE viết màn hình",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                },
            }
        ]
    )
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="FE viết màn hình",
                ac=["AC 1"],
                key="HMK-10",
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert any(
        f.path == "flows[0].summary" and f.level == "warning" for f in report.findings
    )

    # Lowercase "fe" is not a platform token anymore.
    ctx2 = make_context()
    plan2 = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="fe check-in",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report2 = validate_plan(plan2, ctx2, cfg)
    assert not any(f.path == "flows[0].summary" for f in report2.findings)


def test_tbd_check_is_case_insensitive():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Flow",
                ac=["tbd: chưa chốt"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert any(
        f.path == "flows[0].ac" and f.level == "warning" for f in report.findings
    )


def test_depends_on_self_reference_and_cycle_are_errors():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="One",
                ac=["AC 1"],
                depends_on=["CH-01"],
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
            PlanFlow(
                code="CH-02",
                actor="CH",
                summary="Two",
                ac=["AC 1"],
                depends_on=["CH-03"],
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
            PlanFlow(
                code="CH-03",
                actor="CH",
                summary="Three",
                ac=["AC 1"],
                depends_on=["CH-02"],
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(
        f.path == "flows[0].depends_on" and "chính nó" in f.message
        for f in report.findings
    )
    cycle = [f for f in report.findings if "vòng" in f.message]
    assert cycle


def test_flow_key_and_part_key_must_belong_to_epic_and_story():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic_issue(),
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] Story",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "customfield_10100": "HMK-1",
                },
            },
            "HMK-101": {
                "key": "HMK-101",
                "fields": {
                    "summary": "[BE] Dev task",
                    "issuetype": {"name": "Dev Task"},
                    "parent": {"key": "HMK-10"},
                },
            },
        },
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Story",
                ac=["AC 1"],
                key="HMK-10",
                parts=[PlanPart(part="BE", estimate="1d", key="HMK-999")],
            ),
            PlanFlow(
                code="CH-02",
                actor="CH",
                summary="Other",
                ac=["AC 2"],
                key="HMK-999",
                parts=[PlanPart(part="BE", estimate="1d")],
            ),
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    report = validate_plan(plan, ctx, cfg)
    assert not report.ok
    assert any(
        f.path == "flows[0].parts[0].key" and "HMK-999" in f.message
        for f in report.findings
    )
    assert any(f.path == "flows[1].key" for f in report.findings)


def test_unkeyed_flow_resolves_to_existing_story_and_does_not_duplicate_code():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic_issue(),
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] Check-in",
                    "issuetype": {"name": "Story"},
                    "status": {"name": "Analyzing"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                },
            },
        },
    )
    gateway.issue_properties[("HMK-10", "eggs.flowCode")] = "CH-01"
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Check-in",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    assert ctx.resolved_flow_keys.get(0) == "HMK-10"
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert report.plan.flows[0].key == "HMK-10"


# --- Batch T review fixes (I5, I6, M1, M2) ---


def test_property_read_error_on_unresolved_flow_is_an_error():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic_issue(),
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] Something else",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "customfield_10100": "HMK-1",
                },
            },
        },
        errors_on_get_property={
            ("HMK-10", "eggs.flowCode"): JiraError("property read failed")
        },
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            PlanFlow(
                code="CH-09",
                actor="CH",
                summary="Đăng nhập",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    report = validate_plan(plan, ctx, cfg)

    assert not report.ok
    assert any(
        f.path == "flows[0].key"
        and "eggs.flowCode" in f.message
        and "property read failed" in f.message
        for f in report.findings
    )


def test_title_match_with_a_different_code_is_a_validation_error():
    cfg = make_cfg()
    gateway = FakeGateway(
        components_map={"HMK": ["CH - CỬA HÀNG"]},
        issues={
            "HMK-1": _epic_issue(),
            "HMK-10": {
                "key": "HMK-10",
                "fields": {
                    "summary": "[CH-01] Đăng nhập",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "CH - CỬA HÀNG"}],
                    "customfield_10100": "HMK-1",
                },
            },
        },
    )
    gateway.allowed_field_values = {("HMK", "Dev Task", "customfield_10701"): ["BE"]}
    plan = Plan(
        project="HMK",
        epic=PlanEpic(existing="HMK-1"),
        flows=[
            PlanFlow(
                code="CH-09",
                actor="CH",
                summary="Đăng nhập",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    ctx = load_plan_context(gateway, cfg, plan, today=date(2026, 11, 10))
    assert 0 not in ctx.resolved_flow_keys
    report = validate_plan(plan, ctx, cfg)

    assert not report.ok
    conflict = next(f for f in report.findings if f.path == "flows[0].key")
    assert "CH-09" in conflict.message
    assert "CH-01" in conflict.message
    assert "HMK-10" in conflict.message


def test_mobile_title_is_not_a_platform_error():
    cfg = make_cfg()
    ctx = make_context()
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                actor="CH",
                summary="Mobile check-in",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)
    assert report.ok
    assert not any(f.path == "flows[0].summary" for f in report.findings)


def test_epic_key_still_checks_issue_type_and_project():
    cfg = make_cfg()
    ctx = make_context(
        epic_issue={
            "key": "HMK-1",
            "fields": {
                "issuetype": {"name": "Story"},
                "project": {"key": "OTHER"},
            },
        }
    )
    plan = Plan(
        project="HMK",
        epic=PlanEpic(
            key="HMK-1",
            summary="Epic",
            due=date(2026, 11, 20),
            owner="ba.user",
        ),
        flows=[],
    )
    report = validate_plan(plan, ctx, cfg)

    assert not report.ok
    messages = " | ".join(f.message for f in report.findings if f.level == "error")
    assert "không phải là Epic" in messages
    assert "không thuộc dự án" in messages


PLAN_TYPOS_YAML = """
schema: eggs-plan/v2
project: HMKLOYAL
stories:
  - actor: CH
epic: { summary: "Epic", due: 2026-11-27, owner: ba.user, ownr: ba.user }
flows:
  - code: CH-01
    actor: CH
    summary: "Check-in"
    ac: ["Có mã đơn"]
    parts:
      - { part: BE, assignee: dev.user, estimate: 1d, summary: "Dev task summary" }
"""


def test_unknown_stories_key_suggests_flows():
    from mcp_atlassian.eggs.plan import parse_plan

    report = validate_plan(parse_plan(PLAN_TYPOS_YAML), make_context(), make_cfg())

    assert not report.ok
    finding = next(f for f in report.findings if f.path == "stories")
    assert finding.message == 'Khóa không hợp lệ "stories" (đúng là "flows")'


def test_unknown_epic_key_reports_near_match():
    from mcp_atlassian.eggs.plan import parse_plan

    report = validate_plan(parse_plan(PLAN_TYPOS_YAML), make_context(), make_cfg())

    finding = next(f for f in report.findings if f.path == "epic.ownr")
    assert finding.message == 'Khóa không hợp lệ "ownr" (đúng là "owner")'


def test_unknown_part_key_reports_its_location():
    from mcp_atlassian.eggs.plan import parse_plan

    report = validate_plan(parse_plan(PLAN_TYPOS_YAML), make_context(), make_cfg())

    finding = next(f for f in report.findings if f.path == "flows[0].parts[0].summary")
    assert finding.message == 'Khóa không hợp lệ "summary" ở flows[0].parts[0]'


def test_flow_without_actor_names_the_flow():
    cfg = make_cfg()
    ctx = make_context(components={"CH": "CH - CỬA HÀNG"})
    plan = Plan(
        project="HMK",
        epic=PlanEpic(summary="Epic", due=date(2026, 11, 20), owner="ba.user"),
        flows=[
            PlanFlow(
                code="CH-01",
                summary="Flow without actor",
                ac=["AC 1"],
                parts=[PlanPart(part="BE", estimate="1d")],
            )
        ],
    )
    report = validate_plan(plan, ctx, cfg)

    finding = next(f for f in report.findings if f.path == "flows[0].actor")
    assert finding.message == "flows[0] chưa có actor (mã Component)"
