import json
import os
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN, BOT_TOKEN, PASSPHRASE, TG, bearer, do_login, do_setup, new_key, set_users

SEND = f"{TG}/bot{BOT_TOKEN}/sendMessage"


def ok_send(upstream, message_id=42):
    upstream.add("POST", SEND, {"ok": True, "result": {"message_id": message_id}})


def test_send_reaches_allowed_chat(tg_admin, upstream):
    ok_send(upstream)
    k = new_key(tg_admin, grants={"telegram.send": None})
    r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k),
                      json={"bot": "alerts", "chat_id": 111, "text": "hi", "parse_mode": "HTML", "silent": True})
    assert r.status_code == 200 and r.json() == {"message_id": 42}
    assert upstream.bodies() == [{"chat_id": 111, "text": "hi", "parse_mode": "HTML", "disable_notification": True}]


def test_non_allowed_chat_refused_before_upstream(tg_admin, upstream):
    ok_send(upstream)
    k = new_key(tg_admin, grants={"telegram.send": None})
    for chat in (333, 999):  # 333: known user but not on this bot; 999: nowhere
        r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "alerts", "chat_id": chat, "text": "x"})
        assert r.status_code == 403
    r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "nobot", "chat_id": 111, "text": "x"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert upstream.requests == []


def test_scope_by_bot_and_chat(tg_admin, upstream):
    ok_send(upstream)
    k = new_key(tg_admin, grants={"telegram.send": {"chat_ids": [222]}})
    h = bearer(k)
    assert tg_admin.post("/api/v1/telegram/send", headers=h, json={"bot": "alerts", "chat_id": 111, "text": "x"}).status_code == 403
    assert tg_admin.post("/api/v1/telegram/send", headers=h, json={"bot": "alerts", "chat_id": 222, "text": "x"}).status_code == 200
    k2 = new_key(tg_admin, "other", grants={"telegram.send": {"bots": ["different"]}})
    assert tg_admin.post("/api/v1/telegram/send", headers=bearer(k2), json={"bot": "alerts", "chat_id": 222, "text": "x"}).status_code == 403


def test_unknown_user_refused_on_allowlist(tg_admin):
    r = tg_admin.put(f"{ADMIN}/bots/{tg_admin.bot_id}/allowlist", json={"user_ids": [111, 444]})
    assert r.status_code == 400 and r.json() == {"error": "unknown_user", "user_ids": [444]}
    assert tg_admin.get(f"{ADMIN}/bots/{tg_admin.bot_id}/allowlist").json()["user_ids"] == [111, 222]


def test_removing_global_user_blocks_send_and_strips_bot_lists(tg_admin, upstream):
    ok_send(upstream)
    k = new_key(tg_admin, grants={"telegram.send": None})
    set_users(tg_admin, [222, 333])
    r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "alerts", "chat_id": 111, "text": "x"})
    assert r.status_code == 403 and upstream.requests == []
    assert tg_admin.get(f"{ADMIN}/bots/{tg_admin.bot_id}/allowlist").json()["user_ids"] == [222]
    # re-adding the user globally does not silently restore bot access
    set_users(tg_admin, [111, 222, 333])
    r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "alerts", "chat_id": 111, "text": "x"})
    assert r.status_code == 403


def test_users_validation_and_listing(tg_admin):
    r = tg_admin.put(f"{ADMIN}/telegram/users", json={"users": [{"id": 1, "label": "a"}, {"id": 1, "label": "b"}]})
    assert r.status_code == 400
    assert tg_admin.put(f"{ADMIN}/telegram/users", json={"users": [{"id": 0}]}).status_code == 400
    assert tg_admin.put(f"{ADMIN}/telegram/users", json={"users": [{"id": 5, "label": "x" * 65}]}).status_code == 400
    users = tg_admin.get(f"{ADMIN}/telegram/users").json()["users"]
    assert [u["id"] for u in users] == [111, 222, 333] and users[0]["label"] == "user 111"
    assert tg_admin.get(f"{ADMIN}/bots").json()["users"] == users


def test_users_admin_only_and_csrf(tg_admin):
    del tg_admin.headers["X-CSRF-Token"]
    assert tg_admin.put(f"{ADMIN}/telegram/users", json={"users": []}).status_code == 403
    tg_admin.cookies.clear()
    assert tg_admin.get(f"{ADMIN}/telegram/users").status_code == 401


def test_users_encrypted_on_disk(tg_admin, settings):
    set_users(tg_admin, [987654321])
    conn = sqlite3.connect(settings.database_path)
    v = conn.execute("SELECT value FROM meta WHERE key = 'telegram_users_enc'").fetchone()[0]
    assert "987654321" not in v and "user" not in v


def test_upstream_error_mapped_token_not_echoed(tg_admin, upstream):
    upstream.add("POST", SEND, httpx.Response(400, json={"ok": False, "error_code": 400,
                                                         "description": f"Bad Request: chat not found {BOT_TOKEN}"}))
    k = new_key(tg_admin, grants={"telegram.send": None})
    r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "alerts", "chat_id": 111, "text": "x"})
    assert r.status_code == 502
    assert r.json()["error"] == "upstream_error" and r.json()["status"] == 400
    assert BOT_TOKEN not in r.text and "chat not found" in r.text


def test_upstream_timeout_and_connect_error(tg_admin, upstream):
    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)

    upstream.add("POST", SEND, boom)
    k = new_key(tg_admin, grants={"telegram.send": None})
    r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "alerts", "chat_id": 111, "text": "x"})
    assert r.status_code == 504 and r.json() == {"error": "upstream_timeout"}

    def refused(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    upstream.add("POST", SEND, refused)
    r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "alerts", "chat_id": 111, "text": "x"})
    assert r.status_code == 502 and BOT_TOKEN not in r.text


def test_token_encrypted_and_write_only(tg_admin, settings):
    bots = tg_admin.get(f"{ADMIN}/bots").json()["bots"]
    assert "token" not in json.dumps(bots).replace("token_set", "")
    raw = open(settings.database_path, "rb").read()
    conn = sqlite3.connect(settings.database_path)
    tok, allow = conn.execute("SELECT token_enc, allowlist_enc FROM bots").fetchone()
    assert BOT_TOKEN not in tok and "111" not in allow
    assert BOT_TOKEN.encode() not in raw


def test_bad_token_and_validation(tg_admin):
    assert tg_admin.post(f"{ADMIN}/bots", json={"name": "x", "token": "nope"}).status_code == 400
    assert tg_admin.post(f"{ADMIN}/bots", json={"name": "Bad Name", "token": BOT_TOKEN}).status_code == 400
    assert tg_admin.post(f"{ADMIN}/bots", json={"name": "alerts", "token": BOT_TOKEN}).status_code == 409
    k = new_key(tg_admin, grants={"telegram.send": None})
    r = tg_admin.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "alerts", "chat_id": 111, "text": ""})
    assert r.status_code == 400


def test_bot_crud(tg_admin):
    b = tg_admin.patch(f"{ADMIN}/bots/{tg_admin.bot_id}", json={"name": "renamed", "retention_days": 0}).json()
    assert b["name"] == "renamed" and b["retention_days"] == 0
    assert tg_admin.delete(f"{ADMIN}/bots/{tg_admin.bot_id}").status_code == 200
    assert tg_admin.get(f"{ADMIN}/bots").json()["bots"] == []


def test_passphrase_change_reencrypts_bots(tg_admin, make_app, upstream):
    ok_send(upstream)
    k = new_key(tg_admin, grants={"telegram.send": None})
    assert tg_admin.post(f"{ADMIN}/passphrase", json={"current": "correct horse battery staple",
                                                      "new": "a whole new passphrase"}).status_code == 200
    tg_admin.__exit__(None, None, None)
    with TestClient(make_app()) as c:
        c.post(f"{ADMIN}/unlock", json={"passphrase": "a whole new passphrase"})
        r = c.post("/api/v1/telegram/send", headers=bearer(k), json={"bot": "alerts", "chat_id": 111, "text": "x"})
        assert r.status_code == 200
        do_login(c)
        assert [u["id"] for u in c.get(f"{ADMIN}/telegram/users").json()["users"]] == [111, 222, 333]


@pytest.mark.live
def test_live_send(make_client):
    token, chat = os.environ.get("LIVE_TELEGRAM_TOKEN"), os.environ.get("LIVE_TELEGRAM_CHAT_ID")
    if not token or not chat:
        pytest.skip("LIVE_TELEGRAM_TOKEN / LIVE_TELEGRAM_CHAT_ID not set")
    from app.config import Settings  # noqa: F401
    from app.main import create_app
    c = make_client()
    real = TestClient(create_app(c.app.state.gw.settings.with_(database_path=c.app.state.gw.settings.database_path + ".live")))
    with real:
        do_setup(real)
        do_login(real)
        set_users(real, [int(chat)])
        bot = real.post(f"{ADMIN}/bots", json={"name": "live", "token": token, "polling_enabled": False}).json()
        real.put(f"{ADMIN}/bots/{bot['id']}/allowlist", json={"user_ids": [int(chat)]})
        k = new_key(real, grants={"telegram.send": None})
        r = real.post("/api/v1/telegram/send", headers=bearer(k),
                      json={"bot": "live", "chat_id": int(chat), "text": "gateway live test"})
        assert r.status_code == 200, r.text
