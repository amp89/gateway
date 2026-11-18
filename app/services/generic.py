"""Generic services (SPEC 10): upstream HTTP APIs defined as data -- a service plus declared operations.

A client calls POST /{API_PREFIX}/v1/call/{service}/{operation}; the gateway fills the declared path, checks
declared query/body fields, injects the credential, and relays. No undeclared method, path or field gets through.
"""
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .. import db
from ..access import Action, read_scope, run_action
from ..admin_auth import ApiError, require_admin
from ..errors import err

NAME = r"^[a-z0-9][a-z0-9_]{0,31}$"
OP_NAME = r"^[a-z0-9][a-z0-9_]{0,63}$"
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]{0,63})\}")
PATH_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
QUERY_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
RESERVED = {"telegram", "admin", "gateway"}
# Headers the relay or HTTP itself controls; a credential header must not replace them.
RESERVED_HEADERS = {"host", "content-length", "content-type", "transfer-encoding", "connection", "accept"}
# Text that ends up in generated client code (comments, docstrings): one line, no control characters.
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
DETAIL_MAX = 2048
PACK_FILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEMO_KEY = "dev-demo"
DEMO_ACTIONS = ("projects_list", "labels_list", "tasks_list", "tasks_filter", "task_get", "tasks_add")
log = logging.getLogger("gateway.generic")


def verr(e: ValidationError) -> str:
    """Field + message per error, never the submitted value."""
    parts = []
    for x in e.errors()[:5]:
        loc = ".".join(str(v) for v in x.get("loc", ()))
        msg = str(x.get("msg", "invalid")).removeprefix("Value error, ")
        if x.get("type") == "string_pattern_mismatch":
            msg = "invalid characters or length"
        parts.append(f"{loc}: {msg}" if loc and not msg.startswith(loc.rsplit(".", 1)[-1] + " ") else msg)
    return "; ".join(parts)[:300] or "invalid"


# -- validation of definitions -----------------------------------------------------------------
def check_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("base_url must be https://host[/path] with no credentials, query or fragment")
    return url


def check_path_template(t: str) -> str:
    if not t.startswith("/") or any(x in t for x in ("?", "#", "..", "//", "\\")):
        raise ValueError("path_template must start with / and contain no ?, #, .., // or backslash")
    rest = PLACEHOLDER.sub("", t)
    if "{" in rest or "}" in rest or not re.fullmatch(r"[A-Za-z0-9_./~-]*", rest):
        raise ValueError("path_template: only {name} placeholders and plain path characters allowed")
    names = PLACEHOLDER.findall(t)
    if len(names) != len(set(names)):
        raise ValueError("path_template: duplicate placeholder")
    return t


class ServiceIn(BaseModel):
    name: str = Field(pattern=NAME)
    base_url: str
    auth_style: Literal["bearer", "header", "query"] = "bearer"
    auth_name: str = ""
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _url(cls, v):
        return check_base_url(v)

    @model_validator(mode="after")
    def _auth(self):
        if self.name in RESERVED:
            raise ValueError("reserved service name")
        if self.auth_style == "bearer":
            self.auth_name = ""
        elif not (HEADER_NAME if self.auth_style == "header" else QUERY_NAME).match(self.auth_name):
            raise ValueError("auth_name required for header/query auth (header: letters, digits, -)")
        elif self.auth_style == "header" and self.auth_name.lower() in RESERVED_HEADERS:
            raise ValueError(f"auth_name: {self.auth_name!r} is a reserved header")
        return self


class ServicePatch(BaseModel):
    name: str | None = Field(default=None, pattern=NAME)
    base_url: str | None = None
    auth_style: Literal["bearer", "header", "query"] | None = None
    auth_name: str | None = None
    enabled: bool | None = None


class OperationIn(BaseModel):
    name: str = Field(pattern=OP_NAME)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path_template: str
    query_fields: list[str] | None = Field(default_factory=list)
    body_fields: list[str] | None = Field(default_factory=list)
    scope_fields: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)  # must be present (not null/"") in every call
    description: str = Field(default="", max_length=200)
    enabled: bool = True

    @field_validator("path_template")
    @classmethod
    def _path(cls, v):
        return check_path_template(v)

    @field_validator("description")
    @classmethod
    def _desc(cls, v):
        if CONTROL.search(v):
            raise ValueError("description: no line breaks or control characters")
        return v

    @model_validator(mode="after")
    def _fields(self):
        for lst in (self.query_fields or [], self.body_fields or [], self.scope_fields, self.required_fields):
            for f in lst:
                if not FIELD.match(f):
                    raise ValueError(f"bad field name {f!r}")
        if self.method == "GET" and self.body_fields:
            raise ValueError("GET operations take no body")
        sources = [set(PLACEHOLDER.findall(self.path_template)), set(self.query_fields or []),
                   set(self.body_fields or [])]
        known = set().union(*sources)
        for f in self.scope_fields:
            if f not in known:
                raise ValueError(f"scope field {f!r} must be a path placeholder or a declared query/body field")
            if sum(f in s for s in sources) > 1:
                raise ValueError(f"scope field {f!r} is declared in more than one of path, query, body")
        for f in self.required_fields:
            if f not in known:
                raise ValueError(f"required field {f!r} must be a path placeholder or a declared query/body field")
        if len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError("required_fields: duplicate field")
        return self


class OperationPatch(BaseModel):
    name: str | None = Field(default=None, pattern=OP_NAME)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    path_template: str | None = None
    query_fields: list[str] | None = None
    body_fields: list[str] | None = None
    scope_fields: list[str] | None = None
    required_fields: list[str] | None = None
    description: str | None = None
    enabled: bool | None = None
    clear_query_fields: bool = False  # set query_fields to null (= any)
    clear_body_fields: bool = False


class Pack(BaseModel):
    service: ServiceIn
    operations: list[OperationIn]
    self_hosted: bool = False  # base_url is per install: asked on import, skipped by first-setup auto-import


class ImportOpts(BaseModel):
    """Overrides when adding a pack: lets one pack back several services (todoist, todoist2, ...)."""
    name: str | None = Field(default=None, pattern=NAME)
    base_url: str | None = None


class CredentialBody(BaseModel):
    credential: str = Field(min_length=1, max_length=4096)


class CallBody(BaseModel):
    path: dict[str, str | int] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None


# -- storage helpers ---------------------------------------------------------------------------
def op_def(row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "method": row["method"], "path_template": row["path_template"],
        "query_fields": json.loads(row["query_fields_json"]) if row["query_fields_json"] is not None else None,
        "body_fields": json.loads(row["body_fields_json"]) if row["body_fields_json"] is not None else None,
        "scope_fields": json.loads(row["scope_fields_json"]),
        "required_fields": json.loads(row["required_fields_json"]) if row["required_fields_json"] else [],
        "description": row["description"],
        "enabled": bool(row["enabled"]),
    }


def svc_def(row, n_ops: int | None = None) -> dict:
    out = {"id": row["id"], "name": row["name"], "base_url": row["base_url"], "auth_style": row["auth_style"],
           "auth_name": row["auth_name"], "enabled": bool(row["enabled"]),
           "credential_set": row["credential_enc"] is not None, "updated_at": row["updated_at"]}
    if n_ops is not None:
        out["operations"] = n_ops
    return out


def insert_op(c, service_id: int, op: OperationIn) -> int:
    return c.execute(
        "INSERT INTO operations (service_id, name, method, path_template, query_fields_json, body_fields_json,"
        " scope_fields_json, required_fields_json, description, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (service_id, op.name, op.method, op.path_template,
         None if op.query_fields is None else json.dumps(op.query_fields),
         None if op.body_fields is None else json.dumps(op.body_fields),
         json.dumps(op.scope_fields), json.dumps(op.required_fields) if op.required_fields else None,
         op.description, int(op.enabled)),
    ).lastrowid


def _scalar(v) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _redact(text: str, secret: str) -> str:
    if secret:
        for form in (secret, quote(secret, safe=""), quote_plus(secret)):
            text = text.replace(form, "***")
    return text


def _redact_json(v, secret: str):
    """_redact on every string (keys too) of parsed JSON: an upstream may echo the credential back."""
    if isinstance(v, str):
        return _redact(v, secret)
    if isinstance(v, dict):
        return {_redact(k, secret): _redact_json(x, secret) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact_json(x, secret) for x in v]
    return v


# -- call checks (shared by the relay and the bot-command editor, so a saved command can't fail them later) --
class CallError(ApiError):
    """A call that doesn't fit its operation. The detail names fields only, never values: safe to show."""

    def __init__(self, detail: str):
        super().__init__(400, "bad_request", detail=detail)


def _names(xs) -> str:
    return ", ".join(sorted(xs)) or "none"


def _missing(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def check_call(op: dict, call: CallBody) -> str:
    """Every rule the relay applies before contacting the upstream. Returns the filled path."""
    # path
    names = PLACEHOLDER.findall(op["path_template"])
    if set(call.path) != set(names):
        raise CallError(f"path params must be exactly {sorted(names)}")
    path = op["path_template"]
    for n in names:
        v = str(call.path[n])
        if not PATH_VALUE.match(v) or v in (".", ".."):
            raise CallError(f"invalid path param {n!r}")
        path = path.replace("{" + n + "}", v)
    # query
    if op["query_fields"] is not None:
        extra = set(call.query) - set(op["query_fields"])
        if extra:
            raise CallError(f"undeclared query fields {sorted(extra)}; allowed: {_names(op['query_fields'])}")
    for k, v in call.query.items():
        if not (_scalar(v) or (isinstance(v, list) and all(_scalar(x) for x in v))):
            raise CallError(f"query field {k!r} must be scalar or list")
    # body
    if call.body is not None:
        if op["method"] == "GET":
            raise CallError("GET takes no body")
        if op["body_fields"] is not None:
            if not isinstance(call.body, dict):
                raise CallError("body must be an object")
            extra = set(call.body) - set(op["body_fields"])
            if extra:
                raise CallError(f"undeclared body fields {sorted(extra)}; allowed: {_names(op['body_fields'])}")
    # required
    given = {**call.path, **call.query, **(call.body if isinstance(call.body, dict) else {})}
    missing = [f for f in op.get("required_fields", []) if _missing(given.get(f))]
    if missing:
        raise CallError(f"missing required field{'s' if len(missing) > 1 else ''} {', '.join(map(repr, missing))}")
    return path


def scope_resources(op: dict, call: CallBody) -> dict:
    """The values of the operation's scope fields in a call, as the permission check sees them.
    A scope field is checked wherever the caller put it: path, query and body are all relayed, so taking
    only the first copy would let a second, unchecked copy reach the upstream."""
    resources: dict = {}
    src = [call.path, call.query, call.body if isinstance(call.body, dict) else {}]
    for f in op["scope_fields"]:
        found = [s[f] for s in src if f in s]
        if len(found) == 1:
            resources[f] = found[0]
        elif found:
            resources[f] = [x for v in found for x in (v if isinstance(v, list) else [v])]
    return resources


# -- the relay -----------------------------------------------------------------------------------
async def relay(gw, svc, op: dict, call: CallBody):
    path = check_call(op, call)
    if svc["credential_enc"] is None:
        raise ApiError(502, "upstream_error", detail="service credential not set")
    cred = gw.vault.dec(svc["credential_enc"])

    base = svc["base_url"]
    url = base + path
    bu, uu = urlsplit(base), urlsplit(url)
    if uu.scheme != "https" or uu.netloc != bu.netloc or not uu.path.startswith(bu.path):
        raise ApiError(400, "bad_request", detail="path escapes the service base URL")

    headers = {"Accept": "application/json"}
    params = {k: v for k, v in call.query.items() if v is not None}
    if svc["auth_style"] == "bearer":
        headers["Authorization"] = f"Bearer {cred}"
    elif svc["auth_style"] == "header":
        headers[svc["auth_name"]] = cred
    else:
        params[svc["auth_name"]] = cred

    cap = gw.settings.upstream_max_bytes
    try:
        kw = {"json": call.body} if call.body is not None else {}
        async with gw.http.stream(op["method"], url, params=params, headers=headers, **kw) as r:
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > cap:
                    raise ApiError(502, "upstream_error", detail="response too large")
                chunks.append(chunk)
            raw = b"".join(chunks)
            status, ctype = r.status_code, r.headers.get("content-type", "")
    except httpx.TimeoutException:
        raise ApiError(504, "upstream_timeout") from None
    except httpx.HTTPError:
        raise ApiError(502, "upstream_error", detail="connection failed") from None

    text = raw.decode("utf-8", "replace")
    if 200 <= status < 300:
        if not raw:
            body = None
        elif "json" in ctype:
            try:
                body = _redact_json(json.loads(raw), cred)
            except ValueError:
                body = _redact(text, cred)
        else:
            body = _redact(text, cred)
        return {"status": status, "body": body}
    raise ApiError(502, "upstream_error", status=status, detail=_redact(text, cred)[:DETAIL_MAX])


def prepare_call(gw, service: str, operation: str, body: CallBody):
    """Look up an enabled service + operation; returns (scope resources, handler(caller)) for run_action /
    run_checked. An unknown action is refused by the permission check before the handler runs."""
    svc = gw.conn.execute("SELECT * FROM services WHERE name = ? AND enabled = 1", (service,)).fetchone()
    op_row = None
    if svc is not None:
        op_row = gw.conn.execute("SELECT * FROM operations WHERE service_id = ? AND name = ? AND enabled = 1",
                                 (svc["id"], operation)).fetchone()
    op = op_def(op_row) if op_row is not None else None
    resources = scope_resources(op, body) if op is not None else {}

    async def handler(caller):
        if op is None:  # unreachable: the permission check already refused the unknown action
            raise ApiError(403, "forbidden")
        return await relay(gw, svc, op, body)

    return resources, handler


def import_pack_data(gw, pack: Pack, name: str | None = None, base_url: str | None = None,
                     source: str | None = None) -> int:
    """Create a new service from a pack. Never touches an existing service: a taken name is 409 (delete the
    old one first or pick another name). Returns the service id."""
    names = [o.name for o in pack.operations]
    if len(names) != len(set(names)):
        raise ApiError(400, "bad_request", detail="duplicate operation names")
    try:
        s = ServiceIn(**(pack.service.model_dump() | {k: v for k, v in (("name", name), ("base_url", base_url)) if v}))
    except ValidationError as e:
        raise ApiError(400, "bad_request", detail=verr(e)) from None
    try:
        with db.tx(gw.conn) as c:
            sid = c.execute(
                "INSERT INTO services (name, base_url, auth_style, auth_name, enabled, updated_at, source_pack)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (s.name, s.base_url, s.auth_style, s.auth_name, int(s.enabled), gw.clock.now(), source)).lastrowid
            for o in pack.operations:
                insert_op(c, sid, o)
    except sqlite3.IntegrityError:
        raise ApiError(409, "name_taken", detail=f"service {s.name!r} exists; delete it first or pick another name") from None
    gw.audit_event("admin.service.import", "ok", f"service={s.name} operations={len(names)}"
                   + (f" pack={source}" if source else ""))
    return sid


def free_name(conn, base: str) -> str:
    """base, else base2, base3, ... (first name not in use)."""
    taken = {r["name"] for r in conn.execute("SELECT name FROM services")}
    if base not in taken:
        return base
    n = 2
    while f"{base[:30]}{n}" in taken:
        n += 1
    return f"{base[:30]}{n}"


def bundled_packs(gw) -> dict[str, Pack]:
    """Valid *.json packs in PACKS_DIR, by file stem. Broken files are logged and skipped."""
    d = Path(gw.settings.packs_dir)
    out: dict[str, Pack] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        if not PACK_FILE.match(f.stem):
            continue
        try:
            out[f.stem] = Pack.model_validate_json(f.read_text())
        except (OSError, ValueError) as e:
            log.warning("bundled pack %s skipped: %s", f.name, str(e).splitlines()[0])
    return out


def create_demo_key(gw) -> None:
    """DEV only: a key with read access + tasks_add on the bundled Todoist service. Secret logged once."""
    if gw.settings.production:
        return
    if gw.conn.execute("SELECT 1 FROM services WHERE name = 'todoist'").fetchone() is None:
        return
    if gw.conn.execute("SELECT 1 FROM gateway_keys WHERE name = ?", (DEMO_KEY,)).fetchone() is not None:
        return
    key_id, secret = gw.access.create_key(DEMO_KEY)
    for a in DEMO_ACTIONS:
        gw.conn.execute("INSERT INTO permissions (key_id, action, scope_json, created_at) VALUES (?, ?, NULL, ?)",
                        (key_id, f"todoist.{a}", gw.clock.now()))
    gw.audit_event("admin.key.create", "ok", f"key={DEMO_KEY} (dev)")
    log.warning("DEV: demo key %s = %s (todoist read + tasks_add; shown once)", DEMO_KEY, secret)


def setup(gw, client: APIRouter, admin: APIRouter) -> None:
    conn = gw.conn
    auth = [Depends(require_admin)]

    def load_actions() -> list[Action]:
        rows = conn.execute(
            "SELECT s.name AS svc, o.name, o.method, o.path_template, o.scope_fields_json, o.description"
            " FROM operations o JOIN services s ON s.id = o.service_id WHERE s.enabled = 1 AND o.enabled = 1"
        ).fetchall()
        return [Action(f"{r['svc']}.{r['name']}", tuple(json.loads(r["scope_fields_json"])),
                       r["description"] or f"{r['method']} {r['path_template']}", "service") for r in rows]

    gw.access.registry.loaders.append(load_actions)

    # -- client -------------------------------------------------------------------------------
    @client.post("/call/{service}/{operation}")
    async def call(request: Request, service: str, operation: str, body: CallBody):
        resources, handler = prepare_call(gw, service, operation, body)
        return await run_action(gw, request, f"{service}.{operation}", resources, handler)

    # -- admin: services ------------------------------------------------------------------------
    def svc_or_404(service_id: int):
        row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found")
        return row

    def rename_actions(c, old_prefix: str, new_prefix: str) -> None:
        for table in ("permissions", "commands"):  # bot commands follow the rename like grants do
            c.execute(f"UPDATE {table} SET action = ? || substr(action, ?) WHERE action LIKE ? ESCAPE '\\'",
                      (new_prefix, len(old_prefix) + 1, old_prefix.replace("_", "\\_") + "%"))

    @admin.get("/services", dependencies=auth)
    def list_services():
        rows = conn.execute("SELECT s.*, (SELECT count(*) FROM operations o WHERE o.service_id = s.id) AS n"
                            " FROM services s ORDER BY name").fetchall()
        return {"services": [svc_def(r, r["n"]) for r in rows]}

    @admin.post("/services", dependencies=auth)
    def create_service(body: ServiceIn):
        try:
            sid = conn.execute(
                "INSERT INTO services (name, base_url, auth_style, auth_name, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (body.name, body.base_url, body.auth_style, body.auth_name, int(body.enabled), gw.clock.now()),
            ).lastrowid
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        gw.audit_event("admin.service.create", "ok", f"service={body.name}")
        return svc_def(svc_or_404(sid), 0)

    @admin.patch("/services/{service_id}", dependencies=auth)
    def patch_service(service_id: int, body: ServicePatch):
        row = svc_or_404(service_id)
        merged = {"name": row["name"], "base_url": row["base_url"], "auth_style": row["auth_style"],
                  "auth_name": row["auth_name"], "enabled": bool(row["enabled"])}
        merged.update({k: v for k, v in body.model_dump().items() if v is not None})
        try:
            new = ServiceIn(**merged)
        except ValidationError as e:
            return err(400, "bad_request", detail=verr(e))
        try:
            with db.tx(conn) as c:
                c.execute("UPDATE services SET name = ?, base_url = ?, auth_style = ?, auth_name = ?, enabled = ?,"
                          " updated_at = ? WHERE id = ?",
                          (new.name, new.base_url, new.auth_style, new.auth_name, int(new.enabled), gw.clock.now(),
                           service_id))
                if new.name != row["name"]:
                    rename_actions(c, row["name"] + ".", new.name + ".")
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        gw.audit_event("admin.service.update", "ok", f"service={new.name}")
        return svc_def(svc_or_404(service_id))

    @admin.delete("/services/{service_id}", dependencies=auth)
    def delete_service(service_id: int):
        row = svc_or_404(service_id)
        with db.tx(conn) as c:
            c.execute("DELETE FROM permissions WHERE action LIKE ? ESCAPE '\\'",
                      (row["name"].replace("_", "\\_") + ".%",))
            c.execute("DELETE FROM services WHERE id = ?", (service_id,))
        gw.audit_event("admin.service.delete", "ok", f"service={row['name']}")
        return {"ok": True}

    @admin.put("/services/{service_id}/credential", dependencies=auth)
    def set_credential(service_id: int, body: CredentialBody):
        row = svc_or_404(service_id)
        with conn.lock:  # encrypt + write atomically w.r.t. a passphrase change
            conn.execute("UPDATE services SET credential_enc = ?, updated_at = ? WHERE id = ?",
                         (gw.vault.enc(body.credential.strip()), gw.clock.now(), service_id))
        gw.audit_event("admin.service.credential", "ok", f"service={row['name']}")
        return {"credential_set": True}

    # -- admin: operations ------------------------------------------------------------------------
    def op_or_404(op_id: int):
        row = conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found")
        return row

    @admin.get("/services/{service_id}/operations", dependencies=auth)
    def list_operations(service_id: int):
        svc_or_404(service_id)
        rows = conn.execute("SELECT * FROM operations WHERE service_id = ? ORDER BY name", (service_id,))
        return {"operations": [op_def(r) for r in rows]}

    @admin.post("/services/{service_id}/operations", dependencies=auth)
    def create_operation(service_id: int, body: OperationIn):
        row = svc_or_404(service_id)
        try:
            op_id = insert_op(conn, service_id, body)
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        gw.audit_event("admin.operation.create", "ok", f"action={row['name']}.{body.name}")
        return op_def(op_or_404(op_id))

    @admin.patch("/operations/{op_id}", dependencies=auth)
    def patch_operation(op_id: int, body: OperationPatch):
        row = op_or_404(op_id)
        cur = op_def(row)
        upd = {k: v for k, v in body.model_dump(exclude={"clear_query_fields", "clear_body_fields"}).items()
               if v is not None}
        cur.update(upd)
        if body.clear_query_fields:
            cur["query_fields"] = None
        if body.clear_body_fields:
            cur["body_fields"] = None
        cur.pop("id")
        try:
            new = OperationIn(**cur)
        except ValidationError as e:
            return err(400, "bad_request", detail=verr(e))
        svc = svc_or_404(row["service_id"])
        # A grant limited by a scope field that goes away would become unlimited: refuse instead.
        limited = []
        for p in conn.execute(
                "SELECT k.name, p.scope_json, p.scope_enc FROM permissions p JOIN gateway_keys k ON k.id = p.key_id"
                " WHERE p.action = ?", (f"{svc['name']}.{row['name']}",)).fetchall():
            if set(read_scope(gw, p) or {}) - set(new.scope_fields):
                limited.append(p["name"])
        if limited:
            return err(409, "scope_in_use", detail="limited by a removed scope field: " + ", ".join(sorted(limited)))
        try:
            with db.tx(conn) as c:
                c.execute("DELETE FROM operations WHERE id = ?", (op_id,))
                c.execute(
                    "INSERT INTO operations (id, service_id, name, method, path_template, query_fields_json,"
                    " body_fields_json, scope_fields_json, required_fields_json, description, enabled)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (op_id, row["service_id"], new.name, new.method, new.path_template,
                     None if new.query_fields is None else json.dumps(new.query_fields),
                     None if new.body_fields is None else json.dumps(new.body_fields),
                     json.dumps(new.scope_fields), json.dumps(new.required_fields) if new.required_fields else None,
                     new.description, int(new.enabled)))
                if new.name != row["name"]:
                    for table in ("permissions", "commands"):
                        c.execute(f"UPDATE {table} SET action = ? WHERE action = ?",
                                  (f"{svc['name']}.{new.name}", f"{svc['name']}.{row['name']}"))
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        gw.audit_event("admin.operation.update", "ok", f"action={svc['name']}.{new.name}")
        return op_def(op_or_404(op_id))

    @admin.delete("/operations/{op_id}", dependencies=auth)
    def delete_operation(op_id: int):
        row = op_or_404(op_id)
        svc = svc_or_404(row["service_id"])
        with db.tx(conn) as c:
            c.execute("DELETE FROM permissions WHERE action = ?", (f"{svc['name']}.{row['name']}",))
            c.execute("DELETE FROM operations WHERE id = ?", (op_id,))
        gw.audit_event("admin.operation.delete", "ok", f"action={svc['name']}.{row['name']}")
        return {"ok": True}

    # -- packs ---------------------------------------------------------------------------------
    @admin.post("/services/import", dependencies=auth)
    def import_pack(pack: Pack, name: str | None = None, base_url: str | None = None):
        if name is not None:
            ImportOpts(name=name)  # same name rule as the body variant
        sid = import_pack_data(gw, pack, name, base_url)
        return svc_def(svc_or_404(sid), len(pack.operations))

    @admin.get("/packs", dependencies=auth)
    def list_packs():
        out = []
        for f, p in bundled_packs(gw).items():
            used = [r["name"] for r in conn.execute("SELECT name FROM services WHERE source_pack = ? ORDER BY name", (f,))]
            out.append({"file": f, "service": p.service.name, "operations": len(p.operations),
                        "self_hosted": p.self_hosted, "base_url": p.service.base_url, "installed_as": used,
                        "suggested_name": free_name(conn, p.service.name)})
        return {"packs": out}

    @admin.post("/packs/{name}/import", dependencies=auth)
    def import_bundled(name: str, opts: ImportOpts | None = None):
        pack = bundled_packs(gw).get(name)
        if pack is None:
            raise ApiError(404, "not_found")
        opts = opts or ImportOpts()
        if pack.self_hosted and not opts.base_url:
            raise ApiError(400, "bad_request", detail=f"base_url: required, {pack.service.name} is self-hosted (your server's API URL)")
        sid = import_pack_data(gw, pack, opts.name, opts.base_url, source=name)
        return svc_def(svc_or_404(sid), len(pack.operations))

    def on_setup(dev: bool) -> None:
        for name, pack in bundled_packs(gw).items():
            if pack.self_hosted:
                continue
            try:
                import_pack_data(gw, pack, source=name)
            except ApiError as e:
                log.warning("bundled pack %s not imported: %s", name, e.code)
        if dev:
            create_demo_key(gw)

    gw.setup_hooks.append(on_setup)

    @admin.get("/services/{service_id}/export", dependencies=auth)
    def export_pack(service_id: int):
        row = svc_or_404(service_id)
        ops = [op_def(r) for r in conn.execute("SELECT * FROM operations WHERE service_id = ? ORDER BY name",
                                               (service_id,))]
        for o in ops:
            o.pop("id")
        return {"service": {k: row[k] for k in ("name", "base_url", "auth_style", "auth_name")}
                | {"enabled": bool(row["enabled"])}, "operations": ops}
