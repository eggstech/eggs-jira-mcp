from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_atlassian.eggs import uploads
from mcp_atlassian.eggs.actions import Actions
from mcp_atlassian.eggs.calendar import WorkCalendar
from mcp_atlassian.eggs.config import EggsConfig
from mcp_atlassian.eggs.tools import (
    clear_identity_cache,
    register_eggs_tools,
    set_roles_reader,
)
from tests.eggs.fakes import FakeGateway


def _issue(key: str = "EGG-1") -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "issuetype": {"name": "Bug"},
            "summary": "Lỗi đăng nhập",
            "status": {"name": "QC Testing"},
        },
    }


@pytest.fixture(autouse=True)
def _reset():
    clear_identity_cache()
    set_roles_reader(None)
    uploads._tickets.clear()
    yield
    uploads._tickets.clear()
    uploads.set_gateway_factory(None)
    set_roles_reader(None)


@pytest.fixture
def cfg() -> EggsConfig:
    return EggsConfig(lead_user="cuong.huynh", jira_url="https://jira.example")


class TestTickets:
    def test_ticket_is_single_use(self):
        tok = uploads.create_ticket(
            user_token="pat", username="u", key="egg-1", comment=None, now=100.0
        )
        t = uploads.take_ticket(tok, now=101.0)
        assert t is not None and t.key == "EGG-1"
        assert uploads.take_ticket(tok, now=102.0) is None

    def test_ticket_expires(self):
        tok = uploads.create_ticket(
            user_token="pat", username="u", key="EGG-1", comment=None, now=100.0
        )
        later = 100.0 + uploads.UPLOAD_TTL_SECONDS + 1
        assert uploads.take_ticket(tok, now=later) is None

    def test_safe_filename_strips_paths(self):
        assert uploads.safe_filename("../../etc/passwd") == "passwd"
        assert uploads.safe_filename("C:\\Users\\a\\shot.png") == "shot.png"
        assert uploads.safe_filename("") == "file"


class TestAttachFiles:
    def _ticket(self, comment: str | None = None) -> uploads.UploadTicket:
        return uploads.UploadTicket(
            user_token="pat",
            username="u",
            key="EGG-1",
            comment=comment,
            expires_at=1e12,
        )

    def test_attaches_and_comments_with_inline_image(self):
        gw = FakeGateway(issues={"EGG-1": _issue()})
        res = uploads.attach_files(
            gw,
            self._ticket("Bằng chứng test"),
            [("shot.png", b"png"), ("log.txt", b"log")],
        )
        assert res["ok"] is True
        assert res["attached"] == ["shot.png", "log.txt"]
        body = gw.issues["EGG-1"]["fields"]["comment"]["comments"][-1]["body"]
        assert "Bằng chứng test" in body
        assert "!shot.png|thumbnail!" in body
        assert "[^log.txt]" in body

    def test_no_comment_means_attach_only(self):
        gw = FakeGateway(issues={"EGG-1": _issue()})
        res = uploads.attach_files(gw, self._ticket(), [("a.png", b"x")])
        assert res["ok"] is True
        assert "comment" not in gw.issues["EGG-1"]["fields"]

    def test_rejects_empty_and_too_big(self, monkeypatch: pytest.MonkeyPatch):
        gw = FakeGateway(issues={"EGG-1": _issue()})
        assert uploads.attach_files(gw, self._ticket(), [])["ok"] is False
        monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 3)
        res = uploads.attach_files(gw, self._ticket(), [("big.png", b"1234")])
        assert res["ok"] is False and "big.png" in res["message"]

    def test_unknown_issue_reports_jira_refusal(self):
        gw = FakeGateway(issues={})
        res = uploads.attach_files(gw, self._ticket(), [("a.png", b"x")])
        assert res["ok"] is False
        assert "EGG-1" in res["message"]


class TestUploadRoute:
    def _client(self) -> TestClient:
        app = Starlette(
            routes=[Route("/upload/{token}", uploads.handle_upload, methods=["POST"])]
        )
        return TestClient(app)

    def test_upload_uses_ticket_token_and_is_one_time(self):
        gw = FakeGateway(issues={"EGG-1": _issue()})
        seen: list[str] = []

        def factory(token: str) -> FakeGateway:
            seen.append(token)
            return gw

        uploads.set_gateway_factory(factory)
        tok = uploads.create_ticket(
            user_token="user-pat", username="qc", key="EGG-1", comment="ok"
        )
        client = self._client()
        r = client.post(f"/upload/{tok}", files={"file": ("s.png", b"img")})
        assert r.status_code == 200, r.text
        assert r.json()["attached"] == ["s.png"]
        assert seen == ["user-pat"]
        r2 = client.post(f"/upload/{tok}", files={"file": ("s.png", b"img")})
        assert r2.status_code == 403

    def test_bad_token_is_403(self):
        r = self._client().post("/upload/nope", files={"file": ("s.png", b"x")})
        assert r.status_code == 403


class TestCommentAction:
    def test_comment_and_dry_run(self, cfg: EggsConfig):
        gw = FakeGateway(issues={"EGG-1": _issue()})
        actions = Actions(gw, cfg, WorkCalendar(cfg.holidays))
        dry = actions.add_comment("EGG-1", "xin chào", dry_run=True)
        assert dry.ok and "comment" not in gw.issues["EGG-1"]["fields"]
        res = actions.add_comment("EGG-1", "xin chào")
        assert res.ok
        body = gw.issues["EGG-1"]["fields"]["comment"]["comments"][-1]["body"]
        assert body == "xin chào"

    def test_empty_comment_refused(self, cfg: EggsConfig):
        gw = FakeGateway(issues={"EGG-1": _issue()})
        actions = Actions(gw, cfg, WorkCalendar(cfg.holidays))
        assert actions.add_comment("EGG-1", "  ").ok is False


class TestTools:
    @pytest.mark.asyncio
    async def test_attach_url_binds_user_token(self, cfg: EggsConfig):
        server = FastMCP("t")
        register_eggs_tools(server, force=True)
        gw = FakeGateway(user="qc1", issues={"EGG-1": _issue()})
        request = MagicMock()
        request.state.user_atlassian_token = "qc-pat"
        fake_ctx = MagicMock()
        fake_ctx.request_context.request = request
        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=gw),
            ),
            patch(
                "fastmcp.server.context.Context.request_context",
                new=fake_ctx.request_context,
            ),
        ):
            res = await server.call_tool(
                "eggs_attach_url", {"key": "egg-1", "comment": "evidence"}
            )
        data = json.loads(res.content[0].text)
        assert data["ok"] is True
        tok = data["url"].rsplit("/", 1)[1]
        ticket = uploads.take_ticket(tok)
        assert ticket is not None
        assert (ticket.user_token, ticket.username, ticket.key, ticket.comment) == (
            "qc-pat",
            "qc1",
            "EGG-1",
            "evidence",
        )
        assert "curl" in data["command"]

    @pytest.mark.asyncio
    async def test_comment_tool(self, cfg: EggsConfig):
        server = FastMCP("t")
        register_eggs_tools(server, force=True)
        gw = FakeGateway(issues={"EGG-1": _issue()})
        with (
            patch("mcp_atlassian.eggs.tools.load_config", return_value=cfg),
            patch(
                "mcp_atlassian.eggs.tools.get_user_gateway",
                AsyncMock(return_value=gw),
            ),
        ):
            res = await server.call_tool(
                "eggs_comment", {"key": "EGG-1", "text": "đã test xong"}
            )
        data = json.loads(res.content[0].text)
        assert data["ok"] is True
        body = gw.issues["EGG-1"]["fields"]["comment"]["comments"][-1]["body"]
        assert body == "đã test xong"
