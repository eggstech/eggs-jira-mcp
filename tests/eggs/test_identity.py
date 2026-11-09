from __future__ import annotations

from mcp_atlassian.eggs.config import EggsConfig, ProjectConfig
from mcp_atlassian.eggs.identity import IdentityCache, resolve_identity
from tests.eggs.fakes import FakeGateway


def test_identity_roles_per_project():
    # Dev in RFID, QC in CP
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
            "CP": ProjectConfig(key="CP", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
    )
    gateway = FakeGateway(
        me={"name": "alice", "displayName": "Alice Smith"},
        role_members={
            "RFID": {"Developers": ["alice", "bob"], "QC": ["charlie"]},
            "CP": {"Developers": ["david"], "QC": ["alice"]},
        },
    )
    cache = IdentityCache()
    identity = resolve_identity(gateway, cfg, cache)

    assert identity.username == "alice"
    assert identity.display_name == "Alice Smith"
    assert identity.is_lead is False
    assert identity.roles == {
        "RFID": {"dev"},
        "CP": {"qc"},
    }


def test_identity_cached_within_ttl():
    # Second call makes no gateway call; after 601 s it does
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
    )
    gateway = FakeGateway(
        me={"name": "alice", "displayName": "Alice Smith"},
        role_members={
            "RFID": {"Developers": ["alice"]},
        },
    )
    current_time = 1000.0

    def mock_clock() -> float:
        return current_time

    cache = IdentityCache(ttl_seconds=600, clock=mock_clock)

    # First call -> calls gateway
    id1 = resolve_identity(gateway, cfg, cache)
    assert id1.username == "alice"
    calls_after_first = len(gateway.calls)
    assert calls_after_first > 0

    # Second call within TTL -> makes NO gateway call
    current_time += 100.0  # +100s, within 600s TTL
    id2 = resolve_identity(gateway, cfg, cache)
    assert id2 == id1
    assert len(gateway.calls) == calls_after_first

    # Third call after TTL (601 s total) -> makes gateway call again
    current_time += 501.0  # total +601s
    id3 = resolve_identity(gateway, cfg, cache)
    assert id3.username == "alice"
    assert len(gateway.calls) > calls_after_first


def test_identity_without_role_is_empty():
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
    )
    gateway = FakeGateway(
        me={"name": "stranger", "displayName": "Stranger User"},
        role_members={
            "RFID": {"Developers": ["alice"]},
        },
    )
    cache = IdentityCache()
    identity = resolve_identity(gateway, cfg, cache)

    assert identity.username == "stranger"
    assert identity.display_name == "Stranger User"
    assert identity.roles == {}
    assert identity.is_lead is False


def test_identity_administrators_count_as_lead_ba():
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={
            "lead_ba": ["Business Analysts", "Administrators"],
            "qc": ["Testers"],
            "dev": ["Developers"],
        },
    )
    gateway = FakeGateway(
        me={"name": "admin_user", "displayName": "Admin User"},
        role_members={
            "RFID": {"Administrators": ["admin_user"], "Developers": ["alice"]},
        },
    )
    identity = resolve_identity(gateway, cfg)
    assert identity.username == "admin_user"
    assert "lead_ba" in identity.roles.get("RFID", set())


def test_identity_ignores_bot_user():
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        bot_user="eggs-bot",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={
            "lead_ba": ["Business Analysts", "Administrators"],
            "qc": ["Testers"],
            "dev": ["Developers"],
        },
    )
    gateway = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={
            "RFID": {"Administrators": ["eggs-bot"]},
        },
    )
    identity = resolve_identity(gateway, cfg)
    assert identity.roles == {}

