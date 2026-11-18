"""Headless click-through of the admin UI against a fresh dev-mode gateway (run by podman-ui-check.sh).

Fails on: a raw error code on screen, an unexpected error message, a browser console error, a missing element.
Screenshots of every page land in $OUT.
"""
import json
import os
import re
import subprocess
import sys

from playwright.sync_api import expect, sync_playwright

BASE = os.environ["BASE"].rstrip("/")
OUT = os.environ.get("OUT", "/out")
RAW_CODES = re.compile(r"\b(bad_request|name_taken|not_found|unknown_user|upstream_error|csrf)\b")
BOT_TOKEN = "123456789:AAH-uicheck_token_value_abcdefghijk"
steps: list[str] = []
console_errors: list[str] = []


def step(name: str) -> None:
    steps.append(name)
    print(f"- {name}", flush=True)


def shot(page, name: str) -> None:
    page.screenshot(path=f"{OUT}/{len(steps):02d}-{name}.png", full_page=True)


def no_raw_codes(page) -> None:
    text = page.locator("main").inner_text()
    m = RAW_CODES.search(text)
    assert m is None, f"raw error code {m.group(0)!r} on screen: {text[max(0, m.start() - 80):m.end() + 80]!r}"


def go(page, hash_: str) -> None:
    page.goto(f"{BASE}/admin/#/{hash_}")
    page.wait_for_load_state("networkidle")


JUNK = ("BOT_NAME", "CHAT_ID\"", "CURSOR", "LIMIT\"", "localhost:38082", "38081")


def open_menu(page, scope, menu: str, shot_name: str | None = None):
    """Open a Code menu; it must be fully on screen (not clipped by the table's scroll box)."""
    scope.get_by_role("button", name=menu, exact=True).click()
    lst = page.locator(".menu-list")
    expect(lst).to_be_visible()
    box, vp = lst.bounding_box(), page.viewport_size
    assert box and box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= vp["width"] \
        and box["y"] + box["height"] <= vp["height"], f"menu off screen: {box} viewport {vp}"
    for item in ("Copy Python (stdlib)", "Copy curl (sh)", "View code…"):
        expect(lst.get_by_role("menuitem", name=item)).to_be_visible()
    if shot_name:
        shot(page, shot_name)
    return lst


def view_code(page, scope, menu: str, stem: str, shot_name: str | None = None) -> tuple[str, str]:
    """Code menu → View code…: read both tabs, save them as $OUT/<stem>/gateway_client.py + gateway.sh, close."""
    open_menu(page, scope, menu).get_by_role("menuitem", name="View code…").click()
    dlg = page.get_by_role("dialog")
    os.makedirs(f"{OUT}/{stem}", exist_ok=True)
    out = []
    for tab, fname in (("Python (stdlib)", "gateway_client.py"), ("curl (sh)", "gateway.sh")):
        dlg.get_by_role("tab", name=tab).click()
        expect(dlg.get_by_role("tab", name=tab)).to_have_attribute("aria-selected", "true")
        code = dlg.locator("pre.code").inner_text()
        for junk in JUNK:
            assert junk not in code, f"{fname} contains {junk!r}:\n{code}"
        with open(f"{OUT}/{stem}/{fname}", "w") as f:
            f.write(code)
        if shot_name:
            shot(page, f"{shot_name}-{fname.split('.')[-1]}")
        out.append(code)
    dlg.get_by_role("button", name="Close", exact=True).click()
    expect(dlg).to_have_count(0)
    return out[0], out[1]


def run_client(stem: str, args: list[str], key: str | None = None) -> str:
    """Run the saved client in its own directory, as on another machine (this runner is not the gateway host)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")} | ({"GATEWAY_KEY": key} if key else {})
    r = subprocess.run(args, capture_output=True, text=True, timeout=60, env=env, cwd=f"{OUT}/{stem}")
    return f"rc={r.returncode}\nout={r.stdout}\nerr={r.stderr}"


def client_me(stem: str, key_name: str, key: str | None = None) -> None:
    for args in (["python3", "gateway_client.py"], ["sh", "gateway.sh"]):
        res = run_client(stem, args, key)
        assert res.startswith("rc=0") and f'"key": "{key_name}"' in res.replace('":"', '": "'), f"{args}: {res}"


FROZEN_JS = """() => {
  const box = document.querySelector('.matrix-wrap');
  box.scrollLeft = box.scrollWidth; box.scrollTop = box.scrollHeight;
  const b = box.getBoundingClientRect(), r = (el) => el.getBoundingClientRect();
  const corner = box.querySelector('thead th.key'), grp = box.querySelector('thead th.grp:last-child');
  const op = box.querySelector('thead tr + tr th:last-child');
  const keys = [...box.querySelectorAll('tbody td.key')], last = keys[keys.length - 1];
  const at = (el) => { const q = r(el); return document.elementFromPoint(q.left + 6, q.top + q.height / 2); };
  return {
    overflowX: box.scrollWidth > box.clientWidth, overflowY: box.scrollHeight > box.clientHeight,
    scrolled: box.scrollLeft > 0 && box.scrollTop > 0,
    boxTop: b.top + box.clientTop, boxLeft: b.left + box.clientLeft,
    grpName: grp.textContent, grpNameLeft: r(grp.firstElementChild).left, cornerRight: r(corner).right,
    cornerTop: r(corner).top, cornerLeft: r(corner).left, grpTop: r(grp).top,
    grpBottom: r(grp).bottom, opTop: r(op).top,
    keyLeft: r(last).left, keyText: last.textContent, keyBottom: r(last).bottom, boxBottom: b.bottom,
    cornerHit: corner.contains(at(corner)), keyHit: last.contains(at(last)), opHit: op.contains(at(op)),
  };
}"""


def check_frozen(page) -> None:
    """Scroll the matrix to its bottom-right: header rows stay at the box top, the Key column at its left,
    and nothing scrolled underneath paints over them."""
    go(page, "permissions")
    page.locator(".matrix-wrap").scroll_into_view_if_needed()
    f = page.evaluate(FROZEN_JS)
    near = lambda a, b: abs(a - b) <= 1.5  # noqa: E731
    assert f["overflowX"] and f["overflowY"] and f["scrolled"], f"matrix must overflow both ways to test this: {f}"
    assert near(f["cornerTop"], f["boxTop"]) and near(f["grpTop"], f["boxTop"]), f"header row 1 not at top: {f}"
    assert near(f["opTop"], f["grpBottom"]), f"header row 2 not under row 1: {f}"
    assert near(f["cornerLeft"], f["boxLeft"]) and near(f["keyLeft"], f["boxLeft"]), f"Key column not at left: {f}"
    assert f["grpNameLeft"] >= f["cornerRight"], f"service name {f['grpName']!r} hidden under the Key column: {f}"
    assert f["keyBottom"] <= f["boxBottom"] + 1.5, f"last key row not visible: {f}"
    assert f["cornerHit"] and f["keyHit"] and f["opHit"], f"something paints over frozen cells: {f}"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
                and console_errors.append(m.text))
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on("dialog", lambda d: d.accept())

        step("login with the dev password")
        page.goto(f"{BASE}/admin/")
        page.get_by_label("Password").fill("asdf")
        page.get_by_role("button", name="Log in").click()
        expect(page.get_by_role("link", name="Permissions")).to_be_visible()

        step("keys: create one, secret shown once, code menu, rename modal")
        go(page, "keys")
        page.get_by_label("New key name").fill("uicheck")
        page.get_by_role("button", name="Create").click()
        expect(page.locator(".secret")).to_contain_text("gw_")
        secret = page.locator(".secret").inner_text().strip()
        fresh_box = page.locator(".card", has=page.locator(".secret"))
        py, sh = view_code(page, fresh_box, "Code with this key", "client-fresh", "keys-fresh-code")
        assert secret in py and "urllib.request" in py and "no actions allowed yet" in py, py
        assert 'BASE = os.environ.get("GATEWAY_URL", "http://gateway-uicheck:8000/api/v1")' in py, py
        assert secret in sh and "curl" in sh, sh
        client_me("client-fresh", "uicheck")
        row = page.locator("tr", has_text="uicheck")
        open_menu(page, row, "Code", "keys-menu-open").get_by_role("menuitem", name="Copy Python (stdlib)").click()
        expect(row.get_by_role("button", name="Copied ✓")).to_be_visible()
        shot(page, "keys")
        page.get_by_role("button", name="Done").click()
        page.locator("tr", has_text="uicheck").get_by_role("button", name="Rename").click()
        dlg = page.get_by_role("dialog", name="Rename key uicheck")
        dlg.get_by_label("Name").fill("uicheck2")
        shot(page, "keys-rename")
        dlg.get_by_role("button", name="Save").click()
        expect(dlg).to_have_count(0)
        page.locator("tr", has_text="uicheck2").get_by_role("button", name="Rename").click()
        dlg = page.get_by_role("dialog", name="Rename key uicheck2")
        dlg.get_by_label("Name").fill("uicheck")
        dlg.get_by_role("button", name="Save").click()
        expect(dlg).to_have_count(0)
        no_raw_codes(page)

        step("bots: add a Telegram user and a bot, allow the user")
        go(page, "bots")
        page.get_by_label("User ID").fill("111222333")
        page.get_by_label("Label (optional)").fill("me")
        page.get_by_role("button", name="Add user").click()
        expect(page.locator("td", has_text="111222333")).to_be_visible()
        page.get_by_label("Name (a-z, 0-9, _ -)").fill("alerts")
        page.get_by_label("Bot token").fill(BOT_TOKEN)
        page.get_by_role("button", name="Add bot").click()
        card = page.locator(".card", has=page.get_by_role("heading", name=re.compile("^alerts")))
        card.get_by_label("me", exact=True).check()
        card.get_by_role("button", name="Save allowlist").click()
        expect(card).to_contain_text("Saved: me")
        page.get_by_label("User ID").fill("444")
        shot(page, "bots")
        no_raw_codes(page)

        step("services: add modal, bad URL explains itself inside it")
        go(page, "services")
        expect(page.locator("tr", has_text="todoist")).to_contain_text("9")
        page.get_by_role("button", name="Add service").click()
        add = page.get_by_role("dialog", name="Add service")
        add.get_by_label(re.compile("^Name: lowercase")).fill("demo")
        add.get_by_label(re.compile("^Base URL")).fill("http://api.example.com")
        add.get_by_role("button", name="Add", exact=True).click()
        expect(add.locator(".error")).to_contain_text("must be https://host")
        shot(page, "services-bad-url")
        no_raw_codes(page)

        step("services: new service opens its modal, flagged until it has an operation")
        add.get_by_label(re.compile("^Base URL")).fill("https://api.example.com/v1")
        add.get_by_role("button", name="Add", exact=True).click()
        svc = page.get_by_role("dialog", name="Service demo")
        expect(svc.get_by_text("Keys can't use this service yet")).to_be_visible()
        svc.get_by_role("button", name="Close", exact=True).click()
        expect(page.locator("tr", has_text="api.example.com")).to_contain_text("0 — add one")
        go(page, "permissions")
        expect(page.get_by_text(re.compile("Not shown here: demo"))).to_be_visible()
        go(page, "services")
        page.locator("tr", has_text="api.example.com").click()
        svc = page.get_by_role("dialog", name="Service demo")
        svc.get_by_role("button", name="Add operation").click()
        opd = page.get_by_role("dialog", name="Add operation to demo")
        opd.get_by_label("Name", exact=True).fill("list_items")
        opd.get_by_label(re.compile("^Path")).fill("/items/{item_id}")
        shot(page, "services-op-modal")
        opd.get_by_role("button", name="Add operation").click()
        expect(opd).to_have_count(0)
        expect(svc.get_by_text("Keys can't use this service yet")).to_have_count(0)
        expect(svc.locator("td", has_text="list_items").first).to_be_visible()

        step("services: edit settings, credential and an operation in the modal")
        svc.get_by_label("Auth").select_option("header")
        svc.get_by_label("Header name").fill("X-Api-Key")
        svc.get_by_role("button", name="Save settings").click()
        expect(svc.get_by_role("status")).to_have_text("Saved")
        svc.get_by_label(re.compile("^Not set")).fill("demo-credential-value")
        svc.get_by_role("button", name="Save credential").click()
        expect(svc.get_by_role("status")).to_have_text("Credential saved")
        expect(svc.get_by_text("No credential yet")).to_have_count(0)
        svc.locator("tr", has_text="list_items").get_by_role("button", name="Edit").click()
        opd = page.get_by_role("dialog", name="Edit demo.list_items")
        opd.get_by_label(re.compile("^Description")).fill("List items")
        opd.get_by_role("button", name="Save").click()
        expect(opd).to_have_count(0)
        expect(svc.locator("tr", has_text="list_items")).to_contain_text("List items")
        assert "demo-credential-value" not in page.content()
        shot(page, "services-modal")
        page.keyboard.press("Escape")
        expect(svc).to_have_count(0)
        expect(page.locator("tr", has_text="api.example.com")).to_contain_text("header (X-Api-Key)")
        expect(page.locator("tr", has_text="api.example.com")).to_contain_text("set")
        shot(page, "services")
        no_raw_codes(page)

        step("services: bundled packs add under a new name, never overwrite")
        bundled = page.locator(".card", has=page.get_by_role("heading", name="Bundled packs"))
        expect(bundled.locator("tr", has_text="todoist")).to_contain_text("todoist")
        expect(bundled.locator("tr", has_text="kanbanflow")).to_contain_text("kanbanflow")
        expect(bundled.locator("tr", has_text="vikunja")).to_contain_text("self-hosted")
        bundled.locator("tr", has_text="todoist").get_by_role("button", name="Add…").click()
        addp = page.get_by_role("dialog", name="Add todoist")
        expect(addp.get_by_label(re.compile("^Service name"))).to_have_value("todoist2")
        addp.get_by_label(re.compile("^Service name")).fill("todoist")
        addp.get_by_role("button", name="Add", exact=True).click()
        expect(addp.locator(".error")).to_contain_text("delete it first")
        shot(page, "services-pack-conflict")
        addp.get_by_label(re.compile("^Service name")).fill("todoist2")
        addp.get_by_role("button", name="Add", exact=True).click()
        svc2 = page.get_by_role("dialog", name="Service todoist2")
        expect(svc2.get_by_text("No credential yet")).to_be_visible()
        page.keyboard.press("Escape")
        expect(bundled.locator("tr", has_text="todoist")).to_contain_text("todoist, todoist2")
        bundled.locator("tr", has_text="vikunja").get_by_role("button", name="Add…").click()
        addv = page.get_by_role("dialog", name="Add vikunja")
        addv.get_by_label(re.compile("API URL")).fill("https://tasks.example.org/api/v1")
        addv.get_by_role("button", name="Add", exact=True).click()
        expect(page.get_by_role("dialog", name="Service vikunja")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("tr", has_text="tasks.example.org")).to_contain_text("8")
        shot(page, "services-packs")
        no_raw_codes(page)

        step("permissions: grouped columns, check boxes, limit to one bot")
        go(page, "permissions")
        expect(page.locator("th.grp", has_text=re.compile("^todoist$"))).to_be_visible()
        expect(page.locator("th.grp", has_text="demo")).to_be_visible()
        expect(page.get_by_text(re.compile("Not shown here"))).to_have_count(0)
        cell = page.get_by_role("button", name="uicheck: telegram.send not allowed")
        cell.click()
        allowed = page.get_by_role("button", name="uicheck: telegram.send allowed")
        expect(allowed).to_have_class(re.compile(r"\ball\b"))
        allowed.hover()
        page.locator("td.cell", has=allowed).get_by_role("link", name="limit").click()
        lim = page.get_by_role("dialog", name="Limit uicheck: telegram.send")

        def limit_alerts_me():
            expect(lim).to_be_visible()
            lim.get_by_label("alerts", exact=True).check()
            lim.get_by_label("me (111222333)", exact=True).check()
            lim.get_by_role("button", name="Save").click()
            expect(lim).to_have_count(0)

        def open_limit(cell_btn):
            cell_btn.hover()
            page.locator("td.cell", has=cell_btn).get_by_role("link", name="limit").click()

        expect(lim).to_be_visible()
        expect(lim.get_by_text("No limit = this key may send from any bot, but only to chats on that bot's allowlist")).to_be_visible()
        expect(lim.get_by_role("button", name="Allow all", exact=False)).to_have_count(0)
        lim.get_by_label("alerts", exact=True).check()
        lim.get_by_label("me (111222333)", exact=True).check()
        shot(page, "permissions-limit")
        lim.get_by_role("button", name="Save").click()
        expect(lim).to_have_count(0)
        limited = page.get_by_role("button", name=re.compile(r"^uicheck: telegram.send allowed\. "))
        expect(limited).to_have_class(re.compile(r"\blimited\b"))
        # "Remove limit" keeps the permission, drops only the limit; then limit it again for the later steps
        open_limit(limited)
        expect(lim.get_by_label("alerts", exact=True)).to_be_checked()
        lim.get_by_role("button", name="Remove limit").click()
        expect(lim).to_have_count(0)
        expect(allowed).to_have_class(re.compile(r"\ball\b"))
        open_limit(allowed)
        limit_alerts_me()
        expect(limited).to_have_class(re.compile(r"\blimited\b"))
        # services: the text names the scope field
        page.get_by_role("button", name="uicheck: todoist.tasks_add not allowed").click()
        open_limit(page.get_by_role("button", name="uicheck: todoist.tasks_add allowed"))
        tl = page.get_by_role("dialog", name="Limit uicheck: todoist.tasks_add")
        expect(tl.get_by_text("No limit = this key may use any Todoist projects the service credential can reach.")).to_be_visible()
        shot(page, "permissions-limit-service")
        tl.get_by_role("button", name="Cancel").click()
        page.get_by_role("button", name="uicheck: todoist.tasks_add allowed").click()  # back to not allowed
        page.get_by_role("button", name="uicheck: demo.list_items not allowed").click()
        expect(page.get_by_role("button", name="uicheck: demo.list_items allowed")).to_be_visible()
        shot(page, "permissions")
        no_raw_codes(page)

        step("permissions: header rows + Key column stay put when scrolled (wide, 390px, dark)")
        go(page, "keys")  # enough keys that the matrix overflows downward too
        for i in range(1, 11):
            page.get_by_label("New key name").fill(f"filler-{i:02d}")
            page.get_by_role("button", name="Create").click()
            expect(page.locator(".secret")).to_contain_text("gw_")
            page.get_by_role("button", name="Done").click()
        for w, h, scheme, name in ((1000, 700, "light", "permissions-frozen"), (390, 700, "light", "permissions-frozen-390"),
                                   (1000, 700, "dark", "permissions-frozen-dark")):
            page.emulate_media(color_scheme=scheme)
            page.set_viewport_size({"width": w, "height": h})
            check_frozen(page)
            shot(page, name)
        page.emulate_media(color_scheme="light")
        page.set_viewport_size({"width": 1280, "height": 900})

        step("keys: client code has a working function per allowed action, run from this (other) machine")
        go(page, "keys")
        row = page.locator("tr", has_text="uicheck")
        py, sh = view_code(page, row, "Code", "client-grants", "keys-code")
        assert 'def send(text, chat_id=111222333, bot="alerts", parse_mode=None, silent=False):' in py, py
        assert "def demo_list_items(item_id):" in py and "telegram.send (built into the gateway)" in py, py
        assert 'KEY = os.environ["GATEWAY_KEY"]' in py and secret not in py and secret not in sh, py
        assert "send() {" in sh and "demo_list_items() {" in sh, sh
        client_me("client-grants", "uicheck", key=secret)
        # Real calls: the gateway accepts them (key, scope, payload) and forwards upstream; the test network has
        # no internet, so the answer is the gateway's own upstream error, which proves the request got through.
        upstream = re.compile(r"upstream_(error|timeout)")
        for args in (["python3", "-c", "import gateway_client as g; print(g.send('hello from uicheck'))"],
                     ["python3", "-c", "import gateway_client as g; print(g.demo_list_items('x1'))"],
                     ["sh", "gateway.sh", "send", "hello \"quoted\"\nsecond line"],
                     ["sh", "gateway.sh", "demo_list_items", "x1"]):
            res = run_client("client-grants", args, key=secret)
            assert upstream.search(res) and "forbidden" not in res and "bad_request" not in res, f"{args}: {res}"
        res = run_client("client-grants", ["python3", "-c", "import gateway_client as g; g.send('x', chat_id=999)"], key=secret)
        assert "403" in res and "forbidden" in res, res  # scope still enforced
        no_raw_codes(page)

        step("keys: menu on the last row, narrow screen, dark mode")
        last = page.locator("tbody tr").last
        open_menu(page, last, "Code", "keys-menu-last-row")
        page.keyboard.press("Escape")
        expect(page.locator(".menu-list")).to_have_count(0)
        page.set_viewport_size({"width": 390, "height": 844})
        go(page, "keys")
        open_menu(page, page.locator("tbody tr").last, "Code", "keys-menu-narrow")
        page.keyboard.press("Escape")
        page.emulate_media(color_scheme="dark")
        page.set_viewport_size({"width": 1280, "height": 900})
        go(page, "keys")
        open_menu(page, page.locator("tr", has_text="uicheck"), "Code", "keys-menu-dark")
        page.keyboard.press("Escape")
        view_code(page, page.locator("tr", has_text="uicheck"), "Code", "client-dark", "keys-code-dark")
        page.emulate_media(color_scheme="light")

        step("commands: add, problem shown, edit, bad JSON explains itself, delete")
        go(page, "commands")
        page.get_by_role("button", name="Add command").click()
        dlg = page.get_by_role("dialog", name="Add command")
        dlg.get_by_label(re.compile("^Bot")).select_option(label="alerts")
        dlg.get_by_label("Command (a-z, 0-9, _)").fill("/Todo")
        dlg.get_by_label("Operation").select_option("todoist.quick_add")
        dlg.get_by_label("Run as key").select_option(label="uicheck")
        dlg.get_by_role("button", name="Save").click()
        expect(dlg).to_have_count(0)
        row = page.locator("tr", has_text="/todo")
        expect(row).to_contain_text("todoist.quick_add")
        expect(row.locator(".error")).to_contain_text("key lacks this permission")
        row.get_by_role("button", name="Edit").click()
        dlg = page.get_by_role("dialog", name="Edit /todo")
        dlg.get_by_label(re.compile("^Call template")).fill("{nope")
        dlg.get_by_role("button", name="Save").click()
        expect(dlg.locator(".error").first).to_contain_text("Won't run: key lacks this permission")
        expect(dlg.locator(".error").last).to_contain_text("not valid JSON")
        dlg.get_by_label(re.compile("^Call template")).fill('{"body": {"text": "{text} #Home"}}')
        dlg.get_by_label(re.compile("^Reply template")).fill("added {body.content}")
        dlg.get_by_role("button", name="Save").click()
        expect(dlg).to_have_count(0)
        expect(row).to_contain_text("template")
        shot(page, "commands")
        no_raw_codes(page)
        row.get_by_role("button", name="Delete").click()  # confirm() is accepted by the page-wide handler
        expect(page.get_by_text("No commands yet.")).to_be_visible()

        step("commands: a call that can't work is refused with the reason, dry run, then saved")
        go(page, "commands")
        page.get_by_role("button", name="Add command").click()
        dlg = page.get_by_role("dialog", name="Add command")
        dlg.get_by_label(re.compile("^Bot")).select_option(label="alerts")
        dlg.get_by_label("Command (a-z, 0-9, _)").fill("ass")
        dlg.get_by_label("Operation").select_option("kanbanflow.tasks_add")
        dlg.get_by_label("Run as key").select_option(label="uicheck")
        fields = dlg.get_by_test_id("op-fields")
        expect(fields).to_contain_text("name*")
        expect(fields).to_contain_text("columnId*")
        expect(fields).to_contain_text("(limitable)")
        tpl = dlg.get_by_label(re.compile("^Call template"))
        assert json.loads(tpl.input_value()) == {"path": {}, "query": {}, "body": {"name": "{text}", "columnId": ""}}
        dlg.get_by_role("button", name="Save").click()
        expect(dlg.locator(".error")).to_contain_text("missing required field 'columnId'")
        tpl.fill('{"path": {}, "query": {}, "body": {"text": "{text}"}}')  # the template that used to save fine
        dlg.get_by_role("button", name=re.compile("^Dry run")).click()
        expect(dlg.get_by_test_id("dry-error")).to_contain_text("undeclared body fields ['text']")
        dlg.get_by_role("button", name="Save").click()
        expect(dlg.locator(".error").last).to_contain_text("undeclared body fields ['text']")
        dlg.get_by_role("button", name="Fill template").click()
        tpl.fill(tpl.input_value().replace('"columnId": ""', '"columnId": "col1"'))
        dlg.get_by_label("Try with text").fill("buy milk")
        dlg.get_by_role("button", name=re.compile("^Dry run")).click()
        dry = dlg.get_by_test_id("dry-result")
        expect(dry).to_contain_text("POST https://kanbanflow.com/api/v1/tasks")
        expect(dry).to_contain_text('"name": "buy milk"')
        expect(dry).to_contain_text("key lacks this permission")
        shot(page, "commands-dry-run")
        dlg.get_by_role("button", name="Save").click()
        expect(dlg).to_have_count(0)
        expect(page.locator(".error").first).to_contain_text("saved, but it won't run yet")
        row = page.locator("tr", has_text="/ass")
        expect(row).to_contain_text("kanbanflow.tasks_add")
        no_raw_codes(page)
        row.get_by_role("button", name="Delete").click()
        expect(page.get_by_text("No commands yet.")).to_be_visible()

        step("settings: cleanup run now")
        go(page, "settings")
        page.get_by_role("button", name="Run now").click()
        expect(page.get_by_text(re.compile(r"^Last run"))).to_be_visible()
        expect(page.get_by_text("Fixed in .env")).to_be_visible()
        shot(page, "settings")
        no_raw_codes(page)

        for h in ("messages", "audit"):
            step(f"{h}: loads clean")
            go(page, h)
            shot(page, h)
            no_raw_codes(page)
            assert page.locator("main .error").count() == 0, page.locator("main .error").first.inner_text()

        browser.close()
    assert not console_errors, "browser console errors:\n" + "\n".join(console_errors)
    print(f"UI check passed: {len(steps)} steps; screenshots in {OUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"UI check FAILED at step {len(steps)} ({steps[-1] if steps else '-'}): {e}", file=sys.stderr)
        sys.exit(1)
