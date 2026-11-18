# Quickstart: client

Call the gateway from any Python 3.9+ script using only the standard library. Nothing to `pip install`.

## 1. What you need
- **Base URL**: `https://api.example.com/<API_PREFIX>/v1` (the hostname from `API_HOSTS`, the prefix from the server's `.env`)
- **Gateway key**: `gw_…`, created in the admin UI (Keys) and granted actions (Permissions)

Export them:
```sh
export GATEWAY_URL="https://api.example.com/<API_PREFIX>/v1"
export GATEWAY_KEY="gw_..."
```

## 2. Client code
Save as `gateway_client.py`:
```python
"""Minimal gateway client: standard library only."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ["GATEWAY_URL"].rstrip("/")
KEY = os.environ["GATEWAY_KEY"]


class GatewayError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"HTTP {status}: {body}")


def request(method, path, body=None, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "User-Agent": "gateway-client/1",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            err = json.loads(raw)
        except ValueError:
            err = {"error": "not_found" if e.code == 404 else raw.decode(errors="replace")}
        raise GatewayError(e.code, err) from None


def me():
    """The key's name and the actions it may use."""
    return request("GET", "/me")


def send(bot, chat_id, text, parse_mode=None, silent=False):
    """telegram.send -> {"message_id": ...}"""
    body = {"bot": bot, "chat_id": chat_id, "text": text, "silent": silent}
    if parse_mode:
        body["parse_mode"] = parse_mode  # "HTML" or "MarkdownV2"
    return request("POST", "/telegram/send", body)


def read(bot, since=0, limit=50):
    """telegram.read -> {"messages": [...], "next_since": id}"""
    return request("GET", "/telegram/messages", params={"bot": bot, "since": since, "limit": limit})


def call(service, operation, path=None, query=None, body=None):
    """Any declared operation, e.g. call("todoist", "tasks_add", body={...}) -> {"status", "body"}"""
    payload = {"path": path or {}, "query": query or {}}
    if body is not None:
        payload["body"] = body
    return request("POST", f"/call/{service}/{operation}", payload)


def todoist_add(content, due_string=None, project_id=None):
    body = {"content": content}
    if due_string:
        body["due_string"] = due_string
    if project_id:
        body["project_id"] = project_id
    return call("todoist", "tasks_add", body=body)["body"]


if __name__ == "__main__":
    print(me())
```

## 3. Use it
```python
from gateway_client import GatewayError, read, send, todoist_add

# Send a Telegram message (the chat must be on the bot's allowlist)
send("alerts", 123456789, "Backup finished ✅")

# Read new inbound messages, remembering where you stopped
since = 0
batch = read("alerts", since=since)
for m in batch["messages"]:
    print(m["sender"].get("username"), m["text"])
since = batch["next_since"]

# Add a Todoist task through the generic call endpoint
task = todoist_add("Renew domain", due_string="next friday")
print(task["id"])

# Errors
try:
    send("alerts", 999, "not allowed")
except GatewayError as e:
    print(e.status, e.body)  # 403 {'error': 'forbidden'}
```

## 4. Errors
| Status | `error` | Meaning |
|---|---|---|
| 400 | `bad_request` | invalid body, or a path/query/body field the operation does not declare |
| 401 | `unauthorized` | key missing, wrong, disabled or revoked |
| 403 | `forbidden` | action not granted, scope mismatch, unknown bot/service, chat not allowed |
| 404 | (empty body) | wrong URL prefix or host |
| 429 | `rate_limited` | too many calls; wait `retry_after` seconds |
| 502 | `upstream_error` | Telegram/Todoist refused; `status` and `detail` say why |
| 503 | `sealed` | the gateway restarted and is not unlocked yet |
| 504 | `upstream_timeout` | upstream did not answer in time |

## 5. Generic calls in brief
`POST /call/{service}/{operation}` with:
- `path`: values for `{placeholders}` in the operation's path (letters, digits, `_ . -` only)
- `query`: query parameters (only the declared ones)
- `body`: JSON body (only the declared top-level fields)

Response: `{"status": <upstream status>, "body": <upstream JSON or text>}`.

The Todoist pack (imported on first setup; set its credential under Services):

| Operation | Example |
|---|---|
| `projects_list` | `call("todoist", "projects_list")` |
| `labels_list` | `call("todoist", "labels_list")` |
| `tasks_list` | `call("todoist", "tasks_list", query={"project_id": "6X…"})` |
| `tasks_filter` | `call("todoist", "tasks_filter", query={"query": "today \| overdue"})` |
| `task_get` | `call("todoist", "task_get", path={"task_id": "6X…"})` |
| `tasks_add` | `call("todoist", "tasks_add", body={"content": "x", "priority": 4})` |
| `quick_add` | `call("todoist", "quick_add", body={"text": "Pay rent tomorrow 9am #Home @money"})` |
| `task_close` | `call("todoist", "task_close", path={"task_id": "6X…"})` |
| `task_delete` | `call("todoist", "task_delete", path={"task_id": "6X…"})` |

Todoist list responses are paginated: pass `query={"cursor": body["next_cursor"]}` for the next page.

KanbanFlow and Vikunja work the same way (the service name is what you called it in the admin UI):
```python
board = call("kanbanflow", "board_get")["body"]          # column IDs
call("kanbanflow", "tasks_add", body={"name": "Buy milk", "columnId": board["columns"][0]["uniqueId"]})
call("vikunja", "tasks_list", query={"filter": "done = false"})
call("vikunja", "tasks_add", path={"project_id": 1}, body={"title": "Buy milk"})
```

## 6. Try it in dev with curl
`./podman-start-dev.sh` on a fresh dev volume prints a demo key once:
```
DEV: demo key dev-demo = gw_… (todoist read + tasks_add; shown once)
```
- It may call the Todoist read operations and `tasks_add`.
- Set the Todoist credential first: `http://127.0.0.1:38082/admin/` → Services → todoist → Set credential.
- Lost the key? Create a new one under Keys (the demo key is only made on a fresh dev volume).
- Keys → "Code" on any key: a ready client (Python stdlib or sh + curl) with one function per action that key may use, pointed at your API URL from `.env`. Save it on the calling machine, `export GATEWAY_KEY=gw_...`, run it.

```sh
KEY=gw_...
curl -s -X POST http://127.0.0.1:38081/api/v1/call/todoist/tasks_filter \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query": {"query": "today | overdue"}}'
# -> {"status": 200, "body": {"results": [...], "next_cursor": null}}

curl -s -X POST http://127.0.0.1:38081/api/v1/call/todoist/tasks_add \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"body": {"content": "Try the gateway", "due_string": "today"}}'
```
