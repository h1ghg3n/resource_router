from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    observed_at: datetime

    memory_total_mb: int
    memory_free_mb: int
    memory_cached_mb: int | None
    memory_buffers_mb: int | None
    memory_gpu_shared_mb: int | None
    memory_lfb_mb: int | None

    cpu_total_cores: int | None
    cpu_online_cores: int | None
    cpu_load_percent: float | None
    gpu_load_percent: float | None
    emc_load_percent: float | None

    temperature_max_c: float | None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")

        if self.memory_total_mb <= 0:
            raise ValueError("memory_total_mb must be positive")
        optional_memory_values = (
            self.memory_cached_mb,
            self.memory_buffers_mb,
            self.memory_gpu_shared_mb,
            self.memory_lfb_mb,
        )
        if self.memory_free_mb < 0 or any(
            value is not None and value < 0 for value in optional_memory_values
        ):
            raise ValueError("memory values cannot be negative")
        if self.memory_free_mb > self.memory_total_mb:
            raise ValueError("memory_free_mb cannot exceed memory_total_mb")

        if self.cpu_total_cores is not None and self.cpu_total_cores <= 0:
            raise ValueError("cpu_total_cores must be positive when available")
        if self.cpu_online_cores is not None and self.cpu_online_cores < 0:
            raise ValueError("cpu_online_cores cannot be negative")
        if (
            self.cpu_total_cores is not None
            and self.cpu_online_cores is not None
            and self.cpu_online_cores > self.cpu_total_cores
        ):
            raise ValueError("cpu_online_cores cannot exceed cpu_total_cores")

        if self.cpu_load_percent is not None:
            self._validate_percent("cpu_load_percent", self.cpu_load_percent)
        if self.gpu_load_percent is not None:
            self._validate_percent("gpu_load_percent", self.gpu_load_percent)
        if self.emc_load_percent is not None:
            self._validate_percent("emc_load_percent", self.emc_load_percent)
        if self.temperature_max_c is not None and not isfinite(self.temperature_max_c):
            raise ValueError("temperature_max_c must be finite")

    @staticmethod
    def _validate_percent(name: str, value: float) -> None:
        if not isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"{name} must be between 0 and 100")
