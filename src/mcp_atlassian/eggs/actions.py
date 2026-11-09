from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from .calendar import WorkCalendar
from .config import EggsConfig
from .mapping import CLEAR, issue_fields_for_write, issue_from_json
from .model import Issue


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    changes: list[str]
    next_actions: list[str]


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
        self.today_fn = today_fn or date.today

    def _today(self) -> date:
        return self.today_fn()

    def _get_issue(self, key: str) -> Issue:
        raw = self.gateway.get_issue(key)
        return issue_from_json(raw, self.cfg)

    # -------------------------------------------------------------------------
    # Work items
    # -------------------------------------------------------------------------

    def schedule(
        self,
        key: str,
        start: date,
        end: date,
        estimate: str,
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
        if end < start:
            return ActionResult(
                ok=False,
                message=f"Planned End ({end}) không được trước Planned Start ({start}).",
                changes=[],
                next_actions=[],
            )

        changes = [
            f"Set Planned Start = {start}",
            f"Set Planned End = {end}",
            f"Set Estimate = {estimate}",
        ]
        if not issue.baseline_end:
            changes.append(f"Set Baseline End = {end}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ lên lịch cho {key}",
                changes=changes,
                next_actions=["start"],
            )

        write_args = {"planned_start": start, "planned_end": end}
        if not issue.baseline_end:
            write_args["baseline_end"] = end

        payload = issue_fields_for_write(self.cfg, **write_args)
        self.gateway.update_fields(key, payload)

        trans_name = self.cfg.transitions.get("schedule", "Schedule")
        available = self.gateway.get_transitions(key)
        if any(t["name"].lower() == trans_name.lower() for t in available):
            self.gateway.transition(key, trans_name)

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

        has_schedule = (
            issue.planned_end is not None
            or end is not None
            or issue.baseline_end is not None
        )
        if not has_schedule:
            return ActionResult(
                ok=False,
                message=f"Không thể bắt đầu: issue {key} chưa có lịch (Planned End/Baseline End). Vui lòng lên lịch trước khi bắt đầu.",
                changes=[],
                next_actions=["schedule"],
            )

        changes = ["Chuyển trạng thái sang Dev In Progress"]
        effective_end = end or issue.planned_end
        if not issue.baseline_end and effective_end:
            changes.append(f"Set Baseline End = {effective_end}")
        if start or end or estimate:
            changes.append(f"Cập nhật lịch: start={start}, end={end}, estimate={estimate}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ bắt đầu thực hiện {key}",
                changes=changes,
                next_actions=["log_work", "submit_qc", "block"],
            )

        w_args = {}
        if start:
            w_args["planned_start"] = start
        if end:
            w_args["planned_end"] = end
        if not issue.baseline_end and effective_end:
            w_args["baseline_end"] = effective_end
        if w_args:
            payload = issue_fields_for_write(self.cfg, **w_args)
            self.gateway.update_fields(key, payload)

        trans_name = self.cfg.transitions.get("start", "Start")
        self.gateway.transition(key, trans_name)

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
        dry_run: bool = False,
    ) -> ActionResult:
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
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        if issue.type == "dev_task" and issue.parent_type == "adhoc":
            return ActionResult(
                ok=False,
                message=f"Dev Task {key} thuộc Ad-hoc Task không qua QC. Vui lòng dùng finish() để hoàn thành.",
                changes=[],
                next_actions=["finish"],
            )

        changes = ["Chuyển trạng thái sang QC TO DO"]
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

        trans_name = self.cfg.transitions.get("submit_qc", "Submit QC")
        self.gateway.transition(key, trans_name)

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
        dry_run: bool = False,
    ) -> ActionResult:
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

        available = self.gateway.get_transitions(key)
        matched_trans = next(
            (t["name"] for t in available if t["name"].lower() in ("done", "finish", "closed", "close", "qc pass")),
            "Done",
        )
        self.gateway.transition(key, matched_trans)

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
        blocked_by: Literal["Nội bộ", "Khách hàng", "Bên thứ ba"],
        dry_run: bool = False,
    ) -> ActionResult:
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
            flagged=self.cfg.flagged_impediment_value,
            blocked_by=blocked_by,
            block_reason=reason,
            blocked_since=self._today(),
        )
        self.gateway.update_fields(key, payload)

        trans_name = self.cfg.transitions.get("block", "Block")
        available = self.gateway.get_transitions(key)
        if any(t["name"].lower() == trans_name.lower() for t in available):
            self.gateway.transition(key, trans_name)

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
        dry_run: bool = False,
    ) -> ActionResult:
        changes = ["Gỡ Block và xóa cờ Impediment"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ gỡ block {key}",
                changes=changes,
                next_actions=[],
            )

        payload = issue_fields_for_write(
            self.cfg,
            flagged=CLEAR,
            blocked_by=CLEAR,
            block_reason=CLEAR,
            blocked_since=CLEAR,
        )
        self.gateway.update_fields(key, payload)

        trans_name = self.cfg.transitions.get("unblock", "Unblock")
        available = self.gateway.get_transitions(key)
        if any(t["name"].lower() == trans_name.lower() for t in available):
            self.gateway.transition(key, trans_name)

        if note:
            self.gateway.add_comment(key, note)

        return ActionResult(
            ok=True,
            message=f"Đã gỡ block cho {key}.",
            changes=changes,
            next_actions=[],
        )

    def request_reschedule(
        self,
        key: str,
        new_end: date,
        reason: str,
        interrupted_by: str | None = None,
        no_impact: bool = False,
        note: str = "",
        dry_run: bool = False,
    ) -> ActionResult:
        if new_end < self._today():
            return ActionResult(
                ok=False,
                message=f"Không thể dời lịch về ngày trong quá khứ ({new_end}).",
                changes=[],
                next_actions=[],
            )

        is_chen_ngang = (
            "chen ngang" in reason.lower()
            or "chèn ngang" in reason.lower()
            or reason == "Việc chen ngang"
        )
        if is_chen_ngang and not interrupted_by:
            return ActionResult(
                ok=False,
                message="Lý do việc chen ngang bắt buộc phải có interrupted_by.",
                changes=[],
                next_actions=[],
            )

        changes = []
        if no_impact:
            changes.append("Xác nhận không ảnh hưởng tiến độ (Confirm No Impact)")
        else:
            changes.extend([
                f"Yêu cầu dời lịch đến {new_end}",
                f"Lý do: {reason}",
            ])
            if interrupted_by:
                changes.append(f"Gắn link bị chen ngang bởi: {interrupted_by}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ yêu cầu dời lịch cho {key}",
                changes=changes,
                next_actions=["decide_reschedule"],
            )

        if no_impact:
            trans_name = self.cfg.transitions.get("confirm_no_impact", "Confirm No Impact")
            self.gateway.transition(key, trans_name)
            if note:
                self.gateway.add_comment(key, note)
        else:
            payload = issue_fields_for_write(
                self.cfg,
                proposed_end=new_end,
                reschedule_reason=reason,
                reschedule_status=self.cfg.reschedule_pending_value,
                reschedule_requester=self.gateway.current_user(),
            )
            self.gateway.update_fields(key, payload)

            trans_name = self.cfg.transitions.get("request_reschedule", "Request Reschedule")
            available = self.gateway.get_transitions(key)
            if any(t["name"].lower() == trans_name.lower() for t in available):
                self.gateway.transition(key, trans_name)

            if interrupted_by:
                self.gateway.link("Interrupts", inward_key=key, outward_key=interrupted_by)

            if note:
                self.gateway.add_comment(key, note)

        return ActionResult(
            ok=True,
            message=f"Đã gửi yêu cầu dời lịch cho {key}.",
            changes=changes,
            next_actions=["decide_reschedule"],
        )

    def end_day(
        self,
        entries: list[EndDayEntry],
        dry_run: bool = False,
    ) -> ActionResult:
        changes: list[str] = []
        for e in entries:
            changes.append(f"Log {e.time_spent} trên {e.key}: {e.note}")
            if e.move_to:
                changes.append(f"Chuyển {e.key} sang {e.move_to}")

        if dry_run:
            return ActionResult(
                ok=True,
                message="[Dry-run] Sẽ ghi nhận công việc cuối ngày",
                changes=changes,
                next_actions=[],
            )

        for e in entries:
            self.gateway.add_worklog(e.key, e.time_spent, e.note)
            if e.move_to == "ready_for_qc":
                self.submit_qc(e.key)
            elif e.move_to == "done":
                self.finish(e.key)

        return ActionResult(
            ok=True,
            message="Đã ghi nhận công việc cuối ngày thành công.",
            changes=changes,
            next_actions=[],
        )

    # -------------------------------------------------------------------------
    # QC
    # -------------------------------------------------------------------------

    def qc_start(self, key: str, dry_run: bool = False) -> ActionResult:
        changes = ["Chuyển trạng thái sang Test In Progress"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ bắt đầu kiểm thử {key}",
                changes=changes,
                next_actions=["qc_pass", "qc_fail"],
            )

        trans_name = self.cfg.transitions.get("start_test", "Start Test")
        self.gateway.transition(key, trans_name)

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
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        if issue.type == "story" and not time_spent:
            return ActionResult(
                ok=False,
                message=f"QC Pass cho Story ({key}) bắt buộc phải nhập thời gian (time_spent).",
                changes=[],
                next_actions=[],
            )

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
            self.gateway.add_worklog(key, time_spent, note)

        trans_name = self.cfg.transitions.get("qc_pass", "QC Pass")
        self.gateway.transition(key, trans_name)

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
        bugs: list[str] = [],
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        if issue.type == "story" and not bugs:
            return ActionResult(
                ok=False,
                message=f"QC Fail cho Story ({key}) bắt buộc phải có ít nhất 1 bug.",
                changes=[],
                next_actions=[],
            )

        changes = [f"Chuyển trạng thái QC Fail: {reason}"]
        for b in bugs:
            changes.append(f"Tạo Bug sub-task: {b}")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ báo QC Fail cho {key}",
                changes=changes,
                next_actions=[],
            )

        if issue.type == "story":
            target_story_key = issue.key
            code = issue.actor_code or "FLOW"
        else:
            target_story_key = issue.parent_key
            code = "BUG"

        bug_type_name = self.cfg.issue_types.get("bug_sub", ["Bug"])[0]
        for b in bugs:
            summary = f"[{code}][BUG] {b}"
            create_payload = {
                "project": {"key": issue.project},
                "issuetype": {"name": bug_type_name},
                "summary": summary,
            }
            if target_story_key:
                create_payload["parent"] = {"key": target_story_key}
            self.gateway.create_issue(create_payload)

        if time_spent:
            self.gateway.add_worklog(key, time_spent, comment=reason)

        trans_name = self.cfg.transitions.get("qc_fail", "QC Fail")
        self.gateway.transition(key, trans_name, comment=reason)

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
        story_key: str,
        new_due: date,
        reason: str,
        note: str = "",
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(story_key)
        if issue.type != "story":
            return ActionResult(
                ok=False,
                message=f"Issue {story_key} không phải là Story.",
                changes=[],
                next_actions=[],
            )
        if new_due < self._today():
            return ActionResult(
                ok=False,
                message=f"Không thể dời Due Date về ngày trong quá khứ ({new_due}).",
                changes=[],
                next_actions=[],
            )

        changes = [
            f"Yêu cầu dời Due Date sang {new_due}",
            f"Lý do: {reason}",
        ]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ yêu cầu dời Due Date cho {story_key}",
                changes=changes,
                next_actions=["decide_reschedule"],
            )

        payload = issue_fields_for_write(
            self.cfg,
            proposed_end=new_due,
            reschedule_reason=reason,
            reschedule_status=self.cfg.reschedule_pending_value,
            reschedule_requester=self.gateway.current_user(),
        )
        self.gateway.update_fields(story_key, payload)

        trans_name = self.cfg.transitions.get("request_reschedule", "Request Reschedule")
        available = self.gateway.get_transitions(story_key)
        if any(t["name"].lower() == trans_name.lower() for t in available):
            self.gateway.transition(story_key, trans_name)

        if note:
            self.gateway.add_comment(story_key, note)

        return ActionResult(
            ok=True,
            message=f"Đã gửi yêu cầu dời Due Date cho {story_key}.",
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
        dry_run: bool = False,
    ) -> ActionResult:
        # Fetch stories under this epic
        proj_key = epic_key.split("-")[0]
        raw_issues = self.gateway.search(f'project = "{proj_key}"', fields=["*all"])
        actor_stories: list[Issue] = []
        for raw in raw_issues:
            iss = issue_from_json(raw, self.cfg)
            if iss.type == "story" and (iss.epic_key == epic_key or iss.parent_key == epic_key):
                if actor in iss.components or (iss.actor_code and actor.startswith(iss.actor_code)):
                    actor_stories.append(iss)

        if not actor_stories:
            return ActionResult(
                ok=False,
                message=f"Không tìm thấy Story nào của actor '{actor}' trong epic {epic_key}.",
                changes=[],
                next_actions=[],
            )

        if action == "start":
            target_stories = [s for s in actor_stories if s.status == "ready_for_uat"]
            if not target_stories:
                return ActionResult(
                    ok=False,
                    message=f"Không có Story nào của actor '{actor}' ở trạng thái Ready For UAT để bắt đầu UAT.",
                    changes=[],
                    next_actions=[],
                )
            changes = [f"Chuyển {s.key} sang Client UAT" for s in target_stories]
            if dry_run:
                return ActionResult(
                    ok=True,
                    message=f"[Dry-run] Sẽ bắt đầu UAT cho {len(target_stories)} Story",
                    changes=changes,
                    next_actions=["uat signoff", "uat fail"],
                )
            trans_name = self.cfg.transitions.get("client_uat", "Client UAT")
            for s in target_stories:
                self.gateway.transition(s.key, trans_name, comment=note)
            return ActionResult(
                ok=True,
                message=f"Đã bắt đầu UAT cho {len(target_stories)} Story của actor {actor}.",
                changes=changes,
                next_actions=["uat signoff", "uat fail"],
            )

        elif action == "signoff":
            if story_keys:
                target_stories = [s for s in actor_stories if s.key in story_keys]
            else:
                not_in_uat = [s for s in actor_stories if s.status != "client_uat" and not s.is_resolved]
                if not_in_uat:
                    keys_str = ", ".join(s.key for s in not_in_uat)
                    return ActionResult(
                        ok=False,
                        message=f"Không thể ký UAT: còn Story của actor chưa ở trạng thái Đang UAT (Wait For Review): {keys_str}.",
                        changes=[],
                        next_actions=[],
                    )
                target_stories = [s for s in actor_stories if s.status == "client_uat"]

            changes = [f"Ký duyệt UAT cho {s.key}" for s in target_stories]
            if dry_run:
                return ActionResult(
                    ok=True,
                    message=f"[Dry-run] Sẽ ký duyệt UAT cho {len(target_stories)} Story",
                    changes=changes,
                    next_actions=[],
                )
            trans_name = self.cfg.transitions.get("uat_signoff", "UAT Sign-off")
            for s in target_stories:
                self.gateway.transition(s.key, trans_name, comment=note)
            return ActionResult(
                ok=True,
                message=f"Đã ký duyệt UAT cho {len(target_stories)} Story của actor {actor}.",
                changes=changes,
                next_actions=[],
            )

        elif action == "fail":
            if not story_keys:
                return ActionResult(
                    ok=False,
                    message="Hành động UAT Fail bắt buộc phải cung cấp danh sách story_keys.",
                    changes=[],
                    next_actions=[],
                )
            target_stories = [s for s in actor_stories if s.key in story_keys]
            changes = [f"UAT Fail cho {s.key}" for s in target_stories]
            if dry_run:
                return ActionResult(
                    ok=True,
                    message=f"[Dry-run] Sẽ báo UAT Fail cho {len(target_stories)} Story",
                    changes=changes,
                    next_actions=[],
                )

            trans_name = self.cfg.transitions.get("uat_fail", "UAT Fail")
            bug_type_name = self.cfg.issue_types.get("bug_sub", ["Bug"])[0]
            for s in target_stories:
                s_bugs = (bugs or {}).get(s.key, [])
                actor_code = s.actor_code or "FLOW"
                for b in s_bugs:
                    self.gateway.create_issue({
                        "project": {"key": s.project},
                        "issuetype": {"name": bug_type_name},
                        "summary": f"[{actor_code}][BUG] {b}",
                        "parent": {"key": s.key},
                    })
                self.gateway.transition(s.key, trans_name, comment=note)

            return ActionResult(
                ok=True,
                message=f"Đã xử lý UAT Fail cho {len(target_stories)} Story.",
                changes=changes,
                next_actions=[],
            )

        return ActionResult(ok=False, message=f"Hành động UAT không hợp lệ: {action}", changes=[], next_actions=[])

    # -------------------------------------------------------------------------
    # Lead-BA
    # -------------------------------------------------------------------------

    def decide_reschedule(
        self,
        key: str,
        approve: bool,
        note: str,
        dry_run: bool = False,
    ) -> ActionResult:
        issue = self._get_issue(key)
        current_user = self.gateway.current_user()
        if issue.reschedule_requester and current_user == issue.reschedule_requester:
            return ActionResult(
                ok=False,
                message="Người yêu cầu không được tự duyệt yêu cầu dời lịch của chính mình.",
                changes=[],
                next_actions=[],
            )

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
            trans_name = self.cfg.transitions.get("approve_reschedule", "Approve Reschedule")
            write_args = {
                "reschedule_status": CLEAR,
                "reschedule_requester": CLEAR,
                "proposed_end": CLEAR,
            }
            if issue.type == "story":
                write_args["due_date"] = issue.proposed_end
            else:
                write_args["planned_end"] = issue.proposed_end
            payload = issue_fields_for_write(self.cfg, **write_args)
            self.gateway.update_fields(key, payload)

            available = self.gateway.get_transitions(key)
            if any(t["name"].lower() == trans_name.lower() for t in available):
                self.gateway.transition(key, trans_name)

            if note:
                self.gateway.add_comment(key, f"Phê duyệt dời lịch: {note}")
        else:
            trans_name = self.cfg.transitions.get("reject_reschedule", "Reject Reschedule")
            payload = issue_fields_for_write(
                self.cfg,
                reschedule_status=CLEAR,
                reschedule_requester=CLEAR,
                proposed_end=CLEAR,
            )
            self.gateway.update_fields(key, payload)

            available = self.gateway.get_transitions(key)
            if any(t["name"].lower() == trans_name.lower() for t in available):
                self.gateway.transition(key, trans_name)

            if note:
                self.gateway.add_comment(key, f"Từ chối dời lịch: {note}")

        return ActionResult(
            ok=True,
            message=f"Đã xử lý quyết định dời lịch cho {key}.",
            changes=changes,
            next_actions=[],
        )

    def cancel(self, key: str, reason: str, dry_run: bool = False) -> ActionResult:
        changes = [f"Hủy issue {key}", f"Lý do: {reason}"]
        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ hủy {key}",
                changes=changes,
                next_actions=[],
            )

        trans_name = self.cfg.transitions.get("cancel", "Cancel")
        self.gateway.transition(key, trans_name, comment=f"Hủy: {reason}")
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
        dry_run: bool = False,
    ) -> ActionResult:
        month_str = self._today().strftime("%m/%Y")
        ops_prefix = self.cfg.ops_epic_prefix
        raw_issues = self.gateway.search(f'project = "{project}"', fields=["*all"])
        ops_epic: Issue | None = None
        for raw in raw_issues:
            iss = issue_from_json(raw, self.cfg)
            if iss.type == "epic" and iss.summary.startswith(ops_prefix) and month_str in iss.summary:
                ops_epic = iss
                break

        if not ops_epic:
            return ActionResult(
                ok=False,
                message=f"Không tìm thấy Epic [OPS] tháng hiện tại ({month_str}) cho dự án {project}. Vui lòng chạy eggs-guardian hoặc tạo Epic [OPS] trước.",
                changes=[],
                next_actions=[],
            )

        proj_cfg = self.cfg.projects.get(project)
        triage_owner = proj_cfg.triage_owner if (proj_cfg and proj_cfg.triage_owner) else self.cfg.lead_user

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

        report_type_name = self.cfg.issue_types.get("report", ["User Report"])[0]
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
            create_fields[self.cfg.fields["reported_at"]] = rep_dt.isoformat()

        new_key = self.gateway.create_issue(create_fields)

        start_triage_trans = self.cfg.transitions.get("start_triage", "Start Triage")
        self.gateway.transition(new_key, start_triage_trans)

        return ActionResult(
            ok=True,
            message=f"Đã tạo phản ánh {new_key} và bắt đầu triage.",
            changes=changes,
            next_actions=["triage_close"],
        )

    def triage_close(
        self,
        key: str,
        verdict: Literal["bug", "guidance", "feature", "data_fix", "duplicate", "cannot_reproduce", "third_party"],
        note: str,
        time_spent: str,
        bug_story_key: str | None = None,
        bug_summary: str | None = None,
        link_key: str | None = None,
        dry_run: bool = False,
    ) -> ActionResult:
        report = self._get_issue(key)

        if verdict == "bug":
            if bug_story_key:
                bug_story = self._get_issue(bug_story_key)
                if bug_story.is_resolved:
                    return ActionResult(
                        ok=False,
                        message="Không thể tạo Bug sub-task dưới Story đã hoàn thành (Done).",
                        changes=[],
                        next_actions=[],
                    )
        elif verdict in ("feature", "data_fix", "duplicate"):
            if not link_key:
                return ActionResult(
                    ok=False,
                    message=f"Kết luận '{verdict}' bắt buộc phải cung cấp link_key để liên kết.",
                    changes=[],
                    next_actions=[],
                )

        resolution_name = self.cfg.report_resolutions.get(verdict, verdict)
        changes = [
            f"Log QC time: {time_spent}",
            f"Kết luận: {resolution_name}",
        ]
        if verdict == "bug":
            if bug_story_key:
                changes.append(f"Tạo Bug sub-task dưới Story {bug_story_key} và liên kết Causes")
            else:
                changes.append("Tạo Bug stand-alone trong Epic [OPS] và liên kết Causes")
        elif link_key:
            changes.append(f"Liên kết tới {link_key}")
        changes.append("Đóng User Report (Close Report)")

        if dry_run:
            return ActionResult(
                ok=True,
                message=f"[Dry-run] Sẽ đóng User Report {key}",
                changes=changes,
                next_actions=[],
            )

        self.gateway.add_worklog(key, time_spent=time_spent, comment=note)

        if verdict == "bug":
            if bug_story_key:
                story = self._get_issue(bug_story_key)
                actor_code = story.actor_code or "FLOW"
                summary = bug_summary or f"[{actor_code}][BUG] {note}"
                bug_type_name = self.cfg.issue_types.get("bug_sub", ["Bug"])[0]
                bug_key = self.gateway.create_issue({
                    "project": {"key": story.project},
                    "issuetype": {"name": bug_type_name},
                    "summary": summary,
                    "parent": {"key": bug_story_key},
                })
                self.gateway.link("Causes", inward_key=key, outward_key=bug_key)
            else:
                month_str = self._today().strftime("%m/%Y")
                ops_prefix = self.cfg.ops_epic_prefix
                raw_issues = self.gateway.search(f'project = "{report.project}"', fields=["*all"])
                ops_epic = None
                for raw in raw_issues:
                    iss = issue_from_json(raw, self.cfg)
                    if iss.type == "epic" and iss.summary.startswith(ops_prefix) and month_str in iss.summary:
                        ops_epic = iss
                        break
                bug_type_name = self.cfg.issue_types.get("bug", ["Bug stand-alone"])[0]
                create_payload: dict[str, Any] = {
                    "project": {"key": report.project},
                    "issuetype": {"name": bug_type_name},
                    "summary": bug_summary or f"[HOTFIX] {note}",
                }
                if ops_epic and self.cfg.fields.get("epic_link"):
                    create_payload[self.cfg.fields["epic_link"]] = ops_epic.key
                bug_key = self.gateway.create_issue(create_payload)
                self.gateway.link("Causes", inward_key=key, outward_key=bug_key)

        elif verdict in ("feature", "data_fix"):
            if link_key:
                self.gateway.link("Relates", inward_key=key, outward_key=link_key)
        elif verdict == "duplicate":
            if link_key:
                self.gateway.link("Duplicate", inward_key=key, outward_key=link_key)

        trans_name = self.cfg.transitions.get("close_report", "Close Report")
        self.gateway.transition(
            key,
            trans_name,
            fields={"resolution": {"name": resolution_name}},
            comment=f"Kết luận triage: {resolution_name}. Ghi chú: {note}",
        )

        reply_suggestion = (
            f"Gợi ý trả lời group chat: 'Chào bạn, phản ánh đã được tiếp nhận và xử lý ({resolution_name}). "
            f"{note}. Cảm ơn bạn đã phản ánh!'"
        )

        return ActionResult(
            ok=True,
            message=f"Đã đóng phản ánh {key}. {reply_suggestion}",
            changes=changes,
            next_actions=[],
        )
