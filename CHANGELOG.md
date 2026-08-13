# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH).

The single source of the version is `app/__init__.py` (`__version__`); `pyproject.toml`
reads it dynamically. On every release: bump `__version__` **and** add a section here.

## [Unreleased]

### Added
- **Proxmox Backup Server shutdown targets.** Hosts now carry a platform discriminator;
  existing entries remain Proxmox VE by default, while PBS uses its `PBSAPIToken` header,
  `/system/status` permission scope and `Sys.PowerManagement` privilege. The target
  client boundary is kept separate from the engine so a future SSH-managed Linux target
  can join the same UPS policy and shutdown ordering.

## [3.3.0] - 2026-08-06

### Added
- **Vendor MIB support, starting with APC PowerNet.** An SNMP UPS is no longer read only
  through RFC 1628. Every SNMP UPS has a new **MIB** setting — `Automatic` (default),
  `RFC 1628 (standard)` or `APC PowerNet` — and the poller carries a table-driven profile
  per MIB, so further vendors are a table rather than new code.
- `Automatic` needs no configuration and is what existing UPS entries get on update: the
  regular poll carries the vendor anchor object alongside the standard ones, and the UPS
  is read on the vendor MIB as soon as it answers there. Under SNMPv1, where a single
  missing object aborts the whole request, the poller retries on the vendor MIB instead.
- This makes APC cards work that could not be used before: Schneider only supports
  RFC 1628 on Network Management Card 2 (AP9630/AP9631/AP9635) from firmware sumx/sy
  v5.1.7 on, while the older NMC1 cards (AP9617/AP9618/AP9619) speak PowerNet only.
- Cards that support both are now more accurate as well. `upsAdvBatteryRunTimeRemaining`
  is reported in hundredths of a second instead of whole minutes, and PowerNet reports a
  self-test (`onBatteryTest`) as its own state — RFC 1628 cannot tell one apart from a
  real outage.
- The UPS test walks both MIBs in `Automatic` mode and says which one it settled on and
  why, so "nothing works" becomes "this card has no RFC 1628, but PowerNet answers every
  object". The dashboard names the MIB a UPS is being read on.

## [3.2.0] - 2026-07-26

### Added
- **UPS sources**: a UPS is no longer necessarily an SNMP device. Every UPS entry now has
  a "Read via" selector, and the second source is a **NUT server** (Network UPS Tools,
  TCP 3493) — the answer for UPS devices with no network card, i.e. everything attached
  by USB or a serial cable. Any existing `upsd` works: the UPS server built into a
  Synology/QNAP/TrueNAS NAS, a Raspberry Pi, an OPNsense box, or a NUT install on a
  Proxmox host. Configuration is host, port, the UPS name from upsd's `ups.conf`, and
  optionally a user name and password.
- PVE-UPS acts strictly as a *read-only* NUT client: it only ever sends `LIST VAR`, never
  a command, and never runs `upsmon`. NUT is used as a device driver — the shutdown
  decision, the thresholds and the host policy stay in this appliance, so there are still
  no config files to write and no shutdown scripting on any host.
- The UPS test now names the trigger conditions the device cannot feed at all ("This UPS
  does not report: runtime remaining, battery charge"), because a threshold that can never
  fire is more dangerous than a visible error. Applies to both source types — plenty of
  NUT drivers omit `battery.runtime`, and plenty of SNMP cards omit
  `upsEstimatedMinutesRemaining`.
- `type` field in each UPS entry of the `/api/status` snapshot, and the dashboard card
  says which source a UPS is read through.
- `tests/nutsim.py`, a fake `upsd` for development and tests — the NUT counterpart of
  snmpsim + `snmpdata/`. Runnable standalone (`python -m tests.nutsim --scenario battery`)
  to click through the wizard without any UPS hardware.

### Changed
- `POST /api/test/ups` replaces `POST /api/test/snmp` as the wizard's test endpoint and
  handles every source type. The old path stays as an alias and behaves identically.
- Event-log wording and trigger reasons no longer say "SNMP" where they apply to any
  source ("No response for 3 polls …" instead of "No SNMP response for 3 polls …").
  The fail-safe behaviour itself is unchanged.
- The per-object test diagnosis was generalised from "per SNMP object (OID)" to "per
  object" and gained two outcomes: `missing` (the driver does not publish this variable)
  and `stale` (upsd answers, but its driver has lost contact with the UPS).
- Configuration schema: UPS entries carry an explicit `type` (`snmp` or `nut`). Existing
  configuration files and backups are migrated on load — an entry without `type` is read
  as SNMP, exactly as before — and the field is written out on the next save.

### Security
- A NUT server that reports stale data (`ERR DATA-STALE`, `ERR DRIVER-NOT-CONNECTED`) is
  treated as unreachable rather than as a valid reading. `upsd` keeps serving the last
  known values when its driver dies, and accepting those would have meant reporting
  "on mains" through an outage. The same applies when a driver omits `ups.status`
  entirely: the UPS counts as unreachable (alarm, never a shutdown) instead of silently
  looking healthy.

## [3.1.0] - 2026-07-26

### Added
- SNMP test now reports every RFC 1628 object individually, in an expandable "Details per
  SNMP object (OID)" block below the result line: value or
  `noSuchObject`/`noSuchInstance`/error per OID, plus a short overall diagnosis. It unfolds
  on its own when something is wrong. Each object is queried with its own GET, so one
  unimplemented OID no longer hides the state of all the others — notably the SNMPv1 case
  where a single missing object aborts the whole multi-object GET and the regular poll
  fails entirely.
- Configurable self-test interval (15 min, 30 min, 1 h, 2 h, 3 h, 6 h, 12 h, 24 h),
  anchored at the self-test start time, so the resulting times of day are fixed and
  predictable (start 09:00 every 6 h -> 09:00, 15:00, 21:00, 03:00). Existing
  configurations keep the previous daily cadence (24 h default).
- `next_selftest_at` in the `/api/status` appliance snapshot (naive local time).
- Optional Docker deployment alongside the LXC install: a `Dockerfile` and
  `docker-compose.example.yml`, with images published to
  `ghcr.io/ffind-dev/pve-ups` on each release tag. Configuration and event log
  persist via two volumes (`/etc/pve-usv`, `/var/lib/pve-usv`); the LXC path
  remains the default and unaffected.
- Deployment-mode flag `PVE_USV_DEPLOYMENT` (`lxc` default / `docker`). In Docker
  mode there is no privileged companion agent, so the NTP/timezone fields and the
  in-app update uploader are hidden in the wizard, and `POST /api/update/upload`
  returns HTTP 501 with guidance to pull a new image tag and recreate the container.
- Links to the GitHub project in the appliance's own documentation: a GitHub button in
  both manuals' top bar, a link row in the footer of the manuals and the web UI
  (repository, releases, issues, security policy), and the previously plain-text
  references to the releases page and the changelog are now real links.
- Docker section of both manuals now carries a self-contained Compose snippet, so the
  manuals stay the only documentation needed inside a release.

### Changed
- `selftest_hour` is now the *start time* of the self-test schedule rather than a plain
  daily hour, and the self-test itself changed in three ways that a short interval makes
  necessary: the slot that last ran is persisted in `engine-state.json` so a service
  restart no longer re-triggers the test, the hosts are queried concurrently instead of
  one after another (five unreachable hosts stalled the poll loop for 50 s), and a
  successful run is written to the event log at most once a day plus whenever it recovers
  from a failure. Failures are reported and notified as before. While a UPS is on battery
  the self-test is skipped entirely — the countdown has priority. The last result
  (`last_selftest_at`/`last_selftest_ok`) is persisted with the schedule latch, so
  `/api/status` keeps reporting it across a restart.
- `selftest_hour` and `selftest_interval_min` are normalised rather than rejected on load:
  an out-of-range hour is clamped to 0-23 and an unsupported interval falls back to daily,
  so a backup from another version always imports.
- `docker-compose.example.yml` sets `TZ` (the self-test schedule is interpreted in the
  container's local time, and the timezone cannot be set from the web UI in Docker mode)
  and adds `start_period: 15s` to the health check, matching the `Dockerfile`.
- Release workflow additionally tags the image with the bare version
  (`ghcr.io/ffind-dev/pve-ups:3.1.0`) next to the tag name (`:v3.1.0`) and `:latest`, so
  the image can be pinned with the usual registry convention.
- README (EN/DE) and both manuals warn about Docker's default address pool
  (`172.17.0.0/16` – `172.31.0.0/16`) shadowing networks in that range, which would cut
  the container off from a UPS or Proxmox host there.

### Fixed
- **SNMPv3 with encryption (authPriv) never worked.** Every poll failed with
  `Ciphering services not available`, while authNoPriv and v1/v2c were fine: pysnmp 7.x
  ships no ciphers of its own and delegates DES/3DES/AES to the `cryptography` package,
  which it declares only in its dev extra — so nothing installed it. It is now a declared
  dependency and comes along automatically, including on existing installations, because
  applying an update re-runs `pip install`. All privacy protocols were affected equally,
  so switching from DES to AES was no workaround; authNoPriv was. The dependency is capped
  below `cryptography` 50 because pysnmp's AES still uses an API that 49 deprecates and
  announces for removal; a test guards the exact symbols it relies on.
- The same failure is now reported as what it is instead of as "unreachable": it is
  detected before anything is sent, and the message names the missing package and the
  authNoPriv fallback — the previous wording sent users looking for firewall problems
  even though no packet ever left the appliance.

## [3.0.0] - 2026-07-15

First public release, under the public name **PVE-UPS** (technical identifiers — package,
paths, systemd units, env vars — deliberately stay `pve-usv` so in-place updates from 2.x
keep working).

### Added
- Bilingual web UI: English (default) and German. The language is picked automatically
  from the browser language and is easy to extend
  (`app/web/i18n/<lang>.js` + one `<script>` tag).
- English user manual (`manual.html`) alongside the German one (`handbuch.html`), same
  structure and anchors; the ?-icon and all deep links open the manual matching the UI
  language.
- i18n consistency tests (`tests/test_i18n.py`): key parity between the dictionaries,
  placeholder parity, and referenced-key checks for `index.html`/`app.js`.
- Installation and updates via GitHub releases: install one-liner downloads
  `install.sh` from the latest release; the release tarball doubles as the update package
  for the web UI uploader.

### Changed
- **Breaking:** all backend texts — event log entries, trigger reasons, webhook messages,
  API error details — are now uniformly English, regardless of the UI language.
- Webhook subject prefix is now `[PVE-UPS]`.
- Visible product name is PVE-UPS (UI, documentation, webhook); internal names stay
  `pve-usv`.

### Fixed
- The UPS card and the outage banner no longer show the time-based countdown once a UPS
  has triggered (battery low, runtime or charge threshold) — those conditions fire
  immediately and the ticking countdown wrongly suggested the shutdown would wait for
  it. The banner now also explains when triggered UPS are waiting for a host's
  AND/OR policy.

### Removed
- **Breaking:** e-mail (SMTP) notifications. The webhook remains and covers notification
  needs; a legacy `notifications.smtp` config key is ignored on load and dropped on the
  next save.

[Unreleased]: https://github.com/ffind-dev/pve-ups/compare/v3.3.0...HEAD
[3.3.0]: https://github.com/ffind-dev/pve-ups/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/ffind-dev/pve-ups/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/ffind-dev/pve-ups/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/ffind-dev/pve-ups/releases/tag/v3.0.0
