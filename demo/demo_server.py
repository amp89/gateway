"""Demo gateway for the README screenshots (run by podman-demo-shots.sh).

The real app, served by uvicorn, with fake upstreams (no network) and a demo clock (stable dates).
Once the browser has finished web setup, a seeder thread fills it with made-up data through the real
admin and client API, then writes /run/gateway/demo.seeded. Nothing here touches a real DB, token or person.
"""
import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, "/srv")  # the prod image's app dir (python -I ignores PYTHONPATH and the cwd)

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.clock import Clock  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

HOST = os.environ["DEMO_HOST"]
ADMIN_PASSWORD = os.environ["DEMO_ADMIN_PASSWORD"]
SEEDED = "/run/gateway/demo.seeded"
DAY0 = datetime(2026, 11, 10, 7, 40, tzinfo=timezone.utc).timestamp()  # setup happens here
TAILNET_IP = "100.84.12.7"  # admin "browser" (Tailscale range)


class DemoClock(Clock):
    """Wall time set by the seeder; monotonic stays real (sessions, rate limits, lockouts)."""

    def __init__(self, t: float) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


class DemoSettings(Settings):
    @property
    def api_url(self) -> str:  # what the code samples show
        return "https://api.example.com/api/v1"


# -- fake upstreams --------------------------------------------------------------------------
inbox: dict[str, list] = {}  # bot token -> pending Telegram updates
inbox_lock = threading.Lock()
counter = {"msg": 4100, "update": 905000000, "task": 8812000000}


def ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


LATENCY = (0.142, 0.231, 0.096, 0.318, 0.174, 0.262, 0.119, 0.205)  # seconds, cycled: realistic audit "ms"


async def fake_upstream(request: httpx.Request) -> httpx.Response:
    url, host = str(request.url), request.url.host
    body = json.loads(request.content) if request.content else {}
    if not request.url.path.endswith("/getUpdates"):
        counter["calls"] = counter.get("calls", 0) + 1
        await asyncio.sleep(LATENCY[counter["calls"] % len(LATENCY)])
    if host == "api.telegram.org":
        token, method = request.url.path.split("/")[1][3:], request.url.path.split("/")[2]
        if method == "sendMessage":
            counter["msg"] += 1
            return ok({"message_id": counter["msg"], "chat": {"id": body.get("chat_id")}, "text": body.get("text")})
        if method == "getUpdates":
            for _ in range(500):  # long poll: up to ~25 s, then empty
                with inbox_lock:
                    pending, inbox[token] = inbox.get(token, []), []
                if pending:
                    return ok(pending)
                await asyncio.sleep(0.05)
            return ok([])
        return ok({"id": 1, "is_bot": True, "username": "demo_bot"})
    if host == "api.todoist.com":
        if request.method == "POST" and request.url.path.endswith("/tasks/quick"):
            counter["task"] += 1
            words = [w for w in str(body.get("text", "")).split() if not w.startswith("#")]
            return httpx.Response(200, json={"id": str(counter["task"]), "content": " ".join(words)})
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            counter["task"] += 1
            return httpx.Response(200, json={"id": str(counter["task"]), "content": body.get("content", ""),
                                             "project_id": body.get("project_id", "6Xm5HVVRcX00")})
        return httpx.Response(200, json={"results": [{"id": "8812000101", "content": "Renew passport"}],
                                         "next_cursor": None})
    if host in ("kanbanflow.com", "tasks.example.org"):
        return httpx.Response(200, json={"id": "t-31", "title": body.get("title") or body.get("name", "")})
    return httpx.Response(404, json={"error": f"no fake for {url}"})


# -- seeding -----------------------------------------------------------------------------------
USERS = [
    {"id": 100000001, "label": "Maya (phone)", "first_name": "Maya", "username": "maya_k"},
    {"id": 100000002, "label": "Jonas", "first_name": "Jonas", "username": ""},
    {"id": 100000003, "label": "Priya", "first_name": "Priya", "username": "priya.r"},
    {"id": 100000004, "label": "Sam (on-call)", "first_name": "Sam", "username": "sam_oncall"},
    {"id": 100000005, "label": "Lena", "first_name": "Lena", "username": ""},
]
UID = {u["first_name"]: u["id"] for u in USERS}
BOTS = {  # name -> (token, allowlist)
    "alerts": ("7712345601:AAFdemoAlertsTokenNotReal0000000001", ["Maya", "Sam"]),
    "homelab": ("7712345602:AAFdemoHomelabTokenNotReal000000002", ["Maya", "Jonas", "Sam"]),
    "family": ("7712345603:AAFdemoFamilyTokenNotReal0000000003", ["Maya", "Jonas", "Priya", "Lena"]),
}
P_HOME, P_ERRANDS, P_WORK = "6Xm5HVVRcX00", "6Xm5HVVRcX01", "6Xm5HVVRcX02"
USED_OPS = {"todoist": {"tasks_add", "tasks_filter", "tasks_list", "quick_add", "task_close"},
            "kanbanflow": {"board_get", "tasks_add", "task_update"},
            "vikunja": {"projects_list", "tasks_add", "task_update"}}
KEYS = ["home-assistant", "backup-monitor", "ci-deploy", "n8n-workflows", "grocery-shortcut", "old-laptop"]
GRANTS = {  # key -> {action: scope or None}
    "home-assistant": {"telegram.send": {"bots": ["homelab"], "chat_ids": [UID["Maya"], UID["Jonas"]]},
                       "todoist.tasks_add": {"project_id": [P_HOME]}, "todoist.tasks_list": None,
                       "todoist.tasks_filter": None},
    "backup-monitor": {"telegram.send": {"bots": ["alerts"], "chat_ids": [UID["Sam"]]}},
    "ci-deploy": {"telegram.send": {"bots": ["alerts", "homelab"]}, "kanbanflow.tasks_add": None,
                  "kanbanflow.task_update": None, "kanbanflow.board_get": None},
    "n8n-workflows": {"telegram.read": {"bots": ["family"]}, "telegram.send": None,
                      "todoist.tasks_filter": None, "todoist.tasks_add": None, "todoist.quick_add": None,
                      "vikunja.projects_list": None, "vikunja.tasks_add": None},
    "grocery-shortcut": {"todoist.tasks_add": {"project_id": [P_ERRANDS]}, "todoist.quick_add": None},
    "old-laptop": {"todoist.tasks_list": None, "todoist.tasks_filter": None},
}
COMMANDS = [  # (bot, command, run as key, action, call template, reply template)
    ("family", "todo", "grocery-shortcut", "todoist.quick_add", {"body": {"text": "{text} #Errands"}},
     "Added: {body.content}"),
    ("homelab", "today", "home-assistant", "todoist.tasks_filter", {"query": {"query": "today | overdue"}},
     "{#body.results}- {content}\n{/body.results}"),
]


class Seeder:
    def __init__(self, app, clock: DemoClock) -> None:
        self.gw, self.clock = app.state.gw, clock
        self.http = httpx.Client(base_url="http://127.0.0.1:8000", headers={"Host": HOST}, timeout=30)
        self.csrf = ""
        self.keys: dict[str, str] = {}
        self.ids: dict[str, int] = {}
        self.update_id = 905000000

    def at(self, day: int, hhmm: str) -> None:
        h, m = map(int, hhmm.split(":"))
        s = (h * 7 + m * 13 + day * 29) % 60  # stable, not-round seconds
        self.clock.t = DAY0 - 7 * 3600 - 40 * 60 + day * 86400 + h * 3600 + m * 60 + s

    def admin(self, method: str, path: str, body=None) -> dict:
        r = self.http.request(method, f"/admin/api{path}", json=body,
                              headers={"X-CSRF-Token": self.csrf, "X-Forwarded-For": TAILNET_IP})
        assert r.status_code == 200, f"{method} {path}: {r.status_code} {r.text}"
        return r.json() if r.content else {}

    def call(self, key: str, method: str, path: str, body=None, ip: str = "203.0.113.24", expect: int = 200) -> None:
        r = self.http.request(method, f"/api/v1{path}", json=body,
                              headers={"Authorization": f"Bearer {self.keys[key]}", "X-Forwarded-For": ip})
        assert r.status_code == expect, f"{key} {path}: {r.status_code} {r.text}"

    def send(self, key, bot, who, text, **kw) -> None:
        self.call(key, "POST", "/telegram/send", {"bot": bot, "chat_id": UID[who], "text": text}, **kw)

    def svc(self, key, service, op, body=None, **kw) -> None:
        self.call(key, "POST", f"/call/{service}/{op}", body or {}, **kw)

    def inbound(self, bot: str, who: str, text: str, minutes_ago: float = 0.2) -> None:
        """Hand one message to the fake getUpdates and wait until the poller has stored it."""
        before = len(self.admin("GET", "/messages")["messages"])
        user = next(u for u in USERS if u["first_name"] == who)
        self.update_id += 1
        sender = {"id": user["id"], "is_bot": False, "first_name": who} | ({"username": user["username"]}
                                                                           if user["username"] else {})
        upd = {"update_id": self.update_id, "message": {"message_id": self.update_id % 10000, "from": sender,
               "chat": {"id": user["id"], "type": "private"}, "date": int(self.clock.t - minutes_ago * 60),
               "text": text}}
        with inbox_lock:
            inbox.setdefault(BOTS[bot][0], []).append(upd)
        for _ in range(200):
            if len(self.admin("GET", "/messages")["messages"]) > before:
                return
            time.sleep(0.05)
        raise RuntimeError(f"message not stored: {text}")

    def command(self, bot: str, who: str, text: str, action: str) -> None:
        """An inbound bot command; waits until it has run (its audit row exists) so the clock is still right."""
        before = len(self.admin("GET", f"/audit?action={action}")["rows"])
        self.inbound(bot, who, text)
        for _ in range(200):
            if len(self.admin("GET", f"/audit?action={action}")["rows"]) > before:
                return
            time.sleep(0.05)
        raise RuntimeError(f"command did not run: {text}")

    def run(self) -> None:
        while True:  # the browser does web setup first
            try:
                if self.http.get("/health").json()["state"] == "unsealed":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        self.at(0, "07:42")
        r = self.http.post("/admin/api/login", json={"password": "wrong-password-typo"},
                           headers={"X-Forwarded-For": TAILNET_IP})
        assert r.status_code in (401, 403), r.text
        self.at(0, "07:43")
        r = self.http.post("/admin/api/login", json={"password": ADMIN_PASSWORD}, headers={"X-Forwarded-For": TAILNET_IP})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf"]

        self.at(0, "07:46")
        self.admin("PUT", "/telegram/users", {"users": [{"id": u["id"], "label": u["label"]} for u in USERS]})
        for i, (name, (tok, allow)) in enumerate(BOTS.items()):
            self.at(0, f"07:{48 + i * 2}")
            b = self.admin("POST", "/bots", {"name": name, "token": tok})
            self.ids[name] = b["id"]
            self.admin("PUT", f"/bots/{b['id']}/allowlist", {"user_ids": [UID[w] for w in allow]})
        self.admin("PATCH", f"/bots/{self.ids['family']}", {"retention_days": 14})

        self.at(0, "08:02")
        services = {s["name"]: s for s in self.admin("GET", "/services")["services"]}
        self.admin("PUT", f"/services/{services['todoist']['id']}/credential", {"credential": "demo-todoist-token"})
        self.at(0, "08:04")
        self.admin("PUT", f"/services/{services['kanbanflow']['id']}/credential", {"credential": "demo-kanbanflow-token"})
        self.at(0, "08:07")
        v = self.admin("POST", "/packs/vikunja/import", {"base_url": "https://tasks.example.org/api/v1"})
        self.admin("PUT", f"/services/{v['id']}/credential", {"credential": "demo-vikunja-token"})
        self.at(0, "08:09")  # keep only the operations in use, as one would: a narrower permissions grid
        for name, sid in (("todoist", services["todoist"]["id"]), ("kanbanflow", services["kanbanflow"]["id"]),
                          ("vikunja", v["id"])):
            for op in self.admin("GET", f"/services/{sid}/operations")["operations"]:
                if op["name"] not in USED_OPS[name]:
                    self.admin("PATCH", f"/operations/{op['id']}", {"enabled": False})

        for i, k in enumerate(KEYS):
            self.at(0, f"08:{12 + i}")
            r = self.admin("POST", "/keys", {"name": k})
            self.keys[k], self.ids[k] = r["key"], r["id"]
            for action, scope in GRANTS[k].items():
                self.admin("PUT", f"/permissions/{r['id']}/{action}", {"scope": scope})

        self.at(0, "08:24")  # bot commands, each acting as one of the keys above
        for bot, cmd, key, action, call, reply in COMMANDS:
            self.admin("POST", "/commands", {"bot_id": self.ids[bot], "command": cmd, "key_id": self.ids[key],
                                             "action": action, "call": call, "reply": reply})

        # Day 0: first traffic
        self.at(0, "08:31"); self.send("backup-monitor", "alerts", "Sam", "Backup test run OK", ip="198.51.100.40")
        self.at(0, "08:33"); self.inbound("family", "Maya", "is this the new bot?")
        self.at(0, "09:15"); self.svc("grocery-shortcut", "todoist", "tasks_add", {"body": {"content": "Oat milk", "project_id": P_ERRANDS}}, ip="203.0.113.77")
        self.at(0, "09:16"); self.svc("grocery-shortcut", "todoist", "tasks_add", {"body": {"content": "Coffee beans", "project_id": P_ERRANDS}}, ip="203.0.113.77")
        self.at(0, "10:02"); self.svc("ci-deploy", "kanbanflow", "tasks_add", {"body": {"name": "Release v2.3.0", "columnId": "col-review"}}, ip="198.51.100.12")
        self.at(0, "10:05"); self.send("ci-deploy", "alerts", "Maya", "Deploy v2.3.0 to staging: success (2m 41s)", ip="198.51.100.12")
        self.at(0, "11:20"); self.inbound("homelab", "Jonas", "NAS fan is loud again, can you check?")
        self.at(0, "11:24"); self.send("home-assistant", "homelab", "Jonas", "NAS: fan 2 at 3100 rpm, CPU 61 °C", ip="203.0.113.24")
        self.at(0, "12:40"); self.svc("old-laptop", "todoist", "tasks_list", {"query": {"project_id": P_WORK}}, ip="203.0.113.150")
        self.at(0, "13:02"); self.svc("grocery-shortcut", "todoist", "tasks_add", {"body": {"content": "Printer paper", "project_id": P_WORK}}, ip="203.0.113.77", expect=403)
        self.at(0, "14:30"); self.inbound("family", "Priya", "Dinner at ours on Saturday? 7pm")
        self.at(0, "14:41"); self.inbound("family", "Lena", "Yes! I'll bring dessert")
        self.at(0, "15:10"); self.call("n8n-workflows", "GET", "/telegram/messages?bot=family", ip="198.51.100.88")
        self.at(0, "15:11"); self.svc("n8n-workflows", "todoist", "tasks_add", {"body": {"content": "Dinner Saturday 19:00 (Priya)", "due_string": "saturday 7pm"}}, ip="198.51.100.88")
        self.at(0, "17:55"); self.svc("ci-deploy", "kanbanflow", "task_update", {"path": {"task_id": "t-31"}, "body": {"columnId": "col-done"}}, ip="198.51.100.12")
        self.at(0, "18:00"); self.send("ci-deploy", "homelab", "Maya", "Deploy v2.3.0 to production: success", ip="198.51.100.12")
        self.at(0, "21:47"); self.send("home-assistant", "homelab", "Lena", "Front door opened", ip="203.0.113.24", expect=403)

        # Day 1
        self.at(1, "02:00"); self.send("backup-monitor", "alerts", "Sam", "Backup finished: 41.8 GB in 17 min", ip="198.51.100.40")
        self.at(1, "06:58"); self.inbound("alerts", "Sam", "ack, thanks")
        self.at(1, "07:30"); self.svc("n8n-workflows", "todoist", "tasks_filter", {"query": {"query": "today | overdue"}}, ip="198.51.100.88")
        self.at(1, "07:31"); self.send("n8n-workflows", "family", "Maya", "Today: 4 tasks (1 overdue): Renew passport", ip="198.51.100.88")
        self.at(1, "08:12"); self.inbound("homelab", "Maya", "restart the media server please")
        self.at(1, "08:14"); self.send("home-assistant", "homelab", "Maya", "Media server restarted, up in 38 s", ip="203.0.113.24")
        self.at(1, "09:03"); self.svc("old-laptop", "todoist", "tasks_filter", {"query": {"query": "@work"}}, ip="203.0.113.150")
        self.at(1, "09:20"); self.admin("DELETE", f"/keys/{self.ids['old-laptop']}")
        self.at(1, "09:41"); self.svc("old-laptop", "todoist", "tasks_filter", {"query": {"query": "@work"}}, ip="203.0.113.150", expect=401)
        self.at(1, "10:15"); self.svc("n8n-workflows", "vikunja", "projects_list", {}, ip="198.51.100.88")
        self.at(1, "10:16"); self.svc("n8n-workflows", "vikunja", "tasks_add", {"path": {"project_id": 3}, "body": {"title": "Quarterly invoice to accountant"}}, ip="198.51.100.88")
        self.at(1, "12:02"); self.inbound("family", "Jonas", "who's picking up the kids today?")
        self.at(1, "12:09"); self.inbound("family", "Maya", "me, leaving at 3")
        self.at(1, "13:30"); self.svc("grocery-shortcut", "todoist", "quick_add", {"body": {"text": "Batteries AA #Errands"}}, ip="203.0.113.77")
        self.at(1, "14:12"); self.command("family", "Priya", "/todo Birthday card for Lena", "todoist.quick_add")
        self.at(1, "15:47"); self.send("ci-deploy", "alerts", "Maya", "Build #1842 failed: 2 tests (test_sync, test_retry)", ip="198.51.100.12")
        self.at(1, "15:52"); self.inbound("alerts", "Maya", "on it")
        self.at(1, "16:20"); self.send("ci-deploy", "alerts", "Maya", "Build #1843 passed", ip="198.51.100.12")
        self.at(1, "19:05"); self.inbound("family", "Lena", "Shopping list: eggs, basil, parmesan")
        self.at(1, "19:06"); self.call("n8n-workflows", "GET", "/telegram/messages?bot=family", ip="198.51.100.88")
        for i, item in enumerate(("Eggs", "Basil", "Parmesan")):
            self.at(1, f"19:0{7 + i}")
            self.svc("n8n-workflows", "todoist", "tasks_add", {"body": {"content": item, "project_id": P_ERRANDS}}, ip="198.51.100.88")
        self.at(1, "19:11"); self.send("n8n-workflows", "family", "Lena", "Added 3 items to Errands", ip="198.51.100.88")

        # Day 2 (today in the screenshots)
        self.at(2, "02:00"); self.send("backup-monitor", "alerts", "Sam", "Backup finished: 42.1 GB in 18 min", ip="198.51.100.40")
        self.at(2, "06:40"); self.send("home-assistant", "homelab", "Maya", "Morning: 4 °C, rain from 14:00. Take the umbrella.", ip="203.0.113.24")
        self.at(2, "07:02"); self.inbound("homelab", "Maya", "thanks!")
        self.at(2, "07:30"); self.svc("n8n-workflows", "todoist", "tasks_filter", {"query": {"query": "today | overdue"}}, ip="198.51.100.88")
        self.at(2, "07:31"); self.send("n8n-workflows", "family", "Maya", "Today: 6 tasks: Eggs, Basil, Parmesan, …", ip="198.51.100.88")
        self.at(2, "08:45"); self.svc("home-assistant", "todoist", "tasks_add", {"body": {"content": "Replace hallway bulb", "project_id": P_HOME}}, ip="203.0.113.24")
        self.at(2, "09:10"); self.svc("home-assistant", "todoist", "tasks_add", {"body": {"content": "Order printer ink", "project_id": P_WORK}}, ip="203.0.113.24", expect=403)
        self.at(2, "09:58"); self.inbound("family", "Priya", "Running 10 min late, start without me")
        self.at(2, "10:30"); self.svc("ci-deploy", "kanbanflow", "board_get", {}, ip="198.51.100.12")
        self.at(2, "10:32"); self.svc("ci-deploy", "kanbanflow", "tasks_add", {"body": {"name": "Release v2.3.1", "columnId": "col-review"}}, ip="198.51.100.12")
        self.at(2, "10:41"); self.send("ci-deploy", "alerts", "Maya", "Deploy v2.3.1 to staging: success (2m 37s)", ip="198.51.100.12")
        self.at(2, "10:44"); self.inbound("alerts", "Maya", "looks good, ship it")
        self.at(2, "10:52"); self.send("ci-deploy", "homelab", "Maya", "Deploy v2.3.1 to production: success", ip="198.51.100.12")
        self.at(2, "11:05"); self.call("n8n-workflows", "GET", "/telegram/messages?bot=family", ip="198.51.100.88")
        self.at(2, "11:20"); self.svc("grocery-shortcut", "todoist", "tasks_add", {"body": {"content": "Dish soap", "project_id": P_ERRANDS}}, ip="203.0.113.77")
        self.at(2, "11:27"); self.command("homelab", "Maya", "/today", "todoist.tasks_filter")
        self.at(2, "11:31"); self.command("family", "Lena", "/todo Basil plant", "todoist.quick_add")
        self.at(2, "11:34"); self.inbound("homelab", "Jonas", "printer is offline again")
        self.at(2, "11:36"); self.send("home-assistant", "homelab", "Jonas", "Printer: power-cycled the smart plug, back online", ip="203.0.113.24")
        self.at(2, "11:48")
        os.close(os.open(SEEDED, os.O_CREAT | os.O_WRONLY, 0o600))
        print("demo data seeded", flush=True)


def main() -> None:
    clock = DemoClock(DAY0)
    settings = DemoSettings.from_env().with_(trusted_proxy_ips=("127.0.0.1",))
    app = create_app(settings, clock=clock, transport=httpx.MockTransport(fake_upstream))
    seeder = Seeder(app, clock)

    def seed() -> None:
        try:
            seeder.run()
        except Exception as e:  # noqa: BLE001 - make the failure visible, the runner waits for the marker
            print(f"SEED FAILED: {e!r}", flush=True)
            os._exit(1)

    threading.Thread(target=seed, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=False, server_header=False, log_level="warning")


if __name__ == "__main__":
    main()
