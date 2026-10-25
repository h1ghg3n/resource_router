from __future__ import annotations

import json
import os
import queue
import stat
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any

from app.telemetry.snapshot import ResourceSnapshot

Record = dict[str, object]
Sampler = Callable[[], ResourceSnapshot | None]
MAX_HISTORY_LIMIT = 1000
MAX_RECORD_BYTES = 64 * 1024
MAX_BACKUP_COUNT = 16


@dataclass(frozen=True, slots=True)
class TelemetryHistory:
    """Describe one bounded, chronological journal query result.

    ``records`` contains only valid schema-v1 objects inside the requested UTC
    interval. ``truncated`` means newer matching records were preferred after
    the caller's limit was reached. Read and decode failures are counted
    without exposing filesystem paths or making the live Router unavailable.
    """

    records: tuple[Record, ...]
    truncated: bool
    read_errors: int
    skipped_records: int


def build_record(
    *,
    snapshot: ResourceSnapshot,
    memory: Mapping[str, object],
    cpu: Mapping[str, object],
    gpu: Mapping[str, object],
    temperature: Mapping[str, object],
    stats: Mapping[str, object],
    generation: int,
    monotonic_ns: int | None = None,
) -> Record:
    """Build one secret-free v1 record from a successful jtop sample.

    Input mappings are treated as untrusted platform telemetry; only known
    numeric fields are copied. The returned mapping contains no prompts,
    outputs, environment values, paths, or credentials and performs no I/O.
    Missing platform fields remain null or empty rather than failing admission.
    """

    return {
        "schema_version": 1,
        "observed_at": snapshot.observed_at.isoformat(),
        "monotonic_ns": monotonic_ns if monotonic_ns is not None else time.monotonic_ns(),
        "provider_generation": generation,
        "memory": {
            "total_mb": snapshot.memory_total_mb,
            "free_mb": snapshot.memory_free_mb,
            "cached_mb": snapshot.memory_cached_mb,
            "buffers_mb": snapshot.memory_buffers_mb,
            "gpu_shared_mb": snapshot.memory_gpu_shared_mb,
            "lfb_mb": snapshot.memory_lfb_mb,
            "emc_load_percent": snapshot.emc_load_percent,
            "emc_frequency_hz": _frequency_hz(_mapping(memory.get("EMC")), "cur"),
        },
        "cpu": {
            "total_cores": snapshot.cpu_total_cores,
            "online_cores": snapshot.cpu_online_cores,
            "load_percent": snapshot.cpu_load_percent,
            "cores": _cpu_cores(cpu),
        },
        "gpu": {
            "load_percent": snapshot.gpu_load_percent,
            "devices": _gpu_devices(gpu),
        },
        "temperature": {
            "maximum_c": snapshot.temperature_max_c,
            "sensors_c": _temperature_sensors(temperature),
        },
        "power": _power_rails(stats),
    }


class TelemetryJournal:
    """Sample and rotate bounded JSONL without blocking Router operations.

    The journal owns one daemon thread and a capacity-one latest-record slot.
    The sampler uses the existing provider's best-effort try_read method; it
    skips a contended lock but SDK I/O after acquiring it can still take time. Slow
    writes, rotation errors, ENOSPC, malformed records, and sampling failures
    only increment counters or permanently disable file output; they never
    propagate into admission, lease cleanup, or service shutdown.
    """

    def __init__(
        self,
        path: Path,
        *,
        idle_interval_seconds: float,
        active_interval_seconds: float,
        max_bytes: int,
        backup_count: int,
        session_id: str,
    ) -> None:
        """Pin bounded retention and cadence; create no file or thread until explicitly started.

        A caller supplies one local journal path and a short nonsecret session
        label. Invalid sizes, durations or rotation counts fail before mutation.
        File failures later disable this optional observer, never admission.
        """
        if (
            any(
                type(v) not in (int, float) or not isfinite(v) or v <= 0
                for v in (idle_interval_seconds, active_interval_seconds)
            )
            or type(max_bytes) is not int
            or max_bytes <= 0
            or type(backup_count) is not int
            or not 1 <= backup_count <= MAX_BACKUP_COUNT
            or not isinstance(session_id, str)
            or not 1 <= len(session_id) <= 64
        ):
            raise ValueError("Invalid bounded telemetry journal configuration")
        self._path = path
        self._idle_interval = idle_interval_seconds
        self._active_interval = active_interval_seconds
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._session_id = session_id
        self._slot: queue.Queue[Record] = queue.Queue(maxsize=1)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sampler: Sampler | None = None
        self._sequence = 0
        self._active_until = 0.0
        self._disabled = False
        self._offered = 0
        self._dropped = 0
        self._written = 0
        self._errors = 0

    def offer(self, record: Mapping[str, object]) -> None:
        """Replace the pending sample without waiting for disk or the writer."""

        if self._disabled:
            return
        copied = dict(record)
        self._offered += 1
        load = _nested_number(copied, "gpu", "load_percent")
        if load is not None and load > 0.5:
            self._active_until = time.monotonic() + max(30.0, self._idle_interval)
        try:
            self._slot.put_nowait(copied)
        except queue.Full:
            try:
                self._slot.get_nowait()
            except queue.Empty:
                pass
            self._dropped += 1
            try:
                self._slot.put_nowait(copied)
            except queue.Full:
                self._dropped += 1

    def start(self, sampler: Sampler) -> None:
        """Start one idempotent daemon loop with the shared provider's best-effort sampler."""

        if self._thread is not None:
            return
        self._sampler = sampler
        self._thread = threading.Thread(
            target=self._run,
            name="telemetry-jsonl",
            daemon=True,
        )
        self._thread.start()

    def close(self, timeout_seconds: float = 2.0) -> None:
        """Request bounded shutdown; an unresponsive writer is left as a daemon."""

        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout_seconds))

    def stats(self) -> dict[str, int | bool]:
        """Return nonsecret process-local counters for tests and diagnostics."""

        return {
            "offered": self._offered,
            "dropped": self._dropped,
            "written": self._written,
            "errors": self._errors,
            "disabled": self._disabled,
        }

    def read_history(
        self,
        *,
        since: datetime | None,
        before: datetime | None,
        limit: int,
    ) -> TelemetryHistory:
        """Read recent valid records without sharing writer state or file handles.

        Bounds must be timezone-aware and define ``since <= observed_at < before``.
        The newest matching records win when more than ``limit`` exist, while the
        written records win by append recency; the selected tuple is sorted by
        observation time for consumers, including wall-clock rollback. Rotation may
        occur concurrently; duplicate records are removed by their nonsecret
        sample identity. Missing, malformed, partial, or unreadable files are
        skipped and reported through counters instead of affecting admission.
        """

        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_HISTORY_LIMIT}")
        for name, value in (("since", since), ("before", before)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must include a timezone")
        if since is not None and before is not None and since >= before:
            raise ValueError("since must be earlier than before")

        newest: list[Record] = []
        identities: set[tuple[object, ...]] = set()
        read_errors = 0
        skipped_records = 0
        for path in self._history_paths_newest_first():
            try:
                lines = _reverse_lines(path)
                for raw in lines:
                    try:
                        record = json.loads(raw)
                        if not isinstance(record, dict) or record.get("schema_version") != 1:
                            raise ValueError("unsupported telemetry record")
                        observed_at = _record_time(record)
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                        skipped_records += 1
                        continue
                    if before is not None and observed_at >= before:
                        continue
                    if since is not None and observed_at < since:
                        continue
                    identity = (
                        record.get("session_id"),
                        record.get("sample_id"),
                        record.get("monotonic_ns"),
                        record.get("observed_at"),
                    )
                    try:
                        hash(identity)
                    except TypeError:
                        skipped_records += 1
                        continue
                    if identity in identities:
                        continue
                    identities.add(identity)
                    newest.append(record)
                    if len(newest) > limit:
                        return TelemetryHistory(
                            records=tuple(sorted(newest[:limit], key=_record_time)),
                            truncated=True,
                            read_errors=read_errors,
                            skipped_records=skipped_records,
                        )
            except OSError:
                read_errors += 1
        return TelemetryHistory(
            records=tuple(sorted(newest, key=_record_time)),
            truncated=False,
            read_errors=read_errors,
            skipped_records=skipped_records,
        )

    def _history_paths_newest_first(self) -> tuple[Path, ...]:
        """Return the fixed current-to-oldest path set without reading directories."""

        return (self._path,) + tuple(
            self._path.with_name(f"{self._path.name}.{index}")
            for index in range(1, self._backup_count + 1)
        )

    def _run(self) -> None:
        """Own cadence, sampling and writes while containing every failure."""

        next_sample = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_sample:
                sampler = self._sampler
                if sampler is not None:
                    try:
                        sampler()
                    except Exception:
                        self._errors += 1
                interval = (
                    self._active_interval
                    if time.monotonic() < self._active_until
                    else self._idle_interval
                )
                next_sample = time.monotonic() + interval
                self._write_latest()
            wait_for = max(0.01, min(0.25, next_sample - time.monotonic()))
            self._wake.wait(wait_for)
            self._wake.clear()
        self._write_latest()

    def _write_latest(self) -> None:
        """Write at most one queued record and disable output after I/O failure."""

        if self._disabled:
            return
        try:
            record = self._slot.get_nowait()
        except queue.Empty:
            return
        try:
            self._sequence += 1
            host_memory, host_unavailable = _read_host_memory()
            cgroup, cgroup_unavailable = _read_cgroup()
            enriched = {
                **record,
                "sample_id": self._sequence,
                "session_id": self._session_id,
                "host_memory": host_memory,
                "cgroup": cgroup,
                "unavailable": sorted(set([*host_unavailable, *cgroup_unavailable])),
            }
            encoded = (
                json.dumps(enriched, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n"
            ).encode("utf-8")
            if len(encoded) > min(self._max_bytes, MAX_RECORD_BYTES):
                raise ValueError("Telemetry record exceeds the configured file/record limit")
            self._prepare_path(len(encoded))
            descriptor = os.open(
                self._path,
                os.O_APPEND
                | os.O_CREAT
                | os.O_WRONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            with os.fdopen(descriptor, "ab") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Telemetry output must be a regular file")
                stream.write(encoded)
            self._written += 1
        except Exception:
            self._errors += 1
            self._disabled = True

    def _prepare_path(self, incoming_bytes: int) -> None:
        """Create a private parent if absent and rotate only regular files before the size bound.

        Existing parent permissions stay unchanged; the lease database may share
        that directory. Symlinks and nonregular rotation targets are rejected.
        """

        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in self._history_paths_newest_first():
            if path.is_symlink() or path.exists() and not path.is_file():
                raise ValueError("Telemetry rotation requires regular non-symlink files")
        current = self._path.stat().st_size if self._path.exists() else 0
        if current == 0 or current + incoming_bytes <= self._max_bytes:
            return
        oldest = self._path.with_name(f"{self._path.name}.{self._backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backup_count - 1, 0, -1):
            source = self._path.with_name(f"{self._path.name}.{index}")
            if source.exists():
                source.replace(self._path.with_name(f"{self._path.name}.{index + 1}"))
        self._path.replace(self._path.with_name(f"{self._path.name}.1"))


def _mapping(value: object) -> Mapping[str, object]:
    """Use only mapping-shaped platform data; malformed optional sections become empty mappings."""
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    """Return a finite numeric observation, rejecting booleans and malformed values as null."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _first_number(source: Mapping[str, object], keys: Sequence[str]) -> float | None:
    """Select the first finite optional metric from known aliases without platform reads."""
    for key in keys:
        value = _number(source.get(key))
        if value is not None:
            return value
    return None


def _frequency_hz(source: Mapping[str, object], key: str) -> float | None:
    """Convert a documented nonnegative jetson-stats kHz field to Hz; unknown units or invalid values remain null."""
    value = _number(source.get(key))
    if value is None or value < 0:
        return None
    converted = value * 1000
    return converted if isfinite(converted) else None


def _cpu_cores(cpu: Mapping[str, object]) -> list[dict[str, object]]:
    """Project reported cores to numeric or nullable fields; malformed outer topology becomes an empty list."""
    raw = cpu.get("cpu")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    cores: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        core = _mapping(item)
        cores.append(
            {
                "index": index,
                "online": core.get("online") if isinstance(core.get("online"), bool) else None,
                "idle_percent": _first_number(core, ("idle",)),
                "frequency_hz": _frequency_hz(_mapping(core.get("freq")), "cur"),
            }
        )
    return cores


def _gpu_devices(gpu: Mapping[str, object]) -> dict[str, dict[str, float | None]]:
    """Project device telemetry to known numeric load/frequency fields without reading host files."""
    devices: dict[str, dict[str, float | None]] = {}
    for name, item in gpu.items():
        device = _mapping(item)
        status = _mapping(device.get("status"))
        frequency = _mapping(device.get("freq"))
        devices[str(name)] = {
            "load_percent": _first_number(status, ("load",)),
            "frequency_hz": _frequency_hz(frequency, "cur"),
            "maximum_frequency_hz": _frequency_hz(frequency, "max"),
        }
    return devices


def _temperature_sensors(temperature: Mapping[str, object]) -> dict[str, float]:
    """Return finite temperatures for explicitly online sensors, omitting unavailable values."""
    sensors: dict[str, float] = {}
    for name, item in temperature.items():
        sensor = _mapping(item)
        if sensor.get("online") is not True:
            continue
        value = _number(sensor.get("temp"))
        if value is not None:
            sensors[str(name)] = value
    return sensors


def _power_rails(stats: Mapping[str, object]) -> dict[str, float]:
    """Copy only finite power-rail measurements from platform telemetry, not arbitrary text fields."""
    rails: dict[str, float] = {}
    for name, value in stats.items():
        lowered = str(name).lower()
        number = _number(value)
        if number is not None and ("power" in lowered or lowered.startswith("vdd")):
            rails[str(name)] = number
    return rails


def _read_host_memory() -> tuple[dict[str, int | None], list[str]]:
    """Read fixed allowlisted proc memory counters as bytes; inaccessible or malformed data returns nulls plus an unavailable marker."""
    wanted = {
        "MemAvailable": "available_bytes",
        "Cached": "cached_bytes",
        "SReclaimable": "slab_reclaimable_bytes",
        "SUnreclaim": "slab_unreclaimable_bytes",
        "SwapTotal": "swap_total_bytes",
        "SwapFree": "swap_free_bytes",
        "SwapCached": "swap_cached_bytes",
    }
    result: dict[str, int | None] = {target: None for target in wanted.values()}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            target = wanted.get(key)
            if target is not None:
                result[target] = int(rest.split()[0]) * 1024
        return result, []
    except (OSError, ValueError, IndexError):
        return result, ["host_memory"]


def _read_cgroup() -> tuple[dict[str, Any], list[str]]:
    """Read only fixed numeric counters in the current cgroup namespace; contain read/parse failures without host control."""
    root = Path("/sys/fs/cgroup")
    result: dict[str, Any] = {
        "memory_current_bytes": None,
        "memory_peak_bytes": None,
        "memory_swap_current_bytes": None,
        "memory_events": {},
        "cpu_stat": {},
    }
    try:
        for filename, key in (
            ("memory.current", "memory_current_bytes"),
            ("memory.peak", "memory_peak_bytes"),
            ("memory.swap.current", "memory_swap_current_bytes"),
        ):
            path = root / filename
            if path.exists():
                result[key] = int(path.read_text())
        for filename, key in (("memory.events", "memory_events"), ("cpu.stat", "cpu_stat")):
            path = root / filename
            if path.exists():
                result[key] = {
                    name: int(value)
                    for name, value in (line.split() for line in path.read_text().splitlines())
                }
        return result, []
    except (OSError, ValueError):
        return result, ["cgroup"]


def _nested_number(record: Mapping[str, object], section: str, key: str) -> float | None:
    """Read one finite optional numeric field from a known record section without side effects."""
    return _number(_mapping(record.get(section)).get(key))


def _reverse_lines(path: Path, chunk_size: int = 64 * 1024):
    """Yield non-empty binary lines newest first from one regular journal file.

    The iterator reads fixed-size chunks from the end and caps each line at
    MAX_RECORD_BYTES, yielding an invalid sentinel for oversized records so the
    caller can report partial history without retaining a growing buffer.
    Symlinks and non-regular entries
    are rejected; a concurrent rotation may raise ``OSError`` for the caller to
    count as a partial read.
    """

    if path.is_symlink() or not path.is_file():
        return
    with path.open("rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        remainder = b""
        oversized = False
        while position > 0:
            size = min(chunk_size, position)
            position -= size
            stream.seek(position)
            block = stream.read(size)
            pieces = block.split(b"\n")
            tail_size = len(pieces[-1]) + len(remainder)
            tail_bad = oversized or tail_size > MAX_RECORD_BYTES
            tail = b"!" if tail_bad else pieces[-1] + remainder
            if len(pieces) == 1:
                remainder = b"" if tail_bad else tail
                oversized = tail_bad
                continue
            if tail:
                yield tail
            for line in reversed(pieces[1:-1]):
                if line:
                    yield line if len(line) <= MAX_RECORD_BYTES else b"!"
            remainder = pieces[0]
            oversized = len(remainder) > MAX_RECORD_BYTES
            if oversized:
                remainder = b""
        if oversized or remainder:
            yield b"!" if oversized else remainder


def _record_time(record: Mapping[str, object]) -> datetime:
    """Parse one timezone-aware ISO observation timestamp from a journal record."""

    value = record.get("observed_at")
    if not isinstance(value, str):
        raise ValueError("observed_at is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return parsed
