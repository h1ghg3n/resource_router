from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.application import create_app
from app.telemetry.journal import TelemetryJournal
from app.telemetry.provider import StaticTelemetryProvider
from tests.api.test_api_v1 import MutableClock, SwitchableTelemetryProvider, manager_for, snapshot

UTC = timezone.utc


def history_sample(sample_id: int, observed_at: datetime) -> dict[str, object]:
    """Build one complete public schema-v1 fixture without platform I/O."""

    return {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "monotonic_ns": sample_id * 1_000_000,
        "provider_generation": 2,
        "sample_id": sample_id,
        "session_id": "api-v2-test",
        "memory": {
            "total_mb": 16384,
            "free_mb": 8192 - sample_id,
            "cached_mb": 2048,
            "buffers_mb": 32,
            "gpu_shared_mb": 64,
            "lfb_mb": 128,
            "emc_load_percent": 20.0,
            "emc_frequency_hz": 1600000000.0,
        },
        "cpu": {
            "total_cores": 8,
            "online_cores": 8,
            "load_percent": 12.5,
            "cores": [
                {
                    "index": 0,
                    "online": True,
                    "idle_percent": 87.5,
                    "frequency_hz": 2000000000.0,
                }
            ],
        },
        "gpu": {
            "load_percent": float(sample_id),
            "devices": {
                "gpu": {
                    "load_percent": float(sample_id),
                    "frequency_hz": 900000000.0,
                    "maximum_frequency_hz": 1200000000.0,
                }
            },
        },
        "temperature": {"maximum_c": 55.0, "sensors_c": {"tj": 55.0}},
        "power": {"VDD_IN": 12345.0},
        "host_memory": {
            "available_bytes": 8 * 1024**3,
            "cached_bytes": 2 * 1024**3,
            "slab_reclaimable_bytes": 100,
            "slab_unreclaimable_bytes": 200,
            "swap_total_bytes": 2 * 1024**3,
            "swap_free_bytes": 1024**3,
            "swap_cached_bytes": 0,
        },
        "cgroup": {
            "memory_current_bytes": 1024,
            "memory_peak_bytes": 2048,
            "memory_swap_current_bytes": 0,
            "memory_events": {"oom": 0, "oom_kill": 0},
            "cpu_stat": {"usage_usec": 10},
        },
        "unavailable": [],
    }


def journal_for(path: Path) -> TelemetryJournal:
    """Create an inactive journal reader over a private test path."""

    return TelemetryJournal(
        path,
        idle_interval_seconds=30,
        active_interval_seconds=1,
        max_bytes=1024 * 1024,
        backup_count=2,
        session_id="api-v2-test",
    )


def test_current_is_timestamped_and_keeps_v1_status_shape() -> None:
    """Verify current is timestamped and keeps v1 status shape using isolated inputs; assertion failure rejects the regression."""
    now = datetime(2026, 10, 17, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now, gpu_load_percent=25.0))
    client = TestClient(create_app(manager_for(clock, telemetry)))

    response = client.get("/v2/telemetry/current")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["observed_at"] == now.isoformat().replace("+00:00", "Z")
    assert body["router"]["state"] == "OPEN"
    assert body["router"]["telemetry"]["gpu"]["load_percent"] == 25.0
    assert client.get("/v1/status").json() == body["router"]


def test_current_returns_machine_readable_503_when_telemetry_fails() -> None:
    """Verify current returns machine readable 503 when telemetry fails using isolated inputs; assertion failure rejects the regression."""
    now = datetime(2026, 10, 17, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = SwitchableTelemetryProvider(snapshot(now))
    client = TestClient(create_app(manager_for(clock, telemetry)))
    telemetry.unavailable = True

    response = client.get("/v2/telemetry/current")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    assert response.json()["code"] == "TELEMETRY_UNAVAILABLE"


def test_history_is_bounded_filtered_chronological_and_openapi_named(tmp_path: Path) -> None:
    """Verify history is bounded filtered chronological and openapi named using isolated inputs; assertion failure rejects the regression."""
    now = datetime(2026, 10, 17, tzinfo=UTC)
    path = tmp_path / "telemetry.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(history_sample(index, now + timedelta(seconds=index)))
            for index in range(1, 5)
        )
        + "\n"
    )
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    client = TestClient(
        create_app(
            manager_for(clock, telemetry),
            telemetry_history=journal_for(path),
        )
    )

    response = client.get(
        "/v2/telemetry/history",
        params={
            "since": (now + timedelta(seconds=2)).isoformat(),
            "before": (now + timedelta(seconds=5)).isoformat(),
            "limit": 2,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["truncated"] is True
    assert body["partial"] is False
    assert [item["sample_id"] for item in body["samples"]] == [3, 4]
    operation = client.get("/openapi.json").json()["paths"]["/v2/telemetry/history"]["get"]
    assert operation["operationId"] == "getTelemetryHistory"


def test_history_reports_unconfigured_and_invalid_ranges() -> None:
    """Verify history reports unconfigured and invalid ranges using isolated inputs; assertion failure rejects the regression."""
    now = datetime(2026, 10, 17, tzinfo=UTC)
    clock = MutableClock(now)
    telemetry = StaticTelemetryProvider(snapshot(now))
    manager = manager_for(clock, telemetry)
    client = TestClient(create_app(manager))

    unavailable = client.get("/v2/telemetry/history")
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "TELEMETRY_HISTORY_UNAVAILABLE"

    path = Path("unused-telemetry.jsonl")
    configured = TestClient(create_app(manager, telemetry_history=journal_for(path)))
    invalid = configured.get(
        "/v2/telemetry/history",
        params={"since": now.isoformat(), "before": now.isoformat()},
    )
    assert invalid.status_code == 422
    assert invalid.json() == {
        "code": "VALIDATION_ERROR",
        "message": "since must be earlier than before",
    }
