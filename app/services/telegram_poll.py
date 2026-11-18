"""Telegram polling, stored messages, telegram.read (SPEC 9). Cleanup lives in app/retention.py."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Request

from .. import db
from ..access import run_action
from ..admin_auth import ApiError, require_admin
from .telegram import bot_by_name, effective_allowlist, tg_call

log = logging.getLogger("gateway.telegram.poll")


class Pollers:
    def __init__(self, gw) -> None:
        self.gw = gw
        self.tasks: dict[int, tuple[asyncio.Task, str]] = {}
        self.sleep = asyncio.sleep  # tests replace this

    # -- lifecycle (all on the event loop) ----------------------------------------------
    def sync(self) -> None:
        """Start/stop/restart one task per enabled bot. Runs only while unsealed."""
        if not self.gw.vault.unsealed:
            return
        want = {r["id"]: r["token_enc"] for r in self.gw.conn.execute(
            "SELECT id, token_enc FROM bots WHERE polling_enabled = 1 AND poll_error = ''")}
        for bot_id, (task, tok) in list(self.tasks.items()):
            if bot_id not in want or want[bot_id] != tok or task.done():
                task.cancel()
                del self.tasks[bot_id]
        for bot_id, tok in want.items():
            if bot_id not in self.tasks:
                self.tasks[bot_id] = (asyncio.get_running_loop().create_task(self.run(bot_id)), tok)

    async def stop(self) -> None:
        tasks = [t for t, _ in self.tasks.values()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    # -- polling -------------------------------------------------------------------------
    async def run(self, bot_id: int) -> None:
        backoff = 1.0
        while True:
            try:
                if not await self.poll_once(bot_id):
                    return
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except ApiError as e:
                if e.extra.get("status") in (401, 404):  # token revoked / wrong: stop until an admin fixes it
                    self.gw.conn.execute("UPDATE bots SET poll_error = ? WHERE id = ?", ("unauthorized", bot_id))
                    log.warning("bot %d: token rejected by Telegram; polling stopped", bot_id)
                    return
                log.warning("bot %d: poll error %s; retry in %ss", bot_id, e.code, backoff)
                await self.sleep(backoff)
                backoff = min(60.0, backoff * 2)
            except Exception:  # noqa: BLE001 - keep polling whatever happens
                log.exception("bot %d: unexpected poll failure; retry in %ss", bot_id, backoff)
                await self.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def poll_once(self, bot_id: int) -> bool:
        """One getUpdates round. Returns False when the bot is gone or disabled."""
        row = self.gw.conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if row is None or not row["polling_enabled"] or row["poll_error"] or not self.gw.vault.unsealed:
            return False
        t = self.gw.settings.telegram_poll_timeout_s
        updates = await tg_call(self.gw, row, "getUpdates",
                                {"offset": row["poll_offset"], "timeout": t, "allowed_updates": ["message"]},
                                timeout=t + 10)
        saved = self.store(bot_id, updates)
        if saved:
            for hook in self.gw.inbound_hooks:  # after the commit: a crash here never re-runs a stored message
                await hook(bot_id, saved)
        return True

    def store(self, bot_id: int, updates: list) -> list[dict]:
        """Save allowed messages and advance the offset in one transaction (no gaps, no duplicates).
        Returns the newly stored messages: {id, chat_id, sender_id, text}."""
        if not updates:
            return []
        gw = self.gw
        saved: list[dict] = []
        with db.tx(gw.conn) as c:
            row = c.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
            if row is None:
                return []
            allowed = effective_allowlist(gw, row)
            offset = row["poll_offset"]
            for u in updates:
                uid = int(u.get("update_id", 0))
                offset = max(offset, uid + 1)
                msg = u.get("message") or {}
                sender = msg.get("from") or {}
                text = msg.get("text") or msg.get("caption")
                if sender.get("id") not in allowed or text is None:
                    continue  # dropped unsaved
                chat_id = int((msg.get("chat") or {}).get("id", 0))
                cur = c.execute(
                    "INSERT OR IGNORE INTO messages (bot_id, update_id, tg_message_id, chat_id, chat_id_enc, sent_at,"
                    " received_at, sender_enc, body_enc) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
                    (bot_id, uid, int(msg.get("message_id", 0)), gw.vault.enc(str(chat_id)),
                     float(msg.get("date", 0)), gw.clock.now(),
                     gw.vault.enc_json({k: sender.get(k) for k in ("id", "username", "first_name", "last_name")}),
                     gw.vault.enc(text)),
                )
                if cur.rowcount:
                    saved.append({"id": cur.lastrowid, "chat_id": chat_id, "sender_id": sender.get("id"),
                                  "text": text})
            c.execute("UPDATE bots SET poll_offset = ? WHERE id = ?", (offset, bot_id))
        return saved


def _message_out(gw, r) -> dict:
    chat_id = int(gw.vault.dec(r["chat_id_enc"])) if r["chat_id_enc"] else r["chat_id"]  # plain: pre-0008 row
    return {"id": r["id"], "chat_id": chat_id, "sender": gw.vault.dec_json(r["sender_enc"]),
            "text": gw.vault.dec(r["body_enc"]), "sent_at": r["sent_at"]}


def setup(gw, client: APIRouter, admin: APIRouter) -> None:
    p = gw.pollers = Pollers(gw)
    gw.unseal_hooks.append(p.sync)
    gw.bots_hooks.append(p.sync)
    gw.shutdown_hooks.append(p.stop)
    auth = [Depends(require_admin)]

    @client.get("/telegram/messages")
    async def read(request: Request, bot: str, since: int = 0, limit: int = 50):
        limit = max(1, min(limit, 200))

        async def handler(caller):
            row = bot_by_name(gw, bot)
            if row is None:
                raise ApiError(403, "forbidden")
            rows = gw.conn.execute(
                "SELECT * FROM messages WHERE bot_id = ? AND id > ? ORDER BY id LIMIT ?", (row["id"], since, limit)
            ).fetchall()
            msgs = [_message_out(gw, r) for r in rows]
            return {"messages": msgs, "next_since": msgs[-1]["id"] if msgs else since}

        return await run_action(gw, request, "telegram.read", {"bots": bot}, handler)

    @admin.get("/messages", dependencies=auth)
    def list_messages(bot_id: int | None = None, before_id: int | None = None, limit: int = 50):
        limit = max(1, min(limit, 200))
        where, args = [], []
        if bot_id is not None:
            where.append("bot_id = ?"); args.append(bot_id)
        if before_id is not None:
            where.append("id < ?"); args.append(before_id)
        sql = "SELECT * FROM messages" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = gw.conn.execute(sql, (*args, limit + 1)).fetchall()
        out = [dict(_message_out(gw, r), bot_id=r["bot_id"], received_at=r["received_at"]) for r in rows[:limit]]
        return {"messages": out, "next_before_id": out[-1]["id"] if len(rows) > limit else None}

    @admin.delete("/messages/{message_id}", dependencies=auth)
    def delete_message(message_id: int):
        n = gw.conn.execute("DELETE FROM messages WHERE id = ?", (message_id,)).rowcount
        if not n:
            raise ApiError(404, "not_found")
        gw.audit_event("admin.message.delete", "ok", f"message={message_id}")
        return {"ok": True}
