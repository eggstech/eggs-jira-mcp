from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import WorkCalendar
from .config import EggsConfig
from .identity import Identity
from .model import Issue
from .report import Snapshot


@dataclass(frozen=True)
class SuggestedAction:
    tool: str
    args: dict[str, Any]
    missing: list[str]


@dataclass(frozen=True)
class AgendaItem:
    priority: int
    key: str | None
    project: str | None
    title: str
    say: str
    actions: list[SuggestedAction]


@dataclass(frozen=True)
class Intent:
    name: str
    roles: frozenset[str]
    examples: list[str]
    tool: str | None
    required: list[str]


INTENTS: list[Intent] = [
    # General / Anyone
    Intent(
        name="my_work",
        roles=frozenset({"any"}),
        examples=["việc của tôi", "hôm nay làm gì", "/eggs", "agenda"],
        tool="eggs_next",
        required=[],
    ),
    Intent(
        name="create_issue",
        roles=frozenset({"qc", "lead_ba", "dev"}),
        examples=[
            "tạo bug",
            "tạo 3 bug liên quan EGGSBOX-38",
            "tạo story",
            "tạo task ad-hoc",
        ],
        tool="eggs_create_issue",
        required=["project", "issue_type", "summary"],
    ),
    Intent(
        name="log_bug",
        roles=frozenset({"qc", "lead_ba"}),
        examples=[
            "log bug EGGSBOX-10: nút Lưu không phản hồi",
            "đang test EGGSBOX-10 thấy lỗi",
            "thêm bug cho story đang test",
        ],
        tool="eggs_create_issue",
        required=["parent", "summary"],
    ),
    Intent(
        name="log_work",
        roles=frozenset({"any"}),
        examples=["hôm qua làm 4h RFID-128", "ghi 2h vào RFID-10", "log work 1d"],
        tool="eggs_log_work",
        required=["key", "time_spent", "note"],
    ),
    Intent(
        name="block",
        roles=frozenset({"any"}),
        examples=["báo block", "RFID-10 bị block do đối tác", "tạm dừng vì thiếu api"],
        tool="eggs_block",
        required=["key", "reason", "blocked_by"],
    ),
    Intent(
        name="unblock",
        roles=frozenset({"any"}),
        examples=["gỡ block RFID-10", "hết block", "unblock"],
        tool="eggs_unblock",
        required=["key"],
    ),
    Intent(
        name="end_day",
        roles=frozenset({"any"}),
        examples=["cập nhật cuối ngày", "kết thúc ngày", "hết ngày"],
        tool="eggs_end_day",
        required=["entries"],
    ),
    # Dev
    Intent(
        name="schedule",
        roles=frozenset({"dev"}),
        examples=[
            "nhận việc và đặt lịch RFID-130",
            "lên lịch RFID-130 từ 18/11 đến 21/11",
            "schedule",
        ],
        tool="eggs_schedule",
        required=["key", "start", "end", "estimate"],
    ),
    Intent(
        name="start",
        roles=frozenset({"dev"}),
        examples=["bắt đầu RFID-130", "bắt đầu làm", "start task"],
        tool="eggs_start",
        required=["key"],
    ),
    Intent(
        name="submit_qc",
        roles=frozenset({"dev"}),
        examples=[
            "xong RFID-12, chuyển QC",
            "xong bug dưới adhoc, chuyển QC",
            "chuyển kiểm thử",
            "submit qc",
            "xong ADHOC-5, nhờ tuan.bui test",
        ],
        tool="eggs_submit_qc",
        required=["key"],
    ),
    Intent(
        name="finish",
        roles=frozenset({"dev"}),
        examples=[
            "hoàn thành việc không cần QC",
            "xong task adhoc",
            "xong dev task dưới bug stand-alone",
            "finish",
            "xong ADHOC-5, không cần QC",
            "xong RFID-130, phần này không cần QC riêng",
        ],
        tool="eggs_finish",
        required=["key"],
    ),
    Intent(
        name="request_reschedule",
        roles=frozenset({"dev"}),
        examples=[
            "xin dời lịch RFID-123 sang 20/11 vì bị chèn ngang",
            "dời hạn do bug",
            "xin lùi ngày",
        ],
        tool="eggs_request_reschedule",
        required=["key", "proposed_end", "reason"],
    ),
    # QC
    Intent(
        name="qc_start",
        roles=frozenset({"qc"}),
        examples=["bắt đầu test RFID-12", "bắt đầu kiểm thử", "qc start"],
        tool="eggs_qc_start",
        required=["key"],
    ),
    Intent(
        name="qc_pass",
        roles=frozenset({"qc"}),
        examples=["test pass RFID-12", "kiểm thử đạt", "qc pass"],
        tool="eggs_qc_pass",
        required=["key"],
    ),
    Intent(
        name="qc_fail",
        roles=frozenset({"qc"}),
        examples=["test fail RFID-12 lỗi crash app", "kiểm thử không đạt", "qc fail"],
        tool="eggs_qc_fail",
        required=["key", "reason"],
    ),
    Intent(
        name="triage_open",
        roles=frozenset({"qc"}),
        examples=["ghi nhận phản ánh từ khách hàng", "tạo user report", "triage open"],
        tool="eggs_triage_open",
        required=["project", "text", "source_url", "reported_by"],
    ),
    Intent(
        name="triage_close",
        roles=frozenset({"qc"}),
        examples=[
            "kết luận phản ánh RFID-R12 là lỗi",
            "đóng report hướng dẫn sử dụng",
            "triage close",
        ],
        tool="eggs_triage_close",
        required=["key", "verdict", "note", "time_spent"],
    ),
    # Lead-BA / Lead
    Intent(
        name="decide_reschedule",
        roles=frozenset({"lead_ba", "lead"}),
        examples=[
            "duyệt dời lịch RFID-123",
            "từ chối gia hạn RFID-123",
            "decide reschedule",
        ],
        tool="eggs_decide_reschedule",
        required=["key", "approve", "note"],
    ),
    Intent(
        name="flow_reschedule",
        roles=frozenset({"lead_ba"}),
        examples=["dời hạn luồng RFID-45", "đổi due date story", "flow reschedule"],
        tool="eggs_flow_reschedule",
        required=["key", "proposed_due", "reason"],
    ),
    Intent(
        name="uat",
        roles=frozenset({"lead_ba"}),
        examples=[
            "bắt đầu UAT nhóm CH epic RFID-10",
            "ký nhận UAT nhóm KT",
            "UAT fail nhóm POS",
        ],
        tool="eggs_uat",
        required=["epic_key", "actor", "action", "note"],
    ),
    Intent(
        name="cancel",
        roles=frozenset({"lead_ba"}),
        examples=["hủy task RFID-12", "hủy luồng do thay đổi yêu cầu", "cancel card"],
        tool="eggs_cancel",
        required=["key", "reason"],
    ),
    Intent(
        name="workstream_status",
        roles=frozenset({"lead_ba", "lead"}),
        examples=["tình hình dự án", "tiến độ epic RFID-10", "dự án tới đâu rồi"],
        tool="eggs_workstream_status",
        required=[],
    ),
    Intent(
        name="daily_report",
        roles=frozenset({"lead"}),
        examples=["báo cáo hôm nay", "báo cáo ngày", "daily report"],
        tool="eggs_daily_report",
        required=[],
    ),
    Intent(
        name="pending_approvals",
        roles=frozenset({"lead_ba", "lead"}),
        examples=[
            "danh sách chờ duyệt",
            "có ai xin dời lịch không",
            "pending approvals",
        ],
        tool="eggs_pending_approvals",
        required=[],
    ),
    Intent(
        name="plan_cards",
        roles=frozenset({"lead_ba", "lead"}),
        examples=[
            "tạo card từ tài liệu này",
            "lên card cho spec",
            "tạo story từ yêu cầu",
        ],
        tool="eggs_plan_validate",
        required=["plan_yaml"],
    ),
    Intent(
        name="migrate_epic",
        roles=frozenset({"lead_ba", "lead"}),
        examples=["chuyển đổi Epic RFID-532", "migrate epic HH-317"],
        tool="eggs_migrate_epic",
        required=["epic"],
    ),
]


def intents_for(identity: Identity) -> list[Intent]:
    """Return intents available to this identity across all their project roles."""
    user_roles: set[str] = {"any"}
    for proj_roles in identity.roles.values():
        user_roles.update(proj_roles)
    if identity.is_lead:
        user_roles.add("lead")

    return [it for it in INTENTS if bool(it.roles & user_roles)]


def mentioned_keys(text: str, project_keys: set[str]) -> list[str]:
    """Find issue keys mentioned in text that belong to known project_keys,
    ordered and deduplicated. Case-insensitive normalization to upper.
    Project keys may contain underscores.
    """
    pattern = re.compile(r"\b([A-Za-z0-9_]+)-(\d+)\b")
    norm_project_keys = {p.strip().upper() for p in project_keys}
    seen: set[str] = set()
    result: list[str] = []
    for match in pattern.finditer(text):
        proj = match.group(1).upper()
        if proj in norm_project_keys:
            key = f"{proj}-{match.group(2)}"
            if key not in seen:
                seen.add(key)
                result.append(key)
    return result


def _natural_key(key: str | None) -> tuple[str, int]:
    if not key:
        return ("", 0)
    if "-" in key:
        proj, _, num = key.rpartition("-")
        try:
            return (proj, int(num))
        except ValueError:
            return (key, 0)
    return (key, 0)


def allowed_actions(issue: Issue, identity: Identity, cfg: EggsConfig) -> set[str]:
    """Return the set of actions allowed for `identity` on `issue`.
    Contains both tool names ('eggs_<action>') and bare action names ('<action>').
    """
    if issue is None:
        return set()

    res: set[str] = set()

    def add(act: str) -> None:
        bare = act.removeprefix("eggs_")
        res.add(bare)
        res.add(f"eggs_{bare}")

    proj = issue.project
    proj_roles: set[str] = set()
    for p, r in identity.roles.items():
        if p.upper() == (proj or "").upper():
            proj_roles.update(r)

    is_lead = identity.is_lead
    is_lead_ba = ("lead_ba" in proj_roles) or is_lead
    has_dev_role = ("dev" in proj_roles) or is_lead
    has_qc_role = ("qc" in proj_roles) or is_lead

    username = (identity.username or "").lower()
    assignee = (issue.assignee or "").lower() if issue.assignee else None
    is_assignee = bool(username and assignee and username == assignee)

    qc_owner = (issue.qc_owner or "").lower() if issue.qc_owner else None
    is_qc_owner = bool(username and qc_owner and username == qc_owner)
    is_qc = is_qc_owner or has_qc_role

    status = (cfg.status_logical(issue.status) or issue.status or "").lower()
    if status == "dev_todo":
        status = "todo"
    elif status == "qc_to_do":
        status = "ready_for_qc"
    elif status == "test_in_progress":
        status = "testing"

    is_in_qc = status in ("ready_for_qc", "testing")
    is_resolved = status in ("done", "cancelled") or issue.is_resolved

    if issue.frozen or is_resolved:
        return res

    # 1. Assignee-only actions
    # start:
    if issue.is_work_item and status in ("todo", "open") and is_assignee:
        add("start")

    # finish:
    if (
        issue.is_work_item
        and issue.type not in ("bug", "bug_sub")
        and status == "in_progress"
        and not is_in_qc
        and is_assignee
    ):
        add("finish")

    # submit_qc:
    if (
        issue.is_work_item
        and not (issue.type == "dev_task" and issue.parent_type in ("adhoc", "bug"))
        and status == "in_progress"
        and not is_in_qc
        and is_assignee
    ):
        add("submit_qc")

    # log_work on own work / during testing / lead_ba:
    if is_assignee or (is_qc and is_in_qc) or is_lead_ba:
        add("log_work")

    # block:
    if (
        issue.is_work_item
        and not issue.blocked
        and status in ("todo", "open", "in_progress")
        and is_assignee
    ):
        add("block")

    # unblock:
    if issue.blocked and is_assignee:
        add("unblock")

    # request_reschedule:
    if is_assignee or is_lead_ba:
        add("request_reschedule")

    # 2. QC actions
    # qc_start: status maps to ready_for_qc
    if status == "ready_for_qc" and is_qc:
        if issue.is_work_item or issue.type == "story":
            add("qc_start")

    # qc_pass / qc_fail: status maps to testing
    if status == "testing" and is_qc:
        if issue.is_work_item or issue.type == "story":
            add("qc_pass")
            add("qc_fail")

    # 3. lead_ba actions
    if is_lead_ba:
        add("decide_reschedule")
        if issue.type in ("story", "epic"):
            add("flow_reschedule")
        if issue.type == "epic":
            add("uat")
        add("cancel")

    # schedule:
    if issue.is_work_item and issue.baseline_end is None and status in ("todo", "open"):
        if is_lead_ba or is_assignee or (issue.assignee is None and has_dev_role):
            add("schedule")

    # triage_close:
    if (
        issue.type == "report"
        and status in ("open", "analyzing")
        and (is_assignee or has_qc_role or is_lead_ba)
    ):
        add("triage_close")

    return res


def build_agenda(
    snapshot: Snapshot,
    identity: Identity,
    cfg: EggsConfig,
    now: datetime,
    cal: WorkCalendar,
) -> list[AgendaItem]:
    """Build a prioritized list of agenda items for the identity."""
    # A user without any roles in any project has an empty agenda
    if not identity.roles and not identity.is_lead:
        return []

    items: list[AgendaItem] = []
    seen_keys: set[str] = set()

    # Priority 1: Flags where identity is current escalation owner (L2/L1 before L0)
    # (Exclude level 0 APPROVAL_PENDING which goes into Priority 2)
    for key, esc in snapshot.escalations.items():
        if esc.owner == identity.username:
            item_flags = snapshot.flags.get(key, frozenset())
            if "APPROVAL_PENDING" in item_flags and esc.level == 0:
                continue  # Handled in Priority 2
            issue = snapshot.issues.get(key)
            if not issue or issue.is_resolved or issue.frozen:
                continue

            flag_str = ", ".join(sorted(item_flags))
            title = f"[{key}] Cờ {flag_str} (mức {esc.level}): {issue.summary}"
            say = (
                f"{key} đang gặp cờ {flag_str} (leo thang mức {esc.level}), cần xử lý."
            )
            actions: list[SuggestedAction] = []
            if "APPROVAL_PENDING" in item_flags:
                actions.append(
                    SuggestedAction(
                        tool="eggs_decide_reschedule",
                        args={"key": key},
                        missing=["approve", "note"],
                    )
                )
            elif issue.type == "story" and (
                "AT_RISK" in item_flags or "UNSCHEDULED" in item_flags
            ):
                actions.append(
                    SuggestedAction(
                        tool="eggs_flow_reschedule",
                        args={"key": key},
                        missing=["proposed_due", "reason"],
                    )
                )
            elif issue.is_work_item and (
                "OVERDUE" in item_flags or "NEED_RESCHEDULE" in item_flags
            ):
                if issue.type == "adhoc":
                    actions.append(
                        SuggestedAction(
                            tool="eggs_finish", args={"key": key}, missing=[]
                        )
                    )
                    actions.append(
                        SuggestedAction(
                            tool="eggs_submit_qc",
                            args={"key": key},
                            missing=[] if issue.qc_owner else ["qc_owner"],
                        )
                    )
                elif issue.type == "dev_task" and issue.parent_type in (
                    "adhoc",
                    "bug",
                ):
                    actions.append(
                        SuggestedAction(
                            tool="eggs_finish", args={"key": key}, missing=[]
                        )
                    )
                elif issue.type == "dev_task" and issue.parent_type == "story":
                    actions.append(
                        SuggestedAction(
                            tool="eggs_submit_qc", args={"key": key}, missing=[]
                        )
                    )
                    actions.append(
                        SuggestedAction(
                            tool="eggs_finish", args={"key": key}, missing=[]
                        )
                    )
                elif issue.type == "bug":
                    actions.append(
                        SuggestedAction(
                            tool="eggs_submit_qc",
                            args={"key": key},
                            missing=[] if issue.qc_owner else ["qc_owner"],
                        )
                    )
                else:
                    actions.append(
                        SuggestedAction(
                            tool="eggs_submit_qc", args={"key": key}, missing=[]
                        )
                    )
                actions.append(
                    SuggestedAction(
                        tool="eggs_request_reschedule",
                        args={"key": key},
                        missing=["proposed_end", "reason"],
                    )
                )
            elif issue.is_work_item and "UNSCHEDULED" in item_flags:
                actions.append(
                    SuggestedAction(
                        tool="eggs_schedule",
                        args={"key": key},
                        missing=["start", "end", "estimate"],
                    )
                )
            elif "STALE" in item_flags:
                actions.append(
                    SuggestedAction(
                        tool="eggs_log_work",
                        args={"key": key},
                        missing=["time_spent"],
                    )
                )
            elif "BLOCKED" in item_flags:
                actions.append(
                    SuggestedAction(tool="eggs_unblock", args={"key": key}, missing=[])
                )

            # Filter SuggestedActions with allowed_actions
            allowed = allowed_actions(issue, identity, cfg)
            filtered_actions = [
                a
                for a in actions
                if a.tool in allowed or a.tool.removeprefix("eggs_") in allowed
            ]

            status_norm = (
                cfg.status_logical(issue.status) or issue.status or ""
            ).lower()
            if status_norm == "qc_to_do":
                status_norm = "ready_for_qc"
            elif status_norm == "test_in_progress":
                status_norm = "testing"

            is_in_qc = status_norm in ("ready_for_qc", "testing")
            username = (identity.username or "").lower()
            qc_owner = (issue.qc_owner or "").lower() if issue.qc_owner else None
            is_qc_owner = bool(username and qc_owner and username == qc_owner)

            if actions and not filtered_actions:
                if is_in_qc and is_qc_owner:
                    if status_norm == "ready_for_qc":
                        filtered_actions = [
                            SuggestedAction(
                                tool="eggs_qc_start", args={"key": key}, missing=[]
                            )
                        ]
                        say = f"{key} đang chờ bạn test (cờ {flag_str})."
                    else:
                        open_bugs = [
                            k
                            for k, iss in snapshot.issues.items()
                            if iss.parent_key == key
                            and iss.type == "bug_sub"
                            and not iss.is_resolved
                            and iss.status not in ("done", "cancelled")
                        ]
                        open_bugs.sort(key=lambda k: _natural_key(k))
                        if open_bugs:
                            say = (
                                f"{key} đang test, còn {len(open_bugs)} bug "
                                f"đang sửa: {', '.join(open_bugs)}"
                            )
                            filtered_actions = []
                        else:
                            filtered_actions = [
                                SuggestedAction(
                                    tool="eggs_qc_pass", args={"key": key}, missing=[]
                                ),
                                SuggestedAction(
                                    tool="eggs_qc_fail",
                                    args={"key": key},
                                    missing=["reason"],
                                ),
                            ]
                            say = f"{key} đang chờ bạn test (cờ {flag_str})."
                else:
                    # Informational for identity
                    if issue.assignee and issue.assignee.lower() != username:
                        if "NEED_RESCHEDULE" in item_flags:
                            say = f"Chờ {issue.assignee} xin dời lịch"
                        else:
                            say = f"Chờ {issue.assignee} xử lý"
                        filtered_actions = []
                    else:
                        continue
            elif not actions and not filtered_actions:
                continue

            actions = filtered_actions

            items.append(
                AgendaItem(
                    priority=1,
                    key=key,
                    project=issue.project,
                    title=title,
                    say=say,
                    actions=actions,
                )
            )
            seen_keys.add(key)

    # Priority 2: Reschedules awaiting identity's decision
    for key, esc in snapshot.escalations.items():
        if esc.owner == identity.username:
            item_flags = snapshot.flags.get(key, frozenset())
            if "APPROVAL_PENDING" in item_flags and esc.level == 0:
                if key in seen_keys:
                    continue
                issue = snapshot.issues.get(key)
                if not issue or issue.is_resolved or issue.frozen:
                    continue
                title = f"[{key}] Đề xuất dời lịch chờ duyệt: {issue.summary}"
                say = f"{key} đang chờ bạn duyệt dời lịch sang {issue.proposed_end}."
                actions = [
                    SuggestedAction(
                        tool="eggs_decide_reschedule",
                        args={"key": key},
                        missing=["approve", "note"],
                    )
                ]
                allowed = allowed_actions(issue, identity, cfg)
                actions = [
                    a
                    for a in actions
                    if a.tool in allowed or a.tool.removeprefix("eggs_") in allowed
                ]
                if not actions:
                    continue
                items.append(
                    AgendaItem(
                        priority=2,
                        key=key,
                        project=issue.project,
                        title=title,
                        say=say,
                        actions=actions,
                    )
                )
                seen_keys.add(key)

    # Priority 3: User Reports assigned to identity in open/analyzing
    for key, issue in snapshot.issues.items():
        if (
            issue.type == "report"
            and issue.status in {"open", "analyzing"}
            and issue.assignee == identity.username
            and not issue.frozen
        ):
            if key in seen_keys:
                continue
            title = f"[{key}] Phản ánh cần xử lý: {issue.summary}"
            say = (
                f"Phản ánh {key} ({issue.summary}) đang được giao cho bạn, "
                "cần xác minh/kết luận."
            )
            actions = [
                SuggestedAction(
                    tool="eggs_triage_close",
                    args={"key": key},
                    missing=["verdict", "time_spent", "note"],
                )
            ]
            allowed = allowed_actions(issue, identity, cfg)
            actions = [
                a
                for a in actions
                if a.tool in allowed or a.tool.removeprefix("eggs_") in allowed
            ]
            if not actions:
                continue
            items.append(
                AgendaItem(
                    priority=3,
                    key=key,
                    project=issue.project,
                    title=title,
                    say=say,
                    actions=actions,
                )
            )
            seen_keys.add(key)

    # Priority 4: QC queue (work items in ready_for_qc whose parent
    # Story/Ad-hoc QC Owner is identity, and Stories in ready_for_qc
    # where identity is QC Owner)
    for key, issue in snapshot.issues.items():
        if issue.frozen or issue.is_resolved or key in seen_keys:
            continue
        is_qc_target = False
        if issue.is_work_item and issue.status == "ready_for_qc":
            parent = snapshot.issues.get(issue.parent_key) if issue.parent_key else None
            target_qc = issue.qc_owner or (
                parent.qc_owner
                if parent and parent.type in ("story", "adhoc")
                else None
            )
            if target_qc and target_qc == identity.username:
                is_qc_target = True
        elif (
            issue.type == "story"
            and issue.status == "ready_for_qc"
            and issue.qc_owner == identity.username
        ):
            is_qc_target = True
        elif (
            issue.type in ("story", "adhoc")
            and issue.status == "testing"
            and issue.qc_owner == identity.username
        ):
            open_bugs = [
                k
                for k, iss in snapshot.issues.items()
                if iss.parent_key == key
                and iss.type == "bug_sub"
                and not iss.is_resolved
                and iss.status not in ("done", "cancelled")
            ]
            open_bugs.sort(key=lambda k: _natural_key(k))
            if open_bugs:
                title = f"[{key}] Đang kiểm thử: {issue.summary}"
                open_str = ", ".join(open_bugs)
                say = f"{key} đang test, còn {len(open_bugs)} bug đang sửa: {open_str}"
                items.append(
                    AgendaItem(
                        priority=4,
                        key=key,
                        project=issue.project,
                        title=title,
                        say=say,
                        actions=[],
                    )
                )
                seen_keys.add(key)
                continue

        if is_qc_target:
            title = f"[{key}] Chờ kiểm thử: {issue.summary}"
            say = f"{key} đã sẵn sàng kiểm thử (QC: {identity.username})."
            actions = [
                SuggestedAction(tool="eggs_qc_start", args={"key": key}, missing=[])
            ]
            allowed = allowed_actions(issue, identity, cfg)
            actions = [
                a
                for a in actions
                if a.tool in allowed or a.tool.removeprefix("eggs_") in allowed
            ]
            if not actions:
                continue
            items.append(
                AgendaItem(
                    priority=4,
                    key=key,
                    project=issue.project,
                    title=title,
                    say=say,
                    actions=actions,
                )
            )
            seen_keys.add(key)

    # Priority 5: (epic, actor) groups ready for UAT where identity
    # has Lead-BA role in that project
    for epic_key, em in snapshot.epics.items():
        epic_issue = snapshot.issues.get(epic_key)
        if not epic_issue or epic_issue.frozen or epic_issue.is_resolved:
            continue
        proj_roles = identity.roles.get(epic_issue.project, set())
        if "lead_ba" in proj_roles or identity.is_lead:
            for act in em.actors:
                if act.ready_for_uat > 0 and act.dev == 0 and act.qc == 0:
                    actor_name = act.actor
                    title = (
                        f"[{epic_key}] Nhóm {actor_name} sẵn sàng UAT "
                        f"({act.ready_for_uat}/{act.total} luồng)"
                    )
                    say = (
                        f"Epic {epic_key} có nhóm {actor_name} đã sẵn sàng UAT, "
                        "có thể mời khách hàng kiểm thử."
                    )
                    actions = [
                        SuggestedAction(
                            tool="eggs_uat",
                            args={
                                "epic_key": epic_key,
                                "actor": act.actor,
                                "action": "start",
                            },
                            missing=["note"],
                        )
                    ]
                    allowed = allowed_actions(epic_issue, identity, cfg)
                    actions = [
                        a
                        for a in actions
                        if a.tool in allowed or a.tool.removeprefix("eggs_") in allowed
                    ]
                    if not actions:
                        continue
                    items.append(
                        AgendaItem(
                            priority=5,
                            key=epic_key,
                            project=epic_issue.project,
                            title=title,
                            say=say,
                            actions=actions,
                        )
                    )

    # Priority 5 (Analyzed stories with Dev Tasks having Part + estimate for lead_ba):
    for key, story in snapshot.issues.items():
        if story.type == "story" and not story.frozen and not story.is_resolved:
            status_logical = (
                cfg.status_logical(story.status) or story.status or ""
            ).lower()
            if status_logical in ("analyzing", "analyzed"):
                proj_roles = identity.roles.get(story.project, set())
                if "lead_ba" in proj_roles or identity.is_lead:
                    dev_tasks = [
                        iss
                        for iss in snapshot.issues.values()
                        if iss.parent_key == key and iss.type == "dev_task"
                    ]
                    if dev_tasks and all(
                        bool(dt.part) and dt.estimate_seconds is not None
                        for dt in dev_tasks
                    ):
                        title = f"[{key}] Đã đủ Dev Task và estimate: {story.summary}"
                        say = (
                            f"{key} đã đủ Dev Task và estimate: "
                            "chốt Due Date rồi Ready For Dev"
                        )
                        items.append(
                            AgendaItem(
                                priority=5,
                                key=key,
                                project=story.project,
                                title=title,
                                say=say,
                                actions=[],
                            )
                        )

    # Priority 6: "cập nhật cuối ngày" when now.hour >= end_day_hour
    # on working days (cal.is_working_day), and identity has no worklog dated today.
    # Convert now and every worklog.started to ZoneInfo(cfg.timezone)
    # before taking .hour / .date().
    tz = ZoneInfo(cfg.timezone)
    local_now = (
        now.astimezone(tz)
        if now.tzinfo is not None
        else now.replace(tzinfo=timezone.utc).astimezone(tz)
    )
    end_day_hour = cfg.thresholds.end_day_hour
    if cal.is_working_day(local_now.date()) and local_now.hour >= end_day_hour:
        has_worklog_today = False
        for wl in snapshot.worklogs:
            if wl.author == identity.username:
                wl_dt = (
                    wl.started.astimezone(tz)
                    if wl.started.tzinfo is not None
                    else wl.started.replace(tzinfo=timezone.utc).astimezone(tz)
                )
                if wl_dt.date() == local_now.date():
                    has_worklog_today = True
                    break

        if not has_worklog_today:
            say = (
                "Đã đến giờ cập nhật cuối ngày (sau 17:00) và bạn chưa có "
                "worklog hôm nay. Vui lòng ghi giờ hoặc báo kết thúc ngày."
            )
            items.append(
                AgendaItem(
                    priority=6,
                    key=None,
                    project=None,
                    title="Cập nhật cuối ngày",
                    say=say,
                    actions=[
                        SuggestedAction(
                            tool="eggs_end_day", args={}, missing=["entries"]
                        )
                    ],
                )
            )

    # Priority 7: own items in in_progress or todo
    for key, issue in snapshot.issues.items():
        if (
            issue.is_work_item
            and issue.status in ("in_progress", "todo")
            and issue.assignee == identity.username
            and not issue.is_resolved
            and not issue.frozen
        ):
            if key in seen_keys:
                continue
            actions = []
            if issue.status == "todo":
                title = f"[{key}] Cần làm: {issue.summary}"
                say = (
                    f"{key} ({issue.summary}) đã được giao cho bạn, "
                    "cần bắt đầu thực hiện."
                )
                actions.append(
                    SuggestedAction(tool="eggs_start", args={"key": key}, missing=[])
                )
            else:
                title = f"[{key}] Đang làm: {issue.summary}"
                say = f"Bạn đang thực hiện {key} ({issue.summary})."
                if issue.type == "adhoc":
                    actions.append(
                        SuggestedAction(
                            tool="eggs_finish", args={"key": key}, missing=[]
                        )
                    )
                    actions.append(
                        SuggestedAction(
                            tool="eggs_submit_qc",
                            args={"key": key},
                            missing=[] if issue.qc_owner else ["qc_owner"],
                        )
                    )
                elif issue.type == "dev_task" and issue.parent_type in (
                    "adhoc",
                    "bug",
                ):
                    actions.append(
                        SuggestedAction(
                            tool="eggs_finish", args={"key": key}, missing=[]
                        )
                    )
                elif issue.type == "dev_task" and issue.parent_type == "story":
                    actions.append(
                        SuggestedAction(
                            tool="eggs_submit_qc", args={"key": key}, missing=[]
                        )
                    )
                    actions.append(
                        SuggestedAction(
                            tool="eggs_finish", args={"key": key}, missing=[]
                        )
                    )
                elif issue.type == "bug":
                    actions.append(
                        SuggestedAction(
                            tool="eggs_submit_qc",
                            args={"key": key},
                            missing=[] if issue.qc_owner else ["qc_owner"],
                        )
                    )
                else:
                    actions.append(
                        SuggestedAction(
                            tool="eggs_submit_qc", args={"key": key}, missing=[]
                        )
                    )
                actions.append(
                    SuggestedAction(
                        tool="eggs_log_work",
                        args={"key": key},
                        missing=["time_spent", "note"],
                    )
                )
            allowed = allowed_actions(issue, identity, cfg)
            actions = [
                a
                for a in actions
                if a.tool in allowed or a.tool.removeprefix("eggs_") in allowed
            ]
            if not actions:
                continue
            items.append(
                AgendaItem(
                    priority=7,
                    key=key,
                    project=issue.project,
                    title=title,
                    say=say,
                    actions=actions,
                )
            )
            seen_keys.add(key)

    # Informational for Devs: Stories in Analyzed without Dev Tasks
    for proj, roles in identity.roles.items():
        if "dev" in roles:
            unsplit_stories = [
                k
                for k, iss in snapshot.issues.items()
                if iss.project == proj
                and iss.type == "story"
                and not iss.frozen
                and not iss.is_resolved
                and (cfg.status_logical(iss.status) or iss.status or "").lower()
                in ("analyzing", "analyzed")
                and not any(
                    child.parent_key == k and child.type == "dev_task"
                    for child in snapshot.issues.values()
                )
            ]
            if unsplit_stories:
                unsplit_stories.sort(key=lambda k: _natural_key(k))
                count = len(unsplit_stories)
                keys_str = ", ".join(unsplit_stories)
                title = f"[{proj}] {count} Story đã phân tích xong, chưa chia việc"
                say = f"{count} Story đã phân tích xong, chưa chia việc: {keys_str}"
                items.append(
                    AgendaItem(
                        priority=7,
                        key=None,
                        project=proj,
                        title=title,
                        say=say,
                        actions=[],
                    )
                )

    # Filter out projects where identity has no role
    # (except for the lead, who sees priority 1–2 items escalated to them)
    filtered_items: list[AgendaItem] = []
    for it in items:
        if it.project is None:
            filtered_items.append(it)
            continue
        has_role = bool(identity.roles.get(it.project))
        if has_role:
            filtered_items.append(it)
        elif identity.is_lead and it.priority in {1, 2}:
            filtered_items.append(it)

    # Sort items: by priority then natural key. For priority 1: L2/L1 before L0.
    def sort_key(it: AgendaItem) -> tuple[int, int, str, int]:
        proj, num = _natural_key(it.key) if it.key else (it.project or "", 0)
        if it.priority == 1 and it.key and it.key in snapshot.escalations:
            esc = snapshot.escalations[it.key]
            # Higher level first (-2 before -1 before 0)
            return (1, -esc.level, proj, num)
        if it.priority == 7 and it.key:
            iss = snapshot.issues.get(it.key)
            if iss and iss.type == "bug_sub" and iss.status == "todo":
                return (7, -1, proj, num)
        return (it.priority, 0, proj, num)

    filtered_items.sort(key=sort_key)
    return filtered_items
