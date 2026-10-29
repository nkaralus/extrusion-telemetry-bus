"""Atomic recovery on boot.

``restore(bus)`` runs once, before the server accepts traffic:

1. Every persisted frame is loaded back into ``bus.frames`` at its exact slot and step.
2. An active frame whose staged source has vanished or lost its reservation is re-staged
   (and says so); a frame whose source is intact is left exactly as the operator saw it.
3. Reservations owned by jobs whose frames are all finished are released (no stock held
   hostage by a dead job).
4. Jobs released by an older build (cart + release record, but no persisted frame) get a
   frame rebuilt from their stored intake minus the pieces already on the cart.

Nothing here guesses at physical state: an unacknowledged cut stays unacknowledged, so the
operator confirms or NAKs it exactly as before the crash.
"""
from __future__ import annotations

from collections import Counter
from typing import List

from pydantic import BaseModel, Field

from .bus import FabricationBus
from .mule_frame import Frame, FramePhase, Slot2Plan
from .parametric import POST_PROFILE, RAFTER_PROFILE, ParametricError, solve_envelope
from .physics import CutSpec
from .sales_intake import SalesIntakePackage


class RecoveryReport(BaseModel):
    backup_used: bool = False
    restored: List[dict] = Field(default_factory=list)       # {frame_id, job_id, phase, pending, step}
    restaged: List[str] = Field(default_factory=list)         # frame ids whose staged source was gone
    locks_released: List[str] = Field(default_factory=list)   # stock addrs freed from dead jobs
    rebuilt: List[dict] = Field(default_factory=list)         # {job_id, frame_id, remaining, already_cut}
    skipped: List[dict] = Field(default_factory=list)         # {job_id, reason}


def bom_for_job(bus: FabricationBus, job_id: str) -> List[CutSpec]:
    rec = bus.reg.state.intakes[job_id]
    pkg = SalesIntakePackage.model_validate(rec["package"])
    max_oc = (rec.get("revisions") or [{}])[-1].get("max_oc_in", 48.0)
    model = solve_envelope(
        pkg.width_in, pkg.proj_in, pkg.house_h_in, pkg.front_h_in, max_oc_in=max_oc,
        post_w_in=bus.reg.profile(POST_PROFILE).width_in, rafter_width_in=bus.reg.profile(RAFTER_PROFILE).width_in)
    return Slot2Plan(cuts=model.cuts).expand()


def _key(profile: str, length: float, miter: float, face: str) -> tuple:
    return (profile, round(length, 4), round(miter, 2), face)


def restore(bus: FabricationBus) -> RecoveryReport:
    reg = bus.reg
    report = RecoveryReport(backup_used=reg.recovered_from_backup)

    # 1 + 2: frames back to their exact step
    for fid, rec in list(reg.state.frames.items()):
        frame = Frame.model_validate(rec["frame"])
        bus.frames[fid] = frame
        if frame.phase == FramePhase.ACTIVE and frame.current is not None:
            item = reg.state.stock.get(frame.current.source_addr)
            if item is None or item.allocated_job_id != frame.job_id:
                with bus._atomic(frame):                       # stock moved under us: stage honestly again
                    frame.current = None
                    bus.stage_next_cut(frame)
                report.restaged.append(fid)
        report.restored.append({"frame_id": fid, "job_id": frame.job_id, "phase": frame.phase.value,
                                "pending": len(frame.pending),
                                "step": frame.current.instruction if frame.current else None})

    # 3: reservations held by jobs that only have finished frames
    by_job: dict = {}
    for f in bus.frames.values():
        by_job.setdefault(f.job_id, []).append(f)
    held = {f.current.source_addr for f in bus.frames.values()
            if f.phase == FramePhase.ACTIVE and f.current is not None}
    dead = [i.addr for i in reg.state.stock.values()
            if i.allocated_job_id in by_job and i.addr not in held
            and all(f.phase != FramePhase.ACTIVE for f in by_job[i.allocated_job_id])]
    for addr in dead:
        reg.unlock(addr)
        report.locks_released.append(addr)

    # 4: jobs released by an older build: rebuild the frame from intake minus what is already cut
    for job_id, rec in list(reg.state.intakes.items()):
        if "released" not in rec or job_id in by_job:
            continue
        try:
            bom = bom_for_job(bus, job_id)
        except (ParametricError, KeyError, ValueError) as exc:
            report.skipped.append({"job_id": job_id, "reason": f"cannot rebuild BOM: {exc}"})
            continue
        cut = Counter(_key(p.profile, p.length_in, p.miter_deg, p.face)
                      for p in (reg.state.carts[job_id].pieces if job_id in reg.state.carts else []))
        remaining: List[CutSpec] = []
        for spec in sorted(bom, key=lambda s: (s.profile, -s.length_in)):
            k = _key(spec.profile, spec.length_in, spec.miter_deg, spec.face.value)
            if cut[k] > 0:
                cut[k] -= 1
            else:
                remaining.append(spec)
        if not remaining:
            continue
        frame = bus.frame_from_specs(job_id, remaining, note=f"rebuilt after restart ({len(bom) - len(remaining)} already cut)")
        with reg.transaction():
            reg.state.intakes[job_id]["released"]["frame_id"] = frame.frame_id
        report.rebuilt.append({"job_id": job_id, "frame_id": frame.frame_id, "remaining": len(remaining),
                               "already_cut": len(bom) - len(remaining)})

    if report.restored or report.rebuilt or report.locks_released:
        reg.log("RECOVERY", restored=len(report.restored), restaged=len(report.restaged),
                rebuilt=len(report.rebuilt), locks_released=len(report.locks_released))
        reg.save()
    return report
