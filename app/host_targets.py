"""Shutdown target clients.

The engine deliberately knows nothing about the target's API. It only orders
``HostConfig`` objects and asks this module to test or shut one down. HTTP API
profiles cover Proxmox VE and Proxmox Backup Server today; a future SSH or other
transport can be added as another profile/driver without changing UPS policies,
ordering, or the state machine.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import HostConfig, HostPlatform

log = logging.getLogger("pve-usv.host_targets")


@dataclass
class TestResult:
    ok: bool
    message: str
    has_power_mgmt: bool = False


@dataclass(frozen=True)
class _ApiProfile:
    label: str
    token_prefix: str
    token_separator: str
    power_privilege: str
    permission_paths: tuple[str, ...]


_API_PROFILES: dict[HostPlatform, _ApiProfile] = {
    HostPlatform.pve: _ApiProfile(
        label="Proxmox VE",
        token_prefix="PVEAPIToken",
        token_separator="=",
        power_privilege="Sys.PowerMgmt",
        permission_paths=("/nodes/{name}", "/nodes", "/"),
    ),
    HostPlatform.pbs: _ApiProfile(
        label="Proxmox Backup Server",
        token_prefix="PBSAPIToken",
        token_separator=":",
        power_privilege="Sys.PowerManagement",
        permission_paths=("/system/status",),
    ),
}


def _profile_for(host: HostConfig) -> _ApiProfile | None:
    """Return the API profile, or ``None`` for a future/unrecognised target."""
    try:
        return _API_PROFILES.get(HostPlatform(host.platform))
    except (TypeError, ValueError):
        return None


def _auth_header(host: HostConfig) -> dict[str, str]:
    profile = _profile_for(host)
    if profile is None:
        raise ValueError(f"Unsupported host platform: {host.platform}")
    secret = host.token_secret.get_secret_value()
    return {
        "Authorization": (
            f"{profile.token_prefix}={host.token_id}{profile.token_separator}{secret}"
        )
    }


def _client(host: HostConfig, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=host.api_url.rstrip("/") + "/api2/json",
        headers=_auth_header(host),
        verify=host.verify_tls,
        timeout=timeout,
    )


def _has_power_permission(data: dict, host: HostConfig, profile: _ApiProfile) -> bool:
    for path_template in profile.permission_paths:
        path = path_template.format(name=host.name)
        if data.get(path, {}).get(profile.power_privilege):
            return True
    return False


async def test_connection(host: HostConfig, timeout: float = 10.0) -> TestResult:
    """Validate a target URL, token, and power-management privilege."""
    profile = _profile_for(host)
    if profile is None:
        return TestResult(False, f"Unsupported host platform: {host.platform}")

    try:
        async with _client(host, timeout) as client:
            # Version confirms reachability and token validity for both APIs.
            resp = await client.get("/version")
            if resp.status_code == 401:
                return TestResult(False, "Authentication failed (token invalid?)")
            resp.raise_for_status()

            # PVE exposes node-scoped permissions; PBS exposes the node status
            # permission at /system/status. These are effective token permissions.
            perm = await client.get("/access/permissions")
            has_power = False
            if perm.status_code == 200:
                data = perm.json().get("data", {})
                has_power = _has_power_permission(data, host, profile)

            if not has_power:
                return TestResult(
                    True,
                    f"Connection ok, but '{profile.power_privilege}' could not be "
                    "confirmed. A shutdown might be rejected.",
                    has_power_mgmt=False,
                )
            return TestResult(
                True,
                f"Connection and '{profile.power_privilege}' privilege ok.",
                has_power_mgmt=True,
            )

    except httpx.HTTPStatusError as exc:
        return TestResult(False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        return TestResult(False, f"Connection error: {exc}")


async def shutdown_node(host: HostConfig, timeout: float = 60.0) -> tuple[bool, str]:
    """Issue an orderly target shutdown. Returns ``(ok, message)``."""
    profile = _profile_for(host)
    if profile is None:
        return False, f"Unsupported host platform: {host.platform}"

    try:
        async with _client(host, timeout) as client:
            resp = await client.post(
                f"/nodes/{host.name}/status", data={"command": "shutdown"}
            )
            if resp.status_code in (200, 201):
                return True, "Shutdown command accepted"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        log.error("Shutdown of %s target %s failed: %s", profile.label, host.name, exc)
        return False, f"Error: {exc}"
