"""Tests for the platform-specific shutdown target clients.

These tests use an in-memory async client. They never contact a PVE/PBS host and
never send a real shutdown command.
"""

from dataclasses import dataclass

import pytest

from app.config import AppConfig, HostConfig, HostPlatform
from app import host_targets


@dataclass
class _Response:
    status_code: int = 200
    payload: dict | None = None
    text: str = ""

    def json(self):
        return self.payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, path):
        self.calls.append(("GET", path, None))
        return self.responses[path]

    async def post(self, path, data=None):
        self.calls.append(("POST", path, data))
        return self.responses[path]


def _host(platform=HostPlatform.pbs):
    is_pbs = platform == HostPlatform.pbs
    return HostConfig(
        platform=platform,
        name="localhost" if is_pbs else "pve01",
        api_url="https://target.invalid:8007" if is_pbs else "https://target.invalid:8006",
        token_id="ups@pbs!shutdown" if is_pbs else "ups@pve!shutdown",
        token_secret="test-secret",
    )


def test_legacy_host_defaults_to_pve():
    host = HostConfig(name="pve01", api_url="https://pve.invalid:8006")
    assert host.platform is HostPlatform.pve


def test_pve_and_pbs_token_headers_use_their_wire_formats():
    assert host_targets._auth_header(_host(HostPlatform.pve)) == {
        "Authorization": "PVEAPIToken=ups@pve!shutdown=test-secret"
    }
    assert host_targets._auth_header(_host(HostPlatform.pbs)) == {
        "Authorization": "PBSAPIToken=ups@pbs!shutdown:test-secret"
    }


@pytest.mark.asyncio
async def test_pbs_connection_checks_system_status_power_privilege(monkeypatch):
    client = _Client(
        {
            "/version": _Response(payload={"data": {"release": "test"}}),
            "/access/permissions": _Response(
                payload={"data": {"/system/status": {"Sys.PowerManagement": True}}}
            ),
        }
    )
    monkeypatch.setattr(host_targets, "_client", lambda host, timeout: client)

    result = await host_targets.test_connection(_host())

    assert result.ok is True
    assert result.has_power_mgmt is True
    assert "Sys.PowerManagement" in result.message
    assert client.calls == [("GET", "/version", None), ("GET", "/access/permissions", None)]


@pytest.mark.asyncio
async def test_pve_connection_still_checks_node_scope(monkeypatch):
    client = _Client(
        {
            "/version": _Response(payload={"data": {}}),
            "/access/permissions": _Response(
                payload={"data": {"/nodes/pve01": {"Sys.PowerMgmt": True}}}
            ),
        }
    )
    monkeypatch.setattr(host_targets, "_client", lambda host, timeout: client)

    result = await host_targets.test_connection(_host(HostPlatform.pve))

    assert result.ok is True
    assert result.has_power_mgmt is True


@pytest.mark.asyncio
async def test_pbs_connection_warns_when_power_privilege_is_missing(monkeypatch):
    client = _Client(
        {
            "/version": _Response(payload={"data": {}}),
            "/access/permissions": _Response(payload={"data": {"/system/status": {}}}),
        }
    )
    monkeypatch.setattr(host_targets, "_client", lambda host, timeout: client)

    result = await host_targets.test_connection(_host())

    assert result.ok is True
    assert result.has_power_mgmt is False
    assert "might be rejected" in result.message


@pytest.mark.asyncio
async def test_pbs_shutdown_posts_to_node_status(monkeypatch):
    client = _Client({"/nodes/localhost/status": _Response(status_code=200)})
    monkeypatch.setattr(host_targets, "_client", lambda host, timeout: client)

    ok, message = await host_targets.shutdown_node(_host())

    assert ok is True
    assert message == "Shutdown command accepted"
    assert client.calls == [("POST", "/nodes/localhost/status", {"command": "shutdown"})]


def test_mixed_pve_pbs_targets_share_existing_shutdown_ordering():
    cfg = AppConfig(
        hosts=[
            HostConfig(
                platform="pbs", name="localhost", api_url="https://pbs.invalid:8007", order=2
            ),
            HostConfig(
                platform="pve", name="pve01", api_url="https://pve.invalid:8006", order=1
            ),
            HostConfig(
                platform="pve", name="appliance", api_url="https://pve.invalid:8006", this_host=True
            ),
        ]
    )

    assert [host.name for host in cfg.ordered_hosts()] == ["pve01", "localhost", "appliance"]
    assert cfg.ordered_hosts()[1].platform is HostPlatform.pbs
