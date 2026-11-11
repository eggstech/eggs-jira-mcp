from datetime import date

import pytest

from mcp_atlassian.eggs.plan import (
    PlanError,
    dump_plan,
    format_summary,
    parse_plan,
    split_codes,
)

SPEC_YAML = """
schema: eggs-plan/v2
project: HMKLOYAL
epic: { summary: "[DEMO] Loyalty bổ sung", due: 2026-11-27, owner: ba.username }
flows:
  - code: CH-08
    actor: CH
    summary: "Check-in lịch hẹn tại cửa hàng"
    ac: ["Nhân viên tìm được lịch hẹn theo SĐT", "..."]
    due: 2026-11-20
    ba: ba.username
    qc: qc.username
    depends_on: []
    parts:
      - { part: BE,  assignee: huy.truong, estimate: 1d }
      - { part: POS, assignee: null,       estimate: 2d }
    key: null
tech_tasks: []
open_questions: ["CH-08: Có cho check-in khi trễ giờ hẹn > 30 phút?"]
"""


def test_parse_spec_example():
    plan = parse_plan(SPEC_YAML)
    assert plan.project == "HMKLOYAL"
    assert plan.epic.summary == "[DEMO] Loyalty bổ sung"
    assert plan.epic.due == date(2026, 11, 27)
    assert plan.epic.owner == "ba.username"
    assert len(plan.flows) == 1
    flow = plan.flows[0]
    assert flow.code == "CH-08"
    assert flow.actor == "CH"
    assert flow.summary == "Check-in lịch hẹn tại cửa hàng"
    assert flow.ac == ["Nhân viên tìm được lịch hẹn theo SĐT", "..."]
    assert flow.due == date(2026, 11, 20)
    assert flow.ba == "ba.username"
    assert flow.qc == "qc.username"
    assert flow.depends_on == []
    assert len(flow.parts) == 2
    assert flow.parts[0].part == "BE"
    assert flow.parts[0].assignee == "huy.truong"
    assert flow.parts[0].estimate == "1d"
    assert flow.parts[1].part == "POS"
    assert flow.parts[1].assignee is None
    assert flow.parts[1].estimate == "2d"
    assert flow.key is None
    assert flow.keep_description is False
    assert plan.tech_tasks == []
    assert plan.open_questions == ["CH-08: Có cho check-in khi trễ giờ hẹn > 30 phút?"]
    assert plan.other_issues == []


def test_round_trip():
    plan = parse_plan(SPEC_YAML)
    dumped = dump_plan(plan)
    reloaded = parse_plan(dumped)
    assert reloaded == plan


def test_wrong_schema():
    yaml_text = SPEC_YAML.replace("schema: eggs-plan/v2", "schema: eggs-plan/v1")
    with pytest.raises(PlanError) as exc_info:
        parse_plan(yaml_text)
    assert exc_info.value.path == "schema"


def test_bad_date():
    yaml_text = SPEC_YAML.replace("due: 2026-11-20", "due: 20/11")
    with pytest.raises(PlanError) as exc_info:
        parse_plan(yaml_text)
    assert exc_info.value.path == "flows[0].due"


def test_split_codes():
    assert split_codes("[CH-04,CH-05] Thanh toán") == (
        ["CH-04", "CH-05"],
        "Thanh toán",
    )
    assert split_codes("[DEMO] Loyalty") == ([], "[DEMO] Loyalty")
    assert split_codes("[HMKPOS][CH-01] X") == (["CH-01"], "[HMKPOS] X")


def test_format_summary():
    assert format_summary(["CH-08"], "Check-in") == "[CH-08] Check-in"
    assert (
        format_summary(["CH-04", "CH-05"], "Thanh toán") == "[CH-04,CH-05] Thanh toán"
    )
    assert format_summary([], "Check-in") == "Check-in"


TYPOS_YAML = """
schema: eggs-plan/v2
project: HMKLOYAL
stories:
  - actor: CH
epic: { summary: "Epic", due: 2026-11-27, owner: ba.username, ownr: ba.username }
flows:
  - code: CH-01
    actor: CH
    summary: "Check-in"
    ac: ["Có mã đơn"]
    summary_note: "ghi chú lạ"
    parts:
      - { part: BE, assignee: huy.truong, estimate: 1d, summary: "Dev task summary" }
"""


def test_parse_collects_unknown_keys_at_every_level():
    plan = parse_plan(TYPOS_YAML)

    found = [(u.key, u.location) for u in plan.unknown_keys]
    assert found == [
        ("stories", ""),
        ("ownr", "epic"),
        ("summary_note", "flows[0]"),
        ("summary", "flows[0].parts[0]"),
    ]
    assert plan.unknown_keys[0].path == "stories"
    assert plan.unknown_keys[3].path == "flows[0].parts[0].summary"


def test_unknown_keys_are_not_dumped():
    plan = parse_plan(TYPOS_YAML)
    dumped = dump_plan(plan)

    assert "summary_note" not in dumped
    assert "ownr" not in dumped
