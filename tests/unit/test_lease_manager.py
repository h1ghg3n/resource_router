import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.admission.engine import AdmissionEngine
from app.admission.policy import AdmissionPolicy
from app.core.enums import AcquisitionStatus, BusyReason, ResourceMode
from app.core.errors import (
    ClaimExceedsNodeCapacityError,
    InvalidLeaseTtlError,
    LeaseExpiredError,
    LeaseReleasedError,
    RequestIdReusedError,
    TelemetryUnavailableError,
)
from app.core.models import ResourceClaim, ResourceVector
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


class SwitchableTelemetryProvider(StaticTelemetryProvider):
    def __init__(self, initial_snapshot: ResourceSnapshot) -> None:
        super().__init__(initial_snapshot)
        self.unavailable = False

    def read(self) -> ResourceSnapshot:
        if self.unavailable:
            raise TelemetryUnavailableError("telemetry is unavailable for this test")
        return super().read()


def claim(
    memory_mb: int,
    *,
    mode: ResourceMode = ResourceMode.SHARED,
    gpu: bool = False,
    cpu_cores: float = 0,
    ttl_seconds: int = 30,
    request_id: UUID | None = None,
) -> ResourceClaim:
    return ResourceClaim(
        request_id=request_id or uuid4(),
        client_id="test-client",
        mode=mode,
        resources=ResourceVector(
            memory_mb=memory_mb,
            gpu=gpu,
            cpu_cores=cpu_cores,
        ),
        ttl_seconds=ttl_seconds,
    )


def manager_for(
    clock: MutableClock,
    telemetry: StaticTelemetryProvider,
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
    )


def test_two_simultaneous_claims_cannot_overcommit_memory() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))

        results = await asyncio.gather(
            manager.acquire(claim(4096)),
            manager.acquire(claim(4096)),
        )

        assert [result.status for result in results].count(
            AcquisitionStatus.GRANTED
        ) == 1
        assert [result.status for result in results].count(AcquisitionStatus.BUSY) == 1
        denied = next(
            result for result in results if result.status is AcquisitionStatus.BUSY
        )
        assert denied.reason is BusyReason.INSUFFICIENT_MEMORY
        assert len(await manager.active_leases()) == 1

    asyncio.run(scenario())


def test_telemetry_free_memory_cannot_override_committed_capacity() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        telemetry = StaticTelemetryProvider(snapshot(now, memory_free_mb=8192))
        manager = manager_for(clock, telemetry)

        first = await manager.acquire(claim(4096))
        second = await manager.acquire(claim(3072))

        assert first.status is AcquisitionStatus.GRANTED
        assert second.status is AcquisitionStatus.BUSY
        assert second.reason is BusyReason.INSUFFICIENT_MEMORY

    asyncio.run(scenario())


def test_stale_telemetry_is_unavailable_instead_of_busy() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        stale = snapshot(now - timedelta(seconds=6))
        manager = manager_for(
            clock,
            StaticTelemetryProvider(stale),
            telemetry_max_age_seconds=5,
        )

        with pytest.raises(TelemetryUnavailableError):
            await manager.acquire(claim(1))

        assert await manager.active_leases() == ()

    asyncio.run(scenario())


def test_expired_lease_frees_committed_capacity() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        telemetry = StaticTelemetryProvider(snapshot(now))
        manager = manager_for(clock, telemetry)

        first = await manager.acquire(claim(6144, ttl_seconds=5))
        assert first.status is AcquisitionStatus.GRANTED

        clock.advance(seconds=5)
        telemetry.set_snapshot(snapshot(clock.now))
        second = await manager.acquire(claim(6144))

        assert second.status is AcquisitionStatus.GRANTED
        assert len(await manager.active_leases()) == 1

    asyncio.run(scenario())


def test_release_is_idempotent() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))
        result = await manager.acquire(claim(1024))
        assert result.status is AcquisitionStatus.GRANTED

        assert await manager.release(result.lease.lease_id) is True
        assert await manager.release(result.lease.lease_id) is False
        assert await manager.active_leases() == ()

    asyncio.run(scenario())


def test_unmanaged_gpu_pressure_blocks_gpu_claim() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        telemetry = StaticTelemetryProvider(snapshot(now, gpu_load_percent=95))
        manager = manager_for(
            clock,
            telemetry,
            gpu_pressure_threshold_percent=90,
        )

        result = await manager.acquire(claim(1024, gpu=True))

        assert result.status is AcquisitionStatus.BUSY
        assert result.reason is BusyReason.GPU_PRESSURE

    asyncio.run(scenario())


def test_exclusive_lease_blocks_shared_claims() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))

        exclusive = await manager.acquire(
            claim(1024, mode=ResourceMode.EXCLUSIVE)
        )
        shared = await manager.acquire(claim(1024))

        assert exclusive.status is AcquisitionStatus.GRANTED
        assert shared.status is AcquisitionStatus.BUSY
        assert shared.reason is BusyReason.EXCLUSIVE_ACTIVE

    asyncio.run(scenario())


def test_immediate_exclusive_conflict_is_busy_without_drain_side_effect() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))

        shared = await manager.acquire(claim(1024))
        exclusive = await manager.acquire(
            claim(1024, mode=ResourceMode.EXCLUSIVE)
        )
        another_shared = await manager.acquire(claim(1024))

        assert shared.status is AcquisitionStatus.GRANTED
        assert exclusive.status is AcquisitionStatus.BUSY
        assert exclusive.reason is BusyReason.CONFLICTING_LEASES
        assert another_shared.status is AcquisitionStatus.GRANTED
        assert len(await manager.active_leases()) == 2

    asyncio.run(scenario())


def test_immediate_exclusive_conflicts_with_an_exclusive_lease() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))

        existing = await manager.acquire(
            claim(1024, mode=ResourceMode.EXCLUSIVE)
        )
        blocked = await manager.acquire(
            claim(1024, mode=ResourceMode.EXCLUSIVE)
        )

        assert existing.status is AcquisitionStatus.GRANTED
        assert blocked.status is AcquisitionStatus.BUSY
        assert blocked.reason is BusyReason.CONFLICTING_LEASES
        assert len(await manager.active_leases()) == 1

    asyncio.run(scenario())


def test_successful_request_replay_returns_same_lease() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))
        request = claim(1024)

        first = await manager.acquire(request)
        second = await manager.acquire(request)

        assert first.status is AcquisitionStatus.GRANTED
        assert second.status is AcquisitionStatus.GRANTED
        assert first.lease.lease_id == second.lease.lease_id
        assert len(await manager.active_leases()) == 1

    asyncio.run(scenario())


def test_concurrent_same_request_creates_exactly_one_lease() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))
        request = claim(1024)

        first, second = await asyncio.gather(
            manager.acquire(request),
            manager.acquire(request),
        )

        assert first.status is AcquisitionStatus.GRANTED
        assert second.status is AcquisitionStatus.GRANTED
        assert first.lease.lease_id == second.lease.lease_id
        assert len(await manager.active_leases()) == 1

    asyncio.run(scenario())


def test_successful_request_id_cannot_be_reused_with_different_claim() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))
        request_id = uuid4()

        granted = await manager.acquire(claim(1024, request_id=request_id))
        assert granted.status is AcquisitionStatus.GRANTED

        with pytest.raises(RequestIdReusedError):
            await manager.acquire(claim(2048, request_id=request_id))

        assert len(await manager.active_leases()) == 1

    asyncio.run(scenario())


def test_busy_request_id_can_later_be_granted() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        telemetry = StaticTelemetryProvider(snapshot(now, memory_free_mb=512))
        manager = manager_for(clock, telemetry)
        request = claim(1024)

        busy = await manager.acquire(request)
        assert busy.status is AcquisitionStatus.BUSY

        telemetry.set_snapshot(snapshot(now))
        granted = await manager.acquire(request)

        assert granted.status is AcquisitionStatus.GRANTED
        assert len(await manager.active_leases()) == 1

    asyncio.run(scenario())


def test_unavailable_attempt_does_not_bind_request_id() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        telemetry = SwitchableTelemetryProvider(snapshot(now))
        manager = manager_for(clock, telemetry)
        request = claim(1024)

        telemetry.unavailable = True
        with pytest.raises(TelemetryUnavailableError):
            await manager.acquire(request)

        telemetry.unavailable = False
        granted = await manager.acquire(request)

        assert granted.status is AcquisitionStatus.GRANTED
        assert len(await manager.active_leases()) == 1

    asyncio.run(scenario())


def test_memory_claim_above_node_maximum_is_not_busy() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))

        with pytest.raises(ClaimExceedsNodeCapacityError) as error:
            await manager.acquire(claim(6145))

        assert error.value.resource == "memory_mb"
        assert error.value.maximum == 6144
        assert await manager.active_leases() == ()

    asyncio.run(scenario())


def test_cpu_claim_above_node_maximum_is_not_busy() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(
            clock,
            StaticTelemetryProvider(snapshot(now)),
            system_cpu_reserve_cores=1,
        )

        with pytest.raises(ClaimExceedsNodeCapacityError) as error:
            await manager.acquire(claim(0, cpu_cores=8))

        assert error.value.resource == "cpu_cores"
        assert error.value.maximum == 7

    asyncio.run(scenario())


def test_offline_cpu_capacity_is_temporary_pressure() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        telemetry = StaticTelemetryProvider(snapshot(now, cpu_online_cores=2))
        manager = manager_for(
            clock,
            telemetry,
            system_cpu_reserve_cores=1,
        )

        result = await manager.acquire(claim(0, cpu_cores=2))

        assert result.status is AcquisitionStatus.BUSY
        assert result.reason is BusyReason.CPU_PRESSURE

    asyncio.run(scenario())


def test_cpu_ledger_prevents_commitment_overcommit() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(
            clock,
            StaticTelemetryProvider(snapshot(now, cpu_online_cores=4)),
            system_cpu_reserve_cores=1,
        )

        first = await manager.acquire(claim(0, cpu_cores=2))
        second = await manager.acquire(claim(0, cpu_cores=2))

        assert first.status is AcquisitionStatus.GRANTED
        assert second.status is AcquisitionStatus.BUSY
        assert second.reason is BusyReason.CPU_PRESSURE

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("snapshot_changes", "claim_changes"),
    [
        ({"gpu_load_percent": None}, {"gpu": True}),
        ({"cpu_load_percent": None}, {"cpu_cores": 1}),
        ({"cpu_online_cores": None}, {"cpu_cores": 1}),
    ],
)
def test_claim_specific_missing_telemetry_is_unavailable(
    snapshot_changes: dict[str, object],
    claim_changes: dict[str, object],
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(
            clock,
            StaticTelemetryProvider(snapshot(now, **snapshot_changes)),
        )

        with pytest.raises(TelemetryUnavailableError):
            await manager.acquire(claim(0, **claim_changes))

    asyncio.run(scenario())


def test_missing_claim_unrelated_metrics_do_not_block_admission() -> None:
    async def scenario() -> None:
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
        manager = manager_for(clock, telemetry)

        result = await manager.acquire(claim(1024))

        assert result.status is AcquisitionStatus.GRANTED

    asyncio.run(scenario())


def test_renewal_does_not_require_telemetry() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        telemetry = SwitchableTelemetryProvider(snapshot(now))
        manager = manager_for(clock, telemetry)

        granted = await manager.acquire(claim(1024))
        assert granted.status is AcquisitionStatus.GRANTED

        clock.advance(seconds=10)
        telemetry.unavailable = True
        renewed = await manager.renew(granted.lease.lease_id, 30)

        assert renewed.expires_at == clock.now + timedelta(seconds=30)

    asyncio.run(scenario())


def test_replay_after_expiry_does_not_create_a_new_lease() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        telemetry = StaticTelemetryProvider(snapshot(now))
        manager = manager_for(clock, telemetry)
        request = claim(1024, ttl_seconds=5)

        granted = await manager.acquire(request)
        assert granted.status is AcquisitionStatus.GRANTED

        clock.advance(seconds=5)
        telemetry.set_snapshot(snapshot(clock.now))
        with pytest.raises(LeaseExpiredError):
            await manager.acquire(request)

        assert await manager.active_leases() == ()

    asyncio.run(scenario())


def test_replay_after_release_does_not_create_a_new_lease() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))
        request = claim(1024)

        granted = await manager.acquire(request)
        assert granted.status is AcquisitionStatus.GRANTED
        await manager.release(granted.lease.lease_id)

        with pytest.raises(LeaseReleasedError):
            await manager.acquire(request)

        assert await manager.active_leases() == ()

    asyncio.run(scenario())


def test_replay_returns_current_expiry_after_renewal() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(clock, StaticTelemetryProvider(snapshot(now)))
        request = claim(1024, ttl_seconds=30)

        granted = await manager.acquire(request)
        assert granted.status is AcquisitionStatus.GRANTED

        clock.advance(seconds=10)
        renewed = await manager.renew(granted.lease.lease_id, 60)
        replay = await manager.acquire(request)

        assert replay.status is AcquisitionStatus.GRANTED
        assert replay.lease.lease_id == granted.lease.lease_id
        assert replay.lease.expires_at == renewed.expires_at

    asyncio.run(scenario())


def test_ttl_cannot_exceed_configured_maximum() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        clock = MutableClock(now)
        manager = manager_for(
            clock,
            StaticTelemetryProvider(snapshot(now)),
            max_lease_ttl_seconds=60,
        )

        with pytest.raises(InvalidLeaseTtlError):
            await manager.acquire(claim(1024, ttl_seconds=61))

        assert await manager.active_leases() == ()

    asyncio.run(scenario())
