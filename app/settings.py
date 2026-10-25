from __future__ import annotations

import os
import re
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from app.telemetry.journal import MAX_BACKUP_COUNT

DEFAULT_RECLAIMABLE_CACHE_FRACTION = 0.5
DEFAULT_TELEMETRY_IDLE_INTERVAL_SECONDS = 30.0
DEFAULT_TELEMETRY_ACTIVE_INTERVAL_SECONDS = 1.0
DEFAULT_TELEMETRY_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_TELEMETRY_BACKUP_COUNT = 4


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    reclaimable_cache_fraction: float = DEFAULT_RECLAIMABLE_CACHE_FRACTION
    telemetry_journal_path: Path | None = None
    telemetry_idle_interval_seconds: float = DEFAULT_TELEMETRY_IDLE_INTERVAL_SECONDS
    telemetry_active_interval_seconds: float = DEFAULT_TELEMETRY_ACTIVE_INTERVAL_SECONDS
    telemetry_max_bytes: int = DEFAULT_TELEMETRY_MAX_BYTES
    telemetry_backup_count: int = DEFAULT_TELEMETRY_BACKUP_COUNT
    telemetry_session_id: str = "resource-router"

    @classmethod
    def from_environment(cls) -> Settings:
        """Parse strict Router and optional journal configuration from env.

        The journal stays disabled unless a path is provided. Invalid numeric
        bounds or unsafe correlation labels fail startup before any service or
        file mutation, while omitted values use bounded defaults.
        """

        configured = os.environ.get("JETROUTER_DATABASE_PATH")
        path = Path(configured).expanduser() if configured else Path("data") / "jetrouter.sqlite3"
        configured_fraction = os.environ.get("JETROUTER_RECLAIMABLE_CACHE_FRACTION")
        reclaimable_cache_fraction = (
            DEFAULT_RECLAIMABLE_CACHE_FRACTION
            if configured_fraction is None
            else _parse_reclaimable_cache_fraction(configured_fraction)
        )
        journal = os.environ.get("JETROUTER_TELEMETRY_JOURNAL_PATH")
        telemetry_journal_path = Path(journal).expanduser() if journal else None
        backup_count = _positive_int_environment(
            "JETROUTER_TELEMETRY_BACKUP_COUNT", DEFAULT_TELEMETRY_BACKUP_COUNT
        )
        if backup_count > MAX_BACKUP_COUNT:
            raise ValueError(f"JETROUTER_TELEMETRY_BACKUP_COUNT must be at most {MAX_BACKUP_COUNT}")
        return cls(
            database_path=path,
            reclaimable_cache_fraction=reclaimable_cache_fraction,
            telemetry_journal_path=telemetry_journal_path,
            telemetry_idle_interval_seconds=_positive_float_environment(
                "JETROUTER_TELEMETRY_IDLE_SECONDS",
                DEFAULT_TELEMETRY_IDLE_INTERVAL_SECONDS,
            ),
            telemetry_active_interval_seconds=_positive_float_environment(
                "JETROUTER_TELEMETRY_ACTIVE_SECONDS",
                DEFAULT_TELEMETRY_ACTIVE_INTERVAL_SECONDS,
            ),
            telemetry_max_bytes=_positive_int_environment(
                "JETROUTER_TELEMETRY_MAX_BYTES",
                DEFAULT_TELEMETRY_MAX_BYTES,
            ),
            telemetry_backup_count=backup_count,
            telemetry_session_id=_session_id_environment(
                "JETROUTER_TELEMETRY_SESSION_ID", "resource-router"
            ),
        )


def _parse_reclaimable_cache_fraction(configured: str) -> float:
    try:
        value = float(configured)
    except ValueError as error:
        raise ValueError(
            "JETROUTER_RECLAIMABLE_CACHE_FRACTION must be a finite number between 0 and 1"
        ) from error
    if not isfinite(value) or not 0 <= value <= 1:
        raise ValueError(
            "JETROUTER_RECLAIMABLE_CACHE_FRACTION must be a finite number between 0 and 1"
        )
    return value


def _positive_float_environment(name: str, default: float) -> float:
    """Parse one finite positive duration without accepting silent fallbacks."""

    configured = os.environ.get(name)
    if configured is None:
        return default
    try:
        value = float(configured)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive finite number") from error
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _positive_int_environment(name: str, default: int) -> int:
    """Parse one strictly positive decimal bound for retention settings."""

    configured = os.environ.get(name)
    if configured is None:
        return default
    try:
        value = int(configured)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _session_id_environment(name: str, default: str) -> str:
    """Accept a short opaque correlation label that cannot embed secret text."""

    value = os.environ.get(name, default)
    if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) is None:
        raise ValueError(f"{name} must use 1-64 safe identifier characters")
    return value
