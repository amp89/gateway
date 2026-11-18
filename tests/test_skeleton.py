import sqlite3

import pytest

from app import db
from app.config import Settings


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_no_docs_routes(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_wal_and_migrations(settings, client):
    conn = sqlite3.connect(settings.database_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions and versions == sorted(versions) and versions[0] == 1


def test_migrations_idempotent(settings):
    conn = db.connect(settings.database_path)
    first = db.migrate(conn)
    assert first and db.migrate(conn) == []


def test_data_survives_restart(make_app):
    from fastapi.testclient import TestClient

    with TestClient(make_app()) as c:
        db.meta_set(c.app.state.gw.conn, "probe", "kept")
    with TestClient(make_app()) as c:
        assert db.meta_get(c.app.state.gw.conn, "probe") == "kept"


def test_settings_from_env_defaults():
    s = Settings.from_env({})
    assert s.api_prefix == "api" and s.admin_prefix == "admin"
    s = Settings.from_env({"API_HOSTS": "A.example, b.example", "PACKS_DIR": "/x/packs"})
    assert s.api_hosts == ("a.example", "b.example")
    assert s.packs_dir == "/x/packs"


def test_health_checks_kept_out_of_access_log():
    import logging
    from app.main import QuietHealth
    f = QuietHealth()
    rec = lambda path, status, method="GET": logging.LogRecord(  # noqa: E731
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5", method, path, "1.1", status), None)
    assert f.filter(rec("/health", 200)) is False
    assert f.filter(rec("/health", 503)) is True
    assert f.filter(rec("/api/v1/me", 200)) is True
    assert f.filter(rec("/health?x=1", 200)) is True


def test_api_url_from_env():
    prod = Settings.from_env({"ENV": "production", "API_PREFIX": "p1", "API_HOSTS": "GW.example.com, other.example"})
    assert prod.api_url == "https://gw.example.com/p1/v1"
    dev = Settings.from_env({"ENV": "development", "API_HOSTS": "localhost", "GATEWAY_DEV_PORT": "39000"})
    assert dev.api_url == "http://localhost:39000/api/v1"


def test_prod_refuses_dev_passphrase():
    with pytest.raises(RuntimeError):
        Settings(env="production", dev_auto_unlock_passphrase="x").validate()


def test_prefixes_must_differ():
    with pytest.raises(RuntimeError):
        Settings(api_prefix="same", admin_prefix="same").validate()


@pytest.mark.parametrize("bad", ["", "a/b", "has space", "x?y", "a" * 65])
def test_prefix_charset(bad):
    with pytest.raises(RuntimeError, match="API_PREFIX"):
        Settings(api_prefix=bad).validate()


def test_prefixes_used_exactly_as_given():
    s = Settings.from_env({"API_PREFIX": "My-Api_1", "ADMIN_PREFIX": "ctl"})
    s.validate()
    assert (s.api_prefix, s.admin_prefix) == ("My-Api_1", "ctl")
