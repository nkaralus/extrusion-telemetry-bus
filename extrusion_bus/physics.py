"""Anisotropic extrusion physics.

Everything the saw table needs to know about a stick is geometry, not a scalar
length.  All lengths are inches.  Conventions used throughout:

* ``miter_deg`` is the deflection from square: 0 = square cut, 45 = standard
  miter.  (A "90 degree" square end in shop speak is ``miter_deg = 0`` here.)
* ``long_point_in`` is the extreme-to-extreme length of a stick.  The *short
  point* is ``long_point - delta`` for each mitered end.
* A miter's long point sits on one face of the profile (``Face.SPLINE`` or
  ``Face.BACK``).  A single planar cut makes complementary ends: the piece
  keeps its long point on face F, the stick that remains has its long point on
  ``F.opposite``.
* Usable lengths round DOWN and miter losses round UP to 1/16", so every
  number the bus hands the operator is conservative.
"""
from __future__ import annotations

import math
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KERF_IN = 0.125
SHOP_DENOM = 16
REMNANT_MIN_IN = 36.0
MAX_MITER_DEG = 75.0
MAX_LENGTH_IN = 600.0
_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Shop rounding
# --------------------------------------------------------------------------- #
def floor_to_shop(x: float, denom: int = SHOP_DENOM) -> float:
    """Round DOWN to the nearest 1/denom inch (float-noise safe)."""
    return math.floor(x * denom + _EPS) / denom


def ceil_to_shop(x: float, denom: int = SHOP_DENOM) -> float:
    """Round UP to the nearest 1/denom inch (float-noise safe)."""
    return math.ceil(x * denom - _EPS) / denom


def fmt_inches(x: float, denom: int = SHOP_DENOM) -> str:
    """54.375 -> '54 3/8"'."""
    n = round(x * denom)
    whole, num = divmod(n, denom)
    if num == 0:
        return f'{whole}"'
    g = math.gcd(num, denom)
    frac = f"{num // g}/{denom // g}"
    return f'{whole} {frac}"' if whole else f'{frac}"'


_INCH_RE = re.compile(
    r'^\s*(?:(?P<w>\d+(?:\.\d+)?)(?:[\s-]+(?P<n>\d+)/(?P<d>\d+))?|(?P<n2>\d+)/(?P<d2>\d+))'
    r'\s*(?:"|in|inch|inches)?\s*$', re.I)


def parse_inches(v) -> float:
    """Tape-measure entry: 74.25 | '74 1/4' | '74-1/4"' | '3/8' -> inches (float)."""
    if isinstance(v, (int, float)):
        return float(v)
    m = _INCH_RE.match(str(v))
    if not m:
        raise ValueError(f"cannot read length {v!r}")
    if m["n2"]:
        return int(m["n2"]) / int(m["d2"])
    x = float(m["w"])
    if m["n"]:
        x += int(m["n"]) / int(m["d"])
    return x


_END_RE = re.compile(r"^miter[\s_-]*(\d+(?:\.\d+)?)[\s_-]*(spline|back)$", re.I)


def parse_end_condition(s: str) -> "End":
    """'square' | 'miter_45_spline' | 'miter 30 back' -> End."""
    key = s.strip().lower()
    if key in ("square", "sq", "flat"):
        return End.square()
    m = _END_RE.match(key)
    if not m:
        raise ValueError(f"unknown end condition {s!r} (use square | miter_45_spline | miter_45_back)")
    return End.miter(float(m.group(1)), Face.SPLINE if m.group(2).lower() == "spline" else Face.BACK)


# --------------------------------------------------------------------------- #
# Faces and ends
# --------------------------------------------------------------------------- #
class Face(str, Enum):
    SPLINE = "spline_face"
    BACK = "back_face"
    UNIFORM = "uniform"  # square ends have no long point

    @property
    def opposite(self) -> "Face":
        return {Face.SPLINE: Face.BACK, Face.BACK: Face.SPLINE, Face.UNIFORM: Face.UNIFORM}[self]


class EndType(str, Enum):
    SQUARE = "square"
    MITER = "miter"


class End(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: EndType = EndType.SQUARE
    miter_deg: float = 0.0
    long_point_face: Face = Face.UNIFORM

    @model_validator(mode="after")
    def _check(self) -> "End":
        if self.type == EndType.SQUARE:
            if self.miter_deg != 0 or self.long_point_face != Face.UNIFORM:
                raise ValueError("square end must have miter_deg=0 and face=uniform")
        else:
            if not (0 < self.miter_deg <= MAX_MITER_DEG):
                raise ValueError(f"miter_deg must be in (0, {MAX_MITER_DEG}]")
            if self.long_point_face == Face.UNIFORM:
                raise ValueError("mitered end needs a long-point face (spline/back)")
        return self

    @classmethod
    def square(cls) -> "End":
        return cls()

    @classmethod
    def miter(cls, deg: float, face: Face) -> "End":
        return cls(type=EndType.MITER, miter_deg=deg, long_point_face=face)

    @property
    def is_miter(self) -> bool:
        return self.type == EndType.MITER

    def same_geometry(self, other: "End") -> bool:
        return (
            self.type == other.type
            and abs(self.miter_deg - other.miter_deg) < 1e-6
            and self.long_point_face == other.long_point_face
        )

    def describe(self) -> str:
        if not self.is_miter:
            return "SQ"
        tag = "SPL" if self.long_point_face == Face.SPLINE else "BCK"
        return f"{self.miter_deg:g}deg {tag}-LONG"


# --------------------------------------------------------------------------- #
# Miter / kerf math
# --------------------------------------------------------------------------- #
def miter_delta(width_in: float, miter_deg: float) -> float:
    """dL = W * tan(theta), rounded UP to 1/16" (conservative loss)."""
    if miter_deg <= 0:
        return 0.0
    return ceil_to_shop(width_in * math.tan(math.radians(miter_deg)))


def end_delta(end: End, width_in: float) -> float:
    return miter_delta(width_in, end.miter_deg) if end.is_miter else 0.0


def guaranteed_square(
    long_point_in: float, end_a: End, end_b: End, width_in: float, kerf: float = KERF_IN
) -> float:
    """Worst-case length if every mitered end must be trued square.

    L_sq = L_long - sum(dL_i + kerf) over mitered ends, rounded down.
    """
    loss = sum(end_delta(e, width_in) + kerf for e in (end_a, end_b) if e.is_miter)
    return max(0.0, floor_to_shop(long_point_in - loss))


# --------------------------------------------------------------------------- #
# Cut specification (what the order asks for)
# --------------------------------------------------------------------------- #
_FACE_ALIASES = {
    "spline": "spline_face", "spline_out": "spline_face", "spline_face": "spline_face",
    "spline_face_out": "spline_face", "spline_facing_out": "spline_face",
    "back": "back_face", "back_out": "back_face", "back_face": "back_face",
    "back_face_out": "back_face", "smooth": "back_face", "smooth_face": "back_face",
    "screw_boss": "back_face", "uniform": "uniform",
}


class CutSpec(BaseModel):
    """One piece to cut.

    ``length_in`` is the overall (long-point) length.  ``miter_deg``/``face``
    describe the trailing end the saw creates.  ``lead_miter_deg``/``lead_face``
    describe the end the piece must already have where it meets the stock.
    """

    model_config = ConfigDict(extra="forbid")

    profile: str = Field(min_length=1)
    length_in: float = Field(gt=0, le=MAX_LENGTH_IN)
    face: Face = Face.SPLINE
    miter_deg: float = Field(0.0, ge=0, le=MAX_MITER_DEG)
    lead_miter_deg: float = Field(0.0, ge=0, le=MAX_MITER_DEG)
    lead_face: Optional[Face] = None

    @field_validator("face", "lead_face", mode="before")
    @classmethod
    def _norm_face(cls, v):
        if isinstance(v, str):
            key = v.strip().lower().replace(" ", "_").replace("-", "_")
            return _FACE_ALIASES.get(key, key)
        return v

    @field_validator("length_in")
    @classmethod
    def _round_length_up(cls, v: float) -> float:
        return ceil_to_shop(v)

    @model_validator(mode="after")
    def _normalize(self) -> "CutSpec":
        if self.miter_deg == 0:
            self.face = Face.UNIFORM
        elif self.face == Face.UNIFORM:
            raise ValueError("mitered cut needs face spline_face or back_face")
        if self.lead_miter_deg == 0:
            self.lead_face = Face.UNIFORM
        else:
            if self.lead_face in (None, Face.UNIFORM):
                self.lead_face = self.face if self.face != Face.UNIFORM else Face.SPLINE
        return self

    def trail_end(self) -> End:
        return End.miter(self.miter_deg, self.face) if self.miter_deg > 0 else End.square()

    def lead_end(self) -> End:
        if self.lead_miter_deg > 0:
            return End.miter(self.lead_miter_deg, self.lead_face or Face.SPLINE)
        return End.square()

    def flipped(self) -> "CutSpec":
        """FLIP_MITER: turn the piece over - every long point moves to the other face."""
        if self.miter_deg == 0 and self.lead_miter_deg == 0:
            raise ValueError("cannot flip a square cut")
        upd = {"face": self.face.opposite}
        if self.lead_miter_deg > 0:
            upd["lead_face"] = (self.lead_face or Face.SPLINE).opposite
        return self.model_copy(update=upd)


# --------------------------------------------------------------------------- #
# Cut planning
# --------------------------------------------------------------------------- #
class LeadMode(str, Enum):
    NATIVE = "native"            # stock end already matches: initial cut skipped
    TRUE_SQUARE = "true_square"  # mitered stock end trimmed square: lose dL + kerf
    MITER_LEAD = "miter_lead"    # square stock end gets a lead miter: lose kerf


class GeometryMismatch(ValueError):
    pass


def classify_lead(working_end: End, required_lead: End) -> Optional[LeadMode]:
    """How the stock's working end meets the piece's required lead end (None = impossible)."""
    if not working_end.is_miter and not required_lead.is_miter:
        return LeadMode.NATIVE
    if working_end.is_miter and required_lead.is_miter:
        return LeadMode.NATIVE if working_end.same_geometry(required_lead) else None
    if working_end.is_miter:
        return LeadMode.TRUE_SQUARE
    return LeadMode.MITER_LEAD


def lead_offset(mode: LeadMode, working_end: End, width_in: float, kerf: float = KERF_IN) -> float:
    if mode == LeadMode.NATIVE:
        return 0.0
    if mode == LeadMode.MITER_LEAD:
        return kerf
    return end_delta(working_end, width_in) + kerf


class CutPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: LeadMode
    offset_in: float            # stock consumed before the piece's lead long point
    required_in: float          # piece length + kerf
    usable_in: float            # stock available after lead prep and far-end miter loss
    fits: bool
    trail_delta_in: float
    leftover_long_in: float
    leftover_end_a: End
    leftover_end_b: End
    leftover_guaranteed_in: float


def plan_cut(
    *,
    stock_long_in: float,
    working_end: End,
    far_end: End,
    width_in: float,
    length_in: float,
    trail: End,
    lead: End,
    kerf: float = KERF_IN,
) -> CutPlan:
    """Deterministic kerf + miter accounting for one cut from one stick.

    Gate-3 inequality:  (length + kerf) <= usable
        usable = stock_long - far_end_delta - lead_offset
    Remainder (long point on the working end):
        leftover_long = stock_long - (lead_offset + length - trail_delta + kerf)
    """
    mode = classify_lead(working_end, lead)
    if mode is None:
        raise GeometryMismatch(
            f"stock end {working_end.describe()} cannot supply lead end {lead.describe()}"
        )
    offset = lead_offset(mode, working_end, width_in, kerf)
    usable = max(0.0, floor_to_shop(stock_long_in - end_delta(far_end, width_in) - offset))
    required = length_in + kerf
    trail_delta = end_delta(trail, width_in)
    leftover_long = max(0.0, floor_to_shop(stock_long_in - (offset + length_in - trail_delta + kerf)))
    left_a = End.miter(trail.miter_deg, trail.long_point_face.opposite) if trail.is_miter else End.square()
    return CutPlan(
        mode=mode,
        offset_in=offset,
        required_in=required,
        usable_in=usable,
        fits=required <= usable + _EPS,
        trail_delta_in=trail_delta,
        leftover_long_in=leftover_long,
        leftover_end_a=left_a,
        leftover_end_b=far_end,
        leftover_guaranteed_in=guaranteed_square(leftover_long, left_a, far_end, width_in, kerf),
    )
