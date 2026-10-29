import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import End, Face
from extrusion_bus.printer import MemoryTransport, PrinterService
from extrusion_bus.registry import Registry
from extrusion_bus.server import create_app

from conftest import PN
from test_phase4_viewports import make_ready


def recv(ws, timeout=5.0):
    """Bounded wait: a regression must FAIL in seconds, not hang the suite forever."""
    box = {}

    def run():
        try:
            box["m"] = ws.receive_json()
        except BaseException as exc:
            box["e"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(f"no websocket message within {timeout}s")
    if "e" in box:
        raise box["e"]
    return box["m"]


def build(tmp_path, printer=None, stock=True):
    reg = Registry.load(tmp_path / "state_registry.json")
    if stock:
        reg.add_raw(PN, 288, count=6); reg.add_raw("EXT-220-BOX", 288, count=6)
        reg.add_remnant(PN, 120, End.square()); reg.add_remnant("EXT-220-BOX", 100, End.square())
    app = create_app(reg, parser=RuleParser(reg), media_dir=tmp_path / "media", printer=printer)
    return reg, app


@pytest.fixture
def rig(tmp_path):
    reg, app = build(tmp_path)
    with TestClient(app) as c:
        yield reg, app, c


def recv_until(ws, typ, limit=8):
    seen = []
    for _ in range(limit):
        m = recv(ws)
        seen.append(m)
        if m["type"] == typ:
            return m, seen
    raise AssertionError(f"no {typ!r} within {limit} messages: {seen}")


def open_frame(app, job="W1", scope='four 58" braces square'):
    return app.state.bus.open_frame(job, scope)


def attach_and_stage(ws, frame):
    ws.send_json({"type": "attach", "frame_id": frame.frame_id})
    recv_until(ws, "state")
    ws.send_json({"type": "stage"})
    return recv_until(ws, "state")[0]


# ============================ WebSocket protocol ============================ #
def test_unknown_station_is_refused(rig):
    _, _, c = rig
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws/saw/NOPE"):
            pass
    assert e.value.code == 4404


def test_station_resolves_by_name_or_address_and_greets_with_state(rig):
    reg, app, c = rig
    addr = next(s.addr for s in reg.state.stations.values() if s.name == "SAW_1")
    for ident in ("SAW_1", addr):
        with c.websocket_connect(f"/ws/saw/{ident}") as ws:
            hello = recv(ws)
            assert hello["type"] == "state" and hello["hello"] and hello["station"] == "SAW_1"
            assert hello["station_addr"] == addr and hello["frame"] is None and hello["blade"] is None


def test_attach_stage_reports_the_next_drop_and_blade_setup(rig):
    reg, app, c = rig
    f = open_frame(app, scope='two 58" braces 45 degree miter spline out')
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        st = attach_and_stage(ws, f)
        assert st["frame"]["frame_id"] == f.frame_id and st["frame"]["pending"] == 2 and st["frame"]["slot"] == "SLOT_4_GUARD"
        step, blade = st["step"], st["blade"]
        assert step["source_addr"] == blade["source"]["addr"] and step["source_addr"].startswith("10.2.2.")   # a drop is pulled first
        assert (blade["angle_deg"], blade["swing"], blade["fence_in"]) == (45.0, "left", 58.0)
        assert blade["prep"]["mode"] == "true_square" or blade["prep"]["mode"] == "native"
        assert blade["expected_drop_in"] is not None and blade["expected_square_in"] is not None
        assert [v["gate"] for v in st["guard"]] == ["GI_1", "GI_2", "GI_3"] and all(v["ok"] for v in st["guard"])
    assert app.state.bus.frames[f.frame_id].station == "SAW_1"                               # attachment is persisted
    assert reg.state.frames[f.frame_id]["frame"]["station"] == "SAW_1"


def test_macro_key_1_cuts_and_pushes_the_next_drop_instantly(rig):
    reg, app, c = rig
    f = open_frame(app)
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        first = attach_and_stage(ws, f)
        t0 = time.perf_counter()
        ws.send_json({"type": "key", "key": "1"})
        ev, seen = recv_until(ws, "event")
        st, _ = recv_until(ws, "state")
        assert time.perf_counter() - t0 < 0.5 and ev["elapsed_ms"] < 250
        assert ev["event"] == "CUT_OK" and ev["commit"]["outcome"] == "CUT_OK" and ev["commit"]["screen"]
        assert st["frame"]["pending"] == 3 and st["step"]["step_id"] > first["step"]["step_id"]
        assert st["blade"]["source"]["addr"] == st["step"]["source_addr"]
        assert len(reg.state.carts["W1"].pieces) == 1                                       # really committed, not just displayed


def test_keys_2_and_3_flip_and_miscut_over_the_socket(rig):
    reg, app, c = rig
    f = open_frame(app, "W2", 'two 58" braces 45 degree miter spline out')
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        before = attach_and_stage(ws, f)
        ws.send_json({"type": "key", "key": "2"})
        assert recv_until(ws, "event")[0]["event"] == "FLIP_MITER"
        flipped, _ = recv_until(ws, "state")
        assert flipped["step"]["spec"]["face"] == "back_face" and flipped["blade"]["swing"] == "right"
        ws.send_json({"type": "key", "key": "3", "actual_length_in": 57.5})
        ev, _ = recv_until(ws, "event")
        st, _ = recv_until(ws, "state")
        assert ev["event"] == "MISCUT_NAK" and ev["commit"]["outcome"] == "MISCUT_NAK"
        assert st["frame"]["pending"] == 2 and not reg.state.carts["W2"].pieces            # replacement owed, nothing credited
        assert st["step"] is not None and before["step"]["step_id"] != st["step"]["step_id"]


def test_key_4_purges_a_ghost_drop_and_reroutes(rig):
    reg, app, c = rig
    f = open_frame(app, "W3", 'one 58" brace square')
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        old = attach_and_stage(ws, f)["step"]["source_addr"]
        ws.send_json({"type": "key", "key": "4"})
        recv_until(ws, "event")
        st, _ = recv_until(ws, "state")
        assert old not in reg.state.stock and reg.state.unaccounted[0]["addr"] == old and st["step"]["source_addr"] != old


def test_square_cut_cannot_flip_and_errors_go_only_to_the_sender(rig):
    reg, app, c = rig
    f = open_frame(app, "W4", 'one 58" brace square')
    with c.websocket_connect("/ws/saw/SAW_1") as a, c.websocket_connect("/ws/saw/SAW_1") as b:
        recv(a); recv(b)
        attach_and_stage(a, f); recv_until(b, "state"); recv_until(b, "state")
        a.send_json({"type": "key", "key": "2"})
        assert recv_until(a, "error")[0]["code"] == "INVALID"
        b.send_json({"type": "ping"})
        assert recv(b)["type"] == "pong"                                          # b was not spammed with a's error


def test_every_display_on_a_station_updates_and_other_stations_stay_quiet(rig):
    reg, app, c = rig
    f = open_frame(app, "W5")
    with c.websocket_connect("/ws/saw/SAW_1") as bench, c.websocket_connect("/ws/saw/SAW_1") as wall, \
            c.websocket_connect("/ws/saw/SAW_2") as other:
        for w in (bench, wall, other):
            recv(w)
        attach_and_stage(bench, f)
        bench.send_json({"type": "key", "key": "1"})
        ev, _ = recv_until(wall, "event")                                                  # the wall display sees the cut live
        st, _ = recv_until(wall, "state")
        assert ev["event"] == "CUT_OK" and st["frame"]["pending"] == 3
        other.send_json({"type": "ping"})
        assert recv(other)["type"] == "pong"                                      # nothing queued for SAW_2


def test_http_actions_push_to_websocket_displays(rig):
    reg, app, c = rig
    f = open_frame(app, "W6")
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        attach_and_stage(ws, f)
        r = c.post(f"/order/{f.frame_id}/ack", json={"result": "CUT_OK"})                  # e.g. a phone, not the macro pad
        assert r.status_code == 200
        ev, _ = recv_until(ws, "event")
        st, _ = recv_until(ws, "state")
        assert ev["event"] == "CUT_OK" and st["frame"]["pending"] == 3


def test_sequence_numbers_increase_and_a_reconnect_is_exact(rig):
    reg, app, c = rig
    f = open_frame(app, "W7")
    seqs = []
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        attach_and_stage(ws, f)
        for _ in range(2):
            ws.send_json({"type": "key", "key": "1"})
            seqs += [recv_until(ws, "event")[0]["seq"], recv_until(ws, "state")[0]["seq"]]
        ws.send_json({"type": "sync"})
        live = recv_until(ws, "state")[0]
    assert seqs == sorted(set(seqs)) and len(seqs) == 4
    with c.websocket_connect("/ws/saw/SAW_1") as ws2:                                      # tablet reloaded
        hello = recv(ws2)
    assert hello["hello"] and hello["frame"] == live["frame"] and hello["step"] == live["step"] and hello["blade"] == live["blade"]


def test_protocol_validation_never_drops_the_connection(rig):
    reg, app, c = rig
    f = open_frame(app, "W8", 'one 58" brace square')
    done = open_frame(app, "W9", 'one 58" brace square'); app.state.bus.run_to_completion(done)
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        probes = [
            ("not json at all", "BAD_JSON"), ("[1,2]", "BAD_JSON"),
            ('{"type":"key","key":"1"}', "NO_FRAME"), ('{"type":"stage"}', "NO_FRAME"),
            ('{"type":"attach","frame_id":"zzz"}', "UNKNOWN_FRAME"),
            (f'{{"type":"attach","frame_id":"{done.frame_id}"}}', "FRAME_NOT_ACTIVE"),
            ("x" * 5000, "TOO_LARGE"),
        ]
        for raw, code in probes:
            ws.send_text(raw)
            assert recv(ws)["code"] == code, raw
        ws.send_json({"type": "attach", "frame_id": f.frame_id}); recv_until(ws, "state")
        for msg, code in (({"type": "key", "key": "1"}, "NOTHING_STAGED"), ({"type": "key", "key": "9"}, "BAD_KEY"),
                          ({"type": "warp"}, "UNKNOWN_TYPE")):
            ws.send_json(msg)
            assert recv(ws)["code"] == code, msg
        ws.send_json({"type": "stage"}); recv_until(ws, "state")
        ws.send_json({"type": "key", "key": "3", "actual_length_in": "long"})
        assert recv(ws)["code"] == "BAD_LENGTH"
        ws.send_json({"type": "ping"})
        assert recv(ws)["type"] == "pong"                                         # still alive after all of that


def test_gate_rejection_over_the_socket_is_reported_and_state_resent(rig):
    reg, app, c = rig
    f = open_frame(app, "WA", 'one 58" brace square')
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        step = attach_and_stage(ws, f)["step"]
        reg.state.stock.pop(step["source_addr"])
        ws.send_json({"type": "key", "key": "1"})
        assert recv_until(ws, "error")[0]["code"] == "GATE_REJECTED"
        assert recv_until(ws, "state")[0]["frame"]["phase"] == "dlq"


def test_attachment_and_staged_step_survive_a_server_reboot(tmp_path):
    reg, app = build(tmp_path)
    f = open_frame(app, "WB")
    with TestClient(app) as c, c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        before = attach_and_stage(ws, f)
    reg2 = Registry.load(tmp_path / "state_registry.json")                                  # power cycle
    app2 = create_app(reg2, parser=RuleParser(reg2), media_dir=tmp_path / "media")
    with TestClient(app2) as c2, c2.websocket_connect("/ws/saw/SAW_1") as ws2:
        hello = recv(ws2)
        assert hello["frame"]["frame_id"] == f.frame_id and hello["step"] == before["step"] and hello["blade"] == before["blade"]
        assert c2.get("/api/recovery").json()["restored"][0]["frame_id"] == f.frame_id
        ws2.send_json({"type": "key", "key": "1"})
        assert recv_until(ws2, "event")[0]["event"] == "CUT_OK"                             # and the bench just keeps working


# ============================ printer wiring ============================ #
def test_cut_ok_that_racks_a_drop_fires_the_label_and_tells_the_displays(tmp_path):
    mem = MemoryTransport()
    reg, app = build(tmp_path, printer=PrinterService(Registry.load(tmp_path / "state_registry.json"), mem, poll=0.05), stock=False)
    # the injected service must share the app's registry: rebuild properly
    reg = Registry.load(tmp_path / "state_registry.json2")
    reg.add_raw(PN, 288, count=3)
    svc = PrinterService(reg, mem, poll=0.05)
    app = create_app(reg, parser=RuleParser(reg), media_dir=tmp_path / "media", printer=svc)
    f = app.state.bus.open_frame("WP", 'two 58" braces square')
    with TestClient(app) as c, c.websocket_connect("/ws/saw/SAW_1") as ws:
        recv(ws)
        attach_and_stage(ws, f)
        ws.send_json({"type": "key", "key": "1"})
        ev, _ = recv_until(ws, "event")
        assert ev["commit"]["disposition"] == "REMNANT_RACK" and ev["commit"]["label_lines"]
        pr, _ = recv_until(ws, "print", limit=12)
        assert pr["status"] == "sent" and pr["addr"] == ev["commit"]["leftover_addr"]
        assert len(mem.sent) == 1 and c.get("/api/printer").json()["sent_total"] == 1


def test_printer_endpoints(tmp_path):
    reg = Registry.load(tmp_path / "s.json"); reg.add_raw(PN, 288, count=2)
    mem = MemoryTransport()
    svc = PrinterService(reg, mem, poll=0.05)
    app = create_app(reg, parser=RuleParser(reg), media_dir=tmp_path / "m", printer=svc)
    with TestClient(app) as c:
        assert c.get("/api/printer").json()["configured"] and c.get("/api/printer").json()["target"] == "memory"
        rem = reg.add_remnant(PN, 80, End.miter(45, Face.SPLINE))
        r = c.post("/api/printer/reprint", json={"addr": rem.addr}).json()
        assert r["status"] == "pending"
        deadline = time.time() + 3
        while not mem.sent and time.time() < deadline:
            time.sleep(0.01)
        assert len(mem.sent) == 1
        q = c.get("/api/printer/queue").json()
        assert q[-1]["addr"] == rem.addr and q[-1]["status"] == "sent" and "payload_b64" not in q[-1]
        assert c.post("/api/printer/reprint", json={"addr": "10.2.2.250"}).status_code == 404
        assert c.post("/api/printer/reprint", json={"addr": next(a for a, i in reg.state.stock.items() if i.kind.value == "raw")}).status_code == 404
        assert c.post("/api/printer/test").json() == {"sent": True, "target": "memory"}
        mem.fail_next = 1
        assert c.post("/api/printer/test").status_code == 502
        assert c.post("/api/printer/retry").json() == {"requeued": 0}


def test_without_a_configured_printer_everything_still_works(rig, monkeypatch):
    reg, app, c = rig
    assert c.get("/api/printer").json() == {"configured": False}
    for path in ("/api/printer/retry", "/api/printer/test"):
        assert c.post(path).status_code == 409
    assert c.post("/api/printer/reprint", json={"addr": "10.2.2.1"}).status_code == 409
    assert app.state.bus.print_labels is False


# ============================ boot recovery through the server ============================ #
def test_phase4_deployment_with_released_jobs_recovers_on_first_boot(tmp_path):
    reg, app = build(tmp_path)
    with TestClient(app) as c:
        make_ready(c, "OLD1")
        rel = c.post("/api/engineering/release", json={"job_id": "OLD1"}).json()
        c.post(f"/order/{rel['frame_id']}/next")
        for _ in range(2):
            c.post(f"/order/{rel['frame_id']}/ack", json={"result": "CUT_OK"})
    reg2 = Registry.load(tmp_path / "state_registry.json")
    with reg2.transaction():                                          # exactly what a pre-Phase-5 registry looks like
        reg2.state.frames.clear()
        for i in reg2.state.stock.values():
            i.allocated_job_id = None
    reg3 = Registry.load(tmp_path / "state_registry.json")
    app3 = create_app(reg3, parser=RuleParser(reg3), media_dir=tmp_path / "media")
    with TestClient(app3) as c3:
        rec = c3.get("/api/recovery").json()
        assert [(r["job_id"], r["already_cut"], r["remaining"]) for r in rec["rebuilt"]] == [("OLD1", 2, 28)]
        frames = c3.get("/api/frames").json()
        assert len(frames) == 1 and frames[0]["pending"] == 28 and frames[0]["phase"] == "active"
        assert c3.get("/api/engineering/batchable").json()[0]["job_id"] == "OLD1"
        fid = frames[0]["frame_id"]
        c3.post(f"/order/{fid}/next")
        while c3.post(f"/order/{fid}/ack", json={"result": "CUT_OK"}).json()["frame_phase"] == "active":
            pass
        assert len(reg3.state.carts["OLD1"].pieces) == 30 and reg3.state.carts["OLD1"].addr.startswith("10.4.1.")


def test_two_released_jobs_batch_through_the_api_and_run_on_the_bench(tmp_path):
    reg, app = build(tmp_path)
    with TestClient(app) as c:
        make_ready(c, "S1")
        make_ready(c, "S2", width_in="120", proj_in="96", house_h_in=108, front_h_in=96)
        for j in ("S1", "S2"):
            assert c.post("/api/engineering/release", json={"job_id": j}).status_code == 200
        assert {b["job_id"] for b in c.get("/api/engineering/batchable").json()} == {"S1", "S2"}
        assert c.post("/api/engineering/batch-compile", json={"job_ids": ["S1"]}).status_code == 422
        # each job fits the racks alone, but together they outrun the stock: refused, and nothing changes
        short = c.post("/api/engineering/batch-release", json={"job_ids": ["S1", "S2"]})
        assert short.status_code == 409 and "cannot be cut" in short.json()["detail"]
        assert {b["job_id"] for b in c.get("/api/engineering/batchable").json()} == {"S1", "S2"}
        reg.add_raw(PN, 288, count=8); reg.add_raw("EXT-220-BOX", 288, count=8)                  # a delivery arrives
        plan = c.post("/api/engineering/batch-compile", json={"job_ids": ["S1", "S2"]}).json()
        assert plan["batch"]["pieces"] == plan["sequential"]["pieces"] and plan["batch"]["unmet"] == 0
        assert (plan["batch"]["virgin_sticks"], plan["batch"]["scrap_lb"]) <= (plan["sequential"]["virgin_sticks"], plan["sequential"]["scrap_lb"])
        rel = c.post("/api/engineering/batch-release", json={"job_ids": ["S1", "S2"]})
        assert rel.status_code == 200
        out = rel.json()
        assert out["batch_id"].startswith("BATCH-") and c.get("/api/engineering/batchable").json() == []   # a batch is not re-batchable
        assert [f["job_id"] for f in c.get("/api/frames").json() if f["phase"] == "active"] == [out["batch_id"]]
        assert c.post("/api/engineering/batch-release", json={"job_ids": ["S1", "S2"]}).status_code == 409   # already superseded
        fid = out["frame_id"]
        with c.websocket_connect("/ws/saw/SAW_1") as ws:
            recv(ws)
            st = attach_and_stage(ws, app.state.bus.frames[fid])
            assert st["frame"]["job_id"] == out["batch_id"] and st["frame"]["pending"] == plan["batch"]["pieces"]
            ws.send_json({"type": "key", "key": "1"})
            recv_until(ws, "event")
        n1, n2 = len(reg.state.carts["S1"].pieces), len(reg.state.carts["S2"].pieces)
        assert n1 + n2 == 1
        assert c.get("/api/sales/jobs").json()[0]["released"] is True
