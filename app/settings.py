from __future__ import annotations

import os
from dataclasses import dataclass
from math import isfinite
from pathlib import Path


DEFAULT_RECLAIMABLE_CACHE_FRACTION = 0.5


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    reclaimable_cache_fraction: float = DEFAULT_RECLAIMABLE_CACHE_FRACTION

    @classmethod
    def from_environment(cls) -> Settings:
        configured = os.environ.get("JETROUTER_DATABASE_PATH")
        path = (
            Path(configured).expanduser()
            if configured
            else Path("data") / "jetrouter.sqlite3"
        )
        configured_fraction = os.environ.get(
            "JETROUTER_RECLAIMABLE_CACHE_FRACTION"
        )
        reclaimable_cache_fraction = (
            DEFAULT_RECLAIMABLE_CACHE_FRACTION
            if configured_fraction is None
            else _parse_reclaimable_cache_fraction(configured_fraction)
        )
        return cls(
            database_path=path,
            reclaimable_cache_fraction=reclaimable_cache_fraction,
        )


def _parse_reclaimable_cache_fraction(configured: str) -> float:
    try:
        value = float(configured)
    except ValueError as error:
        raise ValueError(
            "JETROUTER_RECLAIMABLE_CACHE_FRACTION must be a finite number "
            "between 0 and 1"
        ) from error
    if not isfinite(value) or not 0 <= value <= 1:
        raise ValueError(
            "JETROUTER_RECLAIMABLE_CACHE_FRACTION must be a finite number "
            "between 0 and 1"
        )
    return value
