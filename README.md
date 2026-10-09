# Jetson Resource Router

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE.md)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](pyproject.toml)

Jetson Resource Router is a cooperative, single-node admission controller for
NVIDIA Jetson Orin workloads. It grants time-limited resource leases using live
Jetson telemetry and a durable lease ledger, helping independent workloads
avoid oversubscribing unified memory, CPU capacity, and GPU access.

The Router coordinates workloads; it does not run or control them. Clients are
responsible for declaring conservative peak demand, renewing leases while work
continues, stopping resource use before release, and respecting reservation
windows.

The complete v1 protocol is documented in
[`docs/api-contract-v1.md`](docs/api-contract-v1.md).

## What it provides

- Atomic `SHARED` and node-wide `EXCLUSIVE` lease admission.
- Unified-memory accounting that combines committed leases with observed
  memory headroom.
- CPU and GPU pressure guardrails based on Jetson telemetry.
- Fixed future `EXCLUSIVE` reservations with a safe drain interval.
- SQLite persistence for active leases, terminal tombstones, idempotency, and
  reservations.
- Recovery that fails closed after restart or uncertain storage outcomes.
- Liveness, readiness, capacity, telemetry, and ownership status endpoints.
- A single long-lived `jtop` telemetry client rather than one connection per
  HTTP request.

## Scope and non-goals

Resource Router is an admission service, not an orchestrator or kernel-level
resource controller. It does **not**:

- start, stop, suspend, or kill workloads;
- enforce cgroup limits, CPU affinity, memory limits, or fractional GPU quotas;
- inspect models, Docker workloads, or application-specific priorities;
- queue lease requests or provide a `PENDING` state;
- coordinate multiple nodes or multiple Uvicorn workers.

Claims are cooperative declarations. A client that bypasses the Router or
understates its peak memory demand can still cause resource pressure.

## Architecture

```text
Clients
   │
   ▼
FastAPI v1 API
   │
   ▼
ResourceRouterManager ───── SQLite state
   │
   ├── AdmissionEngine ─── lease ledger + policy
   │
   └── JtopTelemetryProvider ─── jtop.service ─── Jetson Orin
```

Jetson CPU and GPU allocations share the same physical memory. The Router
therefore maintains one unified-memory budget instead of separate RAM and VRAM
pools. Telemetry is treated as observation, while active leases are treated as
commitments; the two are checked independently to avoid double-counting.

## Requirements

- Python 3.10 or newer.
- One application process and one Uvicorn worker.
- SQLite on a local filesystem.
- On Jetson: a running `jtop.service` and a compatible `jetson-stats` client.
- For the supplied Compose deployment: Docker Compose and an external Docker
  network named `jetson-resource-plane`.

## Development

The unit and integration tests use static telemetry and do not require Jetson
hardware or `jetson-stats`.

```bash
python -m venv .venv

# Linux/macOS
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest

# Windows PowerShell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

The default application uses `JtopTelemetryProvider`. On a non-Jetson machine,
the process can start, but readiness and telemetry-dependent admissions fail
closed until a usable `jtop` service is available.

## Running on Jetson

Install and start `jetson-stats` on the host using the upstream instructions.
The client used by the Router must match the host service version because the
`jtop` protocol rejects incompatible versions.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e .
systemctl is-active jtop.service
.venv/bin/python -m app
```

The service account must be allowed to access `/run/jtop.sock`, normally by
membership in the host `jtop` group.

The native listener defaults to `127.0.0.1:19081`. Override it only when the
network boundary is understood:

```bash
JETROUTER_HOST=127.0.0.1 JETROUTER_PORT=19081 .venv/bin/python -m app
```

## Docker Compose

The included image installs `jetson-stats==7.2.2`, matching the JetPack 7.2 /
L4T 39.2 recovery deployment. Match the host service release exactly; this is
not a host upgrade command or a claim that all Jetson releases are supported.
See [`docs/deployment-jetson-stats-7.2.2.md`](docs/deployment-jetson-stats-7.2.2.md)
for the client contract, preflight checks, migration, and rollback procedure.

Copy `.env.example` to `.env` and fill in `JETROUTER_UID` and `JTOP_GID` from the
intended non-root service account and existing host socket. Compose deliberately
fails when either is missing instead of guessing a device-specific group ID.
For an older host, set `JETSON_STATS_VERSION` to its installed release and build
a separate image; do not replace or restart the host service to match this image.

```bash
export VCS_REF="$(git rev-parse HEAD)"
export JETROUTER_IMAGE="jetson-resource-router:${VCS_REF}"
docker compose config --quiet
docker compose build
# Start only in an approved maintenance window, after the documented preflight.
docker compose up -d --no-build
docker compose ps
curl --fail http://127.0.0.1:19081/health/ready
```

Before deployment, review these values in [`compose.yml`](compose.yml):

- `JETROUTER_UID` must be the intended non-root service account UID; `JTOP_GID`
  must be the host socket's permitted numeric group, normally the `jtop` group.
- `JETROUTER_IMAGE` selects the image tag; its default `jetson-resource-router:local`
  is for local builds. Use a distinct tag and record the image ID for deployments.
- `VCS_REF` records the source revision in an OCI image label. Set it before
  building; an unset value is explicitly recorded as `unknown`.
- `JETROUTER_RECLAIMABLE_CACHE_FRACTION=0.90` is a measured deployment override,
  not a universal Jetson default. Start with the application default of `0.5`
  unless measurements justify a higher value.
- The host port is published only on `127.0.0.1`. Containers on
  `jetson-resource-plane` use `http://resource-router:19081`.

The only host mounts are the read-only jtop socket and the project-local
`data/` directory. The SQLite database, WAL, and SHM files must stay together on
a local filesystem.

## Configuration

| Environment variable | Default | Description |
| --- | --- | --- |
| `JETROUTER_HOST` | `127.0.0.1` | HTTP bind address |
| `JETROUTER_PORT` | `19081` | HTTP port |
| `JETROUTER_DATABASE_PATH` | `data/jetrouter.sqlite3` | SQLite state path |
| `JETROUTER_RECLAIMABLE_CACHE_FRACTION` | `0.5` | Fraction of reported file cache counted as conservatively reclaimable; must be between `0` and `1` |

Admission policy defaults such as memory reserves, pressure thresholds, lease
TTL, and reservation safety margin are defined in
[`app/admission/policy.py`](app/admission/policy.py). They are intentionally not
all exposed as environment variables in v1.

## API overview

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/leases` | Atomically acquire a lease |
| `GET /v1/leases/{lease_id}` | Read an active lease |
| `POST /v1/leases/{lease_id}/renew` | Renew a lease TTL |
| `DELETE /v1/leases/{lease_id}` | Idempotently release a lease |
| `POST /v1/reservations` | Create a future exclusive reservation |
| `GET /v1/reservations/{reservation_id}` | Read a reservation |
| `DELETE /v1/reservations/{reservation_id}` | Cancel or release a reservation |
| `GET /v1/status` | Read telemetry, capacity, and Router state |
| `GET /health/live` | Process liveness |
| `GET /health/ready` | Admission readiness |

Example lease request:

```bash
curl --request POST http://127.0.0.1:19081/v1/leases \
  --header 'Content-Type: application/json' \
  --data '{
    "request_id": "018f5c30-0000-0000-0000-000000000001",
    "client_id": "model-router",
    "mode": "SHARED",
    "resources": {
      "memory_mb": 2048,
      "gpu": true,
      "cpu_cores": 1.0
    },
    "ttl_seconds": 60
  }'
```

Successful resource acquisition returns `GRANTED`. Contention or unsafe
admission returns `BUSY`; there is no queued or pending acquisition in v1. A
successful `request_id` is permanently bound to its normalized request for
idempotency.

## Security and operations

- The API has no authentication in v1. Keep it on loopback or a trusted private
  network and enforce access at the deployment boundary.
- `GET /v1/status` is informational. Only `POST /v1/leases` creates an
  entitlement.
- Liveness only proves that HTTP is responsive. Use readiness for admission
  availability.
- Resource Router does not protect against unmanaged processes that bypass it.
- Do not run multiple Uvicorn workers; the admission lock is process-local.

## License and third-party software

Source code authored in this repository is licensed under the Apache License,
Version 2.0. See [`LICENSE.md`](LICENSE.md).

This repository does not vendor third-party source code or binary packages.
Dependencies installed by package managers retain their own licenses:

| Component | Role | Upstream license |
| --- | --- | --- |
| [FastAPI](https://github.com/fastapi/fastapi) | HTTP API framework | MIT |
| [Pydantic](https://github.com/pydantic/pydantic) | Validation and data models | MIT |
| [Uvicorn](https://github.com/encode/uvicorn) | ASGI server | BSD-3-Clause |
| [jetson-stats](https://github.com/rbonghi/jetson_stats) | Jetson `jtop` telemetry client and host service | AGPL-3.0-or-later |

The Apache-2.0 license for this repository does not relicense those components.
In particular, the supplied Dockerfile installs `jetson-stats==7.2.2`, and the
Router imports its `jtop` client in-process. Anyone distributing a prebuilt
image or another combined distribution is responsible for satisfying the
licenses of `jetson-stats`, all other installed packages, and the base image.
This summary is informational and is not legal advice.
