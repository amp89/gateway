import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN, bearer, do_login, do_setup, new_key

PACK = json.loads((Path(__file__).parent.parent / "packs" / "todoist.json").read_text())
TOKEN = "todoist-secret-0123456789abcdef"
BASE = "https://api.todoist.com/api/v1"


@pytest.fixture
def todo(admin):
    r = admin.post(f"{ADMIN}/services/import", json=PACK)
    assert r.status_code == 200, r.text
    admin.svc_id = r.json()["id"]
    assert admin.put(f"{ADMIN}/services/{admin.svc_id}/credential", json={"credential": TOKEN}).status_code == 200
    return admin


def call(c, key, op, svc="todoist", **body):
    return c.post(f"/api/v1/call/{svc}/{op}", json=body, headers=bearer(key))


def test_pack_operations_become_actions(todo):
    names = {a["name"] for a in todo.get(f"{ADMIN}/actions").json()["actions"]}
    assert {"todoist.projects_list", "todoist.tasks_list", "todoist.tasks_add", "todoist.task_close"} <= names
    acts = {a["name"]: a for a in todo.get(f"{ADMIN}/actions").json()["actions"]}
    assert acts["todoist.tasks_add"]["scope_fields"] == ["project_id"]


def test_each_todoist_operation_mocked(todo, upstream):
    assert [o["name"] for o in PACK["operations"]] == [
        "projects_list", "labels_list", "tasks_list", "tasks_filter", "task_get", "tasks_add", "quick_add",
        "task_close", "task_delete"]
    page = {"results": [{"id": "x1"}], "next_cursor": None}
    upstream.add("GET", f"{BASE}/projects", page)
    upstream.add("GET", f"{BASE}/labels", page)
    upstream.add("GET", f"{BASE}/tasks", page)
    upstream.add("GET", f"{BASE}/tasks/filter", page)  # later routes win over the /tasks prefix
    upstream.add("GET", f"{BASE}/tasks/t1", {"id": "t1", "content": "x"})
    upstream.add("POST", f"{BASE}/tasks", {"id": "t2", "content": "buy milk", "project_id": "p1"})
    upstream.add("POST", f"{BASE}/tasks/quick", {"id": "t3", "content": "call mum"})
    upstream.add("POST", f"{BASE}/tasks/t1/close", httpx.Response(204))
    upstream.add("DELETE", f"{BASE}/tasks/t1", httpx.Response(204))
    k = new_key(todo, grants={f"todoist.{o['name']}": None for o in PACK["operations"]})
    r = call(todo, k, "projects_list")
    assert r.status_code == 200 and r.json() == {"status": 200, "body": page}
    assert call(todo, k, "labels_list").json()["body"] == page
    assert call(todo, k, "tasks_list", query={"project_id": "p1"}).json()["body"] == page
    assert call(todo, k, "tasks_filter", query={"query": "today | overdue"}).json()["body"] == page
    assert call(todo, k, "task_get", path={"task_id": "t1"}).json()["body"]["id"] == "t1"
    r = call(todo, k, "tasks_add", body={"content": "buy milk", "project_id": "p1", "priority": 4})
    assert r.json()["body"]["id"] == "t2"
    assert call(todo, k, "quick_add", body={"text": "call mum tomorrow #Home"}).json()["body"]["id"] == "t3"
    r = call(todo, k, "task_close", path={"task_id": "t1"})
    assert r.status_code == 200 and r.json() == {"status": 204, "body": None}
    assert call(todo, k, "task_delete", path={"task_id": "t1"}).json() == {"status": 204, "body": None}
    reqs = upstream.requests
    assert [f"{q.method} {q.url}" for q in reqs] == [
        f"GET {BASE}/projects", f"GET {BASE}/labels", f"GET {BASE}/tasks?project_id=p1",
        f"GET {BASE}/tasks/filter?query=today+%7C+overdue", f"GET {BASE}/tasks/t1", f"POST {BASE}/tasks",
        f"POST {BASE}/tasks/quick", f"POST {BASE}/tasks/t1/close", f"DELETE {BASE}/tasks/t1"]
    assert all(q.headers["authorization"] == f"Bearer {TOKEN}" for q in reqs)
    assert json.loads(reqs[5].content) == {"content": "buy milk", "project_id": "p1", "priority": 4}
    assert json.loads(reqs[6].content) == {"text": "call mum tomorrow #Home"}


PACKS_DIR = str(Path(__file__).parent.parent / "packs")


def test_setup_imports_bundled_packs_without_credential(make_client, upstream):
    c = make_client(packs_dir=PACKS_DIR)
    do_setup(c)
    do_login(c)
    svcs = {s["name"]: s for s in c.get(f"{ADMIN}/services").json()["services"]}
    assert {n: s["credential_set"] for n, s in svcs.items()} == {"todoist": False, "kanbanflow": False}  # vikunja: self-hosted
    ops = c.get(f"{ADMIN}/services/{svcs['todoist']['id']}/operations").json()["operations"]
    assert len(ops) == 9
    assert c.get(f"{ADMIN}/keys").json()["keys"] == []  # no demo key outside dev auto-init
    k = new_key(c, grants={"todoist.projects_list": None})
    r = call(c, k, "projects_list")
    assert r.status_code == 502 and upstream.requests == []


def test_bundled_pack_list_and_add_never_overwrites(make_client, tmp_path):
    d = tmp_path / "bundled"
    d.mkdir()
    (d / "todoist.json").write_text(json.dumps(PACK))
    (d / "broken.json").write_text("{not json")
    c = make_client()
    do_setup(c)
    do_login(c)
    c.app.state.gw.settings = c.app.state.gw.settings.with_(packs_dir=str(d))
    assert c.get(f"{ADMIN}/packs").json()["packs"] == [
        {"file": "todoist", "service": "todoist", "operations": 9, "self_hosted": False, "base_url": BASE,
         "installed_as": [], "suggested_name": "todoist"}]
    r = c.post(f"{ADMIN}/packs/todoist/import")
    assert r.status_code == 200 and r.json()["name"] == "todoist"
    sid = r.json()["id"]
    c.put(f"{ADMIN}/services/{sid}/credential", json={"credential": TOKEN})
    for params in ({}, {"replace": "true"}):  # replace is gone: never overwrites
        r = c.post(f"{ADMIN}/packs/todoist/import", params=params)
        assert r.status_code == 409 and "delete it first" in r.json()["detail"]
    svc = next(s for s in c.get(f"{ADMIN}/services").json()["services"] if s["id"] == sid)
    assert svc["credential_set"] is True
    assert c.get(f"{ADMIN}/packs").json()["packs"][0]["suggested_name"] == "todoist2"
    r = c.post(f"{ADMIN}/packs/todoist/import", json={"name": "todoist2"})
    assert r.status_code == 200 and r.json()["name"] == "todoist2" and r.json()["credential_set"] is False
    p = c.get(f"{ADMIN}/packs").json()["packs"][0]
    assert p["installed_as"] == ["todoist", "todoist2"] and p["suggested_name"] == "todoist3"
    r = c.post(f"{ADMIN}/packs/todoist/import", json={"name": "Bad Name"})
    assert r.status_code == 400 and "name" in r.json()["detail"]
    r = c.post(f"{ADMIN}/packs/todoist/import", json={"name": "t3", "base_url": "http://x.example"})
    assert r.status_code == 400 and "https://host" in r.json()["detail"] and "http://x.example" not in r.text
    assert c.post(f"{ADMIN}/packs/broken/import").status_code == 404
    assert c.post(f"{ADMIN}/packs/..%2Fetc/import").status_code == 404


def test_two_services_from_one_pack_keep_separate_grants(todo, upstream):
    r = todo.post(f"{ADMIN}/services/import", params={"name": "todoist2"}, json=PACK)
    assert r.status_code == 200 and r.json()["name"] == "todoist2"
    todo.put(f"{ADMIN}/services/{r.json()['id']}/credential", json={"credential": "second-token-0123456789"})
    upstream.add("GET", f"{BASE}/projects", {"results": []})
    k = new_key(todo, grants={"todoist2.projects_list": None})
    assert call(todo, k, "projects_list").status_code == 403
    assert call(todo, k, "projects_list", svc="todoist2").status_code == 200
    assert upstream.requests[-1].headers["authorization"] == "Bearer second-token-0123456789"
    assert todo.post(f"{ADMIN}/services/import", params={"name": "todoist2"}, json=PACK).status_code == 409


def test_dev_auto_init_creates_demo_key(make_app, upstream, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="gateway")
    with TestClient(make_app(packs_dir=PACKS_DIR, dev_auto_unlock_passphrase="dev passphrase 123",
                             dev_admin_password="dev-admin-pass")) as c:
        assert c.get(f"{ADMIN}/state").json()["state"] == "unsealed"
        line = next(r.getMessage() for r in caplog.records if "demo key" in r.getMessage())
        secret = line.split(" = ")[1].split(" ")[0]
        assert secret.startswith("gw_")
        do_login(c, "dev-admin-pass")
        sid = next(s["id"] for s in c.get(f"{ADMIN}/services").json()["services"] if s["name"] == "todoist")
        c.put(f"{ADMIN}/services/{sid}/credential", json={"credential": TOKEN})
        upstream.add("GET", f"{BASE}/tasks", {"results": [], "next_cursor": None})
        assert call(c, secret, "tasks_list").status_code == 200
        assert call(c, secret, "task_delete", path={"task_id": "t1"}).status_code == 403
        assert [k["name"] for k in c.get(f"{ADMIN}/keys").json()["keys"]] == ["dev-demo"]


def test_no_demo_key_in_production(make_app):
    from app.services.generic import create_demo_key
    with TestClient(make_app(packs_dir=PACKS_DIR, env="production")) as c:
        do_setup(c)
        gw = c.app.state.gw
        create_demo_key(gw)
        assert gw.conn.execute("SELECT count(*) FROM gateway_keys").fetchone()[0] == 0


def test_permissions_default_deny(todo, upstream):
    k = new_key(todo, grants={"todoist.projects_list": None})
    assert call(todo, k, "tasks_add", body={"content": "x"}).status_code == 403
    assert call(todo, k, "nope").status_code == 403
    assert call(todo, k, "projects_list", svc="ghost").status_code == 403
    assert upstream.requests == []


@pytest.mark.parametrize("bad", ["..", ".", "a/b", "a%2Fb", "", "x" * 200, "a b", "../../admin"])
def test_path_traversal_refused(todo, upstream, bad):
    k = new_key(todo, grants={"todoist.task_close": None})
    assert call(todo, k, "task_close", path={"task_id": bad}).status_code == 400
    assert upstream.requests == []


def test_undeclared_fields_and_params_refused(todo, upstream):
    k = new_key(todo, grants={"todoist.tasks_add": None, "todoist.tasks_list": None, "todoist.task_close": None})
    assert call(todo, k, "tasks_add", body={"content": "x", "evil": 1}).status_code == 400
    assert call(todo, k, "tasks_add", body=["not", "object"]).status_code == 400
    assert call(todo, k, "tasks_list", query={"filter": "all"}).status_code == 400
    assert call(todo, k, "tasks_list", body={"content": "x"}).status_code == 400  # GET takes no body
    assert call(todo, k, "task_close").status_code == 400  # missing path param
    assert call(todo, k, "task_close", path={"task_id": "1", "extra": "2"}).status_code == 400
    assert call(todo, k, "tasks_list", query={"project_id": {"nested": 1}}).status_code == 400
    assert upstream.requests == []


def test_undeclared_method_or_path_unreachable(todo, upstream):
    k = new_key(todo, grants={"todoist.tasks_list": None})
    for m in ("get", "put", "delete"):
        assert getattr(todo, m)("/api/v1/call/todoist/tasks_list", headers=bearer(k)).status_code == 404
    assert todo.post("/api/v1/call/todoist/tasks_list/extra", json={}, headers=bearer(k)).status_code == 404
    assert upstream.requests == []


def test_scope_on_query_and_body(todo, upstream):
    upstream.add("GET", f"{BASE}/tasks", {"results": []})
    upstream.add("POST", f"{BASE}/tasks", {"id": "t"})
    k = new_key(todo, grants={"todoist.tasks_list": {"project_id": ["p1"]}, "todoist.tasks_add": {"project_id": ["p1"]}})
    assert call(todo, k, "tasks_list", query={"project_id": "p1"}).status_code == 200
    assert call(todo, k, "tasks_list", query={"project_id": "p2"}).status_code == 403
    assert call(todo, k, "tasks_list").status_code == 403  # scoped field absent
    assert call(todo, k, "tasks_add", body={"content": "a", "project_id": "p1"}).status_code == 200
    assert call(todo, k, "tasks_add", body={"content": "a", "project_id": "p2"}).status_code == 403
    assert call(todo, k, "tasks_add", body={"content": "a"}).status_code == 403
    assert len(upstream.requests) == 2


def test_scope_on_path(admin, upstream):
    admin.post(f"{ADMIN}/services", json={"name": "gh", "base_url": "https://api.example.com/v3"})
    sid = admin.get(f"{ADMIN}/services").json()["services"][0]["id"]
    admin.put(f"{ADMIN}/services/{sid}/credential", json={"credential": "c"})
    r = admin.post(f"{ADMIN}/services/{sid}/operations", json={
        "name": "repo_get", "method": "GET", "path_template": "/repos/{owner}/{repo}", "scope_fields": ["owner"]})
    assert r.status_code == 200, r.text
    upstream.add("GET", "https://api.example.com/v3/repos/", {"ok": 1})
    k = new_key(admin, grants={"gh.repo_get": {"owner": ["me"]}})
    assert call(admin, k, "repo_get", svc="gh", path={"owner": "me", "repo": "x"}).status_code == 200
    assert call(admin, k, "repo_get", svc="gh", path={"owner": "you", "repo": "x"}).status_code == 403


def test_auth_styles(admin, upstream):
    upstream.add("GET", "https://h.example.com/", {"ok": True})
    for name, style, an in (("hdr", "header", "X-Api-Key"), ("qry", "query", "api_key")):
        sid = admin.post(f"{ADMIN}/services", json={"name": name, "base_url": "https://h.example.com",
                                                    "auth_style": style, "auth_name": an}).json()["id"]
        admin.put(f"{ADMIN}/services/{sid}/credential", json={"credential": "S3CRET"})
        admin.post(f"{ADMIN}/services/{sid}/operations", json={"name": "ping", "method": "GET", "path_template": "/ping"})
    k = new_key(admin, grants={"hdr.ping": None, "qry.ping": None})
    assert call(admin, k, "ping", svc="hdr").status_code == 200
    assert call(admin, k, "ping", svc="qry").status_code == 200
    a, b = upstream.requests
    assert a.headers["x-api-key"] == "S3CRET" and "authorization" not in a.headers
    assert b.url.params["api_key"] == "S3CRET"


def test_credential_never_leaks(todo, upstream, settings, caplog):
    upstream.add("GET", f"{BASE}/projects", httpx.Response(401, text=f"bad token {TOKEN} for you"))
    k = new_key(todo, grants={"todoist.projects_list": None})
    r = call(todo, k, "projects_list")
    assert r.status_code == 502 and r.json()["status"] == 401
    assert TOKEN not in r.text and "***" in r.text
    svc = todo.get(f"{ADMIN}/services").json()
    assert TOKEN not in json.dumps(svc) and svc["services"][0]["credential_set"] is True
    export = todo.get(f"{ADMIN}/services/{todo.svc_id}/export").json()
    assert TOKEN not in json.dumps(export) and "credential" not in json.dumps(export)
    conn = sqlite3.connect(settings.database_path)
    assert TOKEN not in json.dumps([list(r) for r in conn.execute("SELECT * FROM audit")])
    assert TOKEN.encode() not in open(settings.database_path, "rb").read()
    assert TOKEN not in caplog.text


def test_query_credential_redacted_in_errors(admin, upstream):
    sid = admin.post(f"{ADMIN}/services", json={"name": "q", "base_url": "https://h.example.com",
                                                "auth_style": "query", "auth_name": "key"}).json()["id"]
    admin.put(f"{ADMIN}/services/{sid}/credential", json={"credential": "a b/c"})
    admin.post(f"{ADMIN}/services/{sid}/operations", json={"name": "x", "method": "GET", "path_template": "/x"})
    upstream.add("GET", "https://h.example.com/x", lambda req: httpx.Response(500, text=f"failed url {req.url}"))
    k = new_key(admin, grants={"q.x": None})
    r = call(admin, k, "x", svc="q")
    assert r.status_code == 502 and "a b/c" not in r.text and "a+b%2Fc" not in r.text and "a%20b%2Fc" not in r.text


def test_no_redirects_and_size_cap(make_client, upstream):
    c = make_client(upstream_max_bytes=100)
    do_setup(c)
    do_login(c)
    c.post(f"{ADMIN}/services/import", json=PACK)
    c.put(f"{ADMIN}/services/1/credential", json={"credential": TOKEN})
    k = new_key(c, grants={"todoist.projects_list": None, "todoist.tasks_list": None})
    upstream.add("GET", f"{BASE}/projects", httpx.Response(302, headers={"Location": "https://evil.example/"}))
    upstream.add("GET", f"{BASE}/tasks", {"results": ["x" * 500]})
    r = call(c, k, "projects_list")
    assert r.status_code == 502 and r.json()["status"] == 302
    assert [str(q.url) for q in upstream.requests] == [f"{BASE}/projects"]
    r = call(c, k, "tasks_list")
    assert r.status_code == 502 and r.json()["detail"] == "response too large"


def test_timeout(todo, upstream):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    upstream.add("GET", f"{BASE}/projects", slow)
    k = new_key(todo, grants={"todoist.projects_list": None})
    assert call(todo, k, "projects_list").json() == {"error": "upstream_timeout"}


def test_definition_validation(admin):
    bad_services = [
        {"name": "x", "base_url": "http://plain.example.com"},
        {"name": "x", "base_url": "https://user:pw@h.example.com"},
        {"name": "x", "base_url": "https://h.example.com/?a=1"},
        {"name": "telegram", "base_url": "https://h.example.com"},
        {"name": "x", "base_url": "https://h.example.com", "auth_style": "header"},
        {"name": "Bad", "base_url": "https://h.example.com"},
    ]
    for b in bad_services:
        assert admin.post(f"{ADMIN}/services", json=b).status_code == 400, b
    sid = admin.post(f"{ADMIN}/services", json={"name": "ok", "base_url": "https://h.example.com"}).json()["id"]
    bad_ops = [
        {"name": "a", "method": "GET", "path_template": "no-slash"},
        {"name": "a", "method": "GET", "path_template": "/a/../b"},
        {"name": "a", "method": "GET", "path_template": "/a?x=1"},
        {"name": "a", "method": "GET", "path_template": "/{x}/{x}"},
        {"name": "a", "method": "GET", "path_template": "/{X}"},
        {"name": "a", "method": "TRACE", "path_template": "/a"},
        {"name": "a", "method": "GET", "path_template": "/a", "body_fields": ["x"]},
        {"name": "a", "method": "GET", "path_template": "/a", "scope_fields": ["undeclared"]},
    ]
    for o in bad_ops:
        assert admin.post(f"{ADMIN}/services/{sid}/operations", json=o).status_code == 400, o


def test_rename_and_delete_follow_permissions(todo, upstream):
    upstream.add("GET", f"{BASE}/projects", {"results": []})
    k = new_key(todo, grants={"todoist.projects_list": None})
    todo.patch(f"{ADMIN}/services/{todo.svc_id}", json={"name": "td"})
    assert call(todo, k, "projects_list", svc="td").status_code == 200
    ops = {o["name"]: o["id"] for o in todo.get(f"{ADMIN}/services/{todo.svc_id}/operations").json()["operations"]}
    todo.patch(f"{ADMIN}/operations/{ops['projects_list']}", json={"name": "projects"})
    assert call(todo, k, "projects", svc="td").status_code == 200
    todo.delete(f"{ADMIN}/operations/{ops['projects_list']}")
    assert todo.get(f"{ADMIN}/permissions").json()["grants"] == []


def test_disabled_service_forbidden(todo, upstream):
    k = new_key(todo, grants={"todoist.projects_list": None})
    todo.patch(f"{ADMIN}/services/{todo.svc_id}", json={"enabled": False})
    assert call(todo, k, "projects_list").status_code == 403
    assert upstream.requests == []


def test_pack_export_import_roundtrip(todo):
    export = todo.get(f"{ADMIN}/services/{todo.svc_id}/export").json()
    assert {o["name"] for o in export["operations"]} == {o["name"] for o in PACK["operations"]}
    k = new_key(todo, grants={"todoist.projects_list": None, "todoist.task_close": None})
    export["operations"] = [o for o in export["operations"] if o["name"] != "task_close"]
    for params in ({}, {"replace": "true"}):
        assert todo.post(f"{ADMIN}/services/import", params=params, json=export).status_code == 409
    grants = {g["action"] for g in todo.get(f"{ADMIN}/permissions").json()["grants"]}
    assert grants == {"todoist.projects_list", "todoist.task_close"}  # existing service untouched
    r = todo.post(f"{ADMIN}/services/import", params={"name": "copy"}, json=export)
    assert r.status_code == 200 and r.json()["credential_set"] is False and r.json()["operations"] == 8


def test_credential_reencrypted_on_passphrase_change(todo, make_app, upstream):
    upstream.add("GET", f"{BASE}/projects", {"results": []})
    k = new_key(todo, grants={"todoist.projects_list": None})
    todo.post(f"{ADMIN}/passphrase", json={"current": "correct horse battery staple", "new": "another passphrase!"})
    todo.__exit__(None, None, None)
    with TestClient(make_app()) as c:
        c.post(f"{ADMIN}/unlock", json={"passphrase": "another passphrase!"})
        assert call(c, k, "projects_list").status_code == 200
        assert upstream.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.live
def test_live_todoist_via_pack(settings):
    token = os.environ.get("LIVE_TODOIST_TOKEN")
    if not token:
        pytest.skip("LIVE_TODOIST_TOKEN not set")
    from app.main import create_app
    with TestClient(create_app(settings)) as c:  # real network transport
        do_setup(c)
        do_login(c)
        sid = c.post(f"{ADMIN}/services/import", json=PACK).json()["id"]
        c.put(f"{ADMIN}/services/{sid}/credential", json={"credential": token})
        k = new_key(c, grants={f"todoist.{o['name']}": None for o in PACK["operations"]})
        projects = call(c, k, "projects_list")
        assert projects.status_code == 200, projects.text
        added = call(c, k, "tasks_add", body={"content": "gateway live test (auto-closed)"})
        assert added.status_code == 200, added.text
        task_id = added.json()["body"]["id"]
        listed = call(c, k, "tasks_list")
        assert any(t["id"] == task_id for t in listed.json()["body"]["results"])
        assert call(c, k, "task_get", path={"task_id": task_id}).json()["body"]["id"] == task_id
        assert call(c, k, "task_close", path={"task_id": task_id}).status_code == 200
        assert call(c, k, "task_delete", path={"task_id": task_id}).status_code == 200


def test_validation_errors_explain_field_without_echoing_input(admin):
    r = admin.post(f"{ADMIN}/services", json={"name": "ok_name", "base_url": "http://plain.example"})
    assert r.status_code == 400
    assert r.json() == {"error": "bad_request",
                        "detail": "base_url must be https://host[/path] with no credentials, query or fragment"}
    r = admin.post(f"{ADMIN}/services", json={"name": "Bad-Name", "base_url": "https://x.example"})
    assert r.json()["detail"] == "name: invalid characters or length"
    r = admin.post(f"{ADMIN}/services", json={"name": "telegram", "base_url": "https://x.example"})
    assert r.json()["detail"] == "reserved service name"
    r = admin.post(f"{ADMIN}/services", json={"base_url": "https://x.example"})
    assert r.json()["detail"] == "name: Field required"
    secret = "sk_live_should_never_echo_123"
    r = admin.put(f"{ADMIN}/telegram/users", json={"users": [{"id": secret}]})
    assert r.status_code == 400 and secret not in r.text and r.json()["detail"].startswith("users.0.id:")
    r = admin.post(f"{ADMIN}/services", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "not valid JSON" in r.json()["detail"]


def _load(name):
    return json.loads((Path(__file__).parent.parent / "packs" / f"{name}.json").read_text())


def test_vikunja_pack_self_hosted(admin, upstream):
    pack = _load("vikunja")
    assert pack["self_hosted"] is True
    admin.app.state.gw.settings = admin.app.state.gw.settings.with_(packs_dir=PACKS_DIR)
    r = admin.post(f"{ADMIN}/packs/vikunja/import")
    assert r.status_code == 400 and "self-hosted" in r.json()["detail"]
    vb = "https://tasks.example.org/api/v1"
    r = admin.post(f"{ADMIN}/packs/vikunja/import", json={"base_url": vb})
    assert r.status_code == 200 and r.json()["base_url"] == vb
    admin.put(f"{ADMIN}/services/{r.json()['id']}/credential", json={"credential": "tk_vikunja_token"})
    upstream.add("GET", f"{vb}/projects", [{"id": 1}])
    upstream.add("GET", f"{vb}/labels", [])
    upstream.add("GET", f"{vb}/tasks", [{"id": 5}])
    upstream.add("GET", f"{vb}/projects/1/views/2/tasks", [])
    upstream.add("GET", f"{vb}/tasks/5", {"id": 5})
    upstream.add("PUT", f"{vb}/projects/1/tasks", {"id": 6})
    upstream.add("POST", f"{vb}/tasks/5", {"id": 5, "done": True})
    upstream.add("DELETE", f"{vb}/tasks/5", {"message": "Successfully deleted."})
    k = new_key(admin, grants={f"vikunja.{o['name']}": None for o in pack["operations"]})
    v = lambda op, **kw: call(admin, k, op, svc="vikunja", **kw)  # noqa: E731
    assert v("projects_list").json()["body"] == [{"id": 1}]
    assert v("labels_list").status_code == 200
    assert v("tasks_list", query={"filter": "done = false"}).json()["body"] == [{"id": 5}]
    assert v("project_tasks", path={"project_id": 1, "view_id": 2}).status_code == 200
    assert v("task_get", path={"task_id": 5}).status_code == 200
    assert v("tasks_add", path={"project_id": 1}, body={"title": "x", "due_date": "2026-12-01T00:00:00Z"}).json()["body"] == {"id": 6}
    assert v("task_update", path={"task_id": 5}, body={"title": "x", "done": True}).status_code == 200
    assert v("task_delete", path={"task_id": 5}).status_code == 200
    assert v("tasks_add", path={"project_id": 1}, body={"title": "x", "owner": 1}).status_code == 400  # undeclared field
    assert [f"{q.method} {q.url}" for q in upstream.requests] == [
        f"GET {vb}/projects", f"GET {vb}/labels", f"GET {vb}/tasks?filter=done+%3D+false",
        f"GET {vb}/projects/1/views/2/tasks", f"GET {vb}/tasks/5", f"PUT {vb}/projects/1/tasks",
        f"POST {vb}/tasks/5", f"DELETE {vb}/tasks/5"]
    assert all(q.headers["authorization"] == "Bearer tk_vikunja_token" for q in upstream.requests)


def test_kanbanflow_pack(admin, upstream):
    pack = _load("kanbanflow")
    kb = "https://kanbanflow.com/api/v1"
    r = admin.post(f"{ADMIN}/services/import", json=pack)
    assert r.status_code == 200
    admin.put(f"{ADMIN}/services/{r.json()['id']}/credential", json={"credential": "kf-token-abc"})
    upstream.add("GET", f"{kb}/board", {"columns": []})
    upstream.add("GET", f"{kb}/members", [])
    upstream.add("GET", f"{kb}/tasks", [])
    upstream.add("GET", f"{kb}/tasks/search", [])
    upstream.add("GET", f"{kb}/tasks/T1", {"_id": "T1"})
    upstream.add("POST", f"{kb}/tasks", {"taskId": "T2"})
    upstream.add("POST", f"{kb}/tasks/T1", {})
    upstream.add("DELETE", f"{kb}/tasks/T1", {})
    k = new_key(admin, grants={f"kanbanflow.{o['name']}": None for o in pack["operations"]}
                | {"kanbanflow.tasks_add": {"columnId": ["C1"]}})
    f = lambda op, **kw: call(admin, k, op, svc="kanbanflow", **kw)  # noqa: E731
    assert f("board_get").status_code == 200
    assert f("members_list").status_code == 200
    assert f("tasks_list", query={"columnId": "C1", "limit": 10}).status_code == 200
    assert f("tasks_search", query={"query": "milk"}).status_code == 200
    assert f("task_get", path={"task_id": "T1"}).json()["body"] == {"_id": "T1"}
    assert f("tasks_add", body={"name": "milk", "columnId": "C1"}).json()["body"] == {"taskId": "T2"}
    assert f("tasks_add", body={"name": "milk", "columnId": "C9"}).status_code == 403  # scope
    assert f("task_update", path={"task_id": "T1"}, body={"columnId": "C1"}).status_code == 200
    assert f("task_delete", path={"task_id": "T1"}).status_code == 200
    assert [f"{q.method} {q.url}" for q in upstream.requests] == [
        f"GET {kb}/board", f"GET {kb}/members", f"GET {kb}/tasks?columnId=C1&limit=10",
        f"GET {kb}/tasks/search?query=milk", f"GET {kb}/tasks/T1", f"POST {kb}/tasks", f"POST {kb}/tasks/T1",
        f"DELETE {kb}/tasks/T1"]
    assert all(q.headers["authorization"] == "Bearer kf-token-abc" for q in upstream.requests)


def test_every_bundled_pack_valid():
    from app.services.generic import Pack
    names = sorted(p.stem for p in Path(PACKS_DIR).glob("*.json"))
    assert names == ["kanbanflow", "todoist", "vikunja"]
    for n in names:
        Pack.model_validate(_load(n))
