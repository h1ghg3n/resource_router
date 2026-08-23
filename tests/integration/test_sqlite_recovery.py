from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.admission.engine import AdmissionEngine
from app.admission.policy import AdmissionPolicy
from app.core.enums import (
    AcquisitionStatus,
    BusyReason,
    ReservationStatus,
    ResourceMode,
)
from app.core.errors import (
    LeaseExpiredError,
    LeaseReleasedError,
    RequestIdReusedError,
    ReservationEndedError,
    RouterRecoveringError,
    StorageUnavailableError,
)
from app.core.models import (
    Lease,
    ReservationRequest,
    ResourceClaim,
    ResourceVector,
)
from app.leases.manager import ResourceRouterManager
from app.persistence.sqlite import SQLiteStateRepository
from app.telemetry.provider import StaticTelemetryProvider
from app.telemetry.snapshot import ResourceSnapshot

LEASE_ID = UUID("01920000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("018f5c30-0000-0000-0000-000000000001")
UTC = timezone.utc


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


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


def claim(
    *,
    request_id: UUID = REQUEST_ID,
    memory_mb: int = 1024,
    ttl_seconds: int = 60,
) -> ResourceClaim:
    return ResourceClaim(
        request_id=request_id,
        client_id="persistence-test",
        mode=ResourceMode.SHARED,
        resources=ResourceVector(memory_mb=memory_mb),
        ttl_seconds=ttl_seconds,
    )


def manager_for(
    database_path: Path,
    clock: MutableClock,
    *,
    lease_id: UUID = LEASE_ID,
    repository: SQLiteStateRepository | None = None,
) -> tuple[ResourceRouterManager, StaticTelemetryProvider]:
    telemetry = StaticTelemetryProvider(snapshot(clock.now))
    manager = ResourceRouterManager(
        telemetry,
        AdmissionEngine(
            AdmissionPolicy(
                system_reserve_mb=2048,
                emergency_reserve_mb=0,
                reclaimable_cache_fraction=0,
            )
        ),
        clock=clock,
        id_factory=lambda: lease_id,
        repository=repository or SQLiteStateRepository(database_path),
    )
    return manager, telemetry


class CommitThenFailOnceRepository(SQLiteStateRepository):
    """Simulate a commit whose successful outcome is hidden by an error."""

    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self._fail_next_grant = True

    def record_grant(self, request: ResourceClaim, lease: Lease) -> None:
        super().record_grant(request, lease)
        if self._fail_next_grant:
            self._fail_next_grant = False
            raise StorageUnavailableError(
                "simulated error after a durable grant commit"
            )


class CommitThenLoseReadsRepository(SQLiteStateRepository):
    """Simulate storage staying unavailable after a durable grant commit."""

    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.reads_available = True

    def load_active_leases(self) -> tuple[Lease, ...]:
        if not self.reads_available:
            raise StorageUnavailableError("simulated persistent read failure")
        return super().load_active_leases()

    def record_grant(self, request: ResourceClaim, lease: Lease) -> None:
        super().record_grant(request, lease)
        self.reads_available = False
        raise StorageUnavailableError(
            "simulated error after a durable grant commit"
        )


def test_grant_renew_release_and_tombstone_survive_restarts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "router.sqlite3"
        clock = MutableClock(datetime(2026, 8, 13, tzinfo=UTC))
        request = claim()

        first_manager, _ = manager_for(database_path, clock)
        granted = await first_manager.acquire(request)
        assert granted.status is AcquisitionStatus.GRANTED
        first_manager.close()

        second_manager, _ = manager_for(database_path, clock)
        replay = await second_manager.acquire(request)
        assert replay.status is AcquisitionStatus.GRANTED
        assert replay.lease.lease_id == LEASE_ID
        assert len(await second_manager.active_leases()) == 1

        with pytest.raises(RequestIdReusedError):
            await second_manager.acquire(claim(memory_mb=2048))

        clock.advance(seconds=10)
        renewed = await second_manager.renew(LEASE_ID, 60)
        renewed_expiry = renewed.expires_at
        second_manager.close()

        third_manager, _ = manager_for(database_path, clock)
        restored = await third_manager.get(LEASE_ID)
        assert restored.expires_at == renewed_expiry
        assert await third_manager.release(LEASE_ID) is True
        third_manager.close()

        fourth_manager, _ = manager_for(database_path, clock)
        assert await fourth_manager.active_leases() == ()
        with pytest.raises(LeaseReleasedError):
            await fourth_manager.get(LEASE_ID)
        with pytest.raises(LeaseReleasedError):
            await fourth_manager.acquire(request)
        fourth_manager.close()

    asyncio.run(scenario())


def test_post_commit_error_reloads_authoritative_lease_ledger(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "post-commit-error.sqlite3"
        clock = MutableClock(datetime(2026, 8, 13, tzinfo=UTC))
        repository = CommitThenFailOnceRepository(database_path)
        manager, _ = manager_for(
            database_path,
            clock,
            repository=repository,
        )
        request = claim(memory_mb=6144)

        with pytest.raises(StorageUnavailableError):
            await manager.acquire(request)

        active = await manager.active_leases()
        assert [lease.request_id for lease in active] == [request.request_id]

        replay = await manager.acquire(request)
        assert replay.status is AcquisitionStatus.GRANTED
        assert replay.lease.lease_id == LEASE_ID

        denied = await manager.acquire(
            claim(
                request_id=UUID("018f5c30-0000-0000-0000-000000000002"),
                memory_mb=6144,
            )
        )
        assert denied.status is AcquisitionStatus.BUSY
        assert denied.reason is BusyReason.INSUFFICIENT_MEMORY
        assert len(await manager.active_leases()) == 1
        manager.close()

    asyncio.run(scenario())


def test_failed_post_commit_reload_keeps_admission_fail_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "failed-post-commit-reload.sqlite3"
        clock = MutableClock(datetime(2026, 8, 13, tzinfo=UTC))
        repository = CommitThenLoseReadsRepository(database_path)
        manager, _ = manager_for(
            database_path,
            clock,
            repository=repository,
        )

        with pytest.raises(StorageUnavailableError):
            await manager.acquire(claim(memory_mb=6144))

        with pytest.raises(RouterRecoveringError):
            await manager.acquire(
                claim(
                    request_id=UUID(
                        "018f5c30-0000-0000-0000-000000000003"
                    ),
                    memory_mb=6144,
                )
            )
        assert await manager.readiness_reason() == "ROUTER_RECOVERING"

        repository.reads_available = True
        manager.close()
        recovered, _ = manager_for(database_path, clock)
        assert len(await recovered.active_leases()) == 1
        recovered.close()

    asyncio.run(scenario())


def test_startup_recovery_expires_elapsed_lease(tmp_path: Path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "expiry.sqlite3"
        clock = MutableClock(datetime(2026, 8, 13, tzinfo=UTC))
        request = claim(ttl_seconds=5)

        first_manager, _ = manager_for(database_path, clock)
        granted = await first_manager.acquire(request)
        assert granted.status is AcquisitionStatus.GRANTED
        first_manager.close()

        clock.advance(seconds=5)
        recovered_manager, _ = manager_for(database_path, clock)

        assert await recovered_manager.active_leases() == ()
        with pytest.raises(LeaseExpiredError):
            await recovered_manager.get(LEASE_ID)
        with pytest.raises(LeaseExpiredError):
            await recovered_manager.acquire(request)
        recovered_manager.close()

    asyncio.run(scenario())


def test_reservation_phase_and_tombstone_survive_restarts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "reservation.sqlite3"
        clock = MutableClock(datetime(2026, 8, 13, tzinfo=UTC))
        request = ReservationRequest(
            reservation_id="persisted-window",
            client_id="scheduled-worker",
            mode=ResourceMode.EXCLUSIVE,
            resources=ResourceVector(
                memory_mb=2048,
                gpu=True,
                cpu_cores=2,
            ),
            start_at=clock.now + timedelta(seconds=70),
            duration_seconds=10,
            drain_before_seconds=65,
        )

        first_manager, _ = manager_for(database_path, clock)
        created = await first_manager.create_reservation(request)
        assert created.reservation.status is ReservationStatus.SCHEDULED
        first_manager.close()

        clock.advance(seconds=70)
        active_manager, _ = manager_for(database_path, clock)
        active = await active_manager.get_reservation("persisted-window")
        assert active.status is ReservationStatus.ACTIVE
        active_manager.close()

        clock.advance(seconds=10)
        ended_manager, _ = manager_for(database_path, clock)
        with pytest.raises(ReservationEndedError):
            await ended_manager.get_reservation("persisted-window")
        with pytest.raises(ReservationEndedError):
            await ended_manager.create_reservation(request)
        ended_manager.close()

    asyncio.run(scenario())


def test_committed_grant_survives_abrupt_process_exit(tmp_path: Path) -> None:
    database_path = tmp_path / "crash.sqlite3"
    script = f"""
import asyncio
import os
from datetime import datetime, timezone
from uuid import UUID

from app.admission.engine import AdmissionEngine
from app.admission.policy import AdmissionPolicy
from app.core.enums import ResourceMode
from app.core.models import ResourceClaim, ResourceVector
from app.leases.manager import ResourceRouterManager
from app.persistence.sqlite import SQLiteStateRepository
from app.telemetry.provider import StaticTelemetryProvider
from app.telemetry.snapshot import ResourceSnapshot

now = datetime(2026, 8, 13, tzinfo=timezone.utc)
telemetry = StaticTelemetryProvider(ResourceSnapshot(
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
))
manager = ResourceRouterManager(
    telemetry,
    AdmissionEngine(AdmissionPolicy(
        system_reserve_mb=2048,
        emergency_reserve_mb=0,
        reclaimable_cache_fraction=0,
    )),
    clock=lambda: now,
    id_factory=lambda: UUID("{LEASE_ID}"),
    repository=SQLiteStateRepository(r"{database_path}"),
)
request = ResourceClaim(
    request_id=UUID("{REQUEST_ID}"),
    client_id="crash-test",
    mode=ResourceMode.SHARED,
    resources=ResourceVector(memory_mb=1024),
    ttl_seconds=60,
)
result = asyncio.run(manager.acquire(request))
assert str(result.lease.lease_id) == "{LEASE_ID}"
os._exit(0)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    repository = SQLiteStateRepository(database_path)
    stored = repository.get_acquire_request(REQUEST_ID)
    assert stored is not None
    assert stored.lease_id == LEASE_ID
    assert stored.lease_end is None
    assert [lease.lease_id for lease in repository.load_active_leases()] == [
        LEASE_ID
    ]
    repository.close()


def test_failed_request_record_insert_rolls_back_entire_grant(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "atomic.sqlite3"
        clock = MutableClock(datetime(2026, 8, 13, tzinfo=UTC))
        manager, _ = manager_for(database_path, clock)

        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_acquire_request_insert
                BEFORE INSERT ON acquire_requests
                BEGIN
                    SELECT RAISE(ABORT, 'forced request-record failure');
                END
                """
            )

        with pytest.raises(StorageUnavailableError):
            await manager.acquire(claim())
        assert await manager.active_leases() == ()
        manager.close()

        with sqlite3.connect(database_path) as connection:
            lease_count = connection.execute(
                "SELECT COUNT(*) FROM leases"
            ).fetchone()[0]
            request_count = connection.execute(
                "SELECT COUNT(*) FROM acquire_requests"
            ).fetchone()[0]
        assert lease_count == 0
        assert request_count == 0

    asyncio.run(scenario())


def test_corrupt_database_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupt.sqlite3"
    database_path.write_bytes(b"not a sqlite database")

    with pytest.raises(StorageUnavailableError):
        SQLiteStateRepository(database_path)


def test_unrecognized_sqlite_database_is_not_modified(tmp_path: Path) -> None:
    database_path = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(StorageUnavailableError):
        SQLiteStateRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
    assert tables == {"unrelated"}
