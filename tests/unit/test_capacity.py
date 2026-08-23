from datetime import datetime, timezone
from uuid import uuid4

from app.admission.capacity import calculate_capacity
from app.admission.policy import AdmissionPolicy
from app.core.enums import ResourceMode
from app.core.models import Lease, ResourceVector
from app.telemetry.snapshot import ResourceSnapshot

UTC = timezone.utc


def test_effective_capacity_uses_lower_of_ledger_and_observed_headroom() -> None:
    now = datetime.now(UTC)
    snapshot = ResourceSnapshot(
        observed_at=now,
        memory_total_mb=8192,
        memory_free_mb=7000,
        memory_cached_mb=1000,
        memory_buffers_mb=100,
        memory_gpu_shared_mb=0,
        memory_lfb_mb=None,
        cpu_total_cores=8,
        cpu_online_cores=8,
        cpu_load_percent=10,
        gpu_load_percent=0,
        emc_load_percent=None,
        temperature_max_c=None,
    )
    lease = Lease(
        lease_id=uuid4(),
        request_id=uuid4(),
        client_id="model-router",
        mode=ResourceMode.SHARED,
        resources=ResourceVector(memory_mb=4096),
        granted_at=now,
        expires_at=now.replace(year=now.year + 1),
    )
    policy = AdmissionPolicy(
        system_reserve_mb=2048,
        emergency_reserve_mb=500,
        reclaimable_cache_fraction=0.5,
    )

    capacity = calculate_capacity(snapshot, [lease], policy)

    assert capacity.allocatable_memory_mb == 6144
    assert capacity.committed_memory_mb == 4096
    assert capacity.ledger_remaining_memory_mb == 2048
    assert capacity.observed_headroom_memory_mb == 7000
    assert capacity.effective_available_memory_mb == 2048
    assert capacity.maximum_allocatable_cpu_cores == 8
    assert capacity.allocatable_cpu_cores == 8


def test_missing_cache_is_conservatively_not_reclaimable() -> None:
    now = datetime.now(UTC)
    snapshot = ResourceSnapshot(
        observed_at=now,
        memory_total_mb=8192,
        memory_free_mb=1000,
        memory_cached_mb=None,
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

    capacity = calculate_capacity(
        snapshot,
        [],
        AdmissionPolicy(
            system_reserve_mb=1024,
            emergency_reserve_mb=100,
            reclaimable_cache_fraction=0.5,
        ),
    )

    assert capacity.observed_headroom_memory_mb == 900
