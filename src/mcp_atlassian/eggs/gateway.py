from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .model import Worklog


class JiraError(Exception):
    """Base exception for Jira gateway errors."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        error_messages: list[str] | None = None,
        errors: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_messages = list(error_messages) if error_messages else []
        self.errors = dict(errors) if errors else {}


@runtime_checkable
class JiraGateway(Protocol):
    """Narrow protocol defining all Jira interactions required by eggs."""

    def search(
        self, jql: str, fields: list[str], *, expand_changelog: bool = False
    ) -> list[dict]:
        """Search issues using JQL with pagination support."""
        ...

    def get_issue(self, key: str) -> dict:
        """Fetch raw issue JSON by key."""
        ...

    def get_transitions(self, key: str) -> list[dict]:
        """Fetch available transitions for an issue: [{'id': ..., 'name': ...}]."""
        ...

    def transition(
        self,
        key: str,
        transition_name: str,
        fields: dict | None = None,
        comment: str | None = None,
    ) -> None:
        """Perform transition on issue by name.
        Raises JiraError if transition is unavailable.
        """
        ...

    def update_fields(
        self,
        key: str,
        fields: dict,
        *,
        notify_users: bool = True,
    ) -> None:
        """Update fields on an issue.
        When notify_users=False, includes query param notifyUsers=false.
        """
        ...

    def add_label(
        self,
        key: str,
        label: str,
        *,
        notify_users: bool = False,
    ) -> None:
        """Add a label to an issue using Jira update syntax."""
        ...

    def remove_label(
        self,
        key: str,
        label: str,
        *,
        notify_users: bool = False,
    ) -> None:
        """Remove a label from an issue using Jira update syntax."""
        ...

    def add_comment(self, key: str, body: str) -> None:
        """Add a comment to an issue."""
        ...

    def add_worklog(
        self,
        key: str,
        time_spent: str,
        comment: str,
        started: datetime | None = None,
    ) -> None:
        """Add a worklog to an issue."""
        ...

    def create_issue(self, fields: dict) -> str:
        """Create a new issue and return its key."""
        ...

    def link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        """Link two issues with a link type."""
        ...

    def worklogs_since(self, since: datetime) -> list[Worklog]:
        """Fetch worklogs updated since given datetime mapped to Issue models."""
        ...

    def components(self, project: str) -> list[str]:
        """Get component names for a project."""
        ...

    def current_user(self) -> str:
        """Get the current authenticated user's name/key."""
        ...

    def myself(self) -> dict:
        """Fetch info about current authenticated user:
        {'name': ..., 'displayName': ...}.
        """
        ...

    def project_role_members(
        self, project: str, role_names: list[str] | None = None
    ) -> dict[str, list[str]]:
        """Fetch role members mapped by Jira role name: {role_name: [usernames]}."""
        ...

    def get_fields(self) -> list[dict]:
        """Fetch all Jira field definitions."""
        ...

    def get_subtasks(self, key: str) -> list[dict]:
        """Fetch all subtasks for an issue."""
        ...

    def get_worklogs(self, key: str) -> list[dict]:
        """Fetch all worklogs for an issue via GET /rest/api/2/issue/{key}/worklog."""
        ...

    def user_exists(self, username: str) -> bool:
        """Check whether a Jira user exists."""
        ...

    def field_allowed_values(
        self, project: str, issue_type: str, field_id: str
    ) -> list[str]:
        """Fetch allowed values for a field in a project/issue type (createmeta)."""
        ...

    def set_issue_property(self, key: str, name: str, value: Any) -> None:
        """Set an issue property via PUT rest/api/2/issue/{key}/properties/{name}."""
        ...
