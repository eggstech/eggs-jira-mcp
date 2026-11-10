from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import WorkCalendar
from .config import EggsConfig
from .identity import Identity
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
            "finish",
            "xong ADHOC-5, không cần QC",
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
        name="plan_input",
        roles=frozenset({"lead_ba", "lead"}),
        examples=["tạo card từ tài liệu", "tạo luồng từ spec", "/eggs tạo card từ ..."],
        tool=None,
        required=[],
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
                elif issue.parent_type == "adhoc":
                    actions.append(
                        SuggestedAction(
                            tool="eggs_finish", args={"key": key}, missing=[]
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

    # Priority 4: QC queue (work items in ready_for_qc whose Story QC Owner
    # is identity, and Stories in ready_for_qc where identity is QC Owner)
    for key, issue in snapshot.issues.items():
        if issue.frozen or issue.is_resolved or key in seen_keys:
            continue
        is_qc_target = False
        if issue.is_work_item and issue.status == "ready_for_qc":
            parent_story = (
                snapshot.issues.get(issue.parent_key) if issue.parent_key else None
            )
            story_qc = (
                parent_story.qc_owner if parent_story else None
            ) or issue.qc_owner
            if story_qc == identity.username:
                is_qc_target = True
        elif (
            issue.type == "story"
            and issue.status == "ready_for_qc"
            and issue.qc_owner == identity.username
        ):
            is_qc_target = True

        if is_qc_target:
            title = f"[{key}] Chờ kiểm thử: {issue.summary}"
            say = f"{key} đã sẵn sàng kiểm thử (QC: {identity.username})."
            actions = [
                SuggestedAction(tool="eggs_qc_start", args={"key": key}, missing=[])
            ]
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

    # Priority 7: own items in in_progress
    for key, issue in snapshot.issues.items():
        if (
            issue.is_work_item
            and issue.status == "in_progress"
            and issue.assignee == identity.username
            and not issue.is_resolved
            and not issue.frozen
        ):
            if key in seen_keys:
                continue
            title = f"[{key}] Đang làm: {issue.summary}"
            say = f"Bạn đang thực hiện {key} ({issue.summary})."
            actions = []
            if issue.type == "adhoc":
                actions.append(
                    SuggestedAction(tool="eggs_finish", args={"key": key}, missing=[])
                )
                actions.append(
                    SuggestedAction(
                        tool="eggs_submit_qc",
                        args={"key": key},
                        missing=[] if issue.qc_owner else ["qc_owner"],
                    )
                )
            elif issue.parent_type == "adhoc":
                actions.append(
                    SuggestedAction(tool="eggs_finish", args={"key": key}, missing=[])
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
        proj, num = _natural_key(it.key)
        if it.priority == 1 and it.key and it.key in snapshot.escalations:
            esc = snapshot.escalations[it.key]
            # Higher level first (-2 before -1 before 0)
            return (1, -esc.level, proj, num)
        return (it.priority, 0, proj, num)

    filtered_items.sort(key=sort_key)
    return filtered_items
