import math

import pytest
from fastapi.testclient import TestClient

from extrusion_bus.mule_frame import Slot2Plan
from extrusion_bus.nesting import compile_cut_list
from extrusion_bus.parametric import ParametricError, bays_for, solve_envelope
from extrusion_bus.physics import End, Face, ceil_to_shop, miter_delta
from extrusion_bus.registry import Registry
from extrusion_bus.sales_gates import evaluate_intake, ge_1, ge_2, ge_3, pitch_per_foot
from extrusion_bus.sales_intake import (
    DetectedObstacle, ObstacleKind, PhotoAnalysis, SalesIntakePackage, Severity, SubstrateType,
    apply_answers,
)
from extrusion_bus.server import create_app

from conftest import PN

TIE = {"kind": "tie_in_closeup", "path": "tie.jpg", "width_px": 1600, "height_px": 1200, "sharpness": 0.8}
WIDE = {"kind": "wide_context", "path": "wide.jpg"}
SKETCH = {"kind": "sketch", "path": "sketch.jpg"}


def pkg(**kw) -> SalesIntakePackage:
    base = dict(job_id="J1", customer_name="Pat", width_in=156, proj_in=144, house_h_in=120, front_h_in=96,
                substrate="brick", site_photos=[WIDE, TIE, SKETCH], door_swing_detected=False,
                obstacles_checked=True)
    base.update(kw)
    return SalesIntakePackage(**base)


def keys(prompts, sev=None):
    return {p.field_key for p in prompts if sev is None or p.severity == sev}


class StaticAnalyzer:
    def __init__(self, analysis): self.analysis = analysis
    def analyze(self, p): return self.analysis


# ============================ GE_1 geometric envelope ============================ #
def test_ge1_flags_all_four_missing_dimensions():
    v, prompts = ge_1(SalesIntakePackage(job_id="J"))
    assert (v.ok, v.gate, v.code) == (False, "GE_1", "MISSING_DIMENSION")
    assert keys(prompts, Severity.CRITICAL) == {"width_in", "proj_in", "house_h_in", "front_h_in"}
    assert all(p.input_type.value == "number" and "inches" in p.instructions_for_rep.lower() for p in prompts)


def test_ge1_flags_only_the_missing_one():
    v, prompts = ge_1(pkg(width_in=None))
    assert not v.ok and keys(prompts) == {"width_in"}


def test_ge1_catches_feet_typed_as_inches_and_zero():
    v, prompts = ge_1(pkg(width_in=12))
    assert v.code == "DIMENSION_OUT_OF_RANGE" and "feet" in prompts[0].instructions_for_rep
    assert not ge_1(pkg(proj_in=0))[0].ok
    assert not ge_1(pkg(house_h_in=-96))[0].ok


def test_ge1_missing_reported_before_out_of_range_and_clean_pass():
    assert ge_1(pkg(width_in=None, proj_in=12))[0].code == "MISSING_DIMENSION"
    v, prompts = ge_1(pkg())
    assert v.ok and v.code == "OK" and prompts == []


def test_tape_strings_are_accepted():
    p = pkg(width_in="156 1/2", proj_in="144")
    assert p.width_in == 156.5 and p.proj_in == 144.0


# ============================ GE_2 substrate, photo, pitch ============================ #
def test_ge2_rejects_unknown_substrate():
    v, prompts = ge_2(pkg(substrate="unknown"))
    assert (v.ok, v.code) == (False, "UNKNOWN_SUBSTRATE")
    p = next(p for p in prompts if p.field_key == "substrate")
    assert p.severity == Severity.CRITICAL and p.input_type.value == "choice" and "unknown" not in p.choices
    assert "brick" in p.choices and "soffit_overhang" in p.choices


def test_ge2_rejects_missing_tie_in_photo():
    v, prompts = ge_2(pkg(site_photos=[WIDE, SKETCH]))
    p = next(p for p in prompts if p.field_key == "photo.tie_in_closeup")
    assert (v.ok, v.code) == (False, "TIE_IN_PHOTO_MISSING")
    assert p.requires_photo and p.input_type.value == "photo" and p.severity == Severity.CRITICAL


def test_ge2_rejects_blurry_or_tiny_tie_in_but_accepts_missing_metadata():
    blurry = {**TIE, "sharpness": 0.2}
    tiny = {**TIE, "width_px": 640, "height_px": 480}
    bare = {"kind": "tie_in_closeup", "path": "tie.jpg"}
    assert ge_2(pkg(site_photos=[WIDE, blurry, SKETCH]))[0].code == "TIE_IN_PHOTO_QUALITY"
    assert ge_2(pkg(site_photos=[WIDE, tiny, SKETCH]))[0].code == "TIE_IN_PHOTO_QUALITY"
    assert ge_2(pkg(site_photos=[WIDE, bare, SKETCH]))[0].ok


def test_ge2_missing_wide_and_sketch_warn_but_do_not_block():
    v, prompts = ge_2(pkg(site_photos=[TIE]))
    assert v.ok and v.code == "VERIFY"
    assert keys(prompts, Severity.VERIFY) == {"photo.wide_context", "photo.sketch"}


def test_pitch_formula_and_exact_half_inch_boundary():
    assert pitch_per_foot(120, 96, 144) == 2.0
    ok = ge_2(pkg(house_h_in=96, front_h_in=90))[0]            # 6" over 12 ft = 0.50"/ft exactly
    assert ok.ok
    low_v, low = ge_2(pkg(house_h_in=95.88, front_h_in=90))    # 0.49"/ft
    assert (low_v.ok, low_v.code) == (False, "INSUFFICIENT_PITCH")
    p = next(p for p in low if p.field_key == "front_h_in")
    assert p.severity == Severity.CRITICAL and "0.49" in p.title and "6.00" in p.instructions_for_rep


def test_negative_and_flat_pitch_block_with_clear_reason():
    v, prompts = ge_2(pkg(house_h_in=96, front_h_in=100))
    assert v.code == "INSUFFICIENT_PITCH" and "HIGHER" in prompts[0].instructions_for_rep
    assert ge_2(pkg(house_h_in=96, front_h_in=96))[0].code == "INSUFFICIENT_PITCH"


def test_steep_pitch_is_a_verify_not_a_blocker():
    v, prompts = ge_2(pkg(house_h_in=200, front_h_in=96))      # 8.67"/ft
    assert v.ok and "front_h_in" in keys(prompts, Severity.VERIFY)


def test_pitch_not_evaluated_without_valid_dimensions():
    r = evaluate_intake(SalesIntakePackage(job_id="J", substrate="brick"))
    assert r.pitch_per_foot_in is None and "INSUFFICIENT_PITCH" not in {v.code for v in r.verdicts}


def test_analyzer_hint_mismatch_and_low_confidence():
    brick = PhotoAnalysis(substrate=SubstrateType.BRICK, substrate_confidence=0.9)
    v, prompts = ge_2(pkg(substrate="unknown"), brick)                 # rep must still confirm
    assert v.code == "UNKNOWN_SUBSTRATE" and "brick" in prompts[0].instructions_for_rep
    v2, p2 = ge_2(pkg(substrate="siding_vinyl"), brick)
    assert v2.ok and "substrate" in keys(p2, Severity.VERIFY)
    weak = PhotoAnalysis(substrate=SubstrateType.BRICK, substrate_confidence=0.4)
    assert "substrate" not in keys(ge_2(pkg(substrate="siding_vinyl"), weak)[1])
    assert evaluate_intake(pkg(substrate="unknown"), brick).detected_substrate == SubstrateType.BRICK
    assert evaluate_intake(pkg(substrate="unknown"), weak).detected_substrate == SubstrateType.UNKNOWN


# ============================ GE_3 obstacles & clearance ============================ #
def test_ge3_blocks_unverified_door_swing():
    v, prompts = ge_3(pkg(door_swing_detected=None))
    assert (v.ok, v.code) == (False, "DOOR_SWING_UNVERIFIED")
    p = prompts[0]
    assert p.field_key == "door_swing_detected" and p.choices == ["yes", "no"]


def test_ge3_blocks_out_swing_door_until_clearance_confirmed():
    for clearance in (None, False):
        v, prompts = ge_3(pkg(door_swing_detected=True, door_clearance_confirmed=clearance))
        assert (v.ok, v.code) == (False, "OUT_SWING_DOOR_UNCONFIRMED")
        assert '36"' in prompts[0].title and "chair rail" in prompts[0].instructions_for_rep
    assert ge_3(pkg(door_swing_detected=True, door_clearance_confirmed=True))[0].ok
    assert ge_3(pkg(door_swing_detected=False))[0].ok


def test_ge3_flags_downspouts_and_steps_until_acknowledged():
    v, prompts = ge_3(pkg(obstacles_declared=["downspout", "steps"]))
    assert v.ok and v.code == "VERIFY"
    assert keys(prompts, Severity.VERIFY) == {"obstacle_ack.downspout", "obstacle_ack.steps"}
    _, after = ge_3(pkg(obstacles_declared=["downspout", "steps"], acknowledged_obstacles=["downspout", "steps"]))
    assert after == []
    assert ge_3(pkg(obstacles_declared=["window"]))[1] == []            # not a flagged kind


def test_ge3_merges_analyzer_detections_by_confidence():
    seen = PhotoAnalysis(obstacles=[DetectedObstacle(kind=ObstacleKind.DOWNSPOUT, confidence=0.8),
                                    DetectedObstacle(kind=ObstacleKind.STEPS, confidence=0.3)])
    _, prompts = ge_3(pkg(), seen)
    assert keys(prompts) == {"obstacle_ack.downspout"}
    assert [o.kind for o in evaluate_intake(pkg(), seen).detected_obstacles] == [ObstacleKind.DOWNSPOUT]


def test_ge3_perimeter_walk_prompt_and_door_hint():
    assert "obstacles_checked" in keys(ge_3(pkg(obstacles_checked=None))[1])
    assert "obstacles_checked" not in keys(ge_3(pkg(obstacles_checked=True))[1])
    _, prompts = ge_3(pkg(door_swing_detected=None), PhotoAnalysis(door_swing_detected=True))
    assert "photos appear to show a door" in prompts[0].instructions_for_rep


# ============================ report / answers ============================ #
def test_report_blocks_on_any_critical_and_scores_completeness():
    r = evaluate_intake(SalesIntakePackage(job_id="J"))
    assert not r.is_ready_for_engineering and r.detected_substrate == SubstrateType.UNKNOWN
    crit = len(r.critical)
    assert crit >= 7 and r.confidence_score == round(max(0, 1 - 0.2 * crit - 0.05 * (len(r.missing_items) - crit)), 2)
    assert {v.gate for v in r.verdicts} == {"GE_1", "GE_2", "GE_3"}


def test_verify_items_do_not_block_release():
    r = evaluate_intake(pkg(site_photos=[TIE], obstacles_checked=None))
    assert r.is_ready_for_engineering and r.confidence_score == 0.85 and len(r.missing_items) == 3


def test_fully_resolved_package_is_ready_at_full_score():
    r = evaluate_intake(pkg())
    assert r.is_ready_for_engineering and r.confidence_score == 1.0 and r.missing_items == []
    assert r.pitch_per_foot_in == 2.0 and r.pitch_deg == 9.46


def test_apply_answers_resolves_gates():
    p = SalesIntakePackage(job_id="J", customer_name="Pat")
    p = apply_answers(p, {"width_in": "156", "proj_in": 144, "house_h_in": 120,
                          "front_h_in": "96", "substrate": "Brick", "door_swing_detected": "no",
                          "obstacles_checked": "yes", "photo.tie_in_closeup": TIE})
    r = evaluate_intake(p)
    assert r.is_ready_for_engineering and {m.field_key for m in r.missing_items} == {"photo.wide_context", "photo.sketch"}
    p = apply_answers(p, {"photo.tie_in_closeup": "retake.jpg", "obstacle_ack.downspout": "acknowledged"})
    assert len(p.site_photos) == 1 and p.site_photos[0].path == "retake.jpg"
    assert p.acknowledged_obstacles == [ObstacleKind.DOWNSPOUT]
    for bad in ({"nonsense": 1}, {"door_swing_detected": "maybe"}, {"substrate": "stucco"}, {"width_in": "wide"}):
        with pytest.raises(ValueError):
            apply_answers(p, bad)


# ============================ parametric roof angle & bay spacing ============================ #
def test_roof_angle_matches_arctan_of_drop_over_projection():
    m = solve_envelope(156, 144, 120, 96)
    assert m.drop_in == 24 and m.pitch_per_foot_in == 2.0
    assert m.pitch_deg == round(math.degrees(math.atan(24 / 144)), 2) == 9.46
    assert math.isclose(math.tan(math.radians(m.pitch_deg)), m.pitch_per_foot_in / 12, rel_tol=1e-3)
    rafters = [c for c in m.cuts if c.miter_deg > 0]
    assert len(rafters) == 1 and rafters[0].miter_deg == 9.46 and rafters[0].lead_miter_deg == 9.46
    assert (rafters[0].lead_face, rafters[0].face) == (Face.SPLINE, Face.BACK)   # parallelogram: opposite faces


def test_rafter_length_is_slope_plus_miter_triangle():
    m = solve_envelope(156, 144, 120, 96)
    delta = math.ceil(4 * math.tan(math.radians(9.46)) * 16 - 1e-9) / 16
    assert m.rafter_slope_in == pytest.approx(math.hypot(144, 24)) and m.rafter_delta_in == delta == 0.6875
    assert m.rafter_length_in == ceil_to_shop(math.hypot(144, 24) + delta) == 146.6875
    assert solve_envelope(144, 72, 144, 72).rafter_length_in == 105.875    # 45 deg: slope 101.82 + dL 4.0
    assert solve_envelope(144, 72, 144, 72).pitch_deg == 45.0


def test_flat_roof_rafters_are_square_and_equal_projection():
    m = solve_envelope(144, 120, 96, 96)
    r = next(c for c in m.cuts if c.profile == "EXT-240-OPEN" and c.length_in == 120)
    assert r.miter_deg == 0 and r.lead_miter_deg == 0 and m.rafter_delta_in == 0


def test_bay_spacing_never_exceeds_48_and_uses_fewest_bays():
    assert bays_for(144) == (3, 48.0)            # exactly 48 O.C. is allowed
    assert bays_for(145) == (4, 36.25)
    assert bays_for(40) == (1, 40.0)
    for w in range(40, 721, 7):
        n, sp = bays_for(w)
        assert sp <= 48 + 1e-9 and (n == 1 or w / (n - 1) > 48)


def test_post_counts_heights_and_square_cuts():
    m = solve_envelope(156, 144, 120, 96)
    posts = {(c.length_in): c.qty for c in m.cuts if c.profile == "EXT-220-BOX"}
    assert (m.front_bays, m.front_spacing_in, m.side_bays, m.side_spacing_in) == (4, 39.0, 3, 48.0)
    assert posts == {96.0: 5, 120.0: 2, 112.0: 2, 104.0: 2}   # roof-plane height at y=0,48,96 on both sides
    assert all(c.miter_deg == 0 for c in m.cuts if c.profile == "EXT-220-BOX")
    assert sum(posts.values()) == (4 + 1) + 2 * 3


def test_rails_span_the_clear_gap_and_are_spline_out():
    m = solve_envelope(156, 144, 120, 96)
    rails = {c.length_in: c.qty for c in m.cuts if c.profile == "EXT-240-OPEN" and c.miter_deg == 0}
    assert rails == {37.0: 8, 46.0: 6}            # front chair+top (39-2) x4 each; side chair (48-2) x3 per side
    chair = [x for x in m.members if x.kind == "chair_rail"]
    assert {x.a[2] for x in chair} == {36.0} and all(x.face_out == "spline_face" for x in chair)


def test_wireframe_is_consistent_with_the_cut_list():
    m = solve_envelope(156, 144, 120, 96)
    assert m.pieces == len(m.members) == 30 and sum(c.qty for c in m.cuts) == 30
    raf = next(x for x in m.members if x.kind == "rafter")
    assert raf.a[2] == 120 and raf.b[2] == 96 and raf.b[1] == 144
    assert math.dist(raf.a, raf.b) == pytest.approx(m.rafter_slope_in)
    assert all(x.a[2] == 0 for x in m.members if x.kind == "post")
    Slot2Plan(cuts=m.cuts).expand()               # valid for the nesting/quote endpoints


def test_solver_rejects_impossible_geometry():
    for bad in ((0, 144, 120, 96), (156, 144, 96, 100), (156, 144, 120, 40)):
        with pytest.raises(ParametricError):
            solve_envelope(*bad)
    with pytest.raises(ParametricError):
        solve_envelope(156, 144, 120, 96, max_oc_in=2.0)
    assert len(solve_envelope(720, 360, 140, 100).cuts) <= 50


def test_rafters_chain_natively_when_nested(reg, bus):
    m = solve_envelope(156, 72, 108, 96)
    reg.add_raw("EXT-240-OPEN", 288, count=4)
    specs = Slot2Plan(cuts=[c for c in m.cuts if c.miter_deg > 0]).expand()
    plan = compile_cut_list(bus, "J", specs)
    assert not plan.unmet and plan.steps[0].lead_mode == "miter_lead"
    assert plan.steps[1].lead_mode == "native" and plan.steps[1].skipped_lead_cut and plan.native_skips >= 1


# ============================ backfill ============================ #
def test_older_registry_gets_the_box_tube_profile(tmp_path):
    reg = Registry.load(tmp_path / "state_registry.json")
    del reg.state.profiles["EXT-220-BOX"]
    reg.save()
    assert "EXT-220-BOX" in Registry.load(tmp_path / "state_registry.json").state.profiles


# ============================ integration: intake -> gates -> BOM -> quote ============================ #
@pytest.fixture
def shop(reg):
    reg.add_raw(PN, 288, count=8)
    reg.add_raw("EXT-220-BOX", 288, count=6)
    reg.add_remnant(PN, 120, End.square()); reg.add_remnant(PN, 90, End.square())
    reg.add_remnant("EXT-220-BOX", 100, End.square())
    return reg


def test_intake_to_quote_end_to_end(shop):
    c = TestClient(create_app(shop))
    # 1. rep submits what they have on the patio
    r = c.post("/api/sales/intake", json={"job_id": "J7", "customer_name": "Pat", "width_in": 156,
                                          "site_photos": [WIDE]}).json()
    assert not r["is_ready_for_engineering"]
    need = {m["field_key"] for m in r["missing_items"] if m["severity"] == "critical_blocker"}
    assert need == {"proj_in", "house_h_in", "front_h_in", "substrate", "photo.tie_in_closeup",
                    "door_swing_detected"}
    assert c.post("/api/sales/generate-bom", json={"job_id": "J7"}).status_code == 409   # nothing released

    # 2. gate resolution: the rep answers the cards
    r = c.post("/api/sales/intake/answer", json={"job_id": "J7", "answers": {
        "proj_in": "144", "house_h_in": 120, "front_h_in": 96, "substrate": "brick",
        "door_swing_detected": "no", "photo.tie_in_closeup": TIE}}).json()
    assert r["is_ready_for_engineering"] and r["pitch_per_foot_in"] == 2.0
    assert {m["severity"] for m in r["missing_items"]} <= {"field_verify"}

    # 3. parametric BOM + dry-run quote with drop credits
    out = c.post("/api/sales/generate-bom", json={"job_id": "J7", "margin_pct": 20}).json()
    model, q = out["parametric"], out["quote"]
    assert model["pitch_deg"] == 9.46 and sum(x["qty"] for x in model["cuts"]) == q["pieces"] == 30
    assert q["unmet"] == 0 and q["material_cost"] > 0
    assert q["reclaimed_ft"] > 0 and q["savings_from_drops"] > 0 and q["price"] < q["price_virgin_only"]
    assert q["lines"] and {l["profile"] for l in q["lines"]} == {"EXT-220-BOX", "EXT-240-OPEN"}

    # 4. the same cut lines feed the nesting compiler unchanged
    plan = c.post("/api/nest/compile", json={"job_id": "J7", "cuts": model["cuts"]}).json()
    assert plan["pieces"] == 30 and plan["unmet"] == [] and plan["drops_used"] > 0
    assert plan["native_skips"] >= 0 and plan["virgin_pulls"] >= 1

    # 5. quoting never touched inventory; the intake is persisted
    assert len(shop.state.stock) == 8 + 6 + 3
    assert shop.state.intakes["J7"]["report"]["is_ready_for_engineering"] is True


def test_generate_bom_with_explicit_package_and_blockers(shop):
    c = TestClient(create_app(shop))
    body = {"job_id": "J8", "package": pkg(job_id="J8", house_h_in=95.88, front_h_in=90).model_dump(mode="json")}
    r = c.post("/api/sales/generate-bom", json=body)
    assert r.status_code == 409 and any(v["code"] == "INSUFFICIENT_PITCH" for v in r.json()["detail"]["verdicts"])
    ok = c.post("/api/sales/generate-bom", json={"job_id": "J8", "package": pkg(job_id="J8").model_dump(mode="json")})
    assert ok.status_code == 200 and ok.json()["report"]["confidence_score"] == 1.0


def test_endpoint_errors_and_unready_out_swing_door(shop):
    c = TestClient(create_app(shop))
    assert c.post("/api/sales/intake/answer", json={"job_id": "nope", "answers": {}}).status_code == 404
    c.post("/api/sales/intake", json=pkg(job_id="J9", door_swing_detected=True).model_dump(mode="json"))
    assert c.post("/api/sales/intake/answer", json={"job_id": "J9", "answers": {"bogus": 1}}).status_code == 422
    r = c.post("/api/sales/generate-bom", json={"job_id": "J9"})
    assert r.status_code == 409 and r.json()["detail"]["verdicts"][2]["code"] == "OUT_SWING_DOOR_UNCONFIRMED"
    c.post("/api/sales/intake/answer", json={"job_id": "J9", "answers": {"door_clearance_confirmed": "yes"}})
    assert c.post("/api/sales/generate-bom", json={"job_id": "J9"}).status_code == 200


def test_vision_analyzer_hook_feeds_the_report(shop):
    seen = PhotoAnalysis(substrate=SubstrateType.BRICK, substrate_confidence=0.9,
                         obstacles=[DetectedObstacle(kind=ObstacleKind.DOWNSPOUT, confidence=0.9)])
    c = TestClient(create_app(shop, analyzer=StaticAnalyzer(seen)))
    r = c.post("/api/sales/intake", json=pkg(job_id="J10", substrate="unknown").model_dump(mode="json")).json()
    assert r["detected_substrate"] == "brick" and not r["is_ready_for_engineering"]
    assert [o["kind"] for o in r["detected_obstacles"]] == ["downspout"]
    assert any(m["field_key"] == "obstacle_ack.downspout" for m in r["missing_items"])
