"""Tell agents when their /eggs skill is older than the published kit.

The kit's SKILL.md carries a line ``Kit version: 2026.11.12.1``. The agent
passes its own value to ``eggs_next(kit=...)``; the server reads the latest
SKILL.md from GitHub (cached) and, when the agent's copy is older, returns
``kit_update`` with the one-line update commands.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

GET_BASE = "https://raw.githubusercontent.com/eggstech/eggs-agent-kit/main"
DEFAULT_SKILL_URL = f"{GET_BASE}/skills/eggs/SKILL.md"
CACHE_SECONDS = 600
FETCH_TIMEOUT = 3.0

_VERSION_RE = re.compile(r"Kit version:\s*`?([0-9][0-9.]*[0-9])`?")

_cache: dict[str, Any] = {"at": 0.0, "value": None}
_lock = threading.Lock()


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """``"2026.11.12.1"`` → ``(2026, 11, 12, 1)``; None when unreadable."""
    if not text:
        return None
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts) if parts else None


def version_in(text: str) -> str | None:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def _fetch() -> str | None:
    url = os.getenv("EGGS_KIT_SKILL_URL", DEFAULT_SKILL_URL)
    if not url.startswith("https://"):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "eggs-mcp"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310
        return version_in(resp.read(200_000).decode("utf-8", "replace"))


def latest_version(now: float | None = None) -> str | None:
    """Latest published kit version, cached; None when GitHub is unreachable."""
    now = time.time() if now is None else now
    with _lock:
        if now - _cache["at"] < CACHE_SECONDS:
            return _cache["value"]
    try:
        value = _fetch()
    except Exception as exc:  # noqa: BLE001 - never slow or break eggs_next
        logger.info("Không đọc được phiên bản kit mới nhất: %s", exc)
        value = None
    with _lock:
        _cache["at"] = now
        _cache["value"] = value
    return value


def clear_cache() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["value"] = None


def update_info(current: str | None, latest: str | None) -> dict[str, Any] | None:
    """``kit_update`` payload when ``current`` is older than ``latest``."""
    newest = parse_version(latest)
    if newest is None:
        return None
    mine = parse_version(current)
    if current and current.strip() and mine is None:
        return None  # agent sent something that is not a version: do not nag
    if mine is not None and mine >= newest:
        return None
    return {
        "current": current,
        "latest": latest,
        "commands": {
            "macos_linux": f"curl -fsSL {GET_BASE}/get.sh | sh -s -- --update",
            "windows": f"$env:EGGS_KIT_UPDATE='1'; irm {GET_BASE}/get.ps1 | iex",
        },
    }
