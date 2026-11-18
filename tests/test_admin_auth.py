from fastapi.testclient import TestClient

from tests.conftest import ADMIN, ADMIN_PASSWORD, PASSPHRASE, do_login, do_setup


def test_admin_api_requires_session(unsealed):
    r = unsealed.post(f"{ADMIN}/password", json={"current": ADMIN_PASSWORD, "new": "x" * 12})
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert unsealed.post(f"{ADMIN}/logout").status_code == 401


def test_login_sets_strict_httponly_cookie(unsealed):
    r = unsealed.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/admin" in cookie
    assert "secure" not in cookie  # dev; prod adds Secure
    st = unsealed.get(f"{ADMIN}/state").json()
    assert st["logged_in"] and st["csrf"] == r.json()["csrf"]


def test_secure_cookie_in_production(make_client):
    c = make_client(env="production")
    do_setup(c)
    r = c.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD})
    assert "secure" in r.headers["set-cookie"].lower()


def test_csrf_required_on_writes(admin):
    good = admin.headers["X-CSRF-Token"]
    del admin.headers["X-CSRF-Token"]
    r = admin.post(f"{ADMIN}/password", json={"current": ADMIN_PASSWORD, "new": "new-password-123"})
    assert r.status_code == 403 and r.json() == {"error": "csrf"}
    r = admin.post(f"{ADMIN}/password", json={"current": ADMIN_PASSWORD, "new": "new-password-123"},
                   headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    admin.headers["X-CSRF-Token"] = good
    r = admin.post(f"{ADMIN}/password", json={"current": ADMIN_PASSWORD, "new": "new-password-123"})
    assert r.status_code == 200


def test_login_brute_force_throttled(unsealed, clock):
    for _ in range(5):
        assert unsealed.post(f"{ADMIN}/login", json={"password": "guess-guess"}).status_code == 403
    r = unsealed.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD})
    assert r.status_code == 429 and r.json()["retry_after"] == 30
    clock.advance(30)
    assert unsealed.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD}).status_code == 200


def test_logout_ends_session(admin):
    assert admin.post(f"{ADMIN}/logout").status_code == 200
    assert admin.get(f"{ADMIN}/state").json()["logged_in"] is False
    assert admin.post(f"{ADMIN}/logout").status_code == 401


def test_restart_ends_session(make_app):
    with TestClient(make_app()) as c:
        do_setup(c)
        do_login(c)
        cookies = dict(c.cookies)
    with TestClient(make_app()) as c:
        c.post(f"{ADMIN}/unlock", json={"passphrase": PASSPHRASE})
        c.cookies.update(cookies)
        assert c.get(f"{ADMIN}/state").json()["logged_in"] is False


def test_session_idle_timeout(admin, clock):
    clock.advance(61 * 60)
    assert admin.get(f"{ADMIN}/state").json()["logged_in"] is False


def test_password_change(admin):
    r = admin.post(f"{ADMIN}/password", json={"current": "wrong-password", "new": "new-password-123"})
    assert r.status_code == 403
    r = admin.post(f"{ADMIN}/password", json={"current": ADMIN_PASSWORD, "new": "short"})
    assert r.status_code == 400
    assert admin.post(f"{ADMIN}/password", json={"current": ADMIN_PASSWORD, "new": "new-password-123"}).status_code == 200
    admin.post(f"{ADMIN}/logout")
    assert admin.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD}).status_code == 403
    do_login(admin, "new-password-123")


def test_passphrase_change_endpoint(admin):
    r = admin.post(f"{ADMIN}/passphrase", json={"current": PASSPHRASE, "new": "brand new passphrase"})
    assert r.status_code == 200


def test_login_blocked_while_sealed(make_app):
    with TestClient(make_app()) as c:
        do_setup(c)
    with TestClient(make_app()) as c:
        assert c.post(f"{ADMIN}/login", json={"password": ADMIN_PASSWORD}).status_code == 503
