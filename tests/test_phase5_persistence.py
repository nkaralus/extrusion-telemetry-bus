import os

import pytest

from extrusion_bus import registry as R
from extrusion_bus.bus import FabricationBus, GateRejected
from extrusion_bus.llm import RuleParser
from extrusion_bus.mule_frame import FramePhase
from extrusion_bus.physics import End
from extrusion_bus.recovery import bom_for_job, restore
from extrusion_bus.registry import Registry
from extrusion_bus.sales_gates import evaluate_intake
from extrusion_bus.sales_intake import SalesIntakePackage, SitePhoto

from conftest import PN


def boot(path):
    """A cold start: fresh process state from disk only."""
    reg = Registry.load(path)
    bus = FabricationBus(reg, parser=RuleParser(reg))
    return reg, bus, restore(bus)


@pytest.fixture
def shop(tmp_path):
    path = tmp_path / "state_registry.json"
    reg = Registry.load(path)
    reg.add_raw(PN, 288, count=3)
    bus = FabricationBus(reg, parser=RuleParser(reg))
    return path, reg, bus


# ============================ restart at the exact step ============================ #
def test_frame_survives_restart_at_its_exact_step(shop):
    path, reg, bus = shop
    frame = bus.open_frame("J1", 'four 58" braces square')
    bus.stage_next_cut(frame)
    bus.ack(frame, "CUT_OK")                                     # piece 1 done, piece 2 staged
    before = (frame.frame_id, frame.slot, len(frame.pending), frame.current.model_dump(), frame.guard.model_dump())

    reg2, bus2, rep = boot(path)                                  # power cycle
    f2 = bus2.frames[frame.frame_id]
    assert rep.restored == [{"frame_id": frame.frame_id, "job_id": "J1", "phase": "active", "pending": 3,
                             "step": frame.current.instruction}]
    assert rep.restaged == [] and rep.locks_released == []
    assert (f2.frame_id, f2.slot, len(f2.pending), f2.current.model_dump(), f2.guard.model_dump()) == before
    assert reg2.state.stock[f2.current.source_addr].allocated_job_id == "J1"        # reservation intact
    assert len(reg2.state.carts["J1"].pieces) == 1 and reg2.state.carts["J1"].addr.startswith("10.4.1.")

    acks = bus2.run_to_completion(f2)                              # and it keeps working
    assert f2.phase == FramePhase.DONE and len(reg2.state.carts["J1"].pieces) == 4 and len(acks) == 3


def test_restart_result_matches_an_uninterrupted_run(tmp_path):
    def run(interrupt):
        path = tmp_path / ("a.json" if interrupt else "b.json")
        reg = Registry.load(path); reg.add_raw(PN, 288, count=2)
        bus = FabricationBus(reg, parser=RuleParser(reg))
        f = bus.open_frame("J", 'five 58" braces 45 degree miter spline out')
        bus.stage_next_cut(f); bus.ack(f, "CUT_OK"); bus.ack(f, "CUT_OK")
        if interrupt:
            reg, bus, _ = boot(path)
            f = bus.frames[f.frame_id]
        bus.run_to_completion(f)
        return sorted((i.kind.value, i.long_point_in, str(i.end_a.long_point_face)) for i in reg.state.stock.values()), \
            [(p.length_in, p.source_addr) for p in reg.state.carts["J"].pieces]
    assert run(True) == run(False)


def test_crash_during_the_commit_write_is_all_or_nothing(shop, monkeypatch):
    path, reg, bus = shop
    frame = bus.open_frame("J2", 'two 58" braces square')
    bus.stage_next_cut(frame)
    pre = (frame.model_dump(), reg.state.model_dump(exclude={"journal", "seq"}))

    def power_cut(*a, **k):
        raise OSError("power lost during replace")
    monkeypatch.setattr(R.os, "replace", power_cut)
    with pytest.raises(OSError):
        bus.ack(frame, "CUT_OK")
    monkeypatch.undo()
    # memory rolled back: same frame, same stock, nothing credited
    assert frame.model_dump() == pre[0]
    assert reg.state.model_dump(exclude={"journal", "seq"}) == pre[1] and not reg.state.carts["J2"].pieces

    reg2, bus2, rep = boot(path)                                  # and disk agrees with memory
    f2 = bus2.frames[frame.frame_id]
    assert f2.current.model_dump() == frame.current.model_dump() and len(f2.pending) == 2
    assert not reg2.state.carts["J2"].pieces
    bus2.ack(f2, "CUT_OK")                                         # the operator presses OK once: exactly one piece
    assert len(reg2.state.carts["J2"].pieces) == 1 and len(f2.pending) == 1


def test_gate_rejection_dlq_is_committed_not_rolled_back(shop):
    path, reg, bus = shop
    frame = bus.open_frame("J3", 'one 58" brace square')
    step = bus.stage_next_cut(frame)
    reg.state.stock.pop(step.source_addr)                          # stock vanished after staging
    reg.save()
    with pytest.raises(GateRejected):
        bus.ack(frame, "CUT_OK")
    assert frame.phase == FramePhase.DLQ and frame.dlq.code == "COMMIT_REJECTED"
    reg2, bus2, _ = boot(path)
    assert bus2.frames[frame.frame_id].phase == FramePhase.DLQ and reg2.state.dlq[-1]["code"] == "COMMIT_REJECTED"


def test_open_frame_and_dlq_frame_are_persisted(shop):
    path, reg, bus = shop
    ok = bus.open_frame("J4", 'two 58" braces square')             # opened but never staged
    dead = FabricationBus(reg, parser=lambda s, f: "garbage").open_frame("J5", "x")
    reg2, bus2, rep = boot(path)
    assert bus2.frames[ok.frame_id].phase == FramePhase.ACTIVE and len(bus2.frames[ok.frame_id].pending) == 2
    assert bus2.frames[dead.frame_id].phase == FramePhase.DLQ


# ============================ re-stage, locks, pruning ============================ #
def test_vanished_source_is_restaged_not_trusted(shop):
    path, reg, bus = shop
    frame = bus.open_frame("J6", 'one 58" brace square')
    old = bus.stage_next_cut(frame).source_addr
    reg.state.stock.pop(old); reg.save()                           # e.g. someone purged it by hand
    reg2, bus2, rep = boot(path)
    f2 = bus2.frames[frame.frame_id]
    assert rep.restaged == [frame.frame_id] and f2.current.source_addr != old
    assert reg2.state.stock[f2.current.source_addr].allocated_job_id == "J6"


def test_locks_of_finished_jobs_are_released_but_adopt_locks_are_kept(shop):
    path, reg, bus = shop
    f = bus.open_frame("DONE-JOB", 'one 58" brace square')
    bus.run_to_completion(f)
    held = reg.add_remnant(PN, 80, End.square())
    reg.lock(held.addr, "DONE-JOB")                                # stale: its job is finished
    adopted = bus.adopt_drop(PN, 70, End.square(), job_id="FUTURE-JOB")   # reservation for a job with no frame yet
    reg2, bus2, rep = boot(path)
    assert held.addr in rep.locks_released and reg2.state.stock[held.addr].allocated_job_id is None
    assert reg2.state.stock[adopted.addr].allocated_job_id == "FUTURE-JOB"


def test_terminal_frames_are_pruned_but_active_ones_never(shop):
    path, reg, bus = shop
    live = bus.open_frame("LIVE", 'one 58" brace square')
    for i in range(R.MAX_TERMINAL_FRAMES + 8):
        f = bus.frame_from_specs(f"T{i}", [])
        bus.stage_next_cut(f)                                       # nothing pending -> done
    assert sum(v["frame"]["phase"] == "done" for v in reg.state.frames.values()) == R.MAX_TERMINAL_FRAMES
    assert live.frame_id in reg.state.frames


# ============================ file-level durability ============================ #
def test_stray_tmp_file_from_a_torn_write_is_ignored(shop):
    path, reg, bus = shop
    (path.parent / ".state_registry.json.deadbeef.tmp").write_text('{"truncated": ')
    reg2, _, _ = boot(path)
    assert len(reg2.state.stock) == 3


def test_corrupt_main_file_falls_back_to_the_previous_version(shop):
    path, reg, bus = shop
    f = bus.open_frame("J7", 'two 58" braces square')
    bus.stage_next_cut(f)                                           # a few writes so a .bak exists
    path.write_text('{"schema_version": 1, "stock": {"10.2.1.1": {BROKEN')
    reg2 = Registry.load(path)
    assert reg2.recovered_from_backup and len(reg2.state.stock) == 3
    assert [e["kind"] for e in reg2.state.journal][-1] == "RECOVERED_FROM_BACKUP"
    assert Registry.load(path).state.stock.keys() == reg2.state.stock.keys()   # main file was rewritten good
    lone = path.parent / "lone.json"                                 # corrupt and no backup: fail loudly, never reset
    lone.write_text("{nope")
    with pytest.raises(Exception):
        Registry.load(lone)


def test_writes_fsync_the_file_and_its_directory(shop, monkeypatch):
    path, reg, bus = shop
    calls = []
    real = os.fsync
    monkeypatch.setattr(R.os, "fsync", lambda fd: (calls.append(os.fstat(fd).st_mode), real(fd)))
    reg.add_raw(PN, 240)
    import stat
    assert any(stat.S_ISREG(m) for m in calls) and any(stat.S_ISDIR(m) for m in calls)


def test_after_commit_hooks_run_only_after_a_durable_commit(shop):
    path, reg, bus = shop
    seen = []
    with reg.transaction():
        reg.after_commit(lambda: seen.append(path.read_text().count("10.2.1.4")))
        reg.add_raw(PN, 240)
        assert seen == []                                           # not yet
    assert len(seen) == 1 and seen[0] >= 1                          # the file on disk already held the new stick
    with pytest.raises(RuntimeError):
        with reg.transaction():
            reg.after_commit(lambda: seen.append("ROLLED BACK HOOK"))
            raise RuntimeError("abort")
    assert "ROLLED BACK HOOK" not in seen
    reg.after_commit(lambda: seen.append("immediate"))              # no open transaction: runs now
    assert seen[-1] == "immediate"


# ============================ jobs released by an older build ============================ #
def test_released_job_with_no_frame_is_rebuilt_minus_pieces_already_cut(tmp_path):
    path = tmp_path / "state_registry.json"
    reg = Registry.load(path)
    reg.add_raw(PN, 288, count=8); reg.add_raw("EXT-220-BOX", 288, count=6)
    bus = FabricationBus(reg)
    pkg = SalesIntakePackage(job_id="OLD", customer_name="Pat", width_in=156, proj_in=144, house_h_in=120, front_h_in=96,
                             substrate="brick", door_swing_detected=False, obstacles_checked=True,
                             site_photos=[SitePhoto(kind=k, path=f"OLD/{k}.jpg") for k in ("wide_context", "tie_in_closeup", "sketch")])
    reg.save_intake("OLD", pkg.model_dump(mode="json"), evaluate_intake(pkg).model_dump(mode="json"))
    bom = bom_for_job(bus, "OLD")
    old_frame = bus.frame_from_specs("OLD", sorted(bom, key=lambda s: (s.profile, -s.length_in)))
    with reg.transaction():
        reg.state.intakes["OLD"]["released"] = {"ts": 1.0, "cart_addr": reg.state.carts["OLD"].addr,
                                                "frame_id": old_frame.frame_id, "pieces": len(bom)}
    bus.stage_next_cut(old_frame)
    for _ in range(3):
        bus.ack(old_frame, "CUT_OK")                                 # 3 pieces physically cut
    with reg.transaction():                                          # what a Phase-4 deployment looks like on disk
        reg.state.frames.clear()
        for i in reg.state.stock.values():
            i.allocated_job_id = None

    reg2, bus2, rep = boot(path)
    assert [(r["job_id"], r["already_cut"], r["remaining"]) for r in rep.rebuilt] == [("OLD", 3, 27)]
    f2 = bus2.frames[rep.rebuilt[0]["frame_id"]]
    assert f2.note.startswith("rebuilt after restart") and reg2.state.intakes["OLD"]["released"]["frame_id"] == f2.frame_id
    bus2.run_to_completion(f2)
    assert len(reg2.state.carts["OLD"].pieces) == 30 and f2.phase == FramePhase.DONE
    _, _, again = boot(path)                                          # finished job: nothing to rebuild next boot
    assert again.rebuilt == []


def test_released_job_with_nothing_left_is_not_rebuilt(shop):
    path, reg, bus = shop
    assert restore(bus).rebuilt == []
