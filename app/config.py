"""Configuration model and persistence.

The entire appliance is configured through a *single* YAML file, written exclusively
by the web UI. No hand-editing of config files anywhere — that is the whole point of
this project. Secrets live in the same file, which is created with 0600 permissions.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import os
import secrets
import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

# Location can be overridden for tests / local runs.
CONFIG_PATH = Path(os.environ.get("PVE_USV_CONFIG", "/etc/pve-usv/config.yaml"))

# Selectable self-test intervals in minutes. Every one of them divides 1440 evenly, so the
# schedule forms an exact daily grid anchored at ``selftest_hour`` (see engine.selftest_slot).
# The <select> in app/web/index.html must offer exactly these values.
SELFTEST_INTERVALS = (15, 30, 60, 120, 180, 360, 720, 1440)


class UpsSourceType(str, Enum):
    """How a UPS is read. The value is the ``type`` discriminator in the config.

    Every member needs a matching ``src.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    snmp = "snmp"  # UPS with an SNMP network card, RFC 1628
    nut = "nut"  # any Network UPS Tools server (upsd) over TCP


class SnmpVersion(str, Enum):
    v1 = "v1"
    v2c = "v2c"
    v3 = "v3"


class SnmpMib(str, Enum):
    """Which UPS MIB the SNMP poller reads (see app/ups.py: PROFILES).

    Every member needs a matching ``mib.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    auto = "auto"  # read RFC 1628, switch to a vendor MIB when the device answers it
    rfc1628 = "rfc1628"  # the standard UPS-MIB only
    apc = "apc"  # APC PowerNet-MIB (1.3.6.1.4.1.318) only


class SnmpAuthProto(str, Enum):
    none = "none"
    md5 = "md5"
    sha = "sha"
    sha256 = "sha256"
    sha512 = "sha512"


class SnmpPrivProto(str, Enum):
    none = "none"
    des = "des"
    aes = "aes"
    aes256 = "aes256"


class UpsThresholdOverride(BaseModel):
    """Optional per-UPS override of the global trigger thresholds.

    Every field defaults to ``None`` meaning "inherit the global value". Only the
    trigger-relevant thresholds are overridable; the loop cadence
    (poll intervals, ``unreachable_alarm_after_polls``) stays global.
    """

    on_battery_seconds: Optional[int] = None
    runtime_below_minutes: Optional[int] = None
    charge_below_percent: Optional[int] = None
    on_battery_low: Optional[bool] = None
    comm_loss_shutdown_after_min: Optional[int] = None
    keep_shutdown_on_comm_loss: Optional[bool] = None


class UpsBase(BaseModel):
    """Fields every UPS source shares, regardless of how it is read.

    The engine, the thresholds and the host↔UPS mapping only ever touch these — a new
    source type therefore inherits the whole trigger/policy/fail-safe machinery.
    """

    # Identity (multi-UPS): ``id`` is a stable slug referenced by hosts, ``name`` is the
    # human label shown in the UI. ``id`` is auto-filled on save if left empty.
    id: str = ""
    name: str = ""

    # Optional per-UPS threshold override (None fields inherit the global thresholds).
    overrides: UpsThresholdOverride = UpsThresholdOverride()

    @property
    def configured(self) -> bool:
        """True once the source has enough information to be polled."""
        return False

    @property
    def label(self) -> str:
        """Display label, falling back to id and a per-type hint when no name is set."""
        return self.name or self.id or self._fallback_label() or "UPS"

    def _fallback_label(self) -> str:
        return ""

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        """Secret field names -> default value, for the API's masked-secret reconcile."""
        return {}


class SnmpConfig(UpsBase):
    # Plain string literal (not the enum) so a YAML/JSON "snmp" validates directly and
    # round-trips through yaml.safe_dump untouched. UpsSourceType stays the single list
    # of valid values; tests/test_i18n.py keeps both in sync.
    type: Literal["snmp"] = "snmp"

    host: str = ""
    port: int = 161
    version: SnmpVersion = SnmpVersion.v2c
    timeout_s: float = 3.0
    retries: int = 1

    # Which MIB to read. "auto" needs no migration: a config written before 3.3.0 has no
    # ``mib`` key and picks up this default, which is exactly what an existing APC device
    # wants. See app/ups.py for the profiles and how "auto" resolves.
    mib: SnmpMib = SnmpMib.auto

    # v1/v2c
    community: SecretStr = SecretStr("public")

    # v3
    v3_user: str = ""
    v3_auth_proto: SnmpAuthProto = SnmpAuthProto.sha
    v3_auth_pass: SecretStr = SecretStr("")
    v3_priv_proto: SnmpPrivProto = SnmpPrivProto.aes
    v3_priv_pass: SecretStr = SecretStr("")

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def _fallback_label(self) -> str:
        return self.host

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        return {"community": "public", "v3_auth_pass": "", "v3_priv_pass": ""}


class NutConfig(UpsBase):
    """A UPS read from a Network UPS Tools server (``upsd``) over TCP.

    We are a read-only client: PVE-UPS never runs ``upsmon`` and never hands the
    shutdown decision to NUT. Any existing upsd works — a NAS with its built-in UPS
    server, a Raspberry Pi, or the driver bundled with this appliance.
    """

    type: Literal["nut"] = "nut"

    host: str = ""  # upsd host (127.0.0.1 for a locally attached UPS)
    port: int = 3493
    ups_name: str = ""  # section name in upsd's ups.conf, e.g. "ups"
    # Credentials are optional: reading variables is anonymous on a default upsd.
    username: str = ""
    password: SecretStr = SecretStr("")
    timeout_s: float = 3.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.ups_name)

    def _fallback_label(self) -> str:
        return f"{self.ups_name}@{self.host}" if self.host else self.ups_name

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        return {"password": ""}


# Discriminated union of every supported source. Adding a type means: a model here, a
# branch in app/sources.py, an entry in UpsSourceType and the matching i18n keys.
UpsSource = Annotated[Union[SnmpConfig, NutConfig], Field(discriminator="type")]

UPS_SOURCE_MODELS: dict[str, type[UpsBase]] = {
    "snmp": SnmpConfig,
    "nut": NutConfig,
}


class ShutdownMethod(str, Enum):
    api_token = "api_token"


class HostPlatform(str, Enum):
    """API family used to control a shutdown target.

    Keep this discriminator separate from ``ShutdownMethod``: PVE and PBS both use
    an HTTP API today, while a future SSH-managed Linux target should be able to
    join the host flow without changing the engine's ordering or UPS policy logic.
    """

    pve = "pve"
    pbs = "pbs"


class HostConfig(BaseModel):
    platform: HostPlatform = HostPlatform.pve
    name: str  # target node name, e.g. "pve01" or "pbs"
    api_url: str  # e.g. "https://10.0.0.10:8006" (PVE) / :8007 (PBS)
    method: ShutdownMethod = ShutdownMethod.api_token
    # API token: user@realm!tokenid + secret. The platform selects the wire format.
    token_id: str = ""  # e.g. "ups@pve!shutdown"
    token_secret: SecretStr = SecretStr("")
    verify_tls: bool = False  # PVE/PBS ship self-signed certs by default
    this_host: bool = False  # the host carrying this appliance -> shut down last
    order: int = 0  # ascending; this_host is forced last regardless
    enabled: bool = True

    # Multi-UPS: which UPS devices feed this host (by UPS id). Empty = depends
    # on ALL configured UPS (conservative fallback). ``ups_policy`` decides how the
    # feeds combine: "all" = shut down only when every feed has triggered (redundant
    # PSUs, default), "any" = shut down as soon as one feed triggers (split, non-
    # redundant load).
    ups_ids: list[str] = Field(default_factory=list)
    ups_policy: Literal["all", "any"] = "all"


class Thresholds(BaseModel):
    """Shutdown triggers. Any condition that is met (and enabled) fires the shutdown."""

    on_battery_seconds: Optional[int] = 600  # on battery longer than this
    runtime_below_minutes: Optional[int] = 10  # estimated runtime under this
    charge_below_percent: Optional[int] = 30  # battery charge under this
    on_battery_low: bool = True  # UPS reports batteryLow/Depleted

    poll_interval_normal_s: int = 30
    poll_interval_battery_s: int = 8

    # If the UPS is unreachable, do NOT shut down (fail safe, not fail shutdown).
    # We only raise an alarm. This is intentional and stays the default.
    unreachable_alarm_after_polls: int = 3

    # OPT-IN override of the fail-safe: if set, a *pure* communication loss (SNMP
    # unreachable) for this many minutes triggers a shutdown anyway. None = off
    # (recommended default); only set this when a comms loss must be treated like
    # an outage. The power state is unknown while unreachable — use with care.
    comm_loss_shutdown_after_min: Optional[int] = None

    # If communication is lost *while already on battery* (a confirmed outage, e.g. a
    # switch between us and the UPS just lost power), do NOT abort the shutdown: keep the
    # on_battery_seconds countdown running on our own clock and fire when it elapses. A
    # pure comms loss on mains stays fail safe (alarm only). Only effective when
    # on_battery_seconds is set (default 600 s) — runtime/charge are unreadable while blind.
    keep_shutdown_on_comm_loss: bool = True

    # Seconds to wait for a guest/node shutdown to be accepted before moving on.
    host_shutdown_timeout_s: int = 60


class WebhookConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    # POSTs the /api/status payload as JSON on each notable event.


class Notifications(BaseModel):
    # Legacy configs (< 3.0.0) may still contain a ``smtp`` block; Pydantic ignores
    # unknown keys, so it is dropped silently on load and gone after the next save.
    webhook: WebhookConfig = WebhookConfig()


class AppConfig(BaseModel):
    # Marks whether the setup wizard has been completed at least once.
    configured: bool = False

    # Master safety switch: when True the engine only logs, never shuts anything down.
    dry_run: bool = True

    ups: list[UpsSource] = Field(default_factory=list)
    hosts: list[HostConfig] = Field(default_factory=list)
    thresholds: Thresholds = Thresholds()
    notifications: Notifications = Notifications()

    # Scheduled self-test: verify each shutdown target's API token and power privilege,
    # so a broken/expired credential is caught long before a real outage needs it.
    selftest_enabled: bool = True
    selftest_hour: int = 9  # anchor: hour of day (0-23, server local time)
    selftest_interval_min: int = 1440  # repeat every N minutes from the anchor; 1440 = daily

    # Optional NTP server pushed to the container's systemd-timesyncd (empty = leave
    # the system default untouched). Applied by the privileged deploy agent.
    ntp_server: str = ""

    # Optional IANA timezone (e.g. "Europe/Berlin") applied to the container by the
    # privileged deploy agent (empty = leave the system default, usually UTC, untouched).
    # Matters because the self-test schedule is interpreted in the container's local time.
    timezone: str = ""

    # Web UI auth. Read-only endpoints (/api/status, /api/health) are NOT protected.
    ui_password_hash: str = ""
    session_secret: str = Field(default_factory=lambda: secrets.token_hex(32))

    @model_validator(mode="before")
    @classmethod
    def _migrate_ups_list(cls, data):
        """Bring older UPS schemas up to the current ``ups: [{type: ..., ...}]`` shape.

        Two steps, both required for an existing ``config.yaml`` to keep working:

        * pre-2.0 had a single ``snmp: {...}`` block and hosts without ``ups_ids`` — it
          is wrapped into one UPS ``id="ups1"`` that every host is pointed at;
        * pre-3.2 UPS entries have no ``type`` at all, and the discriminated union would
          reject them. SNMP was the only source back then, so that is the default.

        An import must never be refused (same rule as the self-test validators below).
        """
        if not isinstance(data, dict):
            return data
        legacy = data.pop("snmp", None)
        if legacy is not None and not data.get("ups"):
            if not isinstance(legacy, dict):  # a SnmpConfig instance
                legacy = legacy.model_dump() if isinstance(legacy, BaseModel) else None
            if isinstance(legacy, dict):
                legacy = dict(legacy)
                legacy.setdefault("id", "ups1")
                legacy.setdefault("name", "UPS")
                data["ups"] = [legacy]
                for host in data.get("hosts", []) or []:
                    if isinstance(host, dict) and not host.get("ups_ids"):
                        host["ups_ids"] = ["ups1"]
        ups = data.get("ups")
        if isinstance(ups, list):
            data["ups"] = [
                {**u, "type": u.get("type") or "snmp"} if isinstance(u, dict) else u for u in ups
            ]
        return data

    @field_validator("selftest_interval_min", mode="before")
    @classmethod
    def _normalise_selftest_interval(cls, value):
        """Snap an unsupported interval to the daily default instead of rejecting it.

        A backup from another version or a hand-edited YAML must still import: a
        self-test running on the wrong cadence is cosmetic, a refused import is not.
        """
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return 1440
        return minutes if minutes in SELFTEST_INTERVALS else 1440

    @field_validator("selftest_hour", mode="before")
    @classmethod
    def _clamp_selftest_hour(cls, value):
        """Clamp to 0-23 — an out-of-range hour must not break the import either."""
        try:
            return min(23, max(0, int(value)))
        except (TypeError, ValueError):
            return 9

    def effective_thresholds(self, ups: UpsBase) -> Thresholds:
        """Global thresholds with this UPS's non-None overrides applied."""
        ov = ups.overrides
        merged = self.thresholds.model_copy()
        for field in (
            "on_battery_seconds",
            "runtime_below_minutes",
            "charge_below_percent",
            "on_battery_low",
            "comm_loss_shutdown_after_min",
            "keep_shutdown_on_comm_loss",
        ):
            val = getattr(ov, field)
            if val is not None:
                setattr(merged, field, val)
        return merged

    def ups_by_id(self, ups_id: str) -> Optional[UpsBase]:
        for u in self.ups:
            if u.id == ups_id:
                return u
        return None

    def feed_ids_for(self, host: HostConfig) -> list[str]:
        """UPS ids feeding a host; empty ups_ids means "all configured UPS"."""
        if host.ups_ids:
            return [i for i in host.ups_ids if self.ups_by_id(i) is not None]
        return [u.id for u in self.ups]

    def ordered_hosts(self) -> list[HostConfig]:
        """Enabled hosts in shutdown order; the appliance's own host always last."""
        active = [h for h in self.hosts if h.enabled]
        return sorted(active, key=lambda h: (h.this_host, h.order, h.name))


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "ups"


def assign_ups_ids(ups: list[UpsBase]) -> None:
    """Fill empty UPS ids with stable, collision-free slugs (in place)."""
    taken = {u.id for u in ups if u.id}
    for i, u in enumerate(ups, start=1):
        if u.id:
            continue
        base = _slugify(u.name) if u.name else f"ups{i}"
        candidate = base
        n = 2
        while candidate in taken or candidate == "":
            candidate = f"{base}-{n}"
            n += 1
        u.id = candidate
        taken.add(candidate)


def _to_serialisable(cfg: AppConfig) -> dict:
    """Dump the model to plain types: reveal SecretStr, unwrap Enums (so YAML works)."""

    def reveal(obj):
        if isinstance(obj, SecretStr):
            return obj.get_secret_value()
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, dict):
            return {k: reveal(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [reveal(v) for v in obj]
        return obj

    return reveal(cfg.model_dump(mode="python"))


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Load config from disk, or return defaults if it does not exist yet."""
    if not path.exists():
        return AppConfig()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return AppConfig.model_validate(data)


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    """Persist config to a single YAML file with 0600 permissions, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = _to_serialisable(cfg)
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
