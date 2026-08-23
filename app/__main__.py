from __future__ import annotations

import os

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19081


def main() -> None:
    host = os.environ.get("JETROUTER_HOST", DEFAULT_HOST)
    port = _read_port(os.environ.get("JETROUTER_PORT"))
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        workers=1,
    )


def _read_port(raw_port: str | None) -> int:
    if raw_port is None:
        return DEFAULT_PORT
    try:
        port = int(raw_port)
    except ValueError as error:
        raise SystemExit("JETROUTER_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise SystemExit("JETROUTER_PORT must be between 1 and 65535")
    return port


if __name__ == "__main__":
    main()
