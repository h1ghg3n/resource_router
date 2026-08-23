from enum import Enum


class ResourceMode(str, Enum):
    SHARED = "SHARED"
    EXCLUSIVE = "EXCLUSIVE"


class AcquisitionStatus(str, Enum):
    GRANTED = "GRANTED"
    BUSY = "BUSY"


class BusyReason(str, Enum):
    INSUFFICIENT_MEMORY = "INSUFFICIENT_MEMORY"
    GPU_PRESSURE = "GPU_PRESSURE"
    CPU_PRESSURE = "CPU_PRESSURE"
    CONFLICTING_LEASES = "CONFLICTING_LEASES"
    EXCLUSIVE_ACTIVE = "EXCLUSIVE_ACTIVE"
    EXCLUSIVE_RESERVATION = "EXCLUSIVE_RESERVATION"


class LeaseStatus(str, Enum):
    ACTIVE = "ACTIVE"


class ReservationStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    DRAINING = "DRAINING"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class RouterState(str, Enum):
    RECOVERING = "RECOVERING"
    OPEN = "OPEN"
    DRAINING = "DRAINING"
    EXCLUSIVE = "EXCLUSIVE"
