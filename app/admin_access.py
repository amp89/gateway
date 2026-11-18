"""Admin API: gateway keys, actions, permissions, audit (SPEC 8.1)."""
import re
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from .access import read_scope, write_scope
from .admin_auth import ApiError, require_admin
from .errors import err


def _no_control(v: str | None) -> str | None:
    """Key names end up in generated client code (comments, docstrings): one line, no control characters."""
    if v is not None and re.search(r"[\x00-\x1f\x7f]", v):
        raise ValueError("name: no line breaks or control characters")
    return v


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    _name = field_validator("name")(_no_control)


class KeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["active", "disabled"] | None = None
    _name = field_validator("name")(_no_control)


class Grant(BaseModel):
    scope: dict[str, Any] | None = None


def _key_row(r) -> dict:
    return {k: r[k] for k in ("id", "name", "key_hint", "status", "created_at", "last_used_at")}


def admin_access_routes(r: APIRouter, gw) -> None:
    acc = gw.access
    conn = gw.conn
    auth = [Depends(require_admin)]

    def key_or_404(key_id: int):
        row = conn.execute("SELECT * FROM gateway_keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found")
        return row

    @r.get("/keys", dependencies=auth)
    def list_keys():
        return {"keys": [_key_row(x) for x in conn.execute("SELECT * FROM gateway_keys ORDER BY name")]}

    @r.post("/keys", dependencies=auth)
    def create_key(body: KeyCreate):
        try:
            key_id, secret = acc.create_key(body.name.strip())
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        gw.audit_event("admin.key.create", "ok", f"key={body.name}")
        return {"id": key_id, "name": body.name.strip(), "key": secret}

    @r.patch("/keys/{key_id}", dependencies=auth)
    def patch_key(key_id: int, body: KeyPatch):
        row = key_or_404(key_id)
        if row["status"] == "revoked":
            return err(409, "revoked")
        try:
            if body.name is not None:
                conn.execute("UPDATE gateway_keys SET name = ? WHERE id = ?", (body.name.strip(), key_id))
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        if body.status is not None:
            conn.execute("UPDATE gateway_keys SET status = ? WHERE id = ?", (body.status, key_id))
        gw.audit_event("admin.key.update", "ok", f"key={row['name']}")
        return _key_row(key_or_404(key_id))

    @r.delete("/keys/{key_id}", dependencies=auth)
    def revoke_key(key_id: int):
        row = key_or_404(key_id)
        conn.execute("UPDATE gateway_keys SET status = 'revoked' WHERE id = ?", (key_id,))
        conn.execute("DELETE FROM permissions WHERE key_id = ?", (key_id,))
        gw.audit_event("admin.key.revoke", "ok", f"key={row['name']}")
        return {"ok": True}

    @r.get("/actions", dependencies=auth)
    def list_actions():
        acts = sorted(acc.registry.all().values(), key=lambda a: a.name)
        return {"actions": [{"name": a.name, "scope_fields": list(a.scope_fields), "description": a.description,
                             "source": a.source} for a in acts]}

    @r.get("/permissions", dependencies=auth)
    def list_permissions():
        grants = [
            {"key_id": x["key_id"], "action": x["action"], "scope": read_scope(gw, x)}
            for x in conn.execute("SELECT key_id, action, scope_json, scope_enc FROM permissions ORDER BY key_id, action")
        ]
        keys = [_key_row(x) for x in conn.execute("SELECT * FROM gateway_keys WHERE status != 'revoked' ORDER BY name")]
        return {"keys": keys, "actions": sorted(acc.registry.all()), "grants": grants}

    @r.put("/permissions/{key_id}/{action}", dependencies=auth)
    def put_permission(key_id: int, action: str, body: Grant):
        row = key_or_404(key_id)
        if row["status"] == "revoked":
            return err(409, "revoked")
        scope = acc.validate_scope(action, body.scope)
        with conn.lock:  # encrypt + write atomically w.r.t. a passphrase change
            conn.execute(
                "INSERT INTO permissions (key_id, action, scope_json, created_at) VALUES (?, ?, NULL, ?)"
                " ON CONFLICT(key_id, action) DO NOTHING", (key_id, action, gw.clock.now()))
            perm = conn.execute("SELECT id FROM permissions WHERE key_id = ? AND action = ?", (key_id, action)).fetchone()
            write_scope(gw, perm["id"], scope)
        gw.audit_event("admin.permission.set", "ok", f"key={row['name']} action={action}")
        return {"key_id": key_id, "action": action, "scope": scope}

    @r.delete("/permissions/{key_id}/{action}", dependencies=auth)
    def delete_permission(key_id: int, action: str):
        row = key_or_404(key_id)
        conn.execute("DELETE FROM permissions WHERE key_id = ? AND action = ?", (key_id, action))
        gw.audit_event("admin.permission.delete", "ok", f"key={row['name']} action={action}")
        return {"ok": True}

    @r.get("/audit", dependencies=auth)
    def audit(key_id: int | None = None, action: str | None = None, status: str | None = None,
              since: float | None = None, until: float | None = None, before_id: int | None = None,
              limit: int = 100):
        limit = max(1, min(limit, 500))
        where, args = [], []
        for col, val, op in (("key_id", key_id, "="), ("action", action, "="), ("status", status, "="),
                             ("ts", since, ">="), ("ts", until, "<"), ("id", before_id, "<")):
            if val is not None:
                where.append(f"{col} {op} ?")
                args.append(val)
        sql = "SELECT * FROM audit" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = [{k: x[k] for k in x.keys() if k != "resource_enc"} | {"resource": gw.audit_resource(x)}
                for x in conn.execute(sql, (*args, limit + 1))]
        nxt = rows[limit - 1]["id"] if len(rows) > limit else None
        return {"rows": rows[:limit], "next_before_id": nxt}
