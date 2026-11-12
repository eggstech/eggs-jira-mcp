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
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from .gateway import JiraGateway

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


def restore_ticket(token: str, ticket: UploadTicket) -> None:
    """Put back a ticket whose upload was rejected before reaching Jira."""
    with _lock:
        _tickets[token] = ticket


def take_ticket(token: str, now: float | None = None) -> UploadTicket | None:
    """Remove and return a valid ticket (one use only)."""
    now = time.time() if now is None else now
    with _lock:
        ticket = _tickets.pop(token, None)
    if ticket is None or ticket.expires_at <= now:
        return None
    return ticket


_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f|!\[\]{}^]")


def safe_filename(name: str | None) -> str:
    """Base name without folders, control chars or Jira wiki markup chars."""
    base = PurePath((name or "").replace("\\", "/")).name
    base = " ".join(_UNSAFE_NAME_CHARS.sub("_", base).split())
    return base[:200] or "file"


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


def validate_files(files: list[tuple[str, bytes]]) -> str | None:
    """Return why the upload is refused, or None when it can go to Jira."""
    if not files:
        return "Không có file nào (dùng -F file=@đường_dẫn)."
    if len(files) > MAX_FILES:
        return f"Tối đa {MAX_FILES} file mỗi lần."
    empty = [n for n, c in files if not c]
    if empty:
        return f"File rỗng: {', '.join(empty)}."
    too_big = [n for n, c in files if len(c) > MAX_FILE_BYTES]
    if too_big:
        return f"File quá lớn (tối đa 20 MB): {', '.join(too_big)}."
    return None


def attach_files(
    gateway_factory: Callable[[], JiraGateway],
    ticket: UploadTicket,
    files: list[tuple[str, bytes]],
) -> dict[str, Any]:
    """Attach files (and the optional comment) to the ticket's issue."""
    refused = validate_files(files)
    if refused:
        return {"ok": False, "message": refused, "status": 400}
    attached: list[str] = []
    body: str | None = None
    try:
        gateway = gateway_factory()
        for name, content in files:
            gateway.add_attachment(ticket.key, name, content)
            attached.append(name)
        body = comment_with_files(ticket.comment, attached)
        if body:
            gateway.add_comment(ticket.key, body)
    except Exception as exc:  # noqa: BLE001 - report partial progress, never a 500
        status = getattr(exc, "status_code", None) or 502
        logger.error("Đính kèm %s lỗi (%s): %s", ticket.key, status, exc)
        done = f" Đã đính kèm: {', '.join(attached)}." if attached else ""
        what = "ghi comment" if body else "đính kèm"
        reason = {
            401: "token hết hạn hoặc sai",
            403: "không có quyền",
            404: "không thấy issue",
            413: "file vượt giới hạn của Jira",
        }.get(status, "lỗi Jira")
        return {
            "ok": False,
            "message": f"Jira từ chối {what} vào {ticket.key} ({reason}).{done}",
            "attached": attached,
            "status": status if 400 <= status < 600 else 502,
        }
    msg = f"Đã đính kèm {len(attached)} file vào {ticket.key}"
    msg += " và ghi comment." if body else "."
    return {"ok": True, "message": msg, "attached": attached, "status": 200}


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

    def reply(result: dict[str, Any]) -> Any:
        status = int(result.pop("status", 200 if result.get("ok") else 400))
        return JSONResponse(result, status_code=status)

    token = request.path_params.get("token", "")
    ticket = take_ticket(token)
    if ticket is None:
        return reply(
            {
                "ok": False,
                "message": "Link đính kèm không hợp lệ hoặc đã hết hạn; "
                "gọi lại eggs_attach_url.",
                "status": 403,
            }
        )
    limit = MAX_FILES * MAX_FILE_BYTES + 1024 * 1024
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        restore_ticket(token, ticket)
        return reply(
            {"ok": False, "message": "Tổng dung lượng quá lớn.", "status": 413}
        )
    files: list[tuple[str, bytes]] = []
    try:
        form = await request.form(max_files=MAX_FILES, max_fields=20)
        for item in form.getlist("file"):
            if not hasattr(item, "read"):
                continue
            size = getattr(item, "size", None)
            if size is not None and size > MAX_FILE_BYTES:
                restore_ticket(token, ticket)
                return reply(
                    {
                        "ok": False,
                        "message": "File quá lớn (tối đa 20 MB): "
                        f"{safe_filename(item.filename)}.",
                        "status": 413,
                    }
                )
            files.append((safe_filename(item.filename), await item.read()))
    except Exception as exc:  # noqa: BLE001 - malformed multipart
        restore_ticket(token, ticket)
        logger.warning("Upload form lỗi: %s", exc)
        return reply(
            {
                "ok": False,
                "message": "Dữ liệu tải lên không hợp lệ (dùng -F file=@đường_dẫn, "
                f"tối đa {MAX_FILES} file).",
                "status": 400,
            }
        )
    refused = validate_files(files)
    if refused:
        restore_ticket(token, ticket)
        return reply({"ok": False, "message": refused, "status": 400})
    result = await asyncio.to_thread(
        attach_files, lambda: _factory(ticket.user_token), ticket, files
    )
    return reply(result)
