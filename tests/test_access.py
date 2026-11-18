import sqlite3

import pytest
from fastapi import Request

from app.access import Action, run_action
from tests.conftest import ADMIN, bearer, new_key


class EchoModule:
    """Test-only action: test.echo, scoped by `thing`."""

    @staticmethod
    def setup(gw, client, admin):
        gw.access.registry.register(Action("test.echo", ("thing",), "echo"))

        @client.post("/test/echo")
        async def echo(request: Request, body: dict):
            async def handler(caller):
                return {"echo": body, "key": caller.name}

            return await run_action(gw, request, "test.echo", {"thing": body.get("thing")}, handler)


@pytest.fixture
def extra_modules():
    return [EchoModule]


def audit(settings):
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM audit WHERE action = 'test.echo' ORDER BY id")]


def test_key_shown_once_and_hashed(admin, settings):
    r = admin.post(f"{ADMIN}/keys", json={"name": "phone"})
    secret = r.json()["key"]
    assert secret.startswith("gw_") and len(secret) >= 40
    listed = admin.get(f"{ADMIN}/keys").json()["keys"][0]
    assert "key" not in listed and listed["key_hint"] == secret[-4:]
    db = open(settings.database_path, "rb").read()
    assert secret.encode() not in db


def test_duplicate_key_name(admin):
    new_key(admin, "dup")
    assert admin.post(f"{ADMIN}/keys", json={"name": "dup"}).status_code == 409


def test_key_with_no_rows_can_do_nothing(admin):
    k = new_key(admin)
    r = admin.post("/api/v1/test/echo", json={"thing": "a"}, headers=bearer(k))
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert admin.get("/api/v1/me", headers=bearer(k)).json() == {"key": "client", "actions": []}


def test_granted_unscoped(admin):
    k = new_key(admin, grants={"test.echo": None})
    r = admin.post("/api/v1/test/echo", json={"thing": "anything"}, headers=bearer(k))
    assert r.status_code == 200 and r.json()["key"] == "client"
    assert admin.get("/api/v1/me", headers=bearer(k)).json()["actions"] == ["test.echo"]


def test_scope_enforced(admin):
    k = new_key(admin, grants={"test.echo": {"thing": ["a", 7]}})
    h = bearer(k)
    assert admin.post("/api/v1/test/echo", json={"thing": "a"}, headers=h).status_code == 200
    assert admin.post("/api/v1/test/echo", json={"thing": 7}, headers=h).status_code == 200
    assert admin.post("/api/v1/test/echo", json={"thing": "b"}, headers=h).status_code == 403
    assert admin.post("/api/v1/test/echo", json={}, headers=h).status_code == 403


@pytest.mark.parametrize("header", [None, "Bearer nope", "Basic gw_x", "Bearer gw_unknown"])
def test_bad_credentials_401(admin, header):
    h = {"Authorization": header} if header else {}
    r = admin.post("/api/v1/test/echo", json={"thing": "a"}, headers=h)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


def test_disabled_and_revoked_keys_401(admin):
    k = new_key(admin, grants={"test.echo": None})
    key_id = admin.get(f"{ADMIN}/keys").json()["keys"][0]["id"]
    admin.patch(f"{ADMIN}/keys/{key_id}", json={"status": "disabled"})
    assert admin.post("/api/v1/test/echo", json={"thing": 1}, headers=bearer(k)).status_code == 401
    admin.patch(f"{ADMIN}/keys/{key_id}", json={"status": "active"})
    assert admin.post("/api/v1/test/echo", json={"thing": 1}, headers=bearer(k)).status_code == 200
    admin.delete(f"{ADMIN}/keys/{key_id}")
    assert admin.post("/api/v1/test/echo", json={"thing": 1}, headers=bearer(k)).status_code == 401
    assert admin.patch(f"{ADMIN}/keys/{key_id}", json={"status": "active"}).status_code == 409


def test_every_call_audited(admin, settings):
    k = new_key(admin, grants={"test.echo": {"thing": ["a"]}})
    admin.post("/api/v1/test/echo", json={"thing": "a"}, headers=bearer(k))
    admin.post("/api/v1/test/echo", json={"thing": "b"}, headers=bearer(k))
    admin.post("/api/v1/test/echo", json={"thing": "a"}, headers=bearer("gw_bogus"))
    rows = admin.get(f"{ADMIN}/audit", params={"action": "test.echo"}).json()["rows"][::-1]
    assert [(r["key_name"], r["status"], r["resource"]) for r in rows] == [
        ("client", "200", "thing=a"), ("client", "403", "thing=b"), ("-", "401", "thing=a")]
    assert all(k not in str(r) for r in rows)
    raw = audit(settings)  # on disk: resource encrypted, plain column empty
    assert all(r["resource"] == "" and r["resource_enc"] and "thing" not in r["resource_enc"] for r in raw)


def test_last_used_updated(admin, clock):
    k = new_key(admin, grants={"test.echo": None})
    assert admin.get(f"{ADMIN}/keys").json()["keys"][0]["last_used_at"] is None
    admin.post("/api/v1/test/echo", json={"thing": 1}, headers=bearer(k))
    assert admin.get(f"{ADMIN}/keys").json()["keys"][0]["last_used_at"] == clock.now()


def test_rate_limit(make_client, clock):
    from tests.conftest import do_login, do_setup
    c = make_client(api_rate_per_min=3)
    do_setup(c)
    do_login(c)
    k = new_key(c, grants={"test.echo": None})
    codes = [c.post("/api/v1/test/echo", json={"thing": 1}, headers=bearer(k)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    clock.advance(20)
    assert c.post("/api/v1/test/echo", json={"thing": 1}, headers=bearer(k)).status_code == 200


def test_permission_validation(admin):
    new_key(admin)
    key_id = admin.get(f"{ADMIN}/keys").json()["keys"][0]["id"]
    assert admin.put(f"{ADMIN}/permissions/{key_id}/nope.action", json={"scope": None}).status_code == 400
    assert admin.put(f"{ADMIN}/permissions/{key_id}/test.echo", json={"scope": {"other": ["x"]}}).status_code == 400
    assert admin.put(f"{ADMIN}/permissions/{key_id}/test.echo", json={"scope": {"thing": "x"}}).status_code == 400
    assert admin.put(f"{ADMIN}/permissions/999/test.echo", json={"scope": None}).status_code == 404


def test_permission_matrix_and_delete(admin):
    k = new_key(admin, grants={"test.echo": {"thing": ["a"]}})
    m = admin.get(f"{ADMIN}/permissions").json()
    assert "test.echo" in m["actions"] and m["grants"][0]["scope"] == {"thing": ["a"]}
    key_id = m["keys"][0]["id"]
    admin.delete(f"{ADMIN}/permissions/{key_id}/test.echo")
    assert admin.post("/api/v1/test/echo", json={"thing": "a"}, headers=bearer(k)).status_code == 403


def test_actions_listing(admin):
    acts = {a["name"]: a for a in admin.get(f"{ADMIN}/actions").json()["actions"]}
    assert acts["test.echo"]["scope_fields"] == ["thing"]


def test_audit_filter_and_paginate(admin):
    k = new_key(admin, grants={"test.echo": None})
    for i in range(5):
        admin.post("/api/v1/test/echo", json={"thing": i}, headers=bearer(k))
    page = admin.get(f"{ADMIN}/audit", params={"action": "test.echo", "limit": 3}).json()
    assert len(page["rows"]) == 3 and page["next_before_id"]
    rest = admin.get(f"{ADMIN}/audit", params={"action": "test.echo", "limit": 3,
                                                 "before_id": page["next_before_id"]}).json()
    assert len(rest["rows"]) == 2 and rest["next_before_id"] is None


def test_admin_endpoints_need_session(unsealed):
    for m, p in (("get", "/keys"), ("post", "/keys"), ("get", "/permissions"), ("get", "/audit"), ("get", "/actions")):
        assert getattr(unsealed, m)(f"{ADMIN}{p}").status_code == 401
