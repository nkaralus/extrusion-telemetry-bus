"""5-slot TDMA execution frame (ported from mule-protocol).

    SLOT_1_TASK     raw scope ingest
    SLOT_2_PLAN     3B model -> strict Pydantic cut plan (the ONLY thing it may emit)
    SLOT_3_EXECUTE  bus checks 10.2.2.0/24 for drops before pulling raw stock
    SLOT_4_GUARD    GI_1/GI_2/GI_3 with deterministic kerf + miter math
    SLOT_5_VERIFY   atomic registry commit, or DLQ trip

Slot order is enforced.  After SLOT_5 the clock wraps to SLOT_3 for the next
piece; a gate rejection or PIECE_MISSING wraps SLOT_4 back to SLOT_3.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Literal, Optional, Set
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .gates import GateVerdict
from .physics import CutSpec

MAX_PIECES_PER_ORDER = 1000


class SlotId(str, Enum):
    SLOT_1_TASK = "SLOT_1_TASK"
    SLOT_2_PLAN = "SLOT_2_PLAN"
    SLOT_3_EXECUTE = "SLOT_3_EXECUTE"
    SLOT_4_GUARD = "SLOT_4_GUARD"
    SLOT_5_VERIFY = "SLOT_5_VERIFY"


class SlotOrderError(RuntimeError):
    pass


_S1, _S2, _S3, _S4, _S5 = (
    SlotId.SLOT_1_TASK, SlotId.SLOT_2_PLAN, SlotId.SLOT_3_EXECUTE,
    SlotId.SLOT_4_GUARD, SlotId.SLOT_5_VERIFY,
)
_ALLOWED: Dict[Optional[SlotId], Set[SlotId]] = {
    None: {_S1},
    _S1: {_S2},
    _S2: {_S3},
    _S3: {_S4},
    _S4: {_S3, _S5},
    _S5: {_S3},
}


# --------------------------------------------------------------------------- #
# Slot payloads
# --------------------------------------------------------------------------- #
class Slot1Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_id: str
    job_id: str = Field(min_length=1)
    raw_scope: str = Field(min_length=1, max_length=4000)


class CutLine(CutSpec):
    """One line of the model's plan: a CutSpec plus a quantity."""
    qty: int = Field(ge=1, le=MAX_PIECES_PER_ORDER)


class Slot2Plan(BaseModel):
    """The strict grammar the 3B model must satisfy (no free-form text allowed)."""
    model_config = ConfigDict(extra="forbid")
    op: Literal["allocate_cuts"] = "allocate_cuts"
    cuts: List[CutLine] = Field(min_length=1, max_length=50)

    def expand(self) -> List[CutSpec]:
        pieces: List[CutSpec] = []
        for line in self.cuts:
            spec = CutSpec.model_validate(line.model_dump(exclude={"qty"}))
            pieces.extend(spec.model_copy() for _ in range(line.qty))
        if len(pieces) > MAX_PIECES_PER_ORDER:
            raise ValueError(f"order expands to {len(pieces)} pieces (max {MAX_PIECES_PER_ORDER})")
        return pieces


class Slot3Execute(BaseModel):
    step_id: int
    source_addr: str
    source_kind: str
    source_bin: str
    via_remnant: bool
    lead_mode: str
    spec: CutSpec
    instruction: str


class Slot4Guard(BaseModel):
    step_id: int
    ok: bool
    verdicts: List[GateVerdict]


class Slot5Verify(BaseModel):
    step_id: int
    result: Literal["CUT_OK", "MISCUT_NAK", "PIECE_MISSING"]
    committed: bool
    disposition: Optional[str] = None
    leftover_addr: Optional[str] = None
    scrap_lb: float = 0.0


class DeadLetter(BaseModel):
    frame_id: str
    slot: SlotId
    code: str
    message: str
    payload: dict = Field(default_factory=dict)


class FramePhase(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    DLQ = "dlq"


# --------------------------------------------------------------------------- #
# Frame (the TDMA clock + order state)
# --------------------------------------------------------------------------- #
class Frame(BaseModel):
    frame_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    job_id: str
    slot: Optional[SlotId] = None
    phase: FramePhase = FramePhase.ACTIVE
    naks: int = 0
    task: Optional[Slot1Task] = None
    plan: Optional[Slot2Plan] = None
    pending: List[CutSpec] = Field(default_factory=list)
    piece_jobs: List[str] = Field(default_factory=list)   # batch frames: job that owns each pending piece
    station: Optional[str] = None                          # saw station this frame is attached to
    note: str = ""                                         # e.g. "rebuilt from cart after restart"
    due_ts: Optional[float] = None                         # when the shop needs this job's material (procurement lookahead)
    excluded: List[str] = Field(default_factory=list)   # source addrs rejected for the current piece
    step_counter: int = 0
    current: Optional[Slot3Execute] = None
    guard: Optional[Slot4Guard] = None
    history: List[Slot5Verify] = Field(default_factory=list)
    dlq: Optional[DeadLetter] = None

    def advance(self, to: SlotId) -> None:
        if self.phase != FramePhase.ACTIVE:
            raise SlotOrderError(f"frame {self.frame_id} is {self.phase.value}")
        if to not in _ALLOWED.get(self.slot, set()):
            raise SlotOrderError(f"illegal slot transition {self.slot} -> {to}")
        self.slot = to

    def trip_dlq(self, slot: SlotId, code: str, message: str, **payload) -> DeadLetter:
        self.dlq = DeadLetter(frame_id=self.frame_id, slot=slot, code=code,
                              message=message, payload=payload)
        self.phase = FramePhase.DLQ
        self.current = None
        return self.dlq

    def resume(self) -> None:
        """Un-trip a NO_STOCK DLQ (stock was adopted/received).  Other DLQs stay dead."""
        if self.phase == FramePhase.DLQ and self.dlq and self.dlq.code == "NO_STOCK":
            self.phase, self.dlq = FramePhase.ACTIVE, None
        else:
            raise SlotOrderError("only a NO_STOCK frame can resume")

    def finish(self) -> None:
        self.phase = FramePhase.DONE
        self.current = None
