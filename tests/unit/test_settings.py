from pathlib import Path

import pytest

from app.settings import Settings


def test_default_database_path_is_project_local_data(
    monkeypatch,
) -> None:
    """Verify default database path is project local data using isolated inputs; assertion failure rejects the regression."""
    monkeypatch.delenv("JETROUTER_DATABASE_PATH", raising=False)
    monkeypatch.delenv("JETROUTER_RECLAIMABLE_CACHE_FRACTION", raising=False)
    monkeypatch.delenv("JETROUTER_TELEMETRY_JOURNAL_PATH", raising=False)
    monkeypatch.delenv("JETROUTER_TELEMETRY_IDLE_SECONDS", raising=False)
    monkeypatch.delenv("JETROUTER_TELEMETRY_ACTIVE_SECONDS", raising=False)
    monkeypatch.delenv("JETROUTER_TELEMETRY_MAX_BYTES", raising=False)
    monkeypatch.delenv("JETROUTER_TELEMETRY_BACKUP_COUNT", raising=False)
    monkeypatch.delenv("JETROUTER_TELEMETRY_SESSION_ID", raising=False)

    settings = Settings.from_environment()

    assert settings.database_path == Path("data") / "jetrouter.sqlite3"
    assert settings.reclaimable_cache_fraction == 0.5
    assert settings.telemetry_journal_path is None
    assert settings.telemetry_idle_interval_seconds == 30.0
    assert settings.telemetry_active_interval_seconds == 1.0
    assert settings.telemetry_max_bytes == 16 * 1024 * 1024
    assert settings.telemetry_backup_count == 4
    assert settings.telemetry_session_id == "resource-router"


def test_database_path_can_be_overridden(monkeypatch, tmp_path: Path) -> None:
    configured = tmp_path / "state" / "router.sqlite3"
    monkeypatch.setenv("JETROUTER_DATABASE_PATH", str(configured))

    settings = Settings.from_environment()

    assert settings.database_path == configured


def test_reclaimable_cache_fraction_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("JETROUTER_RECLAIMABLE_CACHE_FRACTION", "0.85")

    settings = Settings.from_environment()

    assert settings.reclaimable_cache_fraction == 0.85


@pytest.mark.parametrize(
    "configured",
    ["", "-0.1", "1.1", "nan", "inf", "not-a-number"],
)
def test_reclaimable_cache_fraction_rejects_invalid_values(
    monkeypatch,
    configured: str,
) -> None:
    monkeypatch.setenv("JETROUTER_RECLAIMABLE_CACHE_FRACTION", configured)

    with pytest.raises(
        ValueError,
        match="JETROUTER_RECLAIMABLE_CACHE_FRACTION",
    ):
        Settings.from_environment()


def test_telemetry_journal_settings_can_be_overridden(monkeypatch, tmp_path: Path) -> None:
    """Verify telemetry journal settings can be overridden using isolated inputs; assertion failure rejects the regression."""
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("JETROUTER_TELEMETRY_JOURNAL_PATH", str(path))
    monkeypatch.setenv("JETROUTER_TELEMETRY_IDLE_SECONDS", "7.5")
    monkeypatch.setenv("JETROUTER_TELEMETRY_ACTIVE_SECONDS", "0.5")
    monkeypatch.setenv("JETROUTER_TELEMETRY_MAX_BYTES", "4096")
    monkeypatch.setenv("JETROUTER_TELEMETRY_BACKUP_COUNT", "2")
    monkeypatch.setenv("JETROUTER_TELEMETRY_SESSION_ID", "candidate-20261016")

    settings = Settings.from_environment()

    assert settings.telemetry_journal_path == path
    assert settings.telemetry_idle_interval_seconds == 7.5
    assert settings.telemetry_active_interval_seconds == 0.5
    assert settings.telemetry_max_bytes == 4096
    assert settings.telemetry_backup_count == 2
    assert settings.telemetry_session_id == "candidate-20261016"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("JETROUTER_TELEMETRY_IDLE_SECONDS", "0"),
        ("JETROUTER_TELEMETRY_ACTIVE_SECONDS", "nan"),
        ("JETROUTER_TELEMETRY_MAX_BYTES", "-1"),
        ("JETROUTER_TELEMETRY_BACKUP_COUNT", "0"),
        ("JETROUTER_TELEMETRY_BACKUP_COUNT", "17"),
        ("JETROUTER_TELEMETRY_SESSION_ID", "contains secret spaces"),
    ],
)
def test_invalid_telemetry_settings_are_rejected(monkeypatch, name: str, value: str) -> None:
    """Verify invalid telemetry settings are rejected using isolated inputs; assertion failure rejects the regression."""
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings.from_environment()
