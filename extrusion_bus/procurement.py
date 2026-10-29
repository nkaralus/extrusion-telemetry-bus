"""Stock depletion vs vendor latency: reorder alerts before the raw rack runs dry.

burn  = virgin sticks pulled in the trailing window / days observed (since the shop started logging, min 1 day)
cover = (on hand + open orders that land before stockout) / burn
status:
    STOCKOUT_RISK  cover < lead time   (an order placed now still lands late)
    REORDER_NOW    cover <= lead + safety days, or below the profile's hard floor
    OK
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .bus import FabricationBus
from .mule_frame import FramePhase
from .nesting import _orderings, _simulate_ex
from .registry import Registry, StockKind

DAY = 86400.0


class ReorderAlert(BaseModel):
    profile: str
    status: str                  # OK | REORDER_NOW | STOCKOUT_RISK
    reason: str
    on_hand_sticks: int
    on_order_sticks: int
    burn_sticks_per_day: float
    days_of_cover: Optional[float]   # None when nothing is being consumed
    lead_time_days: float
    suggested_order_sticks: int
    # ---- lookahead (released / scheduled jobs on 10.4.1.x carts); all defaults when no forecast is supplied
    committed_sticks: int = 0                 # virgin sticks the released, uncut pieces will pull
    committed_in_horizon: int = 0
    breach_in_days: Optional[int] = None      # first day stock would fall under the safety floor
    stockout_in_days: Optional[int] = None    # first day stock would hit zero
    order_by_in_days: Optional[float] = None  # breach - vendor lead time (negative = already late)
    early_po: bool = False
    driving_jobs: List[str] = Field(default_factory=list)


def reorder_report(reg: Registry, *, window_days: float = 14.0, safety_days: float = 2.0,
                   review_days: float = 7.0, now: Optional[float] = None, forecast: Optional["Forecast"] = None,
                   early_warning_days: float = 3.0) -> List[ReorderAlert]:
    now = reg.clock() if now is None else now
    out: List[ReorderAlert] = []
    for pn, prof in sorted(reg.state.profiles.items()):
        on_hand = len(reg.stock_of(pn, StockKind.RAW))
        orders = sorted((o for o in reg.state.on_order if o["profile"] == pn), key=lambda o: o["eta_ts"])
        incoming_all = sum(o["qty"] for o in orders)
        evts = [e for e in reg.state.raw_usage if e["profile"] == pn and e["ts"] >= now - window_days * DAY]
        burn = 0.0
        if evts:
            start = min(e["ts"] for e in evts)
            if reg.state.journal:                  # observation began when the shop started logging,
                start = min(start, reg.state.journal[0]["ts"])   # not at the first pull
            span = min(window_days, max(1.0, (now - start) / DAY))
            burn = len(evts) / span

        cover: Optional[float] = None
        landed = 0
        if burn > 0:
            cover = on_hand / burn
            for o in orders:                      # orders landing before the shelf is empty extend cover
                if (o["eta_ts"] - now) / DAY <= cover:
                    landed += o["qty"]
                    cover = (on_hand + landed) / burn
        status, reason = "OK", "cover exceeds lead time + safety"
        if burn > 0 and cover < prof.lead_time_days:
            status, reason = "STOCKOUT_RISK", f"{cover:.1f} d of cover < {prof.lead_time_days:g} d lead time"
        elif burn > 0 and cover <= prof.lead_time_days + safety_days:
            status, reason = "REORDER_NOW", f"{cover:.1f} d of cover <= lead {prof.lead_time_days:g} d + {safety_days:g} d safety"
        if status == "OK" and on_hand + incoming_all < prof.min_stock_sticks:
            status, reason = "REORDER_NOW", f"below hard floor of {prof.min_stock_sticks} sticks"
        if burn == 0 and status == "OK":
            reason = "no recent consumption"
        suggest = 0
        if status != "OK":
            need = burn * (prof.lead_time_days + safety_days + review_days)
            suggest = max(math.ceil(need - on_hand - incoming_all), max(0, prof.min_stock_sticks - on_hand - incoming_all), 1)
        alert = ReorderAlert(
            profile=pn, status=status, reason=reason, on_hand_sticks=on_hand,
            on_order_sticks=incoming_all, burn_sticks_per_day=round(burn, 3),
            days_of_cover=None if cover is None else round(cover, 2),
            lead_time_days=prof.lead_time_days, suggested_order_sticks=suggest)
        if forecast is not None:
            alert = apply_lookahead(alert, reg=reg, forecast=forecast, now=now, safety_days=safety_days,
                                    review_days=review_days, early_warning_days=early_warning_days)
        out.append(alert)
    return out


# --------------------------------------------------------------------------- #
# Lookahead: forward demand from released jobs
# --------------------------------------------------------------------------- #
STATUS_RANK = {"OK": 0, "EARLY_PO": 1, "REORDER_NOW": 2, "STOCKOUT_RISK": 3}


class DemandEvent(BaseModel):
    day: float          # days from now the material is needed (0 = now / overdue)
    sticks: int
    job_id: str
    frame_id: str


class Forecast(BaseModel):
    by_profile: Dict[str, List[DemandEvent]] = Field(default_factory=dict)
    frames: int = 0


def committed_demand(bus: FabricationBus, now: Optional[float] = None) -> Forecast:
    """Virgin sticks the released-but-uncut pieces will pull, by profile and due day.

    Dry-runs the real nest compiler (drops first, reservations respected) over every active frame in
    due-date order, each on the stock the previous ones leave behind, so a job that depends on a drop
    an earlier job will consume is priced honestly.  Never touches the live registry.
    """
    now = bus.reg.clock() if now is None else now
    frames = sorted((f for f in bus.frames.values() if f.phase == FramePhase.ACTIVE and f.pending),
                    key=lambda f: (f.due_ts if f.due_ts is not None else now, f.frame_id))
    out = Forecast(frames=len(frames))
    state = bus.reg.state
    for f in frames:
        order = _orderings(list(f.pending))[0][1]                       # best-fit decreasing, as the compiler does
        plan, scratch = _simulate_ex(bus, f.job_id, "lookahead", order, True, True, base_state=state)
        state = scratch.state
        day = max(0.0, ((f.due_ts if f.due_ts is not None else now) - now) / DAY)
        per: Dict[str, set] = {}
        for s in plan.steps:
            if s.source_kind in ("raw", "purchased"):
                per.setdefault(s.spec.profile, set()).add(s.source_addr)
        for pn, addrs in per.items():
            out.by_profile.setdefault(pn, []).append(DemandEvent(day=round(day, 3), sticks=len(addrs),
                                                                 job_id=f.job_id, frame_id=f.frame_id))
    return out


class Projection(BaseModel):
    levels: List[float]
    breach_day: Optional[int]
    stockout_day: Optional[int]
    safety_sticks: int


def project_levels(*, on_hand: int, events: List[DemandEvent], arrivals: List[tuple], burn_per_day: float,
                   safety_sticks: int, horizon_days: int) -> Projection:
    """Daily end-of-day stick level.  Usage each day = max(committed jobs due that day, historical baseline):
    the baseline stands in for unannounced work and is never added on top of the same sticks."""
    level = float(on_hand)
    levels: List[float] = []
    applied = [False] * len(arrivals)
    breach = stockout = None
    for d in range(horizon_days + 1):
        for i, (eta_day, qty) in enumerate(arrivals):
            if not applied[i] and eta_day <= d:
                level += qty
                applied[i] = True
        committed = sum(e.sticks for e in events if int(e.day) == d)
        level -= max(float(committed), burn_per_day)
        levels.append(round(level, 3))
        if breach is None and level < safety_sticks:
            breach = d
        if stockout is None and level < 0:
            stockout = d
    return Projection(levels=levels, breach_day=breach, stockout_day=stockout, safety_sticks=safety_sticks)


def apply_lookahead(alert: ReorderAlert, *, reg: Registry, forecast: Forecast, now: float, safety_days: float,
                    review_days: float, early_warning_days: float) -> ReorderAlert:
    prof = reg.state.profiles[alert.profile]
    events = forecast.by_profile.get(alert.profile, [])
    lead = prof.lead_time_days
    horizon = int(min(90, max(30, math.ceil(lead + safety_days + review_days + early_warning_days))))
    arrivals = [((o["eta_ts"] - now) / DAY, o["qty"]) for o in reg.state.on_order if o["profile"] == alert.profile]
    safety = max(prof.min_stock_sticks, math.ceil(alert.burn_sticks_per_day * safety_days))
    proj = project_levels(on_hand=alert.on_hand_sticks, events=events, arrivals=arrivals,
                          burn_per_day=alert.burn_sticks_per_day, safety_sticks=safety, horizon_days=horizon)
    alert.committed_sticks = sum(e.sticks for e in events)
    alert.committed_in_horizon = sum(e.sticks for e in events if e.day <= horizon)
    alert.breach_in_days, alert.stockout_in_days = proj.breach_day, proj.stockout_day
    status, reason = alert.status, alert.reason
    if proj.breach_day is not None:
        alert.order_by_in_days = round(proj.breach_day - lead, 2)
        alert.driving_jobs = sorted({e.job_id for e in events if e.day <= proj.breach_day})
        late = alert.order_by_in_days <= 0
        if proj.stockout_day is not None and proj.stockout_day < lead:
            p_status = "STOCKOUT_RISK"
            p_reason = f"released jobs run stock to zero on day {proj.stockout_day}, before an order placed now ({lead:g} d) can land"
        elif late:
            p_status = "REORDER_NOW"
            p_reason = f"released jobs push stock under the safety floor ({safety}) on day {proj.breach_day}; order now"
        elif alert.order_by_in_days <= early_warning_days:
            p_status = "EARLY_PO"
            alert.early_po = True
            p_reason = (f"released jobs push stock under the safety floor ({safety}) on day {proj.breach_day}; "
                        f"place the PO within {alert.order_by_in_days:g} d to land in time")
        else:
            p_status, p_reason = "OK", reason
        if STATUS_RANK[p_status] > STATUS_RANK[status]:
            status, reason = p_status, p_reason
        # sticks to order now so the level stays at/above the floor once the order can land
        first = min(len(proj.levels) - 1, math.ceil(lead))
        last = min(len(proj.levels) - 1, math.ceil(lead + safety_days + review_days))     # same cover window as the history rule
        need = math.ceil(max(0.0, safety - min(proj.levels[first:max(first, last) + 1])))
        alert.suggested_order_sticks = max(alert.suggested_order_sticks, need if status != "OK" else 0)
    alert.status, alert.reason = status, reason
    return alert


def po_draft(alerts: List[ReorderAlert], now: float) -> List[dict]:
    """What to order, how many, and by when, for every profile that needs a PO."""
    out = []
    for a in alerts:
        if a.status != "OK" and a.suggested_order_sticks > 0:
            by = None if a.order_by_in_days is None else now + max(0.0, a.order_by_in_days) * DAY
            out.append({"profile": a.profile, "qty": a.suggested_order_sticks, "status": a.status,
                        "order_by_ts": by, "order_by_in_days": a.order_by_in_days, "lead_time_days": a.lead_time_days,
                        "jobs": a.driving_jobs, "reason": a.reason})
    return sorted(out, key=lambda r: (-STATUS_RANK[r["status"]], r["profile"]))
