from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from mcp_atlassian.eggs.gateway import JiraError, JiraGateway
from mcp_atlassian.eggs.model import Worklog


class FakeGateway(JiraGateway):
    """In-memory fake gateway for offline testing.

    `search` ignores JQL unless a `search_filter: Callable[[str, dict], bool]`
    is passed to the constructor.
    `worklogs_since` filters on `started >= since`.
    """

    def __init__(
        self,
        issues: dict[str, dict] | None = None,
        worklogs: list[Worklog] | None = None,
        transitions: dict[str, list[str]] | None = None,
        components_map: dict[str, list[str]] | None = None,
        user: str = "cuong.huynh",
        status_after: dict[str, str] | None = None,
        search_filter: Callable[[str, dict], bool] | None = None,
        me: dict | None = None,
        role_members: dict[str, dict[str, list[str]]] | None = None,
        errors_on_transition: dict[tuple[str, str], JiraError] | None = None,
        field_definitions: list[dict] | None = None,
    ) -> None:
        self.issues = dict(issues or {})
        self.worklogs = list(worklogs or [])
        self.transitions = dict(transitions or {})
        self.components_map = dict(components_map or {})
        self.user = user
        self.status_after = dict(status_after or {})
        self.search_filter = search_filter
        self.me = dict(me if me is not None else {"name": user, "displayName": user})
        self.role_members = dict(role_members or {})
        self.errors_on_transition = dict(errors_on_transition or {})
        self.field_definitions = list(field_definitions or [])
        self.calls: list[tuple[str, tuple, dict[str, Any]]] = []
        self._next_id = 1000

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def search(
        self,
        jql: str,
        fields: list[str],
        expand_changelog: bool = False,  # noqa: FBT001, FBT002
    ) -> list[dict]:
        """Search in-memory issues.

        Note:
            Ignores JQL unless a `search_filter: Callable[[str, dict], bool]` is passed to __init__.
        """
        self._record("search", jql, fields, expand_changelog=expand_changelog)
        if self.search_filter is not None:
            return [iss for iss in self.issues.values() if self.search_filter(jql, iss)]
        return list(self.issues.values())

    def get_issue(self, key: str) -> dict:
        self._record("get_issue", key)
        if key not in self.issues:
            msg = f"Issue {key} not found"
            raise JiraError(msg)
        return self.issues[key]

    def get_transitions(self, key: str) -> list[dict]:
        self._record("get_transitions", key)
        names = self.transitions.get(key, [])
        return [{"id": str(i + 1), "name": name} for i, name in enumerate(names)]

    def transition(
        self,
        key: str,
        transition_name: str,
        fields: dict | None = None,
        comment: str | None = None,
    ) -> None:
        self._record("transition", key, transition_name, fields=fields, comment=comment)
        matched_trans_key = next(
            (
                k
                for k in self.errors_on_transition
                if k[0] == key and k[1].lower() == transition_name.lower()
            ),
            None,
        )
        if matched_trans_key:
            raise self.errors_on_transition[matched_trans_key]
        available = self.transitions.get(key, [])
        matched = next(
            (name for name in available if name.lower() == transition_name.lower()),
            None,
        )
        if not matched:
            msg = f"Transition '{transition_name}' not available for {key}"
            raise JiraError(msg)

        issue = self.issues.get(key)
        if issue:
            issue_fields = issue.setdefault("fields", {})
            for t_name, new_status in self.status_after.items():
                if t_name.lower() == transition_name.lower():
                    status_dict = issue_fields.setdefault("status", {})
                    status_dict["name"] = new_status
                    break
            if fields:
                issue_fields.update(fields)
            if comment:
                comments = issue_fields.setdefault("comment", {}).setdefault(
                    "comments", []
                )
                comments.append({"body": comment, "author": {"name": self.user}})

    def update_fields(
        self,
        key: str,
        fields: dict,
        *,
        notify_users: bool = True,  # noqa: FBT001, FBT002
    ) -> None:
        self._record("update_fields", key, fields, notify_users=notify_users)
        if key in self.issues:
            issue_fields = self.issues[key].setdefault("fields", {})
            issue_fields.update(fields)

    def add_comment(self, key: str, body: str) -> None:
        self._record("add_comment", key, body)
        if key in self.issues:
            issue_fields = self.issues[key].setdefault("fields", {})
            comments = issue_fields.setdefault("comment", {}).setdefault("comments", [])
            comments.append({"body": body, "author": {"name": self.user}})

    def add_worklog(
        self,
        key: str,
        time_spent: str,
        comment: str,
        started: datetime | None = None,
    ) -> None:
        self._record("add_worklog", key, time_spent, comment, started=started)
        # Parse approximate seconds from time_spent (e.g. 1h, 2d, 30m, 3600)
        seconds = 3600
        if time_spent.endswith("h"):
            seconds = int(time_spent[:-1]) * 3600
        elif time_spent.endswith("d"):
            seconds = int(time_spent[:-1]) * 8 * 3600
        elif time_spent.endswith("m"):
            seconds = int(time_spent[:-1]) * 60
        elif time_spent.isdigit():
            seconds = int(time_spent)

        log = Worklog(
            issue_key=key,
            author=self.user,
            started=started or datetime.now(timezone.utc),
            seconds=seconds,
        )
        self.worklogs.append(log)
        if key in self.issues:
            issue_fields = self.issues[key].setdefault("fields", {})
            wls = issue_fields.setdefault("worklog", {}).setdefault("worklogs", [])
            wls.append(
                {
                    "author": {"name": self.user},
                    "timeSpent": time_spent,
                    "comment": comment,
                }
            )

    def create_issue(self, fields: dict) -> str:
        self._record("create_issue", fields)
        project = fields.get("project", {}).get("key", "TEST")
        self._next_id += 1
        key = f"{project}-{self._next_id}"
        self.issues[key] = {
            "id": str(self._next_id),
            "key": key,
            "fields": dict(fields),
        }
        if "parent" in fields and fields["parent"].get("key") in self.issues:
            parent_key = fields["parent"]["key"]
            p_subtasks = (
                self.issues[parent_key]
                .setdefault("fields", {})
                .setdefault("subtasks", [])
            )
            p_subtasks.append(
                {
                    "id": str(self._next_id),
                    "key": key,
                    "fields": {
                        "issuetype": fields.get("issuetype", {}),
                        "status": {"name": "DEV To Do"},
                        "summary": fields.get("summary", ""),
                    },
                }
            )
        return key

    def link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        self._record("link", link_type, inward_key, outward_key)

    def worklogs_since(self, since: datetime) -> list[Worklog]:
        """Fetch worklogs updated since given datetime.

        Note:
            Filters in-memory worklogs on `w.started >= since`.
        """
        self._record("worklogs_since", since)
        return [w for w in self.worklogs if w.started >= since]

    def components(self, project: str) -> list[str]:
        self._record("components", project)
        return list(self.components_map.get(project, []))

    def current_user(self) -> str:
        self._record("current_user")
        return self.user

    def myself(self) -> dict:
        self._record("myself")
        return dict(self.me)

    def project_role_members(
        self, project: str, role_names: list[str] | None = None
    ) -> dict[str, list[str]]:
        self._record("project_role_members", project)
        all_roles = dict(self.role_members.get(project, {}))
        if role_names is not None:
            norm_names = {r.strip().lower() for r in role_names}
            return {
                k: v for k, v in all_roles.items() if k.strip().lower() in norm_names
            }
        return all_roles

    def get_fields(self) -> list[dict]:
        self._record("get_fields")
        return list(self.field_definitions)

    def get_subtasks(self, key: str) -> list[dict]:
        self._record("get_subtasks", key)
        subtasks = self.issues.get(key, {}).get("fields", {}).get("subtasks", [])
        if subtasks:
            return list(subtasks)
        found = []
        for iss_key, iss_dict in self.issues.items():
            fields = iss_dict.get("fields", {})
            if fields.get("parent", {}).get("key") == key:
                found.append(
                    {
                        "id": iss_dict.get("id", ""),
                        "key": iss_key,
                        "fields": {
                            "issuetype": fields.get("issuetype", {}),
                            "status": fields.get("status", {}),
                            "summary": fields.get("summary", ""),
                        },
                    }
                )
        return found

    def get_worklogs(self, key: str) -> list[dict]:
        self._record("get_worklogs", key)
        wls: list[dict[str, Any]] = []
        if key in self.issues:
            wls = list(
                self.issues[key]
                .get("fields", {})
                .get("worklog", {})
                .get("worklogs", [])
            )
        for w in self.worklogs:
            if w.issue_key == key:
                if not any(wl.get("author", {}).get("name") == w.author for wl in wls):
                    wls.append(
                        {
                            "author": {"name": w.author},
                            "timeSpentSeconds": w.seconds,
                            "started": w.started.isoformat(),
                        }
                    )
        return wls
