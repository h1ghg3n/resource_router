from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from app.core.models import ReservationRequest, ResourceClaim


class LeaseTerminalReason(str, Enum):
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"


class ReservationTerminalReason(str, Enum):
    ENDED = "ENDED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class StoredAcquireRequest:
    claim: ResourceClaim
    lease_id: UUID
    lease_end: LeaseTerminalReason | None


@dataclass(frozen=True, slots=True)
class StoredReservation:
    request: ReservationRequest
    drain_at: datetime
    lease_deadline: datetime
    end_at: datetime
    activated_at: datetime | None = None
    terminal_reason: ReservationTerminalReason | None = None
    terminal_at: datetime | None = None
