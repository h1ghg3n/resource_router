from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from math import ceil
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.admission.engine import AdmissionEngine
from app.admission.policy import AdmissionPolicy
from app.api.schemas import (
    AcquireGrantedResponse,
    CapacityResponse,
    CpuTelemetryResponse,
    EmcTelemetryResponse,
    ExclusiveOwnerResponse,
    GpuTelemetryResponse,
    HealthLiveResponse,
    HealthReadyResponse,
    LeaseCountsResponse,
    LeaseGrant,
    LimitsResponse,
    MemoryTelemetryResponse,
    RenewLeaseRequest,
    RenewLeaseResponse,
    ReservationStatusResponse,
    ReservationWindowResponse,
    RouterStatusResponse,
    TelemetryResponse,
)
from app.core.enums import AcquisitionStatus
from app.core.errors import (
    ClaimExceedsNodeCapacityError,
    ExclusiveReservationError,
    InvalidLeaseTtlError,
    InvalidReservationError,
    LeaseExpiredError,
    LeaseNotFoundError,
    LeaseReleasedError,
    RequestIdReusedError,
    ReservationCancelledError,
    ReservationConflictError,
    ReservationEndedError,
    ReservationIdReusedError,
    ReservationNotFoundError,
    ResourceRouterError,
    RouterRecoveringError,
    StorageUnavailableError,
    TelemetryUnavailableError,
)
from app.core.models import (
    BusyLeaseResult,
    Lease,
    Reservation,
    ReservationRequest,
    ResourceClaim,
)
from app.leases.manager import ResourceRouterManager
from app.persistence.sqlite import SQLiteStateRepository
from app.settings import Settings
from app.telemetry.jtop import JtopTelemetryProvider


def create_app(
    manager: ResourceRouterManager | None = None,
    *,
    settings: Settings | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings.from_environment()
    service = manager or ResourceRouterManager(
        JtopTelemetryProvider(),
        AdmissionEngine(
            AdmissionPolicy(
                reclaimable_cache_fraction=(
                    runtime_settings.reclaimable_cache_fraction
                )
            )
        ),
        repository=SQLiteStateRepository(runtime_settings.database_path),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title="Jetson Resource Router",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.router = service

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        message = "request validation failed"
        errors = error.errors()
        if errors:
            location = ".".join(str(part) for part in errors[0].get("loc", ()))
            detail = errors[0].get("msg", "invalid value")
            message = f"{location}: {detail}" if location else str(detail)
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": message},
        )

    @app.exception_handler(ResourceRouterError)
    async def domain_error_handler(
        _request: Request,
        error: ResourceRouterError,
    ) -> JSONResponse:
        status_code, code = _map_domain_error(error)
        content: dict[str, object] = {
            "code": code,
            "message": str(error),
        }
        headers: dict[str, str] = {}
        if status_code == 503:
            retry_ms = service.policy.unavailable_retry_after_ms
            content["retry_after_ms"] = retry_ms
            headers["Retry-After"] = str(ceil(retry_ms / 1000))
        return JSONResponse(
            status_code=status_code,
            content=content,
            headers=headers,
        )

    @app.post(
        "/v1/leases",
        response_model=AcquireGrantedResponse | BusyLeaseResult,
    )
    async def acquire_lease(
        claim: ResourceClaim,
    ) -> AcquireGrantedResponse | JSONResponse:
        result = await service.acquire(claim)
        if result.status is AcquisitionStatus.BUSY:
            return JSONResponse(
                status_code=409,
                content=jsonable_encoder(result),
            )
        return AcquireGrantedResponse(
            lease=LeaseGrant(
                lease_id=result.lease.lease_id,
                expires_at=result.lease.expires_at,
            )
        )

    @app.get("/v1/leases/{lease_id}", response_model=Lease)
    async def get_lease(lease_id: UUID) -> Lease:
        return await service.get(lease_id)

    @app.post(
        "/v1/leases/{lease_id}/renew",
        response_model=RenewLeaseResponse,
    )
    async def renew_lease(
        lease_id: UUID,
        request: RenewLeaseRequest,
    ) -> RenewLeaseResponse:
        lease = await service.renew(lease_id, request.ttl_seconds)
        return RenewLeaseResponse(
            lease_id=lease.lease_id,
            expires_at=lease.expires_at,
        )

    @app.delete("/v1/leases/{lease_id}", status_code=204)
    async def release_lease(lease_id: UUID) -> Response:
        await service.release(lease_id)
        return Response(status_code=204)

    @app.post(
        "/v1/reservations",
        response_model=ReservationWindowResponse,
    )
    async def create_reservation(
        request: ReservationRequest,
    ) -> JSONResponse:
        result = await service.create_reservation(request)
        response = ReservationWindowResponse(
            status=result.reservation.status,
            reservation_id=result.reservation.reservation_id,
            drain_at=result.reservation.drain_at,
            lease_deadline=result.reservation.lease_deadline,
            start_at=result.reservation.start_at,
            end_at=result.reservation.end_at,
        )
        return JSONResponse(
            status_code=201 if result.created else 200,
            content=jsonable_encoder(response),
        )

    @app.get(
        "/v1/reservations/{reservation_id}",
        response_model=Reservation,
    )
    async def get_reservation(reservation_id: str) -> Reservation:
        return await service.get_reservation(reservation_id)

    @app.delete("/v1/reservations/{reservation_id}", status_code=204)
    async def release_reservation(reservation_id: str) -> Response:
        await service.release_reservation(reservation_id)
        return Response(status_code=204)

    @app.get("/v1/status", response_model=RouterStatusResponse)
    async def get_status() -> RouterStatusResponse:
        current = await service.status()
        snapshot = current.telemetry
        telemetry = (
            None
            if snapshot is None or current.telemetry_age_ms is None
            else TelemetryResponse(
                age_ms=current.telemetry_age_ms,
                memory=MemoryTelemetryResponse(
                    total_mb=snapshot.memory_total_mb,
                    free_mb=snapshot.memory_free_mb,
                    cached_mb=snapshot.memory_cached_mb,
                    buffers_mb=snapshot.memory_buffers_mb,
                    gpu_shared_mb=snapshot.memory_gpu_shared_mb,
                    lfb_mb=snapshot.memory_lfb_mb,
                ),
                cpu=CpuTelemetryResponse(
                    total_cores=snapshot.cpu_total_cores,
                    online_cores=snapshot.cpu_online_cores,
                    load_percent=snapshot.cpu_load_percent,
                ),
                gpu=GpuTelemetryResponse(
                    load_percent=snapshot.gpu_load_percent,
                ),
                emc=EmcTelemetryResponse(
                    load_percent=snapshot.emc_load_percent,
                ),
                temperature_max_c=snapshot.temperature_max_c,
            )
        )
        capacity = (
            None
            if current.capacity is None
            else CapacityResponse(
                allocatable_memory_mb=current.capacity.allocatable_memory_mb,
                committed_memory_mb=current.capacity.committed_memory_mb,
                effective_available_memory_mb=(
                    current.capacity.effective_available_memory_mb
                ),
                allocatable_cpu_cores=current.capacity.allocatable_cpu_cores,
                committed_cpu_cores=current.capacity.committed_cpu_cores,
            )
        )
        owner = None
        if current.exclusive_owner is not None:
            if current.exclusive_owner.owner_type == "LEASE":
                owner = ExclusiveOwnerResponse(
                    type="LEASE",
                    lease_id=UUID(current.exclusive_owner.owner_id),
                )
            else:
                owner = ExclusiveOwnerResponse(
                    type="RESERVATION",
                    reservation_id=current.exclusive_owner.owner_id,
                )
        reservation = (
            None
            if current.reservation is None
            else ReservationStatusResponse(
                reservation_id=current.reservation.reservation_id,
                status=current.reservation.status,
                drain_at=current.reservation.drain_at,
                lease_deadline=current.reservation.lease_deadline,
                start_at=current.reservation.start_at,
                end_at=current.reservation.end_at,
            )
        )
        return RouterStatusResponse(
            state=current.state,
            exclusive_owner=owner,
            telemetry=telemetry,
            capacity=capacity,
            leases=LeaseCountsResponse(
                shared=current.shared_leases,
                exclusive=current.exclusive_leases,
            ),
            reservation=reservation,
            limits=LimitsResponse(
                max_lease_ttl_seconds=service.policy.max_lease_ttl_seconds,
                minimum_reservation_drain_before_seconds=(
                    service.minimum_reservation_drain_before_seconds
                ),
            ),
        )

    @app.get("/health/live", response_model=HealthLiveResponse)
    async def health_live() -> HealthLiveResponse:
        return HealthLiveResponse()

    @app.get(
        "/health/ready",
        response_model=HealthReadyResponse,
        responses={503: {"description": "Router is not ready"}},
    )
    async def health_ready() -> HealthReadyResponse | JSONResponse:
        reason = await service.readiness_reason()
        if reason is not None:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": reason},
            )
        return HealthReadyResponse()

    return app


def _map_domain_error(error: ResourceRouterError) -> tuple[int, str]:
    mappings: tuple[tuple[type[ResourceRouterError], int, str], ...] = (
        (LeaseNotFoundError, 404, "LEASE_NOT_FOUND"),
        (ReservationNotFoundError, 404, "RESERVATION_NOT_FOUND"),
        (RequestIdReusedError, 409, "REQUEST_ID_REUSED"),
        (ReservationIdReusedError, 409, "RESERVATION_ID_REUSED"),
        (ReservationConflictError, 409, "RESERVATION_CONFLICT"),
        (ExclusiveReservationError, 409, "EXCLUSIVE_RESERVATION"),
        (LeaseExpiredError, 410, "LEASE_EXPIRED"),
        (LeaseReleasedError, 410, "LEASE_RELEASED"),
        (ReservationEndedError, 410, "RESERVATION_ENDED"),
        (ReservationCancelledError, 410, "RESERVATION_CANCELLED"),
        (ClaimExceedsNodeCapacityError, 422, "CLAIM_EXCEEDS_NODE_CAPACITY"),
        (InvalidLeaseTtlError, 422, "VALIDATION_ERROR"),
        (InvalidReservationError, 422, "VALIDATION_ERROR"),
        (TelemetryUnavailableError, 503, "TELEMETRY_UNAVAILABLE"),
        (RouterRecoveringError, 503, "ROUTER_RECOVERING"),
        (StorageUnavailableError, 503, "STORAGE_UNAVAILABLE"),
    )
    for error_type, status_code, code in mappings:
        if isinstance(error, error_type):
            return status_code, code
    raise RuntimeError(f"unmapped domain error: {type(error).__name__}")
