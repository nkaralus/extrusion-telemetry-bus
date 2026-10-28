"""FastAPI surface for the saw bench, engineering desk, and office stations.

Zero cloud.  Everything runs on the shop LAN against state_registry.json.

    POST /cut/verify       gate check, no mutation
    POST /cut/complete     CUT_OK commit (atomic) -> remnant label or SCRAP BIN
    POST /cut/miscut       MISCUT_NAK commit + immediate replacement routing
    POST /cut/flip         FLIP_MITER: flip the long-point face and re-verify
    POST /drop/missing     ghost-drop bypass (+ optional re-route)
    GET  /label/{addr}     ESC/POS sticker for a racked remnant
    POST /order            SLOT_1+2: parse scope into a frame
    POST /order/{id}/next  SLOT_3+4: stage next cut
    POST /order/{id}/ack   SLOT_5: CUT_OK | MISCUT_NAK | PIECE_MISSING
"""
from __future__ import annotations

import os
import threading
from typing import Dict, List, Literal, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from .bus import AckResult, AdoptResult, CommitResult, FabricationBus, GateRejected
from .gates import CutProposal, GateReport
from .labels import Label, build_label
from .llm import ChainParser, OllamaParser, PlanParser, RuleParser
from .mule_frame import CutLine, Frame, Slot2Plan, Slot3Execute, Slot4Guard
from .nesting import NestPlan, compile_cut_list
from .physics import CutSpec, parse_end_condition, parse_inches
from .procurement import ReorderAlert, reorder_report
from .quoting import Quote, quote_job
from .registry import Registry, StockKind


class OrderIn(BaseModel):
    job_id: str
    scope: str


class AckIn(BaseModel):
    result: Literal["CUT_OK", "MISCUT_NAK", "PIECE_MISSING"]
    actual_length_in: Optional[float] = None
    station: Optional[str] = None


class MiscutIn(BaseModel):
    proposal: CutProposal
    actual_length_in: Optional[float] = None


class MissingIn(BaseModel):
    addr: str
    job_id: Optional[str] = None
    station: Optional[str] = None
    spec: Optional[CutSpec] = None


class FlipOut(BaseModel):
    spec: CutSpec
    report: GateReport


class RerouteOut(BaseModel):
    purged: bool
    reroute_addr: Optional[str] = None
    reroute_report: Optional[GateReport] = None


class StepOut(BaseModel):
    frame_id: str
    phase: str
    step: Optional[Slot3Execute] = None
    guard: Optional[Slot4Guard] = None
    dlq: Optional[dict] = None


class AdoptIn(BaseModel):
    profile: str
    tape_in: Union[float, str]               # 74.25 | "74 1/4"
    end_a: str = "square"                    # square | miter_45_spline | miter_45_back
    end_b: str = "square"
    job_id: Optional[str] = None
    frame_id: Optional[str] = None

    @field_validator("tape_in")
    @classmethod
    def _tape(cls, v):
        return parse_inches(v)


class BomIn(BaseModel):
    job_id: str
    scope: Optional[str] = None
    cuts: Optional[List[CutLine]] = None
    allow_purchase: bool = False


class QuoteIn(BomIn):
    margin_pct: float = 25.0
    labor_per_cut: float = 0.0


class OrderStickIn(BaseModel):
    profile: str
    qty: int
    eta_days: Optional[float] = None
    length_in: Optional[float] = None


def default_parser(registry: Registry) -> PlanParser:
    rules = RuleParser(registry)
    mode = os.environ.get("EXTBUS_PARSER", "auto")
    if mode == "rules":
        return rules
    ollama = OllamaParser(registry, model=os.environ.get("EXTBUS_MODEL", "qwen2.5:3b"),
                          host=os.environ.get("EXTBUS_OLLAMA", "http://127.0.0.1:11434"))
    return ollama if mode == "ollama" else ChainParser(ollama, rules)


def create_app(registry: Optional[Registry] = None, parser: Optional[PlanParser] = None) -> FastAPI:
    if registry is None:
        registry = Registry.load(os.environ.get("EXTBUS_STATE", "state_registry.json"))
    bus = FabricationBus(registry, parser=parser or default_parser(registry))
    frames: Dict[str, Frame] = {}
    lock = threading.RLock()  # one writer at a time: deterministic closed loop
    app = FastAPI(title="extrusion-telemetry-bus", version="0.1.0")
    app.state.bus, app.state.frames = bus, frames

    def frame_or_404(fid: str) -> Frame:
        if fid not in frames:
            raise HTTPException(404, f"unknown frame {fid}")
        return frames[fid]

    def step_out(frame: Frame, step: Optional[Slot3Execute]) -> StepOut:
        return StepOut(frame_id=frame.frame_id, phase=frame.phase.value, step=step,
                       guard=frame.guard if step else None,
                       dlq=frame.dlq.model_dump(mode="json") if frame.dlq else None)

    @app.get("/health")
    def health() -> dict:
        s = registry.state
        return {"ok": True, "raw": sum(i.kind == StockKind.RAW for i in s.stock.values()),
                "remnants": sum(i.kind == StockKind.REMNANT for i in s.stock.values()),
                "scrap_lb": registry.scrap_total_lb(), "dlq": len(s.dlq)}

    @app.post("/cut/verify", response_model=GateReport)
    def cut_verify(p: CutProposal) -> GateReport:
        with lock:
            return bus.verify(p)

    @app.post("/cut/complete", response_model=CommitResult)
    def cut_complete(p: CutProposal) -> CommitResult:
        with lock:
            try:
                return bus.commit(p, "CUT_OK")
            except GateRejected as exc:
                raise HTTPException(409, detail=exc.report.model_dump(mode="json")) from exc

    @app.post("/cut/miscut", response_model=CommitResult)
    def cut_miscut(body: MiscutIn) -> CommitResult:
        with lock:
            try:
                return bus.commit(body.proposal, "MISCUT_NAK", body.actual_length_in)
            except GateRejected as exc:
                raise HTTPException(409, detail=exc.report.model_dump(mode="json")) from exc

    @app.post("/cut/flip", response_model=FlipOut)
    def cut_flip(p: CutProposal) -> FlipOut:
        with lock:
            try:
                flipped = p.model_copy(update={"spec": p.spec.flipped()})
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return FlipOut(spec=flipped.spec, report=bus.verify(flipped))

    @app.post("/drop/missing", response_model=RerouteOut)
    def drop_missing(body: MissingIn) -> RerouteOut:
        with lock:
            purged = bus.report_missing(body.addr)
            if body.spec is None or body.job_id is None:
                return RerouteOut(purged=purged)
            for item, _ in bus.candidates(body.spec, body.job_id):
                prop = bus.proposal(item.addr, body.spec, body.job_id, body.station)
                report = bus.verify(prop)
                if report.ok:
                    return RerouteOut(purged=purged, reroute_addr=item.addr, reroute_report=report)
            return RerouteOut(purged=purged)

    @app.get("/label/{addr}", response_model=Label)
    def label(addr: str) -> Label:
        with lock:
            item = registry.state.stock.get(addr)
            if item is None:
                raise HTTPException(404, f"{addr} not in registry")
            if item.kind != StockKind.REMNANT:
                raise HTTPException(409, f"{addr} is a raw stick; labels are for racked remnants")
            return build_label(item, registry.profile(item.profile))

    @app.post("/order", response_model=StepOut)
    def order(body: OrderIn) -> StepOut:
        with lock:
            frame = bus.open_frame(body.job_id, body.scope)
            frames[frame.frame_id] = frame
            out = step_out(frame, None)
            return out

    @app.post("/order/{fid}/next", response_model=StepOut)
    def order_next(fid: str, station: Optional[str] = None) -> StepOut:
        with lock:
            frame = frame_or_404(fid)
            if frame.current is not None:
                return step_out(frame, frame.current)
            return step_out(frame, bus.stage_next_cut(frame, station))

    @app.post("/order/{fid}/ack", response_model=AckResult)
    def order_ack(fid: str, body: AckIn) -> AckResult:
        with lock:
            frame = frame_or_404(fid)
            try:
                return bus.ack(frame, body.result, body.actual_length_in, body.station)
            except GateRejected as exc:
                raise HTTPException(409, detail=exc.report.model_dump(mode="json")) from exc
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc

    # ---------------- Phase 2 ---------------- #
    def specs_of(body: BomIn) -> List[CutSpec]:
        if body.cuts:
            return Slot2Plan(cuts=body.cuts).expand()
        if body.scope:
            plan, pieces, naks, fb = bus.parse_scope(body.scope)
            if plan is None:
                raise HTTPException(422, f"scope rejected after {naks} NAKs: {fb}")
            return pieces
        raise HTTPException(422, "send `scope` text or `cuts`")

    @app.post("/api/saw/adopt-drop", response_model=AdoptResult)
    def adopt_drop(body: AdoptIn) -> AdoptResult:
        with lock:
            frame = frame_or_404(body.frame_id) if body.frame_id else None
            try:
                return bus.adopt_drop(body.profile, body.tape_in, parse_end_condition(body.end_a),
                                      parse_end_condition(body.end_b), body.job_id, frame)
            except (ValueError, KeyError) as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:  # unknown profile
                raise HTTPException(422, f"{type(exc).__name__}: {exc}") from exc

    @app.post("/api/nest/compile", response_model=NestPlan)
    def nest_compile(body: BomIn) -> NestPlan:
        with lock:
            return compile_cut_list(bus, body.job_id, specs_of(body), allow_purchase=body.allow_purchase)

    @app.post("/api/nest/commit", response_model=StepOut)
    def nest_commit(body: BomIn) -> StepOut:
        """Compile, then open a frame that executes the compiled piece order on the real registry."""
        with lock:
            plan = compile_cut_list(bus, body.job_id, specs_of(body))
            if plan.unmet:
                raise HTTPException(409, f"{len(plan.unmet)} piece(s) cannot be cut from current stock; "
                                         f"quote with allow_purchase or adopt/receive stock")
            frame = bus.frame_from_specs(body.job_id, plan.order)
            frames[frame.frame_id] = frame
            return step_out(frame, bus.stage_next_cut(frame))

    @app.post("/api/quote", response_model=Quote)
    def quote(body: QuoteIn) -> Quote:
        with lock:
            return quote_job(bus, body.job_id, specs_of(body), margin_pct=body.margin_pct,
                             labor_per_cut=body.labor_per_cut)

    @app.get("/api/procurement/reorder", response_model=List[ReorderAlert])
    def reorder(window_days: float = 14.0, safety_days: float = 2.0) -> List[ReorderAlert]:
        with lock:
            return reorder_report(registry, window_days=window_days, safety_days=safety_days)

    @app.post("/api/procurement/order")
    def place_order(body: OrderStickIn) -> dict:
        with lock:
            return registry.place_order(body.profile, body.qty, body.eta_days)

    @app.post("/api/procurement/receive")
    def receive(body: OrderStickIn) -> dict:
        with lock:
            items = registry.receive(body.profile, body.qty, body.length_in)
            return {"received": [i.addr for i in items]}

    @app.get("/ledger/scrap")
    def scrap_ledger() -> dict:
        with lock:
            return {"total_lb": registry.scrap_total_lb(),
                    "entries": [e.model_dump() for e in registry.state.scrap_ledger]}

    return app


def main() -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("EXTBUS_HOST", "0.0.0.0"),
                port=int(os.environ.get("EXTBUS_PORT", "8000")))
