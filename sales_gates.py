"""Pre-flight engineering readiness gates (the sales-side twin of GI_1..GI_3).

    GE_1  Geometric Envelope        width, projection, house-wall and front heights
    GE_2  Substrate & Pitch         known substrate, usable tie-in photo, runoff pitch >= 0.5"/ft
    GE_3  Obstacles & Clearance     door swing vs 36" chair rail, downspouts, steps

Unlike GI_*, these do NOT short-circuit: the rep is still on the patio, so every gate runs and
every missing item comes back at once.  A gate's verdict is ok=False only if it raised a
``critical_blocker``; ``field_verify`` cards ride along as warnings (code ``VERIFY``).

Photo pixels are never read here.  Substrate / obstacle / door detections come from an
optional ``PhotoAnalyzer`` (vision model); without one, the gates rely on the rep's answers.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .gates import GateVerdict
from .sales_intake import (
    DetectedObstacle, EngineeringReadinessReport, InputType, MissingItemPrompt, ObstacleKind,
    PhotoAnalysis, PhotoKind, SalesIntakePackage, Severity, SubstrateType,
)

MIN_PITCH_IN_PER_FT = 0.5       # water runoff; strictly below this blocks
STEEP_PITCH_IN_PER_FT = 6.0     # above this, confirm (unusual for a screen room)
CHAIR_RAIL_AFF_IN = 36.0

# plausibility bounds, inches.  Anything outside is almost always a unit slip (feet typed as inches).
BOUNDS: Dict[str, Tuple[float, float, str, str]] = {
    "width_in": (48.0, 720.0, "Overall width", "Tape the full width along the house wall, outside edge to outside edge."),
    "proj_in": (36.0, 360.0, "Projection (house wall to front posts)", "Tape from the house wall straight out to where the front posts will stand."),
    "house_h_in": (60.0, 240.0, "Height at the house wall", "Measure slab/floor up to the eave or fascia where the roof will tie in."),
    "front_h_in": (48.0, 216.0, "Height at the front (outboard) line", "Measure slab/floor up to the underside of the roof at the front posts."),
}

MIN_PHOTO_SHORT_SIDE_PX = 800
MIN_PHOTO_SHARPNESS = 0.35
ANALYZER_MIN_CONFIDENCE = 0.6
FLAGGED_OBSTACLES = (ObstacleKind.DOWNSPOUT, ObstacleKind.STEPS)

Outcome = Tuple[GateVerdict, List[MissingItemPrompt]]


def _critical(gate, key, title, how, *, photo=False, kind=InputType.NUMBER, choices=None) -> MissingItemPrompt:
    return MissingItemPrompt(gate=gate, field_key=key, severity=Severity.CRITICAL, title=title,
                             instructions_for_rep=how, requires_photo=photo, input_type=kind,
                             choices=choices or [])


def _verify(gate, key, title, how, *, photo=False, kind=InputType.CHOICE, choices=None) -> MissingItemPrompt:
    return MissingItemPrompt(gate=gate, field_key=key, severity=Severity.VERIFY, title=title,
                             instructions_for_rep=how, requires_photo=photo, input_type=kind,
                             choices=choices or [])


def _verdict(gate: str, prompts: List[MissingItemPrompt], codes: List[str], ok_msg: str) -> GateVerdict:
    crit = [p for p in prompts if p.severity == Severity.CRITICAL]
    if crit:
        return GateVerdict(ok=False, gate=gate, code=codes[0],
                           message=f"{len(crit)} blocker(s): " + ", ".join(p.field_key for p in crit))
    if prompts:
        return GateVerdict(ok=True, gate=gate, code="VERIFY",
                           message=f"{len(prompts)} item(s) to verify: " + ", ".join(p.field_key for p in prompts))
    return GateVerdict(ok=True, gate=gate, code="OK", message=ok_msg)


def pitch_per_foot(house_h_in: float, front_h_in: float, proj_in: float) -> float:
    """Water runoff pitch, inches of drop per foot of projection."""
    return (house_h_in - front_h_in) / (proj_in / 12.0)


def dims_valid(pkg: SalesIntakePackage) -> bool:
    return all(getattr(pkg, k) is not None and BOUNDS[k][0] <= getattr(pkg, k) <= BOUNDS[k][1] for k in BOUNDS)


# --------------------------------------------------------------------------- #
def ge_1(pkg: SalesIntakePackage) -> Outcome:
    """Geometric envelope: the four numbers Engineering cannot guess."""
    prompts: List[MissingItemPrompt] = []
    codes: List[str] = []
    for key, (lo, hi, title, how) in BOUNDS.items():
        v = getattr(pkg, key)
        if v is None:
            codes.append("MISSING_DIMENSION")
            prompts.append(_critical("GE_1", key, f"{title} is missing", f"{how} Enter inches.", kind=InputType.NUMBER))
        elif not (lo <= v <= hi):
            codes.append("DIMENSION_OUT_OF_RANGE")
            prompts.append(_critical("GE_1", key, f"{title}: {v:g}\" looks wrong",
                                     f"Expected {lo:g}-{hi:g} inches. Did you enter feet? {how} Enter inches."))
    codes.sort(key=lambda c: c != "MISSING_DIMENSION")   # report missing before out-of-range
    return _verdict("GE_1", prompts, codes, "width, projection and both heights present"), prompts


def ge_2(pkg: SalesIntakePackage, analysis: Optional[PhotoAnalysis] = None) -> Outcome:
    """Substrate validity, tie-in evidence, and runoff pitch."""
    analysis = analysis or PhotoAnalysis()
    prompts: List[MissingItemPrompt] = []
    codes: List[str] = []
    choices = [s.value for s in SubstrateType if s != SubstrateType.UNKNOWN]
    suggest = (analysis.substrate if analysis.substrate not in (None, SubstrateType.UNKNOWN)
               and analysis.substrate_confidence >= ANALYZER_MIN_CONFIDENCE else None)

    if pkg.substrate == SubstrateType.UNKNOWN:
        codes.append("UNKNOWN_SUBSTRATE")
        hint = f" The photos suggest {suggest.value}; confirm or correct it." if suggest else ""
        prompts.append(_critical("GE_2", "substrate", "What is the roof tying into?",
                                 "Pick the wall/eave material at the attachment line." + hint,
                                 kind=InputType.CHOICE, choices=choices))
    elif suggest and suggest != pkg.substrate:
        prompts.append(_verify("GE_2", "substrate", f"Photo looks like {suggest.value}, you chose {pkg.substrate.value}",
                               "Re-check the attachment surface and re-pick if needed.", choices=choices))

    tie = pkg.photo(PhotoKind.TIE_IN_CLOSEUP)
    retake = ("Close-up of the exact attachment line: eave/fascia edge, wall surface and any trim, "
              "from 3-4 ft away, in focus.")
    if tie is None:
        codes.append("TIE_IN_PHOTO_MISSING")
        prompts.append(_critical("GE_2", "photo.tie_in_closeup", "Tie-in close-up photo is missing", retake,
                                 photo=True, kind=InputType.PHOTO))
    else:
        short = min(x for x in (tie.width_px, tie.height_px) if x is not None) if (tie.width_px or tie.height_px) else None
        if (short is not None and short < MIN_PHOTO_SHORT_SIDE_PX) or \
                (tie.sharpness is not None and tie.sharpness < MIN_PHOTO_SHARPNESS):
            codes.append("TIE_IN_PHOTO_QUALITY")
            prompts.append(_critical("GE_2", "photo.tie_in_closeup", "Tie-in photo is too small or blurry",
                                     "Retake it. " + retake, photo=True, kind=InputType.PHOTO))
    if pkg.photo(PhotoKind.WIDE_CONTEXT) is None:
        prompts.append(_verify("GE_2", "photo.wide_context", "Add a wide shot of the whole patio",
                               "Stand back far enough to show the house wall, the slab and the yard edge.",
                               photo=True, kind=InputType.PHOTO))
    if pkg.photo(PhotoKind.SKETCH) is None:
        prompts.append(_verify("GE_2", "photo.sketch", "Add a photo of your sketch",
                               "Dimensions and post locations marked on a sketch, photographed flat.",
                               photo=True, kind=InputType.PHOTO))

    pitch = None
    if dims_valid(pkg):
        pitch = pitch_per_foot(pkg.house_h_in, pkg.front_h_in, pkg.proj_in)
        if round(pitch, 9) < MIN_PITCH_IN_PER_FT:
            codes.append("INSUFFICIENT_PITCH")
            why = ("The front is HIGHER than the house wall, so water would run into the house. "
                   if pitch < 0 else "")
            prompts.append(_critical(
                "GE_2", "front_h_in", f"Roof pitch is only {pitch:.2f}\"/ft (minimum {MIN_PITCH_IN_PER_FT:g}\"/ft)",
                f"{why}Re-measure both heights. If they are right, the front must come down: at "
                f"{pkg.proj_in:g}\" projection it needs at least {MIN_PITCH_IN_PER_FT * pkg.proj_in / 12:.2f}\" of drop.",
                kind=InputType.NUMBER))
        elif pitch > STEEP_PITCH_IN_PER_FT:
            prompts.append(_verify("GE_2", "front_h_in", f"Roof pitch is steep: {pitch:.1f}\"/ft",
                                   "Confirm both heights; this is unusually steep for a screen room.",
                                   kind=InputType.NUMBER))
    return _verdict("GE_2", prompts, codes, "substrate known, tie-in photo usable, pitch ok"), prompts


def effective_obstacles(pkg: SalesIntakePackage, analysis: PhotoAnalysis) -> List[DetectedObstacle]:
    seen: Dict[ObstacleKind, float] = {k: 1.0 for k in pkg.obstacles_declared}
    for o in analysis.obstacles:
        if o.confidence >= 0.5:
            seen[o.kind] = max(seen.get(o.kind, 0.0), o.confidence)
    return [DetectedObstacle(kind=k, confidence=c) for k, c in seen.items()]


def ge_3(pkg: SalesIntakePackage, analysis: Optional[PhotoAnalysis] = None) -> Outcome:
    """Door swing vs the 36" chair rail, plus downspouts and steps."""
    analysis = analysis or PhotoAnalysis()
    prompts: List[MissingItemPrompt] = []
    codes: List[str] = []
    yn = ["yes", "no"]
    door_seen = pkg.door_swing_detected is True or analysis.door_swing_detected is True or \
        any(o.kind == ObstacleKind.EXTERIOR_DOOR and o.confidence >= 0.5 for o in analysis.obstacles)

    if pkg.door_swing_detected is None:
        codes.append("DOOR_SWING_UNVERIFIED")
        seen = " The photos appear to show a door." if door_seen else ""
        prompts.append(_critical("GE_3", "door_swing_detected", "Does any exterior door swing OUT into the room?",
                                 "Check every door opening onto the footprint, including the garage side door." + seen,
                                 kind=InputType.CHOICE, choices=yn))
    elif pkg.door_swing_detected and pkg.door_clearance_confirmed is not True:
        codes.append("OUT_SWING_DOOR_UNCONFIRMED")
        prompts.append(_critical(
            "GE_3", "door_clearance_confirmed", "Out-swing door vs the 36\" chair rail: confirm clearance",
            f"Open the door fully. Measure leaf width, hinge side and how far the leaf sweeps; confirm it clears "
            f"the {CHAIR_RAIL_AFF_IN:g}\" chair rail and the nearest post, or take a photo for Engineering. "
            f"Answer yes only if it clears.", kind=InputType.CHOICE, choices=yn))
    elif pkg.door_swing_detected is False and analysis.door_swing_detected is True:
        prompts.append(_verify("GE_3", "door_swing_detected", "Photos show a door; you said none swings out",
                               "Look again at every door on the footprint.", choices=yn))

    declared = effective_obstacles(pkg, analysis)
    for ob in declared:
        if ob.kind in FLAGGED_OBSTACLES and ob.kind not in pkg.acknowledged_obstacles:
            prompts.append(_verify("GE_3", f"obstacle_ack.{ob.kind.value}",
                                   f"{ob.kind.value.title()} in the footprint",
                                   "Note where it is and whether it moves, is boxed around, or stays. "
                                   "Tap acknowledged once Engineering can see it in the photos.",
                                   choices=["acknowledged"]))
    if not declared and pkg.obstacles_checked is not True:
        prompts.append(_verify("GE_3", "obstacles_checked", "Walk the perimeter: any downspouts, steps or meters?",
                               "Tap yes once you have checked every wall and corner.", choices=yn))
    return _verdict("GE_3", prompts, codes, "door swing confirmed, obstacles acknowledged"), prompts


# --------------------------------------------------------------------------- #
def evaluate_intake(pkg: SalesIntakePackage, analysis: Optional[PhotoAnalysis] = None) -> EngineeringReadinessReport:
    analysis = analysis or PhotoAnalysis()
    outcomes = [ge_1(pkg), ge_2(pkg, analysis), ge_3(pkg, analysis)]
    prompts = [p for _, ps in outcomes for p in ps]
    crit = sum(p.severity == Severity.CRITICAL for p in prompts)
    verify = len(prompts) - crit
    pitch = pitch_per_foot(pkg.house_h_in, pkg.front_h_in, pkg.proj_in) if dims_valid(pkg) else None
    detected = pkg.substrate
    if detected == SubstrateType.UNKNOWN and analysis.substrate and analysis.substrate_confidence >= ANALYZER_MIN_CONFIDENCE:
        detected = analysis.substrate
    return EngineeringReadinessReport(
        job_id=pkg.job_id,
        is_ready_for_engineering=crit == 0,
        confidence_score=round(max(0.0, 1.0 - 0.2 * crit - 0.05 * verify), 2),
        detected_substrate=detected,
        missing_items=prompts,
        detected_obstacles=effective_obstacles(pkg, analysis),
        pitch_per_foot_in=None if pitch is None else round(pitch, 3),
        pitch_deg=None if pitch is None else round(math.degrees(math.atan(pitch / 12.0)), 2),
        verdicts=[v for v, _ in outcomes],
    )
