from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import EggsConfig
from .gateway import JiraGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    username: str
    display_name: str
    roles: dict[
        str, set[str]
    ]  # project key -> set of logical roles (e.g. {"dev", "qc"})
    is_lead: bool
    errors: list[str] = field(default_factory=list)


class IdentityCache:
    """Cache for project role memberships with TTL."""

    def __init__(
        self,
        ttl_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._project_roles: dict[str, tuple[dict[str, list[str]], float]] = {}

    def get_project_role_members(self, project: str) -> dict[str, list[str]] | None:
        if project not in self._project_roles:
            return None
        members, ts = self._project_roles[project]
        if self.clock() - ts < self.ttl_seconds:
            return members
        self._project_roles.pop(project, None)
        return None

    def set_project_role_members(
        self, project: str, members: dict[str, list[str]]
    ) -> None:
        self._project_roles[project] = (members, self.clock())

    def clear(self) -> None:
        self._project_roles.clear()


def resolve_identity(
    user_gateway: JiraGateway,
    roles_reader: JiraGateway,
    cfg: EggsConfig,
    cache: IdentityCache | None = None,
) -> Identity:
    """Resolve the current user's Identity and roles across active projects."""
    try:
        me = user_gateway.myself()
    except Exception as e:
        raise RuntimeError(
            "PAT không hợp lệ hoặc đã hết hạn — tạo PAT mới trong Jira → Profile"
            " → Personal Access Tokens"
        ) from e

    username = me.get("name") or me.get("key") or ""
    display_name = me.get("displayName") or username
    norm = username.strip().lower()
    is_lead = norm == cfg.lead_user.strip().lower() or norm in cfg.lead_users

    # Bot user is ignored by identity
    if username.strip().lower() == cfg.bot_user.strip().lower():
        return Identity(
            username=username,
            display_name=display_name,
            roles={},
            is_lead=False,
            errors=[],
        )

    roles: dict[str, set[str]] = {}
    errors: list[str] = []

    # Flatten configured Jira role names
    configured_role_names: list[str] = []
    for jira_role_names in cfg.roles.values():
        if isinstance(jira_role_names, list):
            configured_role_names.extend(jira_role_names)
        elif isinstance(jira_role_names, str):
            configured_role_names.append(jira_role_names)

    # Check active projects only (non-frozen)
    active_projects = {k: p for k, p in cfg.projects.items() if not p.frozen}
    norm_user = username.strip().lower()

    for proj_key in sorted(active_projects.keys()):
        members: dict[str, list[str]] | None = None
        if cache is not None:
            members = cache.get_project_role_members(proj_key)

        if members is None:
            try:
                members = roles_reader.project_role_members(
                    proj_key, role_names=configured_role_names
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Failed to read role members for project %s: %s", proj_key, e
                )
                errors.append(proj_key)
                continue

            if cache is not None:
                cache.set_project_role_members(proj_key, members)

            # Log warning when a configured role name does not exist in project
            norm_members_keys = {k.strip().lower() for k in members.keys()}
            for target_name in configured_role_names:
                if target_name.strip().lower() not in norm_members_keys:
                    logger.warning(
                        "Configured role %r does not exist in project %s",
                        target_name,
                        proj_key,
                    )

        user_proj_roles: set[str] = set()
        for logical_role, jira_role_names in cfg.roles.items():
            names = (
                jira_role_names
                if isinstance(jira_role_names, list)
                else [jira_role_names]
            )
            matched_members: list[str] = []
            for r_name, u_list in members.items():
                for target_name in names:
                    if r_name.strip().lower() == target_name.strip().lower():
                        matched_members.extend(u_list)
            if any(u.strip().lower() == norm_user for u in matched_members):
                user_proj_roles.add(logical_role)

        if user_proj_roles:
            roles[proj_key] = user_proj_roles

    return Identity(
        username=username,
        display_name=display_name,
        roles=roles,
        is_lead=is_lead,
        errors=errors,
    )
