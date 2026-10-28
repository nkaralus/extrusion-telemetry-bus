"""Sales field intake: the package a rep builds on the customer's patio.

The rep's tablet posts a ``SalesIntakePackage``; the pre-flight gates (``sales_gates``) answer
in real time with ``MissingItemPrompt`` cards.  Nothing is released to Engineering until no
``critical_blocker`` remains.  ``field_verify`` cards never block; they lower the score.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .gates import GateVerdict
from .physics import parse_inches


class SubstrateType(str, Enum):
    FASCIA = "fascia"
    BRICK = "brick"
    SOFFIT_OVERHANG = "soffit_overhang"
    CONCRETE_BLOCK = "concrete_block"
    SIDING_VINYL = "siding_vinyl"
    SIDING_WOOD = "siding_wood"
    UNKNOWN = "unknown"


class PhotoKind(str, Enum):
    WIDE_CONTEXT = "wide_context"
    TIE_IN_CLOSEUP = "tie_in_closeup"
    SKETCH = "sketch"


class ObstacleKind(str, Enum):
    DOWNSPOUT = "downspout"
    STEPS = "steps"
    WINDOW = "window"
    HVAC_UNIT = "hvac_unit"
    UTILITY_METER = "utility_meter"
    EXTERIOR_DOOR = "exterior_door"
    OTHER = "other"


class Severity(str, Enum):
    CRITICAL = "critical_blocker"
    VERIFY = "field_verify"


class InputType(str, Enum):
    PHOTO = "photo"
    NUMBER = "number"
    CHOICE = "choice"


class SitePhoto(BaseModel):
    """Photo metadata captured by the tablet.  The server never needs the pixels for the gates;
    width/height/sharpness (0..1 focus score) are optional client-side quality signals."""
    kind: PhotoKind
    path: str = Field(min_length=1)
    width_px: Optional[int] = Field(None, gt=0)
    height_px: Optional[int] = Field(None, gt=0)
    sharpness: Optional[float] = Field(None, ge=0, le=1)
    note: str = ""


class MissingItemPrompt(BaseModel):
    """An actionable card for the rep, rendered while still on site."""
    gate: str
    field_key: str
    severity: Severity
    title: str
    instructions_for_rep: str
    requires_photo: bool = False
    input_type: InputType
    choices: List[str] = Field(default_factory=list)


class SalesIntakePackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    customer_name: str = ""
    site_photos: List[SitePhoto] = Field(default_factory=list)
    width_in: Optional[float] = None
    proj_in: Optional[float] = None
    house_h_in: Optional[float] = None     # height at the house-wall attachment
    front_h_in: Optional[float] = None     # height at the outboard (front) line
    substrate: SubstrateType = SubstrateType.UNKNOWN
    door_swing_detected: Optional[bool] = None       # None = not answered yet
    door_clearance_confirmed: Optional[bool] = None
    obstacles_declared: List[ObstacleKind] = Field(default_factory=list)
    acknowledged_obstacles: List[ObstacleKind] = Field(default_factory=list)
    obstacles_checked: Optional[bool] = None         # rep walked the perimeter

    @field_validator("width_in", "proj_in", "house_h_in", "front_h_in", mode="before")
    @classmethod
    def _tape(cls, v):
        return parse_inches(v) if isinstance(v, str) else v   # accepts "144" or "12 1/2"

    def photo(self, kind: PhotoKind) -> Optional[SitePhoto]:
        return next((p for p in self.site_photos if p.kind == kind), None)


class DetectedObstacle(BaseModel):
    kind: ObstacleKind
    confidence: float = Field(1.0, ge=0, le=1)


class PhotoAnalysis(BaseModel):
    """Output of an optional vision step (local multimodal model).  All fields optional."""
    substrate: Optional[SubstrateType] = None
    substrate_confidence: float = Field(0.0, ge=0, le=1)
    obstacles: List[DetectedObstacle] = Field(default_factory=list)
    door_swing_detected: Optional[bool] = None


class PhotoAnalyzer(Protocol):
    def analyze(self, pkg: SalesIntakePackage) -> PhotoAnalysis: ...


class NullAnalyzer:
    """Default: no vision model wired.  Gates then rely on what the rep declares."""
    def analyze(self, pkg: SalesIntakePackage) -> PhotoAnalysis:
        return PhotoAnalysis()


class EngineeringReadinessReport(BaseModel):
    job_id: str
    is_ready_for_engineering: bool
    confidence_score: float          # completeness score 0..1 (not a model probability)
    detected_substrate: SubstrateType
    missing_items: List[MissingItemPrompt] = Field(default_factory=list)
    detected_obstacles: List[DetectedObstacle] = Field(default_factory=list)
    pitch_per_foot_in: Optional[float] = None
    pitch_deg: Optional[float] = None
    verdicts: List[GateVerdict] = Field(default_factory=list)

    @property
    def critical(self) -> List[MissingItemPrompt]:
        return [m for m in self.missing_items if m.severity == Severity.CRITICAL]


# --------------------------------------------------------------------------- #
# Rep answers -> package (gate resolution)
# --------------------------------------------------------------------------- #
_YES = {"yes", "y", "true", "1", "confirmed", "confirmed_clear"}
_NO = {"no", "n", "false", "0"}


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _YES:
        return True
    if s in _NO:
        return False
    raise ValueError(f"expected yes/no, got {v!r}")


def apply_answers(pkg: SalesIntakePackage, answers: Dict[str, Any]) -> SalesIntakePackage:
    """Merge the rep's answers to prompt cards (keyed by ``field_key``) into a new package.

    Keys: width_in | proj_in | house_h_in | front_h_in (tape string or number), substrate,
    door_swing_detected, door_clearance_confirmed, obstacles_checked (yes/no),
    obstacle_ack.<kind>, photo.<wide_context|tie_in_closeup|sketch> (path string or SitePhoto dict).
    """
    data = pkg.model_dump(mode="python")
    for key, val in answers.items():
        if key in ("width_in", "proj_in", "house_h_in", "front_h_in"):
            data[key] = parse_inches(val)
        elif key == "substrate":
            data[key] = SubstrateType(str(val).strip().lower())
        elif key in ("door_swing_detected", "door_clearance_confirmed", "obstacles_checked"):
            data[key] = _bool(val)
        elif key.startswith("obstacle_ack."):
            kind = ObstacleKind(key.split(".", 1)[1])
            if kind not in data["acknowledged_obstacles"]:
                data["acknowledged_obstacles"].append(kind)
        elif key.startswith("photo."):
            kind = PhotoKind(key.split(".", 1)[1])
            photo = {"kind": kind, "path": val} if isinstance(val, str) else {**val, "kind": kind}
            data["site_photos"] = [p for p in data["site_photos"] if p["kind"] != kind] + [photo]
        else:
            raise ValueError(f"unknown answer key {key!r}")
    return SalesIntakePackage.model_validate(data)
