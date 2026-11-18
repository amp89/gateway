import pytest
from starlette.requests import Request

from tests.conftest import do_setup


@pytest.fixture
def split(make_client):
    c = make_client(api_hosts=("api.example",), admin_hosts=("admin.example",),
                    api_prefix="k3x9api", admin_prefix="q7zadm")
    c.headers["host"] = "admin.example"
    do_setup(c, "/q7zadm/api")
    return c


def get(c, path, host):
    return c.get(path, headers={"host": host})


@pytest.mark.parametrize("path", ["/", "/api/v1/me", "/admin/api/state", "/docs", "/openapi.json",
                                  "/k3x9apiX/v1/me", "/q7zadm.bak", "/.env"])
def test_wrong_prefix_bare_404(split, path):
    for host in ("api.example", "admin.example"):
        r = get(split, path, host)
        assert r.status_code == 404 and r.content == b""


def test_admin_unreachable_on_api_host(split):
    r = get(split, "/q7zadm/api/state", "api.example")
    assert r.status_code == 404 and r.content == b""
    r = split.post("/q7zadm/api/unlock", json={"passphrase": "x"}, headers={"host": "api.example"})
    assert r.status_code == 404 and r.content == b""
    assert get(split, "/q7zadm/api/state", "admin.example").status_code == 200


def test_api_unreachable_on_admin_host_and_unknown_host(split):
    for host in ("admin.example", "evil.example", "api.example.evil.example"):
        r = get(split, "/k3x9api/v1/me", host)
        assert r.status_code == 404 and r.content == b""


def test_host_port_and_case_ignored(split):
    assert get(split, "/q7zadm/api/state", "ADMIN.example:443").status_code == 200


def test_unknown_route_under_prefix_bare_404(split):
    r = get(split, "/k3x9api/v1/nothing-here", "api.example")
    assert r.status_code == 404 and r.content == b""
    r = split.delete("/q7zadm/api/state", headers={"host": "admin.example"})  # 405 -> 404
    assert r.status_code == 404 and r.content == b""


def test_health_not_host_guarded(split):
    assert get(split, "/health", "anything").status_code == 200


def _req(peer, headers):
    return Request({"type": "http", "client": (peer, 1234), "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


def test_client_ip_trusted_only_from_proxy(make_app):
    gw = make_app(trusted_proxy_ips=("10.89.47.0/24",)).state.gw
    hdr = {"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "198.51.100.1, 10.0.0.1"}
    assert gw.client_ip(_req("10.89.47.3", hdr)) == "203.0.113.9"
    assert gw.client_ip(_req("10.89.47.3", {"X-Forwarded-For": "198.51.100.1, 10.0.0.1"})) == "198.51.100.1"
    assert gw.client_ip(_req("192.0.2.50", hdr)) == "192.0.2.50"
