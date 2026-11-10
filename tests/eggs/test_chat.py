from __future__ import annotations

import logging
from typing import Any

import pytest
import requests

from mcp_atlassian.eggs.chat import _sanitize_error, post_to_chat


def test_chat_splits_long_messages():
    # Long message > 4000 chars should be split into chunks <= 4000 chars
    lines = [
        f"Line {i:04d}: This is detailed content for the daily report item"
        for i in range(120)
    ]
    full_text = "\n".join(lines)
    assert len(full_text) > 4000

    calls: list[dict[str, Any]] = []

    def mock_http_post(
        url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
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
    sleeps = []

    def failing_then_succeeding_post(
        url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
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
        post_to_chat(
            webhook,
            "Hello team",
            http_post=failing_then_succeeding_post,
            project_key="RFID",
            sleep=lambda s: sleeps.append(s),
        )

    assert len(calls) == 2
    assert sleeps == [1]
    # Verify no log message contains the webhook URL or secret
    for rec in caplog.records:
        assert webhook_secret not in rec.message
        assert "secret_token" not in rec.message
        assert webhook not in rec.message
        assert "/v1/spaces" not in rec.message
    # Verify project key was logged
    assert any("RFID" in rec.message for rec in caplog.records)


def test_post_to_chat_5xx_twice_raises_runtime_error_and_does_not_leak(caplog):
    webhook_secret = "AIzaSyD-SecretKey-5xx"
    webhook = f"https://chat.googleapis.com/v1/spaces/SPACE123/messages?key={webhook_secret}&token=token5xx"
    calls = []
    sleeps = []

    def always_500_post(
        url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        calls.append((url, json))
        resp = requests.Response()
        resp.status_code = 500
        err_msg = f"500 Internal Server Error for url: {url}"
        raise requests.exceptions.HTTPError(err_msg, response=resp)

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError) as exc_info:
            post_to_chat(
                webhook,
                "5xx message",
                http_post=always_500_post,
                project_key="RFID",
                sleep=lambda s: sleeps.append(s),
            )

    assert len(calls) == 2
    assert sleeps == [1]
    exc = exc_info.value
    assert str(exc) == "Gửi Google Chat thất bại cho RFID (HTTP 500 | HTTPError)"
    assert exc.__cause__ is None
    # Context suppressed via from None
    assert getattr(exc, "__suppress_context__", False) is True

    # Assert neither secrets nor path appears anywhere in exc string, cause repr, context repr, or caplog
    cause_repr = repr(exc.__cause__)
    assert "key=" not in str(exc)
    assert webhook_secret not in str(exc)
    assert "token=" not in str(exc)
    assert "token5xx" not in str(exc)
    assert "/v1/spaces" not in str(exc)
    assert "/v1/spaces" not in cause_repr

    for rec in caplog.records:
        assert webhook_secret not in rec.message
        assert "token5xx" not in rec.message
        assert "/v1/spaces" not in rec.message


def test_post_to_chat_4xx_raises_immediately_without_retry(caplog):
    webhook_secret = "AIzaSyD-SecretKey-4xx"
    webhook = f"https://chat.googleapis.com/v1/spaces/SPACE4xx/messages?key={webhook_secret}&token=token4xx"
    calls = []
    sleeps = []

    def client_error_post(
        url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        calls.append((url, json))
        resp = requests.Response()
        resp.status_code = 404
        err_msg = f"404 Not Found for url: {url}"
        raise requests.exceptions.HTTPError(err_msg, response=resp)

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError) as exc_info:
            post_to_chat(
                webhook,
                "4xx message",
                http_post=client_error_post,
                project_key="RFID",
                sleep=lambda s: sleeps.append(s),
            )

    # 4xx must NOT retry
    assert len(calls) == 1
    assert len(sleeps) == 0

    exc = exc_info.value
    assert str(exc) == "Gửi Google Chat thất bại cho RFID (HTTP 404 | HTTPError)"
    assert exc.__cause__ is None
    assert getattr(exc, "__suppress_context__", False) is True

    assert webhook_secret not in str(exc)
    assert "token4xx" not in str(exc)
    assert "/v1/spaces" not in str(exc)

    # caplog must log status + project key (no URL)
    assert any("404" in rec.message and "RFID" in rec.message for rec in caplog.records)
    for rec in caplog.records:
        assert webhook_secret not in rec.message
        assert "token4xx" not in rec.message
        assert "/v1/spaces" not in rec.message


def test_post_to_chat_connection_error_redacts_path_and_query(caplog):
    webhook = "https://chat.googleapis.com/v1/spaces/X/messages?key=K&token=T"
    calls = []
    sleeps = []

    def conn_error_post(
        url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        calls.append((url, json))
        err_msg = "Connection error: failed to connect to chat.googleapis.com: /v1/spaces/X/messages?key=K&token=T"
        raise requests.exceptions.ConnectionError(err_msg)

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError) as exc_info:
            post_to_chat(
                webhook,
                "Conn error message",
                http_post=conn_error_post,
                project_key=None,  # lead
                sleep=lambda s: sleeps.append(s),
            )

    exc = exc_info.value
    assert str(exc) == "Gửi Google Chat thất bại cho lead (HTTP None | ConnectionError)"
    assert exc.__cause__ is None
    assert getattr(exc, "__suppress_context__", False) is True

    assert "key=K" not in str(exc)
    assert "token=T" not in str(exc)
    assert "/v1/spaces" not in str(exc)

    for rec in caplog.records:
        assert "key=K" not in rec.message
        assert "token=T" not in rec.message
        assert "/v1/spaces" not in rec.message


def test_sanitize_error_redacts_path_and_query():
    webhook = "https://chat.googleapis.com/v1/spaces/X/messages?key=K&token=T"
    err = requests.exceptions.ConnectionError(
        "Failed to reach https://chat.googleapis.com/v1/spaces/X/messages?key=K&token=T: /v1/spaces/X/messages?key=K&token=T"
    )
    sanitized = _sanitize_error(err, webhook)
    assert "key=K" not in sanitized
    assert "token=T" not in sanitized
    assert "/v1/spaces" not in sanitized


def test_post_to_chat_429_retries_honours_retry_after(caplog):
    webhook = (
        "https://chat.googleapis.com/v1/spaces/SPACE429/messages?key=secret&token=token"
    )
    calls = []
    sleeps = []

    def rate_limited_then_ok_post(
        url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        calls.append((url, json))
        if len(calls) == 1:
            resp = requests.Response()
            resp.status_code = 429
            resp.headers["Retry-After"] = "12"
            err_msg = f"429 Too Many Requests for url: {url}"
            raise requests.exceptions.HTTPError(err_msg, response=resp)
        resp = requests.Response()
        resp.status_code = 200
        return resp

    with caplog.at_level(logging.INFO):
        post_to_chat(
            webhook,
            "Rate limited message",
            http_post=rate_limited_then_ok_post,
            project_key="RFID",
            sleep=lambda s: sleeps.append(s),
        )

    assert len(calls) == 2
    assert sleeps == [12.0]
    for rec in caplog.records:
        assert "secret" not in rec.message
        assert "token" not in rec.message
        assert "/v1/spaces" not in rec.message


def test_post_to_chat_429_retries_caps_retry_after_at_30s():
    webhook = (
        "https://chat.googleapis.com/v1/spaces/SPACE429/messages?key=secret&token=token"
    )
    calls = []
    sleeps = []

    def rate_limited_then_ok_post(
        url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        calls.append((url, json))
        if len(calls) == 1:
            resp = requests.Response()
            resp.status_code = 429
            resp.headers["Retry-After"] = "60"
            err_msg = f"429 Too Many Requests for url: {url}"
            raise requests.exceptions.HTTPError(err_msg, response=resp)
        resp = requests.Response()
        resp.status_code = 200
        return resp

    post_to_chat(
        webhook,
        "Capped message",
        http_post=rate_limited_then_ok_post,
        project_key="RFID",
        sleep=lambda s: sleeps.append(s),
    )

    assert len(calls) == 2
    assert sleeps == [30.0]


def test_post_to_chat_429_default_5s_when_missing_or_non_numeric_header():
    webhook = (
        "https://chat.googleapis.com/v1/spaces/SPACE429/messages?key=secret&token=token"
    )
    # Case 1: missing Retry-After header
    calls1: list[Any] = []
    sleeps1: list[float] = []

    def post1(url: str, json: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        calls1.append((url, json))
        if len(calls1) == 1:
            resp = requests.Response()
            resp.status_code = 429
            raise requests.exceptions.HTTPError("429 Too Many Requests", response=resp)
        resp = requests.Response()
        resp.status_code = 200
        return resp

    post_to_chat(
        webhook,
        "Msg1",
        http_post=post1,
        project_key="RFID",
        sleep=lambda s: sleeps1.append(s),
    )
    assert len(calls1) == 2
    assert sleeps1 == [5.0]

    # Case 2: non-numeric Retry-After header
    calls2: list[Any] = []
    sleeps2: list[float] = []

    def post2(url: str, json: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        calls2.append((url, json))
        if len(calls2) == 1:
            resp = requests.Response()
            resp.status_code = 429
            resp.headers["Retry-After"] = "Wed, 21 Oct 2026 07:28:00 GMT"
            raise requests.exceptions.HTTPError("429 Too Many Requests", response=resp)
        resp = requests.Response()
        resp.status_code = 200
        return resp

    post_to_chat(
        webhook,
        "Msg2",
        http_post=post2,
        project_key="RFID",
        sleep=lambda s: sleeps2.append(s),
    )
    assert len(calls2) == 2
    assert sleeps2 == [5.0]


def test_post_to_chat_429_twice_raises_runtime_error():
    webhook = (
        "https://chat.googleapis.com/v1/spaces/SPACE429/messages?key=secret&token=token"
    )
    calls = []
    sleeps = []

    def always_429_post(
        url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        calls.append((url, json))
        resp = requests.Response()
        resp.status_code = 429
        resp.headers["Retry-After"] = "10"
        raise requests.exceptions.HTTPError("429 Too Many Requests", response=resp)

    with pytest.raises(RuntimeError) as exc_info:
        post_to_chat(
            webhook,
            "Permanent 429",
            http_post=always_429_post,
            project_key="RFID",
            sleep=lambda s: sleeps.append(s),
        )

    assert len(calls) == 2
    assert sleeps == [10.0]
    assert "429" in str(exc_info.value)
    assert "secret" not in str(exc_info.value)
    assert "/v1/spaces" not in str(exc_info.value)
