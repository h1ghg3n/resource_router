# Read-only telemetry API v2

Status: minimal stable read contract
Introduced in package release: `0.2.0` (the frozen v1 routes remain available)
Base path: `/v2/telemetry`
Media type: `application/json`

This surface lets an LLM tool and a time-series visualization adapter read the
same allowlisted telemetry already used by Resource Router. It does not change
the frozen v1 admission API, collect a second copy of platform data, create a
lease, alter a reservation, or control the host.

Normal status reads still run the manager's existing expiry reconciliation.
The optional journal reuses the same jtop provider and additionally samples
allowlisted proc-memory and current cgroup namespace counters; it is not a new
admission authority or a view of every workload's private cgroup.

The deployment remains local-only. Import `/openapi.json` from a trusted adapter
and select the stable operation IDs below. Do not publish the Router directly to
an untrusted network; add authentication and transport policy at a gateway if a
remote integration is later required.

## Current snapshot

`GET /v2/telemetry/current`
OpenAPI operation ID: `getTelemetryCurrent`

The response contains:

- `schema_version`: currently `1`;
- `observed_at`: the platform observation timestamp with timezone;
- `router`: the complete frozen `/v1/status` response, including sample age,
  normalized metrics, effective capacity, lease counts, and reservation state.

The endpoint returns `503 TELEMETRY_UNAVAILABLE` with `Retry-After` when a fresh
sample is unavailable. Null optional metrics remain different from measured
zeroes. The response is informational and cannot predict or reserve capacity.

## Bounded history

`GET /v2/telemetry/history`
OpenAPI operation ID: `getTelemetryHistory`

| Query | Default | Contract |
|---|---:|---|
| `since` | none | Inclusive ISO-8601 timestamp with timezone |
| `before` | none | Exclusive ISO-8601 timestamp with timezone |
| `limit` | `120` | Integer from `1` through `1000` |

`since` must be earlier than `before`. Samples are returned oldest first. When
more matching samples exist, the newest `limit` samples win and `truncated` is
true. A caller that needs an older window should move `before` to an earlier
timestamp rather than increasing the hard limit.

The result window uses journal append recency and is sorted by observation time
within that window, even if the system clock moves backward. Clock frequency
fields ending in `_hz` convert the documented jetson-stats CPU/GPU/EMC kHz
values to Hz; missing values remain null. See the
[upstream property reference](https://rnext.it/jetson_stats/reference/jtop.html).

The response also reports `partial`, `read_errors`, and `skipped_records` so a
chart or LLM does not silently treat corrupt or concurrently rotated input as a
complete interval. `writer` contains only process-local nonsecret counters and
the fail-open disabled flag. Missing history configuration returns
`503 TELEMETRY_HISTORY_UNAVAILABLE`; this does not change Router readiness.

Each sample is the journal's allowlisted schema-v1 record: observation and
monotonic timestamps, provider/session/sample identities, normalized memory,
CPU, GPU, EMC, temperature and power values, bounded host/cgroup counters, and
an `unavailable` field list. Prompts, generated output, request bodies,
credentials, environment values, filesystem paths, and arbitrary jtop fields
are excluded.

## Suggested consumers

An LLM tool normally calls `getTelemetryCurrent` first and requests history only
when the user asks for a trend or comparison. It must cite `observed_at`, sample
age, null fields, and `partial`/`truncated` state when explaining the result.

A visualization adapter should poll the current endpoint for status panels or
query bounded history windows for charts. It should key a sample by
`session_id`, `sample_id`, `monotonic_ns`, and `observed_at`; a process restart
may reset the integer sample sequence. The API intentionally does not provide a
Prometheus exporter, dashboard, database, unbounded download, or push stream.
