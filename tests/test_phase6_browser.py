"""Phase 6 in a real browser: sign-in, role-filtered tabs, #saw alias, admin panel, lookahead/PO draft."""
import re
import socket
import threading
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import CutSpec, End
from extrusion_bus.registry import Registry
from extrusion_bus.server import create_app

from conftest import PN

SECRETS = {"rep1": "rep1-secret-ok", "eng": "eng-secret-ok", "op": "op-secret-ok"}
ROLES = {"rep1": "rep", "eng": "engineer", "op": "operator"}


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
        yield b
        b.close()


def start(tmp_path, auth):
    reg = Registry.load(tmp_path / "bus.db")
    reg.add_raw(PN, 288, count=6); reg.add_raw("EXT-220-BOX", 288, count=10)
    reg.add_remnant(PN, 120, End.square())
    app = create_app(reg, parser=RuleParser(reg), media_dir=tmp_path / "media", auth=auth)
    if auth:
        for u, s in SECRETS.items():
            app.state.auth.create_user(u, ROLES[u], s)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    while not srv.started:
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", reg, app, srv, t


@pytest.fixture
def live(tmp_path):
    base, reg, app, srv, t = start(tmp_path, auth=True)
    yield base, reg, app
    srv.should_exit = True; t.join(timeout=5); reg.close()


@pytest.fixture
def open_live(tmp_path):
    base, reg, app, srv, t = start(tmp_path / "o", auth=False)
    yield base, reg, app
    srv.should_exit = True; t.join(timeout=5); reg.close()


def page(browser, errors=None, ignore_401=True):
    ctx = browser.new_context(viewport={"width": 1250, "height": 950})
    p = ctx.new_page()
    p.set_default_timeout(10000)
    if errors is not None:
        p.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        p.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" and not (ignore_401 and "401" in m.text) else None)
    return p


def sign_in(p, base, user, secret, path="/"):
    p.goto(base + path)
    pw.expect(p.locator("#login")).to_be_visible()
    p.fill("#l-user", user); p.fill("#l-secret", secret); p.click("#l-go")
    pw.expect(p.locator("#tabs")).to_be_visible()


def tabs(p):
    return [t.strip() for t in p.locator("#tabs button[data-tab]").all_inner_texts()]


def test_sign_in_screen_blocks_everything_until_credentials_are_given(browser, live):
    base, reg, app = live
    errors, expect = [], pw.expect
    p = page(browser, errors)
    p.goto(base + "/")
    expect(p.locator("#login")).to_be_visible()
    expect(p.locator("#tabs")).to_be_hidden()
    assert "Sales" not in p.locator("body").inner_text().split("Sign in")[0]          # nothing behind the overlay is rendered
    p.fill("#l-user", "rep1"); p.fill("#l-secret", "wrong-secret-1"); p.click("#l-go")
    expect(p.locator("#l-err")).to_have_text("invalid username or secret")
    expect(p.locator("#l-secret")).to_have_value("")
    p.fill("#l-secret", SECRETS["rep1"]); p.press("#l-secret", "Enter")
    expect(p.locator("#tabs")).to_be_visible()
    expect(p.locator("#whoami")).to_have_text("rep1 · rep")
    assert errors == []


def test_each_role_gets_only_its_tabs(browser, live):
    base, reg, app = live
    expect = pw.expect
    for user, want in (("rep1", ["Sales"]), ("eng", ["Engineering", "Inventory"]), ("op", ["Saw Bench"])):
        p = page(browser)
        sign_in(p, base, user, SECRETS[user])
        assert tabs(p) == want, user
        p.goto(base + "/#admin")                                                          # a forbidden tab quietly falls back
        expect(p.locator(f"#p-{ {'rep1': 'sales', 'eng': 'engineering', 'op': 'bench'}[user] }")).to_have_class(re.compile("on"))
        assert p.locator("#p-admin.on").count() == 0
    a = page(browser)
    sign_in(a, base, "admin", app.state.bootstrap_secret)
    assert tabs(a) == ["Sales", "Engineering", "Saw Bench", "Inventory", "Accounting", "Admin"]


def test_rep_sees_the_intake_page_inside_the_shell_and_can_sign_out(browser, live):
    base, reg, app = live
    p = page(browser)
    sign_in(p, base, "rep1", SECRETS["rep1"])
    fr = p.frame_locator("#p-sales iframe")
    pw.expect(fr.locator("#pill")).to_contain_text("BLOCKER")                             # the iframe carries the session cookie
    assert "rep1" in [j for j in reg.state.intakes][0] or reg.state.intakes
    p.click("#logout")
    pw.expect(p.locator("#login")).to_be_visible()
    p.goto(base + "/#sales")
    pw.expect(p.locator("#login")).to_be_visible()                                         # the cookie is really gone


def test_opening_a_standalone_page_signed_out_bounces_to_login_then_returns(browser, live):
    base, reg, app = live
    p = page(browser)
    p.goto(base + "/sales?job=BOUNCE-1")
    pw.expect(p.locator("#login")).to_be_visible()
    assert "login=1" in p.url and "next=%2Fsales" in p.url
    p.fill("#l-user", "rep1"); p.fill("#l-secret", SECRETS["rep1"]); p.click("#l-go")
    pw.expect(p.locator("#tabs")).to_be_visible()
    assert p.url.endswith("/#sales")


def test_a_session_ended_elsewhere_sends_the_user_back_to_sign_in(browser, live):
    base, reg, app = live
    p = page(browser)
    sign_in(p, base, "eng", SECRETS["eng"])
    p.click("#tab-inventory")
    pw.expect(p.locator("#inv-racks")).to_contain_text(PN)
    for sid in [s["id"] for s in app.state.auth.list_sessions() if s["user"] == "eng"]:
        app.state.auth.revoke(sid)
    p.click("#tab-engineering"); p.click("#tab-inventory")
    pw.expect(p.locator("#login")).to_be_visible()
    pw.expect(p.locator("#l-err")).to_contain_text("session ended")


def test_operator_runs_the_saw_bench_over_an_authenticated_websocket(browser, live):
    base, reg, app = live
    fr = app.state.bus.open_frame("OP1", 'two 58" braces square')
    errors = []
    p = page(browser, errors)
    sign_in(p, base, "op", SECRETS["op"])
    expect = pw.expect
    expect(p.locator("#b-live")).to_have_text("LIVE")                                    # cookie + Origin accepted by the socket
    p.locator(f"[data-frame='{fr.frame_id}'] button").click()
    expect(p.locator("#b-step")).to_contain_text("PULL")
    p.keyboard.press("1")
    expect(p.locator("#screen")).to_be_visible()
    assert len(reg.state.carts["OP1"].pieces) == 1
    assert errors == []


def test_hash_saw_is_an_alias_for_bench(browser, open_live):
    base, reg, app = open_live
    p = page(browser)
    p.goto(base + "/#saw")
    pw.expect(p.locator("#p-bench")).to_have_class(re.compile("on"))
    pw.expect(p.locator("#tab-bench")).to_have_attribute("aria-selected", "true")
    pw.expect(p.locator("#b-live")).to_be_visible()
    assert p.url.endswith("#bench")                                                       # canonicalised
    p.evaluate("location.hash = '#saw'")
    pw.expect(p.locator("#p-bench")).to_have_class(re.compile("on"))
    p.click("#tab-inventory"); p.evaluate("location.hash = '#saw'")                        # and it works as a navigation, not just on load
    pw.expect(p.locator("#p-bench")).to_have_class(re.compile("on"))


def test_admin_manages_users_tokens_and_reads_the_audit_log(browser, live):
    base, reg, app = live
    errors, expect = [], pw.expect
    p = page(browser, errors)
    sign_in(p, base, "admin", app.state.bootstrap_secret)
    p.click("#tab-admin")
    expect(p.locator("#adm-users")).to_contain_text("rep1")
    p.fill("#u-name", "newtech"); p.select_option("#u-role", "operator"); p.fill("#u-secret", "newtech-secret-ok"); p.click("#u-add")
    expect(p.locator("#adm-users")).to_contain_text("newtech")
    assert reg.state.users["newtech"]["role"] == "operator" and "newtech-secret-ok" not in reg.state.model_dump_json()
    p.click("[data-toggle='newtech']")
    expect(p.locator("#adm-users tr", has_text="newtech")).to_contain_text("DISABLED")
    p.fill("#t-label", "bench-1"); p.click("#t-mint")
    tok = p.locator("#tokenvalue").inner_text()
    assert len(tok) >= 40 and tok not in reg.state.model_dump_json()
    expect(p.locator("#adm-sessions")).to_contain_text("device · bench-1")
    expect(p.locator("#adm-audit")).to_contain_text("USER_CREATED")
    expect(p.locator("#adm-audit")).to_contain_text("DEVICE_TOKEN_MINTED")
    assert errors == []


def test_lookahead_and_po_draft_in_the_inventory_tab(browser, live):
    base, reg, app = live
    reg.state.profiles[PN].min_stock_sticks = 3
    app.state.bus.frame_from_specs("RUSH", [CutSpec(profile=PN, length_in=150)] * 9, due_ts=reg.clock() + 86400)
    expect = pw.expect
    adm = page(browser)
    sign_in(adm, base, "admin", app.state.bootstrap_secret)
    adm.click("#tab-inventory")
    row = adm.locator("#inv-reorder tr", has_text=PN)
    expect(row).to_contain_text("STOCKOUT RISK")
    expect(row).to_contain_text("9")                                                       # committed sticks
    expect(adm.locator("#inv-podraft")).to_contain_text("RUSH")
    adm.locator(f"[data-po='{PN}']").click()
    expect(adm.locator(".toast")).to_contain_text("PO placed")
    assert reg.state.on_order and reg.state.on_order[0]["profile"] == PN
    eng = page(browser)
    sign_in(eng, base, "eng", SECRETS["eng"])
    eng.click("#tab-inventory")
    expect(eng.locator("#inv-reorder")).to_contain_text("STOCKOUT RISK")                   # engineers see it...
    expect(eng.locator("[data-po]")).to_have_count(0)                                      # ...but cannot place the PO
    expect(eng.locator(".po-controls")).to_be_hidden()
