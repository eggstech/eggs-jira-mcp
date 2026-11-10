from __future__ import annotations

import logging
from typing import Any

import pytest
import requests

from mcp_atlassian.eggs.chat import post_to_chat


def test_chat_splits_long_messages():
    # Long message > 4000 chars should be split into chunks <= 4000 chars
    lines = [f"Line {i:04d}: This is detailed content for the daily report item" for i in range(120)]
    full_text = "\n".join(lines)
    assert len(full_text) > 4000

    calls: list[dict[str, Any]] = []

    def mock_http_post(url: str, json: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        calls.append({"url": url, "json": json, "kwargs": kwargs})
        class FakeResponse:
            status_code = 200
        return FakeResponse()

    webhook = "https://chat.googleapis.com/v1/spaces/XYZ/messages?key=secret_key"
    post_to_chat(webhook, full_text, http_post=mock_http_post, project_key="RFID")

    assert len(calls) > 1
    for c in calls:
        assert c["url"] == webhook
        chunk = c["json"]["text"]
        assert len(chunk) <= 4000
    # All lines preserved across chunks
    combined = "\n".join(c["json"]["text"] for c in calls)
    for line in lines:
        assert line in combined


def test_post_to_chat_retries_on_5xx_and_does_not_log_webhook(caplog):
    webhook_secret = "AIzaSyD-SecretKey-999888"
    webhook = f"https://chat.googleapis.com/v1/spaces/SPACE123/messages?key={webhook_secret}&token=secret_token"
    calls = []

    def failing_then_succeeding_post(url: str, json: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        calls.append((url, json))
        if len(calls) == 1:
            resp = requests.Response()
            resp.status_code = 502
            err_msg = f"502 Bad Gateway for url: {url}"
            raise requests.exceptions.HTTPError(err_msg, response=resp)
        resp = requests.Response()
        resp.status_code = 200
        return resp

    with caplog.at_level(logging.INFO):
        post_to_chat(webhook, "Hello team", http_post=failing_then_succeeding_post, project_key="RFID")

    assert len(calls) == 2
    # Verify no log message contains the webhook URL or secret
    for rec in caplog.records:
        assert webhook_secret not in rec.message
        assert "secret_token" not in rec.message
        assert webhook not in rec.message
    # Verify project key was logged
    assert any("RFID" in rec.message for rec in caplog.records)


def test_post_to_chat_timeout_retries_and_raises(caplog):
    webhook_secret = "AIzaSyD-TimeoutSecret"
    webhook = f"https://chat.googleapis.com/v1/spaces/SPACE123/messages?key={webhook_secret}"
    calls = []

    def always_timeout_post(url: str, json: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        calls.append((url, json))
        err_msg = f"Connection timed out to {url}"
        raise requests.exceptions.Timeout(err_msg)

    with caplog.at_level(logging.INFO):
        with pytest.raises(requests.exceptions.Timeout):
            post_to_chat(webhook, "Timeout message", http_post=always_timeout_post, project_key="CP")

    assert len(calls) == 2  # 1 initial + 1 retry
    for rec in caplog.records:
        assert webhook_secret not in rec.message
        assert webhook not in rec.message
