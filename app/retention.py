"""Cleanup (SPEC 9, 8.1): hourly delete of old messages and audit rows; settings + run-now admin routes.

Deletes by timestamp only, so it also runs while sealed (nothing is decrypted).
"""
import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import db
from .admin_auth import require_admin

log = logging.getLogger("gateway.retention")
CLEANUP_EVERY_S = 3600


def audit_retention_days(gw) -> int:
    v = db.meta_get(gw.conn, "audit_retention_days")
    return gw.settings.audit_retention_days if v is None else int(v)


class Retention:
    def __init__(self, gw) -> None:
        self.gw = gw
        self.task: asyncio.Task | None = None
        self.last: dict | None = None  # {at, messages_deleted, audit_deleted}, in memory only

    def run(self) -> tuple[int, int]:
        """One cleanup pass. Returns (messages deleted, audit rows deleted)."""
        gw = self.gw
        now = gw.clock.now()
        msgs = 0
        for b in gw.conn.execute("SELECT id, retention_days FROM bots WHERE retention_days > 0").fetchall():
            msgs += gw.conn.execute("DELETE FROM messages WHERE bot_id = ? AND received_at < ?",
                                    (b["id"], now - b["retention_days"] * 86400)).rowcount
        days = audit_retention_days(gw)
        audits = 0
        if days > 0:
            audits = gw.conn.execute("DELETE FROM audit WHERE ts < ?", (now - days * 86400,)).rowcount
        self.last = {"at": now, "messages_deleted": msgs, "audit_deleted": audits}
        return msgs, audits

    async def loop(self) -> None:
        while True:
            try:
                self.run()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("cleanup failed")
            await asyncio.sleep(CLEANUP_EVERY_S)

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self.loop())

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


class SettingsBody(BaseModel):
    audit_retention_days: int = Field(ge=0)


def retention_routes(r: APIRouter, gw) -> None:
    auth = [Depends(require_admin)]
    ret = gw.retention

    def cleanup_out() -> dict:
        last = ret.last or {}
        return {"every_s": CLEANUP_EVERY_S, "last_run_at": last.get("at"),
                "last_messages_deleted": last.get("messages_deleted"), "last_audit_deleted": last.get("audit_deleted")}

    @r.get("/settings", dependencies=auth)
    def get_settings():
        s = gw.settings
        return {"audit_retention_days": audit_retention_days(gw), "api_rate_per_min": s.api_rate_per_min,
                "default_message_retention_days": s.default_message_retention_days, "api_prefix": s.api_prefix,
                "api_url": s.api_url, "api_hosts": list(s.api_hosts),
                "cleanup": cleanup_out()}

    @r.put("/settings", dependencies=auth)
    def put_settings(body: SettingsBody):
        db.meta_set(gw.conn, "audit_retention_days", str(body.audit_retention_days))
        gw.audit_event("admin.settings", "ok", f"audit_retention_days={body.audit_retention_days}")
        return get_settings()

    @r.post("/cleanup", dependencies=auth)
    def cleanup_now():
        msgs, audits = ret.run()
        gw.audit_event("admin.cleanup", "ok", f"messages={msgs} audit={audits}")
        return cleanup_out()
