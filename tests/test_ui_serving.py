import pytest


@pytest.fixture
def ui_dir(settings):
    from pathlib import Path
    d = Path(settings.ui_dir)
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text('<!doctype html><script type="module" src="./assets/app.js"></script>')
    (d / "assets" / "app.js").write_text("console.log('ui')")
    return d


def test_ui_served_under_random_prefix(ui_dir, make_client):
    c = make_client(admin_prefix="r4nd0madm", api_prefix="r4nd0mapi")
    r = c.get("/r4nd0madm", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/r4nd0madm/"
    r = c.get("/r4nd0madm/")
    assert r.status_code == 200 and "./assets/app.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/r4nd0madm/assets/app.js").text == "console.log('ui')"
    assert c.get("/admin/").status_code == 404


def test_ui_available_while_sealed(ui_dir, client):
    assert client.get("/admin/api/state").json()["state"] == "uninitialized"
    assert client.get("/admin/").status_code == 200


def test_ui_host_guarded(ui_dir, make_client):
    c = make_client(admin_hosts=("admin.example",))
    r = c.get("/admin/", headers={"host": "api.example"})
    assert r.status_code == 404 and r.content == b""
    assert c.get("/admin/assets/app.js", headers={"host": "api.example"}).status_code == 404


def test_no_ui_dir_is_404(client):
    assert client.get("/admin/").status_code == 404
