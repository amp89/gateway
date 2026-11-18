"""Sealed boot (SPEC 4.3, 5.2): first-run setup, unlock, passphrase change, internal unlock token."""
import hmac
import logging
import os
import secrets

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import crypto, db
from .errors import bare_404, err

log = logging.getLogger("gateway.seal")

MIN_PASSPHRASE = 12
MIN_ADMIN_PASSWORD = 10


class SealService:
    def __init__(self, gw) -> None:
        self.gw = gw
        self.vault = crypto.Vault()
        self.unlock_token: str | None = None
        self.setup_code: str | None = None  # one-time code for web setup; None = web setup refused

    # -- state -----------------------------------------------------------------
    def state(self) -> str:
        if self.vault.unsealed:
            return "unsealed"
        if db.meta_get(self.gw.conn, "kdf_salt") is None:
            return "uninitialized"
        return "sealed"

    def _opened(self, f) -> None:
        self.vault.unseal(f)
        try:
            self.gw.backfill_encrypted()
        except Exception:  # noqa: BLE001 - never block unlock; retried on the next unseal
            log.exception("encrypting old plaintext rows failed")
        self.gw.fire_unseal()

    # -- setup / unlock ----------------------------------------------------------
    def setup(self, passphrase: str, admin_password: str, dev: bool = False) -> None:
        if self.state() != "uninitialized":
            raise PermissionError("already initialized")
        if len(passphrase) < MIN_PASSPHRASE or (len(admin_password) < MIN_ADMIN_PASSWORD and not dev) or not admin_password:
            raise ValueError("too short")
        n = self.gw.settings.kdf_n
        f, meta = crypto.new_kdf(passphrase, n)
        with db.tx(self.gw.conn) as c:
            if db.meta_get(c, "kdf_salt") is not None:
                raise PermissionError("already initialized")
            for k, v in meta.items():
                db.meta_set(c, k, v)
            db.meta_set(c, "admin_password_hash", crypto.hash_password(admin_password, n))
        self.drop_setup_code()
        self._opened(f)
        for hook in self.gw.setup_hooks:  # e.g. bundled packs; setup itself already succeeded
            try:
                hook(dev)
            except Exception:  # noqa: BLE001
                log.exception("post-setup hook failed")

    def try_passphrase(self, passphrase: str):
        c = self.gw.conn
        return crypto.check_passphrase(
            passphrase, db.meta_get(c, "kdf_salt"), db.meta_get(c, "kdf_params"), db.meta_get(c, "verify_token")
        )

    def unlock(self, passphrase: str, via: str, client_ip: str = "") -> tuple[int, dict]:
        """Returns (http status, body). Rate-limited; every attempt is audited."""
        state = self.state()
        if state == "uninitialized":
            return 409, {"error": "uninitialized"}
        if state == "unsealed":
            return 200, {"state": "unsealed"}
        lock = self.gw.unlock_lockout
        wait = lock.retry_after()
        if wait:
            self.gw.audit_event("seal.unlock", "locked", via, client_ip=client_ip)
            return 429, {"error": "locked", "retry_after": wait}
        f = self.try_passphrase(passphrase)
        if f is None:
            lock.fail()
            self.gw.audit_event("seal.unlock", "denied", via, client_ip=client_ip)
            log.warning("unlock failed (%d consecutive)", lock.failures)
            return 403, {"error": "wrong_passphrase", "retry_after": lock.retry_after()}
        lock.success()
        self._opened(f)
        self.gw.audit_event("seal.unlock", "ok", via, client_ip=client_ip)
        log.info("unsealed")
        return 200, {"state": "unsealed"}

    def auto_unlock_dev(self) -> None:
        s = self.gw.settings
        if not s.dev_auto_unlock_passphrase or s.production:
            return
        if self.state() == "uninitialized":
            self.setup(s.dev_auto_unlock_passphrase, s.dev_admin_password, dev=True)
            log.warning("DEV: initialized with the dev-only passphrase")
        elif self.state() == "sealed":
            f = self.try_passphrase(s.dev_auto_unlock_passphrase)
            if f is None:
                log.error("DEV: auto-unlock passphrase does not match this database")
                return
            # dev only: the admin password always follows DEV_ADMIN_PASSWORD, also on existing dev volumes
            db.meta_set(self.gw.conn, "admin_password_hash", crypto.hash_password(s.dev_admin_password, s.kdf_n))
            self._opened(f)
            log.warning("DEV: auto-unlocked with the dev-only passphrase; admin password = DEV_ADMIN_PASSWORD")

    # -- passphrase change ---------------------------------------------------------
    def change_passphrase(self, current: str, new: str) -> None:
        if len(new) < MIN_PASSPHRASE:
            raise ValueError("too short")
        old = self.try_passphrase(current)
        if old is None:
            raise PermissionError("wrong passphrase")
        f, meta = crypto.new_kdf(new, self.gw.settings.kdf_n)
        conn = self.gw.conn
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with db.tx(conn) as c:
            for table, pk, cols in crypto.ENCRYPTED_COLUMNS:
                if table not in tables:
                    continue
                rows = c.execute(f"SELECT {pk}, {', '.join(cols)} FROM {table}").fetchall()
                for row in rows:
                    # None / '' (e.g. audit rows written while sealed) carry no ciphertext: kept as they are
                    vals = [row[col] if not row[col] else f.encrypt(old.decrypt(row[col].encode())).decode()
                            for col in cols]
                    sets = ", ".join(f"{col} = ?" for col in cols)
                    c.execute(f"UPDATE {table} SET {sets} WHERE {pk} = ?", (*vals, row[pk]))
            for key in crypto.ENCRYPTED_META:
                v = db.meta_get(c, key)
                if v is not None:
                    db.meta_set(c, key, f.encrypt(old.decrypt(v.encode())).decode())
            for k, v in meta.items():
                db.meta_set(c, k, v)
        self.vault.unseal(f)

    # -- private files in /run/gateway (tmpfs, readable only inside the container) ----------------
    @staticmethod
    def _write_private(path: str, value: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        os.chmod(path, 0o600)

    # -- internal unlock token (used by unlock.sh via exec) ------------------------------
    def write_unlock_token(self) -> None:
        path = self.gw.settings.unlock_token_path
        token = secrets.token_urlsafe(32)
        try:
            self._write_private(path, token)
        except OSError as e:
            log.warning("internal unlock disabled: cannot write %s (%s)", path, e.strerror)
            return
        self.unlock_token = token

    def check_unlock_token(self, presented: str | None) -> bool:
        return bool(self.unlock_token and presented and hmac.compare_digest(presented, self.unlock_token))

    # -- one-time setup code (web setup proves shell access to the host) ----------------------
    def write_setup_code(self) -> None:
        """Only while uninitialized: a fresh code per boot, printed by the start scripts."""
        self.drop_setup_code()
        if self.state() != "uninitialized":
            return
        path = self.gw.settings.setup_code_path
        code = secrets.token_urlsafe(16)
        try:
            self._write_private(path, code)
        except OSError as e:
            log.warning("web setup disabled: cannot write %s (%s)", path, e.strerror)
            return
        self.setup_code = code
        log.warning("uninitialized: web setup needs the setup code in %s", path)

    def check_setup_code(self, presented: str | None) -> bool:
        return bool(self.setup_code and presented and hmac.compare_digest(presented.strip(), self.setup_code))

    def drop_setup_code(self) -> None:
        self.setup_code = None
        try:
            os.unlink(self.gw.settings.setup_code_path)
        except OSError:
            pass


class SetupBody(BaseModel):
    passphrase: str
    admin_password: str
    setup_code: str = ""


class PassphraseBody(BaseModel):
    passphrase: str


def internal_router(gw) -> APIRouter:
    r = APIRouter()

    @r.post("/_unlock", include_in_schema=False)
    def internal_unlock(request: Request, body: PassphraseBody):
        if not gw.seal.check_unlock_token(request.headers.get("x-unlock-token")):
            return bare_404()
        status, payload = gw.seal.unlock(body.passphrase, "unlock.sh")
        return err(status, payload["error"], **{k: v for k, v in payload.items() if k != "error"}) if "error" in payload else payload

    return r


def admin_seal_routes(r: APIRouter, gw) -> None:
    @r.post("/setup")
    def setup(request: Request, body: SetupBody):
        if gw.seal.state() != "uninitialized":
            return err(409, "already_initialized")
        if not gw.seal.check_setup_code(body.setup_code):
            gw.audit_event("seal.setup", "denied", "admin", client_ip=gw.client_ip(request))
            return err(403, "wrong_setup_code")
        try:
            gw.seal.setup(body.passphrase, body.admin_password)
        except PermissionError:
            return err(409, "already_initialized")
        except ValueError:
            return err(400, "bad_request", detail=f"passphrase >= {MIN_PASSPHRASE}, password >= {MIN_ADMIN_PASSWORD} chars")
        gw.audit_event("seal.setup", "ok", "admin", client_ip=gw.client_ip(request))
        return {"state": gw.seal.state()}

    @r.post("/unlock")
    def unlock(request: Request, body: PassphraseBody):
        status, payload = gw.seal.unlock(body.passphrase, "admin", gw.client_ip(request))
        if "error" in payload:
            return err(status, payload["error"], **{k: v for k, v in payload.items() if k != "error"})
        return payload
