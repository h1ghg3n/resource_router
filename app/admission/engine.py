from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from app.admission.capacity import Capacity, calculate_capacity
from app.admission.policy import AdmissionPolicy
from app.core.enums import BusyReason, ResourceMode
from app.core.errors import ClaimExceedsNodeCapacityError, TelemetryUnavailableError
from app.core.models import Lease, ResourceClaim, ResourceVector
from app.telemetry.snapshot import ResourceSnapshot


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    granted: bool
    reason: BusyReason | None
    capacity: Capacity | None


class AdmissionEngine:
    def __init__(self, policy: AdmissionPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> AdmissionPolicy:
        return self._policy

    def evaluate(
        self,
        claim: ResourceClaim,
        leases: Iterable[Lease],
        snapshot: ResourceSnapshot,
        now: datetime,
    ) -> AdmissionDecision:
        return self.evaluate_resources(
            resources=claim.resources,
            mode=claim.mode,
            leases=leases,
            snapshot=snapshot,
            now=now,
        )

    def evaluate_resources(
        self,
        *,
        resources: ResourceVector,
        mode: ResourceMode,
        leases: Iterable[Lease],
        snapshot: ResourceSnapshot,
        now: datetime,
    ) -> AdmissionDecision:
        active_leases = tuple(leases)
        conflict = self.ownership_conflict(mode, active_leases)
        if conflict is not None:
            return AdmissionDecision(False, conflict, None)

        self.validate_snapshot_freshness(snapshot, now)

        capacity = calculate_capacity(snapshot, active_leases, self._policy)
        self.validate_node_capacity(
            resources,
            snapshot,
            now,
            capacity=capacity,
            require_freshness=False,
        )
        self._require_claim_metrics(resources, snapshot, capacity)

        if resources.memory_mb > capacity.effective_available_memory_mb:
            return AdmissionDecision(False, BusyReason.INSUFFICIENT_MEMORY, capacity)

        if (
            resources.gpu
            and snapshot.gpu_load_percent is not None
            and snapshot.gpu_load_percent
            >= self._policy.gpu_pressure_threshold_percent
        ):
            return AdmissionDecision(False, BusyReason.GPU_PRESSURE, capacity)

        if resources.cpu_cores > 0:
            if capacity.ledger_remaining_cpu_cores is None:
                raise AssertionError("validated CPU telemetry produced no capacity")
            if resources.cpu_cores > capacity.ledger_remaining_cpu_cores:
                return AdmissionDecision(False, BusyReason.CPU_PRESSURE, capacity)

            if (
                snapshot.cpu_load_percent is not None
                and snapshot.cpu_load_percent
                >= self._policy.cpu_pressure_threshold_percent
            ):
                return AdmissionDecision(False, BusyReason.CPU_PRESSURE, capacity)

        return AdmissionDecision(True, None, capacity)

    @staticmethod
    def ownership_conflict(
        mode: ResourceMode,
        leases: Iterable[Lease],
    ) -> BusyReason | None:
        active_leases = tuple(leases)
        if mode is ResourceMode.EXCLUSIVE and active_leases:
            # Immediate EXCLUSIVE requests never create drain intent in v1.
            return BusyReason.CONFLICTING_LEASES
        if any(lease.mode is ResourceMode.EXCLUSIVE for lease in active_leases):
            return BusyReason.EXCLUSIVE_ACTIVE
        return None

    def validate_snapshot_freshness(
        self,
        snapshot: ResourceSnapshot,
        now: datetime,
    ) -> None:
        telemetry_age = (now - snapshot.observed_at).total_seconds()
        if (
            telemetry_age > self._policy.telemetry_max_age_seconds
            or telemetry_age < -self._policy.telemetry_future_tolerance_seconds
        ):
            raise TelemetryUnavailableError(
                "telemetry snapshot is stale or future-dated"
            )

    def validate_node_capacity(
        self,
        resources: ResourceVector,
        snapshot: ResourceSnapshot,
        now: datetime,
        *,
        capacity: Capacity | None = None,
        require_freshness: bool = True,
    ) -> Capacity:
        if require_freshness:
            self.validate_snapshot_freshness(snapshot, now)
        checked_capacity = capacity or calculate_capacity(snapshot, (), self._policy)

        if resources.memory_mb > checked_capacity.allocatable_memory_mb:
            raise ClaimExceedsNodeCapacityError(
                "memory_mb",
                resources.memory_mb,
                checked_capacity.allocatable_memory_mb,
            )

        if resources.cpu_cores > 0:
            maximum_cpu = checked_capacity.maximum_allocatable_cpu_cores
            if maximum_cpu is None:
                raise TelemetryUnavailableError(
                    "total CPU capacity telemetry is unavailable"
                )
            if resources.cpu_cores > maximum_cpu:
                raise ClaimExceedsNodeCapacityError(
                    "cpu_cores",
                    resources.cpu_cores,
                    maximum_cpu,
                )

        return checked_capacity

    @staticmethod
    def _require_claim_metrics(
        resources: ResourceVector,
        snapshot: ResourceSnapshot,
        capacity: Capacity,
    ) -> None:
        if resources.cpu_cores > 0 and (
            capacity.allocatable_cpu_cores is None
            or snapshot.cpu_load_percent is None
        ):
            raise TelemetryUnavailableError(
                "CPU capacity or load telemetry is unavailable"
            )

        if resources.gpu and snapshot.gpu_load_percent is None:
            raise TelemetryUnavailableError("GPU load telemetry is unavailable")
