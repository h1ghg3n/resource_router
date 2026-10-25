#!/usr/bin/env python3
"""Prove that a stable front socket follows a replaced upstream socket."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path


class UnixReplyServer:
    """Own a temporary Unix listener that prefixes one reply per connection."""

    def __init__(self, path: Path, label: bytes) -> None:
        """Bind ``path`` and start the background reply loop.

        ``path`` must not already exist and ``label`` must be bytes suitable for
        a socket response. Construction creates the socket node and starts one
        daemon thread; bind or thread-start failures are surfaced to the caller.
        The caller owns shutdown through :meth:`close`.
        """
        self.path = path
        self.label = label
        self.stop_event = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.settimeout(0.1)
        self.listener.bind(str(path))
        self.listener.listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        """Accept and answer connections until shutdown is requested.

        Each connection receives ``label + b":" + payload``. Socket failures
        other than the expected accept timeout terminate the daemon thread.
        """
        while not self.stop_event.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            with connection:
                payload = connection.recv(1024)
                connection.sendall(self.label + b":" + payload)

    def close(self, preserve_as: Path | None = None) -> None:
        """Stop the reply loop, close the listener, and handle its socket node.

        When ``preserve_as`` is omitted the node is removed. Otherwise an
        existing node is renamed to that path so inode replacement remains
        observable. Cleanup errors are surfaced and the method is intended for
        one call per server instance.
        """
        self.stop_event.set()
        self.thread.join(timeout=1)
        self.listener.close()
        if preserve_as is None:
            self.path.unlink(missing_ok=True)
        elif self.path.exists():
            self.path.replace(preserve_as)


def request(path: Path, payload: bytes) -> bytes:
    """Send one payload to ``path`` and return the first response chunk.

    ``path`` must name a connectable Unix stream socket and ``payload`` must be
    bytes. The temporary client has a three-second timeout; connection, send,
    timeout, and receive failures are exposed to the caller.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path))
        client.sendall(payload)
        return client.recv(1024)


def wait_for(path: Path, timeout: float = 3) -> None:
    """Wait for a socket path to appear or raise after ``timeout`` seconds.

    The timeout must be positive. This function only observes path existence
    and returns ``None`` without opening or mutating the socket.
    """
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"socket was not created: {path}")
        time.sleep(0.02)


def main() -> int:
    """Run the isolated proxy replacement proof and return zero on success.

    The proof creates temporary sockets, launches one local systemd proxy
    process, replaces its upstream listener, and verifies that the front inode
    remains stable while replies switch upstream generations. Any mismatch or
    process cleanup failure raises; the host jtop service is never contacted.
    """
    with tempfile.TemporaryDirectory(prefix="jtop-proxy-proof-") as directory:
        root = Path(directory)
        upstream_path = root / "upstream.sock"
        old_upstream_path = root / "upstream-old.sock"
        front_path = root / "front.sock"
        upstream = UnixReplyServer(upstream_path, b"first")
        process = subprocess.Popen(
            [
                "systemd-socket-activate",
                f"--listen={front_path}",
                "--fdname=proxy",
                "/usr/lib/systemd/systemd-socket-proxyd",
                "--connections-max=16",
                "--exit-idle-time=30s",
                str(upstream_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for(front_path)
            front_inode = front_path.stat().st_ino
            first_upstream_inode = upstream_path.stat().st_ino
            first = request(front_path, b"one")

            # Keep the old node linked so the filesystem cannot recycle its
            # inode for the replacement and hide the replacement event.
            upstream.close(old_upstream_path)
            upstream = UnixReplyServer(upstream_path, b"second")
            second_upstream_inode = upstream_path.stat().st_ino
            second = request(front_path, b"two")

            result = {
                "front_inode_stable": front_inode == front_path.stat().st_ino,
                "upstream_inode_changed": first_upstream_inode != second_upstream_inode,
                "first_response": first.decode(),
                "second_response": second.decode(),
            }
            expected = {
                "front_inode_stable": True,
                "upstream_inode_changed": True,
                "first_response": "first:one",
                "second_response": "second:two",
            }
            if result != expected:
                raise RuntimeError(json.dumps(result, sort_keys=True))
            print(json.dumps(result, sort_keys=True))
            return 0
        finally:
            upstream.close()
            old_upstream_path.unlink(missing_ok=True)
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.returncode not in (-15, 0):
                error = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"proxy exited {process.returncode}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
