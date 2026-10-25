# Changelog

## 0.2.0 — 2026-10-26

### Fixed

- Derive CPU busy percentage from every explicitly online core's interval idle
  value, avoiding a stale aggregate in affected jetson-stats releases. Incomplete
  or invalid topology returns null; offline cores do not skew the result.
- Replace one failed jtop client generation within a read. A second failure
  remains unavailable; no stale snapshot or unbounded recovery loop is returned.
- Normalize documented CPU/GPU/EMC kHz readings to the history API's Hz fields,
  and sort the selected history window by observation time across clock rollback.

### Added

- Optional stable Unix socket proxy examples for recovering after upstream jtop
  service restarts without replacing the Router container. Paths and identity
  are configured locally; no service is installed or started by the package.
- Read-only `/v2/telemetry/current` and bounded `/v2/telemetry/history` with stable
  OpenAPI operation IDs, explicit unavailable responses and partial-history metadata.
- Opt-in bounded JSONL telemetry journal, using the existing provider and
  allowlisted numeric observations. Rotation, record size, queue capacity and
  history result count are bounded; journal failure does not change admission.

### Compatibility

- Frozen v1 admission/lease/reservation responses and SQLite schema are unchanged.
  Existing authoritative-storage recovery and EXCLUSIVE conflict semantics remain.
- The journal is disabled unless `JETROUTER_TELEMETRY_JOURNAL_PATH` is configured.
  No database migration, authentication expansion, workload control or host
  permission change is performed. Existing data-directory modes are preserved.
- Keep the published jetson-stats 7.2.2 deployment identity and native image CI.
  Review the optional [proxy setup](docs/jtop-recovery.md) before host installation.

## 0.1.0

Initial admission/lease/reservation API and deployment baseline, including
authoritative storage recovery and the published JetPack 7.2.2 compatibility update.
