from __future__ import annotations

from datetime import datetime
from typing import Any

from .gateway import JiraError, JiraGateway
from .model import Worklog


class AtlassianGateway(JiraGateway):
    """Adapter implementing JiraGateway on top of the upstream Jira client."""

    def __init__(self, client: Any) -> None:
        self.client = client

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
        since_ms = int(since.timestamp() * 1000)
        updated_res = self.client.get(f"rest/api/2/worklog/updated?since={since_ms}")
        values = updated_res.get("values", []) if isinstance(updated_res, dict) else []
        ids = [v["worklogId"] for v in values if "worklogId" in v]
        if not ids:
            return []

        worklogs_data = self.client.post("rest/api/2/worklog/list", data={"ids": ids})
        if not isinstance(worklogs_data, list):
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
            issue_key = (
                item.get("issueKey")
                or (item.get("issue") and item["issue"].get("key"))
                or id_to_key_map.get(str(item.get("issueId")))
                or str(item.get("issueId"))
            )
            author_data = item.get("author") or {}
            author = (
                author_data.get("name")
                or author_data.get("key")
                or author_data.get("displayName")
                or "unknown"
            )
            started_str = item.get("started")
            started_dt = (
                datetime.fromisoformat(started_str)
                if started_str
                else datetime.min
            )
            seconds = item.get("timeSpentSeconds") or 0

            result.append(
                Worklog(
                    issue_key=issue_key,
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
