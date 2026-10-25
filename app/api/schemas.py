from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import AcquisitionStatus, ReservationStatus, RouterState


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeaseGrant(ApiModel):
    lease_id: UUID
    expires_at: datetime


class AcquireGrantedResponse(ApiModel):
    status: Literal[AcquisitionStatus.GRANTED] = AcquisitionStatus.GRANTED
    lease: LeaseGrant


class RenewLeaseRequest(ApiModel):
    ttl_seconds: int = Field(ge=1, strict=True)


class RenewLeaseResponse(ApiModel):
    lease_id: UUID
    status: Literal["ACTIVE"] = "ACTIVE"
    expires_at: datetime


class ReservationWindowResponse(ApiModel):
    status: ReservationStatus
    reservation_id: str
    drain_at: datetime
    lease_deadline: datetime
    start_at: datetime
    end_at: datetime


class MemoryTelemetryResponse(ApiModel):
    total_mb: int
    free_mb: int
    cached_mb: int | None
    buffers_mb: int | None
    gpu_shared_mb: int | None
    lfb_mb: int | None


class CpuTelemetryResponse(ApiModel):
    total_cores: int | None
    online_cores: int | None
    load_percent: float | None


class GpuTelemetryResponse(ApiModel):
    load_percent: float | None


class EmcTelemetryResponse(ApiModel):
    load_percent: float | None


class TelemetryResponse(ApiModel):
    age_ms: int
    memory: MemoryTelemetryResponse
    cpu: CpuTelemetryResponse
    gpu: GpuTelemetryResponse
    emc: EmcTelemetryResponse
    temperature_max_c: float | None


class CapacityResponse(ApiModel):
    allocatable_memory_mb: int
    committed_memory_mb: int
    effective_available_memory_mb: int
    allocatable_cpu_cores: float | None
    committed_cpu_cores: float


class LeaseCountsResponse(ApiModel):
    shared: int
    exclusive: int


class ExclusiveOwnerResponse(ApiModel):
    type: Literal["LEASE", "RESERVATION"]
    lease_id: UUID | None = None
    reservation_id: str | None = None


class ReservationStatusResponse(ApiModel):
    reservation_id: str
    status: ReservationStatus
    drain_at: datetime
    lease_deadline: datetime
    start_at: datetime
    end_at: datetime


class LimitsResponse(ApiModel):
    max_lease_ttl_seconds: int
    minimum_reservation_drain_before_seconds: int


class RouterStatusResponse(ApiModel):
    state: RouterState
    exclusive_owner: ExclusiveOwnerResponse | None
    telemetry: TelemetryResponse | None
    capacity: CapacityResponse | None
    leases: LeaseCountsResponse
    reservation: ReservationStatusResponse | None
    limits: LimitsResponse


class TelemetryCurrentResponse(ApiModel):
    schema_version: Literal[1] = 1
    observed_at: datetime
    router: RouterStatusResponse


class TelemetryHistoryMemoryResponse(ApiModel):
    total_mb: int
    free_mb: int
    cached_mb: int | None
    buffers_mb: int | None
    gpu_shared_mb: int | None
    lfb_mb: int | None
    emc_load_percent: float | None
    emc_frequency_hz: float | None


class TelemetryHistoryCpuCoreResponse(ApiModel):
    index: int
    online: bool | None
    idle_percent: float | None
    frequency_hz: float | None


class TelemetryHistoryCpuResponse(ApiModel):
    total_cores: int | None
    online_cores: int | None
    load_percent: float | None
    cores: list[TelemetryHistoryCpuCoreResponse]


class TelemetryHistoryGpuDeviceResponse(ApiModel):
    load_percent: float | None
    frequency_hz: float | None
    maximum_frequency_hz: float | None


class TelemetryHistoryGpuResponse(ApiModel):
    load_percent: float | None
    devices: dict[str, TelemetryHistoryGpuDeviceResponse]


class TelemetryHistoryTemperatureResponse(ApiModel):
    maximum_c: float | None
    sensors_c: dict[str, float]


class TelemetryHistoryHostMemoryResponse(ApiModel):
    available_bytes: int | None
    cached_bytes: int | None
    slab_reclaimable_bytes: int | None
    slab_unreclaimable_bytes: int | None
    swap_total_bytes: int | None
    swap_free_bytes: int | None
    swap_cached_bytes: int | None


class TelemetryHistoryCgroupResponse(ApiModel):
    memory_current_bytes: int | None
    memory_peak_bytes: int | None
    memory_swap_current_bytes: int | None
    memory_events: dict[str, int]
    cpu_stat: dict[str, int]


class TelemetryHistorySampleResponse(ApiModel):
    schema_version: Literal[1]
    observed_at: datetime
    monotonic_ns: int
    provider_generation: int
    sample_id: int
    session_id: str
    memory: TelemetryHistoryMemoryResponse
    cpu: TelemetryHistoryCpuResponse
    gpu: TelemetryHistoryGpuResponse
    temperature: TelemetryHistoryTemperatureResponse
    power: dict[str, float]
    host_memory: TelemetryHistoryHostMemoryResponse
    cgroup: TelemetryHistoryCgroupResponse
    unavailable: list[str]


class TelemetryHistoryWriterResponse(ApiModel):
    offered: int
    dropped: int
    written: int
    errors: int
    disabled: bool


class TelemetryHistoryResponse(ApiModel):
    schema_version: Literal[1] = 1
    order: Literal["oldest_first"] = "oldest_first"
    count: int
    limit: int
    truncated: bool
    partial: bool
    read_errors: int
    skipped_records: int
    writer: TelemetryHistoryWriterResponse
    samples: list[TelemetryHistorySampleResponse]


class HealthLiveResponse(ApiModel):
    status: Literal["alive"] = "alive"


class HealthReadyResponse(ApiModel):
    status: Literal["ready"] = "ready"
