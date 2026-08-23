from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from math import floor, isfinite
from numbers import Real
from threading import RLock
from typing import Protocol

from app.core.errors import TelemetryUnavailableError
from app.telemetry.snapshot import ResourceSnapshot

UTC = timezone.utc
KIB_PER_MIB = 1024
MIB_PER_LFB_BLOCK = 4


class _JtopClient(Protocol):
    @property
    def stats(self) -> object: ...

    @property
    def memory(self) -> object: ...

    @property
    def cpu(self) -> object: ...

    @property
    def gpu(self) -> object: ...

    @property
    def temperature(self) -> object: ...

    def start(self) -> None: ...

    def ok(self, spin: bool = False) -> bool: ...

    def close(self) -> None: ...


JtopClientFactory = Callable[[float], _JtopClient]
Clock = Callable[[], datetime]


class JtopTelemetryProvider:
    """Normalize a long-lived jetson-stats client into Router telemetry."""

    def __init__(
        self,
        *,
        interval_seconds: float = 1.0,
        client_factory: JtopClientFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive and finite")
        self._interval_seconds = interval_seconds
        self._client_factory = client_factory or _create_jtop_client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client: _JtopClient | None = None
        self._closed = False
        self._lock = RLock()

    def read(self) -> ResourceSnapshot:
        with self._lock:
            if self._closed:
                raise TelemetryUnavailableError("jtop telemetry provider is closed")

            try:
                client = self._ensure_client()
                if not client.ok(spin=True):
                    raise RuntimeError("jtop client is not running")

                memory = _required_dict_like(client.memory, "memory")
                cpu = _optional_dict_like(client, "cpu")
                gpu = _optional_dict_like(client, "gpu")
                temperature = _optional_dict_like(client, "temperature")
                stats = _optional_dict_like(client, "stats")
                return _normalize_snapshot(
                    memory=memory,
                    cpu=cpu,
                    gpu=gpu,
                    temperature=temperature,
                    observed_at=_observed_at(stats.get("time"), self._clock),
                )
            except TelemetryUnavailableError:
                self._discard_client()
                raise
            except Exception as error:
                self._discard_client()
                detail = str(error).strip() or type(error).__name__
                raise TelemetryUnavailableError(
                    f"jtop telemetry is unavailable: {detail}"
                ) from error

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._discard_client()

    def _ensure_client(self) -> _JtopClient:
        if self._client is not None:
            return self._client

        client = self._client_factory(self._interval_seconds)
        try:
            client.start()
        except Exception:
            _safe_close(client)
            raise
        self._client = client
        return client

    def _discard_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            _safe_close(client)


def _create_jtop_client(interval_seconds: float) -> _JtopClient:
    try:
        from jtop import jtop as client_type
    except ImportError as error:
        raise RuntimeError(
            "jetson-stats client is not installed in this Python environment"
        ) from error
    return client_type(interval=interval_seconds)


def _normalize_snapshot(
    *,
    memory: Mapping[str, object],
    cpu: Mapping[str, object],
    gpu: Mapping[str, object],
    temperature: Mapping[str, object],
    observed_at: datetime,
) -> ResourceSnapshot:
    ram = _nested_mapping(memory, "RAM")
    if ram is None:
        raise ValueError("jtop memory telemetry has no RAM section")

    total_mb = _required_kib_to_mib(ram.get("tot"), "RAM.tot")
    free_mb = _required_kib_to_mib(ram.get("free"), "RAM.free")
    if total_mb <= 0:
        raise ValueError("jtop RAM.tot must be at least one MiB")
    if free_mb > total_mb:
        raise ValueError("jtop RAM.free exceeds RAM.tot")

    cpu_total_cores, cpu_online_cores, cpu_load_percent = _cpu_metrics(cpu)

    return ResourceSnapshot(
        observed_at=observed_at,
        memory_total_mb=total_mb,
        memory_free_mb=free_mb,
        memory_cached_mb=_optional_kib_to_mib(ram.get("cached")),
        memory_buffers_mb=_optional_kib_to_mib(ram.get("buffers")),
        memory_gpu_shared_mb=_optional_kib_to_mib(ram.get("shared")),
        memory_lfb_mb=_lfb_megabytes(ram.get("lfb")),
        cpu_total_cores=cpu_total_cores,
        cpu_online_cores=cpu_online_cores,
        cpu_load_percent=cpu_load_percent,
        gpu_load_percent=_gpu_load_percent(gpu),
        emc_load_percent=_emc_load_percent(memory),
        temperature_max_c=_maximum_temperature(temperature),
    )


def _cpu_metrics(
    cpu: Mapping[str, object],
) -> tuple[int | None, int | None, float | None]:
    raw_cores = cpu.get("cpu")
    if (
        not isinstance(raw_cores, Sequence)
        or isinstance(raw_cores, (str, bytes, bytearray))
        or not raw_cores
    ):
        total_cores = None
        online_cores = None
    else:
        total_cores = len(raw_cores)
        online_values = [
            core.get("online") if isinstance(core, Mapping) else None
            for core in raw_cores
        ]
        online_cores = (
            sum(value is True for value in online_values)
            if all(isinstance(value, bool) for value in online_values)
            else None
        )

    aggregate = _nested_mapping(cpu, "total")
    idle_percent = None if aggregate is None else _percentage(aggregate.get("idle"))
    load_percent = None if idle_percent is None else 100.0 - idle_percent
    return total_cores, online_cores, load_percent


def _gpu_load_percent(gpu: Mapping[str, object]) -> float | None:
    loads: list[float] = []
    for raw_device in gpu.values():
        if not isinstance(raw_device, Mapping):
            continue
        status = raw_device.get("status")
        if not isinstance(status, Mapping):
            continue
        load = _percentage(status.get("load"))
        if load is not None:
            loads.append(load)
    return max(loads, default=None)


def _emc_load_percent(memory: Mapping[str, object]) -> float | None:
    emc = _nested_mapping(memory, "EMC")
    if emc is None or emc.get("online") is False:
        return None
    return _percentage(emc.get("val"))


def _maximum_temperature(temperature: Mapping[str, object]) -> float | None:
    readings: list[float] = []
    for raw_sensor in temperature.values():
        if not isinstance(raw_sensor, Mapping) or raw_sensor.get("online") is not True:
            continue
        reading = _finite_number(raw_sensor.get("temp"), minimum=None)
        if reading is not None:
            readings.append(reading)
    return max(readings, default=None)


def _required_dict_like(value: object, label: str) -> dict[str, object]:
    try:
        copied = deepcopy(dict(value))  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"jtop {label} telemetry is not dictionary-like") from error
    return {str(key): item for key, item in copied.items()}


def _optional_dict_like(client: object, attribute: str) -> dict[str, object]:
    try:
        return _required_dict_like(getattr(client, attribute), attribute)
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}


def _nested_mapping(
    source: Mapping[str, object],
    key: str,
) -> Mapping[str, object] | None:
    value = source.get(key)
    return value if isinstance(value, Mapping) else None


def _required_kib_to_mib(value: object, label: str) -> int:
    converted = _optional_kib_to_mib(value)
    if converted is None:
        raise ValueError(f"jtop {label} is missing or invalid")
    return converted


def _optional_kib_to_mib(value: object) -> int | None:
    number = _finite_number(value, minimum=0)
    return None if number is None else floor(number / KIB_PER_MIB)


def _lfb_megabytes(value: object) -> int | None:
    blocks = _finite_number(value, minimum=0)
    return None if blocks is None else floor(blocks) * MIB_PER_LFB_BLOCK


def _percentage(value: object) -> float | None:
    return _finite_number(value, minimum=0, maximum=100)


def _finite_number(
    value: object,
    *,
    minimum: float | None,
    maximum: float | None = None,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    if not isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _observed_at(value: object, clock: Clock) -> datetime:
    observed_at = value if isinstance(value, datetime) else clock()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        observed_at = observed_at.astimezone()
    return observed_at.astimezone(UTC)


def _safe_close(client: _JtopClient) -> None:
    try:
        client.close()
    except Exception:
        pass
