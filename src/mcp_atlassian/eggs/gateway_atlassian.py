from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from requests.exceptions import HTTPError

from .gateway import JiraError, JiraGateway
from .mapping import parse_jira_dt
from .model import Worklog

logger = logging.getLogger(__name__)


def _handle_http_error(e: HTTPError) -> JiraError:
    status_code = e.response.status_code if e.response is not None else None
    error_messages: list[str] = []
    errors_dict: dict[str, str] = {}
    if e.response is not None:
        try:
            data = e.response.json()
            if isinstance(data, dict):
                error_messages = data.get("errorMessages") or []
                errors_dict = data.get("errors") or {}
        except Exception:  # noqa: BLE001, S110
            pass
    all_errs = list(error_messages) + list(errors_dict.values())
    msg = "\n".join(all_errs) if all_errs else str(e)
    return JiraError(
        msg,
        status_code=status_code,
        error_messages=error_messages,
        errors=errors_dict,
    )


class AtlassianGateway(JiraGateway):
    """Adapter implementing JiraGateway on top of the upstream Jira client."""

    def __init__(self, client: Any, cfg: Any = None) -> None:
        self.client = client
        self.cfg = cfg
        self._myself_cache: dict | None = None

    def search(
        self, jql: str, fields: list[str], *, expand_changelog: bool = False
    ) -> list[dict]:
        """Search issues using JQL with pagination support."""
        issues: list[dict] = []
        start_at = 0
        max_results = 50
        expand = "changelog" if expand_changelog else None

        while True:
            res = self.client.jql(
                jql,
                start=start_at,
                limit=max_results,
                fields=fields,
                expand=expand,
            )
            if not isinstance(res, dict):
                break
            batch = res.get("issues", [])
            issues.extend(batch)
            total = res.get("total", 0)
            start_at += len(batch)
            if start_at >= total or not batch:
                break

        return issues

    def get_issue(self, key: str) -> dict:
        """Fetch raw issue JSON by key."""
        res = self.client.get(f"rest/api/2/issue/{key}")
        if not isinstance(res, dict):
            msg = f"Failed to fetch issue {key}"
            raise JiraError(msg)
        return res

    def get_transitions(self, key: str) -> list[dict]:
        """Fetch available transitions for an issue: [{'id': ..., 'name': ...}]."""
        res = self.client.get(f"rest/api/2/issue/{key}/transitions")
        transitions = res.get("transitions", []) if isinstance(res, dict) else []
        return [
            {"id": str(t["id"]), "name": t["name"]}
            for t in transitions
            if "id" in t and "name" in t
        ]

    def transition(
        self,
        key: str,
        transition_name: str,
        fields: dict | None = None,
        comment: str | None = None,
    ) -> None:
        """Perform transition on issue by name. Raises JiraError if unavailable."""
        transitions = self.get_transitions(key)
        matched = next(
            (t for t in transitions if t["name"].lower() == transition_name.lower()),
            None,
        )
        if not matched:
            msg = f"Transition '{transition_name}' not available for {key}"
            raise JiraError(msg)

        payload: dict[str, Any] = {"transition": {"id": matched["id"]}}
        if fields:
            payload["fields"] = fields
        if comment:
            payload["update"] = {"comment": [{"add": {"body": comment}}]}

        try:
            self.client.post(f"rest/api/2/issue/{key}/transitions", data=payload)
        except HTTPError as e:
            raise _handle_http_error(e) from e
        if comment:
            self._ensure_comment_kept(key, comment)

    def _ensure_comment_kept(self, key: str, comment: str) -> None:
        """Re-add a transition comment that Jira dropped.

        Jira DC silently ignores ``update.comment`` on a transition without a
        screen (Unblock, Start, Finish...). It does not say which transitions
        have one, so check the latest comments and post it when missing.
        """
        try:
            recent = self.get_comments(key)[-3:]
            if any(str(c.get("body", "")).strip() == comment.strip() for c in recent):
                return
        except Exception:  # noqa: BLE001
            logger.warning("Issue %s: không đọc được comment để kiểm tra", key)
        self.add_comment(key, comment)

    def update_fields(
        self,
        key: str,
        fields: dict,
        *,
        notify_users: bool = True,
    ) -> None:
        """Update fields on an issue."""
        params = {"notifyUsers": "false"} if not notify_users else None
        try:
            self.client.put(
                f"rest/api/2/issue/{key}",
                data={"fields": fields},
                params=params,
            )
        except HTTPError as e:
            raise _handle_http_error(e) from e

    def add_label(
        self,
        key: str,
        label: str,
        *,
        notify_users: bool = False,
    ) -> None:
        """Add a label to an issue using Jira update syntax."""
        params = {"notifyUsers": "false"} if not notify_users else None
        try:
            self.client.put(
                f"rest/api/2/issue/{key}",
                data={"update": {"labels": [{"add": label}]}},
                params=params,
            )
        except HTTPError as e:
            raise _handle_http_error(e) from e

    def remove_label(
        self,
        key: str,
        label: str,
        *,
        notify_users: bool = False,
    ) -> None:
        """Remove a label from an issue using Jira update syntax."""
        params = {"notifyUsers": "false"} if not notify_users else None
        try:
            self.client.put(
                f"rest/api/2/issue/{key}",
                data={"update": {"labels": [{"remove": label}]}},
                params=params,
            )
        except HTTPError as e:
            raise _handle_http_error(e) from e

    def add_comment(self, key: str, body: str) -> None:
        """Add a comment to an issue."""
        self.client.post(f"rest/api/2/issue/{key}/comment", data={"body": body})

    def add_worklog(
        self,
        key: str,
        time_spent: str,
        comment: str,
        started: datetime | None = None,
    ) -> None:
        """Add a worklog to an issue."""
        if started is not None:
            if started.tzinfo is None or started.tzinfo.utcoffset(started) is None:
                raise ValueError("started datetime must be timezone-aware")
        payload: dict[str, Any] = {
            "timeSpent": time_spent,
            "comment": comment,
        }
        if started:
            payload["started"] = started.strftime("%Y-%m-%dT%H:%M:%S.000%z")
        self.client.post(f"rest/api/2/issue/{key}/worklog", data=payload)

    def create_issue(self, fields: dict) -> str:
        """Create a new issue and return its key."""
        res = self.client.post("rest/api/2/issue", data={"fields": fields})
        if not isinstance(res, dict) or "key" not in res:
            msg = f"Failed to create issue: {res}"
            raise JiraError(msg)
        return res["key"]

    def link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        """Link two issues with a link type."""
        payload = {
            "type": {"name": link_type},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        }
        self.client.post("rest/api/2/issueLink", data=payload)

    def worklogs_since(self, since: datetime) -> list[Worklog]:
        """Fetch worklogs updated since given datetime mapped to Worklog models."""
        if since.tzinfo is None or since.tzinfo.utcoffset(since) is None:
            raise ValueError("since datetime must be timezone-aware")

        since_ms = int(since.timestamp() * 1000)
        ids: list[int] = []
        url: str | None = f"rest/api/2/worklog/updated?since={since_ms}"

        while url:
            updated_res = self.client.get(url)
            if not isinstance(updated_res, dict):
                break
            values = updated_res.get("values", [])
            for v in values:
                if "worklogId" in v:
                    ids.append(v["worklogId"])

            if updated_res.get("lastPage", True):
                break

            # Follow nextPage or since of the response
            next_page = updated_res.get("nextPage")
            if next_page:
                if "rest/api/2/worklog/updated" in next_page:
                    idx = next_page.find("rest/api/2/worklog/updated")
                    url = next_page[idx:]
                else:
                    url = next_page
            elif "until" in updated_res or "since" in updated_res:
                next_since = updated_res.get("until", updated_res.get("since"))
                url = f"rest/api/2/worklog/updated?since={next_since}"
            else:
                break

        if not ids:
            return []

        # Call /worklog/list in chunks of <= 1000 ids
        worklogs_data: list[dict] = []
        chunk_size = 1000
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            res = self.client.post("rest/api/2/worklog/list", data={"ids": chunk})
            if isinstance(res, list):
                worklogs_data.extend(res)

        if not worklogs_data:
            return []

        # Find which worklogs need issue key resolution
        issue_ids_to_resolve: set[str] = set()
        for item in worklogs_data:
            if (
                not item.get("issueKey")
                and not (item.get("issue") and item["issue"].get("key"))
                and item.get("issueId")
            ):
                issue_ids_to_resolve.add(str(item["issueId"]))

        id_to_key_map: dict[str, str] = {}
        if issue_ids_to_resolve:
            jql = f"id in ({','.join(sorted(issue_ids_to_resolve))})"
            issues = self.search(jql, fields=["key"])
            for iss in issues:
                if "id" in iss and "key" in iss:
                    id_to_key_map[str(iss["id"])] = iss["key"]

        result: list[Worklog] = []
        for item in worklogs_data:
            resolved_key = (
                item.get("issueKey")
                or (item.get("issue") and item["issue"].get("key"))
                or id_to_key_map.get(str(item.get("issueId")))
            )
            if not resolved_key:
                logger.warning(
                    "Skipping worklog %s: issueId %s could not be resolved "
                    "to an issue key",
                    item.get("id"),
                    item.get("issueId"),
                )
                continue

            author_data = item.get("author") or {}
            author = (
                author_data.get("name")
                or author_data.get("key")
                or author_data.get("displayName")
                or "unknown"
            )
            started_str = item.get("started")
            started_dt = parse_jira_dt(started_str, self.cfg)
            if started_dt is None:
                logger.warning(
                    "Skipping worklog %s: invalid or missing started timestamp %s",
                    item.get("id"),
                    started_str,
                )
                continue

            seconds = item.get("timeSpentSeconds") or 0

            result.append(
                Worklog(
                    issue_key=resolved_key,
                    author=author,
                    started=started_dt,
                    seconds=int(seconds),
                )
            )

        return result

    def components(self, project: str) -> list[str]:
        """Get component names for a project."""
        res = self.client.get(f"rest/api/2/project/{project}/components")
        if isinstance(res, list):
            return [c["name"] for c in res if isinstance(c, dict) and "name" in c]
        return []

    def create_component(
        self, project: str, name: str, description: str | None = None
    ) -> dict[str, Any]:
        """Create a component via POST rest/api/2/component.

        Call this on the bot gateway only (its client uses EGGS_BOT_TOKEN).
        """
        client = self.client
        payload: dict[str, Any] = {
            "name": name,
            "project": project,
        }
        if description:
            payload["description"] = description
        try:
            res = client.post("rest/api/2/component", data=payload)
            if isinstance(res, dict):
                return res
            return {"name": name, "project": project}
        except HTTPError as e:
            raise _handle_http_error(e) from e

    def get_components(self, project: str) -> list[dict[str, Any]]:
        """Get component objects for a project: [{'name': ..., 'description': ...}]."""
        res = self.client.get(f"rest/api/2/project/{project}/components")
        if isinstance(res, list):
            return [c for c in res if isinstance(c, dict) and "name" in c]
        return []

    def current_user(self) -> str:
        """Get the current authenticated user's name/key."""
        res = self.client.get("rest/api/2/myself")
        if isinstance(res, dict):
            return res.get("name") or res.get("key") or res.get("displayName") or ""
        return ""

    def myself(self) -> dict:
        """Fetch info about the current authenticated user:
        {'name': ..., 'displayName': ...}. Cached.
        """
        if self._myself_cache is None:
            res = self.client.get("rest/api/2/myself")
            self._myself_cache = res if isinstance(res, dict) else {}
        return self._myself_cache

    def project_role_members(
        self, project: str, role_names: list[str] | None = None
    ) -> dict[str, list[str]]:
        """Fetch role members mapped by Jira role name: {role_name: [usernames]}."""
        target_role_names: set[str] | None = None
        if role_names is not None:
            target_role_names = {r.strip().lower() for r in role_names}
        elif self.cfg and getattr(self.cfg, "roles", None):
            flattened: list[str] = []
            for val in self.cfg.roles.values():
                if isinstance(val, list):
                    flattened.extend(val)
                elif isinstance(val, str):
                    flattened.append(val)
            target_role_names = {r.strip().lower() for r in flattened}

        try:
            res = self.client.get(f"rest/api/2/project/{project}/role")
        except Exception as e:
            msg = f"Failed to fetch roles for project {project}: {e}"
            raise JiraError(msg) from e

        if not isinstance(res, dict):
            msg = f"Expected dict from project role API for {project}, got {type(res)}"
            raise JiraError(msg)

        role_members: dict[str, list[str]] = {}
        for role_name, role_val in res.items():
            if (
                target_role_names is not None
                and role_name.strip().lower() not in target_role_names
            ):
                continue

            url_str = (
                role_val
                if isinstance(role_val, str)
                else (role_val.get("url") if isinstance(role_val, dict) else None)
            )
            if not url_str:
                continue

            if "/rest/" in url_str:
                path = "rest/" + url_str.split("/rest/", 1)[1]
            else:
                path = url_str

            try:
                role_data = self.client.get(path)
            except Exception as e:
                msg = f"Failed to fetch role data for {role_name} in {project}: {e}"
                raise JiraError(msg) from e

            if not isinstance(role_data, dict):
                continue

            usernames: list[str] = []
            for actor in role_data.get("actors", []):
                if not isinstance(actor, dict):
                    continue
                actor_type = actor.get("type")
                if actor_type == "atlassian-group-role-actor":
                    group_name = actor.get("name")
                    if group_name:
                        start_at = 0
                        max_results = 50
                        while True:
                            try:
                                group_res = self.client.get(
                                    "rest/api/2/group/member",
                                    params={
                                        "groupname": group_name,
                                        "startAt": start_at,
                                        "maxResults": max_results,
                                    },
                                )
                            except Exception as e:
                                msg = (
                                    f"Failed to expand group {group_name} "
                                    f"in project {project}: {e}"
                                )
                                raise JiraError(msg) from e

                            if not isinstance(group_res, dict):
                                msg = (
                                    f"Unexpected response expanding group "
                                    f"{group_name}: {group_res}"
                                )
                                raise JiraError(msg)

                            values = group_res.get("values", [])
                            for u in values:
                                if isinstance(u, dict):
                                    if u.get("active") is False:
                                        continue
                                    uname = u.get("name") or u.get("key")
                                    if uname and uname not in usernames:
                                        usernames.append(uname)

                            if group_res.get("isLast", True) or not values:
                                break
                            start_at += max_results
                else:
                    if actor.get("active") is False:
                        continue
                    name = actor.get("name")
                    if name and name not in usernames:
                        usernames.append(name)

            role_members[role_name] = usernames

        return role_members

    def get_fields(self) -> list[dict]:
        """Fetch all Jira field definitions."""
        res = self.client.get("rest/api/2/field")
        return res if isinstance(res, list) else []

    def get_subtasks(self, key: str) -> list[dict]:
        """Fetch all subtasks for an issue."""
        issue = self.get_issue(key)
        return list(issue.get("fields", {}).get("subtasks", []))

    def get_worklogs(self, key: str) -> list[dict]:
        """Fetch all worklogs for an issue via GET /rest/api/2/issue/{key}/worklog."""
        res = self.client.get(f"rest/api/2/issue/{key}/worklog")
        if isinstance(res, dict):
            return list(res.get("worklogs", []))
        if isinstance(res, list):
            return list(res)
        return []

    def get_comments(self, key: str) -> list[dict]:
        """Fetch all comments for an issue via GET /rest/api/2/issue/{key}/comment."""
        res = self.client.get(f"rest/api/2/issue/{key}/comment")
        if isinstance(res, dict):
            return list(res.get("comments", []))
        if isinstance(res, list):
            return list(res)
        return []

    def get_changelog(self, key: str) -> list[dict]:
        """Fetch full changelog histories for an issue."""
        res = self.client.get(f"rest/api/2/issue/{key}?expand=changelog")
        if isinstance(res, dict):
            changelog = res.get("changelog")
            if isinstance(changelog, dict):
                histories = list(changelog.get("histories", []))
                total = changelog.get("total")
                if isinstance(total, int) and total > len(histories):
                    logger.warning(
                        "Issue %s: changelog không đầy đủ (total: %s, returned: %s)",
                        key,
                        total,
                        len(histories),
                    )
                return histories
        return []

    def user_exists(self, username: str) -> bool:
        """Check if user exists via GET rest/api/2/user?username=."""
        try:
            res = self.client.get(f"rest/api/2/user?username={username}")
            return bool(res and isinstance(res, dict) and res.get("name"))
        except HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return False
            raise _handle_http_error(e) from e

    def field_allowed_values(
        self, project: str, issue_type: str, field_id: str
    ) -> list[str]:
        """Fetch allowed values for a field via createmeta."""
        try:
            res = self.client.get(
                "rest/api/2/issue/createmeta",
                params={
                    "projectKeys": project,
                    "issuetypeNames": issue_type,
                    "expand": "projects.issuetypes.fields",
                },
            )
        except HTTPError as e:
            raise _handle_http_error(e) from e

        if not isinstance(res, dict):
            return []
        projects = res.get("projects") or []
        for p in projects:
            for it in p.get("issuetypes") or []:
                fields = it.get("fields") or {}
                field_info = fields.get(field_id)
                if not field_info:
                    for f in fields.values():
                        if isinstance(f, dict) and f.get("name") == field_id:
                            field_info = f
                            break
                if field_info and isinstance(field_info, dict):
                    allowed = field_info.get("allowedValues") or []
                    vals: list[str] = []
                    for opt in allowed:
                        if isinstance(opt, dict):
                            val = opt.get("value") or opt.get("name")
                            if val:
                                vals.append(str(val))
                        elif isinstance(opt, str):
                            vals.append(opt)
                    return vals
        return []

    def set_issue_property(self, key: str, name: str, value: Any) -> None:
        """Set an issue property via PUT rest/api/2/issue/{key}/properties/{name}."""
        try:
            self.client.put(f"rest/api/2/issue/{key}/properties/{name}", data=value)
        except HTTPError as e:
            raise _handle_http_error(e) from e
