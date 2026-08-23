from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.admission.capacity import Capacity, calculate_capacity
from app.admission.engine import AdmissionEngine
from app.admission.policy import AdmissionPolicy
from app.core.enums import (
    BusyReason,
    ReservationStatus,
    ResourceMode,
    RouterState,
)
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
    RouterRecoveringError,
    StorageUnavailableError,
    TelemetryUnavailableError,
)
from app.core.models import (
    BusyLeaseResult,
    GrantedLeaseResult,
    Lease,
    Reservation,
    ReservationRequest,
    ResourceClaim,
)
from app.persistence.models import (
    LeaseTerminalReason,
    ReservationTerminalReason,
    StoredAcquireRequest,
    StoredReservation,
)
from app.persistence.sqlite import SQLiteStateRepository
from app.telemetry.provider import TelemetryProvider
from app.telemetry.snapshot import ResourceSnapshot

Clock = Callable[[], datetime]
IdFactory = Callable[[], UUID]
UTC = timezone.utc
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _ReservationRecord:
    request: ReservationRequest
    drain_at: datetime
    lease_deadline: datetime
    end_at: datetime
    activated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReservationCreation:
    reservation: Reservation
    created: bool


@dataclass(frozen=True, slots=True)
class ExclusiveOwner:
    owner_type: str
    owner_id: str


@dataclass(frozen=True, slots=True)
class RouterStatusSnapshot:
    state: RouterState
    exclusive_owner: ExclusiveOwner | None
    telemetry: ResourceSnapshot | None
    capacity: Capacity | None
    telemetry_age_ms: int | None
    shared_leases: int
    exclusive_leases: int
    reservation: Reservation | None


class ResourceRouterManager:
    """SQLite-authoritative v1 manager with a process-local admission lock."""

    def __init__(
        self,
        telemetry: TelemetryProvider,
        admission: AdmissionEngine,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        recovering: bool = False,
        repository: SQLiteStateRepository | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._admission = admission
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4
        self._recovering = recovering
        self._repository = repository or SQLiteStateRepository()
        try:
            self._reload_authoritative_state()
            self._admission_lock = asyncio.Lock()
            if not self._recovering:
                self._synchronize(self._now())
        except Exception:
            try:
                self._repository.close()
            except StorageUnavailableError:
                pass
            raise

    @property
    def policy(self) -> AdmissionPolicy:
        return self._admission.policy

    @property
    def minimum_reservation_drain_before_seconds(self) -> int:
        return (
            self.policy.max_lease_ttl_seconds
            + self.policy.reservation_safety_margin_seconds
        )

    async def acquire(
        self,
        claim: ResourceClaim,
    ) -> GrantedLeaseResult | BusyLeaseResult:
        async with self._admission_lock:
            self._require_reconciled()
            now = self._now()
            self._synchronize(now)

            existing_request = self._repository.get_acquire_request(
                claim.request_id
            )
            if existing_request is not None:
                return self._replay(claim, existing_request)

            self._validate_ttl(claim.ttl_seconds)

            if self._restrictive_reservation(now) is not None:
                return self._busy(BusyReason.EXCLUSIVE_RESERVATION)

            conflict = self._admission.ownership_conflict(
                claim.mode,
                self._leases.values(),
            )
            if conflict is not None:
                return self._busy(conflict)

            snapshot = self._read_telemetry()
            decision = self._admission.evaluate(
                claim,
                self._leases.values(),
                snapshot,
                now,
            )
            if not decision.granted:
                if decision.reason is None:
                    raise AssertionError("denied admission must include a reason")
                return self._busy(decision.reason)

            lease = Lease(
                lease_id=self._id_factory(),
                request_id=claim.request_id,
                client_id=claim.client_id,
                mode=claim.mode,
                resources=claim.resources,
                granted_at=now,
                expires_at=now + timedelta(seconds=claim.ttl_seconds),
            )
            if self._repository.get_lease_lifecycle(lease.lease_id) is not None:
                raise RuntimeError(f"duplicate lease_id generated: {lease.lease_id}")

            with self._authoritative_mutation():
                self._repository.record_grant(claim, lease)
                self._leases[lease.lease_id] = lease
            return GrantedLeaseResult(lease=lease)

    async def get(self, lease_id: UUID) -> Lease:
        async with self._admission_lock:
            self._require_reconciled()
            self._synchronize(self._now())
            lease = self._leases.get(lease_id)
            if lease is not None:
                return lease
            self._raise_for_missing_lease(lease_id)

    async def renew(self, lease_id: UUID, ttl_seconds: int) -> Lease:
        async with self._admission_lock:
            self._require_reconciled()
            now = self._now()
            self._synchronize(now)
            self._validate_ttl(ttl_seconds)

            lease = self._leases.get(lease_id)
            if lease is None:
                self._raise_for_missing_lease(lease_id)

            expires_at = now + timedelta(seconds=ttl_seconds)
            deadline = self._next_lease_deadline(now)
            if deadline is not None and expires_at > deadline:
                expires_at = deadline
            if expires_at <= now:
                raise ExclusiveReservationError(
                    "an exclusive reservation leaves no positive renewal interval"
                )

            renewed = Lease(
                lease_id=lease.lease_id,
                request_id=lease.request_id,
                client_id=lease.client_id,
                mode=lease.mode,
                resources=lease.resources,
                granted_at=lease.granted_at,
                expires_at=expires_at,
            )
            with self._authoritative_mutation():
                self._repository.renew_lease(renewed)
                self._leases[lease_id] = renewed
            return renewed

    async def active_leases(self) -> tuple[Lease, ...]:
        async with self._admission_lock:
            self._require_reconciled()
            self._synchronize(self._now())
            return tuple(self._leases.values())

    async def release(self, lease_id: UUID) -> bool:
        """Release a lease; False is an idempotent success for absent IDs."""
        async with self._admission_lock:
            self._require_reconciled()
            now = self._now()
            self._synchronize(now)
            lease = self._leases.get(lease_id)
            if lease is None:
                return False

            with self._authoritative_mutation():
                self._repository.terminalize_leases(
                    ((lease_id, LeaseTerminalReason.RELEASED, now),)
                )
                self._leases.pop(lease_id)
            self._reconcile_reservations(now)
            return True

    async def create_reservation(
        self,
        request: ReservationRequest,
    ) -> ReservationCreation:
        async with self._admission_lock:
            self._require_reconciled()
            now = self._now()
            self._synchronize(now)

            stored_existing = self._repository.get_reservation(
                request.reservation_id
            )
            if stored_existing is not None:
                if stored_existing.request != request:
                    raise ReservationIdReusedError(request.reservation_id)
                self._raise_for_terminal_reservation(stored_existing)
                existing = self._reservations.get(request.reservation_id)
                if existing is None:
                    raise StorageUnavailableError(
                        "nonterminal reservation is missing from memory"
                    )
                return ReservationCreation(
                    reservation=self._reservation_view(existing, now),
                    created=False,
                )

            try:
                drain_at = request.start_at - timedelta(
                    seconds=request.drain_before_seconds
                )
                lease_deadline = request.start_at - timedelta(
                    seconds=self.policy.reservation_safety_margin_seconds
                )
                end_at = request.start_at + timedelta(
                    seconds=request.duration_seconds
                )
            except OverflowError as error:
                raise InvalidReservationError(
                    "reservation timestamps exceed the supported datetime range"
                ) from error
            self._validate_reservation_window(
                request,
                drain_at=drain_at,
                lease_deadline=lease_deadline,
                end_at=end_at,
                now=now,
            )

            snapshot = self._read_telemetry()
            self._admission.validate_node_capacity(
                request.resources,
                snapshot,
                now,
            )
            self._ensure_reservation_does_not_overlap(drain_at, end_at)

            record = _ReservationRecord(
                request=request,
                drain_at=drain_at,
                lease_deadline=lease_deadline,
                end_at=end_at,
            )
            with self._authoritative_mutation():
                self._repository.record_reservation(
                    StoredReservation(
                        request=request,
                        drain_at=drain_at,
                        lease_deadline=lease_deadline,
                        end_at=end_at,
                    ),
                    created_at=now,
                )
                self._reservations[request.reservation_id] = record
            return ReservationCreation(
                reservation=self._reservation_view(record, now),
                created=True,
            )

    async def get_reservation(self, reservation_id: str) -> Reservation:
        async with self._admission_lock:
            self._require_reconciled()
            now = self._now()
            self._synchronize(now)
            record = self._reservations.get(reservation_id)
            if record is None:
                stored = self._repository.get_reservation(reservation_id)
                if stored is None:
                    raise ReservationNotFoundError(reservation_id)
                self._raise_for_terminal_reservation(stored)
                raise StorageUnavailableError(
                    "nonterminal reservation is missing from memory"
                )
            return self._reservation_view(record, now)

    async def release_reservation(self, reservation_id: str) -> bool:
        """Cancel or release a reservation; absent and terminal IDs are safe."""
        async with self._admission_lock:
            self._require_reconciled()
            now = self._now()
            self._synchronize(now)
            record = self._reservations.get(reservation_id)
            if record is None:
                return False

            with self._authoritative_mutation():
                self._repository.terminalize_reservation(
                    reservation_id,
                    ReservationTerminalReason.CANCELLED,
                    now,
                )
                self._reservations.pop(reservation_id)
            return True

    async def status(self) -> RouterStatusSnapshot:
        async with self._admission_lock:
            now = self._now()
            if not self._recovering:
                self._synchronize(now)

            snapshot: ResourceSnapshot | None
            capacity: Capacity | None
            telemetry_age_ms: int | None
            try:
                snapshot = self._read_telemetry()
            except TelemetryUnavailableError:
                snapshot = None

            if snapshot is None:
                capacity = None
                telemetry_age_ms = None
            else:
                active_reservation_commitments = (
                    record.request.resources
                    for record in self._reservations.values()
                    if record.activated_at is not None
                    and record.request.start_at <= now < record.end_at
                )
                capacity = calculate_capacity(
                    snapshot,
                    self._leases.values(),
                    self.policy,
                    additional_commitments=active_reservation_commitments,
                )
                telemetry_age_ms = max(
                    round((now - snapshot.observed_at).total_seconds() * 1000),
                    0,
                )

            reservation_record = self._nearest_reservation(now)
            reservation = (
                None
                if reservation_record is None
                else self._reservation_view(reservation_record, now)
            )
            state = self._router_state(now, reservation)
            owner = self._exclusive_owner(state, reservation)

            return RouterStatusSnapshot(
                state=state,
                exclusive_owner=owner,
                telemetry=snapshot,
                capacity=capacity,
                telemetry_age_ms=telemetry_age_ms,
                shared_leases=sum(
                    lease.mode is ResourceMode.SHARED
                    for lease in self._leases.values()
                ),
                exclusive_leases=sum(
                    lease.mode is ResourceMode.EXCLUSIVE
                    for lease in self._leases.values()
                ),
                reservation=reservation,
            )

    async def readiness_reason(self) -> str | None:
        async with self._admission_lock:
            if self._recovering:
                return "ROUTER_RECOVERING"
            now = self._now()
            try:
                self._repository.check_health()
            except StorageUnavailableError:
                return "STORAGE_UNAVAILABLE"
            try:
                snapshot = self._read_telemetry()
                self._admission.validate_snapshot_freshness(snapshot, now)
            except TelemetryUnavailableError:
                return "TELEMETRY_UNAVAILABLE"
            return None

    def close(self) -> None:
        try:
            self._telemetry.close()
        finally:
            self._repository.close()

    def _busy(self, reason: BusyReason) -> BusyLeaseResult:
        return BusyLeaseResult(
            reason=reason,
            retry_after_ms=self.policy.busy_retry_after_ms,
        )

    def _synchronize(self, now: datetime) -> None:
        self._expire_stale_leases(now)
        self._reconcile_reservations(now)

    def _expire_stale_leases(self, now: datetime) -> None:
        expired_ids = tuple(
            lease_id
            for lease_id, lease in self._leases.items()
            if not lease.is_active(now)
        )
        with self._authoritative_mutation():
            self._repository.terminalize_leases(
                (
                    (
                        lease_id,
                        LeaseTerminalReason.EXPIRED,
                        self._leases[lease_id].expires_at,
                    )
                    for lease_id in expired_ids
                )
            )
            for lease_id in expired_ids:
                self._leases.pop(lease_id)

    def _reconcile_reservations(self, now: datetime) -> None:
        ended: list[_ReservationRecord] = []
        candidates: list[_ReservationRecord] = []
        for record in self._reservations.values():
            if now >= record.end_at:
                ended.append(record)
                continue
            if (
                now >= record.request.start_at
                and record.activated_at is None
            ):
                candidates.append(record)

        activated: _ReservationRecord | None = None
        if candidates and not self._leases:
            try:
                snapshot = self._read_telemetry()
            except TelemetryUnavailableError:
                snapshot = None

            if snapshot is not None:
                for record in sorted(
                    candidates,
                    key=lambda item: item.request.start_at,
                ):
                    try:
                        decision = self._admission.evaluate_resources(
                            resources=record.request.resources,
                            mode=ResourceMode.EXCLUSIVE,
                            leases=(),
                            snapshot=snapshot,
                            now=now,
                        )
                    except (
                        ClaimExceedsNodeCapacityError,
                        TelemetryUnavailableError,
                    ):
                        continue
                    if decision.granted:
                        activated = record
                        break

        with self._authoritative_mutation():
            self._repository.terminalize_and_activate_reservation(
                ended_reservations=(
                    (record.request.reservation_id, record.end_at)
                    for record in ended
                ),
                activated_reservation=(
                    None
                    if activated is None
                    else (activated.request.reservation_id, now)
                ),
            )
            for record in ended:
                self._reservations.pop(record.request.reservation_id)
            if activated is not None:
                activated.activated_at = now

    @contextmanager
    def _authoritative_mutation(self) -> Iterator[None]:
        """Rebuild resident state when a durable mutation has unknown outcome."""
        try:
            yield
        except Exception:
            self._recovering = True
            try:
                self._reload_authoritative_state()
            except Exception:
                LOGGER.exception(
                    "failed to reload authoritative state after mutation error"
                )
            else:
                self._recovering = False
            raise

    def _reload_authoritative_state(self) -> None:
        leases = {
            lease.lease_id: lease
            for lease in self._repository.load_active_leases()
        }
        reservations = {
            stored.request.reservation_id: self._record_from_stored(stored)
            for stored in self._repository.load_nonterminal_reservations()
        }
        self._leases = leases
        self._reservations = reservations

    def _replay(
        self,
        claim: ResourceClaim,
        request: StoredAcquireRequest,
    ) -> GrantedLeaseResult:
        if request.claim != claim:
            raise RequestIdReusedError(claim.request_id)
        if request.lease_end is LeaseTerminalReason.EXPIRED:
            raise LeaseExpiredError(request.lease_id)
        if request.lease_end is LeaseTerminalReason.RELEASED:
            raise LeaseReleasedError(request.lease_id)

        lease = self._leases.get(request.lease_id)
        if lease is None:
            raise AssertionError("active request record has no active lease")
        return GrantedLeaseResult(lease=lease)

    def _raise_for_missing_lease(self, lease_id: UUID) -> None:
        lifecycle = self._repository.get_lease_lifecycle(lease_id)
        if lifecycle == LeaseTerminalReason.EXPIRED.value:
            raise LeaseExpiredError(lease_id)
        if lifecycle == LeaseTerminalReason.RELEASED.value:
            raise LeaseReleasedError(lease_id)
        raise LeaseNotFoundError(lease_id)

    def _validate_ttl(self, ttl_seconds: int) -> None:
        max_ttl = self.policy.max_lease_ttl_seconds
        if not 1 <= ttl_seconds <= max_ttl:
            raise InvalidLeaseTtlError(ttl_seconds, max_ttl)

    def _validate_reservation_window(
        self,
        request: ReservationRequest,
        *,
        drain_at: datetime,
        lease_deadline: datetime,
        end_at: datetime,
        now: datetime,
    ) -> None:
        if request.drain_before_seconds < (
            self.minimum_reservation_drain_before_seconds
        ):
            raise InvalidReservationError(
                "drain_before_seconds must cover the maximum lease TTL "
                "and reservation safety margin"
            )
        if drain_at <= now:
            raise InvalidReservationError("drain_at must be in the future")
        if not drain_at <= lease_deadline < request.start_at < end_at:
            raise InvalidReservationError(
                "reservation boundaries must form a future non-empty window"
            )

    def _ensure_reservation_does_not_overlap(
        self,
        drain_at: datetime,
        end_at: datetime,
    ) -> None:
        for record in self._reservations.values():
            if drain_at < record.end_at and record.drain_at < end_at:
                raise ReservationConflictError(
                    "reservation drain interval overlaps an existing reservation"
                )

    @staticmethod
    def _raise_for_terminal_reservation(record: StoredReservation) -> None:
        if record.terminal_reason is ReservationTerminalReason.ENDED:
            raise ReservationEndedError(record.request.reservation_id)
        if record.terminal_reason is ReservationTerminalReason.CANCELLED:
            raise ReservationCancelledError(record.request.reservation_id)

    @staticmethod
    def _reservation_view(
        record: _ReservationRecord,
        now: datetime,
    ) -> Reservation:
        if record.activated_at is not None:
            status = ReservationStatus.ACTIVE
        elif now < record.drain_at:
            status = ReservationStatus.SCHEDULED
        else:
            status = ReservationStatus.DRAINING

        return Reservation(
            reservation_id=record.request.reservation_id,
            client_id=record.request.client_id,
            mode=ResourceMode.EXCLUSIVE,
            resources=record.request.resources,
            status=status,
            drain_at=record.drain_at,
            lease_deadline=record.lease_deadline,
            start_at=record.request.start_at,
            end_at=record.end_at,
        )

    def _restrictive_reservation(
        self,
        now: datetime,
    ) -> _ReservationRecord | None:
        restrictive = [
            record
            for record in self._reservations.values()
            if record.drain_at <= now < record.end_at
        ]
        return min(restrictive, key=lambda record: record.drain_at, default=None)

    def _nearest_reservation(self, now: datetime) -> _ReservationRecord | None:
        relevant = [
            record
            for record in self._reservations.values()
            if now < record.end_at
        ]
        return min(relevant, key=lambda record: record.drain_at, default=None)

    def _next_lease_deadline(self, now: datetime) -> datetime | None:
        deadlines = [
            record.lease_deadline
            for record in self._reservations.values()
            if now < record.end_at
        ]
        return min(deadlines, default=None)

    def _router_state(
        self,
        now: datetime,
        reservation: Reservation | None,
    ) -> RouterState:
        if self._recovering:
            return RouterState.RECOVERING
        if reservation is not None and reservation.drain_at <= now:
            if reservation.status is ReservationStatus.ACTIVE:
                return RouterState.EXCLUSIVE
            return RouterState.DRAINING
        if any(
            lease.mode is ResourceMode.EXCLUSIVE for lease in self._leases.values()
        ):
            return RouterState.EXCLUSIVE
        return RouterState.OPEN

    def _exclusive_owner(
        self,
        state: RouterState,
        reservation: Reservation | None,
    ) -> ExclusiveOwner | None:
        if state is not RouterState.EXCLUSIVE:
            return None
        if reservation is not None and reservation.status is ReservationStatus.ACTIVE:
            return ExclusiveOwner(
                owner_type="RESERVATION",
                owner_id=reservation.reservation_id,
            )
        exclusive = next(
            (
                lease
                for lease in self._leases.values()
                if lease.mode is ResourceMode.EXCLUSIVE
            ),
            None,
        )
        if exclusive is None:
            raise AssertionError("EXCLUSIVE state has no owner")
        return ExclusiveOwner(owner_type="LEASE", owner_id=str(exclusive.lease_id))

    def _read_telemetry(self) -> ResourceSnapshot:
        try:
            return self._telemetry.read()
        except TelemetryUnavailableError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise TelemetryUnavailableError(
                "telemetry provider could not produce a valid snapshot"
            ) from error

    def _require_reconciled(self) -> None:
        if self._recovering:
            raise RouterRecoveringError("router state is still recovering")

    @staticmethod
    def _record_from_stored(stored: StoredReservation) -> _ReservationRecord:
        if stored.terminal_reason is not None:
            raise ValueError("cannot load a terminal reservation into memory")
        return _ReservationRecord(
            request=stored.request,
            drain_at=stored.drain_at,
            lease_deadline=stored.lease_deadline,
            end_at=stored.end_at,
            activated_at=stored.activated_at,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now
