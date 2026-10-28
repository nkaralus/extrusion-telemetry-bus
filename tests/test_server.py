import pytest
from fastapi.testclient import TestClient

from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import End, Face
from extrusion_bus.server import create_app

from conftest import PN


@pytest.fixture
def client(reg):
    reg.add_raw(PN, 288)
    return TestClient(create_app(reg, parser=RuleParser(reg)))


def body(addr, **spec):
    spec.setdefault("length_in", 58)
    return {"addr": addr, "station": "SAW_1", "job_id": "J1", "spec": {"profile": PN, **spec}}


def test_health(client):
    r = client.get("/health").json()
    assert r["ok"] and r["raw"] == 1 and r["remnants"] == 0


def test_verify_complete_label_flow(client, reg):
    addr = next(iter(reg.state.stock))
    v = client.post("/cut/verify", json=body(addr)).json()
    assert v["verdicts"][-1]["gate"] == "GI_3" and all(x["ok"] for x in v["verdicts"])
    done = client.post("/cut/complete", json=body(addr))
    assert done.status_code == 200 and done.json()["disposition"] == "REMNANT_RACK"
    lab = client.get(f"/label/{done.json()['leftover_addr']}").json()
    assert lab["qr_payload"].startswith("EXTBUS|10.2.2.") and lab["bin_location"] == "BLUE-LONG"


def test_gate_rejections_surface_as_409_and_verify_reports_them(client):
    assert client.post("/cut/complete", json=body("10.2.2.99")).status_code == 409
    rep = client.post("/cut/verify", json=body("10.2.2.99")).json()
    assert rep["verdicts"][0]["code"] == "UNKNOWN_TARGET"
    assert client.get("/label/10.2.2.99").status_code == 404


def test_label_refuses_raw_stick(client, reg):
    assert client.get(f"/label/{next(iter(reg.state.stock))}").status_code == 409


def test_order_flow_with_macro_pad_keys(client):
    fid = client.post("/order", json={"job_id": "J1", "scope": 'two 58" braces square'}).json()["frame_id"]
    s1 = client.post(f"/order/{fid}/next").json()
    assert s1["step"]["source_kind"] == "raw" and s1["guard"]["ok"]
    a1 = client.post(f"/order/{fid}/ack", json={"result": "CUT_OK"}).json()
    assert a1["commit"]["leftover_bin"] == "BLUE-LONG" and a1["next_step"]["via_remnant"]
    a2 = client.post(f"/order/{fid}/ack", json={"result": "CUT_OK"}).json()
    assert a2["frame_phase"] == "done" and a2["next_step"] is None
    assert client.post(f"/order/{fid}/ack", json={"result": "CUT_OK"}).status_code == 409
    assert client.post("/order/nope/next").status_code == 404


def test_order_dlq_on_unparseable_scope(client):
    r = client.post("/order", json={"job_id": "J1", "scope": "make me a sandwich"}).json()
    assert r["phase"] == "dlq" and r["dlq"]["code"] == "PLAN_NAK_LIMIT"


def test_miscut_endpoint(client, reg):
    addr = next(iter(reg.state.stock))
    r = client.post("/cut/miscut", json={"proposal": body(addr), "actual_length_in": 57.5}).json()
    assert r["outcome"] == "MISCUT_NAK" and not r["piece_credited"]
    assert client.get("/ledger/scrap").json()["entries"][0]["reason"] == "MISCUT"


def test_flip_miter_repairs_orientation(client, reg):
    rem = reg.add_remnant(PN, 80, End.miter(45, Face.BACK))
    b = body(rem.addr, miter_deg=45, face="spline_face", lead_miter_deg=45, lead_face="spline_face")
    assert client.post("/cut/verify", json=b).json()["verdicts"][-1]["code"] == "GEOMETRY_MISMATCH"
    flipped = client.post("/cut/flip", json=b).json()
    assert flipped["spec"]["face"] == "back_face" and all(v["ok"] for v in flipped["report"]["verdicts"])
    assert client.post("/cut/flip", json=body(rem.addr)).status_code == 422


def test_missing_drop_endpoint_reroutes(client, reg):
    ghost = reg.add_remnant(PN, 70, End.square())
    b = body(ghost.addr)
    r = client.post("/drop/missing", json={"addr": ghost.addr, "job_id": "J1", "spec": b["spec"]}).json()
    assert r["purged"] and r["reroute_addr"].startswith("10.2.1.") and r["reroute_report"]["verdicts"][-1]["ok"]
