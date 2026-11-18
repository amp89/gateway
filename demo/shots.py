"""README screenshots of the demo gateway (run by podman-demo-shots.sh, Playwright).

PHASE=setup: the setup page with the code prefilled from the link, then finishes web setup.
PHASE=pages: logs in and captures the seeded admin pages into $OUT.
Fails on a raw error code, an error box or a browser console error on screen.
"""
import os
import re
import sys

from playwright.sync_api import expect, sync_playwright

BASE = os.environ["BASE"].rstrip("/")
OUT = os.environ.get("OUT", "/out")
PHASE = os.environ["PHASE"]
PASSWORD = os.environ["DEMO_ADMIN_PASSWORD"]
RAW_CODES = re.compile(r"\b(bad_request|name_taken|not_found|unknown_user|upstream_error|csrf|sealed)\b")
console_errors: list[str] = []


def clean(page) -> None:
    text = page.locator("main").inner_text()
    m = RAW_CODES.search(text)
    assert m is None, f"raw error code {m.group(0)!r} on screen"
    assert page.locator("main .error").count() == 0, page.locator("main .error").first.inner_text()


def shot(page, name: str, max_h: int = 900, crop: bool = True) -> None:
    """crop: from the top down to just below the last card (at most max_h px); else the viewport as is."""
    page.wait_for_timeout(250)  # let hover/transition styles settle
    clip = None
    if crop:
        bottom = page.evaluate("Math.max(...[...document.querySelectorAll('main .card')]"
                               ".map(e => e.getBoundingClientRect().bottom + window.scrollY))")
        clip = {"x": 0, "y": 0, "width": page.viewport_size["width"], "height": min(max_h, bottom + 24)}
    page.screenshot(path=f"{OUT}/{name}.png", full_page=crop, clip=clip, animations="disabled")
    print(f"- {name}.png", flush=True)


def go(page, hash_: str) -> None:
    page.goto(f"{BASE}/admin/#/{hash_}")
    page.wait_for_load_state("networkidle")


def setup_phase(page) -> None:
    page.goto(f"{BASE}/admin/#setup-code={os.environ['SETUP_CODE']}")
    code = page.get_by_label("Setup code")
    expect(code).to_have_value(os.environ["SETUP_CODE"])
    page.get_by_label("Master passphrase (12+ chars)").fill("demo passphrase for screenshots")
    page.get_by_label("Repeat passphrase").fill("demo passphrase for screenshots")
    page.get_by_label("Admin password (10+ chars)").fill(PASSWORD)
    page.get_by_label("Setup code").focus()
    page.set_viewport_size({"width": 1280, "height": 540})
    shot(page, "setup", crop=False)
    page.get_by_role("button", name="Set up").click()
    expect(page.get_by_role("button", name="Set up")).to_have_count(0)


def pages_phase(page) -> None:
    page.goto(f"{BASE}/admin/")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    expect(page.get_by_role("link", name="Permissions")).to_be_visible()

    go(page, "permissions")
    expect(page.locator("th.grp", has_text=re.compile("^vikunja$"))).to_be_visible()
    clean(page)
    shot(page, "permissions")
    cell = page.get_by_role("button", name=re.compile(r"^home-assistant: telegram\.send allowed"))
    cell.hover()
    page.locator("td.cell", has=cell).get_by_role("link", name="limit").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    shot(page, "permissions-limit", crop=False)
    page.keyboard.press("Escape")
    page.mouse.move(0, 0)
    page.emulate_media(color_scheme="dark")
    shot(page, "permissions-dark")
    page.emulate_media(color_scheme="light")

    go(page, "keys")
    expect(page.locator("tr", has_text="old-laptop")).to_contain_text("revoked")
    clean(page)
    shot(page, "keys")
    page.locator("tr", has_text="n8n-workflows").get_by_role("button", name="Code", exact=True).click()
    page.locator(".menu-list").get_by_role("menuitem", name="View code…").click()
    pre = page.locator("pre.code")
    expect(pre).to_contain_text("def send(")
    # scroll past the shared boilerplate to the generated per-action functions
    pre.evaluate("""el => { const lines = el.innerText.split('\\n');
        const i = lines.findIndex(l => l.startsWith('def ') && !/^def (request|me)\\(/.test(l));
        el.scrollTop = el.scrollHeight * Math.max(0, i - 2) / lines.length; }""")
    shot(page, "keys-code", crop=False)
    page.get_by_role("dialog").get_by_role("button", name="Close", exact=True).click()

    go(page, "bots")
    clean(page)
    shot(page, "bots", max_h=1600)

    go(page, "commands")
    expect(page.locator("tr", has_text="/today")).to_be_visible()
    clean(page)
    shot(page, "commands")

    go(page, "services")
    clean(page)
    shot(page, "services", max_h=1200)

    for name in ("messages", "audit"):
        go(page, name)
        expect(page.locator("main tbody tr").nth(5)).to_be_visible()
        clean(page)
        shot(page, name, max_h=820)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=2,
                                  locale="en-GB", timezone_id="UTC", color_scheme="light")
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
                and console_errors.append(m.text))
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on("dialog", lambda d: d.accept())
        (setup_phase if PHASE == "setup" else pages_phase)(page)
        browser.close()
    assert not console_errors, "browser console errors:\n" + "\n".join(console_errors)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"demo shots FAILED ({PHASE}): {e}", file=sys.stderr)
        sys.exit(1)
