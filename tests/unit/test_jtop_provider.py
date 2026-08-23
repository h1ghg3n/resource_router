from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.application as application_module
from app.core.errors import TelemetryUnavailableError
from app.settings import Settings
from app.telemetry.jtop import JtopTelemetryProvider
from app.telemetry.provider import StaticTelemetryProvider
from app.telemetry.snapshot import ResourceSnapshot

UTC = timezone.utc


class FakeJtopClient:
    def __init__(
        self,
        *,
        memory: object,
        cpu: object | None = None,
        gpu: object | None = None,
        temperature: object | None = None,
        stats: object | None = None,
        running: bool = True,
        start_error: Exception | None = None,
    ) -> None:
        self.memory = memory
        self.cpu = {} if cpu is None else cpu
        self.gpu = {} if gpu is None else gpu
        self.temperature = {} if temperature is None else temperature
        self.stats = {} if stats is None else stats
        self.running = running
        self.start_error = start_error
        self.start_count = 0
        self.close_count = 0
        self.ok_calls: list[bool] = []

    def start(self) -> None:
        self.start_count += 1
        if self.start_error is not None:
            raise self.start_error

    def ok(self, spin: bool = False) -> bool:
        self.ok_calls.append(spin)
        return self.running

    def close(self) -> None:
        self.close_count += 1


def test_normalizes_jetson_stats_metrics() -> None:
    observed_at = datetime(2026, 8, 13, 23, 10, tzinfo=UTC)
    client = FakeJtopClient(
        memory={
            "RAM": {
                "tot": 8 * 1024 + 512,
                "free": 3 * 1024 + 900,
                "cached": 1536,
                "buffers": 1023,
                "shared": 2048,
                "lfb": 5,
            },
            "EMC": {"online": True, "val": 31},
        },
        cpu={
            "cpu": [
                {"online": True},
                {"online": False},
                {"online": True},
            ],
            "total": {"idle": 62.5},
        },
        gpu={
            "integrated": {"status": {"load": 42}},
            "other": {"status": {"load": 73.25}},
        },
        temperature={
            "cpu": {"online": True, "temp": 61.5},
            "gpu": {"online": True, "temp": 64},
            "offline": {"online": False, "temp": 200},
        },
        stats={"time": observed_at},
    )
    intervals: list[float] = []

    def factory(interval: float) -> FakeJtopClient:
        intervals.append(interval)
        return client

    provider = JtopTelemetryProvider(client_factory=factory)

    snapshot = provider.read()

    assert intervals == [1.0]
    assert client.start_count == 1
    assert client.ok_calls == [True]
    assert snapshot.observed_at == observed_at
    assert snapshot.memory_total_mb == 8
    assert snapshot.memory_free_mb == 3
    assert snapshot.memory_cached_mb == 1
    assert snapshot.memory_buffers_mb == 0
    assert snapshot.memory_gpu_shared_mb == 2
    assert snapshot.memory_lfb_mb == 20
    assert snapshot.cpu_total_cores == 3
    assert snapshot.cpu_online_cores == 2
    assert snapshot.cpu_load_percent == 37.5
    assert snapshot.gpu_load_percent == 73.25
    assert snapshot.emc_load_percent == 31
    assert snapshot.temperature_max_c == 64

    provider.read()
    assert client.start_count == 1


def test_missing_optional_metrics_are_null() -> None:
    now = datetime(2026, 8, 13, 23, 15, tzinfo=UTC)
    client = FakeJtopClient(
        memory={"RAM": {"tot": 8192, "free": 4096}},
        cpu={"cpu": [{"online": True}], "total": {}},
        gpu={"gpu": {"status": {}}},
        temperature={"offline": {"online": False, "temp": -256}},
    )
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: client,
        clock=lambda: now,
    )

    snapshot = provider.read()

    assert snapshot.observed_at == now
    assert snapshot.memory_cached_mb is None
    assert snapshot.memory_buffers_mb is None
    assert snapshot.memory_gpu_shared_mb is None
    assert snapshot.memory_lfb_mb is None
    assert snapshot.cpu_total_cores == 1
    assert snapshot.cpu_online_cores == 1
    assert snapshot.cpu_load_percent is None
    assert snapshot.gpu_load_percent is None
    assert snapshot.emc_load_percent is None
    assert snapshot.temperature_max_c is None


def test_malformed_optional_metrics_do_not_invent_zeroes() -> None:
    client = FakeJtopClient(
        memory={
            "RAM": {
                "tot": 8192,
                "free": 4096,
                "cached": -1,
                "buffers": "unknown",
                "shared": True,
                "lfb": float("nan"),
            },
            "EMC": {"online": False, "val": 88},
        },
        cpu={"cpu": [{"online": "yes"}], "total": {"idle": 101}},
        gpu={"gpu": {"status": {"load": -1}}},
        temperature={"cpu": {"online": True, "temp": float("nan")}},
    )
    provider = JtopTelemetryProvider(client_factory=lambda _interval: client)

    snapshot = provider.read()

    assert snapshot.memory_cached_mb is None
    assert snapshot.memory_buffers_mb is None
    assert snapshot.memory_gpu_shared_mb is None
    assert snapshot.memory_lfb_mb is None
    assert snapshot.cpu_total_cores == 1
    assert snapshot.cpu_online_cores is None
    assert snapshot.cpu_load_percent is None
    assert snapshot.gpu_load_percent is None
    assert snapshot.emc_load_percent is None
    assert snapshot.temperature_max_c is None


def test_invalid_required_memory_discards_client_and_reconnects() -> None:
    broken = FakeJtopClient(memory={"RAM": {"tot": 8192}})
    healthy = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 4096}})
    clients = iter((broken, healthy))
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: next(clients),
    )

    with pytest.raises(TelemetryUnavailableError, match="RAM.free"):
        provider.read()

    assert broken.close_count == 1
    assert provider.read().memory_free_mb == 4
    assert healthy.start_count == 1


def test_stopped_client_is_unavailable_and_retried_later() -> None:
    stopped = FakeJtopClient(
        memory={"RAM": {"tot": 8192, "free": 4096}},
        running=False,
    )
    provider = JtopTelemetryProvider(client_factory=lambda _interval: stopped)

    with pytest.raises(TelemetryUnavailableError, match="not running"):
        provider.read()

    assert stopped.close_count == 1


def test_start_failure_closes_partial_client() -> None:
    client = FakeJtopClient(
        memory={"RAM": {"tot": 8192, "free": 4096}},
        start_error=RuntimeError("service unavailable"),
    )
    provider = JtopTelemetryProvider(client_factory=lambda _interval: client)

    with pytest.raises(TelemetryUnavailableError, match="service unavailable"):
        provider.read()

    assert client.close_count == 1


def test_close_is_idempotent_and_prevents_reuse() -> None:
    client = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 4096}})
    provider = JtopTelemetryProvider(client_factory=lambda _interval: client)
    provider.read()

    provider.close()
    provider.close()

    assert client.close_count == 1
    with pytest.raises(TelemetryUnavailableError, match="closed"):
        provider.read()


def test_default_application_wires_and_closes_jtop_provider(monkeypatch) -> None:
    now = datetime.now(UTC)

    class TrackingProvider(StaticTelemetryProvider):
        def __init__(self) -> None:
            super().__init__(
                ResourceSnapshot(
                    observed_at=now,
                    memory_total_mb=8192,
                    memory_free_mb=4096,
                    memory_cached_mb=0,
                    memory_buffers_mb=None,
                    memory_gpu_shared_mb=None,
                    memory_lfb_mb=None,
                    cpu_total_cores=None,
                    cpu_online_cores=None,
                    cpu_load_percent=None,
                    gpu_load_percent=None,
                    emc_load_percent=None,
                    temperature_max_c=None,
                )
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    telemetry = TrackingProvider()
    monkeypatch.setattr(
        application_module,
        "JtopTelemetryProvider",
        lambda: telemetry,
    )

    with TestClient(
        application_module.create_app(
            settings=Settings(database_path=Path(":memory:")),
        )
    ) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200

    assert telemetry.closed is True


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_invalid_interval_is_rejected(interval: float) -> None:
    with pytest.raises(ValueError, match="interval_seconds"):
        JtopTelemetryProvider(interval_seconds=interval)
