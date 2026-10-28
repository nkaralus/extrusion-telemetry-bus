from extrusion_bus.gates import gi_1, gi_2, gi_3, run_gates
from extrusion_bus.physics import End, Face
from extrusion_bus.registry import SUBNETS, StockItem, StockKind

from conftest import PN, make_proposal


def codes(report):
    return [(v.gate, v.code) for v in report.verdicts]


# ---- GI_1: identity & allocation ------------------------------------------- #
def test_unknown_drop_rejected_at_gi1(reg):
    r = run_gates(reg, make_proposal("10.2.2.99"))
    assert codes(r) == [("GI_1", "UNKNOWN_TARGET")]  # firewall stops at the first failure
    assert not r.ok and r.plan is None


def test_double_allocation_rejected_but_same_job_allowed(reg):
    raw = reg.add_raw(PN, 288)[0]
    reg.lock(raw.addr, "JOB-A")
    assert gi_1(reg, make_proposal(raw.addr, job="JOB-B")).code == "ALREADY_ALLOCATED"
    assert gi_1(reg, make_proposal(raw.addr, job="JOB-A")).ok


def test_profile_mismatch_is_ref_mismatch(reg):
    raw = reg.add_raw(PN, 288)[0]
    assert gi_1(reg, make_proposal(raw.addr, profile="EXT-220-OPEN")).code == "REF_MISMATCH"
    assert gi_1(reg, make_proposal(raw.addr, profile="EXT-NOPE")).code == "REF_MISMATCH"


# ---- GI_2: isolation & face alignment -------------------------------------- #
def test_cross_subnet_station_cannot_reach_raw_yard(reg):
    raw = reg.add_raw(PN, 288)[0]
    reg.add_station("REMNANT_ONLY", reach=[SUBNETS["REMNANT"]])
    v = gi_2(reg, make_proposal(raw.addr, station="REMNANT_ONLY"))
    assert (v.ok, v.code) == (False, "CROSS_SUBNET")


def test_unknown_station_is_cross_subnet(reg):
    raw = reg.add_raw(PN, 288)[0]
    assert gi_2(reg, make_proposal(raw.addr, station="10.9.9.9")).code == "CROSS_SUBNET"


def test_stock_leaked_into_scrap_or_wrong_subnet_is_rejected(reg):
    for addr, kind in (("10.5.1.7", StockKind.RAW),      # sitting in the scrap hopper subnet
                       ("10.4.1.5", StockKind.REMNANT),  # sitting on a job cart
                       ("10.2.1.9", StockKind.REMNANT)): # remnant parked in the raw yard
        reg.state.stock[addr] = StockItem(addr=addr, uid=f"u{addr}", kind=kind, profile=PN,
                                          long_point_in=100, guaranteed_square_in=100)
        reg.state.stations["10.3.1.1"].reach.append("10.0.0.0/8")  # reach is not the issue here
        v = gi_2(reg, make_proposal(addr))
        assert (v.ok, v.code) == (False, "CROSS_SUBNET"), addr


def test_face_mismatch_is_geometry_mismatch(reg):
    rem = reg.add_remnant(PN, 80, End.miter(45, Face.BACK))
    r = run_gates(reg, make_proposal(rem.addr, miter_deg=45, face="spline_face", lead_miter_deg=45,
                                     lead_face="spline_face"))
    assert codes(r) == [("GI_1", "OK"), ("GI_2", "GEOMETRY_MISMATCH")]
    # flipping the piece (FLIP_MITER) repairs the orientation
    flipped = make_proposal(rem.addr, miter_deg=45, face="back_face", lead_miter_deg=45, lead_face="back_face")
    assert run_gates(reg, flipped).ok


# ---- GI_3: bounds & kerf ---------------------------------------------------- #
def test_bounds_overflow_is_exact_to_the_kerf(reg):
    raw = reg.add_raw(PN, 100)[0]
    assert gi_3(reg, make_proposal(raw.addr, length_in=100)).code == "BOUNDS_OVERFLOW"
    assert gi_3(reg, make_proposal(raw.addr, length_in=99.9375)).code == "BOUNDS_OVERFLOW"  # 100.0625 > 100
    assert gi_3(reg, make_proposal(raw.addr, length_in=99.875)).ok                          # 100.0 <= 100


def test_miter_triangle_loss_counts_against_usable(reg):
    rem = reg.add_remnant(PN, 60, End.miter(45, Face.SPLINE))
    # lead must be trued square: usable = 60 - (4 + 0.125) = 55.875 < 56 + 0.125
    v = gi_3(reg, make_proposal(rem.addr, length_in=56))
    assert (v.ok, v.code) == (False, "BOUNDS_OVERFLOW")
    # the native miter match skips the trim, so the same cut fits
    ok = make_proposal(rem.addr, length_in=56, lead_miter_deg=45, lead_face="spline_face", miter_deg=45,
                       face="spline_face")
    assert gi_3(reg, ok).ok


def test_far_end_miter_counts_against_usable(reg):
    rem = reg.add_remnant(PN, 60, End.square(), End.miter(45, Face.BACK))
    assert gi_3(reg, make_proposal(rem.addr, length_in=56)).code == "BOUNDS_OVERFLOW"  # usable 56
    assert gi_3(reg, make_proposal(rem.addr, length_in=55.875)).ok


# ---- whole firewall ---------------------------------------------------------- #
def test_all_three_gates_pass_and_plan_attached(reg):
    raw = reg.add_raw(PN, 288)[0]
    r = run_gates(reg, make_proposal(raw.addr, miter_deg=45, face="spline_face"))
    assert r.ok and codes(r) == [("GI_1", "OK"), ("GI_2", "OK"), ("GI_3", "OK")]
    assert r.plan.leftover_long_in == 233.875


def test_gates_never_mutate_state(reg):
    raw = reg.add_raw(PN, 288)[0]
    before = reg.state.model_dump()
    run_gates(reg, make_proposal(raw.addr))
    run_gates(reg, make_proposal("10.2.2.99"))
    assert reg.state.model_dump() == before
