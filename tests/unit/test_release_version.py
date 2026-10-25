"""Keep the package release and advertised application version consistent, without host I/O."""

import re
from datetime import datetime, timezone
from pathlib import Path

from app import __version__
from app.api.application import create_app
from app.telemetry.provider import StaticTelemetryProvider
from tests.api.test_api_v1 import MutableClock, manager_for, snapshot


def test_release_version_matches_package_and_openapi():
    """Verify source metadata and OpenAPI report the same 0.2.0 release while preserving the v1 route."""
    source = (Path(__file__).parents[2] / "pyproject.toml").read_text()
    package_version = re.search(r'^version = "([^"]+)"', source, re.MULTILINE).group(1)
    now = datetime.now(timezone.utc)
    manager = manager_for(MutableClock(now), StaticTelemetryProvider(snapshot(now)))
    try:
        schema = create_app(manager).openapi()
        assert schema["info"]["version"] == package_version == __version__ == "0.2.0"
        assert "/v1/leases" in schema["paths"] and "/v2/telemetry/current" in schema["paths"]
        dockerfile = (Path(__file__).parents[2] / "Dockerfile").read_text()
        assert f"ARG VERSION={package_version}" in dockerfile
        assert 'org.opencontainers.image.version="${VERSION}"' in dockerfile
    finally:
        manager.close()
