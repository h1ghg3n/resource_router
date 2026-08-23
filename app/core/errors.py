from uuid import UUID


class ResourceRouterError(Exception):
    """Base exception for Resource Router domain failures."""


class TelemetryUnavailableError(ResourceRouterError):
    """Raised when a telemetry provider cannot produce a safe snapshot."""


class RouterRecoveringError(ResourceRouterError):
    """Raised while persisted state has not been authoritatively reconciled."""


class StorageUnavailableError(ResourceRouterError):
    """Raised when authoritative persisted state cannot be read or committed."""


class ClaimExceedsNodeCapacityError(ResourceRouterError):
    """Raised when a claim can never fit the node's maximum capacity."""

    def __init__(self, resource: str, requested: float, maximum: float) -> None:
        self.resource = resource
        self.requested = requested
        self.maximum = maximum
        super().__init__(
            f"requested {resource} capacity {requested} exceeds node maximum {maximum}"
        )


class RequestIdReusedError(ResourceRouterError):
    """Raised when a successful request ID is reused with different input."""

    def __init__(self, request_id: UUID) -> None:
        self.request_id = request_id
        super().__init__(f"request_id {request_id} is bound to another request")


class LeaseLifecycleError(ResourceRouterError):
    """Base exception for a known lease in a terminal lifecycle state."""

    def __init__(self, lease_id: UUID) -> None:
        self.lease_id = lease_id
        super().__init__(f"lease {lease_id} is {self.__class__.__name__}")


class LeaseExpiredError(LeaseLifecycleError):
    """Raised when an operation targets a known expired lease."""


class LeaseReleasedError(LeaseLifecycleError):
    """Raised when an operation targets a known released lease."""


class LeaseNotFoundError(ResourceRouterError):
    """Raised when an operation requires a known lease."""

    def __init__(self, lease_id: UUID) -> None:
        self.lease_id = lease_id
        super().__init__(f"lease {lease_id} was not found")


class InvalidLeaseTtlError(ResourceRouterError):
    """Raised when a requested TTL exceeds the configured lease limit."""

    def __init__(self, ttl_seconds: int, max_ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_ttl_seconds = max_ttl_seconds
        super().__init__(
            f"ttl_seconds must be between 1 and {max_ttl_seconds}, got {ttl_seconds}"
        )


class ExclusiveReservationError(ResourceRouterError):
    """Raised when a reservation leaves no positive renewal interval."""


class InvalidReservationError(ResourceRouterError):
    """Raised when reservation timing violates configured v1 limits."""


class ReservationNotFoundError(ResourceRouterError):
    def __init__(self, reservation_id: str) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"reservation {reservation_id} was not found")


class ReservationEndedError(ResourceRouterError):
    def __init__(self, reservation_id: str) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"reservation {reservation_id} has ended")


class ReservationCancelledError(ResourceRouterError):
    def __init__(self, reservation_id: str) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"reservation {reservation_id} was cancelled or released")


class ReservationIdReusedError(ResourceRouterError):
    def __init__(self, reservation_id: str) -> None:
        self.reservation_id = reservation_id
        super().__init__(
            f"reservation_id {reservation_id} is bound to another reservation"
        )


class ReservationConflictError(ResourceRouterError):
    """Raised when reservation drain windows overlap."""
