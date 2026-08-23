from pathlib import Path

import pytest

from app.settings import Settings


def test_default_database_path_is_project_local_data(
    monkeypatch,
) -> None:
    monkeypatch.delenv("JETROUTER_DATABASE_PATH", raising=False)
    monkeypatch.delenv("JETROUTER_RECLAIMABLE_CACHE_FRACTION", raising=False)

    settings = Settings.from_environment()

    assert settings.database_path == Path("data") / "jetrouter.sqlite3"
    assert settings.reclaimable_cache_fraction == 0.5


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
