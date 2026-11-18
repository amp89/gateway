"""Per-test isolation: own SQLite file, own app, fake clock, mock upstream. Safe under pytest -n 4."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clock import FakeClock
from app.config import Settings
from app.main import create_app


class Upstream:
    """Routes (method, url-prefix) to handlers; records every request. No network."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, object]] = []
        self.requests: list[httpx.Request] = []

    def add(self, method: str, url_prefix: str, handler) -> None:
        """handler: httpx.Response, dict (JSON 200), or callable(request) -> Response."""
        self.routes.insert(0, (method.upper(), url_prefix, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for method, prefix, handler in self.routes:
            if request.method == method and url.startswith(prefix):
                if callable(handler):
                    return handler(request)
                if isinstance(handler, httpx.Response):
                    return handler
                return httpx.Response(200, json=handler)
        return httpx.Response(599, json={"error": "no mock route", "url": url})

    def bodies(self) -> list:
        return [json.loads(r.content) if r.content else None for r in self.requests]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "gateway.sqlite3"),
        unlock_token_path=str(tmp_path / "unlock.token"),
        setup_code_path=str(tmp_path / "setup.code"),
        ui_dir=str(tmp_path / "ui"),
        api_hosts=("testserver",),
        admin_hosts=("testserver",),
        packs_dir=str(tmp_path / "packs"),
        kdf_n=2**10,
    )


@pytest.fixture
def extra_modules() -> list:
    """Override in a test module to add test-only service modules."""
    return []


@pytest.fixture
def make_app(settings, clock, upstream, extra_modules):
    from app.main import default_modules

    def make(**overrides):
        s = settings.with_(**overrides) if overrides else settings
        return create_app(s, clock=clock, transport=httpx.MockTransport(upstream),
                          modules=default_modules() + list(extra_modules))

    return make


@pytest.fixture
def make_client(make_app):
    clients = []

    def make(**overrides) -> TestClient:
        c = TestClient(make_app(**overrides))
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


PASSPHRASE = "correct horse battery staple"
ADMIN_PASSWORD = "admin-password-123"
ADMIN = "/admin/api"


def setup_code(c: TestClient) -> str:
    """The one-time code this boot wrote (what the start script prints)."""
    return c.app.state.gw.seal.setup_code or ""


def do_setup(c: TestClient, prefix: str = ADMIN) -> None:
    r = c.post(f"{prefix}/setup", json={"passphrase": PASSPHRASE, "admin_password": ADMIN_PASSWORD,
                                        "setup_code": setup_code(c)})
    assert r.status_code == 200, r.text


@pytest.fixture
def unsealed(client) -> TestClient:
    do_setup(client)
    return client


def do_login(c: TestClient, password: str = ADMIN_PASSWORD) -> str:
    r = c.post(f"{ADMIN}/login", json={"password": password})
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf"]
    c.headers["X-CSRF-Token"] = csrf
    return csrf


@pytest.fixture
def admin(unsealed) -> TestClient:
    """Unsealed, logged in, CSRF header set."""
    do_login(unsealed)
    return unsealed


def new_key(c: TestClient, name: str = "client", grants: dict | None = None) -> str:
    """Create a gateway key via the admin API; grants = {action: scope or None}. Returns the secret."""
    r = c.post(f"{ADMIN}/keys", json={"name": name})
    assert r.status_code == 200, r.text
    key_id, secret = r.json()["id"], r.json()["key"]
    for action, scope in (grants or {}).items():
        r = c.put(f"{ADMIN}/permissions/{key_id}/{action}", json={"scope": scope})
        assert r.status_code == 200, r.text
    return secret


def set_users(c: TestClient, ids) -> None:
    """Replace the global Telegram user list via the admin API."""
    r = c.put(f"{ADMIN}/telegram/users", json={"users": [{"id": i, "label": f"user {i}"} for i in ids]})
    assert r.status_code == 200, r.text


def bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


BOT_TOKEN = "123456789:AAH-test_token_value_abcdefghijklmnop"
TG = "https://api.telegram.org"


@pytest.fixture
def tg_admin(make_client):
    """Unsealed + logged in, global users {111, 222, 333}, bot 'alerts' allowing 111 and 222."""
    c = make_client()
    do_setup(c)
    do_login(c)
    set_users(c, [111, 222, 333])
    r = c.post(f"{ADMIN}/bots", json={"name": "alerts", "token": BOT_TOKEN, "polling_enabled": False})
    assert r.status_code == 200, r.text
    c.bot_id = r.json()["id"]
    assert c.put(f"{ADMIN}/bots/{c.bot_id}/allowlist", json={"user_ids": [111, 222]}).status_code == 200
    return c
