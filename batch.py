"""Multi-job batch nesting: one nest run across several released jobs sharing the same stock.

Why batching saves material: job-by-job, each job's leftovers are racked and may fall under the 36"
floor or the 85% rack gate before the next job can use them.  Pooled, a 40" offcut from job A is
immediately the right drop for a 38" piece of job B on the same shift.

Honesty built in: the plan reports the SAME jobs compiled one after another on the evolving
registry (the "sequential" baseline) next to the batch result, and the compiler only ever returns a
batch plan at least as good as that baseline (the baseline's own piece order is one of the
candidates it evaluates).

Execution model: the batch becomes ONE frame (job id ``BATCH-xxxxxx``) that owns the stock
reservations, while each piece is tagged with its real job so it lands on that job's own cart
(10.4.1.x), scrap is attributed to it, and each job's quote and records stay separate.  The member
jobs' own frames are retired (superseded) so nothing is cut twice.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from .bus import FabricationBus
from .mule_frame import Frame, FramePhase
from .nesting import NestPlan, PlannedStep, _orderings, _simulate_ex
from .physics import CutSpec


class BatchError(ValueError):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class BatchJob(BaseModel):
    job_id: str
    frame_id: str
    pieces: int


class BatchMetrics(BaseModel):
    pieces: int
    unmet: int
    virgin_sticks: int            # registered raw sticks pulled + shortfall sticks to buy
    drops_used: int
    remnants_created: int
    native_skips: int
    scrap_lb: float
    scrap_in: float               # leftover length sent to scrap (rack-full or under 36")
    piece_in: float
    yield_pct: float              # piece length / (piece length + scrapped leftover length)


class BatchSavings(BaseModel):
    virgin_sticks: int
    scrap_lb: float
    scrap_in: float
    yield_pts: float
    remnants_gained: int


class BatchPlan(BaseModel):
    batch_id: str
    strategy: str
    jobs: List[BatchJob]
    batch: BatchMetrics
    sequential: BatchMetrics
    saved: BatchSavings
    steps: List[PlannedStep] = Field(default_factory=list)
    order: List[CutSpec] = Field(default_factory=list)
    piece_jobs: List[str] = Field(default_factory=list)
    unmet: List[CutSpec] = Field(default_factory=list)
    unmet_jobs: List[str] = Field(default_factory=list)
    purchased_sticks: Dict[str, int] = Field(default_factory=dict)


def metrics_of(plans: List[NestPlan]) -> BatchMetrics:
    steps = [s for p in plans for s in p.steps]
    piece_in = sum(s.spec.length_in for s in steps)
    scrap_in = sum(s.leftover_long_in for s in steps if s.disposition == "SCRAP_BIN")
    bought: Dict[str, int] = {}
    for p in plans:
        for k, v in p.purchased_sticks.items():
            bought[k] = bought.get(k, 0) + v
    return BatchMetrics(
        pieces=sum(p.pieces for p in plans), unmet=sum(len(p.unmet) for p in plans),
        virgin_sticks=sum(p.virgin_pulls for p in plans) + sum(bought.values()),
        drops_used=len({s.source_addr for s in steps if s.source_kind in ("drop", "offcut")}),
        remnants_created=sum(s.leftover_addr is not None for s in steps),
        native_skips=sum(s.skipped_lead_cut for s in steps),
        scrap_lb=round(sum(s.scrap_lb for s in steps), 3), scrap_in=round(scrap_in, 4),
        piece_in=round(piece_in, 4),
        yield_pct=round(100.0 * piece_in / (piece_in + scrap_in), 2) if piece_in + scrap_in else 0.0)


def _score(plan: NestPlan) -> tuple:
    return (len(plan.unmet), plan.virgin_total, plan.scrap_lb, -plan.native_skips)


def gather(bus: FabricationBus, job_ids: List[str]) -> List[Tuple[str, Frame]]:
    """One live, un-batched frame per selected job (frames come from the persisted registry)."""
    if len(job_ids) != len(set(job_ids)):
        raise BatchError("a job was selected twice", 422)
    if len(job_ids) < 2:
        raise BatchError("select at least two released jobs to batch", 422)
    out: List[Tuple[str, Frame]] = []
    for jid in job_ids:
        live = [f for f in bus.frames.values() if f.job_id == jid and f.phase == FramePhase.ACTIVE]
        if not live:
            raise BatchError(f"{jid} has no active frame (release it first, or it is already finished/batched)")
        if not live[0].pending:
            raise BatchError(f"{jid} has no pieces left to cut")
        out.append((jid, live[0]))
    return out


def compile_batch(bus: FabricationBus, job_ids: List[str], *, allow_purchase: bool = False) -> BatchPlan:
    members = gather(bus, job_ids)
    base = bus.reg.state

    # sequential baseline: each job compiled on what the previous jobs left behind
    state, seq_plans, seq_order, seq_jobs = base, [], [], []
    for jid, frame in members:
        best, best_state = None, None
        for name, order in _orderings(list(frame.pending)):
            plan, scratch = _simulate_ex(bus, jid, name, order, allow_purchase, True, base_state=state)
            if best is None or _score(plan) < _score(best):
                best, best_state = plan, scratch.state
        seq_plans.append(best)
        state = best_state
        seq_order += best.order
        seq_jobs += [jid] * len(best.order)
    baseline = metrics_of(seq_plans)

    pool: List[Tuple[CutSpec, str]] = [(s, jid) for jid, f in members for s in f.pending]
    candidates: List[Tuple[str, List[CutSpec], List[str]]] = [("sequential_order", seq_order, seq_jobs)]
    for name, key in (("pooled_best_fit_decreasing", lambda p: (p[0].profile, -p[0].length_in)),
                      ("pooled_grouped_by_miter", lambda p: (p[0].profile, -p[0].miter_deg, p[0].face.value,
                                                             p[0].lead_miter_deg, -p[0].length_in))):
        ordered = sorted(pool, key=key)
        candidates.append((name, [s for s, _ in ordered], [j for _, j in ordered]))

    batch_id = f"BATCH-{uuid4().hex[:6].upper()}"
    best: Optional[NestPlan] = None
    for name, order, owners in candidates:
        plan, _ = _simulate_ex(bus, batch_id, name, order, allow_purchase, True, piece_jobs=owners)
        if best is None or _score(plan) < _score(best):
            best = plan
    m = metrics_of([best])
    return BatchPlan(
        batch_id=batch_id, strategy=best.strategy,
        jobs=[BatchJob(job_id=j, frame_id=f.frame_id, pieces=len(f.pending)) for j, f in members],
        batch=m, sequential=baseline,
        saved=BatchSavings(virgin_sticks=baseline.virgin_sticks - m.virgin_sticks,
                           scrap_lb=round(baseline.scrap_lb - m.scrap_lb, 3),
                           scrap_in=round(baseline.scrap_in - m.scrap_in, 4),
                           yield_pts=round(m.yield_pct - baseline.yield_pct, 2),
                           remnants_gained=m.remnants_created - baseline.remnants_created),
        steps=best.steps, order=best.order, piece_jobs=best.piece_jobs, unmet=best.unmet,
        unmet_jobs=best.unmet_jobs, purchased_sticks=best.purchased_sticks)


def release_batch(bus: FabricationBus, job_ids: List[str]) -> Tuple[BatchPlan, Frame]:
    """Retire the member frames and open ONE executable batch frame, in a single durable commit."""
    plan = compile_batch(bus, job_ids)
    if plan.unmet:
        raise BatchError(f"{len(plan.unmet)} piece(s) cannot be cut from current stock")
    members = gather(bus, job_ids)
    snaps = {f.frame_id: f.model_copy(deep=True) for _, f in members}
    known = set(bus.frames)
    try:
        with bus.reg.transaction():
            for jid, f in members:
                if f.current is not None:                           # give back the staged reservation
                    bus.reg.unlock(f.current.source_addr)
                f.current = None
                f.note = f"superseded by {plan.batch_id}"
                f.finish()
                bus._save(f)
                rec = bus.reg.state.intakes.get(jid)
                if rec is not None and "released" in rec:
                    rec["released"]["batch"] = plan.batch_id
            frame = bus.frame_from_specs(plan.batch_id, plan.order, plan.piece_jobs,
                                         note=f"batch of {', '.join(job_ids)}")
            bus.reg.log("BATCH_RELEASED", batch=plan.batch_id, jobs=job_ids, frame=frame.frame_id)
    except BaseException:
        for fid in set(bus.frames) - known:
            del bus.frames[fid]
        for _, f in members:                                         # memory must match the rolled-back disk
            for name in type(f).model_fields:
                setattr(f, name, getattr(snaps[f.frame_id], name))
        raise
    return plan, frame
