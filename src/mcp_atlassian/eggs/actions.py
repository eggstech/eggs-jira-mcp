from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .calendar import WorkCalendar
from .config import EggsConfig
from .gateway import JiraError
from .mapping import issue_fields_for_write, issue_from_json
from .model import Issue


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    changes: list[str]
    next_actions: list[str]
    next: Any = None
    code: str | None = None


@dataclass(frozen=True)
class EndDayEntry:
    key: str
    time_spent: str
    note: str
    move_to: Literal["ready_for_qc", "done"] | None = None


class Actions:
    """Domain write actions for eggs toolset."""

    def __init__(
        self,
        gateway: Any,
        cfg: EggsConfig,
        cal: WorkCalendar,
        today_fn: Callable[[], date] | None = None,
    ) -> None:
        self.gateway = gateway
        self.cfg = cfg
        self.cal = cal
        self.today_fn = today_fn or (
            lambda: datetime.now(ZoneInfo(cfg.timezone)).date()
        )

    def _today(self) -> date:
        return self.today_fn()

    def _get_issue(self, key: str) -> Issue:
        raw = self.gateway.get_issue(key)
        return issue_from_json(raw, self.cfg)

    def _find_transition(
        self, key: str, logical_trans_name: str
    ) -> tuple[str | None, list[str]]:
        target_name = self.cfg.transitions[logical_trans_name]
        available = self.gateway.get_transitions(key)
        avail_names = [t["name"] for t in available]
        matched = next(
            (name for name in avail_names if name.lower() == target_name.lower()), None
        )
        return matched, avail_names

    def _unavailable_result(
        self, key: str, logical_trans_name: str, avail_names: list[str]
    ) -> ActionResult:
        target_name = self.cfg.transitions[logical_trans_name]
        avail_str = ", ".join(avail_names) if avail_names else "(không có)"
        return ActionResult(
            ok=False,
            message=(
                f"Transition '{target_name}' không khả dụng cho {key}. "
                f"Các transition hiện có: {avail_str}."
            ),
            changes=[],
            next_actions=[],
        )

    def _do_transition(
        self,
        key: str,
        transition_name: str,
        fields: dict | None = None,
        comment: str | None = None,
    ) -> ActionResult | None:
        try:
            self.gateway.transition(
                key, transition_name, fields=fields, comment=comment
            )
            return None
        except JiraError as e:
            if e.status_code == 400 or e.error_messages or e.errors:
                msgs = list(e.error_messages) + list(e.errors.values())
                msg = (
                    "\n".join(msgs)
                    if msgs
                    else (str(e) or "Lỗi từ Jira (400 Bad Request)")
                )
                return ActionResult(ok=False, message=msg, changes=[], next_actions=[])
            raise

    def _has_open_bug_subtask(self, story_issue: Issue) -> bool:
        subtasks = self.gateway.get_subtasks(story_issue.key)
        for st in subtasks:
            fields = st.get("fields", {})
            type_name = fields.get("issuetype", {}).get("name")
            status_name = fields.get("status", {}).get("name")
            if (
                type_name
                and self.cfg.type_logical(type_name, is_subtask=True) == "bug_sub"
            ):
                if status_name and self.cfg.status_logical(status_name) not in (
                    "done",
                    "cancelled",
                ):
                    return True
        return False

    def _get_open_subtasks(self, key: str) -> list[str]:
        subtasks = self.gateway.get_subtasks(key)
        open_keys: list[str] = []
        for st in subtasks:
            st_key = st.get("key", "") or str(st.get("id", ""))
            fields = st.get("fields", {})
            status_obj = fields.get("status", {})
            status_name = status_obj.get("name", "")
            logical = self.cfg.status_logical(status_name)
            is_done = logical in ("done", "cancelled")
            status_cat = status_obj.get("statusCategory", {}).get("key")
            if not is_done and status_cat == "done":
                is_done = True
            if not is_done:
                open_keys.append(st_key)
        return open_keys

    # -------------------------------------------------------------------------
    # Work items
    # -------------------------------------------------------------------------

    def schedule(
        self,
        key: str,
        start: date,
        end: date,
        estimate: str,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        if not issue.is_work_item:
            return ActionResult(
                ok=False,
                message=f"Issue {key} không phải là phần việc (work item).",
                changes=[],
                next_actions=[],
            )
        if issue.baseline_end is not None:
            return ActionResult(
                ok=False,
                message=(
                    f"Issue {key} đã có Baseline, "
                    "hãy xin dời lịch (request_reschedule)."
                ),
                changes=[],
                next_actions=["request_reschedule"],
            )
        if not estimate or not estimate.strip():
            return ActionResult(
                ok=False,
                message="Estimate là bắt buộc và không được để trống.",
                changes=[],
                next_actions=[],
            )
        if end < self._today():
            return ActionResult(
                ok=False,
                message=(
                    f"Planned End ({end}) không được trước ngày hôm nay "
                    f"({self._today()})."
                ),
                changes=[],
                next_actions=[],
            )
        if end < start:
            return ActionResult(
                ok=False,
                message=(
                    f"Planned End ({end}) không được trước Planned Start ({start})."
                ),
                changes=[],
                next_actions=[],
            )

        matched, avail = self._find_transition(key, "schedule")
        if not matched:
            return self._unavailable_result(key, "schedule", avail)

        changes = [
            f"Set Planned Start = {start}",
            f"Set Planned End = {end}",
            f"Set Estimate = {estimate.strip()}",
        ]

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ lên lịch cho {key}",
                changes=changes,
                next_actions=["start"],
            )

        payload = issue_fields_for_write(self.cfg, planned_start=start, planned_end=end)
        payload["timetracking"] = {"originalEstimate": estimate.strip()}
        err = self._do_transition(key, matched, fields=payload)
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã lên lịch thành công cho {key}.",
            changes=changes,
            next_actions=["start"],
        )

    def start(
        self,
        key: str,
        start: date | None = None,
        end: date | None = None,
        estimate: str | None = None,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        if not issue.is_work_item:
            return ActionResult(
                ok=False,
                message=f"Issue {key} không phải là phần việc.",
                changes=[],
                next_actions=[],
            )

        dates_passed = (
            start is not None
            or end is not None
            or (estimate is not None and bool(estimate.strip()))
        )

        if dates_passed:
            if issue.baseline_end is not None:
                return ActionResult(
                    ok=False,
                    message=(
                        f"Issue {key} đã có Baseline, "
                        "hãy xin dời lịch (request_reschedule)."
                    ),
                    changes=[],
                    next_actions=["request_reschedule"],
                )
            if start is None or end is None or not estimate or not estimate.strip():
                return ActionResult(
                    ok=False,
                    message=(
                        "Khi truyền ngày bắt đầu, cần cung cấp đầy đủ "
                        "start, end và estimate."
                    ),
                    changes=[],
                    next_actions=[],
                )
            sched_res = self.schedule(
                key, start=start, end=end, estimate=estimate, dry_run=True
            )
            if not sched_res.ok:
                return sched_res

            matched, avail = self._find_transition(key, "start")
            if not matched:
                return self._unavailable_result(key, "start", avail)

            changes = sched_res.changes + ["Chuyển trạng thái sang Dev In Progress"]
            if dry_run:
                return ActionResult(
                    ok=True,
                    message=f"[Dry-run] Sẽ lên lịch và bắt đầu thực hiện {key}",
                    changes=changes,
                    next_actions=["log_work", "submit_qc", "block"],
                )

            sched_write_res = self.schedule(
                key, start=start, end=end, estimate=estimate, dry_run=False
            )
            if not sched_write_res.ok:
                return sched_write_res

            err = self._do_transition(key, matched)
            if err:
                return err
            return ActionResult(
                ok=True,
                message=f"Đã lên lịch và bắt đầu thực hiện {key}.",
                changes=changes,
                next_actions=["log_work", "submit_qc", "block"],
            )

        if issue.baseline_end is None:
            return ActionResult(
                ok=False,
                message=(
                    f"Không thể bắt đầu: issue {key} chưa có lịch (Baseline End). "
                    "Vui lòng lên lịch trước khi bắt đầu."
                ),
                changes=[],
                next_actions=["schedule"],
            )

        matched, avail = self._find_transition(key, "start")
        if not matched:
            return self._unavailable_result(key, "start", avail)

        changes = ["Chuyển trạng thái sang Dev In Progress"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ bắt đầu thực hiện {key}",
                changes=changes,
                next_actions=["log_work", "submit_qc", "block"],
            )

        err = self._do_transition(key, matched)
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã bắt đầu thực hiện {key}.",
            changes=changes,
            next_actions=["log_work", "submit_qc", "block"],
        )

    def log_work(
        self,
        key: str,
        time_spent: str,
        note: str,
        kind: Literal["dev", "qc", "ba"] = "dev",
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        if not time_spent or not time_spent.strip():
            return ActionResult(
                ok=False,
                message="time_spent không được để trống.",
                changes=[],
                next_actions=[],
            )

        try:
            issue = self._get_issue(key)
        except (JiraError, KeyError, ValueError) as e:
            return ActionResult(
                ok=False,
                message=f"Issue {key} không tồn tại hoặc không thể truy cập: {e}",
                changes=[],
                next_actions=[],
            )

        if issue.is_resolved:
            return ActionResult(
                ok=False,
                message=f"Issue {key} đã hoàn thành (resolved), không thể log work.",
                changes=[],
                next_actions=[],
            )

        changes = [f"Ghi nhận {time_spent} ({kind}): {note}"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ log {time_spent} cho {key}",
                changes=changes,
                next_actions=[],
            )

        self.gateway.add_worklog(key, time_spent=time_spent, comment=note)
        return ActionResult(
            ok=True,
            message=f"Đã log {time_spent} cho {key}.",
            changes=changes,
            next_actions=[],
        )

    def submit_qc(
        self,
        key: str,
        time_spent: str | None = None,
        note: str = "",
        *,
        qc_owner: str | None = None,
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        if issue.type == "dev_task" and issue.parent_type in ("adhoc", "bug"):
            parent_desc = (
                "Ad-hoc Task" if issue.parent_type == "adhoc" else "Bug stand-alone"
            )
            return ActionResult(
                ok=False,
                message=(
                    f"Dev Task {key} thuộc {parent_desc} không qua QC. "
                    "Vui lòng dùng finish() để hoàn thành."
                ),
                changes=[],
                next_actions=["finish"],
            )

        if issue.type in ("adhoc", "bug"):
            open_subtasks = self._get_open_subtasks(key)
            if open_subtasks:
                sub_list = ", ".join(open_subtasks)
                type_name = (
                    "Ad-hoc Task" if issue.type == "adhoc" else "Bug stand-alone"
                )
                return ActionResult(
                    ok=False,
                    message=(
                        f"{type_name} {key} vẫn còn sub-task chưa hoàn thành "
                        f"({sub_list}). Cần hoàn thành tất cả sub-task trước khi "
                        "Submit QC."
                    ),
                    changes=[],
                    next_actions=[],
                )

        if qc_owner and issue.type not in ("adhoc", "bug"):
            return ActionResult(
                ok=False,
                message=(
                    "QC Owner của Dev Task/Bug con lấy từ Story hoặc Ad-hoc Task cha."
                ),
                changes=[],
                next_actions=[],
            )

        fields: dict[str, Any] | None = None
        effective_qc_owner: str | None = None
        if issue.type in ("adhoc", "bug"):
            effective_qc_owner = qc_owner or issue.qc_owner
            if not effective_qc_owner:
                if issue.type == "bug":
                    message = (
                        f"Bug stand-alone {key} cần có QC Owner để chuyển sang "
                        "QC TO DO. Ai sẽ kiểm thử task này? Vui lòng cung cấp "
                        "qc_owner."
                    )
                    next_acts = ["submit_qc"]
                else:
                    message = (
                        f"Ad-hoc Task {key} cần có QC Owner để chuyển sang "
                        "QC TO DO. Ai sẽ kiểm thử task này? Vui lòng cung cấp "
                        "qc_owner hoặc dùng finish() nếu không cần QC."
                    )
                    next_acts = ["submit_qc", "finish"]
                return ActionResult(
                    ok=False,
                    message=message,
                    changes=[],
                    next_actions=next_acts,
                    code="missing_qc_owner",
                )
            if qc_owner:
                qc_field_id = self.cfg.fields.get("qc_owner")
                if not qc_field_id:
                    return ActionResult(
                        ok=False,
                        message="Thiếu cấu hình fields.qc_owner",
                        changes=[],
                        next_actions=[],
                    )
                fields = {qc_field_id: {"name": qc_owner}}

        matched, avail = self._find_transition(key, "submit_qc")
        if not matched:
            return self._unavailable_result(key, "submit_qc", avail)

        changes = ["Chuyển trạng thái sang QC TO DO"]
        if issue.type in ("adhoc", "bug") and effective_qc_owner:
            if issue.qc_owner and qc_owner and issue.qc_owner != qc_owner:
                changes.append(f"Đổi QC Owner: {issue.qc_owner} → {qc_owner}")
            else:
                changes.append(f"QC Owner: {effective_qc_owner}")
        if time_spent:
            changes.append(f"Log work {time_spent}: {note}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ nộp QC cho {key}",
                changes=changes,
                next_actions=["qc_start"],
            )

        if time_spent:
            self.gateway.add_worklog(key, time_spent=time_spent, comment=note)

        err = self._do_transition(key, matched, fields=fields)
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã nộp QC thành công cho {key}.",
            changes=changes,
            next_actions=["qc_start"],
        )

    def finish(
        self,
        key: str,
        time_spent: str | None = None,
        note: str = "",
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        if issue.type in ("adhoc", "bug"):
            open_subtasks = self._get_open_subtasks(key)
            if open_subtasks:
                sub_list = ", ".join(open_subtasks)
                type_name = (
                    "Ad-hoc Task" if issue.type == "adhoc" else "Bug stand-alone"
                )
                return ActionResult(
                    ok=False,
                    message=(
                        f"{type_name} {key} vẫn còn sub-task chưa hoàn thành "
                        f"({sub_list}). Cần hoàn thành tất cả sub-task trước khi "
                        "hoàn thành."
                    ),
                    changes=[],
                    next_actions=[],
                )

        matched, avail = self._find_transition(key, "finish")
        if not matched:
            return self._unavailable_result(key, "finish", avail)

        changes = ["Chuyển trạng thái sang Done"]
        if time_spent:
            changes.append(f"Log work {time_spent}: {note}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ hoàn thành {key}",
                changes=changes,
                next_actions=[],
            )

        if time_spent:
            self.gateway.add_worklog(key, time_spent=time_spent, comment=note)

        err = self._do_transition(
            key, matched, comment=note if (note and not time_spent) else None
        )
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã hoàn thành {key}.",
            changes=changes,
            next_actions=[],
        )

    def block(
        self,
        key: str,
        reason: str,
        blocked_by: str,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        if not reason or not reason.strip():
            return ActionResult(
                ok=False,
                message="Lý do block không được để trống.",
                changes=[],
                next_actions=[],
            )
        if not blocked_by or blocked_by not in self.cfg.blocked_by_values:
            allowed_str = ", ".join(f"'{v}'" for v in self.cfg.blocked_by_values)
            return ActionResult(
                ok=False,
                message=(
                    f"blocked_by bắt buộc là một trong các giá trị: {allowed_str}."
                ),
                changes=[],
                next_actions=[],
            )

        self._get_issue(key)
        matched, avail = self._find_transition(key, "block")
        if not matched:
            return self._unavailable_result(key, "block", avail)

        changes = [
            "Chuyển trạng thái Block",
            f"Set Blocked By = {blocked_by}",
            f"Set Block Reason = {reason}",
        ]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ block {key}",
                changes=changes,
                next_actions=["unblock"],
            )

        payload = issue_fields_for_write(
            self.cfg,
            blocked_by=blocked_by,
            block_reason=reason,
        )
        err = self._do_transition(key, matched, fields=payload)
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã block {key}.",
            changes=changes,
            next_actions=["unblock"],
        )

    def unblock(
        self,
        key: str,
        note: str = "",
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        self._get_issue(key)
        matched, avail = self._find_transition(key, "unblock")
        if not matched:
            return self._unavailable_result(key, "unblock", avail)

        changes = ["Gỡ Block và xóa cờ Impediment"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ gỡ block {key}",
                changes=changes,
                next_actions=[],
            )

        err = self._do_transition(key, matched, comment=note if note else None)
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã gỡ block cho {key}.",
            changes=changes,
            next_actions=[],
        )

    def request_reschedule(
        self,
        key: str,
        new_end: date | None = None,
        reason: str = "",
        interrupted_by: str | None = None,
        *,
        proposed_end: date | None = None,
        no_impact: bool = False,
        note: str = "",
        dry_run: bool = False,
    ) -> ActionResult:
        if no_impact:
            return self.confirm_no_impact(key, note=note or reason, dry_run=dry_run)

        actual_end = proposed_end or new_end
        if actual_end is None:
            return ActionResult(
                ok=False,
                message="Ngày đề xuất (proposed_end/new_end) không được để trống.",
                changes=[],
                next_actions=[],
            )
        if isinstance(actual_end, str):
            try:
                actual_end = date.fromisoformat(actual_end)
            except ValueError:
                return ActionResult(
                    ok=False,
                    message=(
                        f"Định dạng ngày không hợp lệ: {actual_end}. "
                        "Yêu cầu YYYY-MM-DD."
                    ),
                    changes=[],
                    next_actions=[],
                )

        if not reason or not reason.strip():
            return ActionResult(
                ok=False,
                message="Lý do dời lịch không được để trống.",
                changes=[],
                next_actions=[],
            )

        issue = self._get_issue(key)
        if not issue.baseline_end:
            return ActionResult(
                ok=False,
                message="Chưa có Baseline End, không thể yêu cầu dời lịch.",
                changes=[],
                next_actions=[],
            )
        if issue.is_resolved:
            return ActionResult(
                ok=False,
                message="Issue đã hoàn thành (resolved), không thể yêu cầu dời lịch.",
                changes=[],
                next_actions=[],
            )
        if actual_end < self._today():
            return ActionResult(
                ok=False,
                message=f"Không thể dời lịch về ngày trong quá khứ ({actual_end}).",
                changes=[],
                next_actions=[],
            )

        is_interrupt = (
            reason.strip().lower()
            == self.cfg.reschedule_interrupt_value.strip().lower()
        )
        if is_interrupt and not interrupted_by:
            val = self.cfg.reschedule_interrupt_value
            return ActionResult(
                ok=False,
                message=f"Lý do '{val}' bắt buộc phải có interrupted_by.",
                changes=[],
                next_actions=[],
            )

        if interrupted_by:
            try:
                self._get_issue(interrupted_by)
            except (JiraError, KeyError, ValueError) as e:
                return ActionResult(
                    ok=False,
                    message=(
                        f"Issue chen ngang {interrupted_by} không tồn tại hoặc "
                        f"không thể truy cập: {e}"
                    ),
                    changes=[],
                    next_actions=[],
                )

        matched, avail = self._find_transition(key, "request_reschedule")
        if not matched:
            return self._unavailable_result(key, "request_reschedule", avail)

        changes = [
            f"Yêu cầu dời lịch đến {actual_end}",
            f"Lý do: {reason}",
        ]
        if interrupted_by:
            changes.append(f"Gắn link bị chen ngang bởi: {interrupted_by}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ yêu cầu dời lịch cho {key}",
                changes=changes,
                next_actions=["decide_reschedule"],
            )

        if interrupted_by:
            self.gateway.link(
                self.cfg.link_types["interrupts"],
                inward_key=key,
                outward_key=interrupted_by,
            )

        payload = issue_fields_for_write(
            self.cfg,
            proposed_end=actual_end,
            reschedule_reason=reason,
        )
        err = self._do_transition(
            key, matched, fields=payload, comment=note if note else None
        )
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã gửi yêu cầu dời lịch cho {key}.",
            changes=changes,
            next_actions=["decide_reschedule"],
        )

    def confirm_no_impact(
        self,
        key: str,
        note: str,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        if not note or not note.strip():
            return ActionResult(
                ok=False,
                message=(
                    "Ghi chú xác nhận không ảnh hưởng tiến độ (comment) "
                    "là bắt buộc và không được để trống."
                ),
                changes=[],
                next_actions=[],
            )

        issue = self._get_issue(key)
        current_user = self.gateway.current_user()
        if not issue.assignee or issue.assignee.lower() != current_user.lower():
            return ActionResult(
                ok=False,
                message=(
                    f"Chỉ người được giao việc ({issue.assignee}) "
                    "mới có thể xác nhận không ảnh hưởng."
                ),
                changes=[],
                next_actions=[],
            )
        if "NEED_RESCHEDULE" not in issue.health_flags:
            return ActionResult(
                ok=False,
                message=f"Issue {key} không có cờ cảnh báo NEED_RESCHEDULE.",
                changes=[],
                next_actions=[],
            )
        matched, avail = self._find_transition(key, "confirm_no_impact")
        if not matched:
            return self._unavailable_result(key, "confirm_no_impact", avail)

        changes = ["Xác nhận không ảnh hưởng tiến độ (Confirm No Impact)"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ xác nhận không ảnh hưởng tiến độ cho {key}",
                changes=changes,
                next_actions=[],
            )

        err = self._do_transition(key, matched, comment=note)
        if err:
            return err
        return ActionResult(
            ok=True,
            message=f"Đã xác nhận không ảnh hưởng tiến độ cho {key}.",
            changes=changes,
            next_actions=[],
        )

    def end_day(
        self,
        entries: list[EndDayEntry],
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        # Pre-validate all entries first before executing any writes
        sub_results: list[tuple[str, ActionResult]] = []
        for e in entries:
            if not e.time_spent or not e.time_spent.strip():
                sub_results.append(
                    (
                        e.key,
                        ActionResult(
                            ok=False,
                            message="time_spent không được để trống.",
                            changes=[],
                            next_actions=[],
                        ),
                    )
                )
                continue
            if e.move_to == "ready_for_qc":
                sub_res = self.submit_qc(
                    e.key, time_spent=e.time_spent, note=e.note, dry_run=True
                )
            elif e.move_to == "done":
                sub_res = self.finish(
                    e.key, time_spent=e.time_spent, note=e.note, dry_run=True
                )
            elif e.move_to is None:
                sub_res = self.log_work(
                    e.key, time_spent=e.time_spent, note=e.note, dry_run=True
                )
            else:
                sub_res = ActionResult(
                    ok=False,
                    message=f"move_to không hợp lệ: {e.move_to}",
                    changes=[],
                    next_actions=[],
                )
            sub_results.append((e.key, sub_res))

        missing_qc_types: dict[str, str] = {}
        fatal_failures: list[str] = []

        for k, r in sub_results:
            if not r.ok:
                if r.code == "missing_qc_owner":
                    issue_type = "bug" if "Bug stand-alone" in r.message else "adhoc"
                    missing_qc_types[k] = issue_type
                else:
                    fatal_failures.append(f"{k}: {r.message}")

        if fatal_failures:
            return ActionResult(
                ok=False,
                message="Một số công việc không hợp lệ: " + "; ".join(fatal_failures),
                changes=[],
                next_actions=[],
            )

        def _missing_qc_message(k: str, itype: str) -> str:
            if itype == "bug":
                return (
                    f"Chưa log time cho {k}: Bug stand-alone cần có QC Owner "
                    "để chuyển sang QC TO DO. Ai sẽ kiểm thử task này? "
                    "Vui lòng cung cấp qc_owner."
                )
            return (
                f"Chưa log time cho {k}: Ad-hoc Task cần có QC Owner để chuyển sang "
                "QC TO DO. Ai sẽ kiểm thử task này? Vui lòng cung cấp qc_owner "
                "hoặc dùng finish() nếu không cần QC."
            )

        if dry_run:
            if missing_qc_types:
                dry_results = []
                for _e, (k, r) in zip(entries, sub_results, strict=True):
                    if k in missing_qc_types:
                        dry_results.append(
                            (k, False, _missing_qc_message(k, missing_qc_types[k]))
                        )
                    else:
                        dry_results.append((k, True, r.message))
                per_key_lines = [
                    f"- {k}: {'Thành công' if ok else 'Thất bại'} - {msg}"
                    for k, ok, msg in dry_results
                ]
                return ActionResult(
                    ok=False,
                    message=(
                        "Một số công việc ghi nhận thất bại:\n"
                        + "\n".join(per_key_lines)
                    ),
                    changes=[],
                    next_actions=[],
                )
            changes: list[str] = []
            for e in entries:
                changes.append(f"Log {e.time_spent} trên {e.key}: {e.note}")
                if e.move_to:
                    changes.append(f"Chuyển {e.key} sang {e.move_to}")
            return ActionResult(
                ok=True,
                message="[Dry-run] Sẽ ghi nhận công việc cuối ngày",
                changes=changes,
                next_actions=[],
            )

        write_results: list[tuple[str, bool, str]] = []
        write_changes: list[str] = []
        any_failed = False
        for e in entries:
            if e.key in missing_qc_types:
                any_failed = True
                write_results.append(
                    (e.key, False, _missing_qc_message(e.key, missing_qc_types[e.key]))
                )
                continue
            try:
                if e.move_to == "ready_for_qc":
                    sub_res = self.submit_qc(
                        e.key, time_spent=e.time_spent, note=e.note, dry_run=False
                    )
                elif e.move_to == "done":
                    sub_res = self.finish(
                        e.key, time_spent=e.time_spent, note=e.note, dry_run=False
                    )
                else:
                    sub_res = self.log_work(
                        e.key, time_spent=e.time_spent, note=e.note, dry_run=False
                    )
                write_results.append((e.key, sub_res.ok, sub_res.message))
                if sub_res.ok:
                    write_changes.extend(sub_res.changes)
                else:
                    any_failed = True
            except (JiraError, KeyError, ValueError) as exc:
                any_failed = True
                write_results.append((e.key, False, str(exc)))

        per_key_lines = [
            f"- {k}: {'Thành công' if ok else 'Thất bại'} - {msg}"
            for k, ok, msg in write_results
        ]
        if any_failed:
            err_msg = "Một số công việc ghi nhận thất bại:\n" + "\n".join(per_key_lines)
            return ActionResult(
                ok=False,
                message=err_msg,
                changes=write_changes,
                next_actions=[],
            )

        ok_msg = "Đã ghi nhận công việc cuối ngày thành công:\n" + "\n".join(
            per_key_lines
        )
        return ActionResult(
            ok=True,
            message=ok_msg,
            changes=write_changes if write_changes else changes,
            next_actions=[],
        )

    # -------------------------------------------------------------------------
    # QC
    # -------------------------------------------------------------------------

    def qc_start(self, key: str, *, dry_run: bool = False) -> ActionResult:
        self._get_issue(key)
        matched, avail = self._find_transition(key, "start_test")
        if not matched:
            return self._unavailable_result(key, "start_test", avail)

        changes = ["Chuyển trạng thái sang Test In Progress"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ bắt đầu kiểm thử {key}",
                changes=changes,
                next_actions=["qc_pass", "qc_fail"],
            )

        err = self._do_transition(key, matched)
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã bắt đầu kiểm thử {key}.",
            changes=changes,
            next_actions=["qc_pass", "qc_fail"],
        )

    def qc_pass(
        self,
        key: str,
        time_spent: str | None = None,
        note: str = "",
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        if not note or not note.strip():
            return ActionResult(
                ok=False,
                message="Comment (ghi chú) bắt buộc cho QC Pass.",
                changes=[],
                next_actions=[],
            )

        issue = self._get_issue(key)
        if issue.type == "story" and not time_spent:
            caller = self.gateway.current_user()
            raw = self.gateway.get_issue(key)
            worklog_field = raw.get("fields", {}).get("worklog", {})
            raw_worklogs = worklog_field.get("worklogs", [])
            total_worklogs = worklog_field.get("total", len(raw_worklogs))
            if total_worklogs > len(raw_worklogs):
                raw_worklogs = self.gateway.get_worklogs(key)

            has_worklog = any(
                wl.get("author", {}).get("name", "").lower() == caller.lower()
                for wl in raw_worklogs
            )
            if not has_worklog:
                return ActionResult(
                    ok=False,
                    message=(
                        f"QC Pass cho Story ({key}) yêu cầu người thực hiện ({caller}) "
                        "phải có worklog trên Story. Vui lòng cung cấp time_spent."
                    ),
                    changes=[],
                    next_actions=[],
                )

        if issue.type in ("adhoc", "bug"):
            open_subtasks = self._get_open_subtasks(key)
            if open_subtasks:
                sub_list = ", ".join(open_subtasks)
                type_name = (
                    "Ad-hoc Task" if issue.type == "adhoc" else "Bug stand-alone"
                )
                return ActionResult(
                    ok=False,
                    message=(
                        f"{type_name} {key} vẫn còn sub-task chưa hoàn thành "
                        f"({sub_list}). Cần hoàn thành tất cả sub-task trước khi "
                        "QC Pass."
                    ),
                    changes=[],
                    next_actions=[],
                )

        matched, avail = self._find_transition(key, "qc_pass")
        if not matched:
            return self._unavailable_result(key, "qc_pass", avail)

        changes = ["Xác nhận QC Pass"]
        if time_spent:
            changes.append(f"Log work {time_spent}: {note}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ xác nhận QC Pass cho {key}",
                changes=changes,
                next_actions=[],
            )

        if time_spent:
            self.gateway.add_worklog(key, time_spent, comment=note)

        err = self._do_transition(key, matched, comment=note)
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã xác nhận QC Pass cho {key}.",
            changes=changes,
            next_actions=[],
        )

    def qc_fail(
        self,
        key: str,
        time_spent: str | None = None,
        reason: str = "",
        bugs: list[str] | None = None,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        if not reason or not reason.strip():
            return ActionResult(
                ok=False,
                message="Lý do QC Fail (comment) bắt buộc và không được để trống.",
                changes=[],
                next_actions=[],
            )

        issue = self._get_issue(key)
        actual_bugs = bugs or []
        if issue.type == "story":
            if not actual_bugs and not self._has_open_bug_subtask(issue):
                return ActionResult(
                    ok=False,
                    message=(
                        f"QC Fail cho Story ({key}) bắt buộc phải có ít nhất 1 "
                        "Bug sub-task đang mở (hãy truyền bugs hoặc tạo Bug "
                        "sub-task trước)."
                    ),
                    changes=[],
                    next_actions=[],
                )
            target_story_key = issue.key
            code = issue.actor_code or "FLOW"
        elif issue.type == "adhoc":
            target_story_key = issue.key
            code = issue.actor_code or "BUG"
        elif issue.type == "bug":
            if actual_bugs:
                return ActionResult(
                    ok=False,
                    message=(
                        "Bug stand-alone không có Bug con: chưa đạt thì QC Fail "
                        "để trả về Dev; lỗi khác thì tạo Bug stand-alone mới "
                        "và link 'relates to'."
                    ),
                    changes=[],
                    next_actions=[],
                )
            target_story_key = issue.key
            code = issue.actor_code or "BUG"
        else:
            if not issue.parent_key:
                return ActionResult(
                    ok=False,
                    message=(
                        f"Không thể QC Fail: phần việc {key} không thuộc một Story "
                        "hoặc Ad-hoc Task."
                    ),
                    changes=[],
                    next_actions=[],
                )
            parent = self._get_issue(issue.parent_key)
            if parent.type == "bug":
                if actual_bugs:
                    return ActionResult(
                        ok=False,
                        message=(
                            "Bug stand-alone không có Bug con: chưa đạt thì QC Fail "
                            "để trả về Dev; lỗi khác thì tạo Bug stand-alone mới "
                            "và link 'relates to'."
                        ),
                        changes=[],
                        next_actions=[],
                    )
                return ActionResult(
                    ok=False,
                    message=(
                        f"Không thể QC Fail: phần việc {key} thuộc Bug stand-alone "
                        "không qua QC riêng."
                    ),
                    changes=[],
                    next_actions=[],
                )
            if parent.type == "adhoc":
                if issue.type != "bug_sub":
                    return ActionResult(
                        ok=False,
                        message=(
                            f"Không thể QC Fail: phần việc {key} không thuộc Story "
                            "(thuộc Ad-hoc Task nhưng chỉ Bug sub-task mới qua "
                            "QC riêng)."
                        ),
                        changes=[],
                        next_actions=[],
                    )
            elif parent.type != "story":
                return ActionResult(
                    ok=False,
                    message=(
                        f"Không thể QC Fail: phần việc {key} không thuộc một Story."
                    ),
                    changes=[],
                    next_actions=[],
                )
            target_story_key = issue.parent_key
            code = parent.actor_code or "BUG"

        matched, avail = self._find_transition(key, "qc_fail")
        if not matched:
            return self._unavailable_result(key, "qc_fail", avail)

        changes = [f"Chuyển trạng thái QC Fail: {reason}"]
        for b in actual_bugs:
            changes.append(f"Tạo Bug sub-task: {b}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ báo QC Fail cho {key}",
                changes=changes,
                next_actions=[],
            )

        bug_type_name = self.cfg.issue_types["bug_sub"][0]
        created_bugs: list[str] = []
        for b in actual_bugs:
            summary = f"[{code}][BUG] {b}"
            create_payload = {
                "project": {"key": issue.project},
                "issuetype": {"name": bug_type_name},
                "summary": summary,
                "parent": {"key": target_story_key},
            }
            new_key = self.gateway.create_issue(create_payload)
            created_bugs.append(new_key)

        if time_spent:
            self.gateway.add_worklog(key, time_spent, comment=reason)

        try:
            err = self._do_transition(key, matched, comment=reason)
        except JiraError as exc:
            # Intentionally catch all JiraErrors (e.g. non-400) so transition errors
            # return ok=False on every path instead of raising.
            err = ActionResult(
                ok=False,
                message=str(exc) or "Lỗi từ Jira",
                changes=[],
                next_actions=[],
            )

        if err:
            notes: list[str] = []
            if time_spent:
                notes.append(f"Đã log {time_spent} trên {key}.")
            if created_bugs:
                bug_list = ", ".join(created_bugs)
                notes.append(
                    f"Các Bug sub-task sau đã được tạo: {bug_list}. "
                    "Bạn có thể huỷ hoặc tái sử dụng chúng."
                )
            if notes:
                note_str = " ".join(notes)
                return ActionResult(
                    ok=False,
                    message=f"{err.message}\nLưu ý: {note_str}",
                    changes=err.changes,
                    next_actions=err.next_actions,
                )
            return err

        return ActionResult(
            ok=True,
            message=f"Đã báo QC Fail cho {key}.",
            changes=changes,
            next_actions=[],
        )

    # -------------------------------------------------------------------------
    # Flow & UAT
    # -------------------------------------------------------------------------

    def flow_reschedule(
        self,
        story_key: str | None = None,
        new_due: date | None = None,
        reason: str = "",
        note: str = "",
        *,
        key: str | None = None,
        proposed_due: date | None = None,
        dry_run: bool = False,
    ) -> ActionResult:
        actual_key = key or story_key
        actual_due = proposed_due or new_due
        if not actual_key:
            return ActionResult(
                ok=False,
                message="Key story không được để trống.",
                changes=[],
                next_actions=[],
            )
        if actual_due is None:
            return ActionResult(
                ok=False,
                message="Hạn mới (proposed_due/new_due) không được để trống.",
                changes=[],
                next_actions=[],
            )

        if not reason or not reason.strip():
            return ActionResult(
                ok=False,
                message="Lý do dời hạn luồng không được để trống.",
                changes=[],
                next_actions=[],
            )

        issue = self._get_issue(actual_key)
        if issue.type != "story":
            return ActionResult(
                ok=False,
                message=f"Issue {actual_key} không phải là Story.",
                changes=[],
                next_actions=[],
            )
        if actual_due < self._today():
            return ActionResult(
                ok=False,
                message=f"Không thể dời Due Date về ngày trong quá khứ ({actual_due}).",
                changes=[],
                next_actions=[],
            )

        if issue.status in ("open", "analyzing"):
            matched, avail = self._find_transition(actual_key, "set_due")
            if not matched:
                return self._unavailable_result(actual_key, "set_due", avail)

            changes = [
                f"Đặt Due Date = {actual_due}",
                f"Lý do: {reason}",
            ]
            if dry_run:
                return ActionResult(
                    ok=True,
                    message=f"[Dry-run] Sẽ đặt Due Date cho {actual_key}",
                    changes=changes,
                    next_actions=[],
                )

            payload = issue_fields_for_write(self.cfg, due_date=actual_due)
            comment_text = f"Đặt Due Date: {actual_due}. Lý do: {reason}"
            if note:
                comment_text += f". Ghi chú: {note}"
            err = self._do_transition(
                actual_key, matched, fields=payload, comment=comment_text
            )
            if err:
                return err

            return ActionResult(
                ok=True,
                message=f"Đã đặt Due Date thành công cho {actual_key}.",
                changes=changes,
                next_actions=[],
            )

        matched, avail = self._find_transition(actual_key, "request_reschedule")
        if not matched:
            return self._unavailable_result(actual_key, "request_reschedule", avail)

        changes = [
            f"Yêu cầu dời Due Date sang {actual_due}",
            f"Lý do: {reason}",
        ]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ yêu cầu dời Due Date cho {actual_key}",
                changes=changes,
                next_actions=["decide_reschedule"],
            )

        payload = issue_fields_for_write(
            self.cfg,
            proposed_end=actual_due,
            reschedule_reason=reason,
        )
        err = self._do_transition(
            actual_key, matched, fields=payload, comment=note if note else None
        )
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã gửi yêu cầu dời Due Date cho {actual_key}.",
            changes=changes,
            next_actions=["decide_reschedule"],
        )

    def uat(
        self,
        epic_key: str,
        actor: str,
        action: Literal["start", "signoff", "fail"],
        note: str,
        story_keys: list[str] | None = None,
        bugs: dict[str, list[str]] | None = None,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        if action in ("signoff", "fail") and (not note or not note.strip()):
            return ActionResult(
                ok=False,
                message=f"Comment (ghi chú) bắt buộc cho UAT {action}.",
                changes=[],
                next_actions=[],
            )

        actor_code = actor.split(" - ")[0].strip()
        proj_key = epic_key.split("-")[0]
        story_types = ", ".join(f'"{t}"' for t in self.cfg.issue_types["story"])
        jql = (
            f'project = "{proj_key}" AND issuetype in ({story_types}) '
            f'AND ("Epic Link" = "{epic_key}" OR parent = "{epic_key}")'
        )
        raw_issues = self.gateway.search(jql, fields=["*all"])

        actor_stories: list[Issue] = []
        for raw in raw_issues:
            iss = issue_from_json(raw, self.cfg)
            if iss.type == "story" and (
                iss.epic_key == epic_key or iss.parent_key == epic_key
            ):
                if iss.actor_code == actor_code or actor_code in [
                    c.split(" - ")[0].strip() for c in iss.components
                ]:
                    actor_stories.append(iss)

        if not actor_stories:
            return ActionResult(
                ok=False,
                message=(
                    f"Không tìm thấy Story nào của actor '{actor}' "
                    f"trong epic {epic_key}."
                ),
                changes=[],
                next_actions=[],
            )

        target_stories: list[Issue] = []
        if action == "start":
            required_status = "ready_for_uat"
            trans_logical = "client_uat"
            if story_keys:
                for k in story_keys:
                    matched_s = next((s for s in actor_stories if s.key == k), None)
                    if not matched_s:
                        return ActionResult(
                            ok=False,
                            message=(
                                f"Story {k} không thuộc actor '{actor}' "
                                f"trong epic {epic_key}."
                            ),
                            changes=[],
                            next_actions=[],
                        )
                    if matched_s.status != required_status:
                        return ActionResult(
                            ok=False,
                            message=(
                                f"Story {k} không ở trạng thái Ready For UAT "
                                f"(hiện tại: {matched_s.status})."
                            ),
                            changes=[],
                            next_actions=[],
                        )
                    target_stories.append(matched_s)
            else:
                target_stories = [
                    s for s in actor_stories if s.status == required_status
                ]

        elif action == "signoff":
            required_status = "client_uat"
            trans_logical = "uat_signoff"
            if story_keys:
                for k in story_keys:
                    matched_s = next((s for s in actor_stories if s.key == k), None)
                    if not matched_s:
                        return ActionResult(
                            ok=False,
                            message=(
                                f"Story {k} không thuộc actor '{actor}' "
                                f"trong epic {epic_key}."
                            ),
                            changes=[],
                            next_actions=[],
                        )
                    if matched_s.status != required_status:
                        return ActionResult(
                            ok=False,
                            message=(
                                f"Story {k} không ở trạng thái Đang UAT "
                                f"(Wait For Review) (hiện tại: {matched_s.status})."
                            ),
                            changes=[],
                            next_actions=[],
                        )
                    target_stories.append(matched_s)
            else:
                not_in_uat = [
                    s
                    for s in actor_stories
                    if s.status != required_status and not s.is_resolved
                ]
                if not_in_uat:
                    keys_str = ", ".join(s.key for s in not_in_uat)
                    return ActionResult(
                        ok=False,
                        message=(
                            "Không thể ký UAT: còn Story của actor chưa ở "
                            f"trạng thái Đang UAT (Wait For Review): {keys_str}."
                        ),
                        changes=[],
                        next_actions=[],
                    )
                target_stories = [
                    s for s in actor_stories if s.status == required_status
                ]

        elif action == "fail":
            if not story_keys:
                return ActionResult(
                    ok=False,
                    message=(
                        "Hành động UAT Fail bắt buộc phải cung cấp "
                        "danh sách story_keys."
                    ),
                    changes=[],
                    next_actions=[],
                )
            required_status = "client_uat"
            trans_logical = "uat_fail"
            for k in story_keys:
                matched_s = next((s for s in actor_stories if s.key == k), None)
                if not matched_s:
                    return ActionResult(
                        ok=False,
                        message=(
                            f"Story {k} không thuộc actor '{actor}' "
                            f"trong epic {epic_key}."
                        ),
                        changes=[],
                        next_actions=[],
                    )
                if matched_s.status != required_status:
                    return ActionResult(
                        ok=False,
                        message=(
                            f"Story {k} không ở trạng thái Đang UAT "
                            f"(Wait For Review) (hiện tại: {matched_s.status})."
                        ),
                        changes=[],
                        next_actions=[],
                    )
                s_bugs = (bugs or {}).get(k, [])
                if not s_bugs and not self._has_open_bug_subtask(matched_s):
                    return ActionResult(
                        ok=False,
                        message=(
                            f"Story {k} chưa có bug nào được khai báo và không "
                            "có Bug sub-task mở cho UAT Fail."
                        ),
                        changes=[],
                        next_actions=[],
                    )
                target_stories.append(matched_s)
        else:
            return ActionResult(
                ok=False,
                message=f"Hành động UAT không hợp lệ: {action}",
                changes=[],
                next_actions=[],
            )

        if not target_stories:
            return ActionResult(
                ok=False,
                message=(
                    f"Không có Story nào của actor '{actor}' phù hợp để "
                    f"thực hiện UAT {action}."
                ),
                changes=[],
                next_actions=[],
            )

        trans_name = self.cfg.transitions[trans_logical]
        for s in target_stories:
            matched_trans, avail = self._find_transition(s.key, trans_logical)
            if not matched_trans:
                avail_str = ", ".join(avail)
                return ActionResult(
                    ok=False,
                    message=(
                        f"Transition '{trans_name}' không khả dụng cho {s.key}. "
                        f"Các transition hiện có: {avail_str}."
                    ),
                    changes=[],
                    next_actions=[],
                )

        changes = [f"UAT {action} cho {s.key}" for s in target_stories]
        if dry_run:
            return ActionResult(
                ok=True,
                message=(
                    f"[Dry-run] Sẽ xử lý UAT {action} cho {len(target_stories)} Story"
                ),
                changes=changes,
                next_actions=[],
            )

        bug_type_name = self.cfg.issue_types["bug_sub"][0]
        for s in target_stories:
            if action == "fail":
                for b in (bugs or {}).get(s.key, []):
                    code = s.actor_code or "FLOW"
                    self.gateway.create_issue(
                        {
                            "project": {"key": s.project},
                            "issuetype": {"name": bug_type_name},
                            "summary": f"[{code}][BUG] {b}",
                            "parent": {"key": s.key},
                        }
                    )
            err = self._do_transition(s.key, trans_name, comment=note if note else None)
            if err:
                return err

        story_keys_str = ", ".join(s.key for s in target_stories)
        return ActionResult(
            ok=True,
            message=(
                f"Đã xử lý UAT {action} thành công cho {len(target_stories)} "
                f"Story ({story_keys_str})."
            ),
            changes=changes,
            next_actions=[],
        )

    # -------------------------------------------------------------------------
    # Lead-BA
    # -------------------------------------------------------------------------

    def decide_reschedule(
        self,
        key: str,
        *,
        approve: bool,
        note: str,
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        if issue.reschedule_status != self.cfg.reschedule_pending_value:
            return ActionResult(
                ok=False,
                message=(
                    f"Issue {key} không ở trạng thái chờ duyệt dời lịch "
                    f"(reschedule_status = {issue.reschedule_status}, "
                    f"kỳ vọng: {self.cfg.reschedule_pending_value})."
                ),
                changes=[],
                next_actions=[],
            )

        current_user = self.gateway.current_user()
        if (
            approve
            and issue.reschedule_requester
            and current_user.lower() == issue.reschedule_requester.lower()
        ):
            return ActionResult(
                ok=False,
                message=(
                    "Người yêu cầu không được tự duyệt yêu cầu dời lịch của chính mình."
                ),
                changes=[],
                next_actions=[],
            )

        if not approve and (not note or not note.strip()):
            return ActionResult(
                ok=False,
                message="Ghi chú (comment) bắt buộc khi từ chối yêu cầu dời lịch.",
                changes=[],
                next_actions=[],
            )

        trans_logical = "approve_reschedule" if approve else "reject_reschedule"
        matched, avail = self._find_transition(key, trans_logical)
        if not matched:
            return self._unavailable_result(key, trans_logical, avail)

        if approve:
            changes = [
                f"Phê duyệt dời lịch cho {key}",
                f"Cập nhật hạn mới = {issue.proposed_end}",
            ]
        else:
            changes = [f"Từ chối dời lịch cho {key}"]

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ quyết định dời lịch cho {key}",
                changes=changes,
                next_actions=[],
            )

        if approve:
            err = self._do_transition(
                key,
                matched,
                comment=f"Phê duyệt dời lịch: {note}" if note else None,
            )
            if err:
                return err
        else:
            err = self._do_transition(key, matched, comment=f"Từ chối dời lịch: {note}")
            if err:
                return err

        return ActionResult(
            ok=True,
            message=f"Đã xử lý quyết định dời lịch cho {key}.",
            changes=changes,
            next_actions=[],
        )

    def cancel(
        self,
        key: str,
        reason: str,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        if not reason or not reason.strip():
            return ActionResult(
                ok=False,
                message="Lý do hủy (comment) là bắt buộc và không được để trống.",
                changes=[],
                next_actions=[],
            )

        self._get_issue(key)
        matched, avail = self._find_transition(key, "cancel")
        if not matched:
            return self._unavailable_result(key, "cancel", avail)

        changes = [f"Hủy issue {key}", f"Lý do: {reason}"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ hủy {key}",
                changes=changes,
                next_actions=[],
            )

        err = self._do_transition(key, matched, comment=f"Hủy: {reason}")
        if err:
            return err
        return ActionResult(
            ok=True,
            message=f"Đã hủy {key}.",
            changes=changes,
            next_actions=[],
        )

    # -------------------------------------------------------------------------
    # Triage
    # -------------------------------------------------------------------------

    def triage_open(
        self,
        project: str,
        text: str,
        source_url: str,
        reported_by: str,
        reported_at: datetime | None = None,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        expected_summary = self.cfg.ops_epic_summary.format(
            month=self._today().month, year=self._today().year
        )
        epic_types = ", ".join(f'"{t}"' for t in self.cfg.issue_types["epic"])
        jql = f'project = "{project}" AND issuetype in ({epic_types})'
        raw_issues = self.gateway.search(jql, fields=["*all"])

        ops_epic: Issue | None = None
        for raw in raw_issues:
            iss = issue_from_json(raw, self.cfg)
            if iss.type == "epic" and iss.summary == expected_summary:
                ops_epic = iss
                break

        if not ops_epic:
            return ActionResult(
                ok=False,
                message=(
                    f"Không tìm thấy Epic [OPS] tháng hiện tại cho dự án {project} "
                    f"({expected_summary}). "
                    "Vui lòng chạy eggs-guardian hoặc tạo Epic [OPS] trước."
                ),
                changes=[],
                next_actions=[],
            )

        proj_cfg = self.cfg.projects.get(project)
        if not proj_cfg:
            return ActionResult(
                ok=False,
                message=f"Dự án {project} chưa được khai báo cấu hình.",
                changes=[],
                next_actions=[],
            )
        triage_owner = proj_cfg.triage_owner

        changes = [
            f"Tạo User Report trong Epic {ops_epic.key}",
            f"Gán Assignee = {triage_owner}",
            "Chuyển trạng thái Start Triage",
        ]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ tạo User Report trong Epic {ops_epic.key}",
                changes=changes,
                next_actions=["triage_close"],
            )

        report_type_name = self.cfg.issue_types["report"][0]
        create_fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": report_type_name},
            "summary": text,
            "assignee": {"name": triage_owner},
        }
        if self.cfg.fields.get("epic_link"):
            create_fields[self.cfg.fields["epic_link"]] = ops_epic.key
        if self.cfg.fields.get("report_source"):
            create_fields[self.cfg.fields["report_source"]] = source_url
        if self.cfg.fields.get("reported_by"):
            create_fields[self.cfg.fields["reported_by"]] = reported_by
        if self.cfg.fields.get("reported_at"):
            rep_dt = reported_at or datetime.now(ZoneInfo(self.cfg.timezone))
            dt_payload = issue_fields_for_write(self.cfg, reported_at=rep_dt)
            create_fields[self.cfg.fields["reported_at"]] = dt_payload[
                self.cfg.fields["reported_at"]
            ]

        new_key = self.gateway.create_issue(create_fields)

        matched, avail = self._find_transition(new_key, "start_triage")
        if not matched:
            avail_str = ", ".join(avail) if avail else "(không có)"
            warn_msg = (
                f"Đã tạo phản ánh {new_key}, nhưng transition 'Start Triage' "
                f"không khả dụng (các transition: {avail_str}). "
                "Phản ánh vẫn đang ở trạng thái Open và được theo dõi."
            )
            return ActionResult(
                ok=True,
                message=warn_msg,
                changes=[
                    f"Tạo User Report trong Epic {ops_epic.key}",
                    f"Gán Assignee = {triage_owner}",
                ],
                next_actions=["triage_close"],
            )

        err = self._do_transition(new_key, matched)
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã tạo phản ánh {new_key} và bắt đầu triage.",
            changes=changes,
            next_actions=["triage_close"],
        )

    def triage_close(
        self,
        key: str,
        verdict: Literal[
            "bug",
            "guidance",
            "feature",
            "data_fix",
            "duplicate",
            "cannot_reproduce",
            "third_party",
        ],
        note: str,
        time_spent: str,
        bug_story_key: str | None = None,
        bug_summary: str | None = None,
        link_key: str | None = None,
        *,
        dry_run: bool = False,
    ) -> ActionResult:
        if not note or not note.strip():
            return ActionResult(
                ok=False,
                message="Ghi chú (note) bắt buộc và không được để trống.",
                changes=[],
                next_actions=[],
            )
        if not time_spent or not time_spent.strip():
            return ActionResult(
                ok=False,
                message=(
                    "Thời gian làm việc (time_spent) bắt buộc và không được để trống."
                ),
                changes=[],
                next_actions=[],
            )

        report = self._get_issue(key)
        if report.type != "report":
            return ActionResult(
                ok=False,
                message=f"Issue {key} không phải là User Report.",
                changes=[],
                next_actions=[],
            )
        if report.is_resolved:
            return ActionResult(
                ok=False,
                message=f"User Report {key} đã được xử lý (resolved).",
                changes=[],
                next_actions=[],
            )

        if verdict not in self.cfg.report_resolutions:
            return ActionResult(
                ok=False,
                message=(
                    f"Kết luận '{verdict}' không tồn tại trong cấu hình "
                    "report_resolutions."
                ),
                changes=[],
                next_actions=[],
            )

        resolution_name = self.cfg.report_resolutions[verdict]
        reply_suggestion = (
            f"Gợi ý trả lời group chat: 'Chào bạn, phản ánh đã được tiếp nhận "
            f"và xử lý ({resolution_name}). {note}. Cảm ơn bạn đã phản ánh!'"
        )

        ops_epic: Issue | None = None
        bug_story: Issue | None = None

        if verdict == "bug":
            if bug_story_key:
                try:
                    bug_story = self._get_issue(bug_story_key)
                except (JiraError, KeyError, ValueError) as e:
                    return ActionResult(
                        ok=False,
                        message=(
                            f"Issue {bug_story_key} không tồn tại hoặc "
                            f"không thể truy cập: {e}"
                        ),
                        changes=[],
                        next_actions=[],
                    )
                if bug_story.type == "bug":
                    return ActionResult(
                        ok=False,
                        message=(
                            "Bug stand-alone không có Bug con: chưa đạt thì QC Fail "
                            "để trả về Dev; lỗi khác thì tạo Bug stand-alone mới "
                            "và link 'relates to'."
                        ),
                        changes=[],
                        next_actions=[],
                    )
                if bug_story.type not in ("story", "adhoc"):
                    return ActionResult(
                        ok=False,
                        message=(
                            f"Issue {bug_story_key} không phải là Story hoặc "
                            "Ad-hoc Task."
                        ),
                        changes=[],
                        next_actions=[],
                    )
                if bug_story.is_resolved:
                    parent_label = (
                        "Story" if bug_story.type == "story" else "Ad-hoc Task"
                    )
                    return ActionResult(
                        ok=False,
                        message=(
                            f"Không thể tạo Bug sub-task dưới {parent_label} đã "
                            "hoàn thành (Done)."
                        ),
                        changes=[],
                        next_actions=[],
                    )
            else:
                expected_summary = self.cfg.ops_epic_summary.format(
                    month=self._today().month, year=self._today().year
                )
                epic_types = ", ".join(f'"{t}"' for t in self.cfg.issue_types["epic"])
                jql = f'project = "{report.project}" AND issuetype in ({epic_types})'
                raw_issues = self.gateway.search(jql, fields=["*all"])
                for raw in raw_issues:
                    iss = issue_from_json(raw, self.cfg)
                    if iss.type == "epic" and iss.summary == expected_summary:
                        ops_epic = iss
                        break
                if not ops_epic:
                    return ActionResult(
                        ok=False,
                        message=(
                            f"Không tìm thấy Epic [OPS] tháng hiện tại cho dự án "
                            f"{report.project} ({expected_summary})."
                        ),
                        changes=[],
                        next_actions=[],
                    )

        elif verdict in ("feature", "data_fix", "duplicate"):
            if not link_key:
                return ActionResult(
                    ok=False,
                    message=(
                        f"Kết luận '{verdict}' bắt buộc phải cung cấp "
                        "link_key để liên kết."
                    ),
                    changes=[],
                    next_actions=[],
                )
            try:
                target_iss = self._get_issue(link_key)
            except (JiraError, KeyError, ValueError) as e:
                return ActionResult(
                    ok=False,
                    message=(
                        f"Issue {link_key} không tồn tại hoặc không thể truy cập: {e}"
                    ),
                    changes=[],
                    next_actions=[],
                )
            if verdict == "feature" and target_iss.type != "story":
                return ActionResult(
                    ok=False,
                    message=(
                        "Link target cho Feature Request phải là Story "
                        f"(hiện tại: {target_iss.type})."
                    ),
                    changes=[],
                    next_actions=[],
                )
            elif verdict == "data_fix" and target_iss.type != "adhoc":
                return ActionResult(
                    ok=False,
                    message=(
                        "Link target cho Data/Config Fix phải là Ad-hoc Task "
                        f"(hiện tại: {target_iss.type})."
                    ),
                    changes=[],
                    next_actions=[],
                )
            elif verdict == "duplicate" and target_iss.type != "report":
                return ActionResult(
                    ok=False,
                    message=(
                        "Link target cho Duplicate phải là User Report "
                        f"(hiện tại: {target_iss.type})."
                    ),
                    changes=[],
                    next_actions=[],
                )

        matched, avail = self._find_transition(key, "close_report")
        if not matched:
            return self._unavailable_result(key, "close_report", avail)

        changes = [
            f"Log QC time: {time_spent}",
            f"Kết luận: {resolution_name}",
        ]
        if verdict == "bug":
            if bug_story_key:
                parent_label = (
                    "Story"
                    if bug_story and bug_story.type == "story"
                    else "Ad-hoc Task"
                )
                changes.append(
                    f"Tạo Bug sub-task dưới {parent_label} {bug_story_key} "
                    "và liên kết Causes"
                )
            else:
                changes.append(
                    "Tạo Bug stand-alone trong Epic [OPS] và liên kết Causes"
                )
                proj_cfg = self.cfg.projects.get(report.project)
                qc_owner_user = report.assignee or (
                    proj_cfg.triage_owner if proj_cfg else None
                )
                if self.cfg.fields.get("qc_owner"):
                    if qc_owner_user:
                        changes.append(f"QC Owner: {qc_owner_user}")
                    else:
                        changes.append(
                            "Không xác định được QC Owner: Dev sẽ chọn khi Submit QC"
                        )
                else:
                    changes.append("Không có cấu hình fields.qc_owner")
        elif link_key:
            changes.append(f"Liên kết tới {link_key}")
        changes.append("Đóng User Report (Close Report)")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ đóng User Report {key}. {reply_suggestion}",
                changes=changes,
                next_actions=[],
            )

        self.gateway.add_worklog(key, time_spent=time_spent, comment=note)

        if verdict == "bug":
            if bug_story_key and bug_story:
                default_code = "BUG" if bug_story.type == "adhoc" else "FLOW"
                actor_code = bug_story.actor_code or default_code
                summary = bug_summary or f"[{actor_code}][BUG] {note}"
                bug_type_name = self.cfg.issue_types["bug_sub"][0]
                bug_key = self.gateway.create_issue(
                    {
                        "project": {"key": bug_story.project},
                        "issuetype": {"name": bug_type_name},
                        "summary": summary,
                        "parent": {"key": bug_story_key},
                    }
                )
                self.gateway.link(
                    self.cfg.link_types["causes"], inward_key=key, outward_key=bug_key
                )
            elif ops_epic:
                bug_type_name = self.cfg.issue_types["bug"][0]
                summary = bug_summary or f"{self.cfg.hotfix_prefix} {note}"
                create_payload: dict[str, Any] = {
                    "project": {"key": report.project},
                    "issuetype": {"name": bug_type_name},
                    "summary": summary,
                }
                if self.cfg.fields.get("epic_link"):
                    create_payload[self.cfg.fields["epic_link"]] = ops_epic.key
                proj_cfg = self.cfg.projects.get(report.project)
                qc_owner_user = report.assignee or (
                    proj_cfg.triage_owner if proj_cfg else None
                )
                qc_field_id = self.cfg.fields.get("qc_owner")
                if qc_field_id and qc_owner_user:
                    create_payload[qc_field_id] = {"name": qc_owner_user}
                bug_key = self.gateway.create_issue(create_payload)
                self.gateway.link(
                    self.cfg.link_types["causes"], inward_key=key, outward_key=bug_key
                )

        elif verdict in ("feature", "data_fix"):
            if link_key:
                self.gateway.link(
                    self.cfg.link_types["relates"], inward_key=key, outward_key=link_key
                )
        elif verdict == "duplicate":
            if link_key:
                self.gateway.link(
                    self.cfg.link_types["duplicates"],
                    inward_key=key,
                    outward_key=link_key,
                )

        err = self._do_transition(
            key,
            matched,
            fields={"resolution": {"name": resolution_name}},
            comment=f"Kết luận triage: {resolution_name}. Ghi chú: {note}",
        )
        if err:
            return err

        return ActionResult(
            ok=True,
            message=f"Đã đóng phản ánh {key}. {reply_suggestion}",
            changes=changes,
            next_actions=[],
        )
