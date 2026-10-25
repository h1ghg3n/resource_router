"""Validate Compose without connecting to a Docker daemon or host jtop."""
import json
import os
import subprocess

env = dict(os.environ, JETROUTER_UID="11001", JTOP_GID="11002")
for required in ("JETROUTER_UID", "JTOP_GID"):
    missing = dict(env, **{required: ""})
    result = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        env=missing, capture_output=True, text=True,
    )
    assert result.returncode != 0 and required in result.stderr, required

result = subprocess.run(
    ["docker", "compose", "config", "--format", "json"],
    env=env, check=True, capture_output=True, text=True,
)
service = json.loads(result.stdout)["services"]["resource-router"]
assert service["user"] == "11001:11002"
assert service["build"]["args"]["JETSON_STATS_VERSION"] == "7.2.2"
assert not service.get("privileged", False)
assert not service.get("cap_add")
assert not service.get("group_add")
mounts = {mount["target"]: mount for mount in service["volumes"]}
assert set(mounts) == {"/run/jtop.sock", "/app/data"}
assert mounts["/run/jtop.sock"]["source"] == "/run/jtop.sock"
assert mounts["/run/jtop.sock"]["read_only"] is True
assert not mounts["/app/data"].get("read_only", False)
assert service["ports"][0]["host_ip"] == "127.0.0.1"
print("Compose identity, mounts, and loopback contract passed")

proxy_env = dict(env, JETROUTER_PROXY_SOCKET="/run/user/11001/jetson-jtop-proxy.sock")
for files, source in (
    (["compose.yml", "compose.jtop-proxy.yml"], proxy_env["JETROUTER_PROXY_SOCKET"]),
    (["compose.yml", "compose.jtop-proxy.yml", "compose.jtop-direct-rollback.yml"], "/run/jtop.sock"),
):
    command = ["docker", "compose"]
    for filename in files:
        command.extend(["-f", filename])
    result = subprocess.run(
        command + ["config", "--format", "json"], env=proxy_env,
        check=True, capture_output=True, text=True,
    )
    overlay = json.loads(result.stdout)["services"]["resource-router"]
    mounts = {mount["target"]: mount for mount in overlay["volumes"]}
    assert mounts["/run/jtop.sock"]["source"] == source
    assert mounts["/run/jtop.sock"]["read_only"] is True
    # Some Compose releases omit explicitly false values in normalized JSON.
    assert mounts["/run/jtop.sock"].get("bind", {}).get("create_host_path", False) is False
    assert mounts["/app/data"] == {
        mount["target"]: mount for mount in service["volumes"]
    }["/app/data"]
    assert overlay["user"] == service["user"]
    assert overlay["ports"] == service["ports"]
print("Optional proxy/direct rollback preserve data, identity, and loopback mounts")
