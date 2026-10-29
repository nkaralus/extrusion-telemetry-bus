"""Parametric wireframe & cut solver: verified envelope -> deterministic structural cut list.

Coordinates (inches): x along the house wall (0..width), y outward from the wall (0..proj),
z up from the slab.  The roof plane drops from ``house_h`` at y=0 to ``front_h`` at y=proj.

    drop         = house_h - front_h
    pitch/ft     = drop / (proj / 12)
    theta        = arctan(drop / proj)                      (rafter plumb-cut miter)

Members
  posts        EXT-220-BOX, square.  Front line at y=proj (height front_h) plus both side lines
               (height follows the roof plane).  Bays <= max_oc_in on centers.
  rafters      EXT-240-OPEN, one per front post line, plumb-cut at both ends.  Both ends are
               theta miters with long points on OPPOSITE faces (a parallelogram), so the leftover
               of one cut is a native lead for the next rafter (no extra lead cut when nested).
               length_in is extreme-to-extreme:  hypot(proj, drop) + dL,  dL = W_bar * tan(theta).
  chair rails  EXT-240-OPEN, square, 36" AFF, one per bay on the front and both side walls.
  top rails    EXT-240-OPEN, square, one per front bay (side walls are topped by the edge rafters).
               Rails span the clear gap between posts: bay spacing - post width.

Assumptions (documented, not hidden): post length = local roof-plane height; rafters are cut on a
plumb line through the profile width; cut lengths round UP to 1/16" via CutSpec.
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from pydantic import BaseModel, Field

from .mule_frame import CutLine
from .physics import Face, ceil_to_shop, miter_delta

POST_PROFILE = "EXT-220-BOX"
RAFTER_PROFILE = "EXT-240-OPEN"
RAIL_PROFILE = "EXT-240-OPEN"
MAX_OC_IN = 48.0
CHAIR_RAIL_AFF_IN = 36.0
MAX_CUT_LINES = 50          # Slot2Plan limit


class ParametricError(ValueError):
    pass


Vec = Tuple[float, float, float]


class WireMember(BaseModel):
    id: str
    kind: str                    # post | rafter | chair_rail | top_rail
    profile: str
    a: Vec
    b: Vec
    length_in: float             # the cut length ordered for this member
    miter_deg: float = 0.0
    face_out: str = "n/a"        # install orientation (square cuts carry no face in the cut list)


class ParametricModel(BaseModel):
    width_in: float
    proj_in: float
    house_h_in: float
    front_h_in: float
    drop_in: float
    pitch_per_foot_in: float
    pitch_deg: float
    front_bays: int
    front_spacing_in: float
    side_bays: int
    side_spacing_in: float
    rafter_slope_in: float       # centerline length along the roof plane
    rafter_delta_in: float       # miter triangle dL
    rafter_length_in: float      # ordered extreme-to-extreme length
    cuts: List[CutLine] = Field(default_factory=list)
    members: List[WireMember] = Field(default_factory=list)

    @property
    def pieces(self) -> int:
        return sum(c.qty for c in self.cuts)


def bays_for(span_in: float, max_oc_in: float = MAX_OC_IN) -> Tuple[int, float]:
    """Fewest equal bays so that spacing <= max_oc.  Returns (bays, spacing)."""
    n = max(1, math.ceil(span_in / max_oc_in - 1e-9))
    return n, span_in / n


def solve_envelope(
    width_in: float, proj_in: float, house_h_in: float, front_h_in: float, *,
    max_oc_in: float = MAX_OC_IN, chair_rail_aff_in: float = CHAIR_RAIL_AFF_IN,
    post_w_in: float = 2.0, rafter_width_in: float = 4.0,
) -> ParametricModel:
    for name, v in (("width", width_in), ("projection", proj_in), ("house height", house_h_in),
                    ("front height", front_h_in), ("max_oc", max_oc_in), ("post width", post_w_in)):
        if not v or v <= 0:
            raise ParametricError(f"{name} must be > 0")
    drop = house_h_in - front_h_in
    if drop < 0:
        raise ParametricError("front height exceeds house height: roof would drain toward the house")
    if front_h_in < chair_rail_aff_in + 6:
        raise ParametricError(f"front height {front_h_in:g}\" leaves no room above the {chair_rail_aff_in:g}\" chair rail")

    n_f, sp_f = bays_for(width_in, max_oc_in)
    n_s, sp_s = bays_for(proj_in, max_oc_in)
    if sp_f <= post_w_in or sp_s <= post_w_in:
        raise ParametricError("bay spacing is not wider than the post")

    theta = round(math.degrees(math.atan2(drop, proj_in)), 2)
    slope_len = math.hypot(proj_in, drop)
    delta = miter_delta(rafter_width_in, theta)
    rafter_len = ceil_to_shop(slope_len + delta)
    roof_h = lambda y: house_h_in - drop * (y / proj_in)       # roof-plane height at depth y

    members: List[WireMember] = []
    groups: Dict[tuple, CutLine] = {}

    def cut(m: WireMember, *, lead_deg: float = 0.0, lead_face: Face = Face.SPLINE, face: Face = Face.SPLINE):
        m.length_in = ceil_to_shop(m.length_in)
        members.append(m)
        key = (m.profile, m.length_in, m.miter_deg, face.value if m.miter_deg else "-", lead_deg)
        if key in groups:
            groups[key].qty += 1
        else:
            groups[key] = CutLine(profile=m.profile, length_in=m.length_in, qty=1, miter_deg=m.miter_deg,
                                  face=face, lead_miter_deg=lead_deg, lead_face=lead_face)

    # posts: front line, then both side lines (j=0 sits at the house wall)
    for i in range(n_f + 1):
        x = i * sp_f
        cut(WireMember(id=f"POST-F{i}", kind="post", profile=POST_PROFILE, a=(x, proj_in, 0.0),
                       b=(x, proj_in, front_h_in), length_in=front_h_in))
    for side, x in (("L", 0.0), ("R", width_in)):
        for j in range(n_s):
            y = j * sp_s
            h = roof_h(y)
            cut(WireMember(id=f"POST-{side}{j}", kind="post", profile=POST_PROFILE, a=(x, y, 0.0),
                           b=(x, y, h), length_in=h))

    # rafters: plumb cut both ends, long points on opposite faces
    for i in range(n_f + 1):
        x = i * sp_f
        cut(WireMember(id=f"RAFTER-{i}", kind="rafter", profile=RAFTER_PROFILE, a=(x, 0.0, house_h_in),
                       b=(x, proj_in, front_h_in), length_in=rafter_len, miter_deg=theta, face_out="spline_face"),
            lead_deg=theta, lead_face=Face.SPLINE, face=Face.BACK)

    # chair rails (36" AFF) on three walls; clear span between posts
    clear_f, clear_s = sp_f - post_w_in, sp_s - post_w_in
    for i in range(n_f):
        x0 = i * sp_f + post_w_in / 2
        cut(WireMember(id=f"CHAIR-F{i}", kind="chair_rail", profile=RAIL_PROFILE, a=(x0, proj_in, chair_rail_aff_in),
                       b=(x0 + clear_f, proj_in, chair_rail_aff_in), length_in=clear_f, face_out="spline_face"))
    for side, x in (("L", 0.0), ("R", width_in)):
        for j in range(n_s):
            y0 = j * sp_s + post_w_in / 2
            cut(WireMember(id=f"CHAIR-{side}{j}", kind="chair_rail", profile=RAIL_PROFILE, a=(x, y0, chair_rail_aff_in),
                           b=(x, y0 + clear_s, chair_rail_aff_in), length_in=clear_s, face_out="spline_face"))
    # front top rails (level, under the roof's front edge)
    for i in range(n_f):
        x0 = i * sp_f + post_w_in / 2
        cut(WireMember(id=f"TOP-F{i}", kind="top_rail", profile=RAIL_PROFILE, a=(x0, proj_in, front_h_in),
                       b=(x0 + clear_f, proj_in, front_h_in), length_in=clear_f, face_out="spline_face"))

    if len(groups) > MAX_CUT_LINES:
        raise ParametricError(f"{len(groups)} distinct cut lines exceeds the {MAX_CUT_LINES}-line plan limit")
    return ParametricModel(
        width_in=width_in, proj_in=proj_in, house_h_in=house_h_in, front_h_in=front_h_in, drop_in=drop,
        pitch_per_foot_in=drop / (proj_in / 12.0), pitch_deg=theta, front_bays=n_f, front_spacing_in=sp_f,
        side_bays=n_s, side_spacing_in=sp_s, rafter_slope_in=slope_len, rafter_delta_in=delta,
        rafter_length_in=rafter_len, cuts=list(groups.values()), members=members)
