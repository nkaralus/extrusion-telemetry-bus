import pytest
from fastapi.testclient import TestClient

from extrusion_bus.bus import GateRejected
from extrusion_bus.gates import run_gates
from extrusion_bus.llm import RuleParser
from extrusion_bus.mule_frame import FramePhase, SlotOrderError
from extrusion_bus.physics import End, Face, parse_end_condition, parse_inches
from extrusion_bus.registry import StockKind, subnet_of
from extrusion_bus.server import create_app

from conftest import PN, make_proposal


def test_parse_inches_and_end_conditions():
    assert parse_inches("74 1/4") == 74.25 and parse_inches('74-1/4"') == 74.25
    assert parse_inches("3/8") == 0.375 and parse_inches(60) == 60.0 and parse_inches("74.5in") == 74.5
    with pytest.raises(ValueError):
        parse_inches("about six feet")
    assert parse_end_condition("square") == End.square()
    assert parse_end_condition("miter_45_spline") == End.miter(45, Face.SPLINE)
    assert parse_end_condition("Miter 30 Back") == End.miter(30, Face.BACK)
    with pytest.raises(ValueError):
        parse_end_condition("ragged")


def test_adopt_registers_legacy_drop_with_label(reg, bus):
    res = bus.adopt_drop(PN, 74.3, End.miter(45, Face.SPLINE))        # tape 74.3 -> floors to 74 1/4
    assert subnet_of(res.addr) == "REMNANT" and res.long_point_in == 74.25
    assert res.guaranteed_square_in == 70.125 and res.bin_location == "BLUE-MEDIUM"
    assert res.label.qr_payload.startswith(f"EXTBUS|{res.addr}|") and not res.below_remnant_min
    item = reg.state.stock[res.addr]
    assert item.legacy and item.kind == StockKind.REMNANT and item.allocated_job_id is None
    assert any(j["kind"] == "ADOPTED" for j in reg.state.journal)


def test_adopt_flags_short_pieces_but_still_registers(bus):
    res = bus.adopt_drop(PN, 30, End.square())
    assert res.below_remnant_min and res.addr


def test_adopt_rejects_nonsense_and_is_atomic(reg, bus):
    before = reg.state.model_dump()
    for bad in (0, -5, 9999):
        with pytest.raises(ValueError):
            bus.adopt_drop(PN, bad, End.square())
    with pytest.raises(Exception):
        bus.adopt_drop("EXT-NOPE", 60, End.square())
    assert reg.state.model_dump() == before


def test_adopt_with_job_locks_the_drop_to_that_job(reg, bus):
    res = bus.adopt_drop(PN, 80, End.square(), job_id="JOB-A")
    assert run_gates(reg, make_proposal(res.addr, job="JOB-B")).failed.code == "ALREADY_ALLOCATED"
    assert run_gates(reg, make_proposal(res.addr, job="JOB-A")).ok


def test_adopt_mid_workflow_resumes_a_no_stock_frame(reg, bus):
    frame = bus.open_frame("J", 'one 58" brace square')
    assert bus.stage_next_cut(frame) is None and frame.dlq.code == "NO_STOCK"
    res = bus.adopt_drop(PN, "62 1/2", End.square(), job_id="J", frame=frame)
    assert frame.phase == FramePhase.ACTIVE and frame.dlq is None
    assert res.next_step.source_addr == res.addr and res.next_step.via_remnant
    ack = bus.ack(frame, "CUT_OK")
    assert ack.frame_phase == "done" and len(reg.state.carts["J"].pieces) == 1
    assert res.addr not in reg.state.stock                      # the adopted drop was consumed by the cut


def test_only_no_stock_frames_resume(bus):
    frame = bus.open_frame("J", "gibberish")  # RuleParser -> NAK x3 -> DLQ PLAN_NAK_LIMIT
    assert frame.dlq.code == "PLAN_NAK_LIMIT"
    with pytest.raises(SlotOrderError):
        frame.resume()
    res = bus.adopt_drop(PN, 80, End.square(), frame=frame)   # adopting does not revive it
    assert frame.phase == FramePhase.DLQ and res.next_step is None


# ---- HTTP ---------------------------------------------------------------------- #
@pytest.fixture
def client(reg):
    return TestClient(create_app(reg, parser=RuleParser(reg)))


def test_adopt_endpoint(client, reg):
    r = client.post("/api/saw/adopt-drop", json={"profile": PN, "tape_in": "74 1/4",
                                                 "end_a": "miter_45_spline", "end_b": "square"})
    assert r.status_code == 200
    j = r.json()
    assert j["addr"].startswith("10.2.2.") and j["guaranteed_square_in"] == 70.125
    assert j["label"]["escpos_b64"] and reg.state.stock[j["addr"]].legacy


def test_adopt_endpoint_validation(client):
    assert client.post("/api/saw/adopt-drop", json={"profile": PN, "tape_in": "??"}).status_code == 422
    assert client.post("/api/saw/adopt-drop", json={"profile": PN, "tape_in": 60, "end_a": "ragged"}).status_code == 422
    assert client.post("/api/saw/adopt-drop", json={"profile": "EXT-NOPE", "tape_in": 60}).status_code == 422
    assert client.post("/api/saw/adopt-drop", json={"profile": PN, "tape_in": 60, "frame_id": "nope"}).status_code == 404


def test_adopt_endpoint_resumes_parked_frame(client):
    fid = client.post("/order", json={"job_id": "J9", "scope": 'one 58" brace square'}).json()["frame_id"]
    assert client.post(f"/order/{fid}/next").json()["dlq"]["code"] == "NO_STOCK"
    r = client.post("/api/saw/adopt-drop", json={"profile": PN, "tape_in": 70, "job_id": "J9", "frame_id": fid}).json()
    assert r["next_step"]["source_addr"] == r["addr"]
