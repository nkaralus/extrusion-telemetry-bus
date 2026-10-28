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
from typing import List, Optional

from pydantic import BaseModel

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


def reorder_report(reg: Registry, *, window_days: float = 14.0, safety_days: float = 2.0,
                   review_days: float = 7.0, now: Optional[float] = None) -> List[ReorderAlert]:
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
        out.append(ReorderAlert(
            profile=pn, status=status, reason=reason, on_hand_sticks=on_hand,
            on_order_sticks=incoming_all, burn_sticks_per_day=round(burn, 3),
            days_of_cover=None if cover is None else round(cover, 2),
            lead_time_days=prof.lead_time_days, suggested_order_sticks=suggest))
    return out
