import random

import pytest

from extrusion_bus import registry as R
from extrusion_bus.batch import BatchError, compile_batch, gather, release_batch
from extrusion_bus.bus import FabricationBus
from extrusion_bus.mule_frame import FramePhase
from extrusion_bus.physics import CutSpec, End, Face
from extrusion_bus.recovery import restore
from extrusion_bus.registry import Registry, StockKind

from conftest import PN


def sq(*lens, **kw):
    return [CutSpec(profile=PN, length_in=l, **kw) for l in lens]


def shop(tmp_path, sticks=10, jobs=None, path=True):
    reg = Registry.load(tmp_path / "state_registry.json") if path else Registry()
    reg.add_raw(PN, 288, count=sticks)
    bus = FabricationBus(reg)
    frames = {j: bus.frame_from_specs(j, specs) for j, specs in (jobs or {}).items()}
    return reg, bus, frames


A, B = sq(130, 54, 70), sq(34, 150, 54, 62)      # found by search: pooled needs 2 sticks, one-after-another needs 3


# ============================ batch yield ============================ #
def test_batch_uses_fewer_sticks_and_scraps_less_than_running_jobs_back_to_back(tmp_path):
    reg, bus, _ = shop(tmp_path, jobs={"A": A, "B": B})
    plan = compile_batch(bus, ["A", "B"])
    assert (plan.sequential.virgin_sticks, plan.batch.virgin_sticks) == (3, 2)
    assert plan.saved.virgin_sticks == 1 and plan.saved.scrap_lb > 1.0 and plan.saved.yield_pts > 2.0
    assert plan.batch.unmet == 0 and plan.batch.pieces == plan.sequential.pieces == 7
    assert plan.strategy.startswith("pooled") and plan.batch.yield_pct > plan.sequential.yield_pct
    assert plan.batch.piece_in == plan.sequential.piece_in == sum(s.length_in for s in A + B)


def test_batch_is_never_worse_than_sequential_across_many_random_shifts():
    rng = random.Random(2026)
    strictly_better = 0
    for trial in range(40):
        reg = Registry(); reg.add_raw(PN, 288, count=12)
        for _ in range(rng.randint(0, 3)):
            reg.add_remnant(PN, rng.choice([40, 66, 90, 120]), End.square())
        bus = FabricationBus(reg, persist_frames=False)
        jobs = {}
        for j in ("A", "B", "C")[: rng.choice((2, 3))]:
            specs = sq(*[rng.choice([24, 30, 38, 46, 54, 62, 70, 90, 110, 130]) for _ in range(rng.randint(2, 5))])
            if rng.random() < 0.4:
                specs += sq(*[rng.choice([40, 58]) for _ in range(2)], miter_deg=45, face="spline_face")
            bus.frame_from_specs(j, specs)
            jobs[j] = specs
        plan = compile_batch(bus, list(jobs))
        b, s = plan.batch, plan.sequential
        assert (b.unmet, b.virgin_sticks, round(b.scrap_lb, 3)) <= (s.unmet, s.virgin_sticks, round(s.scrap_lb, 3)), \
            f"trial {trial}: batch scored worse than sequential: {b} vs {s}"
        strictly_better += (b.virgin_sticks, b.scrap_lb) < (s.virgin_sticks, s.scrap_lb)
    assert strictly_better >= 5                      # and it does win often enough to matter


def test_every_piece_stays_attributed_to_its_own_job(tmp_path):
    reg, bus, _ = shop(tmp_path, jobs={"A": A, "B": B})
    plan = compile_batch(bus, ["A", "B"])
    per_job = {}
    for s in plan.steps:
        per_job.setdefault(s.job_id, []).append(s.spec.length_in)
    assert {j: sorted(v) for j, v in per_job.items()} == {"A": sorted(x.length_in for x in A), "B": sorted(x.length_in for x in B)}
    assert len(plan.order) == len(plan.piece_jobs) == 7


def test_planning_never_touches_the_live_registry(tmp_path):
    reg, bus, frames = shop(tmp_path, jobs={"A": A, "B": B})
    before = reg.state.model_dump()
    compile_batch(bus, ["A", "B"])
    assert reg.state.model_dump() == before and all(f.phase == FramePhase.ACTIVE for f in frames.values())


# ============================ release + execution ============================ #
def test_release_runs_one_batch_frame_that_credits_each_jobs_own_cart(tmp_path):
    reg, bus, frames = shop(tmp_path, jobs={"A": A, "B": B})
    bus.stage_next_cut(frames["A"])                                   # A already has a staged, reserved source
    staged = frames["A"].current.source_addr
    assert reg.state.stock[staged].allocated_job_id == "A"
    plan, bf = release_batch(bus, ["A", "B"])
    assert bf.job_id == plan.batch_id and bf.job_id.startswith("BATCH-") and len(bf.pending) == 7 and bf.piece_jobs == plan.piece_jobs
    assert all(f.phase == FramePhase.DONE and f.note == f"superseded by {plan.batch_id}" for f in frames.values())
    assert reg.state.stock[staged].allocated_job_id is None             # A's old reservation was handed back

    raw_before = sum(i.kind == StockKind.RAW for i in reg.state.stock.values())
    acks = bus.run_to_completion(bf)
    assert bf.phase == FramePhase.DONE and len(acks) == 7
    assert sorted(p.length_in for p in reg.state.carts["A"].pieces) == sorted(x.length_in for x in A)
    assert sorted(p.length_in for p in reg.state.carts["B"].pieces) == sorted(x.length_in for x in B)
    assert BATCH_CART_ABSENT(reg, plan.batch_id)
    # execution reproduces the plan: same sticks pulled, same scrap
    assert raw_before - sum(i.kind == StockKind.RAW for i in reg.state.stock.values()) == plan.batch.virgin_sticks
    assert round(reg.scrap_total_lb(), 3) == plan.batch.scrap_lb
    assert {e.job_id for e in reg.state.scrap_ledger} <= {"A", "B"}      # scrap attributed to real jobs, not BATCH-x


def BATCH_CART_ABSENT(reg, batch_id):
    return all(c.job_id != batch_id or not c.pieces for c in reg.state.carts.values())


def test_batch_frame_survives_a_restart_mid_run_and_keeps_crediting_the_right_jobs(tmp_path):
    reg, bus, frames = shop(tmp_path, jobs={"A": A, "B": B})
    plan, bf = release_batch(bus, ["A", "B"])
    bus.stage_next_cut(bf)
    for _ in range(3):
        bus.ack(bf, "CUT_OK")
    reg2 = Registry.load(tmp_path / "state_registry.json")
    bus2 = FabricationBus(reg2)
    rep = restore(bus2)
    bf2 = bus2.frames[bf.frame_id]
    assert [r["job_id"] for r in rep.restored if r["phase"] == "active"] == [plan.batch_id]
    assert bf2.piece_jobs == bf.piece_jobs and len(bf2.pending) == 4 and rep.rebuilt == []   # members are done: not "rebuilt"
    bus2.run_to_completion(bf2)
    assert len(reg2.state.carts["A"].pieces) == 3 and len(reg2.state.carts["B"].pieces) == 4


def test_release_is_atomic_when_the_write_fails(tmp_path, monkeypatch):
    reg, bus, frames = shop(tmp_path, jobs={"A": A, "B": B})
    bus.stage_next_cut(frames["A"])
    pre_frames = {k: f.model_dump() for k, f in frames.items()}
    pre_state = reg.state.model_dump(exclude={"journal", "seq"})
    monkeypatch.setattr(R.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        release_batch(bus, ["A", "B"])
    monkeypatch.undo()
    assert {k: f.model_dump() for k, f in frames.items()} == pre_frames       # members untouched in memory
    assert set(bus.frames) == {f.frame_id for f in frames.values()} and reg.state.model_dump(exclude={"journal", "seq"}) == pre_state
    plan, bf = release_batch(bus, ["A", "B"])                                  # and it works once the disk is back
    assert bf.phase == FramePhase.ACTIVE


# ============================ guard rails ============================ #
def test_selection_errors(tmp_path):
    reg, bus, frames = shop(tmp_path, jobs={"A": A, "B": B, "EMPTY": []})
    for ids, status in ((["A"], 422), (["A", "A"], 422), ([], 422), (["A", "NOPE"], 409), (["A", "EMPTY"], 409)):
        with pytest.raises(BatchError) as e:
            gather(bus, ids)
        assert e.value.status == status, ids
    release_batch(bus, ["A", "B"])
    with pytest.raises(BatchError):                                            # already superseded: cannot be batched twice
        compile_batch(bus, ["A", "B"])


def test_unmet_pieces_block_release_but_can_be_planned_with_purchase(tmp_path):
    reg, bus, frames = shop(tmp_path, sticks=1, jobs={"A": sq(200, 200), "B": sq(200, 90)})
    plan = compile_batch(bus, ["A", "B"])
    assert plan.batch.unmet > 0 and plan.unmet_jobs and set(plan.unmet_jobs) <= {"A", "B"}
    before = reg.state.model_dump()
    with pytest.raises(BatchError) as e:
        release_batch(bus, ["A", "B"])
    assert e.value.status == 409 and reg.state.model_dump() == before and all(f.phase == FramePhase.ACTIVE for f in frames.values())
    buy = compile_batch(bus, ["A", "B"], allow_purchase=True)
    assert buy.batch.unmet == 0 and buy.purchased_sticks == {PN: sum(buy.purchased_sticks.values())} and buy.batch.virgin_sticks >= 3


def test_native_miter_matches_are_found_across_jobs(tmp_path):
    reg, bus, _ = shop(tmp_path, sticks=2)
    reg.add_remnant(PN, 70, End.miter(45, Face.SPLINE))
    mit = dict(miter_deg=45, face="spline_face", lead_miter_deg=45, lead_face="spline_face")
    bus.frame_from_specs("M1", sq(30, **mit)); bus.frame_from_specs("M2", sq(26, **mit))
    plan = compile_batch(bus, ["M1", "M2"])
    assert plan.batch.native_skips >= 1 and plan.batch.drops_used >= 1
