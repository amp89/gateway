"""Telegram module (SPEC 9): bots, allowlists, telegram.send; polling + telegram.read live in telegram_poll."""
import logging
import re
import sqlite3
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import db
from ..access import Action, read_scope, run_action, write_scope
from ..admin_auth import ApiError, require_admin
from ..errors import err

log = logging.getLogger("gateway.telegram")
API = "https://api.telegram.org"
NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"
TOKEN_RE = re.compile(r"^\d{3,20}:[A-Za-z0-9_-]{20,100}$")


# -- storage helpers ------------------------------------------------------------------------
def bot_by_name(gw, name: str):
    return gw.conn.execute("SELECT * FROM bots WHERE name = ?", (name,)).fetchone()


def allowlist(gw, row) -> list[int]:
    return gw.vault.dec_json(row["allowlist_enc"])


USERS_META = "telegram_users_enc"


def global_users(gw, conn=None) -> list[dict]:
    """The admin-managed Telegram users [{id, label}], encrypted in meta."""
    v = db.meta_get(conn or gw.conn, USERS_META)
    return [] if v is None else gw.vault.dec_json(v)


def global_ids(gw, conn=None) -> set[int]:
    return {u["id"] for u in global_users(gw, conn)}


def effective_allowlist(gw, row) -> set[int]:
    """Per-bot list intersected with the global user list (a user removed there is cut off everywhere)."""
    return set(allowlist(gw, row)) & global_ids(gw)


def token(gw, row) -> str:
    return gw.vault.dec(row["token_enc"])


async def tg_call(gw, row, method: str, payload: dict, timeout: float | None = None) -> dict:
    """POST to the Bot API. Never lets the token (it is in the URL) reach errors or logs."""
    url = f"{API}/bot{token(gw, row)}/{method}"
    try:
        r = await gw.http.post(url, json=payload, timeout=timeout or gw.settings.upstream_timeout_s)
    except httpx.TimeoutException:
        raise ApiError(504, "upstream_timeout") from None
    except httpx.HTTPError:
        raise ApiError(502, "upstream_error", detail="connection failed") from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("ok"):
        desc = str(data.get("description", "")).replace(token(gw, row), "***")[:300]
        raise ApiError(502, "upstream_error", status=r.status_code, detail=desc)
    return data["result"]


# -- models -----------------------------------------------------------------------------------
class BotCreate(BaseModel):
    name: str = Field(pattern=NAME_RE)
    token: str
    polling_enabled: bool = True
    retention_days: int | None = Field(default=None, ge=0)


class BotPatch(BaseModel):
    name: str | None = Field(default=None, pattern=NAME_RE)
    token: str | None = None
    polling_enabled: bool | None = None
    retention_days: int | None = Field(default=None, ge=0)


class AllowlistBody(BaseModel):
    user_ids: list[int]


class TgUser(BaseModel):
    id: int = Field(gt=0)
    label: str = Field(default="", max_length=64)


class UsersBody(BaseModel):
    users: list[TgUser] = Field(max_length=500)


class SendBody(BaseModel):
    bot: str
    chat_id: int
    text: str = Field(min_length=1, max_length=4096)
    parse_mode: Literal["HTML", "MarkdownV2"] | None = None
    silent: bool = False


_PLAIN_TOKEN = object()  # placeholder in patch_bot's argument list for the token, encrypted at write time


def rename_in_scopes(gw, c, old: str, new: str | None) -> None:
    """Follow a bot rename (new) or deletion (None) in telegram.* scopes, inside the caller's transaction.
    A grant whose bot list ends up empty is deleted: an empty scope would mean "no limit"."""
    for p in c.execute("SELECT id, scope_json, scope_enc FROM permissions WHERE action LIKE 'telegram.%'").fetchall():
        scope = read_scope(gw, p)
        if not scope or old not in scope.get("bots", []):
            continue
        bots = [b for b in scope["bots"] if b != old] + ([new] if new and new not in scope["bots"] else [])
        if bots:
            write_scope(gw, p["id"], scope | {"bots": bots})
        else:
            c.execute("DELETE FROM permissions WHERE id = ?", (p["id"],))


def _check_token(t: str) -> str:
    t = t.strip()
    if not TOKEN_RE.match(t):
        raise ApiError(400, "bad_request", detail="not a bot token")
    return t


def setup(gw, client: APIRouter, admin: APIRouter) -> None:
    reg = gw.access.registry
    reg.register(Action("telegram.send", ("bots", "chat_ids"), "Send a text message from a bot"))
    reg.register(Action("telegram.read", ("bots",), "Read stored inbound messages"))
    auth = [Depends(require_admin)]
    conn = gw.conn

    def bot_out(row) -> dict:
        return {
            "id": row["id"], "name": row["name"], "token_set": True, "allowlist": sorted(allowlist(gw, row)),
            "polling_enabled": bool(row["polling_enabled"]), "retention_days": row["retention_days"],
            "poll_error": row["poll_error"], "created_at": row["created_at"],
        }

    def bot_or_404(bot_id: int):
        row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found")
        return row

    @admin.get("/bots", dependencies=auth)
    def list_bots():
        return {"bots": [bot_out(r) for r in conn.execute("SELECT * FROM bots ORDER BY name")],
                "users": global_users(gw)}

    @admin.get("/telegram/users", dependencies=auth)
    def get_users():
        return {"users": global_users(gw)}

    @admin.put("/telegram/users", dependencies=auth)
    def put_users(body: UsersBody):
        users = [{"id": u.id, "label": u.label.strip()} for u in body.users]
        ids = [u["id"] for u in users]
        if len(ids) != len(set(ids)):
            return err(400, "bad_request", detail="duplicate user id")
        keep = set(ids)
        stripped = 0
        with db.tx(conn) as c:
            db.meta_set(c, USERS_META, gw.vault.enc_json(users))
            for row in c.execute("SELECT id, allowlist_enc FROM bots").fetchall():
                cur = allowlist(gw, row)
                new = [i for i in cur if i in keep]
                if new != cur:
                    stripped += len(cur) - len(new)
                    c.execute("UPDATE bots SET allowlist_enc = ? WHERE id = ?", (gw.vault.enc_json(new), row["id"]))
        gw.audit_event("admin.telegram.users", "ok", f"count={len(users)} stripped={stripped}")
        return {"users": users}

    @admin.post("/bots", dependencies=auth)
    def create_bot(body: BotCreate):
        tok = _check_token(body.token)
        retention = gw.settings.default_message_retention_days if body.retention_days is None else body.retention_days
        try:
            with conn.lock:  # encrypt + write atomically w.r.t. a passphrase change
                cur = conn.execute(
                    "INSERT INTO bots (name, token_enc, allowlist_enc, polling_enabled, retention_days, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (body.name, gw.vault.enc(tok), gw.vault.enc_json([]), int(body.polling_enabled), retention,
                     gw.clock.now()),
                )
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        gw.audit_event("admin.bot.create", "ok", f"bot={body.name}")
        gw.bots_changed()
        return bot_out(bot_or_404(cur.lastrowid))

    @admin.patch("/bots/{bot_id}", dependencies=auth)
    def patch_bot(bot_id: int, body: BotPatch):
        row = bot_or_404(bot_id)
        tok = _check_token(body.token) if body.token is not None else None
        sets, args = [], []
        if body.name is not None:
            sets.append("name = ?"); args.append(body.name)
        if tok is not None:
            sets.append("token_enc = ?"); args.append(_PLAIN_TOKEN)  # encrypted below, under the DB lock
            sets.append("poll_error = ''")
        if body.polling_enabled is not None:
            sets.append("polling_enabled = ?"); args.append(int(body.polling_enabled))
            if body.polling_enabled:
                sets.append("poll_error = ''")
        if body.retention_days is not None:
            sets.append("retention_days = ?"); args.append(body.retention_days)
        if sets:
            try:
                with db.tx(conn) as c:  # encrypt + write atomically w.r.t. a passphrase change
                    args = [gw.vault.enc(tok) if a is _PLAIN_TOKEN else a for a in args]
                    c.execute(f"UPDATE bots SET {', '.join(sets)} WHERE id = ?", (*args, bot_id))
                    if body.name is not None and body.name != row["name"]:
                        rename_in_scopes(gw, c, row["name"], body.name)
            except sqlite3.IntegrityError:
                return err(409, "name_taken")
        gw.audit_event("admin.bot.update", "ok", f"bot={row['name']}")
        gw.bots_changed()
        return bot_out(bot_or_404(bot_id))

    @admin.delete("/bots/{bot_id}", dependencies=auth)
    def delete_bot(bot_id: int):
        row = bot_or_404(bot_id)
        with db.tx(conn) as c:
            c.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
            rename_in_scopes(gw, c, row["name"], None)
        gw.audit_event("admin.bot.delete", "ok", f"bot={row['name']}")
        gw.bots_changed()
        return {"ok": True}

    @admin.get("/bots/{bot_id}/allowlist", dependencies=auth)
    def get_allowlist(bot_id: int):
        return {"user_ids": sorted(allowlist(gw, bot_or_404(bot_id))), "users": global_users(gw)}

    @admin.put("/bots/{bot_id}/allowlist", dependencies=auth)
    def put_allowlist(bot_id: int, body: AllowlistBody):
        row = bot_or_404(bot_id)
        ids = sorted(set(body.user_ids))
        known = global_ids(gw)
        unknown = [i for i in ids if i not in known]
        if unknown:
            return err(400, "unknown_user", user_ids=unknown)
        with conn.lock:  # encrypt + write atomically w.r.t. a passphrase change
            conn.execute("UPDATE bots SET allowlist_enc = ? WHERE id = ?", (gw.vault.enc_json(ids), bot_id))
        gw.audit_event("admin.bot.allowlist", "ok", f"bot={row['name']} count={len(ids)}")
        return {"user_ids": ids}

    @client.post("/telegram/send")
    async def send(request: Request, body: SendBody):
        async def handler(caller):
            row = bot_by_name(gw, body.bot)
            if row is None or body.chat_id not in effective_allowlist(gw, row):
                raise ApiError(403, "forbidden")
            payload = {"chat_id": body.chat_id, "text": body.text}
            if body.parse_mode:
                payload["parse_mode"] = body.parse_mode
            if body.silent:
                payload["disable_notification"] = True
            result = await tg_call(gw, row, "sendMessage", payload)
            return {"message_id": result.get("message_id")}

        return await run_action(gw, request, "telegram.send", {"bots": body.bot, "chat_ids": body.chat_id}, handler)
