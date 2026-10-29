import math
import time

import pytest
from fastapi.testclient import TestClient

from extrusion_bus.bus import FabricationBus
from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import CutSpec, End
from extrusion_bus.procurement import (
    DAY, DemandEvent, Forecast, committed_demand, po_draft, project_levels, reorder_report,
)
from extrusion_bus.registry import Registry
from extrusion_bus.server import create_app

from conftest import PN
from test_phase4_viewports import make_ready


class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, days): self.t += days * DAY


def specs(length, n, profile=PN):
    return [CutSpec(profile=profile, length_in=length) for _ in range(n)]


def rig(sticks=6, clk=None, profile=PN):
    clk = clk or Clock()
    reg = Registry(clock=clk)
    reg.add_raw(profile, 288, count=sticks)
    return reg, FabricationBus(reg, persist_frames=False), clk


def demand(bus, pn=PN):
    fc = committed_demand(bus)
    return fc, sum(e.sticks for e in fc.by_profile.get(pn, []))


def alert(reg, fc, pn=PN, **kw):
    return next(a for a in reorder_report(reg, forecast=fc, **kw) if a.profile == pn)


def burn(reg, clk, days, per_day=1):
    for _ in range(days):
        clk.advance(1)
        for _ in range(per_day):
            reg.consume(next(i.addr for i in reg.state.stock.values() if i.kind.value == "raw"))


# ============================ committed demand ============================ #
def test_released_jobs_translate_into_virgin_sticks_due_by_day():
    reg, bus, clk = rig(sticks=2)
    bus.frame_from_specs("J1", specs(150, 6), due_ts=clk() + 7.5 * DAY)            # one 150" piece per 24' stick
    fc, total = demand(bus)
    assert total == 6 and fc.frames == 1                                              # 2 on the rack + 4 that must be bought
    (ev,) = fc.by_profile[PN]
    assert (ev.job_id, ev.sticks, ev.day) == ("J1", 6, 7.5)


def test_drops_on_the_rack_reduce_the_stick_demand_honestly():
    reg, bus, clk = rig(sticks=2)
    reg.add_remnant(PN, 200, End.square())
    bus.frame_from_specs("J1", specs(150, 6))
    assert demand(bus)[1] == 5                                                         # one piece comes off the drop


def test_overdue_and_undated_jobs_count_as_today():
    reg, bus, clk = rig()
    bus.frame_from_specs("LATE", specs(150, 1), due_ts=clk() - 3 * DAY)
    bus.frame_from_specs("NODATE", specs(150, 1))
    assert sorted(e.day for e in committed_demand(bus).by_profile[PN]) == [0.0, 0.0]


def test_jobs_are_chained_in_due_order_on_the_stock_earlier_jobs_leave_behind():
    # job A's 150" pieces leave 137 7/8" drops; job B's 120" pieces can live on those drops
    reg, bus, clk = rig(sticks=10)
    bus.frame_from_specs("A", specs(150, 4), due_ts=clk() + 1 * DAY)
    bus.frame_from_specs("B", specs(120, 3), due_ts=clk() + 5 * DAY)
    fc = committed_demand(bus)
    assert {e.job_id: e.sticks for e in fc.by_profile[PN]} == {"A": 4}               # B needs no virgin stock at all
    # swap the order: B (due first) has no drops to use and pulls 2 sticks; two of its leftovers (47 3/4") are
    # useless to A's 150" pieces, but the third stick's 167 7/8" leftover is not, so A needs 3, not 4
    reg2, bus2, clk2 = rig(sticks=10)
    bus2.frame_from_specs("A", specs(150, 4), due_ts=clk2() + 5 * DAY)
    bus2.frame_from_specs("B", specs(120, 3), due_ts=clk2() + 1 * DAY)
    assert {e.job_id: e.sticks for e in committed_demand(bus2).by_profile[PN]} == {"B": 2, "A": 3}


def test_only_pieces_still_to_cut_on_active_frames_count():
    reg, bus, clk = rig(sticks=10)
    f = bus.frame_from_specs("J", specs(150, 5))
    bus.stage_next_cut(f); bus.ack(f, "CUT_OK"); bus.ack(f, "CUT_OK")                   # two already cut
    assert demand(bus)[1] == 3
    done = bus.frame_from_specs("DONE", [])
    bus.stage_next_cut(done)
    dead = FabricationBus(reg, parser=lambda s, f: "garbage", persist_frames=False).open_frame("DLQ", "x")
    assert demand(bus)[1] == 3 and committed_demand(bus).frames == 1


def test_forecasting_never_touches_the_live_registry():
    reg, bus, clk = rig(sticks=3)
    f = bus.frame_from_specs("J", specs(150, 6)); bus.stage_next_cut(f)
    before = (reg.state.model_dump(), f.model_dump())
    committed_demand(bus); reorder_report(reg, forecast=committed_demand(bus))
    assert (reg.state.model_dump(), f.model_dump()) == before


def test_each_profile_is_forecast_separately():
    reg, bus, clk = rig(sticks=4)
    reg.add_raw("EXT-220-BOX", 288, count=4)
    bus.frame_from_specs("J", specs(150, 3) + specs(150, 2, "EXT-220-BOX"))
    fc = committed_demand(bus)
    assert sum(e.sticks for e in fc.by_profile[PN]) == 3 and sum(e.sticks for e in fc.by_profile["EXT-220-BOX"]) == 2


# ============================ the projection ============================ #
def test_projection_uses_the_larger_of_committed_and_baseline_never_the_sum():
    ev = [DemandEvent(day=3, sticks=12, job_id="J", frame_id="f")]
    p = project_levels(on_hand=10, events=ev, arrivals=[], burn_per_day=1.0, safety_sticks=0, horizon_days=6)
    assert p.levels[:4] == [9.0, 8.0, 7.0, -5.0] and p.stockout_day == 3 and p.breach_day == 3
    one = project_levels(on_hand=10, events=[DemandEvent(day=0, sticks=1, job_id="J", frame_id="f")],
                         arrivals=[], burn_per_day=1.0, safety_sticks=0, horizon_days=1)
    assert one.levels == [9.0, 8.0]                                                    # not 8.0 on day 0: the baseline IS that stick


def test_arrivals_before_the_breach_save_you_and_after_it_do_not():
    ev = [DemandEvent(day=3, sticks=12, job_id="J", frame_id="f")]
    kw = dict(on_hand=10, events=ev, burn_per_day=1.0, safety_sticks=0, horizon_days=8)
    assert project_levels(arrivals=[(2.0, 20)], **kw).stockout_day is None
    assert project_levels(arrivals=[(4.0, 20)], **kw).stockout_day == 3
    mid = project_levels(arrivals=[(2.4, 20)], **kw)                                    # lands during day 2, so it is on the shelf for day 3
    assert mid.stockout_day is None and mid.levels[3] == 15.0


# ============================ statuses ============================ #
def prof(reg, **kw):
    for k, v in kw.items():
        setattr(reg.state.profiles[PN], k, v)


def test_the_alert_walks_ok_then_early_po_then_reorder_now_as_the_job_gets_closer():
    reg, bus, clk = rig(sticks=6)
    prof(reg, min_stock_sticks=4, lead_time_days=5)
    bus.frame_from_specs("J", specs(150, 3), due_ts=clk() + 12 * DAY)                  # 6 on hand - 3 = 3 < floor of 4
    a = alert(reg, committed_demand(bus))
    assert a.status == "OK" and a.breach_in_days == 12 and a.order_by_in_days == 7 and not a.early_po
    clk.advance(5)                                                                      # due in 7 d: order_by = 2 <= 3 d warning
    a = alert(reg, committed_demand(bus))
    assert a.status == "EARLY_PO" and a.early_po and a.order_by_in_days == 2 and a.driving_jobs == ["J"]
    assert a.suggested_order_sticks >= 1 and "within 2 d" in a.reason
    clk.advance(3)                                                                      # order_by = -1: the PO is already late
    a = alert(reg, committed_demand(bus))
    assert a.status == "REORDER_NOW" and a.order_by_in_days == -1 and "order now" in a.reason


def test_a_shortfall_that_lands_inside_the_vendor_lead_time_is_a_stockout_risk():
    reg, bus, clk = rig(sticks=6)
    prof(reg, min_stock_sticks=3, lead_time_days=5)
    bus.frame_from_specs("BIG", specs(150, 8))                                          # needs 8, 6 on hand, due today
    a = alert(reg, committed_demand(bus))
    assert a.status == "STOCKOUT_RISK" and a.stockout_in_days == 0 and a.committed_sticks == 8 and "zero on day 0" in a.reason
    assert a.suggested_order_sticks == 5                                                # ceil(floor 3 - (6 - 8)) once an order can land


def test_lookahead_sees_what_history_alone_cannot():
    reg, bus, clk = rig(sticks=17)
    burn(reg, clk, 7)                                                                   # 1 stick/day, 10 on the rack
    history = alert(reg, None)
    assert history.status == "OK" and history.days_of_cover == 10.0 and history.committed_sticks == 0
    bus.frame_from_specs("RUSH", specs(150, 12), due_ts=clk() + 3 * DAY)                # a released job wants 12 sticks on day 3
    ahead = alert(reg, committed_demand(bus))
    assert ahead.status == "STOCKOUT_RISK" and ahead.stockout_in_days == 3 and ahead.days_of_cover == 10.0
    assert ahead.driving_jobs == ["RUSH"] and ahead.suggested_order_sticks >= 3


def test_status_is_the_worse_of_history_and_pipeline():
    reg, bus, clk = rig(sticks=10)
    burn(reg, clk, 7)                                                                   # 3 left, burning 1/day: history says STOCKOUT_RISK
    bus.frame_from_specs("TINY", specs(150, 1), due_ts=clk() + 30 * DAY)
    a = alert(reg, committed_demand(bus))
    assert a.status == "STOCKOUT_RISK" and "of cover" in a.reason                       # history's verdict survives a calm pipeline


def test_an_open_order_that_lands_in_time_clears_the_alert_and_a_late_one_does_not():
    reg, bus, clk = rig(sticks=6)
    prof(reg, min_stock_sticks=3, lead_time_days=5)
    bus.frame_from_specs("BIG", specs(150, 8), due_ts=clk() + 6 * DAY)
    first = alert(reg, committed_demand(bus))
    assert first.status == "EARLY_PO" and first.order_by_in_days == 1                    # due in 6 d, vendor needs 5: order within a day
    reg.place_order(PN, 6, eta_days=4)                                                  # lands before day 6
    ok = alert(reg, committed_demand(bus))
    assert ok.status == "OK" and ok.on_order_sticks == 6
    reg.state.on_order.clear(); reg.place_order(PN, 6, eta_days=9)                      # lands after the job needs it
    assert alert(reg, committed_demand(bus)).status == "EARLY_PO"


def test_with_no_released_jobs_lookahead_only_ever_warns_earlier_never_later():
    rank = {"OK": 0, "EARLY_PO": 1, "REORDER_NOW": 2, "STOCKOUT_RISK": 3}
    for burned in (4, 6, 9):
        reg, bus, clk = rig(sticks=16)
        burn(reg, clk, burned)
        old = {a.profile: a for a in reorder_report(reg)}[PN]
        new = alert(reg, committed_demand(bus))
        assert new.committed_sticks == 0 and rank[new.status] >= rank[old.status]
        if old.status != "OK":
            assert new.status == old.status                                              # the history verdict is never softened
    reg, bus, clk = rig(sticks=16)
    burn(reg, clk, 7)                                                                    # 9 left at 1/day: history says OK (9 > lead 5 + safety 2)...
    assert alert(reg, None).status == "OK" and alert(reg, committed_demand(bus)).status == "EARLY_PO"   # ...the projection warns a few days sooner


def test_other_profiles_are_not_alarmed_by_one_profiles_jobs():
    reg, bus, clk = rig(sticks=2)
    reg.add_raw("EXT-220-BOX", 288, count=20)
    bus.frame_from_specs("J", specs(150, 8))
    fc = committed_demand(bus)
    assert alert(reg, fc).status != "OK" and alert(reg, fc, "EXT-220-BOX").status == "OK"


def test_po_draft_lists_what_to_buy_how_many_and_by_when(request):
    reg, bus, clk = rig(sticks=6)
    prof(reg, min_stock_sticks=3, lead_time_days=5)
    bus.frame_from_specs("BIG", specs(150, 8), due_ts=clk() + 2 * DAY)
    alerts = reorder_report(reg, forecast=committed_demand(bus))
    (line,) = po_draft(alerts, clk())
    assert line["profile"] == PN and line["qty"] >= 3 and line["jobs"] == ["BIG"] and line["status"] in ("REORDER_NOW", "STOCKOUT_RISK")
    assert line["order_by_ts"] == clk() and line["lead_time_days"] == 5                  # late: order today
    assert po_draft([a for a in alerts if a.profile != PN], clk()) == []                  # healthy profiles draft nothing


# ============================ API, release, alerts ============================ #
@pytest.fixture
def api(tmp_path):
    reg = Registry.load(tmp_path / "bus.db")
    reg.add_raw(PN, 288, count=6); reg.add_raw("EXT-220-BOX", 288, count=40)
    app = create_app(reg, parser=RuleParser(reg), media_dir=tmp_path / "media", auth=False)
    with TestClient(app) as c:
        yield reg, app, c
    reg.close()


def test_release_records_when_the_material_is_needed_and_reports_the_purchase(api):
    reg, app, c = api
    make_ready(c, "L1")
    assert c.post("/api/engineering/release", json={"job_id": "L1", "due_in_days": -1}).status_code == 422
    assert c.post("/api/engineering/release", json={"job_id": "L1", "due_in_days": 400}).status_code == 422
    r = c.post("/api/engineering/release", json={"job_id": "L1", "due_in_days": 4, "overrides": {}})
    assert r.status_code == 409 or r.status_code == 200
    if r.status_code == 409:                                                             # 6 open-profile sticks may not cover the job
        reg.add_raw(PN, 288, count=20)
        r = c.post("/api/engineering/release", json={"job_id": "L1", "due_in_days": 4})
    assert r.status_code == 200, r.text
    f = app.state.bus.frames[r.json()["frame_id"]]
    assert f.due_ts == pytest.approx(reg.clock() + 4 * 86400, abs=5)
    assert reg.state.frames[f.frame_id]["frame"]["due_ts"] == f.due_ts                  # persisted with the frame


def test_a_release_that_drains_a_profile_raises_one_po_alert_not_a_flood(api):
    reg, app, c = api
    reg.state.profiles[PN].min_stock_sticks = 15
    make_ready(c, "A1")
    reg.add_raw(PN, 288, count=12)                                                        # enough to cut, but under the floor of 15 after the job
    got = []
    with c.websocket_connect("/ws/saw/SAW_1") as ws:
        ws.receive_json()
        r = c.post("/api/engineering/release", json={"job_id": "A1", "due_in_days": 1})
        assert r.status_code == 200, r.text
        alerts = r.json()["po_alerts"]
        assert alerts and alerts[0]["profile"] == PN and alerts[0]["status"] != "OK"
        msg = ws.receive_json()
        while msg["type"] != "po_alert":
            msg = ws.receive_json()
        got.append(msg)
    assert got[0]["alerts"][0]["profile"] == PN and "A1" in got[0]["why"]
    audit = [a for a in reg.state.audit if a["action"] == "PO_ALERT"]
    assert len(audit) == 1 and audit[0]["profile"] == PN and audit[0]["qty"] >= 1
    c.get("/api/procurement/reorder"); c.get("/api/procurement/reorder")                  # reading never alerts at all
    assert len([a for a in reg.state.audit if a["action"] == "PO_ALERT"]) == 1
    status = lambda: next(a for a in c.get("/api/procurement/reorder").json() if a["profile"] == PN)["status"]
    assert status() != "OK"
    c.post("/api/procurement/receive", json={"profile": PN, "qty": 1})                    # re-evaluates, but nothing got WORSE
    assert status() != "OK" and len([a for a in reg.state.audit if a["action"] == "PO_ALERT"]) == 1
    c.post("/api/procurement/order", json={"profile": PN, "qty": 20, "eta_days": 1})      # ordering resolves it
    assert next(a for a in c.get("/api/procurement/reorder").json() if a["profile"] == PN)["status"] == "OK"


def test_reorder_endpoint_lookahead_switch_and_po_draft(api):
    reg, app, c = api
    reg.state.profiles[PN].min_stock_sticks = 3
    app.state.bus.frame_from_specs("RAW-JOB", specs(150, 9), due_ts=reg.clock() + 1 * DAY)
    with_ = {a["profile"]: a for a in c.get("/api/procurement/reorder").json()}[PN]
    without = {a["profile"]: a for a in c.get("/api/procurement/reorder?lookahead=false").json()}[PN]
    assert with_["committed_sticks"] == 9 and with_["status"] == "STOCKOUT_RISK" and without["committed_sticks"] == 0
    draft = c.get("/api/procurement/po-draft").json()["lines"]
    assert draft[0]["profile"] == PN and draft[0]["qty"] >= 4 and draft[0]["jobs"] == ["RAW-JOB"]
    la = c.get("/api/procurement/lookahead?days=14").json()
    assert la["frames"] == 1 and len(la["profiles"][PN]["levels"]) == 15 and la["profiles"][PN]["status"] == "STOCKOUT_RISK"
    assert la["profiles"][PN]["events"][0]["sticks"] == 9 and la["profiles"]["EXT-220-BOX"]["status"] == "OK"


def test_batch_release_inherits_the_earliest_due_date(api):
    reg, app, c = api
    bus = app.state.bus
    a = bus.frame_from_specs("A", specs(60, 2), due_ts=reg.clock() + 9 * DAY)
    b = bus.frame_from_specs("B", specs(70, 2), due_ts=reg.clock() + 3 * DAY)
    from extrusion_bus.batch import release_batch
    _, bf = release_batch(bus, ["A", "B"])
    assert bf.due_ts == b.due_ts


def test_due_dates_survive_a_restart(tmp_path):
    reg = Registry.load(tmp_path / "bus.db"); reg.add_raw(PN, 288, count=3)
    bus = FabricationBus(reg)
    f = bus.frame_from_specs("J", specs(100, 2), due_ts=1234.5)
    reg.close()
    reg2 = Registry.load(tmp_path / "bus.db")
    from extrusion_bus.recovery import restore
    bus2 = FabricationBus(reg2); restore(bus2)
    assert bus2.frames[f.frame_id].due_ts == 1234.5
    reg2.close()


def test_procurement_permissions_engineers_read_only_admin_orders(tmp_path):
    reg = Registry.load(tmp_path / "bus.db")
    app = create_app(reg, parser=lambda s, f: "{}", media_dir=tmp_path / "m", auth=True)
    svc = app.state.auth
    svc.create_user("eng", "engineer", "eng-secret-ok"); svc.create_user("op", "operator", "op-secret-ok")
    def cl(u, s):
        c = TestClient(app, headers={"x-extbus": "1"}); c.post("/api/auth/login", json={"username": u, "secret": s}); return c
    eng, op, ad = cl("eng", "eng-secret-ok"), cl("op", "op-secret-ok"), cl("admin", app.state.bootstrap_secret)
    for path in ("/api/procurement/reorder", "/api/procurement/po-draft", "/api/procurement/lookahead"):
        assert eng.get(path).status_code == 200 and op.get(path).status_code == 403 and ad.get(path).status_code == 200, path
    assert eng.post("/api/procurement/order", json={"profile": PN, "qty": 1}).status_code == 403
    assert ad.post("/api/procurement/order", json={"profile": PN, "qty": 1}).status_code == 200
    reg.close()
