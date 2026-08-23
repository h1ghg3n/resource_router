from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.errors import TelemetryUnavailableError
from app.telemetry.snapshot import ResourceSnapshot


@runtime_checkable
class TelemetryProvider(Protocol):
    def read(self) -> ResourceSnapshot:
        """Return the latest normalized telemetry snapshot."""

    def close(self) -> None:
        """Release provider resources. Implementations must be idempotent."""


class StaticTelemetryProvider:
    """Mutable provider intended for tests and local policy simulations."""

    def __init__(self, snapshot: ResourceSnapshot) -> None:
        self._snapshot = snapshot

    def read(self) -> ResourceSnapshot:
        return self._snapshot

    def set_snapshot(self, snapshot: ResourceSnapshot) -> None:
        self._snapshot = snapshot

    def close(self) -> None:
        pass


class UnavailableTelemetryProvider:
    """Startup-safe placeholder used until a platform adapter is configured."""

    def read(self) -> ResourceSnapshot:
        raise TelemetryUnavailableError("no telemetry provider is configured")

    def close(self) -> None:
        pass
