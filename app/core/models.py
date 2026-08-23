from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import (
    AcquisitionStatus,
    BusyReason,
    LeaseStatus,
    ReservationStatus,
    ResourceMode,
)


class DomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class ResourceVector(DomainModel):
    memory_mb: int = Field(default=0, ge=0, strict=True)
    gpu: bool = Field(default=False, strict=True)
    cpu_cores: float = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def require_declared_demand(self) -> ResourceVector:
        if self.memory_mb == 0 and not self.gpu and self.cpu_cores == 0:
            raise ValueError("at least one resource demand must be non-zero")
        return self


class ResourceClaim(DomainModel):
    request_id: UUID
    client_id: str = Field(min_length=1, max_length=128)
    mode: ResourceMode
    resources: ResourceVector
    ttl_seconds: int = Field(ge=1, strict=True)


class Lease(DomainModel):
    lease_id: UUID
    request_id: UUID
    client_id: str = Field(min_length=1, max_length=128)
    mode: ResourceMode
    resources: ResourceVector
    granted_at: datetime
    expires_at: datetime
    status: Literal[LeaseStatus.ACTIVE] = LeaseStatus.ACTIVE

    @field_validator("granted_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def require_positive_lifetime(self) -> Lease:
        if self.expires_at <= self.granted_at:
            raise ValueError("expires_at must be later than granted_at")
        return self

    def is_active(self, at: datetime) -> bool:
        return self.expires_at > at


class GrantedLeaseResult(DomainModel):
    status: Literal[AcquisitionStatus.GRANTED] = AcquisitionStatus.GRANTED
    lease: Lease


class BusyLeaseResult(DomainModel):
    status: Literal[AcquisitionStatus.BUSY] = AcquisitionStatus.BUSY
    reason: BusyReason
    retry_after_ms: int = Field(ge=1)


LeaseAcquisitionResult = Annotated[
    GrantedLeaseResult | BusyLeaseResult,
    Field(discriminator="status"),
]


class ReservationRequest(DomainModel):
    reservation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    client_id: str = Field(min_length=1, max_length=128)
    mode: ResourceMode
    resources: ResourceVector
    start_at: datetime
    duration_seconds: int = Field(gt=0, strict=True)
    drain_before_seconds: int = Field(ge=0, strict=True)

    @field_validator("start_at")
    @classmethod
    def require_start_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("start_at must include a timezone")
        return value

    @model_validator(mode="after")
    def require_exclusive_mode(self) -> ReservationRequest:
        if self.mode is not ResourceMode.EXCLUSIVE:
            raise ValueError("v1 reservations must use EXCLUSIVE mode")
        return self


class Reservation(DomainModel):
    reservation_id: str
    client_id: str
    mode: Literal[ResourceMode.EXCLUSIVE] = ResourceMode.EXCLUSIVE
    resources: ResourceVector
    status: ReservationStatus
    drain_at: datetime
    lease_deadline: datetime
    start_at: datetime
    end_at: datetime

    @field_validator("drain_at", "lease_deadline", "start_at", "end_at")
    @classmethod
    def require_reservation_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reservation timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def require_ordered_window(self) -> Reservation:
        if not self.drain_at <= self.lease_deadline < self.start_at < self.end_at:
            raise ValueError("reservation boundaries are not ordered")
        return self
