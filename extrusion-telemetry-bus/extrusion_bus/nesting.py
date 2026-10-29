"""1D nesting & cut-list compiler.

Evaluates a full BOM against active drops + virgin stock by *dry-running the real bus*
on a scratch copy of the registry.  Because the plan IS the execution logic (same
candidate ranking, same gates, same rack gate), what the compiler promises is what the
saw table will do, as long as the registry has not changed in between.

Strategy: best-fit decreasing, drops (10.2.2.0/24) before virgin sticks, native miter
matches ranked first (lead cut skipped).  Several piece orderings are simulated and the
winner is the one with the fewest unmet pieces, then fewest virgin pulls, then least
scrap, then the most skipped lead cuts.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .bus import FabricationBus
from .gates import _plan_for
from .mule_frame import FramePhase
from .physics import CutSpec, LeadMode
from .registry import Registry, StockKind


class PlannedStep(BaseModel):
    index: int
    spec: CutSpec
    source_addr: str
    source_kind: str            # drop (free, reclaimed) | offcut (this job's own leftover) | raw | purchased
    lead_mode: str
    skipped_lead_cut: bool      # native miter match on a miter lead
    source_long_in: float
    leftover_long_in: float
    disposition: str
    leftover_addr: Optional[str] = None
    scrap_lb: float = 0.0
    scrap_reason: Optional[str] = None
    charged_len_in: float = 0.0  # virgin length this piece is billed for (0 for reclaimed drops)
    instruction: str = ""
    job_id: Optional[str] = None  # owner of the piece (batch runs pool several jobs)


class NestPlan(BaseModel):
    job_id: str
    strategy: str
    pieces: int
    steps: List[PlannedStep] = Field(default_factory=list)
    unmet: List[CutSpec] = Field(default_factory=list)
    virgin_pulls: int = 0          # distinct sticks pulled from the registered raw yard
    purchased_sticks: Dict[str, int] = Field(default_factory=dict)  # shortfall by profile
    drops_used: int = 0
    native_skips: int = 0
    remnants_created: int = 0
    scrap_lb: float = 0.0
    order: List[CutSpec] = Field(default_factory=list)  # piece order to execute
    piece_jobs: List[str] = Field(default_factory=list)  # parallel to ``order`` for batch runs
    unmet_jobs: List[str] = Field(default_factory=list)  # parallel to ``unmet`` for batch runs

    @property
    def virgin_total(self) -> int:
        return self.virgin_pulls + sum(self.purchased_sticks.values())


def _orderings(specs: List[CutSpec]) -> List[Tuple[str, List[CutSpec]]]:
    def key_len(s): return (s.profile, -s.length_in)
    def key_group(s): return (s.profile, -s.miter_deg, s.face.value, s.lead_miter_deg, -s.length_in)
    return [
        ("best_fit_decreasing", sorted(specs, key=key_len)),
        ("grouped_by_miter", sorted(specs, key=key_group)),
        ("as_given", list(specs)),
    ]


def _simulate(bus: FabricationBus, job_id: str, name: str, order: List[CutSpec],
              allow_purchase: bool, use_drops: bool) -> NestPlan:
    return _simulate_ex(bus, job_id, name, order, allow_purchase, use_drops)[0]


def _simulate_ex(bus: FabricationBus, job_id: str, name: str, order: List[CutSpec],
                 allow_purchase: bool, use_drops: bool, *, base_state=None,
                 piece_jobs: Optional[List[str]] = None) -> Tuple[NestPlan, Registry]:
    """Dry-run ``order`` on a scratch copy of ``base_state`` (default: the live registry).

    Returns the plan and the scratch registry AFTER the run, so a caller can chain further jobs on
    the leftovers this one produced (batch baseline).
    """
    state = (base_state or bus.reg.state).model_copy(deep=True)
    if not use_drops:
        state.stock = {a: i for a, i in state.stock.items() if i.kind == StockKind.RAW}
    scratch = Registry(state=state, path=None, clock=bus.reg.clock)
    sb = FabricationBus(scratch, kerf=bus.kerf, remnant_min_in=bus.remnant_min_in,
                        default_station=bus.default_station, persist_frames=False)
    frame = sb.frame_from_specs(job_id, order, piece_jobs)
    plan = NestPlan(job_id=job_id, strategy=name, pieces=len(order), order=list(order),
                    piece_jobs=list(piece_jobs or []))
    purchased: set = set()
    real_raw = {a for a, i in state.stock.items() if i.kind == StockKind.RAW}
    pulled_raw: set = set()
    drops: set = set()
    free = {a for a, i in state.stock.items() if i.kind == StockKind.REMNANT}  # $0-basis (pre-existing)
    bought_for_current = 0

    step = sb.stage_next_cut(frame)
    while True:
        if step is None:
            if frame.phase == FramePhase.DLQ and frame.dlq and frame.dlq.code == "NO_STOCK" and frame.pending:
                spec = frame.pending[0]
                if allow_purchase and bought_for_current == 0:
                    stick = scratch.add_raw(spec.profile, scratch.profile(spec.profile).std_stick_in)[0]
                    purchased.add(stick.addr)
                    bought_for_current += 1
                else:
                    if bought_for_current:  # a fresh stick still cannot make this piece
                        for a in [a for a in purchased if a in scratch.state.stock]:
                            scratch.state.stock.pop(a)
                            purchased.discard(a)
                    plan.unmet.append(frame.pending.pop(0))
                    plan.unmet_jobs.append(frame.piece_jobs.pop(0) if frame.piece_jobs else job_id)
                    bought_for_current = 0
                frame.resume()
                step = sb.stage_next_cut(frame)
                continue
            break
        item = scratch.state.stock[step.source_addr].model_copy()
        cplan = _plan_for(scratch, item, step.spec, sb.kerf)
        owner = frame.piece_jobs[0] if frame.piece_jobs else job_id
        ack = sb.ack(frame, "CUT_OK")
        c = ack.commit
        bought_for_current = 0
        virgin = item.kind == StockKind.RAW
        if virgin:
            kind = "purchased" if item.addr in purchased else "raw"
        else:
            kind = "drop" if item.addr in free else "offcut"
            drops.add(item.addr)
        if kind == "raw":
            pulled_raw.add(item.addr)
        racked = c.leftover_addr is not None
        if racked and item.addr in free:
            free.add(c.leftover_addr)  # a drop derived from a reclaimed drop stays $0-basis
        plan.steps.append(PlannedStep(
            index=len(plan.steps) + 1, spec=step.spec, source_addr=item.addr, source_kind=kind,
            lead_mode=cplan.mode.value,
            skipped_lead_cut=cplan.mode == LeadMode.NATIVE and step.spec.lead_miter_deg > 0,
            source_long_in=item.long_point_in, leftover_long_in=cplan.leftover_long_in,
            disposition=c.disposition.value, leftover_addr=c.leftover_addr,
            scrap_lb=c.scrap_lb, scrap_reason=c.scrap_reason,
            charged_len_in=0.0 if kind == "drop" else item.long_point_in - (cplan.leftover_long_in if racked else 0),
            instruction=step.instruction, job_id=owner))
        step = ack.next_step

    plan.virgin_pulls = len(pulled_raw & real_raw)
    for a in purchased & {s.source_addr for s in plan.steps}:
        pn = next(s.spec.profile for s in plan.steps if s.source_addr == a)
        plan.purchased_sticks[pn] = plan.purchased_sticks.get(pn, 0) + 1
    plan.drops_used = len(drops)
    plan.native_skips = sum(s.skipped_lead_cut for s in plan.steps)
    plan.remnants_created = sum(s.leftover_addr is not None for s in plan.steps)
    plan.scrap_lb = round(sum(s.scrap_lb for s in plan.steps), 3)
    return plan, scratch


def compile_cut_list(bus: FabricationBus, job_id: str, specs: List[CutSpec], *,
                     allow_purchase: bool = False, use_drops: bool = True) -> NestPlan:
    """Best plan across piece orderings.  Never mutates the real registry."""
    if not specs:
        raise ValueError("empty BOM")
    plans = [_simulate(bus, job_id, n, o, allow_purchase, use_drops) for n, o in _orderings(specs)]
    return min(plans, key=lambda p: (len(p.unmet), p.virgin_total, p.scrap_lb, -p.native_skips))
