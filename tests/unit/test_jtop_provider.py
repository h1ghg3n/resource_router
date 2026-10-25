from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.application as application_module
from app.admission.engine import AdmissionEngine
from app.admission.policy import AdmissionPolicy
from app.core.errors import TelemetryUnavailableError
from app.leases.manager import ResourceRouterManager
from app.persistence.sqlite import SQLiteStateRepository
from app.settings import Settings
from app.telemetry.jtop import JtopTelemetryProvider, _cpu_metrics
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
        ok_error: Exception | None = None,
    ) -> None:
        """Configure isolated telemetry and failure injection counters without contacting jtop or a host."""
        self.memory = memory
        self.cpu = {} if cpu is None else cpu
        self.gpu = {} if gpu is None else gpu
        self.temperature = {} if temperature is None else temperature
        self.stats = {} if stats is None else stats
        self.running = running
        self.start_error = start_error
        self.ok_error = ok_error
        self.start_count = 0
        self.close_count = 0
        self.ok_calls: list[bool] = []

    def start(self) -> None:
        self.start_count += 1
        if self.start_error is not None:
            raise self.start_error

    def ok(self, spin: bool = False) -> bool:
        """Return injected liveness or raise the configured failure while recording the requested read mode."""
        self.ok_calls.append(spin)
        if self.ok_error is not None:
            raise self.ok_error
        return self.running

    def close(self) -> None:
        self.close_count += 1


def test_normalizes_jetson_stats_metrics() -> None:
    """Verify normalizes jetson stats metrics using isolated inputs; assertion failure rejects the regression."""
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
                {"online": True, "idle": 50},
                {"online": False},
                {"online": True, "idle": 75},
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


def test_cpu_load_follows_online_core_intervals_not_stuck_aggregate() -> None:
    """Track changing core load despite the known faulty, nearly fixed total."""

    cpu = {
        "cpu": [
            {"online": True, "idle": 100},
            {"online": True, "idle": 80},
            {"online": False, "idle": 0},
        ],
        "total": {"idle": 94.5},
    }
    assert _cpu_metrics(cpu) == (3, 2, 10.0)
    cpu["cpu"][0]["idle"] = 0
    cpu["cpu"][1]["idle"] = 20
    assert _cpu_metrics(cpu) == (3, 2, 90.0)
    cpu.pop("total")
    assert _cpu_metrics(cpu) == (3, 2, 90.0)


@pytest.mark.parametrize("invalid_idle", [None, True, "50", -1, 101, float("nan")])
def test_partial_online_core_load_does_not_fall_back_to_aggregate(
    invalid_idle: object,
) -> None:
    """Keep missing/invalid online measurements unavailable, never bias a mean."""

    assert _cpu_metrics(
        {
            "cpu": [
                {"online": True, "idle": 50},
                {"online": True, "idle": invalid_idle},
            ],
            "total": {"idle": 94.5},
        }
    ) == (2, 2, None)


@pytest.mark.parametrize("cores", [None, [], "cpu", [None], [{"online": "yes"}]])
def test_unknown_core_topology_does_not_use_aggregate(cores: object) -> None:
    """Reject incomplete topology without reporting a false zero or total."""

    assert _cpu_metrics({"cpu": cores, "total": {"idle": 94.5}})[2] is None


def test_all_offline_cpu_load_is_unavailable() -> None:
    """Avoid division by zero and exclude obsolete values on offline cores."""

    assert _cpu_metrics({"cpu": [{"online": False, "idle": 0}], "total": {"idle": 94.5}}) == (
        1,
        0,
        None,
    )


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


def test_invalid_required_memory_is_replaced_in_same_read() -> None:
    """Verify invalid required memory is replaced in same read using isolated inputs; assertion failure rejects the regression."""
    broken = FakeJtopClient(memory={"RAM": {"tot": 8192}})
    healthy = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 4096}})
    clients = iter((broken, healthy))
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: next(clients),
    )

    assert provider.read().memory_free_mb == 4

    assert broken.start_count == 1
    assert broken.close_count == 1
    assert healthy.start_count == 1


def test_lost_running_client_is_replaced_in_same_read() -> None:
    """Verify lost running client is replaced in same read using isolated inputs; assertion failure rejects the regression."""
    original = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 4096}})
    replacement = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 3072}})
    clients = iter((original, replacement))
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: next(clients),
    )
    assert provider.read().memory_free_mb == 4

    original.ok_error = ConnectionResetError("upstream socket was replaced")

    assert provider.read().memory_free_mb == 3
    assert original.close_count == 1
    assert replacement.start_count == 1


def test_concurrent_reads_replace_a_failed_generation_once() -> None:
    """Verify concurrent reads replace a failed generation once using isolated inputs; assertion failure rejects the regression."""
    original = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 4096}})
    replacement = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 3072}})
    clients = iter((original, replacement))
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: next(clients),
    )
    assert provider.read().memory_free_mb == 4
    original.ok_error = ConnectionResetError("upstream socket was replaced")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _index: provider.read(), range(4)))

    assert [snapshot.memory_free_mb for snapshot in results] == [3, 3, 3, 3]
    assert original.close_count == 1
    assert replacement.start_count == 1
    assert replacement.ok_calls == [True, True, True, True]


def test_two_stopped_generations_are_bounded_and_unavailable() -> None:
    """Verify two stopped generations are bounded and unavailable using isolated inputs; assertion failure rejects the regression."""
    stopped = [
        FakeJtopClient(
            memory={"RAM": {"tot": 8192, "free": 4096}},
            running=False,
        )
        for _index in range(2)
    ]
    clients = iter(stopped)
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: next(clients),
    )

    with pytest.raises(TelemetryUnavailableError, match="generation 2.*not running"):
        provider.read()

    assert [client.close_count for client in stopped] == [1, 1]


def test_two_start_failures_close_partial_clients_and_stop() -> None:
    """Verify two start failures close partial clients and stop using isolated inputs; assertion failure rejects the regression."""
    failed = [
        FakeJtopClient(
            memory={"RAM": {"tot": 8192, "free": 4096}},
            start_error=RuntimeError(f"service unavailable {index}"),
        )
        for index in range(2)
    ]
    clients = iter(failed)
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: next(clients),
    )

    with pytest.raises(
        TelemetryUnavailableError,
        match="generation 2.*service unavailable 1",
    ):
        provider.read()

    assert [client.close_count for client in failed] == [1, 1]


def test_close_is_idempotent_and_prevents_reuse() -> None:
    client = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 4096}})
    provider = JtopTelemetryProvider(client_factory=lambda _interval: client)
    provider.read()

    provider.close()
    provider.close()

    assert client.close_count == 1
    with pytest.raises(TelemetryUnavailableError, match="closed"):
        provider.read()


def test_successful_read_offers_sanitized_record_without_affecting_snapshot() -> None:
    """Verify successful read offers sanitized record without affecting snapshot using isolated inputs; assertion failure rejects the regression."""
    records: list[dict[str, object]] = []
    client = FakeJtopClient(
        memory={"RAM": {"tot": 8192, "free": 4096}},
        gpu={"gpu": {"status": {"load": 25}}},
        stats={"VDD_IN": 12000, "prompt": "must-not-copy"},
    )
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: client,
        record_observer=records.append,
    )

    assert provider.read().memory_free_mb == 4

    assert len(records) == 1
    assert records[0]["schema_version"] == 1
    assert records[0]["power"] == {"VDD_IN": 12000.0}
    assert "must-not-copy" not in str(records[0])


def test_observer_failure_never_changes_foreground_read() -> None:
    """Verify observer failure never changes foreground read using isolated inputs; assertion failure rejects the regression."""
    client = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 4096}})

    def fail(_record: dict[str, object]) -> None:
        """Inject an observer failure to prove optional reporting cannot invalidate the foreground snapshot."""
        raise OSError("disk unavailable")

    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: client,
        record_observer=fail,
    )

    assert provider.read().memory_free_mb == 4


def test_try_read_contains_two_failed_generations() -> None:
    """Verify try read contains two failed generations using isolated inputs; assertion failure rejects the regression."""
    stopped = [
        FakeJtopClient(
            memory={"RAM": {"tot": 8192, "free": 4096}},
            running=False,
        )
        for _index in range(2)
    ]
    clients = iter(stopped)
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: next(clients),
    )

    assert provider.try_read() is None
    assert [client.close_count for client in stopped] == [1, 1]


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


def test_invalid_required_memory_discards_client_and_reconnects() -> None:
    """Replace malformed memory once within the same read, retaining only the healthy client's data."""
    broken = FakeJtopClient(memory={"RAM": {"tot": 8192}})
    healthy = FakeJtopClient(memory={"RAM": {"tot": 8192, "free": 4096}})
    clients = iter((broken, healthy))
    provider = JtopTelemetryProvider(
        client_factory=lambda _interval: next(clients),
    )

    assert provider.read().memory_free_mb == 4
    assert broken.close_count == 1
    assert provider.read().memory_free_mb == 4
    assert healthy.start_count == 1


def test_stopped_client_is_unavailable_and_retried_later() -> None:
    """Exhaust exactly two failed client generations without returning stale telemetry."""
    stopped = FakeJtopClient(
        memory={"RAM": {"tot": 8192, "free": 4096}},
        running=False,
    )
    provider = JtopTelemetryProvider(client_factory=lambda _interval: stopped)

    with pytest.raises(TelemetryUnavailableError, match="not running"):
        provider.read()

    assert stopped.close_count == stopped.start_count == 2


def test_start_failure_closes_partial_client() -> None:
    """Close both partially started generations and fail closed after the single replacement."""
    client = FakeJtopClient(
        memory={"RAM": {"tot": 8192, "free": 4096}},
        start_error=RuntimeError("service unavailable"),
    )
    provider = JtopTelemetryProvider(client_factory=lambda _interval: client)

    with pytest.raises(TelemetryUnavailableError, match="service unavailable"):
        provider.read()

    assert client.close_count == client.start_count == 2


def test_jetson_stats_7_2_2_property_contract() -> None:
    # Synthetic values using upstream 7.2.2 jtop property shapes; no hardware,
    # captured user telemetry, or upstream source is vendored in this fixture.
    fixture = Path(__file__).parents[1] / "fixtures" / "jetson_stats_7_2_2.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    client = FakeJtopClient(**data, stats={"time": now})
    provider = JtopTelemetryProvider(client_factory=lambda _interval: client)
    try:
        snapshot = provider.read()
        assert snapshot.observed_at == now
        assert snapshot.memory_total_mb == 15360
        assert snapshot.memory_free_mb == 4096
        assert snapshot.memory_cached_mb == 7168
        assert snapshot.memory_buffers_mb == 21
        assert snapshot.memory_gpu_shared_mb == 96
        assert snapshot.memory_lfb_mb == 0
        assert snapshot.cpu_total_cores == snapshot.cpu_online_cores == 8
        assert snapshot.cpu_load_percent == 3.25
        assert snapshot.gpu_load_percent == snapshot.emc_load_percent == 0
        assert snapshot.temperature_max_c == 68.25
    finally:
        provider.close()


@pytest.mark.parametrize(
    "connection_error",
    [PermissionError("socket access denied"), RuntimeError("incompatible jtop version")],
    ids=["socket-permission", "service-client-version"],
)
def test_jtop_connection_errors_fail_api_admission_closed(connection_error) -> None:
    """Verify jtop connection errors fail api admission closed using isolated inputs; assertion failure rejects the regression."""
    client = FakeJtopClient(memory={}, start_error=connection_error)
    provider = JtopTelemetryProvider(client_factory=lambda _interval: client)
    manager = ResourceRouterManager(
        provider,
        AdmissionEngine(AdmissionPolicy()),
        repository=SQLiteStateRepository(Path(":memory:")),
    )
    with TestClient(application_module.create_app(manager)) as api:
        assert api.get("/health/live").status_code == 200
        assert api.get("/health/ready").status_code == 503
        assert client.start_count == client.close_count == 2
        response = api.post(
            "/v1/leases",
            json={
                "request_id": "018f5c30-0000-0000-0000-000000000001",
                "client_id": "deployment-contract-test",
                "mode": "SHARED",
                "resources": {"memory_mb": 1024, "gpu": True, "cpu_cores": 1},
                "ttl_seconds": 60,
            },
        )
        assert response.status_code == 503
        assert response.json()["code"] == "TELEMETRY_UNAVAILABLE"
    assert client.close_count == client.start_count == 4
