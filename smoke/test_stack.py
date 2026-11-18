"""Smoke checks for scripts and the running prod stack. Host-side, serial, read-only:
they never set up, unlock or change the gateway. Run: ./podman-smoke.sh (or docker-smoke.sh)."""
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke
ROOT = Path(__file__).resolve().parent.parent
ENGINE = os.environ.get("SMOKE_ENGINE", "podman")
SCRIPTS = [f"{e}-{a}-{m}.sh" for e in ("podman", "docker") for a in ("start", "stop") for m in ("dev", "prod")]


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=ROOT)


def env() -> dict:
    out = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def running(name: str) -> bool:
    return sh(ENGINE, "inspect", name).returncode == 0


def inspect(name: str) -> dict:
    return json.loads(sh(ENGINE, "inspect", name).stdout)[0]


def get(path: str, host: str = "127.0.0.1", body: dict | None = None):
    port = env().get("GATEWAY_PORT", "38080")
    headers = {"Host": host} | ({"Content-Type": "application/json"} if body is not None else {})
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.mark.parametrize("script", SCRIPTS + ["unlock.sh", "podman-test.sh", "docker-test.sh"])
def test_scripts_exist_executable_and_parse(script):
    p = ROOT / script
    assert p.exists() and os.access(p, os.X_OK)
    assert sh("sh", "-n", str(p)).returncode == 0


def test_only_prod_scripts_detach():
    for s in SCRIPTS:
        text = (ROOT / s).read_text()
        if "start-prod" in s:
            assert "up -d" in text
        else:
            assert "up -d" not in text


def test_images_pinned_by_digest():
    for f in ["Containerfile", "Containerfile.dev", "compose.yaml", "compose.docker.yaml", "compose.dev.yaml",
              "compose.dev.docker.yaml"]:
        for line in (ROOT / f).read_text().splitlines():
            s = line.strip()
            if (s.startswith("FROM ") and "/" in s.split()[1]) or s.startswith("image: docker.io"):
                assert "docker.io/library/" in s and "@sha256:" in s, f"{f}: {s}"


@pytest.fixture(scope="module")
def prod():
    if not (running("gateway") and running("gateway-caddy")):
        pytest.skip(f"prod stack not running ({ENGINE}-start-prod.sh)")
    return env()


def test_prod_hardening(prod):
    for name in ("gateway", "gateway-caddy"):
        hc = inspect(name)["HostConfig"]
        assert hc["ReadonlyRootfs"] is True, name
        assert any("no-new-privileges" in o for o in hc.get("SecurityOpt") or []), name
        assert inspect(name)["Config"]["User"].startswith("10001"), name
        if ENGINE == "docker":
            assert "ALL" in (hc.get("CapDrop") or []), name


def test_backend_not_published(prod):
    assert not inspect("gateway")["HostConfig"].get("PortBindings")
    bindings = inspect("gateway-caddy")["HostConfig"]["PortBindings"]
    assert all(b["HostIp"] == "127.0.0.1" for v in bindings.values() for b in v)


def test_caddy_routes_only_prefixes(prod):
    a, p = prod["ADMIN_PREFIX"], prod["API_PREFIX"]
    for path in ("/", "/health", "/_unlock", "/admin/api/state", "/api/v1/me", f"/{a}x/api/state", "/.env"):
        status, body = get(path)
        assert status == 404 and body == b"", path
    assert get(f"/{a}/api/state")[0] == 200
    assert get(f"/{p}/v1/me")[0] in (401, 503)


def test_wrong_host_404(prod):
    status, body = get(f"/{prod['ADMIN_PREFIX']}/api/state", host="evil.example")
    assert status == 404 and body == b""


def test_prod_image_has_no_pytest_or_node(prod):
    assert sh(ENGINE, "exec", "gateway", "python", "-c", "import pytest").returncode != 0
    assert sh(ENGINE, "exec", "gateway", "sh", "-c", "command -v node").returncode != 0


def test_unlock_token_private(prod):
    r = sh(ENGINE, "exec", "gateway", "stat", "-c", "%a %U", "/run/gateway/unlock.token")
    assert r.stdout.split()[0] == "600"


def test_web_setup_needs_setup_code(prod):
    """Never sets up: a setup without the code is refused (403), or 409 when already set up."""
    status, body = get(f"/{prod['ADMIN_PREFIX']}/api/setup",
                       body={"passphrase": "smoke-test-passphrase", "admin_password": "smoke-test-password"})
    state = json.loads(get(f"/{prod['ADMIN_PREFIX']}/api/state")[1])["state"]
    code_file = sh(ENGINE, "exec", "gateway", "stat", "-c", "%a", "/run/gateway/setup.code")
    if state == "uninitialized":
        assert status == 403 and json.loads(body) == {"error": "wrong_setup_code"}
        assert code_file.stdout.strip() == "600"
    else:
        assert status == 409 and code_file.returncode != 0  # code file gone once set up
