"""One-time upload links so an agent can attach local files to a Jira issue.

The MCP server cannot read files on the user's machine, and sending a file
as base64 inside a tool call costs far too many tokens. ``eggs_attach_url``
creates a short-lived link bound to one user and one issue; the agent then
runs ``curl -F file=@path <link>``. The file is attached with the user's own
token, which stays in server memory only until the link is used or expires.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from .gateway import JiraError, JiraGateway

logger = logging.getLogger(__name__)

UPLOAD_TTL_SECONDS = 600
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


@dataclass
class UploadTicket:
    user_token: str
    username: str
    key: str
    comment: str | None
    expires_at: float


_tickets: dict[str, UploadTicket] = {}
_lock = threading.Lock()


def public_base_url() -> str:
    return os.getenv("EGGS_PUBLIC_URL", "https://jira.eggstech.io.vn/eggs-mcp").rstrip(
        "/"
    )


def create_ticket(
    *,
    user_token: str,
    username: str,
    key: str,
    comment: str | None,
    now: float | None = None,
) -> str:
    """Store a one-time upload ticket and return its id."""
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(24)
    with _lock:
        for tid in [t for t, v in _tickets.items() if v.expires_at <= now]:
            del _tickets[tid]
        _tickets[token] = UploadTicket(
            user_token=user_token,
            username=username,
            key=key.upper(),
            comment=(comment or "").strip() or None,
            expires_at=now + UPLOAD_TTL_SECONDS,
        )
    return token


def take_ticket(token: str, now: float | None = None) -> UploadTicket | None:
    """Remove and return a valid ticket (one use only)."""
    now = time.time() if now is None else now
    with _lock:
        ticket = _tickets.pop(token, None)
    if ticket is None or ticket.expires_at <= now:
        return None
    return ticket


def safe_filename(name: str | None) -> str:
    base = PurePath((name or "").replace("\\", "/")).name.strip()
    return base or "file"


def comment_with_files(comment: str | None, filenames: list[str]) -> str | None:
    """Comment text that shows images inline and links other files."""
    if not comment:
        return None
    refs = []
    for name in filenames:
        if name.lower().endswith(IMAGE_SUFFIXES):
            refs.append(f"!{name}|thumbnail!")
        else:
            refs.append(f"[^{name}]")
    return comment + ("\n\n" + "\n".join(refs) if refs else "")


def attach_files(
    gateway: JiraGateway,
    ticket: UploadTicket,
    files: list[tuple[str, bytes]],
) -> dict[str, Any]:
    """Attach files (and the optional comment) to the ticket's issue."""
    if not files:
        return {"ok": False, "message": "Không có file nào (dùng -F file=@đường_dẫn)."}
    if len(files) > MAX_FILES:
        return {"ok": False, "message": f"Tối đa {MAX_FILES} file mỗi lần."}
    too_big = [n for n, c in files if len(c) > MAX_FILE_BYTES]
    if too_big:
        return {
            "ok": False,
            "message": f"File quá lớn (tối đa 20 MB): {', '.join(too_big)}.",
        }
    attached: list[str] = []
    try:
        for name, content in files:
            gateway.add_attachment(ticket.key, name, content)
            attached.append(name)
        body = comment_with_files(ticket.comment, attached)
        if body:
            gateway.add_comment(ticket.key, body)
    except JiraError as exc:
        logger.error("Đính kèm %s lỗi: %s", ticket.key, exc)
        done = f" Đã đính kèm: {', '.join(attached)}." if attached else ""
        return {
            "ok": False,
            "message": f"Jira từ chối khi đính kèm vào {ticket.key}.{done}",
            "attached": attached,
        }
    msg = f"Đã đính kèm {len(attached)} file vào {ticket.key}"
    msg += " và ghi comment." if body else "."
    return {"ok": True, "message": msg, "attached": attached}


GatewayFactory = Callable[[str], JiraGateway]


def default_gateway_factory(user_token: str) -> JiraGateway:
    from mcp_atlassian.jira import JiraConfig, JiraFetcher

    from .config import load_config
    from .gateway_atlassian import AtlassianGateway

    cfg = load_config()
    url = os.getenv("JIRA_URL", cfg.jira_url if cfg else "")
    fetcher = JiraFetcher(
        JiraConfig(url=url, auth_type="pat", personal_token=user_token)
    )
    return AtlassianGateway(fetcher.jira, cfg=cfg)


_factory: GatewayFactory = default_gateway_factory


def set_gateway_factory(factory: GatewayFactory | None) -> None:
    global _factory
    _factory = factory or default_gateway_factory


async def handle_upload(request: Any) -> Any:
    """Starlette handler for POST /upload/{token} (multipart, field "file")."""
    import asyncio

    from starlette.responses import JSONResponse

    ticket = take_ticket(request.path_params.get("token", ""))
    if ticket is None:
        return JSONResponse(
            {
                "ok": False,
                "message": "Link đính kèm không hợp lệ hoặc đã hết hạn; "
                "gọi lại eggs_attach_url.",
            },
            status_code=403,
        )
    form = await request.form(max_files=MAX_FILES + 1)
    files: list[tuple[str, bytes]] = []
    for item in form.getlist("file"):
        if hasattr(item, "read"):
            files.append((safe_filename(item.filename), await item.read()))
    result = await asyncio.to_thread(
        attach_files, _factory(ticket.user_token), ticket, files
    )
    return JSONResponse(result, status_code=200 if result["ok"] else 400)
