"""Real-browser end-to-end tests (headless Chromium via Playwright).  Skipped when unavailable."""
import re
import socket
import threading
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import End
from extrusion_bus.registry import Registry
from extrusion_bus.server import create_app
from extrusion_bus.vision import MockBackend, VisionAnalyzer

import imgtools as T


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary in this environment
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
        yield b
        b.close()


@pytest.fixture
def live(tmp_path):
    reg = Registry.load(tmp_path / "state_registry.json")
    reg.add_raw("EXT-240-OPEN", 288, count=8); reg.add_raw("EXT-220-BOX", 288, count=6)
    reg.add_remnant("EXT-240-OPEN", 120, End.square()); reg.add_remnant("EXT-220-BOX", 100, End.square())
    media = tmp_path / "media"
    app = create_app(reg, parser=RuleParser(reg), media_dir=media, analyzer=VisionAnalyzer(media, MockBackend()))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    while not srv.started:
        time.sleep(0.02)
    photos = {n: tmp_path / n for n in ("wide_brick.jpg", "tie_brick_downspout.jpg", "sketch.jpg", "blurry_tie.jpg")}
    for n, p in photos.items():
        p.write_bytes(T.jpeg(T.blurred(T.scene(seed=3), 8) if "blurry" in n else T.scene(seed=len(n))))
    yield f"http://127.0.0.1:{port}", reg, photos
    srv.should_exit = True
    t.join(timeout=5)


def new_page(browser, errors, width=1300, height=950, touch=False):
    ctx = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch)
    page = ctx.new_page()
    page.set_default_timeout(10000)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    return page


def test_sales_to_engineering_to_saw_bench_in_a_real_browser(browser, live):
    base, reg, photos = live
    errors = []
    expect = pw.expect

    # ---------------- sales rep on a phone ---------------- #
    m = new_page(browser, errors, 420, 900, touch=True)
    m.goto(base + "/sales?job=B1")
    expect(m.locator("#pill")).to_contain_text("BLOCKER")
    expect(m.locator("#price")).to_have_count(0)
    expect(m.locator("#quote")).to_contain_text("Price locked")

    m.set_input_files("[data-slot=tie_in_closeup] input[type=file]", str(photos["blurry_tie.jpg"]))
    expect(m.locator("[data-slot=tie_in_closeup] .status")).to_have_text("Retake needed")
    expect(m.locator("[data-key='photo.tie_in_closeup']")).to_contain_text("too small or blurry")
    m.set_input_files("[data-slot=tie_in_closeup] input[type=file]", str(photos["tie_brick_downspout.jpg"]))
    expect(m.locator("[data-slot=tie_in_closeup] .status")).to_contain_text("1600×1200px")
    m.set_input_files("[data-slot=wide_context] input[type=file]", str(photos["wide_brick.jpg"]))
    m.set_input_files("[data-slot=sketch] input[type=file]", str(photos["sketch.jpg"]))
    expect(m.locator("[data-slot=sketch] .status")).to_contain_text("sharp")

    for key, val in (("width_in", "156"), ("proj_in", "144"), ("house_h_in", "120"), ("front_h_in", "96")):
        m.fill(f"#{key}", val); m.dispatch_event(f"#{key}", "change")
    expect(m.locator("#pitchline")).to_contain_text("2.00")

    sub = m.locator("[data-key='substrate']")                          # one-tap resolution cards
    expect(sub).to_contain_text("photos suggest brick")
    sub.locator("[data-choice='brick']").click()
    m.locator("[data-key='door_swing_detected'] [data-choice='no']").click()
    expect(m.locator("#pill")).to_have_text("READY")                    # downspout card is a warning, not a blocker
    m.locator("[data-key='obstacle_ack.downspout'] [data-choice='acknowledged']").click()
    expect(m.locator("#cards")).to_contain_text("Nothing missing")
    expect(m.locator("#price")).to_have_text(re.compile(r"^\$\d+\.\d\d$"))
    expect(m.locator("#savings")).to_contain_text("Drop savings offset")
    price = m.inner_text("#price")

    # ---------------- engineer at a desk ---------------- #
    e = new_page(browser, errors)
    e.goto(base + "/engineering?job=B1")
    expect(e.locator("#plan")).to_be_visible(); expect(e.locator("#side")).to_be_visible()
    expect(e.locator("#pill")).to_have_text("READY")
    expect(e.locator("[data-photo]")).to_have_count(3)
    expect(e.locator("#left")).to_contain_text("vision: brick 90%")
    expect(e.locator("#plan rect.hit")).to_have_count(11)                           # posts
    expect(e.locator("#plan line[stroke='#0b57d0']")).to_have_count(5)              # rafters
    expect(e.locator("#plan line[stroke='#d96b00']")).to_have_count(10)             # chair rails
    expect(e.locator("#plan")).to_contain_text("39\"")                              # bay spacing
    expect(e.locator("#pitchlabel")).to_have_text("9.46° · 2.00\"/ft")
    expect(e.locator("#bomtable tbody tr")).to_have_count(7)

    e.fill("#dim-front_h_in", "90")                                                 # live override
    expect(e.locator("#pitchlabel")).to_have_text("11.77° · 2.50\"/ft")
    expect(e.locator("#dirty")).to_contain_text("front_h_in")
    e.fill("#dim-front_h_in", "130")                                                # impossible geometry
    expect(e.locator("#previewerr")).to_contain_text("drain toward the house")
    expect(e.locator("#release")).to_be_disabled()
    e.fill("#dim-front_h_in", "90")
    e.locator("#side [data-id='RAFTER-0']").click()
    expect(e.locator("#info")).to_contain_text("RAFTER-0")
    expect(e.locator("#info")).to_contain_text("11.77° plumb miter")
    assert reg.state.intakes["B1"]["package"]["front_h_in"] == 96                   # nothing saved by overriding

    expect(e.locator("#release")).to_be_enabled()
    e.click("#release")
    expect(e.locator("#release")).to_have_text("Tap again to confirm release")
    e.click("#release")
    expect(e.locator("#cartaddr")).to_have_text(re.compile(r"^10\.4\.1\.\d+$"))
    expect(e.locator("#relpill")).to_be_visible()
    assert reg.state.intakes["B1"]["package"]["front_h_in"] == 90 and "released" in reg.state.intakes["B1"]

    # ---------------- saw bench in the dashboard shell ---------------- #
    d = new_page(browser, errors)
    d.goto(base + "/")
    expect(d.locator("#tabs button")).to_have_text(["Sales", "Engineering", "Saw Bench", "Inventory", "Accounting", "Admin"])
    expect(d.locator("#p-sales")).to_have_class(re.compile("on"))
    d.click("#tab-engineering")
    expect(d.frame_locator("#p-engineering iframe").locator("#jobsel")).to_be_visible()
    d.click("#tab-bench")
    d.locator("[data-frame] button").click()
    expect(d.locator("#b-step")).to_contain_text("PULL")
    expect(d.locator("#b-guard")).to_contain_text("GI_3 OK")
    d.keyboard.press("1")                                                           # macro-pad key 1 = CUT_OK
    expect(d.locator("#screen")).to_be_visible()
    assert len(reg.state.carts["B1"].pieces) == 1

    d.fill("#a-len", "74 1/4"); d.select_option("#a-ea", "miter_45_spline"); d.click("#a-go")
    expect(d.locator("#adopted")).to_contain_text("10.2.2.")
    assert any(i.legacy and i.long_point_in == 74.25 for i in reg.state.stock.values())

    d.click("#tab-inventory")
    expect(d.locator("#inv-stock tbody tr").first).to_be_visible()
    expect(d.locator("#inv-racks")).to_contain_text("EXT-240-OPEN")
    expect(d.locator("#inv-reorder")).to_contain_text("EXT-220-BOX")
    d.click("#tab-accounting")
    expect(d.locator("#scraptotal")).to_contain_text("lb scrap")
    expect(d.locator("#acc-carts")).to_contain_text("B1")

    assert errors == []
