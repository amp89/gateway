"""Admin login (SPEC 8.1): one account, in-memory sessions, CSRF header, login lockout."""
import hmac
import secrets
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from . import crypto, db
from .errors import err
from .lockout import Lockout
from .seal import MIN_ADMIN_PASSWORD

COOKIE = "gw_session"


class ApiError(Exception):
    def __init__(self, http_status: int, code: str, **extra) -> None:
        self.status, self.code, self.extra = http_status, code, extra


@dataclass
class Session:
    csrf: str
    last_seen: float


class AdminAuth:
    def __init__(self, gw) -> None:
        self.gw = gw
        self.sessions: dict[str, Session] = {}
        self.lockout = Lockout(gw.clock)

    def _idle_limit(self) -> float:
        return self.gw.settings.session_idle_min * 60

    def current(self, request: Request) -> tuple[str, Session] | None:
        sid = request.cookies.get(COOKIE)
        if not sid:
            return None
        s = self.sessions.get(sid)
        if s is None:
            return None
        now = self.gw.clock.monotonic()
        if now - s.last_seen > self._idle_limit():
            self.sessions.pop(sid, None)
            return None
        s.last_seen = now
        return sid, s

    def login(self, password: str) -> tuple[str, Session] | None:
        stored = db.meta_get(self.gw.conn, "admin_password_hash")
        if not stored or not crypto.verify_password(password, stored):
            return None
        sid = secrets.token_urlsafe(32)
        s = Session(csrf=secrets.token_urlsafe(32), last_seen=self.gw.clock.monotonic())
        self.sessions[sid] = s
        return sid, s

    def set_cookie(self, response: Response, sid: str) -> None:
        response.set_cookie(
            COOKIE, sid, httponly=True, samesite="strict", secure=self.gw.settings.production,
            path=f"/{self.gw.settings.admin_prefix}",
        )


def require_admin(request: Request) -> Session:
    """Session required; state-changing methods also need X-CSRF-Token."""
    auth: AdminAuth = request.app.state.gw.admin_auth
    cur = auth.current(request)
    if cur is None:
        raise ApiError(401, "unauthorized")
    _, s = cur
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        presented = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(presented, s.csrf):
            raise ApiError(403, "csrf")
    return s


class LoginBody(BaseModel):
    password: str


class ChangeBody(BaseModel):
    current: str
    new: str


def admin_auth_routes(r: APIRouter, gw) -> None:
    auth: AdminAuth = gw.admin_auth

    @r.post("/login")
    def login(request: Request, body: LoginBody):
        ip = gw.client_ip(request)
        wait = auth.lockout.retry_after()
        if wait:
            gw.audit_event("admin.login", "locked", client_ip=ip)
            return err(429, "locked", retry_after=wait)
        res = auth.login(body.password)
        if res is None:
            auth.lockout.fail()
            gw.audit_event("admin.login", "denied", client_ip=ip)
            return err(403, "wrong_password", retry_after=auth.lockout.retry_after())
        auth.lockout.success()
        sid, s = res
        gw.audit_event("admin.login", "ok", client_ip=ip)
        resp = Response(content=f'{{"csrf":"{s.csrf}"}}', media_type="application/json")
        auth.set_cookie(resp, sid)
        return resp

    @r.post("/logout")
    def logout(request: Request, _: Session = Depends(require_admin)):
        sid = request.cookies.get(COOKIE)
        auth.sessions.pop(sid, None)
        resp = Response(content='{"ok":true}', media_type="application/json")
        resp.delete_cookie(COOKIE, path=f"/{gw.settings.admin_prefix}")
        return resp

    @r.post("/password")
    def change_password(request: Request, body: ChangeBody, _: Session = Depends(require_admin)):
        stored = db.meta_get(gw.conn, "admin_password_hash")
        if not crypto.verify_password(body.current, stored or ""):
            return err(403, "wrong_password")
        if len(body.new) < MIN_ADMIN_PASSWORD:
            return err(400, "bad_request", detail=f"password >= {MIN_ADMIN_PASSWORD} chars")
        db.meta_set(gw.conn, "admin_password_hash", crypto.hash_password(body.new, gw.settings.kdf_n))
        # end every other session
        keep = request.cookies.get(COOKIE)
        for sid in [s for s in auth.sessions if s != keep]:
            auth.sessions.pop(sid, None)
        gw.audit_event("admin.password", "ok", client_ip=gw.client_ip(request))
        return {"ok": True}

    @r.post("/passphrase")
    def change_passphrase(request: Request, body: ChangeBody, _: Session = Depends(require_admin)):
        try:
            gw.seal.change_passphrase(body.current, body.new)
        except PermissionError:
            return err(403, "wrong_passphrase")
        except ValueError:
            return err(400, "bad_request", detail="passphrase >= 12 chars")
        gw.audit_event("seal.passphrase", "ok", client_ip=gw.client_ip(request))
        return {"ok": True}
