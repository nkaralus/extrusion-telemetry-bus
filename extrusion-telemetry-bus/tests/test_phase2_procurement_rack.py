import base64

import pytest
from fastapi.testclient import TestClient

from extrusion_bus.bus import Disposition, FabricationBus
from extrusion_bus.llm import RuleParser
from extrusion_bus.nesting import compile_cut_list
from extrusion_bus.physics import CutSpec, End
from extrusion_bus.procurement import DAY, reorder_report
from extrusion_bus.registry import Registry
from extrusion_bus.server import create_app

from conftest import PN, make_proposal


class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, days): self.t += days * DAY


@pytest.fixture
def clk():
    return Clock()


@pytest.fixture
def sreg(clk):  # registry with a controllable clock
    return Registry(clock=clk)


def burn_days(reg, clk, days, per_day=1):
    """Pull `per_day` virgin sticks each day for `days` days (ends on the last pull day)."""
    for _ in range(days):
        clk.advance(1)
        for _ in range(per_day):
            reg.consume(next(i.addr for i in reg.state.stock.values() if i.kind.value == "raw"))


def alert(reg, **kw):
    return next(a for a in reorder_report(reg, **kw) if a.profile == PN)


# ---- procurement ---------------------------------------------------------------- #
def test_stockout_risk_when_cover_is_shorter_than_lead_time(sreg, clk):
    sreg.add_raw(PN, 288, count=10)
    burn_days(sreg, clk, 7)                      # 1 stick/day, 3 left
    a = alert(sreg)
    assert a.burn_sticks_per_day == 1.0 and a.days_of_cover == 3.0 and a.on_hand_sticks == 3
    assert a.status == "STOCKOUT_RISK" and a.suggested_order_sticks >= 1


def test_reorder_now_inside_lead_plus_safety(sreg, clk):
    sreg.add_raw(PN, 288, count=13)
    burn_days(sreg, clk, 7)                      # 6 left -> 6 d cover; lead 5 <= 6 <= 7
    a = alert(sreg)
    assert a.status == "REORDER_NOW" and a.days_of_cover == 6.0
    assert a.suggested_order_sticks == 8         # ceil(1*(5+2+7) - 6)


def test_ok_when_cover_is_comfortable(sreg, clk):
    sreg.add_raw(PN, 288, count=30)
    burn_days(sreg, clk, 7)
    assert alert(sreg).status == "OK" and alert(sreg).suggested_order_sticks == 0


def test_open_order_that_lands_in_time_suppresses_the_alert(sreg, clk):
    sreg.add_raw(PN, 288, count=10)
    burn_days(sreg, clk, 7)                      # 3 d of cover
    sreg.place_order(PN, 10, eta_days=2)         # lands before the rack is empty
    a = alert(sreg)
    assert a.status == "OK" and a.on_order_sticks == 10 and a.days_of_cover == 13.0


def test_open_order_that_lands_too_late_does_not_count(sreg, clk):
    sreg.add_raw(PN, 288, count=10)
    burn_days(sreg, clk, 7)
    sreg.place_order(PN, 10, eta_days=4)         # rack is empty at day 3
    assert alert(sreg).status == "STOCKOUT_RISK"


def test_no_history_hard_floor_and_stale_window(sreg, clk):
    sreg.add_raw(PN, 288, count=2)
    a = alert(sreg)
    assert a.status == "OK" and a.days_of_cover is None and a.reason == "no recent consumption"
    sreg.state.profiles[PN].min_stock_sticks = 5
    assert alert(sreg).status == "REORDER_NOW"
    sreg.state.profiles[PN].min_stock_sticks = 0
    burn_days(sreg, clk, 1)
    clk.advance(30)                              # old pull falls outside the 14-day window
    assert alert(sreg).burn_sticks_per_day == 0.0


def test_remnant_use_is_not_virgin_consumption(sreg, clk, ):
    bus = FabricationBus(sreg)
    sreg.add_raw(PN, 288, count=3)
    rem = sreg.add_remnant(PN, 80, End.square())
    clk.advance(1)
    bus.commit(make_proposal(rem.addr, length_in=58))
    assert sreg.state.raw_usage == []


def test_receive_registers_sticks_and_retires_orders_fifo(sreg):
    sreg.place_order(PN, 6); sreg.place_order(PN, 4)
    items = sreg.receive(PN, 8)
    assert len(items) == 8 and all(i.addr.startswith("10.2.1.") for i in items)
    assert [o["qty"] for o in sreg.state.on_order] == [2]        # 6 retired, 4 -> 2


def test_place_order_defaults_to_vendor_lead_time(sreg, clk):
    o = sreg.place_order(PN, 5)
    assert o["eta_ts"] == clk() + 5 * DAY


def test_commit_consumption_feeds_the_burn_rate(sreg, clk):
    bus = FabricationBus(sreg)
    ids = [i.addr for i in sreg.add_raw(PN, 288, count=4)]
    for a in ids[:2]:
        clk.advance(1)
        bus.commit(make_proposal(a, length_in=58))
    assert len(sreg.state.raw_usage) == 2


# ---- rack capacity gate (85%) -------------------------------------------------------- #
@pytest.fixture
def rack(reg, bus):
    reg.state.profiles[PN].rack_slots = 20       # 85% of 20 = 17 -> accept while count < 17
    return reg


def fill(reg, n):
    return [reg.add_remnant(PN, 50, End.square()) for _ in range(n)]


def test_remainder_racks_below_85_percent(rack, bus):
    fill(rack, 16)                                # 80%
    src = rack.add_raw(PN, 288)[0]
    r = bus.commit(make_proposal(src.addr, length_in=58))
    assert r.disposition == Disposition.REMNANT_RACK and r.label is not None and r.scrap_reason is None
    assert rack.rack_utilization(PN)[:2] == (17, 20)


def test_remainder_goes_to_scrap_at_85_percent(rack, bus):
    fill(rack, 17)                                # exactly 85%: rule is strictly < 85%
    src = rack.add_raw(PN, 288)[0]
    r = bus.commit(make_proposal(src.addr, length_in=58))     # 229 7/8" drop qualifies on length
    assert r.disposition == Disposition.SCRAP_BIN and r.scrap_reason == "RACK_FULL"
    assert r.label is None and r.screen == "SCRAP BIN (RACK FULL)" and r.leftover_addr is None
    e = rack.state.scrap_ledger[-1]
    assert e.reason == "RACK_FULL" and e.length_in == 229.875 and e.weight_lb == round(1.05 * 229.875 / 12, 3)
    assert rack.rack_utilization(PN)[0] == 17


def test_source_remnant_frees_its_slot_before_the_gate(rack, bus):
    drops = fill(rack, 17)
    big = rack.add_remnant(PN, 120, End.square())            # 18 racked: full
    r = bus.commit(make_proposal(big.addr, length_in=50))    # source leaves the rack -> 17? still >= 85%
    assert r.scrap_reason == "RACK_FULL"
    rack.consume(drops[0].addr)                              # drop to 16 racked
    r2 = bus.commit(make_proposal(drops[1].addr, length_in=10))   # 50" - 10 -> 39.875 sq, source freed: 14 -> racks
    assert r2.disposition == Disposition.REMNANT_RACK


def test_under_min_still_reports_its_own_reason(rack, bus):
    fill(rack, 17)
    src = rack.add_remnant(PN, 60, End.square())
    r = bus.commit(make_proposal(src.addr, length_in=58))
    assert r.scrap_reason == "DROP_UNDER_MIN"


def test_racks_are_per_profile(rack, bus):
    fill(rack, 17)
    other = rack.add_raw("EXT-220-OPEN", 288)[0]
    r = bus.commit(make_proposal(other.addr, profile="EXT-220-OPEN", length_in=58))
    assert r.disposition == Disposition.REMNANT_RACK


def test_adopting_a_legacy_drop_is_not_blocked_by_a_full_rack(rack, bus):
    fill(rack, 18)
    assert bus.adopt_drop(PN, 80, End.square()).addr             # operator is holding it: always registered


def test_nest_plan_applies_the_rack_gate(rack, bus):
    fill(rack, 17)
    rack.add_raw(PN, 288)
    plan = compile_cut_list(bus, "J", [CutSpec(profile=PN, length_in=58)] * 2)
    assert plan.steps[0].scrap_reason == "RACK_FULL" and plan.steps[0].disposition == "SCRAP_BIN"
    assert plan.scrap_lb > 200 * 1.05 / 12


# ---- HTTP -------------------------------------------------------------------------- #
def test_procurement_endpoints(reg):
    reg.add_raw(PN, 288, count=2)
    c = TestClient(create_app(reg, parser=RuleParser(reg)))
    rows = {r["profile"]: r for r in c.get("/api/procurement/reorder").json()}
    assert rows[PN]["on_hand_sticks"] == 2 and rows[PN]["status"] == "OK"
    o = c.post("/api/procurement/order", json={"profile": PN, "qty": 6}).json()
    assert o["qty"] == 6
    got = c.post("/api/procurement/receive", json={"profile": PN, "qty": 6}).json()
    assert len(got["received"]) == 6 and c.get("/health").json()["raw"] == 8
    assert reg.state.on_order == []


def test_commit_endpoint_reports_rack_full(reg):
    reg.state.profiles[PN].rack_slots = 5
    for _ in range(5):
        reg.add_remnant(PN, 50, End.square())
    src = reg.add_raw(PN, 288)[0]
    c = TestClient(create_app(reg, parser=RuleParser(reg)))
    r = c.post("/cut/complete", json={"addr": src.addr, "station": "SAW_1", "job_id": "J",
                                      "spec": {"profile": PN, "length_in": 58}}).json()
    assert r["disposition"] == "SCRAP_BIN" and r["scrap_reason"] == "RACK_FULL"
