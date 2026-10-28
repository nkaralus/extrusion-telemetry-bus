import pytest
from pydantic import ValidationError

from extrusion_bus.physics import (
    CutSpec, End, Face, GeometryMismatch, LeadMode, ceil_to_shop, classify_lead, floor_to_shop,
    fmt_inches, guaranteed_square, miter_delta, plan_cut,
)

SQ = End.square()
M_SPL = End.miter(45, Face.SPLINE)
M_BCK = End.miter(45, Face.BACK)


# ---- miter geometry -------------------------------------------------------- #
def test_miter_delta_45_equals_width():
    assert miter_delta(2.0, 45) == 2.0
    assert miter_delta(4.0, 45) == 4.0


def test_miter_delta_other_angles_round_up_to_sixteenth():
    assert miter_delta(4.0, 30) == 2.3125   # 4*tan30 = 2.3094 -> 37/16
    assert miter_delta(4.0, 0) == 0.0


def test_float_noise_does_not_leak():
    assert floor_to_shop(4 - 1e-13) == 4.0
    assert ceil_to_shop(4 + 1e-13) == 4.0
    assert floor_to_shop(70.1259) == 70.125
    assert ceil_to_shop(58.01) == 58.0625


def test_fmt_inches():
    assert fmt_inches(54.375) == '54 3/8"'
    assert fmt_inches(0.5) == '1/2"'
    assert fmt_inches(12.0) == '12"'
    assert fmt_inches(70.125) == '70 1/8"'


# ---- kerf / guaranteed square ---------------------------------------------- #
def test_guaranteed_square_matches_design_schema_example():
    # long 74.25, one 45deg miter on a 4" profile: 74.25 - 4 - 0.125 = 70.125
    assert guaranteed_square(74.25, M_SPL, SQ, 4.0) == 70.125


def test_guaranteed_square_two_miters_and_none():
    assert guaranteed_square(100, M_SPL, M_BCK, 4.0) == 91.75
    assert guaranteed_square(288, SQ, SQ, 4.0) == 288.0
    assert guaranteed_square(3, M_SPL, M_BCK, 4.0) == 0.0


def test_kerf_loss_on_square_cut():
    plan = plan_cut(stock_long_in=288, working_end=SQ, far_end=SQ, width_in=4.0,
                    length_in=58, trail=SQ, lead=SQ)
    assert plan.mode == LeadMode.NATIVE
    assert plan.required_in == 58.125
    assert plan.usable_in == 288
    assert plan.leftover_long_in == 229.875  # 288 - 58 - 0.125


def test_trailing_miter_loss_and_complementary_face():
    plan = plan_cut(stock_long_in=288, working_end=SQ, far_end=SQ, width_in=4.0,
                    length_in=58, trail=M_SPL, lead=SQ)
    # remnant long point = 288 - (58 - 4 + 0.125)
    assert plan.leftover_long_in == 233.875
    assert plan.leftover_end_a == End.miter(45, Face.BACK)     # anisotropic: faces swap
    assert plan.leftover_guaranteed_in == 229.75               # 233.875 - 4 - 0.125


def test_miter_lead_on_square_stock_costs_one_kerf():
    plan = plan_cut(stock_long_in=288, working_end=SQ, far_end=SQ, width_in=4.0,
                    length_in=58, trail=M_SPL, lead=M_SPL)
    assert plan.mode == LeadMode.MITER_LEAD
    assert plan.offset_in == 0.125
    assert plan.usable_in == 287.875
    assert plan.leftover_long_in == 233.75


def test_true_square_costs_delta_plus_kerf():
    plan = plan_cut(stock_long_in=74.25, working_end=M_SPL, far_end=SQ, width_in=4.0,
                    length_in=54.375, trail=SQ, lead=SQ)
    assert plan.mode == LeadMode.TRUE_SQUARE
    assert plan.offset_in == 4.125
    assert plan.usable_in == 70.125
    assert plan.fits


def test_native_match_skips_initial_cut():
    plan = plan_cut(stock_long_in=74.25, working_end=M_SPL, far_end=SQ, width_in=4.0,
                    length_in=54.375, trail=SQ, lead=M_SPL)
    assert plan.mode == LeadMode.NATIVE
    assert plan.offset_in == 0.0
    assert plan.usable_in == 74.25


def test_exact_fit_boundary_and_one_sixteenth_over():
    ok = plan_cut(stock_long_in=58.125, working_end=SQ, far_end=SQ, width_in=4.0,
                  length_in=58, trail=SQ, lead=SQ)
    assert ok.fits and ok.leftover_long_in == 0
    over = plan_cut(stock_long_in=58.125, working_end=SQ, far_end=SQ, width_in=4.0,
                    length_in=58.0625, trail=SQ, lead=SQ)
    assert not over.fits


def test_far_end_miter_reduces_usable():
    plan = plan_cut(stock_long_in=60, working_end=SQ, far_end=M_BCK, width_in=4.0,
                    length_in=40, trail=SQ, lead=SQ)
    assert plan.usable_in == 56.0


# ---- orientation ----------------------------------------------------------- #
def test_face_opposite():
    assert Face.SPLINE.opposite == Face.BACK
    assert Face.BACK.opposite == Face.SPLINE
    assert Face.UNIFORM.opposite == Face.UNIFORM


def test_classify_lead_matrix():
    assert classify_lead(SQ, SQ) == LeadMode.NATIVE
    assert classify_lead(M_SPL, M_SPL) == LeadMode.NATIVE
    assert classify_lead(M_SPL, M_BCK) is None
    assert classify_lead(M_SPL, End.miter(30, Face.SPLINE)) is None
    assert classify_lead(M_SPL, SQ) == LeadMode.TRUE_SQUARE
    assert classify_lead(SQ, M_SPL) == LeadMode.MITER_LEAD


def test_plan_cut_raises_on_geometry_mismatch():
    with pytest.raises(GeometryMismatch):
        plan_cut(stock_long_in=80, working_end=M_BCK, far_end=SQ, width_in=4.0,
                 length_in=40, trail=SQ, lead=M_SPL)


# ---- models ---------------------------------------------------------------- #
def test_end_validation():
    with pytest.raises(ValidationError):
        End(type="square", miter_deg=45)
    with pytest.raises(ValidationError):
        End(type="miter", miter_deg=45, long_point_face="uniform")
    with pytest.raises(ValidationError):
        End(type="miter", miter_deg=0, long_point_face="spline_face")


def test_cutspec_normalizes_faces_and_rounds_length_up():
    s = CutSpec(profile="X", length_in=58.01, miter_deg=45, face="Spline Out")
    assert s.face == Face.SPLINE and s.length_in == 58.0625
    assert CutSpec(profile="X", length_in=10, miter_deg=45, face="smooth face").face == Face.BACK
    sq = CutSpec(profile="X", length_in=10, face="spline_out")
    assert sq.face == Face.UNIFORM and sq.trail_end() == SQ


def test_cutspec_lead_defaults_and_strictness():
    s = CutSpec(profile="X", length_in=10, miter_deg=45, face="back_face", lead_miter_deg=45)
    assert s.lead_face == Face.BACK
    with pytest.raises(ValidationError):
        CutSpec(profile="X", length_in=10, miter_deg=45, face="uniform")
    with pytest.raises(ValidationError):
        CutSpec(profile="X", length_in=10, bogus=1)
    with pytest.raises(ValidationError):
        CutSpec(profile="X", length_in=0)


def test_flipped_moves_every_long_point():
    s = CutSpec(profile="X", length_in=10, miter_deg=45, face="spline_face", lead_miter_deg=45)
    f = s.flipped()
    assert f.face == Face.BACK and f.lead_face == Face.BACK
    with pytest.raises(ValueError):
        CutSpec(profile="X", length_in=10).flipped()
