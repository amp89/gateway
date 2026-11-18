import base64
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import crypto
from tests.conftest import ADMIN, ADMIN_PASSWORD, PASSPHRASE, do_login, do_setup, setup_code


def audit_rows(settings, action):
    conn = sqlite3.connect(settings.database_path)
    return [r[0] for r in conn.execute("SELECT status FROM audit WHERE action = ? ORDER BY id", (action,))]


def test_uninitialized_state_blocks_everything(client):
    assert client.get(f"{ADMIN}/state").json()["state"] == "uninitialized"
    assert client.get("/health").json() == {"status": "ok", "state": "uninitialized"}
    assert client.get("/api/v1/me").status_code == 503
    assert client.get(f"{ADMIN}/keys").json() == {"error": "sealed"}
    assert client.post(f"{ADMIN}/unlock", json={"passphrase": "x"}).status_code == 409


def test_setup_unseals_once(client):
    do_setup(client)
    assert client.get(f"{ADMIN}/state").json()["state"] == "unsealed"
    r = client.post(f"{ADMIN}/setup", json={"passphrase": PASSPHRASE * 2, "admin_password": "another-password"})
    assert r.status_code == 409


def test_setup_rejects_short_secrets(client):
    r = client.post(f"{ADMIN}/setup", json={"passphrase": "short", "admin_password": "admin-password-123",
                                            "setup_code": setup_code(client)})
    assert r.status_code == 400
    assert client.get(f"{ADMIN}/state").json()["state"] == "uninitialized"


def test_setup_needs_the_setup_code(client, settings):
    path = Path(settings.setup_code_path)
    code = path.read_text()
    assert code == setup_code(client) and len(code) >= 20 and path.stat().st_mode & 0o777 == 0o600
    body = {"passphrase": PASSPHRASE, "admin_password": ADMIN_PASSWORD}
    for bad in ({}, {"setup_code": ""}, {"setup_code": "wrong"}, {"setup_code": code + "x"}):
        r = client.post(f"{ADMIN}/setup", json=body | bad)
        assert r.status_code == 403 and r.json() == {"error": "wrong_setup_code"}
    assert client.get(f"{ADMIN}/state").json()["state"] == "uninitialized"
    assert client.post(f"{ADMIN}/setup", json=body | {"setup_code": f" {code}\n"}).status_code == 200
    assert not path.exists() and setup_code(client) == ""  # one-time: gone after setup
    assert client.post(f"{ADMIN}/setup", json=body | {"setup_code": code}).status_code == 409
    assert audit_rows(settings, "seal.setup") == ["denied"] * 4 + ["ok"]


def test_setup_code_new_per_boot_and_none_once_set_up(make_app, settings):
    path = Path(settings.setup_code_path)
    with TestClient(make_app()):
        first = path.read_text()
    with TestClient(make_app()) as c:
        assert path.read_text() != first
        do_setup(c)
    with TestClient(make_app()) as c:  # sealed, not uninitialized: no code, web setup refused
        assert not path.exists() and setup_code(c) == ""
        r = c.post(f"{ADMIN}/setup", json={"passphrase": PASSPHRASE, "admin_password": ADMIN_PASSWORD, "setup_code": first})
        assert r.status_code == 409


def test_setup_refused_when_code_file_unwritable(make_app, tmp_path):
    with TestClient(make_app(setup_code_path=str(tmp_path / "missing-dir" / "setup.code"))) as c:
        assert setup_code(c) == ""
        r = c.post(f"{ADMIN}/setup", json={"passphrase": PASSPHRASE, "admin_password": ADMIN_PASSWORD, "setup_code": ""})
        assert r.status_code == 403


def test_restart_reseals_and_unlock(make_app, settings):
    with TestClient(make_app()) as c:
        do_setup(c)
    with TestClient(make_app()) as c:
        assert c.get(f"{ADMIN}/state").json()["state"] == "sealed"
        assert c.get("/api/v1/me").status_code == 503
        r = c.post(f"{ADMIN}/unlock", json={"passphrase": "wrong passphrase!!"})
        assert r.status_code == 403 and r.json()["error"] == "wrong_passphrase"
        r = c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE})
        assert r.status_code == 200 and r.json() == {"state": "unsealed"}
    assert audit_rows(settings, "seal.unlock") == ["denied", "ok"]


def test_unlock_lockout_backoff(make_app, clock):
    with TestClient(make_app()) as c:
        do_setup(c)
    with TestClient(make_app()) as c:
        for _ in range(5):
            assert c.post(f"{ADMIN}/unlock", json={"passphrase": "nope nope nope"}).status_code == 403
        r = c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE})
        assert r.status_code == 429 and r.json()["retry_after"] == 30
        clock.advance(31)
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": "nope nope nope"}).status_code == 403
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE}).json()["retry_after"] == 60
        clock.advance(61)
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE}).status_code == 200


def test_key_never_on_disk(unsealed, settings):
    f = unsealed.app.state.gw.vault._f
    raw = f._signing_key + f._encryption_key
    blob = b"".join(p.read_bytes() for p in Path(settings.database_path).parent.glob("gateway.sqlite3*"))
    for needle in (raw, base64.urlsafe_b64encode(raw), PASSPHRASE.encode()):
        assert needle not in blob


def test_internal_unlock_requires_token(make_app, settings):
    with TestClient(make_app()) as c:
        do_setup(c)
    with TestClient(make_app()) as c:
        assert c.post("/_unlock", json={"passphrase": PASSPHRASE}).status_code == 404
        r = c.post("/_unlock", json={"passphrase": PASSPHRASE}, headers={"X-Unlock-Token": "bad"})
        assert r.status_code == 404 and r.content == b""
        token = Path(settings.unlock_token_path).read_text()
        assert Path(settings.unlock_token_path).stat().st_mode & 0o777 == 0o600
        r = c.post("/_unlock", json={"passphrase": PASSPHRASE}, headers={"X-Unlock-Token": token})
        assert r.json() == {"state": "unsealed"}


def test_unlock_token_rotates_per_boot(make_app, settings):
    with TestClient(make_app()):
        first = Path(settings.unlock_token_path).read_text()
    with TestClient(make_app()):
        assert Path(settings.unlock_token_path).read_text() != first


def test_dev_auto_unlock_survives_reload(make_app):
    dev = dict(dev_auto_unlock_passphrase="dev-only-passphrase-not-for-production")
    for _ in range(2):  # first boot initializes, second (a reload) unlocks
        with TestClient(make_app(**dev)) as c:
            assert c.get(f"{ADMIN}/state").json()["state"] == "unsealed"


def test_dev_short_admin_password_and_reset_on_boot(make_app):
    dev = dict(dev_auto_unlock_passphrase="dev-only-passphrase-not-for-production")
    with TestClient(make_app(**dev)) as c:  # default dev password "asdf", below the normal minimum
        do_login(c, "asdf")
        assert c.post(f"{ADMIN}/password", json={"current": "asdf", "new": "something-longer"}).status_code == 200
    with TestClient(make_app(**dev, dev_admin_password="qwer")) as c:  # next dev boot re-applies DEV_ADMIN_PASSWORD
        assert c.post(f"{ADMIN}/login", json={"password": "something-longer"}).status_code == 403
        do_login(c, "qwer")


def test_short_admin_password_refused_outside_dev(client):
    r = client.post(f"{ADMIN}/setup", json={"passphrase": PASSPHRASE, "admin_password": "asdf",
                                            "setup_code": setup_code(client)})
    assert r.status_code == 400


def test_dev_auto_unlock_off_in_production(settings):
    with pytest.raises(RuntimeError):
        settings.with_(env="production", dev_auto_unlock_passphrase="x").validate()


def test_change_passphrase(unsealed, make_app):
    gw = unsealed.app.state.gw
    with pytest.raises(PermissionError):
        gw.seal.change_passphrase("wrong wrong wrong", "new passphrase 12345")
    gw.seal.change_passphrase(PASSPHRASE, "new passphrase 12345")
    token = gw.vault.enc("still readable")
    assert gw.vault.dec(token) == "still readable"
    unsealed.__exit__(None, None, None)
    with TestClient(make_app()) as c:
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE}).status_code == 403
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": "new passphrase 12345"}).status_code == 200


def test_old_plaintext_rows_encrypted_on_unlock(make_app, settings):
    """Rows written before migration 0008: plaintext chat_id / audit resource, *_enc NULL."""
    with TestClient(make_app()) as c:
        do_setup(c)
        do_login(c)
        bot = c.post(f"{ADMIN}/bots", json={"name": "alerts", "token": "123456789:AAH-test_token_value_abcdefghijklmnop",
                                            "polling_enabled": False}).json()["id"]
        gw = c.app.state.gw
        now = gw.clock.now()  # recent: the cleanup at boot must not delete them
        gw.conn.execute("INSERT INTO messages (bot_id, update_id, tg_message_id, chat_id, sent_at, received_at,"
                        " sender_enc, body_enc) VALUES (?, 1, 10, 765432109, ?, ?, ?, ?)",
                        (bot, now, now, gw.vault.enc_json({"id": 765432109}), gw.vault.enc("hi")))
        gw.conn.execute("INSERT INTO audit (ts, key_name, action, resource, status) VALUES (?, 'k', 'telegram.send',"
                        " 'bots=alerts chat_ids=765432109', '200')", (now,))
        gw.conn.execute("INSERT INTO audit (ts, key_name, action, resource, status) VALUES (?, 'k', 'x', '', '200')", (now,))
    conn = sqlite3.connect(settings.database_path)
    assert conn.execute("SELECT count(*) FROM audit WHERE resource LIKE '%765432109%'").fetchone()[0] == 1
    with TestClient(make_app()) as c:
        assert c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE}).status_code == 200
        assert conn.execute("SELECT chat_id, chat_id_enc IS NOT NULL FROM messages").fetchall() == [(0, 1)]
        assert conn.execute("SELECT count(*) FROM audit WHERE resource != '' OR resource_enc IS NULL").fetchone()[0] == 0
        do_login(c)
        msgs = c.get(f"{ADMIN}/messages").json()["messages"]
        assert [m["chat_id"] for m in msgs] == [765432109]
        rows = c.get(f"{ADMIN}/audit", params={"action": "telegram.send"}).json()["rows"]
        assert [r["resource"] for r in rows] == ["bots=alerts chat_ids=765432109"] and "resource_enc" not in rows[0]
    blob = b"".join(p.read_bytes() for p in Path(settings.database_path).parent.glob("gateway.sqlite3*"))
    assert b"765432109" not in blob  # VACUUM: no stale copy in free pages


def test_change_passphrase_keeps_ids_readable(make_app):
    with TestClient(make_app()) as c:
        do_setup(c)
        do_login(c)
        gw = c.app.state.gw
        gw.audit_event("telegram.send", "200", "bots=alerts chat_ids=111")
        r = c.post(f"{ADMIN}/passphrase", json={"current": PASSPHRASE, "new": "new passphrase 12345"})
        assert r.status_code == 200
        rows = c.get(f"{ADMIN}/audit", params={"action": "telegram.send"}).json()["rows"]
        assert [r["resource"] for r in rows] == ["bots=alerts chat_ids=111"]
        assert c.get(f"{ADMIN}/audit", params={"action": "seal.setup"}).json()["rows"][0]["resource"] == "admin"


def test_sealed_audit_rows_stay_plain_and_readable(make_app, settings):
    with TestClient(make_app()) as c:
        do_setup(c)
    with TestClient(make_app()) as c:
        c.post(f"{ADMIN}/unlock", json={"passphrase": "wrong passphrase!!"})
        conn = sqlite3.connect(settings.database_path)
        assert conn.execute("SELECT resource, resource_enc FROM audit WHERE status = 'denied'").fetchall() == [("admin", "")]
        c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE})
        do_login(c)
        rows = c.get(f"{ADMIN}/audit", params={"action": "seal.unlock"}).json()["rows"]
        assert [(r["status"], r["resource"]) for r in rows] == [("ok", "admin"), ("denied", "admin")]


def test_vault_refuses_when_sealed():
    v = crypto.Vault()
    with pytest.raises(crypto.SealedError):
        v.enc("x")
