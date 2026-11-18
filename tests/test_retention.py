from fastapi.testclient import TestClient

from tests.conftest import ADMIN, do_login, do_setup
from tests.test_telegram_poll import UPD, enable_polling_off_loop, poll, update


def test_cleanup_messages_and_audit(tg_admin, upstream, clock):
    gw = tg_admin.app.state.gw
    tg_admin.patch(f"{ADMIN}/bots/{tg_admin.bot_id}", json={"retention_days": 2, "polling_enabled": False})
    enable_polling_off_loop(tg_admin, tg_admin.bot_id)
    upstream.add("POST", UPD, {"ok": True, "result": [update(1, 111)]})
    poll(tg_admin, tg_admin.bot_id)
    clock.advance(86400)
    assert gw.retention.run()[0] == 0
    clock.advance(86400 + 1)
    assert gw.retention.run()[0] == 1
    # audit: default 90 days; 0 keeps forever (re-login: the fake clock outran the session)
    do_login(tg_admin)
    r = tg_admin.put(f"{ADMIN}/settings", json={"audit_retention_days": 0})
    assert r.status_code == 200, r.text
    clock.advance(1000 * 86400)
    assert gw.retention.run()[1] == 0
    do_login(tg_admin)
    tg_admin.put(f"{ADMIN}/settings", json={"audit_retention_days": 1})
    clock.advance(2 * 86400)
    assert gw.retention.run()[1] > 0


def test_message_retention_zero_keeps_forever(tg_admin, upstream, clock):
    gw = tg_admin.app.state.gw
    tg_admin.patch(f"{ADMIN}/bots/{tg_admin.bot_id}", json={"retention_days": 0})
    enable_polling_off_loop(tg_admin, tg_admin.bot_id)
    upstream.add("POST", UPD, {"ok": True, "result": [update(1, 111)]})
    poll(tg_admin, tg_admin.bot_id)
    gw.conn.execute("UPDATE bots SET polling_enabled = 0")
    clock.advance(10_000 * 86400)
    assert gw.retention.run()[0] == 0


def test_cleanup_now_endpoint_and_status(admin, clock):
    gw = admin.app.state.gw
    s = admin.get(f"{ADMIN}/settings").json()
    assert s["cleanup"]["every_s"] == 3600 and "telegram_allowed_user_ids" not in s
    admin.put(f"{ADMIN}/settings", json={"audit_retention_days": 1})
    old = gw.conn.execute("SELECT count(*) FROM audit").fetchone()[0]
    assert old > 0
    clock.advance(2 * 86400)
    do_login(admin)  # fake clock outran the session
    r = admin.post(f"{ADMIN}/cleanup")
    assert r.status_code == 200
    c = r.json()
    assert c["last_run_at"] == clock.now() and c["last_audit_deleted"] >= old and c["last_messages_deleted"] == 0
    assert admin.get(f"{ADMIN}/settings").json()["cleanup"] == c
    acts = [r[0] for r in gw.conn.execute("SELECT action FROM audit")]
    assert "admin.cleanup" in acts


def test_cleanup_now_admin_only_and_csrf(admin):
    del admin.headers["X-CSRF-Token"]
    assert admin.post(f"{ADMIN}/cleanup").status_code == 403
    admin.cookies.clear()
    assert admin.post(f"{ADMIN}/cleanup").status_code == 401


def test_cleanup_runs_while_sealed(make_app, clock):
    with TestClient(make_app()) as c:
        do_setup(c)
    with TestClient(make_app(audit_retention_days=1)) as c:
        gw = c.app.state.gw
        assert gw.seal.state() == "sealed"
        clock.advance(2 * 86400)
        assert gw.retention.run()[1] > 0
