"""Non-root image smoke test on a disposable CI runner, without jtop/GPU."""
import json
import subprocess
import time
import urllib.error
import urllib.request

IMAGE = "jetson-resource-router:ci"
NAME = "resource-router-ci"

subprocess.run([
    "docker", "run", "--rm", "--user", "11001:11002", IMAGE,
    "python", "-c",
    "import app.api.application; from importlib.metadata import version; "
    "assert version('jetson-stats') == '7.2.2'",
], check=True)

try:
    subprocess.run([
        "docker", "run", "--detach", "--name", NAME, "--user", "11001:11002",
        "--cpus", "1", "--memory", "512m",
        "--tmpfs", "/app/data:uid=11001,gid=11002,mode=0770",
        "--publish", "127.0.0.1:19081:19081", IMAGE,
    ], check=True)
    base = "http://127.0.0.1:19081"
    for _ in range(30):
        try:
            with urllib.request.urlopen(base + "/health/live", timeout=2) as response:
                assert response.status == 200
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1)
    else:
        raise AssertionError("Non-root application did not become live")

    try:
        urllib.request.urlopen(base + "/health/ready", timeout=2)
    except urllib.error.HTTPError as error:
        assert error.code == 503
        body = json.load(error)
        assert body["status"] == "not_ready"
        assert body["reason"] == "TELEMETRY_UNAVAILABLE", body
    else:
        raise AssertionError("Image without jtop must fail readiness closed")
    print("Non-root image imports, package version, liveness and fail-closed readiness passed")
finally:
    subprocess.run(["docker", "logs", NAME], check=False)
    subprocess.run(["docker", "rm", "--force", NAME], check=False)
