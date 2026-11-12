"""Add a link and the issue type for every issue key an eggs tool returns.

Agents show tool results to people who then had to copy a key and search
Jira by hand. Every JSON object an ``eggs_*`` tool returns gets an
``issue_links`` map: ``{"EGG-12": {"type": "Bug", "url": ".../browse/EGG-12"}}``
covering each key of a configured project that appears anywhere in it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable

from .gateway import JiraGateway

logger = logging.getLogger(__name__)

MAX_TYPED_KEYS = 200
_CACHE_TTL_SECONDS = 3600
_CACHE_MAX = 5000
_LOOKUP_CHUNK = 100
_SINGLE_READ_LIMIT = 20

_KEY_RE = re.compile(r"(?<![A-Za-z0-9_\-/])([A-Z][A-Z0-9_]+-\d+)(?![A-Za-z0-9_])")

_type_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def clear_type_cache() -> None:
    with _cache_lock:
        _type_cache.clear()


def find_issue_keys(text: str, project_keys: Iterable[str]) -> list[str]:
    """Issue keys of known projects in order of first appearance."""
    projects = {p.upper() for p in project_keys}
    seen: dict[str, None] = {}
    for match in _KEY_RE.finditer(text):
        key = match.group(1)
        if key.rsplit("-", 1)[0] in projects:
            seen.setdefault(key, None)
    return list(seen)


def browse_url(jira_url: str, key: str) -> str:
    return f"{jira_url.rstrip('/')}/browse/{key}"


def lookup_types(
    gateway: JiraGateway, keys: list[str], now: float | None = None
) -> dict[str, str]:
    """Issue type names for keys, from cache or one JQL search per 100 keys."""
    now = time.time() if now is None else now
    found: dict[str, str] = {}
    missing: list[str] = []
    with _cache_lock:
        for key in keys:
            hit = _type_cache.get(key)
            if hit and hit[1] > now:
                found[key] = hit[0]
            else:
                missing.append(key)
    for i in range(0, len(missing), _LOOKUP_CHUNK):
        chunk = missing[i : i + _LOOKUP_CHUNK]
        jql = f"key in ({', '.join(chunk)})"
        try:
            rows = gateway.search(jql, ["issuetype"])
        except Exception as exc:  # noqa: BLE001 - one bad key fails the whole JQL
            logger.info("Tra loại issue lỗi, tra từng issue: %s", exc)
            rows = []
            for key in chunk[:_SINGLE_READ_LIMIT]:
                try:
                    rows.append(gateway.get_issue(key))
                except Exception as one:  # noqa: BLE001 - deleted or not visible
                    logger.info("Bỏ qua %s: %s", key, one)
        for row in rows:
            key = str(row.get("key") or "")
            name = ((row.get("fields") or {}).get("issuetype") or {}).get("name")
            if key and name:
                found[key] = name
    with _cache_lock:
        if len(_type_cache) > _CACHE_MAX:
            _type_cache.clear()
        for key in missing:
            if key in found:
                _type_cache[key] = (found[key], now + _CACHE_TTL_SECONDS)
    return found


def enrich_result(
    result: str,
    *,
    jira_url: str,
    project_keys: Iterable[str],
    gateway_getter: Callable[[], JiraGateway] | None,
) -> str:
    """Return the tool result with an ``issue_links`` map added; unchanged if none."""
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return result
    if not isinstance(data, dict):
        return result
    data.pop("issue_links", None)
    keys = find_issue_keys(json.dumps(data, ensure_ascii=False), project_keys)
    if not keys:
        return result
    types: dict[str, str] = {}
    if gateway_getter is not None:
        try:
            types = lookup_types(gateway_getter(), keys[:MAX_TYPED_KEYS])
        except Exception as exc:  # noqa: BLE001 - links matter more than types
            logger.info("Không tra được loại issue: %s", exc)
    data["issue_links"] = {
        key: {"type": types.get(key), "url": browse_url(jira_url, key)} for key in keys
    }
    indent = 2 if "\n" in result else None
    return json.dumps(data, ensure_ascii=False, indent=indent)
