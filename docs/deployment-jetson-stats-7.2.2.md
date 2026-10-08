# Deployment contract: jetson-stats 7.2.2

This deployment update preserves the frozen [v1 API](api-contract-v1.md),
including recovery of resident state after uncertain storage mutations and
EXCLUSIVE conflict reasons. There is no database schema migration, new endpoint,
telemetry polling change, or workload control in this update.

## Client and image contract

- The Dockerfile and Compose default to the exact `jetson-stats==7.2.2` package.
  The host `jtop.service` and in-container client must use the same release.
  A different host release requires an explicit build argument and separate tag.
- Upstream [7.2.2 source](https://github.com/rbonghi/jetson_stats/tree/7.2.2)
  is commit `d0a366f439ca28d5bc4dd2f85861110fd90fe546`. Its
  [L4T mapping](https://github.com/rbonghi/jetson_stats/blob/7.2.2/jtop/core/jetson_variables.py)
  includes 39.2.0, 39.2.1, and 39.2.2. This mapping alone is not hardware validation.
- Container Python 3.10 is independent of the host's Python version; the client
  communicates over `/run/jtop.sock`. No CUDA runtime or GPU device mount is needed
  for this admission service.
- The existing single long-lived client and normalization remain unchanged.
  RAM fields are KiB converted to integer MiB, `lfb` counts 4 MiB blocks,
  CPU load is `100 - idle`, GPU load comes from `status.load`, and maximum
  temperature includes online sensors only. Missing optional metrics remain null.
  See the upstream [property contract](https://github.com/rbonghi/jetson_stats/blob/7.2.2/jtop/jtop.py).
- Missing socket, permission failure, incompatible service/client, and malformed
  required memory telemetry fail admission closed. Liveness may still succeed;
  readiness must succeed before enabling clients.
- Compose requires numeric `JETROUTER_UID` and `JTOP_GID`. It retains a read-only
  socket bind and a writable data bind, without privileged mode, host PID access,
  Docker socket mounts, or new capabilities. A read-only bind does not make the
  jtop IPC protocol read-only; application access remains limited to observations.
- Image source files are made readable/traversable for the configured UID inside
  the image. Host files, socket permissions, groups, and daemons are not changed.
- `JETROUTER_IMAGE` is a configurable tag, not a fixed recovery image reference.
  Set `VCS_REF` to the full commit and record the built image ID. Dependency ranges
  and the base-image tag still float; this change does not promise byte-identical
  builds. Archive the tested image or pin its digest for a reproducible rollback.

## Preflight (read-only)

Run on the intended host, without installing or restarting anything:

```bash
systemctl is-active jtop.service
python3 -c 'from importlib.metadata import version; print(version("jetson-stats"))'
getent group jtop
stat -c 'socket mode=%a uid=%u gid=%g' /run/jtop.sock
test -S /run/jtop.sock
docker network inspect jetson-resource-plane --format '{{.Name}}'
```

If the service uses a different Python environment, query its installed package
there. Do not use the host version query as proof of the container version.
The group must have socket access and its numeric GID must agree with `stat`.
Resolve a mismatch before deployment; do not grant access with `chmod 666`,
privileged mode, or an automatic group/service change.

Copy `.env.example` to `.env`; record the intended service UID and the existing
socket GID. Both are deliberately blank in the example. Before building, export
the same values in the shell for the following validation:

```bash
# Set these to the observed values, not copied device-specific defaults.
export JETROUTER_UID=YOUR_NON_ROOT_SERVICE_UID
export JTOP_GID=YOUR_EXISTING_JTOP_GID
test "$JETROUTER_UID" -gt 0
test "$JTOP_GID" -gt 0
test "$JTOP_GID" = "$(getent group jtop | cut -d: -f3)"
test "$JTOP_GID" = "$(stat -c '%g' /run/jtop.sock)"
export VCS_REF="$(git rev-parse HEAD)"
export JETROUTER_IMAGE="jetson-resource-router:${VCS_REF}"
docker compose config --quiet
docker compose build
docker image inspect "$JETROUTER_IMAGE" --format '{{.Id}} {{json .Config.Labels}}'
docker run --rm --user "$JETROUTER_UID:$JTOP_GID" "$JETROUTER_IMAGE" \
  python -c 'from importlib.metadata import version; import app.api.application; print(version("jetson-stats"))'
```

These build/import checks do not contact the socket or validate a Jetson sensor.
Keep the existing service UID where possible, and verify it can write the existing
`data/` directory and SQLite/WAL/SHM files. For a fresh deployment only, an operator
may create `data/` with mode 0770 owned by the chosen UID/GID. Do not recursively
change ownership of existing state without reviewing that specific change.
Create the external network only if it is absent and that host change is approved.

## Migration in an approved maintenance window

1. Keep the old Compose configuration, environment settings, source revision,
   image ID/tag, and socket group information outside the new build context.
   Retain the old image. Do not reuse its tag for the candidate.
2. Pause new client work and wait for active leases and reservations to finish.
   Verify Router ownership/state as well as counts: a scheduled reservation can
   matter even when the active lease count is zero. Never terminate managed work
   merely to deploy this update.
3. Stop only Resource Router, then back up its complete `data/` directory while
   stopped. Preserve SQLite, WAL, SHM, and permissions together. Do not copy only
   the main database from a live process.
4. Use the validated UID/GID, distinct image tag, and unchanged data directory.
   Run `docker compose up -d --no-build` for this service. Do not restart jtop,
   change host socket permissions, or start multiple Router workers.
5. Verify live and ready endpoints, status freshness, plausible memory/CPU values,
   and container package version. Confirm loopback publishing, the read-only
   socket bind, data ownership, and the expected source revision/image ID.
   Resume clients only after these checks pass. GPU workload tests are separate.

## Rollback

If readiness fails, keep clients paused. Stop the candidate and select the retained
old image and its matching configuration; an old 4.3.2 client is not a valid rollback
against a 7.2.2 host service. For a recovered 7.2.2 deployment, retain its known-good
7.2.2 image as the fallback. Leave host jtop unchanged.

There is no schema migration, so use the current durable ledger when returning to
the old image. Restoring the pre-deployment data backup is safe only if no new
leases/reservations or mutations have occurred since that backup; otherwise it
could lose grants and idempotency history. Never delete state to force readiness.
Verify readiness/status again before resuming clients.

## Validation boundaries and follow-up

CI runs the full suite, including storage-fault recovery and EXCLUSIVE conflicts,
plus synthetic 7.2.2 property-shape and connection-failure regression tests. Image
jobs build on native AMD64 and ARM64 GitHub runners and verify a non-root process
with writable temporary state, liveness, and fail-closed readiness without jtop.
Compose interpolation is checked for missing identity values and socket mount
permissions. CI does not prove real Jetson IPC, sensors, or workload admission.

After an approved deployment, real-host verification must cover UID/GID/socket
access, exact client/service versions, live telemetry and readiness, and preservation
of the durable ledger. A telemetry UI, time-series storage, Prometheus export, and
changes to admission locking remain separate follow-up work.
