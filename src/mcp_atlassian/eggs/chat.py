from __future__ import annotations

import logging
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
    if resp.status_code >= 500:
        resp.raise_for_status()
    return resp


def _sanitize_error(err: Exception, webhook: str) -> str:
    msg = str(err)
    if webhook in msg:
        msg = msg.replace(webhook, "[REDACTED_WEBHOOK]")
    return msg


def post_to_chat(
    webhook: str,
    text: str,
    http_post: Callable[..., Any] | None = None,
    project_key: str | None = None,
) -> None:
    """Post a text message to a Google Chat incoming webhook.

    - Splits messages exceeding 4000 characters.
    - Retries once on HTTP 5xx or timeout.
    - Never logs the webhook URL; logs only the project key.
    """
    post_fn = http_post or _default_http_post
    chunks = split_message(text, max_chars=4000)
    target_desc = f"project {project_key}" if project_key else "lead"

    for i, chunk in enumerate(chunks, 1):
        payload = {"text": chunk}
        attempts = 0
        max_attempts = 2

        while attempts < max_attempts:
            attempts += 1
            try:
                try:
                    resp = post_fn(webhook, json=payload)
                except TypeError:
                    resp = post_fn(webhook, payload)

                # Check if resp returned a status_code >= 500 without raising
                if hasattr(resp, "status_code") and resp.status_code >= 500:
                    msg = f"HTTP {resp.status_code} server error"
                    raise requests.exceptions.HTTPError(msg, response=resp)

                # Success
                break
            except (requests.exceptions.Timeout, requests.exceptions.HTTPError) as e:
                # Check if it's 5xx or Timeout
                is_5xx = isinstance(e, requests.exceptions.HTTPError) and (
                    e.response is not None and e.response.status_code >= 500
                )
                is_timeout = isinstance(e, requests.exceptions.Timeout)

                if (is_5xx or is_timeout) and attempts < max_attempts:
                    logger.warning(
                        "Chat post failed for %s (chunk %d/%d, attempt %d/%d): %s."
                        " Retrying...",
                        target_desc,
                        i,
                        len(chunks),
                        attempts,
                        max_attempts,
                        _sanitize_error(e, webhook),
                    )
                    continue

                logger.error(
                    "Chat post permanently failed for %s (chunk %d/%d): %s",
                    target_desc,
                    i,
                    len(chunks),
                    _sanitize_error(e, webhook),
                )
                raise
            except Exception as e:
                logger.error(
                    "Chat post failed with unexpected error for %s: %s",
                    target_desc,
                    _sanitize_error(e, webhook),
                )
                raise
