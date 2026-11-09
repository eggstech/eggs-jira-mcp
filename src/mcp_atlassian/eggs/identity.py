from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time

from .config import EggsConfig
from .gateway import JiraGateway


@dataclass(frozen=True)
class Identity:
    username: str
    display_name: str
    roles: dict[str, set[str]]  # project key -> set of logical roles (e.g. {"dev", "qc"})
    is_lead: bool


class IdentityCache:
    """Cache for user identities and role memberships with TTL."""

    def __init__(
        self,
        ttl_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._identity: tuple[Identity, float] | None = None
        self._roles: dict[str, tuple[dict[str, set[str]], float]] = {}
        self._project_roles: dict[str, tuple[dict[str, list[str]], float]] = {}

    def get_identity(self) -> Identity | None:
        if self._identity is None:
            return None
        identity, ts = self._identity
        if self.clock() - ts < self.ttl_seconds:
            return identity
        self._identity = None
        return None

    def set_identity(self, identity: Identity) -> None:
        now = self.clock()
        self._identity = (identity, now)
        self._roles[identity.username] = (identity.roles, now)

    def get_user_roles(self, username: str) -> dict[str, set[str]] | None:
        if username not in self._roles:
            return None
        roles, ts = self._roles[username]
        if self.clock() - ts < self.ttl_seconds:
            return roles
        del self._roles[username]
        return None

    def set_user_roles(self, username: str, roles: dict[str, set[str]]) -> None:
        self._roles[username] = (roles, self.clock())

    def get_project_role_members(self, project: str) -> dict[str, list[str]] | None:
        if project not in self._project_roles:
            return None
        members, ts = self._project_roles[project]
        if self.clock() - ts < self.ttl_seconds:
            return members
        del self._project_roles[project]
        return None

    def set_project_role_members(self, project: str, members: dict[str, list[str]]) -> None:
        self._project_roles[project] = (members, self.clock())

    def clear(self) -> None:
        self._identity = None
        self._roles.clear()
        self._project_roles.clear()


def resolve_identity(
    gateway: JiraGateway,
    cfg: EggsConfig,
    cache: IdentityCache | None = None,
) -> Identity:
    """Resolve the current user's Identity and roles across active projects."""
    if cache is not None:
        cached = cache.get_identity()
        if cached is not None:
            return cached

    me = gateway.myself()
    username = me.get("name") or me.get("key") or ""
    display_name = me.get("displayName") or username
    is_lead = username == cfg.lead_user

    roles: dict[str, set[str]] = {}
    cached_roles = cache.get_user_roles(username) if cache is not None else None
    if cached_roles is not None:
        roles = cached_roles
    else:
        # Check active projects only (non-frozen)
        active_projects = {k: p for k, p in cfg.projects.items() if not p.frozen}
        for proj_key in sorted(active_projects.keys()):
            members: dict[str, list[str]] | None = None
            if cache is not None:
                members = cache.get_project_role_members(proj_key)
            if members is None:
                members = gateway.project_role_members(proj_key)
                if cache is not None:
                    cache.set_project_role_members(proj_key, members)

            user_proj_roles: set[str] = set()
            norm_user = username.strip().lower()
            for logical_role, jira_role_name in cfg.roles.items():
                matched_members: list[str] = []
                for r_name, u_list in members.items():
                    if r_name.strip().lower() == jira_role_name.strip().lower():
                        matched_members = u_list
                        break
                if any(u.strip().lower() == norm_user for u in matched_members):
                    user_proj_roles.add(logical_role)

            if user_proj_roles:
                roles[proj_key] = user_proj_roles

        if cache is not None:
            cache.set_user_roles(username, roles)

    identity = Identity(
        username=username,
        display_name=display_name,
        roles=roles,
        is_lead=is_lead,
    )
    if cache is not None:
        cache.set_identity(identity)

    return identity
