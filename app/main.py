"""App factory. Everything stateful hangs off one Gateway object, so each test gets a fresh one."""
import asyncio
import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db
from .clock import Clock
from .config import Settings
from .errors import bare_404, err
from .lockout import Lockout
from .access import Access
from .admin_access import admin_access_routes
from .admin_auth import AdminAuth, ApiError, admin_auth_routes
from .retention import Retention, retention_routes
from .seal import SealService, admin_seal_routes, internal_router

log = logging.getLogger("gateway")
UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
          "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# httpx logs full request URLs at INFO; Telegram puts the bot token in the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class Gateway:
    def __init__(self, settings: Settings, clock: Clock, transport: httpx.AsyncBaseTransport | None):
        self.settings = settings
        self.clock = clock
        self.transport = transport
        self.conn = db.connect(settings.database_path)
        db.migrate(self.conn)
        self.http: httpx.AsyncClient | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.unseal_hooks: list = []  # callables run on the event loop after unseal
        self.shutdown_hooks: list = []  # async callables run at shutdown
        self.startup_hooks: list = []  # callables run on the event loop at startup
        self.bots_hooks: list = []  # callables run on the event loop when bot rows change
        self.inbound_hooks: list = []  # async callables(bot_id, stored messages) after a poll batch is stored
        self.setup_hooks: list = []  # callables(dev: bool) run right after first-run setup (vault open)
        self.unlock_lockout = Lockout(clock)
        self.seal = SealService(self)
        self.admin_auth = AdminAuth(self)
        self.access = Access(self)
        self.retention = Retention(self)
        self._trusted = [ipaddress.ip_network(x, strict=False) for x in settings.trusted_proxy_ips]

    @property
    def vault(self):
        return self.seal.vault

    def new_http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, timeout=self.settings.upstream_timeout_s, follow_redirects=False
        )

    def bots_changed(self) -> None:
        """Bot rows changed: pollers (T7) resync. Safe to call from any thread."""
        if self.loop is not None:
            for hook in self.bots_hooks:
                self.loop.call_soon_threadsafe(hook)

    def fire_unseal(self) -> None:
        """Called from any thread after unseal; hooks run on the event loop."""
        if self.loop is None:
            return
        for hook in self.unseal_hooks:
            self.loop.call_soon_threadsafe(hook)

    def audit_event(self, action: str, status: str, resource: str = "", *, key_id=None, key_name="-",
                    duration_ms: int = 0, client_ip: str = "") -> None:
        """resource (bot names, chat IDs, project IDs, ...) is stored encrypted. While sealed only unlock/setup
        attempts are audited; their resource ("admin", "unlock.sh") stays plain, marked by resource_enc = ''."""
        if self.vault.unsealed:
            plain, enc = "", (self.vault.enc(resource) if resource else "")
        else:
            plain, enc = resource, ""
        self.conn.execute(
            "INSERT INTO audit (ts, key_id, key_name, action, resource, resource_enc, status, duration_ms, client_ip)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.clock.now(), key_id, key_name, action, plain, enc, status, duration_ms, client_ip),
        )

    def audit_resource(self, row) -> str:
        return self.vault.dec(row["resource_enc"]) if row["resource_enc"] else row["resource"]

    def backfill_encrypted(self) -> None:
        """Rows from before migration 0008 hold chat IDs / audit resources in plaintext: encrypt them (runs on
        every unseal; a no-op once done). VACUUM afterwards so the old plaintext does not linger in free pages."""
        v = self.vault
        with db.tx(self.conn) as c:
            msgs = c.execute("SELECT id, chat_id FROM messages WHERE chat_id_enc IS NULL").fetchall()
            for r in msgs:
                c.execute("UPDATE messages SET chat_id_enc = ?, chat_id = 0 WHERE id = ?",
                          (v.enc(str(r["chat_id"])), r["id"]))
            aud = c.execute("SELECT id, resource FROM audit WHERE resource_enc IS NULL").fetchall()
            for r in aud:
                c.execute("UPDATE audit SET resource_enc = ?, resource = '' WHERE id = ?",
                          (v.enc(r["resource"]) if r["resource"] else "", r["id"]))
        if msgs or aud:
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # old page copies in the WAL file too
            log.info("encrypted %d message chat IDs and %d audit resources from before the upgrade", len(msgs), len(aud))

    def client_ip(self, request: Request) -> str:
        peer = request.client.host if request.client else ""
        try:
            trusted = any(ipaddress.ip_address(peer) in net for net in self._trusted)
        except ValueError:
            trusted = False
        if trusted:
            cf = request.headers.get("cf-connecting-ip", "").strip()
            if cf:
                return cf
            xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if xff:
                return xff
        return peer


def default_modules() -> list:
    from .services import generic, telegram, telegram_commands, telegram_poll

    return [telegram, telegram_poll, generic, telegram_commands]


def _host(request: Request) -> str:
    host = request.headers.get("host", "").strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host[: host.find("]") + 1]
    return host.rsplit(":", 1)[0] if ":" in host else host


class QuietHealth(logging.Filter):
    """Drop uvicorn access lines for successful GET /health (the container healthcheck, every 10s)."""
    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        return not (isinstance(a, tuple) and len(a) >= 5 and a[1] == "GET" and a[2] == "/health" and a[4] == 200)


def quiet_health_checks() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietHealth) for f in access.filters):
        access.addFilter(QuietHealth())


_LOC_SKIP = {"body", "query", "path", "header", "cookie"}


def validation_detail(exc: RequestValidationError) -> str:
    """Field + reason for each problem. Never the submitted value (it may be a token)."""
    parts = []
    for e in exc.errors()[:5]:
        loc = ".".join(str(x) for x in e.get("loc", ()) if x not in _LOC_SKIP)
        msg = str(e.get("msg", "invalid")).removeprefix("Value error, ")
        if e.get("type") == "string_pattern_mismatch":
            msg = "invalid characters or length"
        elif e.get("type") == "json_invalid":
            msg = "body is not valid JSON"
        last = loc.rsplit(".", 1)[-1]
        parts.append(f"{loc}: {msg}" if loc and not msg.startswith(last + " ") else msg)
    return "; ".join(parts) or "invalid request"


def create_app(
    settings: Settings | None = None,
    clock: Clock | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    modules: list | None = None,
) -> FastAPI:
    """modules: service modules with setup(gw, client_router, admin_router); default = all built-ins."""
    if modules is None:
        modules = default_modules()
    settings = settings or Settings.from_env()
    settings.validate()
    quiet_health_checks()
    gw = Gateway(settings, clock or Clock(), transport)
    api_root = f"/{settings.api_prefix}"
    admin_root = f"/{settings.admin_prefix}"
    admin_api = f"{admin_root}/api"
    seal_exempt = {f"{admin_api}/state", f"{admin_api}/setup", f"{admin_api}/unlock"}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gw.loop = asyncio.get_running_loop()
        gw.http = gw.new_http()
        gw.seal.write_unlock_token()
        gw.seal.write_setup_code()
        gw.retention.start()
        for hook in gw.startup_hooks:
            hook()
        await asyncio.to_thread(gw.seal.auto_unlock_dev)
        try:
            yield
        finally:
            for hook in gw.shutdown_hooks:
                await hook()
            await gw.retention.stop()
            await gw.http.aclose()
            gw.conn.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.gw = gw

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(request, exc):
        if exc.status_code in (404, 405):
            return bare_404()
        return err(exc.status_code, "error")

    @app.exception_handler(ApiError)
    async def api_error(request, exc: ApiError):
        return err(exc.status, exc.code, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def validation_exc(request, exc):
        return err(400, "bad_request", detail=validation_detail(exc))

    @app.middleware("http")
    async def gatekeeper(request: Request, call_next):
        """Routing guard (SPEC 5.5) then seal check (4.3). Anything unexpected: bare 404."""
        path = request.url.path
        if path in ("/health", "/_unlock"):
            return await call_next(request)
        if path == api_root or path.startswith(api_root + "/"):
            if _host(request) not in settings.api_hosts:
                return bare_404()
            if gw.seal.state() != "unsealed":
                return err(503, "sealed")
            return await call_next(request)
        if path == admin_root or path.startswith(admin_root + "/"):
            if _host(request) not in settings.admin_hosts:
                return bare_404()
            if path.startswith(admin_api + "/") and path not in seal_exempt and gw.seal.state() != "unsealed":
                return err(503, "sealed")
            resp = await call_next(request)
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Cache-Control", "no-store")
            return resp
        return bare_404()

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok", "state": gw.seal.state()}

    app.include_router(internal_router(gw))

    admin = APIRouter(prefix=admin_api)

    @admin.get("/state")
    def state(request: Request):
        cur = gw.admin_auth.current(request)
        body = {"state": gw.seal.state(), "logged_in": cur is not None}
        if cur is not None:
            body["csrf"] = cur[1].csrf
        return body

    admin_seal_routes(admin, gw)
    admin_auth_routes(admin, gw)
    admin_access_routes(admin, gw)
    retention_routes(admin, gw)

    client = APIRouter(prefix=f"{api_root}/v1")

    @client.get("/me")
    def me(request: Request):
        caller = gw.access.authenticate(request)
        gw.access.take(caller.key_id)
        known = gw.access.registry.all()
        return {"key": caller.name, "actions": sorted(a for a in caller.grants if a in known)}

    for module in modules:
        module.setup(gw, client, admin)

    app.include_router(admin)
    app.include_router(client)

    # Admin UI: the built SPA (ui/dist, copied to UI_DIR in the prod image). Missing in dev: Vite serves it.
    ui = Path(settings.ui_dir)
    if (ui / "index.html").is_file():
        if (ui / "assets").is_dir():
            app.mount(f"{admin_root}/assets", StaticFiles(directory=ui / "assets"), name="ui-assets")

        @app.get(admin_root, include_in_schema=False)
        def ui_redirect():
            return RedirectResponse(f"{admin_root}/", status_code=308)

        @app.get(f"{admin_root}/", include_in_schema=False)
        def ui_index():
            return FileResponse(ui / "index.html", headers={"Content-Security-Policy": UI_CSP})

    return app
