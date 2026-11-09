from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from .gateway import JiraError, JiraGateway
from .mapping import parse_jira_dt
from .model import Worklog

logger = logging.getLogger(__name__)


class AtlassianGateway(JiraGateway):
    """Adapter implementing JiraGateway on top of the upstream Jira client."""

    def __init__(self, client: Any, cfg: Any = None) -> None:
        self.client = client
        self.cfg = cfg

    def search(
        self, jql: str, fields: list[str], expand_changelog: bool = False
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
            raise JiraError(f"Failed to fetch issue {key}")
        return res

    def get_transitions(self, key: str) -> list[dict]:
        """Fetch available transitions for an issue: [{'id': ..., 'name': ...}]."""
        res = self.client.get(f"rest/api/2/issue/{key}/transitions")
        transitions = res.get("transitions", []) if isinstance(res, dict) else []
        return [{"id": str(t["id"]), "name": t["name"]} for t in transitions if "id" in t and "name" in t]

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
            raise JiraError(f"Transition '{transition_name}' not available for {key}")

        payload: dict[str, Any] = {"transition": {"id": matched["id"]}}
        if fields:
            payload["fields"] = fields
        if comment:
            payload["update"] = {"comment": [{"add": {"body": comment}}]}

        self.client.post(f"rest/api/2/issue/{key}/transitions", data=payload)

    def update_fields(
        self,
        key: str,
        fields: dict,
        override_screen_security: bool = False,
    ) -> None:
        """Update fields on an issue."""
        params = (
            {"overrideScreenSecurity": "true", "notifyUsers": "false"}
            if override_screen_security
            else None
        )
        self.client.put(
            f"rest/api/2/issue/{key}",
            data={"fields": fields},
            params=params,
        )

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
            raise JiraError(f"Failed to create issue: {res}")
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
                    "Skipping worklog %s: issueId %s could not be resolved to an issue key",
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

    def current_user(self) -> str:
        """Get the current authenticated user's name/key."""
        res = self.client.get("rest/api/2/myself")
        if isinstance(res, dict):
            return res.get("name") or res.get("key") or res.get("displayName") or ""
        return ""

    def myself(self) -> dict:
        """Fetch info about the current authenticated user: {'name': ..., 'displayName': ...}."""
        res = self.client.get("rest/api/2/myself")
        if isinstance(res, dict):
            return res
        return {}

    def project_role_members(self, project: str) -> dict[str, list[str]]:
        """Fetch role members mapped by Jira role name: {role_name: [usernames]}."""
        res = self.client.get(f"rest/api/2/project/{project}/role")
        if not isinstance(res, dict):
            return {}

        role_members: dict[str, list[str]] = {}
        for role_name, role_val in res.items():
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
                logger.warning("Failed to fetch role data for %s: %s", path, e)
                continue

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
                        try:
                            group_res = self.client.get(
                                f"rest/api/2/group/member?groupname={group_name}"
                            )
                            if isinstance(group_res, dict) and "values" in group_res:
                                for u in group_res["values"]:
                                    if isinstance(u, dict) and u.get("name"):
                                        usernames.append(u["name"])
                                continue
                        except Exception:
                            pass
                name = actor.get("name")
                if name:
                    usernames.append(name)

            role_members[role_name] = usernames

        return role_members
