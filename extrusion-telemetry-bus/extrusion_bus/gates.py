"""3-gate pre-execution safety firewall (ported from engine-telemetry-bus).

    GI_1  Identity & Allocation   target exists, matches profile, not locked to another job
    GI_2  Isolation & Face Align  subnet reach + anisotropic end/face compatibility
    GI_3  Physical Bounds & Kerf  (length + kerf) <= usable, miter triangle loss included

Gates are pure functions of (registry, proposal).  No gate mutates state.
A cut traveler is only issued when all three return ok=True.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from .physics import (
    KERF_IN,
    CutPlan,
    CutSpec,
    GeometryMismatch,
    classify_lead,
    fmt_inches,
    plan_cut,
)
from .registry import Registry, StockItem, StockKind, in_cidr, subnet_of


class GateVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    gate: str
    code: str
    message: str


class CutProposal(BaseModel):
    addr: str      # stock address the cut is proposed against
    station: str   # saw station address (or name)
    job_id: str
    spec: CutSpec


class GateReport(BaseModel):
    verdicts: List[GateVerdict]
    plan: Optional[CutPlan] = None

    @property
    def ok(self) -> bool:
        return len(self.verdicts) == 3 and all(v.ok for v in self.verdicts)

    @property
    def failed(self) -> Optional[GateVerdict]:
        return next((v for v in self.verdicts if not v.ok), None)


def _ok(gate: str, msg: str) -> GateVerdict:
    return GateVerdict(ok=True, gate=gate, code="OK", message=msg)


def _no(gate: str, code: str, msg: str) -> GateVerdict:
    return GateVerdict(ok=False, gate=gate, code=code, message=msg)


def _plan_for(reg: Registry, item: StockItem, spec: CutSpec, kerf: float) -> CutPlan:
    prof = reg.profile(spec.profile)
    return plan_cut(
        stock_long_in=item.long_point_in,
        working_end=item.end_a,
        far_end=item.end_b,
        width_in=prof.width_in,
        length_in=spec.length_in,
        trail=spec.trail_end(),
        lead=spec.lead_end(),
        kerf=kerf,
    )


# --------------------------------------------------------------------------- #
def gi_1(reg: Registry, p: CutProposal) -> GateVerdict:
    """Identity & Allocation."""
    item = reg.state.stock.get(p.addr)
    if item is None:
        return _no("GI_1", "UNKNOWN_TARGET", f"{p.addr} is not in state_registry.json")
    if p.spec.profile not in reg.state.profiles or item.profile != p.spec.profile:
        return _no("GI_1", "REF_MISMATCH",
                   f"{p.addr} is {item.profile}, order wants {p.spec.profile}")
    if item.allocated_job_id not in (None, p.job_id):
        return _no("GI_1", "ALREADY_ALLOCATED",
                   f"{p.addr} is locked to job {item.allocated_job_id}")
    return _ok("GI_1", f"{p.addr} exists and is available to job {p.job_id}")


def gi_2(reg: Registry, p: CutProposal) -> GateVerdict:
    """Isolation (subnet boundary) & anisotropic face alignment."""
    item = reg.state.stock.get(p.addr)
    if item is None:
        return _no("GI_2", "UNKNOWN_TARGET", f"{p.addr} is not in state_registry.json")
    station = reg.find_station(p.station)
    if station is None:
        return _no("GI_2", "CROSS_SUBNET", f"unknown station {p.station}")
    if not any(in_cidr(item.addr, c) for c in station.reach):
        return _no("GI_2", "CROSS_SUBNET",
                   f"{station.name} ({station.addr}) cannot reach {item.addr} "
                   f"[{subnet_of(item.addr) or 'outside 10.0.0.0/8 plan'}]")
    expect = "RAW" if item.kind == StockKind.RAW else "REMNANT"
    if subnet_of(item.addr) != expect:
        return _no("GI_2", "CROSS_SUBNET",
                   f"{item.kind.value} {item.addr} is outside its {expect} subnet")
    mode = classify_lead(item.end_a, p.spec.lead_end())
    if mode is None:
        return _no("GI_2", "GEOMETRY_MISMATCH",
                   f"working end {item.end_a.describe()} vs required lead "
                   f"{p.spec.lead_end().describe()} (flip miter or choose other stock)")
    return _ok("GI_2", f"{station.name} may touch {item.addr}; lead mode {mode.value}")


def gi_3(reg: Registry, p: CutProposal, kerf: float = KERF_IN) -> GateVerdict:
    """Physical bounds: (target + kerf) <= usable, miter triangle loss included."""
    item = reg.state.stock.get(p.addr)
    if item is None:
        return _no("GI_3", "UNKNOWN_TARGET", f"{p.addr} is not in state_registry.json")
    try:
        plan = _plan_for(reg, item, p.spec, kerf)
    except GeometryMismatch as exc:
        return _no("GI_3", "GEOMETRY_MISMATCH", str(exc))
    if not plan.fits:
        return _no("GI_3", "BOUNDS_OVERFLOW",
                   f"need {fmt_inches(plan.required_in)} (cut + kerf), "
                   f"usable {fmt_inches(plan.usable_in)} after {plan.mode.value}")
    return _ok("GI_3", f"need {fmt_inches(plan.required_in)} <= usable {fmt_inches(plan.usable_in)}")


def run_gates(reg: Registry, p: CutProposal, kerf: float = KERF_IN) -> GateReport:
    """Run GI_1 -> GI_2 -> GI_3, stopping at the first rejection."""
    verdicts: List[GateVerdict] = []
    for gate in (gi_1, gi_2):
        v = gate(reg, p)
        verdicts.append(v)
        if not v.ok:
            return GateReport(verdicts=verdicts)
    v3 = gi_3(reg, p, kerf)
    verdicts.append(v3)
    plan = _plan_for(reg, reg.state.stock[p.addr], p.spec, kerf) if v3.ok else None
    return GateReport(verdicts=verdicts, plan=plan)
