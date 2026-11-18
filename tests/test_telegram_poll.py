import asyncio
import sqlite3

import httpx
from fastapi.testclient import TestClient

from tests.conftest import ADMIN, BOT_TOKEN, PASSPHRASE, TG, bearer, do_login, do_setup, new_key, set_users

UPD = f"{TG}/bot{BOT_TOKEN}/getUpdates"


def update(uid, sender, text="hello", chat=None):
    return {"update_id": uid, "message": {"message_id": uid * 10, "date": 1_700_000_000 + uid,
                                          "chat": {"id": chat or sender}, "text": text,
                                          "from": {"id": sender, "username": f"u{sender}", "first_name": "Ann"}}}


def poll(c, bot_id):
    return c.portal.call(c.app.state.gw.pollers.poll_once, bot_id)


def enable_polling_off_loop(c, bot_id):
    """Turn polling on in the DB without waking the background task (tests drive poll_once directly)."""
    c.app.state.gw.conn.execute("UPDATE bots SET polling_enabled = 1 WHERE id = ?", (bot_id,))


def test_allowed_message_readable_other_dropped(tg_admin, upstream, settings):
    enable_polling_off_loop(tg_admin, tg_admin.bot_id)
    upstream.add("POST", UPD, {"ok": True, "result": [update(1, 111, "secret words"), update(2, 999, "spam text"),
                                                      update(3, 333, "not on this bot")]})
    assert poll(tg_admin, tg_admin.bot_id) is True
    k = new_key(tg_admin, grants={"telegram.read": {"bots": ["alerts"]}})
    r = tg_admin.get("/api/v1/telegram/messages", params={"bot": "alerts"}, headers=bearer(k)).json()
    assert [m["text"] for m in r["messages"]] == ["secret words"]
    assert r["messages"][0]["sender"]["username"] == "u111" and r["next_since"] == r["messages"][0]["id"]
    conn = sqlite3.connect(settings.database_path)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert r["messages"][0]["chat_id"] == 111
    assert conn.execute("SELECT chat_id FROM messages").fetchone()[0] == 0  # real one only in chat_id_enc
    assert conn.execute("SELECT chat_id_enc FROM messages").fetchone()[0].startswith("gAAAAA")  # Fernet token
    assert conn.execute("SELECT poll_offset FROM bots").fetchone()[0] == 4
    raw = b"".join(open(settings.database_path + sfx, "rb").read() for sfx in ("", "-wal")
                   if __import__("os").path.exists(settings.database_path + sfx))
    for plain in (b"secret words", b"spam text", b"u111", b"Ann"):
        assert plain not in raw


def test_read_scope_and_paging(tg_admin, upstream):
    enable_polling_off_loop(tg_admin, tg_admin.bot_id)
    upstream.add("POST", UPD, {"ok": True, "result": [update(i, 111, f"m{i}") for i in range(1, 6)]})
    poll(tg_admin, tg_admin.bot_id)
    k = new_key(tg_admin, grants={"telegram.read": None})
    first = tg_admin.get("/api/v1/telegram/messages", params={"bot": "alerts", "limit": 2}, headers=bearer(k)).json()
    assert [m["text"] for m in first["messages"]] == ["m1", "m2"]
    rest = tg_admin.get("/api/v1/telegram/messages", params={"bot": "alerts", "since": first["next_since"]},
                        headers=bearer(k)).json()
    assert [m["text"] for m in rest["messages"]] == ["m3", "m4", "m5"]
    k2 = new_key(tg_admin, "scoped", grants={"telegram.read": {"bots": ["other"]}})
    assert tg_admin.get("/api/v1/telegram/messages", params={"bot": "alerts"}, headers=bearer(k2)).status_code == 403
    assert tg_admin.get("/api/v1/telegram/messages", params={"bot": "ghost"}, headers=bearer(k)).status_code == 403


def test_offset_sent_and_no_dupes_across_restart(make_app, upstream):
    with TestClient(make_app()) as c:
        do_setup(c)
        do_login(c)
        set_users(c, [111])
        bot_id = c.post(f"{ADMIN}/bots", json={"name": "a", "token": BOT_TOKEN, "polling_enabled": False}).json()["id"]
        c.put(f"{ADMIN}/bots/{bot_id}/allowlist", json={"user_ids": [111]})
        enable_polling_off_loop(c, bot_id)
        upstream.add("POST", UPD, {"ok": True, "result": [update(7, 111), update(8, 111)]})
        poll(c, bot_id)
        c.app.state.gw.conn.execute("UPDATE bots SET polling_enabled = 0")
    upstream.requests.clear()
    with TestClient(make_app()) as c:
        c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE})
        enable_polling_off_loop(c, bot_id)
        # Telegram re-delivers 8 (as if the ack was lost) plus a new 9
        upstream.add("POST", UPD, {"ok": True, "result": [update(8, 111), update(9, 111)]})
        poll(c, bot_id)
        assert upstream.bodies()[0]["offset"] == 9
        ids = [r[0] for r in c.app.state.gw.conn.execute("SELECT update_id FROM messages ORDER BY update_id")]
        assert ids == [7, 8, 9]


def test_401_stops_polling_and_records_error(tg_admin, upstream):
    enable_polling_off_loop(tg_admin, tg_admin.bot_id)
    upstream.add("POST", UPD, httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}))
    gw = tg_admin.app.state.gw
    tg_admin.portal.call(gw.pollers.run, tg_admin.bot_id)  # returns instead of looping
    bot = tg_admin.get(f"{ADMIN}/bots").json()["bots"][0]
    assert bot["poll_error"] == "unauthorized"


def test_backoff_on_errors(tg_admin, upstream):
    enable_polling_off_loop(tg_admin, tg_admin.bot_id)
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(502, text="bad gateway")
        tg_admin.app.state.gw.conn.execute("UPDATE bots SET polling_enabled = 0")  # stop the loop after success
        return httpx.Response(200, json={"ok": True, "result": []})

    upstream.add("POST", UPD, flaky)
    gw = tg_admin.app.state.gw
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    gw.pollers.sleep = fake_sleep
    tg_admin.portal.call(gw.pollers.run, tg_admin.bot_id)
    assert slept == [1.0, 2.0, 4.0]


def test_pollers_start_on_unlock_and_follow_bot_changes(tg_admin):
    gw = tg_admin.app.state.gw
    assert gw.pollers.tasks == {}
    tg_admin.patch(f"{ADMIN}/bots/{tg_admin.bot_id}", json={"polling_enabled": True})
    tg_admin.portal.call(asyncio.sleep, 0.05)
    assert list(gw.pollers.tasks) == [tg_admin.bot_id]
    tg_admin.patch(f"{ADMIN}/bots/{tg_admin.bot_id}", json={"polling_enabled": False})
    tg_admin.portal.call(asyncio.sleep, 0.05)
    assert gw.pollers.tasks == {}


def test_no_polling_while_sealed(make_app):
    with TestClient(make_app()) as c:
        do_setup(c)
        do_login(c)
        c.post(f"{ADMIN}/bots", json={"name": "a", "token": BOT_TOKEN, "polling_enabled": False})
        c.app.state.gw.conn.execute("UPDATE bots SET polling_enabled = 1")
    with TestClient(make_app()) as c:
        gw = c.app.state.gw
        c.portal.call(asyncio.sleep, 0.05)
        assert gw.pollers.tasks == {}
        c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE})
        c.portal.call(asyncio.sleep, 0.05)
        assert len(gw.pollers.tasks) == 1
        gw.conn.execute("UPDATE bots SET polling_enabled = 0")


def test_admin_messages_list_delete(tg_admin, upstream):
    enable_polling_off_loop(tg_admin, tg_admin.bot_id)
    upstream.add("POST", UPD, {"ok": True, "result": [update(1, 111, "one"), update(2, 222, "two")]})
    poll(tg_admin, tg_admin.bot_id)
    tg_admin.app.state.gw.conn.execute("UPDATE bots SET polling_enabled = 0")
    msgs = tg_admin.get(f"{ADMIN}/messages").json()["messages"]
    assert [m["text"] for m in msgs] == ["two", "one"]
    assert tg_admin.delete(f"{ADMIN}/messages/{msgs[0]['id']}").status_code == 200
    assert tg_admin.delete(f"{ADMIN}/messages/{msgs[0]['id']}").status_code == 404
    assert len(tg_admin.get(f"{ADMIN}/messages").json()["messages"]) == 1
