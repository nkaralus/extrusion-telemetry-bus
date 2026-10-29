"""Sales quoting: material priced from the nest plan, reclaimed drops at $0.

Billing rule (documented so a quote can be audited):
* A piece cut from a registered drop bills $0 material (capital already recovered).
* A piece cut from a virgin stick (or from an offcut this job itself created) bills the
  length that did not survive as a racked remnant:  charged = source_long - racked_leftover_long.
  Scrapped remainders are therefore charged to the job that created them.  Drops derived
  from a reclaimed drop stay $0-basis.
* Shortfall sticks (not in the yard) are priced at the profile's std stick and flagged
  with the vendor lead time.
"""
from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field

from .bus import FabricationBus
from .nesting import NestPlan, compile_cut_list
from .physics import CutSpec


class QuoteLineItem(BaseModel):
    profile: str
    pieces: int
    from_drops: int
    from_virgin: int
    material_cost: float


class Quote(BaseModel):
    job_id: str
    pieces: int
    unmet: int
    material_cost: float
    virgin_only_cost: float          # same job priced as if the drop racks were empty
    savings_from_drops: float
    labor_cost: float
    margin_pct: float
    price: float
    price_virgin_only: float         # what a drops-blind competitor would have to bid
    reclaimed_ft: float
    virgin_pulls: int
    purchase_sticks: Dict[str, int]
    ready_in_days: float             # vendor lead time if anything must be ordered
    lines: List[QuoteLineItem] = Field(default_factory=list)
    plan: NestPlan


def _material(bus: FabricationBus, plan: NestPlan) -> float:
    total = 0.0
    for s in plan.steps:
        total += s.charged_len_in / 12.0 * bus.reg.profile(s.spec.profile).cost_per_ft
    return total


def quote_job(bus: FabricationBus, job_id: str, specs: List[CutSpec], *, margin_pct: float = 25.0,
              labor_per_cut: float = 0.0) -> Quote:
    plan = compile_cut_list(bus, job_id, specs, allow_purchase=True, use_drops=True)
    blind = compile_cut_list(bus, job_id, specs, allow_purchase=True, use_drops=False)
    material, blind_cost = _material(bus, plan), _material(bus, blind)
    labor = labor_per_cut * len(plan.steps)
    mult = 1 + margin_pct / 100.0
    by: Dict[str, QuoteLineItem] = {}
    for s in plan.steps:
        li = by.setdefault(s.spec.profile, QuoteLineItem(profile=s.spec.profile, pieces=0, from_drops=0,
                                                         from_virgin=0, material_cost=0.0))
        li.pieces += 1
        li.from_drops += s.source_kind == "drop"
        li.from_virgin += s.source_kind != "drop"  # raw, purchased or this job's own offcuts
        li.material_cost = round(li.material_cost + s.charged_len_in / 12.0
                                 * bus.reg.profile(s.spec.profile).cost_per_ft, 2)
    lead = max((bus.reg.profile(p).lead_time_days for p in plan.purchased_sticks), default=0.0)
    return Quote(
        job_id=job_id, pieces=plan.pieces, unmet=len(plan.unmet),
        material_cost=round(material, 2), virgin_only_cost=round(blind_cost, 2),
        savings_from_drops=round(blind_cost - material, 2), labor_cost=round(labor, 2),
        margin_pct=margin_pct, price=round((material + labor) * mult, 2),
        price_virgin_only=round((blind_cost + labor) * mult, 2),
        reclaimed_ft=round(sum(s.spec.length_in for s in plan.steps if s.source_kind == "drop") / 12.0, 2),
        virgin_pulls=plan.virgin_pulls, purchase_sticks=plan.purchased_sticks, ready_in_days=lead,
        lines=list(by.values()), plan=plan)
