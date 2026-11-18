"""Gateway keys, action registry, permissions, rate limit and per-call audit (SPEC 5, 7.1)."""
import json
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Request

from . import crypto
from .admin_auth import ApiError

KEY_PREFIX = "gw_"


@dataclass(frozen=True)
class Action:
    name: str
    scope_fields: tuple[str, ...] = ()
    description: str = ""
    source: str = "builtin"


class Registry:
    """Built-in actions (code modules) plus loaders for data-defined ones (generic services)."""

    def __init__(self) -> None:
        self.builtin: dict[str, Action] = {}
        self.loaders: list = []  # callables -> list[Action]

    def register(self, action: Action) -> None:
        self.builtin[action.name] = action

    def all(self) -> dict[str, Action]:
        out = dict(self.builtin)
        for load in self.loaders:
            for a in load():
                out.setdefault(a.name, a)
        return out

    def get(self, name: str) -> Action | None:
        return self.all().get(name)


@dataclass
class Bucket:
    tokens: float
    at: float


@dataclass
class Caller:
    key_id: int
    name: str
    grants: dict[str, dict | None] = field(default_factory=dict)


def new_secret() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


class Access:
    def __init__(self, gw) -> None:
        self.gw = gw
        self.registry = Registry()
        self.buckets: dict[int, Bucket] = {}

    # -- keys ----------------------------------------------------------------------
    def create_key(self, name: str) -> tuple[int, str]:
        secret = new_secret()
        cur = self.gw.conn.execute(
            "INSERT INTO gateway_keys (name, key_hash, key_hint, created_at) VALUES (?, ?, ?, ?)",
            (name, crypto.sha256_hex(secret), secret[-4:], self.gw.clock.now()),
        )
        return cur.lastrowid, secret

    def authenticate(self, request: Request) -> Caller:
        h = request.headers.get("authorization", "")
        scheme, _, token = h.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token.startswith(KEY_PREFIX):
            raise ApiError(401, "unauthorized")
        row = self.gw.conn.execute(
            "SELECT id, name, status FROM gateway_keys WHERE key_hash = ?", (crypto.sha256_hex(token),)
        ).fetchone()
        return self._caller(row)

    def caller_by_id(self, key_id: int) -> Caller:
        """The caller for a key the gateway acts as itself (bot commands); same checks as a bearer key."""
        row = self.gw.conn.execute("SELECT id, name, status FROM gateway_keys WHERE id = ?", (key_id,)).fetchone()
        return self._caller(row)

    def _caller(self, row) -> Caller:
        if row is None or row["status"] != "active":
            raise ApiError(401, "unauthorized")
        self.gw.conn.execute("UPDATE gateway_keys SET last_used_at = ? WHERE id = ?", (self.gw.clock.now(), row["id"]))
        grants = {
            r["action"]: (json.loads(r["scope_json"]) if r["scope_json"] else None)
            for r in self.gw.conn.execute("SELECT action, scope_json FROM permissions WHERE key_id = ?", (row["id"],))
        }
        return Caller(row["id"], row["name"], grants)

    # -- rate limit (token bucket per key) ----------------------------------------------
    def take(self, key_id: int) -> None:
        rate = self.gw.settings.api_rate_per_min
        now = self.gw.clock.monotonic()
        b = self.buckets.get(key_id)
        if b is None:
            b = self.buckets[key_id] = Bucket(float(rate), now)
        b.tokens = min(float(rate), b.tokens + (now - b.at) * rate / 60.0)
        b.at = now
        if b.tokens < 1:
            raise ApiError(429, "rate_limited", retry_after=max(1, int((1 - b.tokens) * 60 / rate + 0.999)))
        b.tokens -= 1

    # -- permission ------------------------------------------------------------------
    def check(self, caller: Caller, action: str, resources: dict) -> None:
        """Default deny. A scope lists allowed values per field; every scoped field must be present and allowed."""
        if self.registry.get(action) is None or action not in caller.grants:
            raise ApiError(403, "forbidden")
        scope = caller.grants[action]
        if not scope:
            return
        for fieldname, allowed in scope.items():
            values = resources.get(fieldname)
            if values is None:
                raise ApiError(403, "forbidden")
            if not isinstance(values, (list, tuple, set)):
                values = [values]
            allowed_s = {str(a) for a in allowed}
            if not values or any(str(v) not in allowed_s for v in values):
                raise ApiError(403, "forbidden")

    def validate_scope(self, action: str, scope) -> dict | None:
        a = self.registry.get(action)
        if a is None:
            raise ApiError(400, "bad_request", detail="unknown action")
        if scope is None:
            return None
        if not isinstance(scope, dict):
            raise ApiError(400, "bad_request", detail="scope must be an object or null")
        out = {}
        for k, v in scope.items():
            if k not in a.scope_fields:
                raise ApiError(400, "bad_request", detail=f"scope field {k!r} not valid for {action}")
            if not isinstance(v, list) or not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in v):
                raise ApiError(400, "bad_request", detail="scope values must be a list of strings or ints")
            out[k] = [str(x) for x in v]
        return out or None


def _resource_str(resources: dict) -> str:
    parts = []
    for k, v in resources.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            v = ",".join(str(x) for x in v)
        parts.append(f"{k}={v}")
    return " ".join(parts)[:500]


async def run_action(gw, request: Request, action: str, resources: dict, handler):
    """Authenticate -> rate limit -> permission -> handler, with one audit row whatever happens."""
    return await run_checked(gw, lambda: gw.access.authenticate(request), action, resources, handler,
                             gw.client_ip(request))


async def run_checked(gw, get_caller, action: str, resources: dict, handler, client_ip: str, audit_resource=None):
    """get_caller() -> Caller, then rate limit -> permission -> handler, with one audit row whatever happens.
    audit_resource: text for the audit row instead of the resources (bot commands add bot + command)."""
    started = time.perf_counter()
    caller: Caller | None = None
    status = "500"
    try:
        caller = get_caller()
        gw.access.take(caller.key_id)
        gw.access.check(caller, action, resources)
        resp = await handler(caller)
        status = str(getattr(resp, "status_code", 200))
        return resp
    except ApiError as e:
        status = str(e.status)
        raise
    finally:
        gw.audit_event(
            action, status, audit_resource if audit_resource is not None else _resource_str(resources),
            key_id=caller.key_id if caller else None, key_name=caller.name if caller else "-",
            duration_ms=int((time.perf_counter() - started) * 1000), client_ip=client_ip,
        )
