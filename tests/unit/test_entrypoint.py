import pytest

from app.__main__ import DEFAULT_PORT, _read_port


def test_default_port_is_19081() -> None:
    assert _read_port(None) == DEFAULT_PORT == 19081


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_invalid_configured_port_is_rejected(value: str) -> None:
    with pytest.raises(SystemExit):
        _read_port(value)
