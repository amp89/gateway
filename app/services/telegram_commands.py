"""Bot commands (SPEC 9.1): `/name rest` sent to a bot runs one declared operation as a gateway key, then replies.

Runs after a poll batch is stored (the message is kept like any other). At most once: a crash between storing
and running loses the command; it never runs twice. A failing command never stops the poller or the batch.
"""
import json
import logging
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError

from ..access import _resource_str, run_checked
from ..admin_auth import ApiError, require_admin
from ..errors import err
from .generic import PLACEHOLDER, CallBody, prepare_call, verr
from .telegram import tg_call

log = logging.getLogger("gateway.telegram.commands")
CMD_NAME = r"^[a-z0-9_]{1,32}$"
COMMAND = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@[A-Za-z0-9_]{1,64})?(?:\s+(.*))?$", re.DOTALL)
ACTION = re.compile(r"^([a-z0-9][a-z0-9_]{0,31})\.([a-z0-9][a-z0-9_]{0,63})$")
TEXT = "{text}"
MAX_REPLY = 4096
ERRORS = {400: "not accepted", 401: "not allowed", 403: "not allowed", 429: "too many requests",
          502: "upstream error", 504: "upstream timeout"}
NEEDS_TEXT = "needs text after the command"


# -- parsing and templates ------------------------------------------------------------------
def parse(text: str) -> tuple[str, str] | None:
    """'/Todo@my_bot buy milk' -> ('todo', 'buy milk'); anything else -> None."""
    m = COMMAND.match(text.strip())
    if m is None:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


def uses_text(v) -> bool:
    if isinstance(v, str):
        return TEXT in v
    if isinstance(v, dict):
        return any(uses_text(x) for x in v.values())
    if isinstance(v, list):
        return any(uses_text(x) for x in v)
    return False


def fill(v, text: str):
    """Put the text into string values of the parsed template only, so it can never add fields or structure."""
    if isinstance(v, str):
        return v.replace(TEXT, text)
    if isinstance(v, dict):
        return {k: fill(x, text) for k, x in v.items()}
    if isinstance(v, list):
        return [fill(x, text) for x in v]
    return v


FIELD_REF = re.compile(r"\{(\.|[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\}")
LOOP = re.compile(r"\{#([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\}(.*?)\{/\1\}", re.DOTALL)


def lookup(data, path: str):
    if path == ".":
        return data
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


def as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, int, float)):
        return str(v)
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def fields(tpl: str, data) -> str:
    return FIELD_REF.sub(lambda m: as_text(lookup(data, m.group(1))), tpl)


# Loops and fields in one pass: text that came from the upstream is never scanned for {...} again.
TOKEN = re.compile(LOOP.pattern + "|" + FIELD_REF.pattern, re.DOTALL)


def render(tpl: str, data) -> str:
    """{a.b} = field; {#list}…{/list} = repeat for each item, with {field} / {.} relative to the item."""
    def one(m):
        if m.group(1) is not None:
            items = lookup(data, m.group(1))
            return "".join(fields(m.group(2), it) for it in items) if isinstance(items, list) else ""
        return as_text(lookup(data, m.group(3)))
    return TOKEN.sub(one, tpl)


def reply_text(tpl: str, result: dict) -> str:
    out = render(tpl, result) if tpl else ""
    return (out if out.strip() else "done")[:MAX_REPLY]


# -- admin models ----------------------------------------------------------------------------
class CommandIn(BaseModel):
    bot_id: int
    command: str = Field(pattern=CMD_NAME)
    key_id: int
    action: str = Field(max_length=97)
    call: dict[str, Any] = Field(default_factory=dict)
    reply: str = Field(default="", max_length=MAX_REPLY)
    enabled: bool = True


class CommandPatch(BaseModel):
    bot_id: int | None = None
    command: str | None = Field(default=None, pattern=CMD_NAME)
    key_id: int | None = None
    action: str | None = Field(default=None, max_length=97)
    call: dict[str, Any] | None = None
    reply: str | None = Field(default=None, max_length=MAX_REPLY)
    enabled: bool | None = None


def setup(gw, client: APIRouter, admin: APIRouter) -> None:
    conn = gw.conn
    auth = [Depends(require_admin)]

    # -- running -------------------------------------------------------------------------------
    async def send(bot, chat_id: int, text: str) -> None:
        try:
            await tg_call(gw, bot, "sendMessage", {"chat_id": chat_id, "text": text})
        except Exception:  # noqa: BLE001 - a lost reply must not stop the poller
            log.warning("bot %s: command reply not sent", bot["name"])

    async def run_one(bot_id: int, m: dict) -> None:
        parsed = parse(m["text"])
        if parsed is None or m["chat_id"] != m["sender_id"]:  # private chats only
            return
        name, text = parsed
        rule = conn.execute("SELECT * FROM commands WHERE bot_id = ? AND command = ? AND enabled = 1",
                            (bot_id, name)).fetchone()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if rule is None or bot is None:
            return
        call = gw.vault.dec_json(rule["call_enc"])
        if not text and uses_text(call):
            await send(bot, m["chat_id"], NEEDS_TEXT)
            return
        tpl = gw.vault.dec(rule["reply_enc"]) if rule["reply_enc"] else ""
        service, operation = rule["action"].split(".", 1)
        try:
            body = CallBody(**fill(call, text))
            resources, handler = prepare_call(gw, service, operation, body)
            where = f"bot={bot['name']} command={name}"
            extra = _resource_str(resources)
            result = await run_checked(gw, lambda: gw.access.caller_by_id(rule["key_id"]), rule["action"],
                                       resources, handler, "telegram", f"{where} {extra}".strip())
            out = reply_text(tpl, result)
        except ApiError as e:
            out = ERRORS.get(e.status, "failed")
        except ValidationError:
            out = ERRORS[400]
        await send(bot, m["chat_id"], out)

    async def on_inbound(bot_id: int, msgs: list[dict]) -> None:
        for m in msgs:  # in order, one at a time
            try:
                await run_one(bot_id, m)
            except Exception:  # noqa: BLE001 - one bad command must not stop the rest
                log.exception("bot %d: command failed", bot_id)

    gw.inbound_hooks.append(on_inbound)

    # -- admin ---------------------------------------------------------------------------------
    def problem(row) -> str:
        key = conn.execute("SELECT status FROM gateway_keys WHERE id = ?", (row["key_id"],)).fetchone()
        if key is None or key["status"] != "active":
            return f"key {key['status'] if key else 'missing'}"
        if gw.access.registry.get(row["action"]) is None:
            return "action missing or disabled"
        if conn.execute("SELECT 1 FROM permissions WHERE key_id = ? AND action = ?",
                        (row["key_id"], row["action"])).fetchone() is None:
            return "key lacks this permission (tick it on Permissions)"
        return ""

    def out(row) -> dict:
        bot = conn.execute("SELECT name FROM bots WHERE id = ?", (row["bot_id"],)).fetchone()
        key = conn.execute("SELECT name FROM gateway_keys WHERE id = ?", (row["key_id"],)).fetchone()
        return {"id": row["id"], "bot_id": row["bot_id"], "bot": bot["name"] if bot else "",
                "command": row["command"], "key_id": row["key_id"], "key": key["name"] if key else "",
                "action": row["action"], "call": gw.vault.dec_json(row["call_enc"]),
                "reply": gw.vault.dec(row["reply_enc"]) if row["reply_enc"] else "",
                "enabled": bool(row["enabled"]), "created_at": row["created_at"], "problem": problem(row)}

    def bad(detail: str) -> ApiError:
        return ApiError(400, "bad_request", detail=detail)

    def check(c: CommandIn) -> dict:
        """Refs exist, action is a declared service operation, call fits it. Returns the normalized call."""
        if conn.execute("SELECT 1 FROM bots WHERE id = ?", (c.bot_id,)).fetchone() is None:
            raise bad("unknown bot")
        key = conn.execute("SELECT status FROM gateway_keys WHERE id = ?", (c.key_id,)).fetchone()
        if key is None or key["status"] == "revoked":
            raise bad("unknown or revoked key")
        m = ACTION.match(c.action)
        op = None if m is None else conn.execute(
            "SELECT o.path_template FROM operations o JOIN services s ON s.id = o.service_id"
            " WHERE s.name = ? AND o.name = ?", (m.group(1), m.group(2))).fetchone()
        if op is None:
            raise bad("action must be a service operation (service.operation)")
        extra = set(c.call) - {"path", "query", "body"}
        if extra:
            raise bad(f"call template: unknown parts {sorted(extra)}; use path, query, body")
        try:
            call = CallBody(**c.call)
        except ValidationError as e:
            raise bad("call template: " + verr(e)) from None
        names = set(PLACEHOLDER.findall(op["path_template"]))
        if set(call.path) != names:
            raise bad(f"call template: path params must be exactly {sorted(names)}")
        return call.model_dump()

    def rule_or_404(cmd_id: int):
        row = conn.execute("SELECT * FROM commands WHERE id = ?", (cmd_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found")
        return row

    @admin.get("/commands", dependencies=auth)
    def list_commands():
        rows = conn.execute("SELECT c.* FROM commands c JOIN bots b ON b.id = c.bot_id ORDER BY b.name, c.command")
        return {"commands": [out(r) for r in rows.fetchall()]}

    @admin.post("/commands", dependencies=auth)
    def create_command(body: CommandIn):
        call = check(body)
        try:
            with conn.lock:  # encrypt + write atomically w.r.t. a passphrase change
                cur = conn.execute(
                    "INSERT INTO commands (bot_id, command, key_id, action, call_enc, reply_enc, enabled, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (body.bot_id, body.command, body.key_id, body.action, gw.vault.enc_json(call),
                     gw.vault.enc(body.reply) if body.reply else "", int(body.enabled), gw.clock.now()))
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        row = rule_or_404(cur.lastrowid)
        gw.audit_event("admin.command.create", "ok", f"bot_id={body.bot_id} command={body.command}")
        return out(row)

    @admin.patch("/commands/{cmd_id}", dependencies=auth)
    def patch_command(cmd_id: int, body: CommandPatch):
        row = rule_or_404(cmd_id)
        cur = out(row)
        merged = {k: cur[k] for k in ("bot_id", "command", "key_id", "action", "call", "reply", "enabled")}
        merged.update({k: v for k, v in body.model_dump().items() if v is not None})
        try:
            new = CommandIn(**merged)
        except ValidationError as e:
            return err(400, "bad_request", detail=verr(e))
        call = check(new)
        try:
            with conn.lock:  # encrypt + write atomically w.r.t. a passphrase change
                conn.execute(
                    "UPDATE commands SET bot_id = ?, command = ?, key_id = ?, action = ?, call_enc = ?,"
                    " reply_enc = ?, enabled = ? WHERE id = ?",
                    (new.bot_id, new.command, new.key_id, new.action, gw.vault.enc_json(call),
                     gw.vault.enc(new.reply) if new.reply else "", int(new.enabled), cmd_id))
        except sqlite3.IntegrityError:
            return err(409, "name_taken")
        gw.audit_event("admin.command.update", "ok", f"bot_id={new.bot_id} command={new.command}")
        return out(rule_or_404(cmd_id))

    @admin.delete("/commands/{cmd_id}", dependencies=auth)
    def delete_command(cmd_id: int):
        row = rule_or_404(cmd_id)
        conn.execute("DELETE FROM commands WHERE id = ?", (cmd_id,))
        gw.audit_event("admin.command.delete", "ok", f"bot_id={row['bot_id']} command={row['command']}")
        return {"ok": True}
