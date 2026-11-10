from __future__ import annotations

import logging
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import requests

logger = logging.getLogger(__name__)


def split_message(text: str, max_chars: int = 4000) -> list[str]:
    """Split text into chunks of at most `max_chars` characters."""
    if len(text) <= max_chars:
        return [text]

    lines = text.split("\n")
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + (1 if current_chunk else 0)
        if current_chunk and current_len + line_len > max_chars:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # If an individual chunk still exceeds max_chars (e.g. single long line)
    final_chunks: list[str] = []
    for c in chunks:
        if len(c) > max_chars:
            for i in range(0, len(c), max_chars):
                final_chunks.append(c[i : i + max_chars])
        else:
            final_chunks.append(c)

    return final_chunks


def _default_http_post(url: str, json: dict[str, Any]) -> Any:
    resp = requests.post(url, json=json, timeout=10)
    if resp.status_code >= 400:
        resp.raise_for_status()
    return resp


def _sanitize_error(err: Exception, webhook: str) -> str:
    msg = str(err)
    if webhook in msg:
        msg = msg.replace(webhook, "[REDACTED_WEBHOOK]")

    try:
        parsed = urllib.parse.urlsplit(webhook)
        # Redact anything after the host
        if parsed.netloc:
            idx = webhook.find(parsed.netloc)
            if idx != -1:
                after_host = webhook[idx + len(parsed.netloc) :]
                if after_host and after_host in msg:
                    msg = msg.replace(after_host, "[REDACTED_PATH_QUERY]")
        if parsed.path and parsed.path in msg:
            msg = msg.replace(parsed.path, "[REDACTED_PATH]")
        if parsed.query and parsed.query in msg:
            msg = msg.replace(parsed.query, "[REDACTED_QUERY]")
        for part in parsed.query.split("&"):
            if part and part in msg:
                msg = msg.replace(part, "[REDACTED]")
    except Exception:  # noqa: BLE001, S110
        pass

    # Generic redaction for any residual query tokens/keys or Google Chat space paths
    msg = re.sub(r"(?:key|token)=[^&\s\"']+", "[REDACTED_SECRET]", msg)
    msg = re.sub(r"/v1/spaces[^\s\"']*", "[REDACTED_PATH]", msg)
    return msg


def post_to_chat(
    webhook: str,
    text: str,
    http_post: Callable[..., Any] | None = None,
    project_key: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Post a text message to a Google Chat incoming webhook.

    - Splits messages exceeding 4000 characters.
    - Retries once on HTTP 5xx or timeout (with 1s backoff).
    - 4xx fails immediately without retry.
    - Never leaks the webhook URL or secret; logs only project key and HTTP status.
    - Raises RuntimeError without chaining causes (`from None`).
    """
    post_fn = http_post or _default_http_post
    chunks = split_message(text, max_chars=4000)
    target = project_key or "lead"

    for i, chunk in enumerate(chunks, 1):
        payload = {"text": chunk}
        attempts = 0
        max_attempts = 2

        while attempts < max_attempts:
            attempts += 1
            try:
                resp = post_fn(webhook, json=payload)
                if hasattr(resp, "status_code"):
                    if 400 <= resp.status_code < 500:
                        msg = f"HTTP {resp.status_code} client error"
                        raise requests.exceptions.HTTPError(msg, response=resp)
                    if resp.status_code >= 500:
                        msg = f"HTTP {resp.status_code} server error"
                        raise requests.exceptions.HTTPError(msg, response=resp)
                # Success
                break
            except Exception as e:  # noqa: BLE001
                status: int | None = None
                if hasattr(e, "response") and getattr(e, "response", None) is not None:
                    status = getattr(e.response, "status_code", None)

                # 4xx client error: no retry, log status + project key (no URL)
                if status is not None and 400 <= status < 500:
                    logger.error(
                        "Chat post failed for %s (chunk %d/%d) with HTTP %d",
                        target,
                        i,
                        len(chunks),
                        status,
                    )
                    err_msg = (
                        f"Gửi Google Chat thất bại cho {target} "
                        f"(HTTP {status} | {type(e).__name__})"
                    )
                    raise RuntimeError(err_msg) from None

                # Retryable: 5xx, timeout, or connection error
                is_5xx = status is not None and status >= 500
                is_timeout = isinstance(e, requests.exceptions.Timeout)
                is_conn = isinstance(e, requests.exceptions.ConnectionError)

                if (is_5xx or is_timeout or is_conn) and attempts < max_attempts:
                    logger.warning(
                        "Chat post failed for %s (chunk %d/%d, attempt %d/%d): "
                        "%s. Retrying...",
                        target,
                        i,
                        len(chunks),
                        attempts,
                        max_attempts,
                        _sanitize_error(e, webhook),
                    )
                    sleep(1)
                    continue

                # Permanent failure
                logger.error(
                    "Chat post permanently failed for %s (chunk %d/%d): %s",
                    target,
                    i,
                    len(chunks),
                    _sanitize_error(e, webhook),
                )
                err_msg = (
                    f"Gửi Google Chat thất bại cho {target} "
                    f"(HTTP {status} | {type(e).__name__})"
                )
                raise RuntimeError(err_msg) from None
