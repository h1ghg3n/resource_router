from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.admission.engine import AdmissionEngine
from app.admission.policy import AdmissionPolicy
from app.api.application import create_app
from app.core.errors import TelemetryUnavailableError
from app.leases.manager import ResourceRouterManager
from app.telemetry.provider import StaticTelemetryProvider
from app.telemetry.snapshot import ResourceSnapshot

UTC = timezone.utc


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class SwitchableTelemetryProvider(StaticTelemetryProvider):
    def __init__(self, initial_snapshot: ResourceSnapshot) -> None:
        super().__init__(initial_snapshot)
        self.unavailable = False

    def read(self) -> ResourceSnapshot:
        if self.unavailable:
            raise TelemetryUnavailableError("telemetry is unavailable for this test")
        return super().read()


def snapshot(now: datetime, **changes: object) -> ResourceSnapshot:
    baseline = ResourceSnapshot(
        observed_at=now,
        memory_total_mb=8192,
        memory_free_mb=8192,
        memory_cached_mb=0,
        memory_buffers_mb=0,
        memory_gpu_shared_mb=0,
        memory_lfb_mb=None,
        cpu_total_cores=8,
        cpu_online_cores=8,
        cpu_load_percent=0,
        gpu_load_percent=0,
        emc_load_percent=None,
        temperature_max_c=None,
    )
    return replace(baseline, **changes)


def manager_for(
    clock: MutableClock,
    telemetry: StaticTelemetryProvider,
    *,
    recovering: bool = False,
    **policy_changes: object,
) -> ResourceRouterManager:
    policy = AdmissionPolicy(
        system_reserve_mb=2048,
        emergency_reserve_mb=0,
        reclaimable_cache_fraction=0,
        **policy_changes,
    )
    return ResourceRouterManager(
        telemetry,
        AdmissionEngine(policy),
        clock=clock,
        recovering=recovering,
    )


def acquire_body(
    *,
    request_id: str | None = None,
    memory_mb: int = 1024,
    gpu: bool = False,
    cpu_cores: float = 0,
    mode: str = "SHARED",
    ttl_seconds: int = 60,
) -> dict[str, object]:
    return {
        "request_id": request_id or str(uuid4()),
        "client_id": "api-test",
        "mode": mode,
        "resources": {
            "memory_mb": memory_mb,
            "gpu": gpu,
            "cpu_cores": cpu_cores,
        },
        "ttl_seconds": ttl_seconds,
    }


def reservation_body(
    now: datetime,
    *,
    reservation_id: str = "night-heavy",
    start_after_seconds: int = 130,
    duration_seconds: int = 300,
    drain_before_seconds: int = 65,
) -> dict[str, object]:
    return {
        "reservation_id": reservation_id,
        "client_id": "scheduled-worker",
        "mode": "EXCLUSIVE",
        "resources": {
            "memory_mb": 2048,
            "gpu": True,
            "cpu_cores": 2,
        },
        "start_at": (now + timedelta(seconds=start_after_seconds)).isoformat(),
        "duration_seconds": duration_seconds,
        "drain_before_seconds": drain_before_seconds,
    }


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_health_and_status_are_available() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))

    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/health/ready").json() == {"status": "ready"}

    response = client.get("/v1/status")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "OPEN"
    assert body["telemetry"]["memory"]["total_mb"] == 8192
    assert body["capacity"]["allocatable_memory_mb"] == 6144
    assert body["leases"] == {"shared": 0, "exclusive": 0}
    assert body["limits"] == {
        "max_lease_ttl_seconds": 60,
        "minimum_reservation_drain_before_seconds": 65,
    }


def test_lease_http_lifecycle_matches_contract() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))
    request = acquire_body(ttl_seconds=30)

    acquired = client.post("/v1/leases", json=request)

    assert acquired.status_code == 200
    assert acquired.json()["status"] == "GRANTED"
    assert set(acquired.json()["lease"]) == {"lease_id", "expires_at"}
    lease_id = acquired.json()["lease"]["lease_id"]

    read = client.get(f"/v1/leases/{lease_id}")
    assert read.status_code == 200
    assert read.json()["request_id"] == request["request_id"]
    assert read.json()["resources"] == request["resources"]

    clock.advance(seconds=10)
    renewed = client.post(
        f"/v1/leases/{lease_id}/renew",
        json={"ttl_seconds": 30},
    )
    assert renewed.status_code == 200
    assert parse_timestamp(renewed.json()["expires_at"]) == (
        clock.now + timedelta(seconds=30)
    )

    assert client.delete(f"/v1/leases/{lease_id}").status_code == 204
    terminal = client.get(f"/v1/leases/{lease_id}")
    assert terminal.status_code == 410
    assert terminal.json()["code"] == "LEASE_RELEASED"
    assert client.delete(f"/v1/leases/{uuid4()}").status_code == 204


def test_busy_is_409_and_does_not_bind_request_id() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now, memory_free_mb=0))
    client = TestClient(create_app(manager_for(clock, telemetry)))
    request = acquire_body()

    busy = client.post("/v1/leases", json=request)

    assert busy.status_code == 409
    assert busy.json() == {
        "status": "BUSY",
        "reason": "INSUFFICIENT_MEMORY",
        "retry_after_ms": 3000,
    }
    assert "code" not in busy.json()

    telemetry.set_snapshot(snapshot(now))
    granted = client.post("/v1/leases", json=request)
    assert granted.status_code == 200
    assert granted.json()["status"] == "GRANTED"


def test_telemetry_failure_is_503_and_does_not_bind_request_id() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = SwitchableTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))
    request = acquire_body()
    telemetry.unavailable = True

    unavailable = client.post("/v1/leases", json=request)

    assert unavailable.status_code == 503
    assert unavailable.headers["Retry-After"] == "3"
    assert unavailable.json()["code"] == "TELEMETRY_UNAVAILABLE"
    assert unavailable.json()["retry_after_ms"] == 3000
    assert "status" not in unavailable.json()

    telemetry.unavailable = False
    granted = client.post("/v1/leases", json=request)
    assert granted.status_code == 200


def test_known_ownership_conflict_remains_busy_without_telemetry() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = SwitchableTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))

    exclusive = client.post(
        "/v1/leases",
        json=acquire_body(mode="EXCLUSIVE"),
    )
    assert exclusive.status_code == 200

    telemetry.unavailable = True
    blocked = client.post("/v1/leases", json=acquire_body())

    assert blocked.status_code == 409
    assert blocked.json()["reason"] == "EXCLUSIVE_ACTIVE"

    blocked_exclusive_owner = client.post(
        "/v1/leases",
        json=acquire_body(mode="EXCLUSIVE"),
    )
    assert blocked_exclusive_owner.status_code == 409
    assert blocked_exclusive_owner.json()["reason"] == "CONFLICTING_LEASES"

    lease_id = exclusive.json()["lease"]["lease_id"]
    assert client.delete(f"/v1/leases/{lease_id}").status_code == 204
    telemetry.unavailable = False
    shared = client.post("/v1/leases", json=acquire_body())
    assert shared.status_code == 200

    telemetry.unavailable = True
    blocked_exclusive = client.post(
        "/v1/leases",
        json=acquire_body(mode="EXCLUSIVE"),
    )
    assert blocked_exclusive.status_code == 409
    assert blocked_exclusive.json()["reason"] == "CONFLICTING_LEASES"


def test_validation_and_permanent_capacity_errors_use_error_envelope() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))

    invalid = acquire_body()
    invalid["allow_pending"] = True
    validation = client.post("/v1/leases", json=invalid)
    assert validation.status_code == 422
    assert validation.json()["code"] == "VALIDATION_ERROR"

    impossible = client.post(
        "/v1/leases",
        json=acquire_body(memory_mb=6145),
    )
    assert impossible.status_code == 422
    assert impossible.json()["code"] == "CLAIM_EXCEEDS_NODE_CAPACITY"

    wrong_type = acquire_body()
    wrong_type["resources"] = {
        "memory_mb": "1024",
        "gpu": 0,
        "cpu_cores": 0,
    }
    strict_validation = client.post("/v1/leases", json=wrong_type)
    assert strict_validation.status_code == 422
    assert strict_validation.json()["code"] == "VALIDATION_ERROR"


def test_optional_metrics_are_nullable_and_do_not_break_memory_only_claims() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(
        snapshot(
            now,
            memory_cached_mb=None,
            memory_buffers_mb=None,
            memory_gpu_shared_mb=None,
            cpu_total_cores=None,
            cpu_online_cores=None,
            cpu_load_percent=None,
            gpu_load_percent=None,
        )
    )
    client = TestClient(create_app(manager_for(clock, telemetry)))

    assert client.get("/health/ready").status_code == 200
    status = client.get("/v1/status").json()
    assert status["telemetry"]["memory"]["cached_mb"] is None
    assert status["telemetry"]["cpu"]["total_cores"] is None
    assert status["telemetry"]["gpu"]["load_percent"] is None
    assert client.post("/v1/leases", json=acquire_body()).status_code == 200


def test_recovering_is_not_busy_and_status_remains_inspectable() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    manager = manager_for(clock, telemetry, recovering=True)
    client = TestClient(create_app(manager))

    acquire = client.post("/v1/leases", json=acquire_body())
    assert acquire.status_code == 503
    assert acquire.json()["code"] == "ROUTER_RECOVERING"
    assert client.get("/health/ready").json() == {
        "status": "not_ready",
        "reason": "ROUTER_RECOVERING",
    }
    assert client.get("/v1/status").json()["state"] == "RECOVERING"


def test_storage_failure_is_503_instead_of_busy() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    manager = manager_for(clock, telemetry)
    client = TestClient(create_app(manager))
    manager.close()

    unavailable = client.post("/v1/leases", json=acquire_body())

    assert unavailable.status_code == 503
    assert unavailable.headers["Retry-After"] == "3"
    assert unavailable.json()["code"] == "STORAGE_UNAVAILABLE"
    assert "status" not in unavailable.json()
    assert client.get("/health/ready").json() == {
        "status": "not_ready",
        "reason": "STORAGE_UNAVAILABLE",
    }


def test_reservation_creation_replay_conflict_and_cancellation() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))
    request = reservation_body(now)

    created = client.post("/v1/reservations", json=request)
    replay = client.post("/v1/reservations", json=request)

    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.json() == replay.json()
    assert created.json()["status"] == "SCHEDULED"

    changed = {**request, "duration_seconds": 301}
    reused = client.post("/v1/reservations", json=changed)
    assert reused.status_code == 409
    assert reused.json()["code"] == "RESERVATION_ID_REUSED"

    overlap = reservation_body(
        now,
        reservation_id="overlap",
        start_after_seconds=150,
    )
    conflict = client.post("/v1/reservations", json=overlap)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "RESERVATION_CONFLICT"

    assert client.delete("/v1/reservations/unknown").status_code == 204
    assert client.delete("/v1/reservations/night-heavy").status_code == 204
    terminal = client.get("/v1/reservations/night-heavy")
    assert terminal.status_code == 410
    assert terminal.json()["code"] == "RESERVATION_CANCELLED"
    replay_terminal = client.post("/v1/reservations", json=request)
    assert replay_terminal.status_code == 410
    assert replay_terminal.json()["code"] == "RESERVATION_CANCELLED"


def test_reservation_drains_caps_renewal_and_activates_safely() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))

    lease = client.post(
        "/v1/leases",
        json=acquire_body(ttl_seconds=60),
    ).json()["lease"]
    lease_id = lease["lease_id"]
    request = reservation_body(now)
    reservation = client.post("/v1/reservations", json=request)
    assert reservation.status_code == 201
    lease_deadline = parse_timestamp(reservation.json()["lease_deadline"])

    clock.advance(seconds=50)
    first_renewal = client.post(
        f"/v1/leases/{lease_id}/renew",
        json={"ttl_seconds": 60},
    )
    assert first_renewal.status_code == 200

    clock.advance(seconds=50)
    capped = client.post(
        f"/v1/leases/{lease_id}/renew",
        json={"ttl_seconds": 60},
    )
    assert capped.status_code == 200
    assert parse_timestamp(capped.json()["expires_at"]) == lease_deadline

    blocked = client.post("/v1/leases", json=acquire_body())
    assert blocked.status_code == 409
    assert blocked.json()["reason"] == "EXCLUSIVE_RESERVATION"

    clock.advance(seconds=30)
    telemetry.set_snapshot(snapshot(clock.now))
    active = client.get("/v1/reservations/night-heavy")
    assert active.status_code == 200
    assert active.json()["status"] == "ACTIVE"

    status = client.get("/v1/status").json()
    assert status["state"] == "EXCLUSIVE"
    assert status["exclusive_owner"] == {
        "type": "RESERVATION",
        "lease_id": None,
        "reservation_id": "night-heavy",
    }
    assert status["capacity"]["committed_memory_mb"] == 2048
    assert status["capacity"]["committed_cpu_cores"] == 2

    assert client.delete("/v1/reservations/night-heavy").status_code == 204
    assert client.post("/v1/leases", json=acquire_body()).status_code == 200


def test_reservation_that_never_activates_ends_without_entitlement() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = SwitchableTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))
    request = reservation_body(
        now,
        start_after_seconds=70,
        duration_seconds=10,
    )
    assert client.post("/v1/reservations", json=request).status_code == 201

    clock.advance(seconds=70)
    telemetry.unavailable = True
    draining = client.get("/v1/reservations/night-heavy")
    assert draining.status_code == 200
    assert draining.json()["status"] == "DRAINING"

    clock.advance(seconds=10)
    ended = client.get("/v1/reservations/night-heavy")
    assert ended.status_code == 410
    assert ended.json()["code"] == "RESERVATION_ENDED"
