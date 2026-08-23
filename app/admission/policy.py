from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    system_reserve_mb: int = 1024
    emergency_reserve_mb: int = 512
    reclaimable_cache_fraction: float = 0.5
    telemetry_max_age_seconds: float = 5.0
    telemetry_future_tolerance_seconds: float = 1.0
    gpu_pressure_threshold_percent: float = 90.0
    cpu_pressure_threshold_percent: float = 95.0
    system_cpu_reserve_cores: float = 0.0
    max_lease_ttl_seconds: int = 60
    reservation_safety_margin_seconds: int = 5
    busy_retry_after_ms: int = 3000
    unavailable_retry_after_ms: int = 3000

    def __post_init__(self) -> None:
        if self.system_reserve_mb < 0 or self.emergency_reserve_mb < 0:
            raise ValueError("memory reserves cannot be negative")
        if not 0 <= self.reclaimable_cache_fraction <= 1:
            raise ValueError("reclaimable_cache_fraction must be between 0 and 1")
        if self.telemetry_max_age_seconds <= 0:
            raise ValueError("telemetry_max_age_seconds must be positive")
        if self.telemetry_future_tolerance_seconds < 0:
            raise ValueError("telemetry_future_tolerance_seconds cannot be negative")
        self._validate_percent(
            "gpu_pressure_threshold_percent", self.gpu_pressure_threshold_percent
        )
        self._validate_percent(
            "cpu_pressure_threshold_percent", self.cpu_pressure_threshold_percent
        )
        if (
            not isfinite(self.system_cpu_reserve_cores)
            or self.system_cpu_reserve_cores < 0
        ):
            raise ValueError("system_cpu_reserve_cores must be non-negative and finite")
        if self.max_lease_ttl_seconds <= 0:
            raise ValueError("max_lease_ttl_seconds must be positive")
        if self.reservation_safety_margin_seconds <= 0:
            raise ValueError("reservation_safety_margin_seconds must be positive")
        if self.busy_retry_after_ms <= 0:
            raise ValueError("busy_retry_after_ms must be positive")
        if self.unavailable_retry_after_ms <= 0:
            raise ValueError("unavailable_retry_after_ms must be positive")

    @staticmethod
    def _validate_percent(name: str, value: float) -> None:
        if not isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"{name} must be between 0 and 100")
