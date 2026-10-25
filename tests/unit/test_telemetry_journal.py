from __future__ import annotations

import json
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.telemetry.journal import MAX_RECORD_BYTES, TelemetryJournal, build_record
from app.telemetry.snapshot import ResourceSnapshot

UTC = timezone.utc


def snapshot(*, gpu_load: float = 0.0) -> ResourceSnapshot:
    """Build a fixed isolated resource snapshot, varying only the synthetic GPU load."""
    return ResourceSnapshot(
        observed_at=datetime(2026, 10, 16, tzinfo=UTC),
        memory_total_mb=16384,
        memory_free_mb=8192,
        memory_cached_mb=2048,
        memory_buffers_mb=32,
        memory_gpu_shared_mb=64,
        memory_lfb_mb=128,
        cpu_total_cores=8,
        cpu_online_cores=8,
        cpu_load_percent=12.5,
        gpu_load_percent=gpu_load,
        emc_load_percent=20.0,
        temperature_max_c=55.0,
    )


def record(*, gpu_load: float = 0.0) -> dict[str, object]:
    """Build a sanitized synthetic journal sample with fixed identity and no host I/O."""
    return build_record(
        snapshot=snapshot(gpu_load=gpu_load),
        memory={"EMC": {"cur": 1600000}},
        cpu={"cpu": [{"online": True, "idle": 80, "freq": {"cur": 2000000}}]},
        gpu={"gpu": {"status": {"load": gpu_load}, "freq": {"cur": 900000, "max": 1300000}}},
        temperature={"tj": {"online": True, "temp": 55}},
        stats={"VDD_IN": 12345, "secret": "never-copy"},
        generation=2,
        monotonic_ns=123,
    )


def wait_until(predicate, timeout: float = 2.0) -> None:
    """Wait for a bounded asynchronous test condition; raise if the synthetic writer does not reach it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_build_record_is_versioned_bounded_and_secret_free() -> None:
    """Verify build record is versioned bounded and secret free using isolated inputs; assertion failure rejects the regression."""
    result = record(gpu_load=75.0)

    assert result["schema_version"] == 1
    assert result["monotonic_ns"] == 123
    assert result["provider_generation"] == 2
    assert result["gpu"]["load_percent"] == 75.0
    assert result["power"] == {"VDD_IN": 12345.0}
    assert result["memory"]["emc_frequency_hz"] == 1600000000
    assert result["cpu"]["cores"][0]["frequency_hz"] == 2000000000
    assert result["gpu"]["devices"]["gpu"]["frequency_hz"] == 900000000
    assert result["gpu"]["devices"]["gpu"]["maximum_frequency_hz"] == 1300000000
    assert "secret" not in json.dumps(result)


def test_latest_slot_drops_old_sample_and_writer_stays_bounded(tmp_path: Path) -> None:
    """Verify latest slot drops old sample and writer stays bounded using isolated inputs; assertion failure rejects the regression."""
    path = tmp_path / "telemetry.jsonl"
    journal = TelemetryJournal(
        path,
        idle_interval_seconds=0.01,
        active_interval_seconds=0.01,
        max_bytes=1024 * 1024,
        backup_count=2,
        session_id="test-session",
    )
    journal.offer(record(gpu_load=1.0))
    journal.offer(record(gpu_load=2.0))
    journal.start(lambda: None)
    wait_until(lambda: journal.stats()["written"] == 1)
    journal.close()

    saved = json.loads(path.read_text())
    assert saved["sample_id"] == 1
    assert saved["session_id"] == "test-session"
    assert saved["gpu"]["load_percent"] == 2.0
    assert journal.stats()["dropped"] == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_rotation_retains_only_configured_backups(tmp_path: Path) -> None:
    """Verify rotation retains only configured backups using isolated inputs; assertion failure rejects the regression."""
    path = tmp_path / "telemetry.jsonl"
    journal = TelemetryJournal(
        path,
        idle_interval_seconds=0.01,
        active_interval_seconds=0.01,
        max_bytes=2048,
        backup_count=2,
        session_id="rotation",
    )
    journal.start(lambda: None)
    for index in range(8):
        item = record(gpu_load=float(index))
        item["padding"] = "x" * 400
        journal.offer(item)
        expected = index + 1
        wait_until(lambda: journal.stats()["written"] >= expected)
    journal.close()

    assert path.exists()
    assert path.with_name("telemetry.jsonl.1").exists()
    assert path.with_name("telemetry.jsonl.2").exists()
    assert not path.with_name("telemetry.jsonl.3").exists()
    assert all(p.stat().st_size <= 2048 for p in path.parent.glob("telemetry.jsonl*"))


def test_oversized_write_never_exceeds_file_budget_or_changes_parent_mode(tmp_path):
    """Disable only the optional journal on an oversized sample, preserving a shared data directory's mode."""
    parent = tmp_path / "shared-data"
    parent.mkdir(mode=0o750)
    before = stat.S_IMODE(parent.stat().st_mode)
    path = parent / "telemetry.jsonl"
    journal = TelemetryJournal(
        path,
        idle_interval_seconds=30,
        active_interval_seconds=1,
        max_bytes=64,
        backup_count=1,
        session_id="bounded",
    )
    journal.offer(record())
    journal._write_latest()
    assert journal.stats()["disabled"] and journal.stats()["errors"] == 1
    assert not path.exists() and stat.S_IMODE(parent.stat().st_mode) == before
    working = TelemetryJournal(
        path,
        idle_interval_seconds=30,
        active_interval_seconds=1,
        max_bytes=8192,
        backup_count=1,
        session_id="bounded",
    )
    working.offer(record())
    working._write_latest()
    assert working.stats()["written"] == 1 and path.stat().st_size <= 8192
    assert stat.S_IMODE(parent.stat().st_mode) == before


def test_history_skips_oversized_and_unhashable_samples(tmp_path):
    """Return valid history around corrupt lines without unbounded line retention or a reader exception."""
    path = tmp_path / "history.jsonl"
    good = {
        "schema_version": 1,
        "observed_at": snapshot().observed_at.isoformat(),
        "session_id": "history",
        "sample_id": 1,
    }
    bad = dict(good, sample_id=[])
    path.write_text(
        json.dumps(good) + "\n" + "x" * (MAX_RECORD_BYTES * 3) + "\n" + json.dumps(bad) + "\n"
    )
    journal = TelemetryJournal(
        path,
        idle_interval_seconds=30,
        active_interval_seconds=1,
        max_bytes=1024 * 1024,
        backup_count=1,
        session_id="history",
    )
    result = journal.read_history(since=None, before=None, limit=2)
    assert result.records == (good,) and result.skipped_records == 2
    with pytest.raises(ValueError):
        journal.read_history(since=None, before=None, limit=True)


def test_history_orders_selected_records_across_clock_rollback(tmp_path):
    """Sort the selected append-recency window by actual observation time, without renewing or changing sample identities."""
    path = tmp_path / "history.jsonl"
    later = {"schema_version": 1, "observed_at": "2026-10-26T02:00:00+00:00", "sample_id": 1}
    earlier = {"schema_version": 1, "observed_at": "2026-10-26T01:00:00+00:00", "sample_id": 2}
    path.write_text(json.dumps(later) + "\n" + json.dumps(earlier) + "\n")
    journal = TelemetryJournal(
        path,
        idle_interval_seconds=30,
        active_interval_seconds=1,
        max_bytes=8192,
        backup_count=1,
        session_id="clock",
    )
    assert journal.read_history(since=None, before=None, limit=2).records == (earlier, later)


@pytest.mark.parametrize("backup_count", [0, 17, True])
def test_journal_rejects_unbounded_rotation_configuration(tmp_path, backup_count):
    """Reject invalid retention counts before any journal directory or thread is created."""
    with pytest.raises(ValueError):
        TelemetryJournal(
            tmp_path / "new" / "telemetry.jsonl",
            idle_interval_seconds=30,
            active_interval_seconds=1,
            max_bytes=1024,
            backup_count=backup_count,
            session_id="bounds",
        )
    assert not (tmp_path / "new").exists()


def test_foreground_offers_do_not_bypass_configured_cadence(tmp_path: Path) -> None:
    """Verify foreground offers do not bypass configured cadence using isolated inputs; assertion failure rejects the regression."""
    path = tmp_path / "telemetry.jsonl"
    journal = TelemetryJournal(
        path,
        idle_interval_seconds=0.5,
        active_interval_seconds=0.5,
        max_bytes=1024 * 1024,
        backup_count=1,
        session_id="cadence",
    )
    journal.start(lambda: None)
    time.sleep(0.05)

    for load in range(10):
        journal.offer(record(gpu_load=float(load)))
    time.sleep(0.1)

    assert journal.stats()["written"] == 0
    assert journal.stats()["dropped"] >= 9
    journal.close()
    assert journal.stats()["written"] == 1


def test_write_failure_disables_only_the_journal(tmp_path: Path) -> None:
    """Verify write failure disables only the journal using isolated inputs; assertion failure rejects the regression."""
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied")
    journal = TelemetryJournal(
        parent_file / "telemetry.jsonl",
        idle_interval_seconds=60,
        active_interval_seconds=60,
        max_bytes=1024,
        backup_count=1,
        session_id="failure",
    )
    journal.offer(record())
    journal.start(lambda: None)
    wait_until(lambda: journal.stats()["disabled"] is True)
    journal.close()

    assert journal.stats()["errors"] == 1
    journal.offer(record(gpu_load=50.0))
    assert journal.stats()["offered"] == 1


def test_history_reads_rotations_newest_first_and_returns_chronological(tmp_path: Path) -> None:
    """Verify history reads rotations newest first and returns chronological using isolated inputs; assertion failure rejects the regression."""
    path = tmp_path / "telemetry.jsonl"
    journal = TelemetryJournal(
        path,
        idle_interval_seconds=30,
        active_interval_seconds=1,
        max_bytes=1024,
        backup_count=2,
        session_id="history",
    )
    start = datetime(2026, 10, 16, tzinfo=UTC)

    def saved(sample_id: int) -> dict[str, object]:
        """Build one minimal historical record with controlled identity/time for rotation ordering tests."""
        item = record(gpu_load=float(sample_id))
        item.update(
            {
                "observed_at": (start + timedelta(seconds=sample_id)).isoformat(),
                "sample_id": sample_id,
                "session_id": "history",
            }
        )
        return item

    path.with_name("telemetry.jsonl.1").write_text(
        "\n".join(json.dumps(saved(index)) for index in (1, 2)) + "\n"
    )
    path.write_text("{malformed}\n" + json.dumps(saved(3)) + "\n" + json.dumps(saved(4)))

    result = journal.read_history(since=start, before=None, limit=2)

    assert [item["sample_id"] for item in result.records] == [3, 4]
    assert result.truncated is True
    assert result.skipped_records == 1
    assert result.read_errors == 0


def test_history_validates_bounds_and_can_return_a_time_window(tmp_path: Path) -> None:
    """Verify history validates bounds and can return a time window using isolated inputs; assertion failure rejects the regression."""
    path = tmp_path / "telemetry.jsonl"
    journal = TelemetryJournal(
        path,
        idle_interval_seconds=30,
        active_interval_seconds=1,
        max_bytes=1024,
        backup_count=1,
        session_id="window",
    )
    start = datetime(2026, 10, 16, tzinfo=UTC)
    items = []
    for sample_id in range(1, 4):
        item = record()
        item.update(
            {
                "observed_at": (start + timedelta(seconds=sample_id)).isoformat(),
                "sample_id": sample_id,
                "session_id": "window",
            }
        )
        items.append(item)
    path.write_text("\n".join(json.dumps(item) for item in items) + "\n")

    result = journal.read_history(
        since=start + timedelta(seconds=2),
        before=start + timedelta(seconds=4),
        limit=10,
    )

    assert [item["sample_id"] for item in result.records] == [2, 3]
    assert result.truncated is False

    try:
        journal.read_history(since=start, before=start, limit=10)
    except ValueError as error:
        assert str(error) == "since must be earlier than before"
    else:
        raise AssertionError("invalid history bounds were accepted")
