from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Iterable

from app.admission.policy import AdmissionPolicy
from app.core.models import Lease, ResourceVector
from app.telemetry.snapshot import ResourceSnapshot


@dataclass(frozen=True, slots=True)
class Capacity:
    allocatable_memory_mb: int
    committed_memory_mb: int
    ledger_remaining_memory_mb: int
    observed_headroom_memory_mb: int
    effective_available_memory_mb: int
    maximum_allocatable_cpu_cores: float | None
    allocatable_cpu_cores: float | None
    committed_cpu_cores: float
    ledger_remaining_cpu_cores: float | None


def calculate_capacity(
    snapshot: ResourceSnapshot,
    leases: Iterable[Lease],
    policy: AdmissionPolicy,
    *,
    additional_commitments: Iterable[ResourceVector] = (),
) -> Capacity:
    active_leases = tuple(leases)
    extra_commitments = tuple(additional_commitments)
    allocatable_memory_mb = max(
        snapshot.memory_total_mb - policy.system_reserve_mb,
        0,
    )
    committed_memory_mb = sum(
        lease.resources.memory_mb for lease in active_leases
    ) + sum(resources.memory_mb for resources in extra_commitments)
    ledger_remaining_memory_mb = max(
        allocatable_memory_mb - committed_memory_mb,
        0,
    )

    reclaimable_cache_mb = floor(
        (snapshot.memory_cached_mb or 0) * policy.reclaimable_cache_fraction
    )
    observed_headroom_memory_mb = max(
        snapshot.memory_free_mb
        + reclaimable_cache_mb
        - policy.emergency_reserve_mb,
        0,
    )
    effective_available_memory_mb = min(
        ledger_remaining_memory_mb,
        observed_headroom_memory_mb,
    )

    committed_cpu_cores = sum(
        lease.resources.cpu_cores for lease in active_leases
    ) + sum(resources.cpu_cores for resources in extra_commitments)
    maximum_allocatable_cpu_cores = (
        None
        if snapshot.cpu_total_cores is None
        else max(
            snapshot.cpu_total_cores - policy.system_cpu_reserve_cores,
            0,
        )
    )
    allocatable_cpu_cores = (
        None
        if snapshot.cpu_online_cores is None
        else max(
            snapshot.cpu_online_cores - policy.system_cpu_reserve_cores,
            0,
        )
    )
    ledger_remaining_cpu_cores = (
        None
        if allocatable_cpu_cores is None
        else max(allocatable_cpu_cores - committed_cpu_cores, 0)
    )

    return Capacity(
        allocatable_memory_mb=allocatable_memory_mb,
        committed_memory_mb=committed_memory_mb,
        ledger_remaining_memory_mb=ledger_remaining_memory_mb,
        observed_headroom_memory_mb=observed_headroom_memory_mb,
        effective_available_memory_mb=effective_available_memory_mb,
        maximum_allocatable_cpu_cores=maximum_allocatable_cpu_cores,
        allocatable_cpu_cores=allocatable_cpu_cores,
        committed_cpu_cores=committed_cpu_cores,
        ledger_remaining_cpu_cores=ledger_remaining_cpu_cores,
    )
