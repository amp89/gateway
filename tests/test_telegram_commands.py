import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.services.telegram_commands import fill, parse, render, reply_text
from tests.conftest import ADMIN, BOT_TOKEN, PASSPHRASE, TG, do_login, do_setup, new_key, set_users

UPD = f"{TG}/bot{BOT_TOKEN}/getUpdates"
SEND = f"{TG}/bot{BOT_TOKEN}/sendMessage"
BASE = "https://api.todoist.com/api/v1"
PACK = json.loads((Path(__file__).parent.parent / "packs" / "todoist.json").read_text())
QUICK = {"body": {"text": "{text}"}}


def update(uid, sender, text, chat=None):
    return {"update_id": uid, "message": {"message_id": uid * 10, "date": 1_700_000_000 + uid,
                                          "chat": {"id": chat or sender}, "text": text,
                                          "from": {"id": sender, "username": f"u{sender}"}}}


def build(make_client, **overrides):
    """Unsealed + logged in; users {111, 222}; bot 'alerts' (polling driven by hand) allowing both; Todoist with a
    credential; key 'family' granted quick_add + tasks_add limited to project p1."""
    c = make_client(**overrides)
    do_setup(c)
    do_login(c)
    set_users(c, [111, 222])
    r = c.post(f"{ADMIN}/bots", json={"name": "alerts", "token": BOT_TOKEN, "polling_enabled": False})
    c.bot_id = r.json()["id"]
    c.put(f"{ADMIN}/bots/{c.bot_id}/allowlist", json={"user_ids": [111, 222]})
    c.app.state.gw.conn.execute("UPDATE bots SET polling_enabled = 1 WHERE id = ?", (c.bot_id,))
    svc = c.post(f"{ADMIN}/services/import", json=PACK).json()
    c.svc_id = svc["id"]
    c.put(f"{ADMIN}/services/{c.svc_id}/credential", json={"credential": "todoist-secret-0123456789"})
    new_key(c, "family", grants={"todoist.quick_add": None, "todoist.tasks_add": {"project_id": ["p1"]}})
    c.key_id = next(k["id"] for k in c.get(f"{ADMIN}/keys").json()["keys"] if k["name"] == "family")
    return c


@pytest.fixture
def env(make_client, upstream):
    upstream.add("POST", SEND, {"ok": True, "result": {"message_id": 1}})
    return build(make_client)


def rule(c, command="todo", action="todoist.quick_add", call=None, reply="", expect=200, **kw):
    body = {"bot_id": c.bot_id, "command": command, "key_id": c.key_id, "action": action,
            "call": QUICK if call is None else call, "reply": reply, **kw}
    r = c.post(f"{ADMIN}/commands", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def deliver(c, upstream, *updates):
    upstream.add("POST", UPD, {"ok": True, "result": list(updates)})
    assert c.portal.call(c.app.state.gw.pollers.poll_once, c.bot_id) is True


def sent(upstream):
    return [json.loads(r.content) for r in upstream.requests if str(r.url) == SEND]


def todoist_calls(upstream):
    return [r for r in upstream.requests if str(r.url).startswith(BASE)]


def last_audit(c, action):
    return c.get(f"{ADMIN}/audit", params={"action": action}).json()["rows"][0]


# -- pure helpers ---------------------------------------------------------------------------
def test_parse():
    assert parse("/todo buy milk") == ("todo", "buy milk")
    assert parse("/TODO@my_bot  buy\nmilk ") == ("todo", "buy\nmilk")
    assert parse("/todo") == ("todo", "")
    assert parse("todo buy") is None and parse("/ todo") is None and parse("/to-do x") is None


def test_fill_keeps_text_inside_values():
    evil = '", "project_id": "p9"} {text}'
    out = fill({"body": {"text": "{text}", "n": 3, "labels": ["a {text}"]}}, evil)
    assert out == {"body": {"text": evil, "n": 3, "labels": ["a " + evil]}}


def test_render():
    data = {"status": 200, "body": {"content": "milk", "results": [{"content": "a"}, {"content": "b"}],
                                    "tags": ["x", "y"], "obj": {"k": 1}, "flag": True}}
    assert render("added {body.content}", data) == "added milk"
    assert render("{#body.results}- {content}\n{/body.results}", data) == "- a\n- b\n"
    assert render("{#body.tags}{.},{/body.tags}", data) == "x,y,"
    assert render("{body.missing}|{body.obj}|{body.flag}|{status}|{body.results.1.content}", data) \
        == '|{"k":1}|true|200|b'
    assert render("{#body.missing}x{/body.missing}", data) == ""
    assert reply_text("", data) == "done" and reply_text("{body.missing}", data) == "done"
    assert len(reply_text("{#body.results}" + "z" * 3000 + "{/body.results}", data)) == 4096


# -- running ----------------------------------------------------------------------------------
def test_command_runs_stores_replies_audits(env, upstream, settings):
    upstream.add("POST", f"{BASE}/tasks/quick", {"id": "t1", "content": "buy milk"})
    rule(env)
    deliver(env, upstream, update(1, 111, "/todo buy milk tomorrow"))
    (call,) = todoist_calls(upstream)
    assert json.loads(call.content) == {"text": "buy milk tomorrow"}
    assert sent(upstream) == [{"chat_id": 111, "text": "done"}]
    msgs = env.get(f"{ADMIN}/messages").json()["messages"]
    assert [m["text"] for m in msgs] == ["/todo buy milk tomorrow"]
    a = last_audit(env, "todoist.quick_add")
    assert a["key_name"] == "family" and a["status"] == "200" and a["client_ip"] == "telegram"
    assert "bot=alerts command=todo" in a["resource"]
    raw = open(settings.database_path, "rb").read()
    assert b"buy milk" not in raw and b"{text}" not in raw  # message and template encrypted


def test_injection_stays_in_value(env, upstream):
    upstream.add("POST", f"{BASE}/tasks", {"id": "t1"})
    rule(env, "add", "todoist.tasks_add", {"body": {"content": "{text}", "project_id": "p1"}})
    evil = 'x", "project_id": "p9", "y": "{text}'
    deliver(env, upstream, update(1, 111, "/add " + evil))
    assert json.loads(todoist_calls(upstream)[0].content) == {"content": evil, "project_id": "p1"}


def test_templated_reply(env, upstream):
    upstream.add("POST", f"{BASE}/tasks/quick", {"id": "t1", "content": "buy milk", "due": {"string": "tomorrow"}})
    rule(env, reply="added {body.content} ({body.due.string})")
    deliver(env, upstream, update(1, 111, "/todo buy milk tomorrow"))
    assert sent(upstream) == [{"chat_id": 111, "text": "added buy milk (tomorrow)"}]  # plain text, no parse_mode


def test_permission_scope_key_state(env, upstream):
    upstream.add("POST", f"{BASE}/tasks", {"id": "t1"})
    rule(env, "p9", "todoist.tasks_add", {"body": {"content": "{text}", "project_id": "p9"}})  # outside scope
    rule(env, "close", "todoist.task_close", {"path": {"task_id": "{text}"}})  # not granted
    rule(env)
    deliver(env, upstream, update(1, 111, "/p9 a"), update(2, 111, "/close t1"))
    assert todoist_calls(upstream) == []
    assert [s["text"] for s in sent(upstream)] == ["not allowed", "not allowed"]
    assert last_audit(env, "todoist.task_close")["status"] == "403"
    env.patch(f"{ADMIN}/keys/{env.key_id}", json={"status": "disabled"})
    deliver(env, upstream, update(3, 111, "/todo x"))
    env.delete(f"{ADMIN}/keys/{env.key_id}")
    deliver(env, upstream, update(4, 111, "/todo x"))
    assert todoist_calls(upstream) == [] and [s["text"] for s in sent(upstream)][2:] == ["not allowed"] * 2


def test_not_run(env, upstream):
    rule(env)
    rule(env, "off", enabled=False)
    deliver(env, upstream, update(1, 111, "/unknown x"), update(2, 111, "/off x"), update(3, 111, "todo x"),
            update(4, 111, "/todo x", chat=-100123), update(5, 999, "/todo x"))
    assert todoist_calls(upstream) == [] and sent(upstream) == []
    assert len(env.get(f"{ADMIN}/messages").json()["messages"]) == 4  # sender 999 dropped as before


def test_needs_text(env, upstream):
    rule(env)
    deliver(env, upstream, update(1, 111, "/todo"))
    assert todoist_calls(upstream) == [] and sent(upstream) == [{"chat_id": 111, "text": "needs text after the command"}]


def timeout(request):
    raise httpx.ReadTimeout("slow", request=request)


@pytest.mark.parametrize("resp,text", [
    (httpx.Response(500, text="secret upstream detail"), "upstream error"),
    (timeout, "upstream timeout"),
])
def test_upstream_failures_short(env, upstream, resp, text):
    upstream.add("POST", f"{BASE}/tasks/quick", resp)
    rule(env, reply="{body.content}")
    deliver(env, upstream, update(1, 111, "/todo x"))
    assert sent(upstream) == [{"chat_id": 111, "text": text}]


def test_bad_path_value_not_accepted(env, upstream):
    new_key(env, "closer", grants={"todoist.task_close": None})
    kid = next(k["id"] for k in env.get(f"{ADMIN}/keys").json()["keys"] if k["name"] == "closer")
    rule(env, "close", "todoist.task_close", {"path": {"task_id": "{text}"}}, key_id=kid)
    deliver(env, upstream, update(1, 111, "/close ../../x y"))
    assert todoist_calls(upstream) == []
    assert sent(upstream) == [{"chat_id": 111, "text": "not accepted: invalid path param 'task_id'"}]


def test_rate_limited(make_client, upstream):
    upstream.add("POST", SEND, {"ok": True, "result": {}})
    upstream.add("POST", f"{BASE}/tasks/quick", {"id": "t"})
    c = build(make_client, api_rate_per_min=1)
    rule(c)
    deliver(c, upstream, update(1, 111, "/todo a"), update(2, 111, "/todo b"))
    assert [s["text"] for s in sent(upstream)] == ["done", "too many requests"]


def test_batch_order_failure_isolated_no_rerun(env, upstream):
    seen = []

    def quick(req):
        t = json.loads(req.content)["text"]
        seen.append(t)
        return httpx.Response(500) if t == "b" else httpx.Response(200, json={"id": t})

    upstream.add("POST", f"{BASE}/tasks/quick", quick)
    rule(env)
    deliver(env, upstream, update(1, 111, "/todo a"), update(2, 222, "/todo b"), update(3, 111, "/todo c"))
    assert seen == ["a", "b", "c"]
    assert [(s["chat_id"], s["text"]) for s in sent(upstream)] == [(111, "done"), (222, "upstream error"),
                                                                    (111, "done")]
    deliver(env, upstream, update(1, 111, "/todo a"), update(3, 111, "/todo c"))  # redelivered: duplicates ignored
    assert seen == ["a", "b", "c"]


def test_reply_send_failure_keeps_polling(env, upstream):
    upstream.add("POST", SEND, httpx.Response(400, json={"ok": False, "description": "chat not found"}))
    upstream.add("POST", f"{BASE}/tasks/quick", {"id": "t"})
    rule(env)
    deliver(env, upstream, update(1, 111, "/todo a"), update(2, 111, "/todo b"))
    assert len(todoist_calls(upstream)) == 2


def test_old_commands_still_run(env, upstream, clock):
    """Commands queued at Telegram while sealed arrive on the first poll after unlock; age is not checked."""
    upstream.add("POST", f"{BASE}/tasks/quick", {"id": "t"})
    rule(env)
    clock.advance(86_000)
    deliver(env, upstream, update(1, 111, "/todo old"))  # message date is far in the past
    assert len(todoist_calls(upstream)) == 1


# -- admin ------------------------------------------------------------------------------------
def test_admin_crud_and_validation(env):
    r = rule(env, reply="ok {body.id}")
    assert r["call"] == {"path": {}, "query": {}, "body": {"text": "{text}"}}
    assert r["bot"] == "alerts" and r["key"] == "family" and r["problem"] == ""
    rule(env, expect=409)  # same bot + command
    rule(env, "Bad", expect=400)
    rule(env, "x1", "telegram.send", expect=400)
    rule(env, "x2", "todoist.nope", expect=400)
    rule(env, "x3", call={"headers": {}}, expect=400)
    rule(env, "x4", "todoist.task_close", {"path": {}}, expect=400)
    rule(env, "x5", key_id=9999, expect=400)
    rule(env, "x6", bot_id=9999, expect=400)
    p = env.patch(f"{ADMIN}/commands/{r['id']}", json={"command": "task", "reply": ""}).json()
    assert p["command"] == "task" and p["reply"] == "" and p["call"]["body"] == {"text": "{text}"}
    assert env.patch(f"{ADMIN}/commands/{r['id']}", json={"action": "todoist.nope"}).status_code == 400
    lst = env.get(f"{ADMIN}/commands").json()["commands"]
    assert [c["command"] for c in lst] == ["task"]
    assert env.delete(f"{ADMIN}/commands/{r['id']}").status_code == 200
    assert env.delete(f"{ADMIN}/commands/{r['id']}").status_code == 404
    acts = [e["action"] for e in env.get(f"{ADMIN}/audit").json()["rows"]]
    assert {"admin.command.create", "admin.command.update", "admin.command.delete"} <= set(acts)


def test_admin_needs_session_and_csrf(env):
    env.headers.pop("X-CSRF-Token")
    assert env.post(f"{ADMIN}/commands", json={}).status_code == 403
    env.cookies.clear()
    assert env.get(f"{ADMIN}/commands").status_code == 401


def test_problem_shown(env):
    r = rule(env, "close", "todoist.task_close", {"path": {"task_id": "{text}"}})
    assert r["problem"].startswith("key lacks this permission")
    env.patch(f"{ADMIN}/keys/{env.key_id}", json={"status": "disabled"})
    assert env.get(f"{ADMIN}/commands").json()["commands"][0]["problem"] == "key disabled"


def test_service_rename_follows_and_bot_delete_cascades(env):
    r = rule(env)
    env.patch(f"{ADMIN}/services/{env.svc_id}", json={"name": "todo2"})
    got = env.get(f"{ADMIN}/commands").json()["commands"][0]
    assert got["action"] == "todo2.quick_add" and got["problem"] == ""
    ops = env.get(f"{ADMIN}/services/{env.svc_id}/operations").json()["operations"]
    op = next(o for o in ops if o["name"] == "quick_add")
    env.patch(f"{ADMIN}/operations/{op['id']}", json={"name": "quick"})
    assert env.get(f"{ADMIN}/commands").json()["commands"][0]["action"] == "todo2.quick"
    env.delete(f"{ADMIN}/bots/{env.bot_id}")
    assert env.get(f"{ADMIN}/commands").json()["commands"] == []
    assert env.delete(f"{ADMIN}/commands/{r['id']}").status_code == 404


def test_passphrase_change_keeps_rules(env, upstream):
    upstream.add("POST", f"{BASE}/tasks/quick", {"id": "t", "content": "a"})
    rule(env, reply="got {body.content}")
    r = env.post(f"{ADMIN}/passphrase", json={"current": PASSPHRASE, "new": "another passphrase!"})
    assert r.status_code == 200, r.text
    got = env.get(f"{ADMIN}/commands").json()["commands"][0]
    assert got["call"]["body"] == {"text": "{text}"} and got["reply"] == "got {body.content}"
    deliver(env, upstream, update(1, 111, "/todo a"))
    assert sent(upstream)[-1]["text"] == "got a"


def test_rules_encrypted_at_rest(env, settings):
    rule(env, reply="secret reply template")
    conn = sqlite3.connect(settings.database_path)
    row = conn.execute("SELECT call_enc, reply_enc FROM commands").fetchone()
    assert row[0].startswith("gAAAAA") and row[1].startswith("gAAAAA")
