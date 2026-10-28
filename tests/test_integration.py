import base64

import pytest

from extrusion_bus.bus import Disposition, FabricationBus, GateRejected
from extrusion_bus.llm import RuleParser
from extrusion_bus.mule_frame import FramePhase, Slot2Plan
from extrusion_bus.physics import End, Face
from extrusion_bus.registry import Registry, StockKind, subnet_of

from conftest import PN, make_proposal

VALID = Slot2Plan.model_validate(
    {"cuts": [{"profile": PN, "qty": 1, "length_in": 58, "miter_deg": 0}]}).model_dump_json()


# ---- end to end: scope text -> plan -> gates -> registry --------------------- #
def test_end_to_end_cut_order_updates_registry(reg, bus, tmp_path):
    reg.add_raw(PN, 288)
    scope = 'We need four 58" screen braces for Job 7 with spline facing out, 45 degree miter'
    frame = bus.open_frame("JOB-7", scope)
    assert frame.naks == 0 and len(frame.pending) == 4
    assert all(p.miter_deg == 45 and p.face == Face.SPLINE for p in frame.pending)

    acks = bus.run_to_completion(frame)
    assert frame.phase == FramePhase.DONE and len(acks) == 4

    # piece 1 pulled the virgin stick; pieces 2-4 chained through the drop (10.2.2.0/24)
    assert [h.committed for h in frame.history] == [True] * 4
    assert frame.history[0].leftover_addr.startswith("10.2.2.")

    s = reg.state
    assert len(s.carts["JOB-7"].pieces) == 4 and subnet_of(s.carts["JOB-7"].addr) == "CART"
    assert not [i for i in s.stock.values() if i.kind == StockKind.RAW]   # virgin stick consumed
    (left,) = s.stock.values()
    # 288 -> 233.875 -> 175.625 -> 117.375 -> 59.125 (each step: -(58 - 4 + .125) after a 4.125 true-up)
    assert left.long_point_in == 59.125 and left.guaranteed_square_in == 55.0
    assert left.end_a == End.miter(45, Face.BACK) and left.bin_location == "BLUE-SHORT"
    assert subnet_of(left.addr) == "REMNANT" and left.allocated_job_id is None
    assert s.scrap_ledger == [] and not s.dlq
    assert sum(1 for j in s.journal if j["kind"] == "CONSUMED") == 4

    # persisted atomically: a cold reload sees exactly the same shop
    assert Registry.load(tmp_path / "state_registry.json").state.model_dump() == s.model_dump()


def test_remnants_are_consumed_before_virgin_stock(reg, bus):
    reg.add_raw(PN, 288)
    rem = reg.add_remnant(PN, 80, End.square())
    frame = bus.open_frame("J", 'one 58" brace square')
    step = bus.stage_next_cut(frame)
    assert step.source_addr == rem.addr and step.via_remnant
    assert reg.state.stock[rem.addr].allocated_job_id == "J"   # reserved for this job


def test_native_miter_match_beats_square_stock(reg, bus):
    reg.add_remnant(PN, 70, End.square())
    native = reg.add_remnant(PN, 70, End.miter(45, Face.SPLINE))
    frame = bus.open_frame("J", 'one 40" brace both ends 45 degree miter spline out')
    step = bus.stage_next_cut(frame)
    assert step.source_addr == native.addr and step.lead_mode == "native"
    assert "END A AS-IS" in step.instruction


def test_best_fit_picks_tightest_drop(reg, bus):
    big = reg.add_remnant(PN, 90, End.square())
    tight = reg.add_remnant(PN, 62, End.square())
    step = bus.stage_next_cut(bus.open_frame("J", 'one 58" brace square'))
    assert step.source_addr == tight.addr != big.addr


# ---- decision gate: rack vs scrap -------------------------------------------- #
def test_remnant_over_36_prints_label_under_36_goes_to_scrap(reg, bus):
    raw = reg.add_raw(PN, 288)[0]
    big = bus.commit(make_proposal(raw.addr, length_in=58))
    assert big.disposition == Disposition.REMNANT_RACK and big.screen == "RACK BLUE-LONG"
    raw_bytes = base64.b64decode(big.label.escpos_b64)
    assert raw_bytes.startswith(b"\x1b@") and b"\x1d(k" in raw_bytes
    assert big.label.qr_payload.startswith("EXTBUS|10.2.2.")

    small_src = reg.add_remnant(PN, 60, End.square())
    small = bus.commit(make_proposal(small_src.addr, length_in=58))   # leaves 1.875"
    assert small.disposition == Disposition.SCRAP_BIN and small.label is None
    assert small.screen == "SCRAP BIN" and small.scrap_lb == pytest.approx(1.05 * 1.875 / 12, abs=1e-3)
    assert reg.state.scrap_ledger[-1].reason == "DROP_UNDER_MIN"


def test_36_inch_threshold_uses_guaranteed_square_not_long_point(reg, bus):
    src = reg.add_remnant(PN, 100, End.square(), End.miter(45, Face.BACK))
    res = bus.commit(make_proposal(src.addr, length_in=61.875))
    # leftover long point is 38" but only 33 7/8" survives truing the far miter -> scrap
    assert res.plan.leftover_long_in == 38.0 and res.plan.leftover_guaranteed_in == 33.875
    assert res.disposition == Disposition.SCRAP_BIN


def test_commit_revalidates_and_rolls_back_atomically(reg, bus):
    raw = reg.add_raw(PN, 100)[0]
    before = reg.state.model_dump()
    with pytest.raises(GateRejected) as exc:
        bus.commit(make_proposal(raw.addr, length_in=100))
    assert exc.value.report.failed.code == "BOUNDS_OVERFLOW"
    assert reg.state.model_dump() == before


# ---- mule NAK loop / DLQ -------------------------------------------------------- #
def test_garbage_then_unknown_part_then_valid_plan(reg):
    seen = []
    bad_part = Slot2Plan.model_validate(
        {"cuts": [{"profile": "EXT-999", "qty": 1, "length_in": 10}]}).model_dump_json()
    replies = iter(["I think you want some braces!", bad_part, VALID])

    def parser(scope, feedback):
        seen.append(feedback)
        return next(replies)

    frame = FabricationBus(reg, parser=parser).open_frame("J", "whatever")
    assert frame.naks == 2 and frame.phase == FramePhase.ACTIVE and len(frame.pending) == 1
    assert seen[0] is None and seen[1].startswith("NAK 1/3") and "unknown part number" in seen[2]


def test_three_naks_trip_the_dlq_and_leave_inventory_untouched(reg):
    reg.add_raw(PN, 288)
    before = reg.state.model_dump(exclude={"dlq", "journal", "seq"})
    frame = FabricationBus(reg, parser=lambda s, f: "{not json").open_frame("J", "gibberish")
    assert frame.phase == FramePhase.DLQ and frame.dlq.code == "PLAN_NAK_LIMIT" and frame.naks == 3
    assert len(reg.state.dlq) == 1 and reg.state.dlq[0]["code"] == "PLAN_NAK_LIMIT"
    assert reg.state.model_dump(exclude={"dlq", "journal", "seq"}) == before
    assert bus_stage_is_noop(reg, frame)


def bus_stage_is_noop(reg, frame):
    return FabricationBus(reg).stage_next_cut(frame) is None


def test_no_stock_trips_dlq_without_mutation(reg, bus):
    short = reg.add_remnant(PN, 40, End.square())
    frame = bus.open_frame("J", 'one 58" brace square')
    assert bus.stage_next_cut(frame) is None
    assert frame.dlq.code == "NO_STOCK" and frame.phase == FramePhase.DLQ
    assert reg.state.stock[short.addr].allocated_job_id is None and len(reg.state.stock) == 1


def test_gate_rejection_reroutes_to_next_candidate(reg, bus):
    locked = reg.add_remnant(PN, 62, End.square())
    spare = reg.add_remnant(PN, 90, End.square())
    reg.lock(locked.addr, "OTHER-JOB")          # tightest fit is taken by another cart
    step = bus.stage_next_cut(bus.open_frame("J", 'one 58" brace square'))
    assert step.source_addr == spare.addr


# ---- miscut NAK loop ---------------------------------------------------------------- #
def test_miscut_nak_recalculates_drop_and_reroutes_replacement_fast(reg, bus):
    reg.add_raw(PN, 288)
    frame = bus.open_frame("J", 'two 58" braces square')
    first = bus.stage_next_cut(frame)
    ack = bus.ack(frame, "MISCUT_NAK", actual_length_in=57.5)

    assert ack.elapsed_ms < 1000
    assert ack.commit.outcome == "MISCUT_NAK" and not ack.commit.piece_credited
    assert ack.commit.plan.leftover_long_in == 230.375          # 288 - (57.5 + kerf), from ACTUAL cut
    assert reg.state.carts["J"].pieces == []                    # ruined piece never credited
    miscut = [e for e in reg.state.scrap_ledger if e.reason == "MISCUT"]
    assert len(miscut) == 1 and miscut[0].length_in == 57.5
    assert len(frame.pending) == 2                              # replacement still owed
    assert ack.next_step.source_addr == ack.commit.leftover_addr != first.source_addr

    bus.ack(frame, "CUT_OK"); last = bus.ack(frame, "CUT_OK")
    assert last.frame_phase == "done" and len(reg.state.carts["J"].pieces) == 2


def test_piece_missing_purges_ghost_drop_and_reroutes(reg, bus):
    r1 = reg.add_remnant(PN, 80, End.square())
    r2 = reg.add_remnant(PN, 70, End.square())
    frame = bus.open_frame("J", 'one 58" brace square')
    assert bus.stage_next_cut(frame).source_addr == r2.addr      # tightest fit
    ack = bus.ack(frame, "PIECE_MISSING")
    assert r2.addr not in reg.state.stock and reg.state.unaccounted[0]["addr"] == r2.addr
    assert ack.next_step.source_addr == r1.addr and ack.commit is None
    assert frame.phase == FramePhase.ACTIVE


def test_scope_with_two_lines_and_fractions(reg, bus):
    reg.add_raw(PN, 288)
    plan = Slot2Plan.model_validate_json(RuleParser(reg)('two 54 3/8" braces square; three 30.5" braces 45 degree back'))
    assert [(c.qty, c.length_in, c.miter_deg) for c in plan.cuts] == [(2, 54.375, 0), (3, 30.5, 45)]
    assert plan.cuts[1].face == Face.BACK
