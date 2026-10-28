import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from extrusion_bus import server
from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import End
from extrusion_bus.registry import Registry
from extrusion_bus.server import create_app

import imgtools as T

STATIC = Path(server.STATIC_DIR)
TABS = ["Sales", "Engineering", "Saw Bench", "Inventory", "Accounting"]


def make(tmp_path, stock=True):
    reg = Registry.load(tmp_path / "state_registry.json")
    if stock:
        reg.add_raw("EXT-240-OPEN", 288, count=8); reg.add_raw("EXT-220-BOX", 288, count=6)
        reg.add_remnant("EXT-240-OPEN", 120, End.square()); reg.add_remnant("EXT-220-BOX", 100, End.square())
    return reg, TestClient(create_app(reg, parser=RuleParser(reg), media_dir=tmp_path / "media"))


@pytest.fixture
def shop(tmp_path):
    return make(tmp_path)


def up(c, job, kind, data=None):
    return c.post("/api/sales/upload-photo", data={"job_id": job, "kind": kind},
                  files={"file": (f"{kind}.jpg", data or T.jpeg(T.scene(seed=hash(kind) % 50)), "image/jpeg")})


def make_ready(c, job="E1", **extra):
    for k in ("wide_context", "tie_in_closeup", "sketch"):
        assert up(c, job, k).status_code == 200
    answers = {"width_in": "156", "proj_in": "144", "house_h_in": 120, "front_h_in": 96, "substrate": "brick",
               "door_swing_detected": "no", "obstacles_checked": "yes", "customer_name": "Pat", **extra}
    r = c.post("/api/sales/intake/answer", json={"job_id": job, "answers": answers})
    assert r.status_code == 200 and r.json()["is_ready_for_engineering"], r.text
    return r.json()


# ============================ static routes ============================ #
def test_dashboard_and_viewports_are_served(shop):
    _, c = shop
    for path in ("/", "/sales", "/engineering"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), path
    index = c.get("/").text
    for label in TABS:
        assert f"'{label}'" in index                       # tab bar is built from this list
    for panel in ("p-sales", "p-engineering", "p-bench", "p-inventory", "p-accounting"):
        assert f'id="{panel}"' in index


def test_sales_viewport_has_the_cards_and_inputs(shop):
    _, c = shop
    html = c.get("/sales").text
    for kind in ("wide_context", "tie_in_closeup", "sketch"):
        assert f"'{kind}'" in html
    for key in ("width_in", "proj_in", "house_h_in", "front_h_in"):
        assert f'id="{key}"' in html
    assert 'name="viewport"' in html and 'capture: \'environment\'' in html and 'id="quote"' in html


def test_engineering_viewport_has_split_panes_and_release(shop):
    _, c = shop
    html = c.get("/engineering").text
    for needle in ('id="left"', 'id="right"', 'id="planbox"', 'id="sidebox"', 'id="release"', "Release to Shop Floor"):
        assert needle in html


def test_static_assets_and_missing_files(shop):
    _, c = shop
    css, js = c.get("/static/app.css"), c.get("/static/common.js")
    assert css.status_code == js.status_code == 200 and "text/css" in css.headers["content-type"] and "javascript" in js.headers["content-type"]
    assert c.get("/static/nope.js").status_code == 404
    assert c.get("/static/../server.py").status_code == 404
    assert c.get("/health").json()["ok"]                    # API unaffected by the mounts


def test_no_external_dependencies_and_no_innerhtml():
    files = [p for p in STATIC.iterdir() if p.suffix in (".html", ".js", ".css")]
    assert {p.name for p in files} >= {"index.html", "sales.html", "engineering.html", "common.js", "app.css"}
    for p in files:
        text = p.read_text()
        urls = [u for u in re.findall(r"https?://[^\s\"')]+", text) if u != "http://www.w3.org/2000/svg"]
        assert urls == [], (p.name, urls)                   # zero-dependency: nothing loads from a CDN
        assert "innerHTML" not in text and "eval(" not in text and "document.write" not in text, p.name


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_and_shared_scripts_parse(tmp_path):
    for name in ("index.html", "sales.html", "engineering.html", "common.js"):
        text = (STATIC / name).read_text()
        code = text if name.endswith(".js") else re.findall(r"<script>(.*?)</script>", text, re.S)[0]
        f = tmp_path / (name + ".js"); f.write_text(code)
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, (name, r.stderr)


# ============================ sales -> engineering ============================ #
def test_engineering_sees_what_sales_uploaded(shop):
    _, c = shop
    make_ready(c, "E1")
    jobs = c.get("/api/sales/jobs").json()
    assert jobs[0] == {**jobs[0], "job_id": "E1", "customer_name": "Pat", "ready": True, "blockers": 0, "photos": 3, "released": False}
    d = c.get("/api/sales/jobs/E1").json()
    assert d["package"]["substrate"] == "brick" and len(d["photos"]) == 3 and d["report"]["pitch_deg"] == 9.46
    assert all(c.get(p["url"]).status_code == 200 for p in d["photos"])


def test_preview_recalculates_and_persists_nothing(shop):
    reg, c = shop
    make_ready(c, "E2")
    base = c.post("/api/engineering/preview", json={"job_id": "E2"}).json()
    assert base["error"] is None and base["parametric"]["pitch_deg"] == 9.46 and base["quote"]["unmet"] == 0
    before = reg.state.model_dump()
    over = c.post("/api/engineering/preview", json={"job_id": "E2", "overrides": {"front_h_in": "90", "width_in": "200"}}).json()
    p = over["parametric"]
    assert p["pitch_deg"] == 11.77 and p["pitch_per_foot_in"] == 2.5 and p["front_bays"] == 5 and p["width_in"] == 200
    assert p["members"] != base["parametric"]["members"] and over["package"]["front_h_in"] == 90
    assert reg.state.model_dump() == before                        # preview is read-only
    assert c.get("/api/sales/jobs/E2").json()["package"]["front_h_in"] == 96


def test_preview_surfaces_blockers_and_geometry_errors(shop):
    _, c = shop
    make_ready(c, "E3")
    low = c.post("/api/engineering/preview", json={"job_id": "E3", "overrides": {"front_h_in": 119}}).json()
    assert low["report"]["is_ready_for_engineering"] is False and low["parametric"] is not None   # shows the drawing, flags the pitch
    assert any("pitch" in m["title"].lower() for m in low["report"]["missing_items"])
    inverted = c.post("/api/engineering/preview", json={"job_id": "E3", "overrides": {"front_h_in": 130}}).json()
    assert inverted["parametric"] is None and "drain toward the house" in inverted["error"]
    bad = c.post("/api/engineering/preview", json={"job_id": "E3", "overrides": {"width_in": 12}}).json()
    assert bad["error"] == "dimensions missing or implausible"
    assert c.post("/api/engineering/preview", json={"job_id": "E3", "overrides": {"substrate": "brick"}}).status_code == 422
    assert c.post("/api/engineering/preview", json={"job_id": "E3", "overrides": {"width_in": "wide"}}).status_code == 422
    assert c.post("/api/engineering/preview", json={"job_id": "nope"}).status_code == 404


def test_release_commits_bom_and_assigns_the_job_cart(shop, tmp_path):
    reg, c = shop
    make_ready(c, "E4")
    r = c.post("/api/engineering/release", json={"job_id": "E4", "overrides": {"front_h_in": "90"}})
    assert r.status_code == 200
    out = r.json()
    assert out["cart_addr"].startswith("10.4.1.") and out["plan"]["pieces"] == 30 and out["plan"]["unmet"] == []
    # the override was committed with the release, and the revision is on record
    rec = Registry.load(reg.path).state.intakes["E4"]
    assert rec["package"]["front_h_in"] == 90 and rec["released"]["cart_addr"] == out["cart_addr"]
    assert rec["revisions"][0]["overrides"] == {"front_h_in": "90"}
    assert reg.state.carts["E4"].addr == out["cart_addr"]
    # an executable frame exists for the saw bench; no stock was reserved yet
    frames = c.get("/api/frames").json()
    assert frames[0]["frame_id"] == out["frame_id"] and frames[0]["pending"] == 30 and frames[0]["phase"] == "active"
    assert all(i.allocated_job_id is None for i in reg.state.stock.values())
    assert c.get("/api/sales/jobs").json()[0]["released"] is True
    # and it runs: the compiled order is what the bench pulls
    step = c.post(f"/order/{out['frame_id']}/next").json()["step"]
    assert step["source_addr"] == out["plan"]["steps"][0]["source_addr"]
    ack = c.post(f"/order/{out['frame_id']}/ack", json={"result": "CUT_OK"}).json()
    assert ack["commit"]["outcome"] == "CUT_OK" and len(reg.state.carts["E4"].pieces) == 1


def test_release_is_blocked_when_not_ready_already_released_or_short_of_stock(tmp_path):
    reg, c = make(tmp_path)
    c.get("/api/sales/jobs/E5?create=1")
    r = c.post("/api/engineering/release", json={"job_id": "E5"})
    assert r.status_code == 409 and r.json()["detail"]["is_ready_for_engineering"] is False
    assert "E5" not in reg.state.carts and "released" not in reg.state.intakes["E5"]
    make_ready(c, "E5")
    assert c.post("/api/engineering/release", json={"job_id": "E5", "overrides": {"substrate": "brick"}}).status_code == 422
    assert c.post("/api/engineering/release", json={"job_id": "E5"}).status_code == 200
    again = c.post("/api/engineering/release", json={"job_id": "E5"})
    assert again.status_code == 409 and "already released" in again.json()["detail"]
    assert c.post("/api/engineering/release", json={"job_id": "ghost"}).status_code == 404

    reg2, c2 = make(tmp_path / "empty", stock=False)                      # nothing on the racks
    make_ready(c2, "E6")
    short = c2.post("/api/engineering/release", json={"job_id": "E6"})
    assert short.status_code == 409 and short.json()["detail"]["purchase_sticks"]
    assert "E6" not in reg2.state.carts and "released" not in reg2.state.intakes["E6"] and c2.get("/api/frames").json() == []


def test_flip_miter_on_a_staged_cut(shop):
    reg, c = shop
    fid = c.post("/order", json={"job_id": "F1", "scope": 'one 58" brace 45 degree miter spline out'}).json()["frame_id"]
    first = c.post(f"/order/{fid}/next").json()["step"]
    assert first["spec"]["face"] == "spline_face"
    flipped = c.post(f"/order/{fid}/flip").json()
    assert flipped["step"]["spec"]["face"] == "back_face"
    locked = [i.addr for i in reg.state.stock.values() if i.allocated_job_id == "F1"]
    assert locked == [flipped["step"]["source_addr"]]                      # only the re-staged source is reserved
    assert c.post(f"/order/{fid}/flip").json()["step"]["spec"]["face"] == "spline_face"
    sq = c.post("/order", json={"job_id": "F2", "scope": 'one 58" brace square'}).json()["frame_id"]
    assert c.post(f"/order/{sq}/flip").status_code == 409                   # nothing staged yet
    c.post(f"/order/{sq}/next")
    assert c.post(f"/order/{sq}/flip").status_code == 422                   # a square cut cannot flip


def test_inventory_and_accounting_feeds(shop):
    reg, c = shop
    make_ready(c, "E7")
    c.post("/api/engineering/release", json={"job_id": "E7"})
    inv = c.get("/api/inventory").json()
    assert len(inv["stock"]) == 8 + 6 + 2 and {r["profile"] for r in inv["racks"]} >= {"EXT-240-OPEN", "EXT-220-BOX"}
    assert inv["carts"] == [{"job_id": "E7", "addr": reg.state.carts["E7"].addr, "pieces": 0}]
    assert inv["hopper_addr"].startswith("10.5.1.") and all(r["full"] is False for r in inv["racks"])
    assert {"addr", "kind", "end_a", "bin_location", "legacy"} <= set(inv["stock"][0])
