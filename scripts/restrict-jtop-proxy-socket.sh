#!/bin/bash
# Constrain only this user's fixed runtime socket; never touch jtop's upstream.
set -euo pipefail
TASK_SOCKET="${XDG_RUNTIME_DIR:?A user runtime directory is required}/jetson-jtop-proxy.sock"
for _attempt in $(seq 1 50); do
  if [[ -S "$TASK_SOCKET" && ! -L "$TASK_SOCKET" ]]; then
    [[ "$(stat -c '%u' -- "$TASK_SOCKET")" == "$(id -u)" ]]
    chmod 0600 -- "$TASK_SOCKET"
    exit 0
  fi
  sleep 0.1
done
printf 'owned proxy socket was not created\n' >&2
exit 1
