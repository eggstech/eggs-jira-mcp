from __future__ import annotations

import logging

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
    user_gateway = FakeGateway(
        me={"name": "alice", "displayName": "Alice Smith"},
    )
    bot_gateway = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={
            "RFID": {"Developers": ["alice", "bob"], "QC": ["charlie"]},
            "CP": {"Developers": ["david"], "QC": ["alice"]},
        },
    )
    cache = IdentityCache()
    identity = resolve_identity(user_gateway, bot_gateway, cfg, cache)

    assert identity.username == "alice"
    assert identity.display_name == "Alice Smith"
    assert identity.is_lead is False
    assert identity.roles == {
        "RFID": {"dev"},
        "CP": {"qc"},
    }


def test_identity_never_shared_between_users():
    # Users A then B with the same cache -> B gets B's roles, myself() called twice
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
    )
    gw_a = FakeGateway(me={"name": "alice", "displayName": "Alice Smith"})
    gw_b = FakeGateway(me={"name": "bob", "displayName": "Bob Jones"})
    bot_gw = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={
            "RFID": {"Developers": ["alice"], "QC": ["bob"]},
        },
    )
    cache = IdentityCache()

    id_a = resolve_identity(gw_a, bot_gw, cfg, cache)
    assert id_a.username == "alice"
    assert id_a.roles == {"RFID": {"dev"}}
    assert any(c[0] == "myself" for c in gw_a.calls)

    id_b = resolve_identity(gw_b, bot_gw, cfg, cache)
    assert id_b.username == "bob"
    assert id_b.roles == {"RFID": {"qc"}}
    assert any(c[0] == "myself" for c in gw_b.calls)


def test_role_members_cached_per_project_within_ttl():
    # Second resolve makes no roles_reader call; after 601 s it does
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
    )
    user_gw = FakeGateway(me={"name": "alice", "displayName": "Alice Smith"})
    bot_gw = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={
            "RFID": {"Developers": ["alice"]},
        },
    )
    current_time = 1000.0

    def mock_clock() -> float:
        return current_time

    cache = IdentityCache(ttl_seconds=600, clock=mock_clock)

    # First call -> calls roles_reader
    id1 = resolve_identity(user_gw, bot_gw, cfg, cache)
    assert id1.username == "alice"
    roles_calls_1 = len([c for c in bot_gw.calls if c[0] == "project_role_members"])
    assert roles_calls_1 == 1

    # Second call within TTL -> makes NO roles_reader call
    current_time += 100.0
    id2 = resolve_identity(user_gw, bot_gw, cfg, cache)
    assert id2.username == "alice"
    roles_calls_2 = len([c for c in bot_gw.calls if c[0] == "project_role_members"])
    assert roles_calls_2 == 1

    # Third call after TTL (+601s total) -> calls roles_reader again
    current_time += 501.0
    id3 = resolve_identity(user_gw, bot_gw, cfg, cache)
    assert id3.username == "alice"
    roles_calls_3 = len([c for c in bot_gw.calls if c[0] == "project_role_members"])
    assert roles_calls_3 == 2


def test_role_read_failure_not_cached_and_reported():
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
    )
    user_gw = FakeGateway(me={"name": "alice", "displayName": "Alice Smith"})

    class FailingGateway(FakeGateway):
        fail_count = 0

        def project_role_members(self, project: str, role_names: list[str] | None = None) -> dict[str, list[str]]:
            self._record("project_role_members", project)
            self.fail_count += 1
            raise RuntimeError("403 Forbidden")

    failing_bot_gw = FailingGateway(me={"name": "eggs-bot", "displayName": "U Guardian"})
    cache = IdentityCache()

    id1 = resolve_identity(user_gw, failing_bot_gw, cfg, cache)
    assert id1.username == "alice"
    assert id1.roles == {}
    assert id1.errors == ["RFID"]
    assert failing_bot_gw.fail_count == 1

    # Second call: failure must NOT have been cached, so roles_reader called again
    id2 = resolve_identity(user_gw, failing_bot_gw, cfg, cache)
    assert id2.errors == ["RFID"]
    assert failing_bot_gw.fail_count == 2


def test_identity_without_role_is_empty():
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
    )
    user_gw = FakeGateway(me={"name": "stranger", "displayName": "Stranger User"})
    bot_gw = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={
            "RFID": {"Developers": ["alice"]},
        },
    )
    cache = IdentityCache()
    identity = resolve_identity(user_gw, bot_gw, cfg, cache)

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
    user_gw = FakeGateway(me={"name": "admin_user", "displayName": "Admin User"})
    bot_gw = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={
            "RFID": {"Administrators": ["admin_user"], "Developers": ["alice"]},
        },
    )
    identity = resolve_identity(user_gw, bot_gw, cfg)
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
    user_gw = FakeGateway(me={"name": "eggs-bot", "displayName": "U Guardian"})
    bot_gw = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={
            "RFID": {"Administrators": ["eggs-bot"]},
        },
    )
    identity = resolve_identity(user_gw, bot_gw, cfg)
    assert identity.roles == {}


def test_identity_is_lead_compares_lowercased():
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers"]},
    )
    user_gw = FakeGateway(me={"name": "Cuong.Huynh", "displayName": "Cuong Huynh"})
    bot_gw = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={"RFID": {}},
    )
    identity = resolve_identity(user_gw, bot_gw, cfg)
    assert identity.is_lead is True


def test_identity_warns_when_configured_role_missing_in_project(caplog):
    cfg = EggsConfig(
        lead_user="cuong.huynh",
        jira_url="https://jira.eggstech.io.vn",
        projects={
            "RFID": ProjectConfig(key="RFID", default_lead_ba="cuong.huynh", triage_owner="cuong.huynh"),
        },
        roles={"lead_ba": ["Lead-BA"], "qc": ["QC"], "dev": ["Developers", "Engineers"]},
    )
    user_gw = FakeGateway(me={"name": "alice", "displayName": "Alice Smith"})
    bot_gw = FakeGateway(
        me={"name": "eggs-bot", "displayName": "U Guardian"},
        role_members={
            "RFID": {"Developers": ["alice"], "QC": ["bob"], "Lead-BA": ["carol"]},
        },
    )
    with caplog.at_level(logging.WARNING):
        identity = resolve_identity(user_gw, bot_gw, cfg)

    assert identity.username == "alice"
    assert any("Engineers" in record.message and "RFID" in record.message for record in caplog.records)


