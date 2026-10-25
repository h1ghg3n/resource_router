# Bounded jtop recovery and optional stable socket

The provider owns one jtop client at a time. A failed generation is closed and
replaced once during the same read; a second failure returns telemetry unavailable.
Concurrent foreground reads share its lock. CPU busy is the mean of valid interval
values for all online cores, with null on incomplete/invalid data. This does not
change admission core commitments or modify the host jetson-stats package.

## Why an optional proxy

A direct container bind mount can retain an old Unix socket inode after jtop
recreates `/run/jtop.sock`. The user service holds a stable front socket and
forwards new connections to the current upstream path. It neither controls the
workload nor restarts jtop. The supplied proxy service requires systemd tools at
the paths in its unit and an existing `jtop` group; verify these for your platform.

All host operations below are explicit operator actions. No setup runs at package
import, API startup, or image build. Do not use these commands during active leases.
Keep your existing Compose project name, database mount and deployment identity.

## Install the example as the Router's non-root host user

```bash
install -d -m 0700 "$HOME/.local/libexec/jetson-resource-router"
install -m 0700 scripts/restrict-jtop-proxy-socket.sh \
  "$HOME/.local/libexec/jetson-resource-router/restrict-jtop-proxy-socket.sh"
systemctl --user link "$PWD/deploy/systemd-user/jetson-jtop-proxy.service"
systemctl --user daemon-reload
systemctl --user enable --now jetson-jtop-proxy.service
```

The service uses `sg jtop` to open the upstream with that group. The front socket
is owner-only mode0600 and must be owned by the UID configured as `JETROUTER_UID`.
The parent user runtime directory must permit that user. Do not fix access by
making either socket world-writable or changing the upstream socket's permissions.
If unattended user services are required, the host administrator must separately
review user lingering; this repository does not enable it automatically.

Set `JETROUTER_PROXY_SOCKET` to the absolute front socket path in your runtime
directory; preserve `JETROUTER_UID`/`JTOP_GID` from your existing deployment.

```bash
export JETROUTER_PROXY_SOCKET="$XDG_RUNTIME_DIR/jetson-jtop-proxy.sock"
test -S "$JETROUTER_PROXY_SOCKET"
docker compose -f compose.yml -f compose.jtop-proxy.yml config --quiet
# After confirming idle work and retaining a rollback image/configuration:
docker compose -f compose.yml -f compose.jtop-proxy.yml up -d --no-deps resource-router
```

Only the socket mount changes; `./data:/app/data` is inherited unchanged. Neither
overlay contains an operator's home directory, fixed UID or absolute database path.
Use the same file list and Compose project for subsequent commands.

Ordinary upstream jtop restarts can recover with the same proxy front inode.
Restarting the **proxy** itself replaces that front inode and requires the
operator to recreate the Router container after rechecking idle state. No
automatic container lifecycle is implemented. A platform-specific live restart
acceptance is still required for a new installation; synthetic CI is not that proof.

## Return to a direct mount

After checking idle leases, append `compose.jtop-direct-rollback.yml` to your
existing Compose file list and recreate only `resource-router`. It replaces the
socket target with `/run/jtop.sock` and does not change the data volume. Keep live
SQLite/WAL/SHM in place; do not restore a database backup as an automatic rollback.

## Synthetic Linux proof

`python scripts/verify-jtop-socket-proxy.py` creates temporary front/upstream
sockets, replaces only its own upstream, checks unchanged front inode and changed
replies, then terminates only its owned proxy. It never contacts `/run/jtop.sock`.
