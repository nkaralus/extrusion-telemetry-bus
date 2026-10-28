"""FabricationBus: runs the 5-slot TDMA frame against the registry.

Frame-free primitives (used by the HTTP API and by the frame flow):
    verify(proposal)  -> GateReport             (no mutation)
    commit(proposal)  -> CommitResult           (CUT_OK or MISCUT_NAK, atomic)
    report_missing()  -> purge ghost drop
Frame flow (operator + macro pad):
    open_frame -> stage_next_cut -> ack(CUT_OK | MISCUT_NAK | PIECE_MISSING) -> ...
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Callable, List, Literal, Optional, Set, Tuple, Union

from pydantic import BaseModel

from .gates import CutProposal, GateReport, run_gates
from .labels import Label, build_label
from .llm import PlanParser
from .mule_frame import (
    Frame, FramePhase, Slot1Task, Slot2Plan, Slot3Execute, Slot4Guard, Slot5Verify, SlotId,
)
from .physics import (
    KERF_IN, REMNANT_MIN_IN, CutPlan, CutSpec, End, LeadMode, ceil_to_shop, classify_lead,
    floor_to_shop, fmt_inches, parse_inches,
)
from .registry import RACK_FULL_RATIO, PieceRecord, Registry, StockItem, StockKind

MODE_RANK = {LeadMode.NATIVE: 0, LeadMode.MITER_LEAD: 1, LeadMode.TRUE_SQUARE: 2}


class GateRejected(RuntimeError):
    def __init__(self, report: GateReport) -> None:
        self.report = report
        f = report.failed
        super().__init__(f"{f.gate} {f.code}: {f.message}" if f else "gate rejected")


class Disposition(str, Enum):
    REMNANT_RACK = "REMNANT_RACK"
    SCRAP_BIN = "SCRAP_BIN"
    NONE = "NONE"


class CommitResult(BaseModel):
    outcome: Literal["CUT_OK", "MISCUT_NAK"]
    source_addr: str
    plan: CutPlan
    disposition: Disposition
    leftover_addr: Optional[str] = None
    leftover_bin: Optional[str] = None
    scrap_lb: float = 0.0
    scrap_reason: Optional[str] = None
    piece_credited: bool
    label: Optional[Label] = None
    screen: str = ""  # what the tablet flashes: "RACK BLUE-MEDIUM" / "SCRAP BIN"


class AdoptResult(BaseModel):
    addr: str
    uid: str
    bin_location: str
    long_point_in: float
    guaranteed_square_in: float
    below_remnant_min: bool
    label: Label
    next_step: Optional[Slot3Execute] = None


class AckResult(BaseModel):
    result: str
    commit: Optional[CommitResult] = None
    next_step: Optional[Slot3Execute] = None
    next_guard: Optional[Slot4Guard] = None
    frame_phase: str
    elapsed_ms: float = 0.0


class FabricationBus:
    def __init__(
        self,
        registry: Registry,
        parser: Optional[PlanParser] = None,
        kerf: float = KERF_IN,
        remnant_min_in: float = REMNANT_MIN_IN,
        max_naks: int = 3,
        default_station: Optional[str] = None,
    ) -> None:
        self.reg = registry
        self.parser = parser
        self.kerf = kerf
        self.remnant_min_in = remnant_min_in
        self.max_naks = max_naks
        self.default_station = default_station or next(iter(registry.state.stations), "")

    # ------------------------------------------------------------------ #
    # Frame-free primitives
    # ------------------------------------------------------------------ #
    def proposal(self, addr: str, spec: CutSpec, job_id: str, station: Optional[str] = None) -> CutProposal:
        return CutProposal(addr=addr, station=station or self.default_station, job_id=job_id, spec=spec)

    def verify(self, p: CutProposal) -> GateReport:
        return run_gates(self.reg, p, self.kerf)

    def candidates(self, spec: CutSpec, job_id: str,
                   exclude: Set[str] = frozenset()) -> List[Tuple[StockItem, CutPlan]]:
        """Drops in 10.2.2.0/24 first, then virgin sticks in 10.2.1.0/24; best-fit within each tier."""
        from .gates import _plan_for  # local import keeps gates' public surface small

        ranked = []
        for tier, kind in ((0, StockKind.REMNANT), (1, StockKind.RAW)):
            for item in self.reg.stock_of(spec.profile, kind):
                if item.addr in exclude or item.allocated_job_id not in (None, job_id):
                    continue
                if classify_lead(item.end_a, spec.lead_end()) is None:
                    continue
                plan = _plan_for(self.reg, item, spec, self.kerf)
                if plan.fits:
                    ranked.append(((tier, MODE_RANK[plan.mode], plan.leftover_long_in, item.addr), item, plan))
        ranked.sort(key=lambda r: r[0])
        return [(i, p) for _, i, p in ranked]

    def commit(self, p: CutProposal, outcome: Literal["CUT_OK", "MISCUT_NAK"] = "CUT_OK",
               actual_length_in: Optional[float] = None) -> CommitResult:
        """SLOT_5: re-run the gates inside the transaction, then apply atomically.

        For MISCUT_NAK the saw already cut, so the stock is consumed and the drop is
        recalculated from ``actual_length_in`` (if given); the piece goes to scrap
        and is NOT credited to the job cart.
        """
        spec = p.spec
        if actual_length_in is not None:
            spec = spec.model_copy(update={"length_in": ceil_to_shop(actual_length_in)})
        p = p.model_copy(update={"spec": spec})
        with self.reg.transaction():
            report = run_gates(self.reg, p, self.kerf)
            if not report.ok:
                raise GateRejected(report)
            plan = report.plan
            prof = self.reg.profile(spec.profile)
            self.reg.consume(p.addr, reason=outcome)
            self.reg.open_job(p.job_id)

            leftover_addr = leftover_bin = None
            label = None
            scrap_lb = 0.0
            disposition = Disposition.NONE
            screen = "NO DROP"
            scrap_reason = None
            if plan.leftover_long_in > 0:
                racked, slots, ratio = self.reg.rack_utilization(spec.profile)
                if plan.leftover_guaranteed_in >= self.remnant_min_in and ratio >= RACK_FULL_RATIO:
                    scrap_reason = "RACK_FULL"      # qualifies as a drop, but the rack is >= 85% full
                elif plan.leftover_guaranteed_in < self.remnant_min_in:
                    scrap_reason = "DROP_UNDER_MIN"
                if scrap_reason is None:
                    rem = self.reg.add_remnant(
                        spec.profile, plan.leftover_long_in, plan.leftover_end_a, plan.leftover_end_b,
                        origin=f"{p.job_id}:{p.addr}")
                    disposition = Disposition.REMNANT_RACK
                    leftover_addr, leftover_bin = rem.addr, rem.bin_location
                    label = build_label(rem, prof)
                    screen = f"RACK {rem.bin_location}"
                else:
                    entry = self.reg.log_scrap(spec.profile, plan.leftover_long_in,
                                               scrap_reason, p.job_id)
                    disposition = Disposition.SCRAP_BIN
                    scrap_lb = entry.weight_lb
                    screen = "SCRAP BIN" + (" (RACK FULL)" if scrap_reason == "RACK_FULL" else "")

            credited = outcome == "CUT_OK"
            seq = self.reg.next_piece_seq()
            if credited:
                self.reg.add_piece(p.job_id, PieceRecord(
                    seq=seq, profile=spec.profile, length_in=spec.length_in,
                    miter_deg=spec.miter_deg, face=spec.face.value, source_addr=p.addr))
            else:
                bad = self.reg.log_scrap(spec.profile, spec.length_in, "MISCUT", p.job_id)
                scrap_lb += bad.weight_lb
                self.reg.log("MISCUT", addr=p.addr, job_id=p.job_id, length_in=spec.length_in)
            return CommitResult(
                outcome=outcome, source_addr=p.addr, plan=plan, disposition=disposition,
                leftover_addr=leftover_addr, leftover_bin=leftover_bin, scrap_lb=round(scrap_lb, 3),
                scrap_reason=scrap_reason,
                piece_credited=credited, label=label, screen=screen)

    def report_missing(self, addr: str) -> bool:
        """Ghost-drop bypass: purge from active registry, log as Unaccounted."""
        return self.reg.purge_missing(addr) is not None

    # ------------------------------------------------------------------ #
    # Frame flow
    # ------------------------------------------------------------------ #
    def open_frame(self, job_id: str, raw_scope: str) -> Frame:
        """SLOT_1 + SLOT_2.  NAK + retry on bad model output; 3 strikes -> DLQ."""
        if self.parser is None:
            raise RuntimeError("no parser configured")
        frame = Frame(job_id=job_id)
        frame.advance(SlotId.SLOT_1_TASK)
        frame.task = Slot1Task(frame_id=frame.frame_id, job_id=job_id, raw_scope=raw_scope)
        plan, pieces, frame.naks, feedback = self.parse_scope(raw_scope)
        if plan is None:
            self._dlq(frame, SlotId.SLOT_2_PLAN, "PLAN_NAK_LIMIT", feedback or "parser failed",
                      scope=raw_scope)
            return frame
        frame.advance(SlotId.SLOT_2_PLAN)
        frame.plan, frame.pending = plan, pieces
        self.reg.open_job(job_id)
        return frame

    def parse_scope(self, raw_scope: str):
        """SLOT_2 NAK loop.  Returns (plan|None, pieces, naks, last_feedback)."""
        if self.parser is None:
            raise RuntimeError("no parser configured")
        feedback: Optional[str] = None
        naks = 0
        for attempt in range(1, self.max_naks + 1):
            try:
                plan = Slot2Plan.model_validate_json(self.parser(raw_scope, feedback))
                unknown = sorted({c.profile for c in plan.cuts} - set(self.reg.state.profiles))
                if unknown:
                    raise ValueError(f"unknown part number(s): {', '.join(unknown)}")
                return plan, plan.expand(), naks, feedback
            except Exception as exc:  # noqa: BLE001 - every parser failure is a NAK
                naks += 1
                feedback = f"NAK {attempt}/{self.max_naks}: {str(exc)[:300]}"
        return None, [], naks, feedback

    def frame_from_specs(self, job_id: str, specs: List[CutSpec]) -> Frame:
        """Open a frame on an already-validated piece list (compiled nest, scratch simulation)."""
        frame = Frame(job_id=job_id)
        frame.advance(SlotId.SLOT_1_TASK)
        frame.advance(SlotId.SLOT_2_PLAN)
        frame.pending = list(specs)
        self.reg.open_job(job_id)
        return frame

    def adopt_drop(self, profile: str, tape_in: Union[float, str], end_a: End, end_b: Optional[End] = None,
                   job_id: Optional[str] = None, frame: Optional[Frame] = None) -> "AdoptResult":
        """Lazy-load onboarding: register an untagged boneyard piece mid-workflow.

        The operator logs profile, tape measurement and end condition once.  The bus assigns
        a 10.2.2.x address, bins it, locks it to the job (if given) and returns the sticker.
        Tape lengths round DOWN to 1/16".  If a frame is parked on NO_STOCK it resumes and
        stages the next cut, which will now find the adopted drop.
        """
        with self.reg.transaction():
            prof = self.reg.profile(profile)
            length = floor_to_shop(parse_inches(tape_in))
            if not (0 < length <= 600):
                raise ValueError(f"tape measurement {tape_in} out of range")
            item = self.reg.add_remnant(profile, length, end_a, end_b or End.square(),
                                        origin="legacy", legacy=True)
            if job_id:
                self.reg.lock(item.addr, job_id)
            self.reg.log("ADOPTED", addr=item.addr, profile=profile, length_in=length)
            label = build_label(item, prof)
        nxt = None
        if frame is not None and frame.phase == FramePhase.DLQ and frame.dlq and frame.dlq.code == "NO_STOCK":
            frame.resume()
            nxt = self.stage_next_cut(frame)
        return AdoptResult(
            addr=item.addr, uid=item.uid, bin_location=item.bin_location or "",
            long_point_in=item.long_point_in, guaranteed_square_in=item.guaranteed_square_in,
            below_remnant_min=item.guaranteed_square_in < self.remnant_min_in,
            label=label, next_step=nxt)

    def _dlq(self, frame: Frame, slot: SlotId, code: str, message: str, **payload) -> None:
        dl = frame.trip_dlq(slot, code, message, **payload)
        self.reg.push_dlq(**dl.model_dump(mode="json"))

    def _describe(self, item: StockItem, plan: CutPlan, spec: CutSpec) -> str:
        what = "DROP" if item.kind == StockKind.REMNANT else "RAW STICK"
        prep = {
            LeadMode.NATIVE: "END A AS-IS",
            LeadMode.TRUE_SQUARE: f"TRUE END A SQUARE (-{fmt_inches(plan.offset_in)})",
            LeadMode.MITER_LEAD: f"MITER END A {spec.lead_miter_deg:g}deg",
        }[plan.mode]
        trail = (f"{spec.miter_deg:g}deg LONG PT {spec.face.value.split('_')[0].upper()}"
                 if spec.miter_deg else "SQUARE")
        return (f"PULL {what} {item.addr} ({item.bin_location}) | {prep} | "
                f"CUT {fmt_inches(spec.length_in)} {trail}")

    def stage_next_cut(self, frame: Frame, station: Optional[str] = None) -> Optional[Slot3Execute]:
        """SLOT_3 (drops before raw) -> SLOT_4 (gates).  Locks the source on success."""
        if frame.phase != FramePhase.ACTIVE:
            return None
        if not frame.pending:
            frame.finish()
            return None
        spec = frame.pending[0]
        if frame.slot != SlotId.SLOT_3_EXECUTE:
            frame.advance(SlotId.SLOT_3_EXECUTE)
        last_reject = "no stock fits"
        tried: Set[str] = set(frame.excluded)
        while True:
            options = self.candidates(spec, frame.job_id, exclude=tried)
            if not options:
                self._dlq(frame, SlotId.SLOT_3_EXECUTE, "NO_STOCK",
                          f"nothing satisfies {spec.profile} {fmt_inches(spec.length_in)} ({last_reject})",
                          spec=spec.model_dump(mode="json"))
                return None
            item, _ = options[0]
            prop = self.proposal(item.addr, spec, frame.job_id, station)
            frame.advance(SlotId.SLOT_4_GUARD)
            report = self.verify(prop)
            frame.step_counter += 1
            frame.guard = Slot4Guard(step_id=frame.step_counter, ok=report.ok, verdicts=report.verdicts)
            if report.ok:
                self.reg.lock(item.addr, frame.job_id)
                frame.excluded = []
                frame.current = Slot3Execute(
                    step_id=frame.step_counter, source_addr=item.addr, source_kind=item.kind.value,
                    source_bin=item.bin_location or "", via_remnant=item.kind == StockKind.REMNANT,
                    lead_mode=report.plan.mode.value, spec=spec,
                    instruction=self._describe(item, report.plan, spec))
                return frame.current
            tried.add(item.addr)
            frame.excluded = sorted(tried)
            last_reject = f"{report.failed.code} on {item.addr}"
            frame.advance(SlotId.SLOT_3_EXECUTE)

    def ack(self, frame: Frame, result: Literal["CUT_OK", "MISCUT_NAK", "PIECE_MISSING"],
            actual_length_in: Optional[float] = None, station: Optional[str] = None) -> AckResult:
        """Macro-pad keys: 1=CUT_OK, 3=MISCUT_NAK; ghost-drop button=PIECE_MISSING."""
        t0 = time.perf_counter()
        cur = frame.current
        if frame.phase != FramePhase.ACTIVE or cur is None:
            raise RuntimeError("no staged cut to acknowledge")
        commit: Optional[CommitResult] = None
        if result == "PIECE_MISSING":
            self.report_missing(cur.source_addr)
            frame.history.append(Slot5Verify(step_id=cur.step_id, result=result, committed=False))
            frame.advance(SlotId.SLOT_3_EXECUTE)
        else:
            frame.advance(SlotId.SLOT_5_VERIFY)
            prop = self.proposal(cur.source_addr, cur.spec, frame.job_id, station)
            try:
                commit = self.commit(prop, outcome=result, actual_length_in=actual_length_in)
            except GateRejected as exc:
                self._dlq(frame, SlotId.SLOT_5_VERIFY, "COMMIT_REJECTED", str(exc), step_id=cur.step_id)
                raise
            frame.history.append(Slot5Verify(
                step_id=cur.step_id, result=result, committed=True, disposition=commit.disposition.value,
                leftover_addr=commit.leftover_addr, scrap_lb=commit.scrap_lb))
            if commit.piece_credited:
                frame.pending.pop(0)  # a miscut leaves the piece pending: replacement is re-routed
        frame.current = None
        nxt = self.stage_next_cut(frame, station)
        return AckResult(result=result, commit=commit, next_step=nxt, next_guard=frame.guard if nxt else None,
                         frame_phase=frame.phase.value, elapsed_ms=round((time.perf_counter() - t0) * 1000, 3))

    def flip_current(self, frame: Frame, station: Optional[str] = None) -> Optional[Slot3Execute]:
        """FLIP_MITER (key 2): turn the staged piece over and re-source it.

        Every long point moves to the other face, the old reservation is released, and the
        next cut is staged against the flipped spec (it may pick different stock).
        """
        cur = frame.current
        if frame.phase != FramePhase.ACTIVE or cur is None:
            raise RuntimeError("no staged cut to flip")
        flipped = frame.pending[0].flipped()      # ValueError for a square cut
        self.reg.unlock(cur.source_addr)
        frame.pending[0] = flipped
        frame.current = None
        frame.excluded = []
        return self.stage_next_cut(frame, station)

    def release(self, frame: Frame) -> None:
        """Abandon a frame: free any reservation it still holds."""
        if frame.current is not None:
            self.reg.unlock(frame.current.source_addr)
            frame.current = None

    def run_to_completion(self, frame: Frame,
                          operator: Callable[[Slot3Execute], str] = lambda step: "CUT_OK") -> List[AckResult]:
        """Drive a frame with a scripted operator (tests, CLI demo, simulations)."""
        acks: List[AckResult] = []
        if self.stage_next_cut(frame) is None:
            return acks
        while frame.current is not None:
            acks.append(self.ack(frame, operator(frame.current)))
        return acks
