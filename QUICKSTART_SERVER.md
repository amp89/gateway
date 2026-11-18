# Quickstart: server

Deploy the gateway on one machine, put the client API on a Cloudflare Tunnel hostname, and keep the admin UI on your tailnet.
Commands use podman. For docker, swap `podman-` for `docker-` in the script names.

## 1. Prerequisites
- Linux host with one of:
  - podman ≥ 4.9 and `podman-compose` (`sudo apt install podman podman-compose`)
  - docker with the compose plugin
- `cloudflared` set up with a tunnel (client API)
- Tailscale on the host (admin UI)
- A Telegram bot token from [@BotFather](https://t.me/BotFather) (`/newbot`)
- Your Telegram user ID: message [@userinfobot](https://t.me/userinfobot)
- Optional: a Todoist API token (Todoist → Settings → Integrations → Developer)

## 2. First start
```sh
./podman-start-prod.sh
```
- The first run copies `.env.example` to `.env` unchanged and stops. Nothing is generated.
- Edit `.env`:
  - `API_PREFIX`, `ADMIN_PREFIX`: your path prefixes, used exactly as written (one segment: letters, digits, `-`, `_`; must differ; hard to guess is better)
  - `API_HOSTS=api.example.com` (your Cloudflare hostname)
  - `ADMIN_HOSTS=gateway.your-tailnet.ts.net` (your Tailscale name), plus `127.0.0.1` if you want local access
  - hosts = the hostname in the URL clients use (Host header), any names you choose; other names get a bare 404
- Start again to apply: `./podman-start-prod.sh`
  - Until setup is done it prints a one-time setup link (`…/<ADMIN_PREFIX>/#setup-code=…`) and the bare code. Keep it for step 4.
- The gateway now listens on `http://127.0.0.1:38080` (`GATEWAY_PORT`) only. Caddy answers that port; the backend port is not published.

## 3. Expose it
### Admin UI on Tailscale
```sh
sudo tailscale serve --bg --https=443 http://127.0.0.1:38080
```
- Open `https://gateway.your-tailnet.ts.net/<ADMIN_PREFIX>/` (the prefix is in `.env`).
- Admin routes answer only on `ADMIN_HOSTS`; everything else gets an empty 404.

### Client API on Cloudflare Tunnel
In the cloudflared config (`~/.cloudflared/config.yml` or the dashboard):
```yaml
ingress:
  - hostname: api.example.com
    service: http://127.0.0.1:38080
  - service: http_status:404
```
- Only `/<API_PREFIX>/…` answers on `API_HOSTS`. The admin UI is unreachable through the tunnel.
- `cloudflared tunnel run <name>` (or its systemd service).

## 4. First run in the browser
1. Open the setup link from the start script. The setup page asks for:
   - **Setup code**: already filled in by the link. Plain admin URL instead: paste the code, or get it with `podman exec gateway cat /run/gateway/setup.code`. Without it nobody else who reaches the admin URL can claim the gateway. It is new on every restart and gone after setup.
   - **Master passphrase** (12+ chars): encrypts every stored token and message. It is never saved. **If you lose it, the stored secrets cannot be recovered.**
   - **Admin password** (10+ chars): for logging in to the UI.
2. Log in.
3. **Bots**:
   - **Telegram users**: add your user ID (and anyone else your bots may talk to), with a label.
   - **Telegram bots**: add your bot (name + token), tick who it may talk to, Save allowlist.
4. **Services**: `todoist` is already there (9 operations, imported on setup). Open it → **Set credential** → paste your Todoist token.
   - Until then, Todoist calls answer `502 upstream_error "service credential not set"`.
   - `kanbanflow` is there too (token: KanbanFlow → board → Settings → API tokens).
   - Vikunja (self-hosted): **Bundled packs** → vikunja → **Add…** → your server's API URL (e.g. `https://vikunja.example.com/api/v1`); token: Vikunja → Settings → API tokens.
   - A second account of the same app: **Add…** again under another name (`todoist2`); each has its own credential and permissions.
   - Adding never changes an existing service. Deleted one? **Add…** brings it back.
5. **Keys**: create a key. Copy it now: it is shown once.
6. **Permissions**: tick the actions the key may use, e.g. `telegram.send`, `todoist.tasks_add`, `todoist.tasks_filter`. A cell reads off / all / limited; "limit…" lets you pick which bots, Telegram users or Todoist projects it may use.

Then follow [QUICKSTART_CLIENT.md](QUICKSTART_CLIENT.md).

## 5. After every restart: unlock
The gateway starts **sealed**: the API answers `503 sealed` and polling waits until you unlock it.
- On the server: `./unlock.sh` (no-echo prompt; works with podman or docker), or
- in the admin UI: the unlock page.

`./podman-start-prod.sh` offers the unlock prompt itself when run in a terminal.
Wrong passphrases are counted: after 5, unlock locks for 30 s, doubling up to 1 h.

## 6. Day to day
| Task | How |
|---|---|
| Logs | `podman logs -f gateway` · `podman logs -f gateway-caddy` |
| Stop | `./podman-stop-prod.sh` |
| Change admin password | UI → Settings |
| Change master passphrase | UI → Settings (re-encrypts everything in one transaction) |
| Audit retention | UI → Settings (`0` = keep forever); cleanup runs hourly, or Settings → Cleanup → Run now |
| Message retention | UI → Bots → per bot |
| Revoke a key | UI → Keys → Revoke (takes effect immediately) |
| Bot command (e.g. `/todo`) | UI → Keys: create a key → Permissions: tick the operation for it → Commands → Add command (bot, name, operation, key). Works in private chats with the bot, from users on its allowlist |

## 7. Backup and restore
Everything lives in the `gateway-data` volume. The secrets in it are encrypted, so a backup is useless without the passphrase. Keep the passphrase somewhere separate.
```sh
./podman-stop-prod.sh
podman volume export gateway-data > gateway-backup-$(date +%F).tar
./podman-start-prod.sh
```
Restore:
```sh
./podman-stop-prod.sh
podman volume rm gateway-data && podman volume create gateway-data
podman volume import gateway-data gateway-backup-YYYY-MM-DD.tar
./podman-start-prod.sh
```
Docker: `docker run --rm -v gateway-data:/d -v "$PWD":/b docker.io/library/python@sha256:89fb7d3da20043c370643435258bdd7ab755d326d359001d02988ed15ae5219e tar cf /b/gateway-backup.tar -C /d .`

Also back up `.env` (prefixes and hosts).

## 8. Update
1. Get the new code (the image digests are pinned in `Containerfile` and `compose.yaml`).
2. Run `./podman-start-prod.sh`. It rebuilds the images; migrations run at start; data stays.
3. Unlock.

## 9. Troubleshooting
| Symptom | Cause |
|---|---|
| Empty 404 everywhere | wrong prefix, or the host is not in `API_HOSTS` / `ADMIN_HOSTS` |
| `503 {"error":"sealed"}` | not unlocked since the last start |
| `401 unauthorized` | key missing, wrong, disabled or revoked |
| `403 forbidden` | key lacks the action, scope mismatch, unknown bot/service, or chat not on the bot's allowlist |
| Bot shows "polling stopped: unauthorized" | Telegram rejected the token: replace it, then re-enable polling |
| Allowlist save fails with `unknown_user` | add the ID under Bots → Telegram users first |
| `502 … service credential not set` | Services → the service → Set credential |
