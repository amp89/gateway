"""A bot command that can't work can't be saved; one that breaks later says why (SPEC 9.1).

Regression for: call body {"text": "{text}"} on kanbanflow.tasks_add saved fine, then every /cmd replied
"not accepted" (undeclared body field, missing name/columnId) with no reason anywhere.
"""
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app import db
from app.services.generic import CallBody, CallError, check_call, op_def
from tests.conftest import ADMIN, BOT_TOKEN, TG, bearer, do_login, do_setup, new_key, set_users

UPD = f"{TG}/bot{BOT_TOKEN}/getUpdates"
SEND = f"{TG}/bot{BOT_TOKEN}/sendMessage"
KF = "https://kanbanflow.com/api/v1"
PACKS = Path(__file__).parent.parent / "packs"
KF_PACK = json.loads((PACKS / "kanbanflow.json").read_text())
GOOD = {"body": {"name": "{text}", "columnId": "col1"}}


def update(uid, text, sender=111):
    return {"update_id": uid, "message": {"message_id": uid * 10, "date": 1_700_000_000 + uid,
                                          "chat": {"id": sender}, "text": text,
                                          "from": {"id": sender, "username": f"u{sender}"}}}


@pytest.fixture
def env(make_client, upstream):
    """Bot 'alerts' for user 111; KanbanFlow from the bundled pack with a credential; key 'kbflow write' granted
    tasks_add limited to column col1, key 'open' granted tasks_add unlimited."""
    upstream.add("POST", SEND, {"ok": True, "result": {"message_id": 1}})
    c = make_client()
    do_setup(c)
    do_login(c)
    set_users(c, [111])
    c.bot_id = c.post(f"{ADMIN}/bots", json={"name": "alerts", "token": BOT_TOKEN, "polling_enabled": False}).json()["id"]
    c.put(f"{ADMIN}/bots/{c.bot_id}/allowlist", json={"user_ids": [111]})
    c.app.state.gw.conn.execute("UPDATE bots SET polling_enabled = 1 WHERE id = ?", (c.bot_id,))
    c.svc_id = c.post(f"{ADMIN}/services/import", json=KF_PACK).json()["id"]
    c.put(f"{ADMIN}/services/{c.svc_id}/credential", json={"credential": "kf-secret-0123456789"})
    c.secret = new_key(c, "kbflow write", grants={"kanbanflow.tasks_add": {"columnId": ["col1"]}})
    c.open_secret = new_key(c, "open", grants={"kanbanflow.tasks_add": None})
    keys = {k["name"]: k["id"] for k in c.get(f"{ADMIN}/keys").json()["keys"]}
    c.key_id, c.open_id = keys["kbflow write"], keys["open"]
    return c


def body(c, call, command="ass", key_id=None, **kw):
    return {"bot_id": c.bot_id, "command": command, "key_id": key_id or c.key_id, "action": "kanbanflow.tasks_add",
            "call": call, "reply": "", **kw}


def save(c, call, expect=200, **kw):
    r = c.post(f"{ADMIN}/commands", json=body(c, call, **kw))
    assert r.status_code == expect, r.text
    return r.json()


def deliver(c, upstream, *updates):
    upstream.add("POST", UPD, {"ok": True, "result": list(updates)})
    assert c.portal.call(c.app.state.gw.pollers.poll_once, c.bot_id) is True


def sent(upstream):
    return [json.loads(r.content)["text"] for r in upstream.requests if str(r.url) == SEND]


def kf_calls(upstream):
    return [r for r in upstream.requests if str(r.url).startswith(KF)]


def commands(c):
    return c.get(f"{ADMIN}/commands").json()["commands"]


def ops(c):
    return {o["name"]: o for o in c.get(f"{ADMIN}/services/{c.svc_id}/operations").json()["operations"]}


# -- saving -----------------------------------------------------------------------------------
def test_the_reported_command_is_refused(env):
    r = save(env, {"path": {}, "query": {}, "body": {"text": "{text}"}}, expect=400)
    assert "undeclared body fields ['text']" in r["detail"]
    assert "allowed:" in r["detail"] and "columnId" in r["detail"] and "name" in r["detail"]
    assert commands(env) == []


def test_required_fields(env):
    assert ops(env)["tasks_add"]["required_fields"] == ["name", "columnId"]
    assert "missing required field 'columnId'" in save(env, {"body": {"name": "{text}"}}, expect=400)["detail"]
    assert "'name', 'columnId'" in save(env, {"body": {}}, expect=400)["detail"]
    assert "missing" in save(env, {"body": {"name": "{text}", "columnId": ""}}, expect=400)["detail"]
    r = save(env, GOOD)
    assert r["problem"] == "" and r["note"] == "" and r["last_error"] == ""


def test_required_fields_on_api_calls(env, upstream):
    r = env.post("/api/v1/call/kanbanflow/tasks_add", json={"body": {"name": "x"}}, headers=bearer(env.open_secret))
    assert r.status_code == 400 and "columnId" in r.json()["detail"]
    assert kf_calls(upstream) == []


def test_patch_is_checked_too(env):
    r = save(env, GOOD)
    p = env.patch(f"{ADMIN}/commands/{r['id']}", json={"call": {"body": {"text": "{text}"}}})
    assert p.status_code == 400 and "undeclared body fields ['text']" in p.json()["detail"]
    assert commands(env)[0]["call"]["body"] == GOOD["body"]


def test_operation_required_fields_validated(env):
    op = ops(env)["tasks_add"]
    r = env.patch(f"{ADMIN}/operations/{op['id']}", json={"required_fields": ["nope"]})
    assert r.status_code == 400 and "required field 'nope'" in r.json()["detail"]
    r = env.patch(f"{ADMIN}/operations/{op['id']}", json={"required_fields": ["name"]})
    assert r.status_code == 200 and r.json()["required_fields"] == ["name"]
    exported = env.get(f"{ADMIN}/services/{env.svc_id}/export").json()
    assert next(o for o in exported["operations"] if o["name"] == "tasks_add")["required_fields"] == ["name"]


# -- permissions --------------------------------------------------------------------------------
def test_scope_problems_shown_at_save(env):
    r = save(env, {"body": {"name": "{text}", "columnId": "col9"}})
    assert r["problem"] == "columnId is outside what key 'kbflow write' may use (see Permissions)"
    r = save(env, {"body": {"name": "{text}", "columnId": "{text}"}}, command="b")
    assert r["problem"] == "" and r["note"].startswith("columnId comes from the message text")
    assert save(env, GOOD, command="c")["problem"] == ""
    assert save(env, {"body": {"name": "{text}", "columnId": "col9"}}, command="d", key_id=env.open_id)["problem"] == ""
    new_key(env, "none")
    kid = next(k["id"] for k in env.get(f"{ADMIN}/keys").json()["keys"] if k["name"] == "none")
    assert save(env, GOOD, command="e", key_id=kid)["problem"].startswith("key lacks this permission")


def test_scope_field_missing_from_template(env):
    env.patch(f"{ADMIN}/operations/{ops(env)['tasks_add']['id']}", json={"required_fields": ["name"]})
    r = save(env, {"body": {"name": "{text}"}})
    assert r["problem"] == "key 'kbflow write' is limited by columnId: set columnId in the call template"


def test_stale_command_flagged(env):
    r = save(env, {"body": {"name": "{text}", "columnId": "col1", "color": "red"}})
    assert r["problem"] == ""
    op = ops(env)["tasks_add"]
    fields = [f for f in op["body_fields"] if f != "color"]
    assert env.patch(f"{ADMIN}/operations/{op['id']}", json={"body_fields": fields}).status_code == 200
    assert commands(env)[0]["problem"].startswith("call template no longer fits kanbanflow.tasks_add: undeclared body fields ['color']")
    env.put(f"{ADMIN}/permissions/{env.key_id}/kanbanflow.tasks_add", json={"scope": {"columnId": ["other"]}})
    env.patch(f"{ADMIN}/operations/{op['id']}", json={"body_fields": op["body_fields"]})
    assert commands(env)[0]["problem"] == "columnId is outside what key 'kbflow write' may use (see Permissions)"


# -- saving and running agree ---------------------------------------------------------------------
CASES = [
    ({"body": {"text": "x"}}, False),
    ({"body": {"name": "x"}}, False),
    ({"body": {"name": "x", "columnId": ""}}, False),
    ({"body": {"name": "x", "columnId": None}}, False),
    ({"body": ["name"]}, False),
    ({"query": {"name": "x"}, "body": {"name": "x", "columnId": "c"}}, False),
    ({"path": {"x": "1"}, "body": {"name": "x", "columnId": "c"}}, False),
    ({"body": {"name": "x", "columnId": "c"}}, True),
    ({"body": {"name": "x", "columnId": "c", "color": "red", "labels": [{"name": "l"}]}}, True),
]


@pytest.mark.parametrize("call,ok", CASES)
def test_save_and_relay_agree(env, upstream, call, ok):
    upstream.add("POST", f"{KF}/tasks", {"taskId": "t1"})
    saved = env.post(f"{ADMIN}/commands", json=body(env, call, key_id=env.open_id)).status_code
    relayed = env.post("/api/v1/call/kanbanflow/tasks_add", json=call, headers=bearer(env.open_secret)).status_code
    assert (saved == 200, relayed == 200) == (ok, ok)


def test_check_call_is_the_relay_check():
    op = op_def({"id": 1, "name": "t", "method": "POST", "path_template": "/p/{id}", "query_fields_json": '["q"]',
                 "body_fields_json": '["a", "b"]', "scope_fields_json": "[]", "required_fields_json": '["a"]',
                 "description": "", "enabled": 1})
    assert check_call(op, CallBody(path={"id": "7"}, body={"a": 1})) == "/p/7"
    for bad in (CallBody(path={"id": "7"}, body={"b": 1}), CallBody(path={"id": "../x"}, body={"a": 1}),
                CallBody(path={"id": "7"}, query={"z": 1}, body={"a": 1}), CallBody(body={"a": 1})):
        with pytest.raises(CallError):
            check_call(op, bad)


# -- running -------------------------------------------------------------------------------------
def test_end_to_end_task_created(env, upstream):
    upstream.add("POST", f"{KF}/tasks", {"taskId": "t1", "name": "buy milk"})
    save(env, GOOD, reply="added {body.name}")
    deliver(env, upstream, update(1, "/ass buy milk"))
    (req,) = kf_calls(upstream)
    assert req.method == "POST" and str(req.url) == f"{KF}/tasks"
    assert json.loads(req.content) == {"name": "buy milk", "columnId": "col1"}
    assert req.headers["authorization"] == "Bearer kf-secret-0123456789"
    assert sent(upstream) == ["added buy milk"]
    assert commands(env)[0]["last_error"] == ""


def test_last_error_recorded_encrypted_and_cleared(env, upstream, settings):
    upstream.add("POST", f"{KF}/tasks", lambda req: httpx.Response(
        400 if "fail" in json.loads(req.content)["name"] else 200, json={"error": "column gone"}))
    save(env, GOOD)
    deliver(env, upstream, update(1, "/ass please fail"))
    assert sent(upstream) == ["upstream error"]
    got = commands(env)[0]
    assert got["last_error"].startswith("upstream error (502 upstream_error)") and "column gone" in got["last_error"]
    assert got["last_error_at"] is not None
    row = sqlite3.connect(settings.database_path).execute("SELECT last_error_enc FROM commands").fetchone()
    assert row[0].startswith("gAAAAA")
    deliver(env, upstream, update(2, "/ass fine"))
    got = commands(env)[0]
    assert got["last_error"] == "" and got["last_error_at"] is None


def test_shape_failure_at_run_time_says_why(env, upstream):
    save(env, GOOD)
    env.app.state.gw.conn.execute(  # an operation changed behind the command's back
        "UPDATE operations SET required_fields_json = '[\"name\", \"columnId\", \"color\"]' WHERE name = 'tasks_add'")
    deliver(env, upstream, update(1, "/ass x"))
    assert kf_calls(upstream) == []
    assert sent(upstream) == ["not accepted: missing required field 'color'"]
    assert commands(env)[0]["last_error"] == "not accepted: missing required field 'color'"


def test_scope_refusal_recorded(env, upstream):
    save(env, {"body": {"name": "{text}", "columnId": "col9"}})
    deliver(env, upstream, update(1, "/ass x"))
    assert kf_calls(upstream) == [] and sent(upstream) == ["not allowed"]
    assert commands(env)[0]["last_error"].startswith("not allowed (403 forbidden)")


# -- dry run ---------------------------------------------------------------------------------------
def test_dry_run(env, upstream):
    r = env.post(f"{ADMIN}/commands/dry-run", json=body(env, GOOD, text="buy milk"))
    assert r.status_code == 200, r.text
    assert r.json() == {"method": "POST", "url": f"{KF}/tasks", "query": {}, "body": {"name": "buy milk", "columnId": "col1"},
                        "problem": "", "note": ""}
    r = env.post(f"{ADMIN}/commands/dry-run", json=body(env, {"body": {"text": "{text}"}}))
    assert r.status_code == 400 and "undeclared body fields ['text']" in r.json()["detail"]
    r = env.post(f"{ADMIN}/commands/dry-run", json=body(env, {"body": {"name": "x", "columnId": "col9"}}))
    assert r.json()["problem"].startswith("columnId is outside")
    assert upstream.requests == [] and commands(env) == []  # nothing sent, nothing saved


def test_dry_run_with_text_that_breaks_a_path(env):
    new_key(env, "del", grants={"kanbanflow.task_delete": None})
    kid = next(k["id"] for k in env.get(f"{ADMIN}/keys").json()["keys"] if k["name"] == "del")
    b = body(env, {"path": {"task_id": "{text}"}}, key_id=kid, text="../x") | {"action": "kanbanflow.task_delete"}
    r = env.post(f"{ADMIN}/commands/dry-run", json=b)
    assert r.status_code == 400 and r.json()["detail"] == "with this text: invalid path param 'task_id'"


def test_operation_hints(env):
    got = {o["action"]: o for o in env.get(f"{ADMIN}/commands/operations").json()["operations"]}
    add = got["kanbanflow.tasks_add"]
    assert add["required_fields"] == ["name", "columnId"] and add["scope_fields"] == ["columnId"]
    assert "name" in add["body_fields"] and add["method"] == "POST" and add["path_fields"] == []
    assert got["kanbanflow.task_delete"]["path_fields"] == ["task_id"]


# -- migration ------------------------------------------------------------------------------------
def test_migration_marks_installed_bundled_packs(tmp_path):
    conn = db.connect(str(tmp_path / "old.sqlite3"))
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
    for f in sorted(db.MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if int(f.name[:4]) >= 11:
            break
        for stmt in f.read_text().split(";\n"):
            if stmt.strip():
                conn.execute(stmt)
        conn.execute("INSERT INTO schema_migrations VALUES (?, 0)", (int(f.name[:4]),))
    for name, pack in (("kanbanflow", "kanbanflow"), ("mykf", None)):
        sid = conn.execute("INSERT INTO services (name, base_url, auth_style, auth_name, enabled, updated_at,"
                           " source_pack) VALUES (?, 'https://x', 'bearer', '', 1, 0, ?)", (name, pack)).lastrowid
        conn.execute("INSERT INTO operations (service_id, name, method, path_template, query_fields_json,"
                     " body_fields_json, scope_fields_json, description, enabled)"
                     " VALUES (?, 'tasks_add', 'POST', '/tasks', '[]', '[\"name\", \"columnId\"]', '[]', '', 1)", (sid,))
    assert db.migrate(conn) == [11]
    got = dict(conn.execute("SELECT s.name, o.required_fields_json FROM operations o JOIN services s"
                            " ON s.id = o.service_id").fetchall())
    assert json.loads(got["kanbanflow"]) == ["name", "columnId"]
    assert got["mykf"] is None  # hand-made service: left alone
