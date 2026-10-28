import pytest
from fastapi.testclient import TestClient

from extrusion_bus.bus import FabricationBus
from extrusion_bus.llm import RuleParser
from extrusion_bus.nesting import _orderings, _simulate, compile_cut_list
from extrusion_bus.physics import CutSpec, End, Face
from extrusion_bus.quoting import quote_job
from extrusion_bus.server import create_app

from conftest import PN


def specs(*lens, **kw):
    return [CutSpec(profile=PN, length_in=l, **kw) for l in lens]


# ---- nesting -------------------------------------------------------------------- #
def test_bfd_beats_as_given_order(reg, bus):
    reg.add_raw(PN, 120.0, count=8)
    bom = specs(50, 20, 50, 80)
    given = _simulate(bus, "J", "as_given", list(bom), False, True)
    bfd = _simulate(bus, "J", "bfd", _orderings(bom)[0][1], False, True)
    assert (given.virgin_pulls, bfd.virgin_pulls) == (3, 2)
    best = compile_cut_list(bus, "J", bom)
    assert best.virgin_pulls == 2 and not best.unmet and best.pieces == 4


def test_drops_cover_the_job_so_no_virgin_pull(reg, bus):
    reg.add_raw(PN, 288)
    reg.add_remnant(PN, 100, End.square()); reg.add_remnant(PN, 70, End.square())
    plan = compile_cut_list(bus, "J", specs(60, 58))
    assert plan.virgin_pulls == 0 and plan.drops_used >= 2 and not plan.unmet


def test_chain_from_one_stick(reg, bus):
    reg.add_raw(PN, 288, count=2)
    plan = compile_cut_list(bus, "J", specs(58, 58, 58, 58))
    assert plan.virgin_pulls == 1 and [s.source_kind for s in plan.steps] == ["raw", "offcut", "offcut", "offcut"]


def test_native_miter_face_skips_lead_cut(reg, bus):
    reg.add_remnant(PN, 70, End.square())
    native = reg.add_remnant(PN, 70, End.miter(45, Face.SPLINE))
    bom = specs(40, miter_deg=45, face="spline_face", lead_miter_deg=45, lead_face="spline_face")
    plan = compile_cut_list(bus, "J", bom)
    s = plan.steps[0]
    assert s.source_addr == native.addr and s.lead_mode == "native" and s.skipped_lead_cut
    assert plan.native_skips == 1


def test_compile_never_mutates_the_real_registry(reg, bus):
    reg.add_raw(PN, 288, count=2); reg.add_remnant(PN, 80, End.square())
    before = reg.state.model_dump()
    compile_cut_list(bus, "J", specs(58, 58, 30, 70), allow_purchase=True)
    assert reg.state.model_dump() == before


def test_plan_is_exactly_what_execution_does(reg, bus):
    reg.add_raw(PN, 288, count=2); reg.add_remnant(PN, 100, End.square()); reg.add_remnant(PN, 74.25, End.miter(45, Face.SPLINE))
    bom = specs(58, 58, 58, 54.375, 40, 70, miter_deg=45, face="spline_face")
    plan = compile_cut_list(bus, "JOB-X", bom)
    assert not plan.unmet
    frame = bus.frame_from_specs("JOB-X", plan.order)
    acks = bus.run_to_completion(frame)
    assert [(a.commit.source_addr, a.commit.leftover_addr, a.commit.disposition.value) for a in acks] == \
           [(s.source_addr, s.leftover_addr, s.disposition) for s in plan.steps]
    assert round(sum(a.commit.scrap_lb for a in acks), 3) == plan.scrap_lb
    assert len(reg.state.carts["JOB-X"].pieces) == 6


def test_unmet_pieces_and_purchase_shortfall(reg, bus):
    plan = compile_cut_list(bus, "J", specs(58, 58))
    assert len(plan.unmet) == 2 and plan.steps == []
    buy = compile_cut_list(bus, "J", specs(58, 58), allow_purchase=True)
    assert not buy.unmet and buy.purchased_sticks == {PN: 1} and buy.virgin_pulls == 0
    too_long = compile_cut_list(bus, "J", specs(400), allow_purchase=True)   # longer than a 24' stick
    assert len(too_long.unmet) == 1 and too_long.purchased_sticks == {}


def test_empty_bom_rejected(bus):
    with pytest.raises(ValueError):
        compile_cut_list(bus, "J", [])


# ---- quoting -------------------------------------------------------------------- #
def test_virgin_price_math(reg, bus):
    reg.add_raw(PN, 288)
    q = quote_job(bus, "J", specs(58), margin_pct=25, labor_per_cut=3)
    charged = 288 - 229.875                                   # stick length that did not survive as a drop
    assert q.material_cost == round(charged / 12 * 4.10, 2) == 19.86
    assert q.price == round((19.859375 + 3) * 1.25, 2) == 28.57
    assert q.virgin_pulls == 1 and q.ready_in_days == 0 and q.unmet == 0


def test_scrapped_remainder_is_charged_to_the_job(reg, bus):
    reg.add_raw(PN, 70)
    q = quote_job(bus, "J", specs(58))                        # leaves 11 7/8" -> scrap, billed in full
    assert q.material_cost == round(70 / 12 * 4.10, 2) == 23.92


def test_reclaimed_drops_cost_zero_and_sharpen_the_bid(reg, bus):
    reg.add_remnant(PN, 100, End.square()); reg.add_remnant(PN, 70, End.square())
    q = quote_job(bus, "J", specs(60, 58), margin_pct=25)
    assert q.material_cost == 0.0 and q.price == 0.0
    assert q.virgin_only_cost > 0 and q.savings_from_drops == q.virgin_only_cost
    assert q.price < q.price_virgin_only and q.reclaimed_ft == round((60 + 58) / 12, 2)
    assert [(l.from_drops, l.from_virgin) for l in q.lines] == [(2, 0)]


def test_mixed_job_partial_savings(reg, bus):
    reg.add_raw(PN, 288)
    reg.add_remnant(PN, 100, End.square())
    q = quote_job(bus, "J", specs(90, 58), margin_pct=0)
    assert 0 < q.material_cost < q.virgin_only_cost and q.savings_from_drops > 0
    assert q.price == q.material_cost


def test_quote_flags_shortfall_with_vendor_lead_time(reg, bus):
    reg.state.profiles[PN].lead_time_days = 5
    q = quote_job(bus, "J", specs(58))                        # empty yard
    assert q.purchase_sticks == {PN: 1} and q.ready_in_days == 5 and q.material_cost == 19.86
    assert q.virgin_pulls == 0


def test_quote_never_mutates_registry(reg, bus):
    reg.add_raw(PN, 288); reg.add_remnant(PN, 80, End.square())
    before = reg.state.model_dump()
    quote_job(bus, "J", specs(58, 58, 58))
    assert reg.state.model_dump() == before


# ---- HTTP ----------------------------------------------------------------------- #
@pytest.fixture
def client(reg):
    reg.add_raw(PN, 288, count=2)
    return TestClient(create_app(reg, parser=RuleParser(reg)))


def test_compile_and_commit_endpoints(client, reg):
    scope = 'two 58" screen braces spline out 45 degree miter; one 54 3/8" brace square'
    plan = client.post("/api/nest/compile", json={"job_id": "J1", "scope": scope}).json()
    assert plan["pieces"] == 3 and plan["virgin_pulls"] == 1 and plan["unmet"] == []
    first = client.post("/api/nest/commit", json={"job_id": "J1", "scope": scope}).json()
    assert first["step"]["source_addr"] == plan["steps"][0]["source_addr"]
    fid = first["frame_id"]
    for _ in range(3):
        a = client.post(f"/order/{fid}/ack", json={"result": "CUT_OK"}).json()
    assert a["frame_phase"] == "done" and len(reg.state.carts["J1"].pieces) == 3


def test_commit_refuses_unmet_bom_and_bad_scope(client):
    too_much = {"job_id": "J2", "cuts": [{"profile": PN, "qty": 20, "length_in": 280}]}
    assert client.post("/api/nest/commit", json=too_much).status_code == 409
    assert client.post("/api/nest/compile", json={"job_id": "J2", "scope": "make me a sandwich"}).status_code == 422
    assert client.post("/api/nest/compile", json={"job_id": "J2"}).status_code == 422


def test_quote_endpoint(client):
    r = client.post("/api/quote", json={"job_id": "Q1", "margin_pct": 20,
                                        "cuts": [{"profile": PN, "qty": 1, "length_in": 58}]}).json()
    assert r["material_cost"] == 19.86 and r["price"] == round(19.859375 * 1.2, 2)
    assert r["plan"]["steps"][0]["source_kind"] == "raw"
