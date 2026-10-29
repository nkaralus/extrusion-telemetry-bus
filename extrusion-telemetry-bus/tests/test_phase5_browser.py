"""Phase 5 in a real browser: live saw telemetry, printer wiring, HTTP fallback, batch UI."""
import re
import socket
import threading
import time

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import End
from extrusion_bus.printer import MemoryTransport, PrinterService
from extrusion_bus.registry import Registry
from extrusion_bus.server import create_app

from conftest import PN
from test_phase4_viewports import make_ready


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
        yield b
        b.close()


@pytest.fixture
def live(tmp_path):
    reg = Registry.load(tmp_path / "state_registry.json")
    reg.add_raw(PN, 288, count=20); reg.add_raw("EXT-220-BOX", 288, count=20)
    reg.add_remnant(PN, 120, End.square()); reg.add_remnant("EXT-220-BOX", 100, End.square())
    mem = MemoryTransport()
    svc = PrinterService(reg, mem, poll=0.05)
    app = create_app(reg, parser=RuleParser(reg), media_dir=tmp_path / "media", printer=svc)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    while not srv.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", reg, app, mem
    srv.should_exit = True
    t.join(timeout=5)


def page(browser, errors, init=None):
    ctx = browser.new_context(viewport={"width": 1250, "height": 950})
    p = ctx.new_page()
    p.set_default_timeout(10000)
    if init:
        p.add_init_script(init)
    p.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    p.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    p.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    return p


def test_two_displays_stay_in_sync_and_the_cut_prints_a_label(browser, live):
    base, reg, app, mem = live
    errors, expect = [], pw.expect
    fr = app.state.bus.open_frame("BR1", 'four 58" braces square')
    bench, wall = page(browser, errors), page(browser, errors)
    for p in (bench, wall):
        p.goto(base + "/#bench")
        expect(p.locator("#b-live")).to_have_text("LIVE")                          # socket is up on both
    bench.locator(f"[data-frame='{fr.frame_id}'] button").click()
    expect(bench.locator("#b-step")).to_contain_text("PULL")
    expect(bench.locator("#b-blade")).to_contain_text("Blade 0° · swing NONE · fence 58\"")
    expect(wall.locator("#b-pending")).to_have_text("4 left")                     # the wall display followed along untouched
    expect(wall.locator("#b-step")).to_have_text(bench.locator("#b-step").inner_text())
    first_next = wall.inner_text("#b-next")

    bench.keyboard.press("1")                                                      # macro pad key 1
    expect(wall.locator("#b-pending")).to_have_text("3 left")
    expect(wall.locator("#screen")).to_be_visible()
    expect(wall.locator("#b-next")).not_to_have_text(first_next)
    expect(bench.locator("#b-print")).to_contain_text("sent")                      # the sticker went out as a side effect
    expect(wall.locator("#b-print")).to_contain_text("sent")
    assert len(mem.sent) == 1 and len(reg.state.carts["BR1"].pieces) == 1

    bench.reload()                                                                  # tablet reloaded mid-job
    bench.click("#tab-bench")
    expect(bench.locator("#b-live")).to_have_text("LIVE")
    expect(bench.locator("#b-pending")).to_have_text("3 left")                     # exact step restored from the server
    expect(bench.locator("#b-step")).to_contain_text("PULL")

    bench.click("#tab-inventory")
    expect(bench.locator("#pr-target")).to_have_text("memory")
    expect(bench.locator("#inv-printer")).to_contain_text("1 sent")
    bench.click("#pr-test")
    expect(bench.locator(".toast")).to_contain_text("Test label sent")               # wait for the request, not a stale label
    assert len(mem.sent) == 2
    assert errors == []


def test_bench_falls_back_to_http_when_the_socket_is_unavailable(browser, live):
    base, reg, app, mem = live
    errors = []
    fr = app.state.bus.open_frame("BR2", 'two 58" braces square')
    dead_socket = "window.WebSocket = class { constructor(){ setTimeout(() => this.onclose && this.onclose(), 0); } send(){} close(){} };"
    p = page(browser, errors, init=dead_socket)
    p.goto(base + "/#bench")
    pw.expect(p.locator("#b-live")).to_contain_text("OFFLINE")
    p.locator(f"[data-frame='{fr.frame_id}'] button").click()
    pw.expect(p.locator("#b-step")).to_contain_text("PULL")
    p.keyboard.press("1")
    pw.expect(p.locator("#screen")).to_be_visible()
    assert len(reg.state.carts["BR2"].pieces) == 1                                 # the bench still works with no socket
    assert errors == []


def test_batch_ui_compiles_compares_and_releases(browser, live):
    base, reg, app, mem = live
    errors, expect = [], pw.expect
    with httpx.Client(base_url=base, timeout=20) as c:
        make_ready(c, "BT1")
        make_ready(c, "BT2", width_in="120", proj_in="96", house_h_in=108, front_h_in=96)
        for j in ("BT1", "BT2"):
            assert c.post("/api/engineering/release", json={"job_id": j}).status_code == 200
    p = page(browser, errors)
    p.goto(base + "/engineering?job=BT1")
    expect(p.locator("#plan")).to_be_visible()
    p.click("#batch summary")
    expect(p.locator("#batchlist label")).to_have_count(2)
    expect(p.locator("#bcompile")).to_be_disabled()
    p.check("[data-job='BT1']")
    expect(p.locator("#bcompile")).to_be_disabled()                                 # needs two
    p.check("[data-job='BT2']")
    p.click("#bcompile")
    expect(p.locator("#batchtable")).to_be_visible()
    expect(p.locator("#batchtable")).to_contain_text("Virgin sticks")
    expect(p.locator("#batchtable")).to_contain_text("Back to back")
    expect(p.locator("#brelease")).to_be_enabled()
    p.click("#brelease")
    expect(p.locator("#batchrel")).to_contain_text("BATCH-")
    expect(p.locator("#batchlist")).to_contain_text("No released jobs waiting")
    batch_id = re.search(r"BATCH-[0-9A-F]{6}", p.inner_text("#batchrel")).group(0)

    d = page(browser, errors)
    d.goto(base + "/#bench")
    expect(d.locator("[data-frame]")).to_have_count(1)
    expect(d.locator("[data-frame]")).to_contain_text(batch_id)
    d.locator("[data-frame] button").click()
    expect(d.locator("#b-title")).to_contain_text(batch_id)
    expect(d.locator("#b-step")).to_contain_text("PULL")                            # staged: only then is a cut key meaningful
    d.keyboard.press("1")
    expect(d.locator("#screen")).to_be_visible()
    assert len(reg.state.carts["BT1"].pieces) + len(reg.state.carts["BT2"].pieces) == 1
    assert errors == []
