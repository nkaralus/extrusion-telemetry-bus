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
from typing import Any, Dict, List, Literal, Optional, Union

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .bus import AckResult, AdoptResult, CommitResult, FabricationBus, GateRejected
from .gates import CutProposal, GateReport
from .labels import Label, build_label
from .media import MAX_BYTES, MediaError, inspect_image, safe_job_id, save_photo
from .llm import ChainParser, OllamaParser, PlanParser, RuleParser
from .mule_frame import CutLine, Frame, Slot2Plan, Slot3Execute, Slot4Guard
from .nesting import NestPlan, compile_cut_list
from .physics import CutSpec, parse_end_condition, parse_inches
from .parametric import POST_PROFILE, RAFTER_PROFILE, ParametricError, ParametricModel, solve_envelope
from .procurement import ReorderAlert, reorder_report
from .quoting import Quote, quote_job
from .sales_gates import dims_valid, evaluate_intake
from .vision import VisionAnalyzer
from .sales_intake import (
    EngineeringReadinessReport, PhotoAnalysis, PhotoAnalyzer, PhotoKind, PhotoTags, SalesIntakePackage,
    SitePhoto, apply_answers,
)
from .registry import RACK_FULL_RATIO, Registry, RegistryError, StockKind

STATIC_DIR = Path(__file__).parent / "static"
DIM_KEYS = ("width_in", "proj_in", "house_h_in", "front_h_in")


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


class AnswerIn(BaseModel):
    job_id: str
    answers: Dict[str, Any]


class GenerateBomIn(BaseModel):
    job_id: str
    package: Optional[SalesIntakePackage] = None   # omit to use the stored intake for job_id
    margin_pct: float = 25.0
    labor_per_cut: float = 0.0
    max_oc_in: float = 48.0


class GenerateBomOut(BaseModel):
    report: EngineeringReadinessReport
    parametric: ParametricModel
    quote: Quote


class PhotoView(BaseModel):
    kind: PhotoKind
    url: str
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    sharpness: Optional[float] = None
    note: str = ""
    bytes: Optional[int] = None
    tags: List[str] = []
    backend: str = "none"
    substrate: Optional[str] = None
    substrate_confidence: float = 0.0
    obstacles: List[dict] = []


class JobSummary(BaseModel):
    job_id: str
    customer_name: str
    ready: bool
    confidence_score: float
    blockers: int
    photos: int
    released: bool
    ts: float


class JobDetail(BaseModel):
    job_id: str
    customer_name: str
    package: SalesIntakePackage
    report: EngineeringReadinessReport
    photos: List[PhotoView]
    analysis: PhotoAnalysis
    released: Optional[dict] = None
    revisions: List[dict] = []
    ts: float = 0.0


class UploadOut(BaseModel):
    photo: PhotoView
    report: EngineeringReadinessReport


class PreviewIn(BaseModel):
    job_id: str
    overrides: Dict[str, Any] = {}
    max_oc_in: float = 48.0
    margin_pct: float = 25.0


class PreviewOut(BaseModel):
    report: EngineeringReadinessReport
    package: SalesIntakePackage
    parametric: Optional[ParametricModel] = None
    quote: Optional[Quote] = None
    error: Optional[str] = None


class ReleaseIn(BaseModel):
    job_id: str
    overrides: Dict[str, Any] = {}
    max_oc_in: float = 48.0


class ReleaseOut(BaseModel):
    job_id: str
    cart_addr: str
    frame_id: str
    plan: NestPlan
    report: EngineeringReadinessReport


def default_parser(registry: Registry) -> PlanParser:
    rules = RuleParser(registry)
    mode = os.environ.get("EXTBUS_PARSER", "auto")
    if mode == "rules":
        return rules
    ollama = OllamaParser(registry, model=os.environ.get("EXTBUS_MODEL", "qwen2.5:3b"),
                          host=os.environ.get("EXTBUS_OLLAMA", "http://127.0.0.1:11434"))
    return ollama if mode == "ollama" else ChainParser(ollama, rules)


def create_app(registry: Optional[Registry] = None, parser: Optional[PlanParser] = None,
               analyzer: Optional[PhotoAnalyzer] = None, media_dir: Optional[os.PathLike] = None) -> FastAPI:
    if registry is None:
        registry = Registry.load(os.environ.get("EXTBUS_STATE", "state_registry.json"))
    bus = FabricationBus(registry, parser=parser or default_parser(registry))
    frames: Dict[str, Frame] = {}
    lock = threading.RLock()  # one writer at a time: deterministic closed loop
    media_root = Path(media_dir or os.environ.get("EXTBUS_MEDIA", "media")).resolve()
    media_root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="extrusion-telemetry-bus", version="0.1.0")
    app.state.bus, app.state.frames, app.state.media_dir = bus, frames, media_root

    @app.middleware("http")
    async def _nosniff(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

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

    # ---------------- Phase 3: sales intake ---------------- #
    photo_analyzer: PhotoAnalyzer = analyzer or VisionAnalyzer(media_root)

    def assess(pkg: SalesIntakePackage) -> EngineeringReadinessReport:
        report = evaluate_intake(pkg, photo_analyzer.analyze(pkg))
        registry.save_intake(pkg.job_id, pkg.model_dump(mode="json"), report.model_dump(mode="json"))
        return report

    def stored_package(job_id: str) -> SalesIntakePackage:
        rec = registry.state.intakes.get(job_id)
        if rec is None:
            raise HTTPException(404, f"no intake on file for {job_id}")
        return SalesIntakePackage.model_validate(rec["package"])

    @app.post("/api/sales/intake", response_model=EngineeringReadinessReport)
    def sales_intake(pkg: SalesIntakePackage) -> EngineeringReadinessReport:
        """Run GE_1..GE_3 now, while the rep is still on site."""
        with lock:
            return assess(pkg)

    @app.post("/api/sales/intake/answer", response_model=EngineeringReadinessReport)
    def sales_answer(body: AnswerIn) -> EngineeringReadinessReport:
        """Merge the rep's answers to prompt cards into the stored package and re-run the gates."""
        with lock:
            try:
                return assess(apply_answers(stored_package(body.job_id), body.answers))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.post("/api/sales/generate-bom", response_model=GenerateBomOut)
    def sales_generate_bom(body: GenerateBomIn) -> GenerateBomOut:
        with lock:
            pkg = body.package or stored_package(body.job_id)
            report = assess(pkg)
            if not report.is_ready_for_engineering:
                raise HTTPException(409, detail=report.model_dump(mode="json"))
            try:
                model = solve_envelope(
                    pkg.width_in, pkg.proj_in, pkg.house_h_in, pkg.front_h_in, max_oc_in=body.max_oc_in,
                    post_w_in=registry.profile(POST_PROFILE).width_in,
                    rafter_width_in=registry.profile(RAFTER_PROFILE).width_in)
                specs = Slot2Plan(cuts=model.cuts).expand()
                q = quote_job(bus, pkg.job_id, specs, margin_pct=body.margin_pct,
                              labor_per_cut=body.labor_per_cut)
            except (ParametricError, RegistryError, ValueError, KeyError) as exc:
                raise HTTPException(422, str(exc)) from exc
            return GenerateBomOut(report=report, parametric=model, quote=q)

    # ---------------- Phase 4: media, viewports, release ---------------- #
    def photo_views(pkg: SalesIntakePackage, analysis: PhotoAnalysis) -> List[PhotoView]:
        tags = {t.kind: t for t in analysis.photo_tags}
        out = []
        for p in pkg.site_photos:
            t = tags.get(p.kind)
            out.append(PhotoView(
                kind=p.kind, url=f"/media/{p.path}", width_px=p.width_px, height_px=p.height_px,
                sharpness=p.sharpness, note=p.note, bytes=p.bytes,
                tags=t.tags if t else [], backend=t.backend if t else "none",
                substrate=t.substrate.value if t and t.substrate else None,
                substrate_confidence=t.substrate_confidence if t else 0.0,
                obstacles=[o.model_dump(mode="json") for o in (t.obstacles if t else [])]))
        return out

    @app.post("/api/sales/upload-photo", response_model=UploadOut)
    def upload_photo(job_id: str = Form(...), kind: str = Form(...), file: UploadFile = File(...)) -> UploadOut:
        """Multipart photo upload: wide_context | tie_in_closeup | sketch."""
        try:
            jid = safe_job_id(job_id)
            pk = PhotoKind(kind)
        except (MediaError, ValueError) as exc:
            raise HTTPException(422, f"bad job_id or kind: {exc}") from exc
        data = file.file.read(MAX_BYTES + 1)
        with lock:
            try:
                info = inspect_image(data)
            except MediaError as exc:
                raise HTTPException(exc.status, str(exc)) from exc
            rel = save_photo(media_root, jid, pk.value, data, info.ext)
            name = "".join(c for c in Path(file.filename or "").name if c.isprintable())[:80]
            photo = SitePhoto(kind=pk, path=rel, width_px=info.width_px, height_px=info.height_px,
                              sharpness=info.sharpness, note=name, bytes=info.bytes, sha256=info.sha256,
                              content_type=info.content_type)
            rec = registry.state.intakes.get(jid)
            pkg = SalesIntakePackage.model_validate(rec["package"]) if rec else SalesIntakePackage(job_id=jid)
            pkg = pkg.model_copy(update={"site_photos": [p for p in pkg.site_photos if p.kind != pk] + [photo]})
            report = assess(pkg)
            view = next(v for v in photo_views(pkg, photo_analyzer.analyze(pkg)) if v.kind == pk)
            return UploadOut(photo=view, report=report)

    def detail(job_id: str) -> JobDetail:
        rec = registry.state.intakes.get(job_id)
        if rec is None:
            raise HTTPException(404, f"no intake on file for {job_id}")
        pkg = SalesIntakePackage.model_validate(rec["package"])
        analysis = photo_analyzer.analyze(pkg)
        return JobDetail(job_id=job_id, customer_name=pkg.customer_name, package=pkg,
                         report=evaluate_intake(pkg, analysis), photos=photo_views(pkg, analysis),
                         analysis=analysis, released=rec.get("released"), revisions=rec.get("revisions", []),
                         ts=rec.get("ts", 0.0))

    @app.get("/api/sales/jobs", response_model=List[JobSummary])
    def list_jobs() -> List[JobSummary]:
        with lock:
            out = []
            for jid, rec in registry.state.intakes.items():
                r = rec["report"]
                out.append(JobSummary(
                    job_id=jid, customer_name=rec["package"].get("customer_name", ""),
                    ready=r["is_ready_for_engineering"], confidence_score=r["confidence_score"],
                    blockers=sum(m["severity"] == "critical_blocker" for m in r["missing_items"]),
                    photos=len(rec["package"].get("site_photos", [])), released="released" in rec, ts=rec["ts"]))
            return sorted(out, key=lambda j: -j.ts)

    @app.get("/api/sales/jobs/{job_id}", response_model=JobDetail)
    def job_detail(job_id: str, create: bool = False) -> JobDetail:
        """``create=true`` makes an empty intake on first visit (the sales page uses it)."""
        with lock:
            if create and job_id not in registry.state.intakes:
                try:
                    safe_job_id(job_id)
                except MediaError as exc:
                    raise HTTPException(422, str(exc)) from exc
                assess(SalesIntakePackage(job_id=job_id))
            return detail(job_id)

    def solve_for(pkg: SalesIntakePackage, max_oc: float) -> ParametricModel:
        return solve_envelope(pkg.width_in, pkg.proj_in, pkg.house_h_in, pkg.front_h_in, max_oc_in=max_oc,
                              post_w_in=registry.profile(POST_PROFILE).width_in,
                              rafter_width_in=registry.profile(RAFTER_PROFILE).width_in)

    def with_overrides(pkg: SalesIntakePackage, overrides: Dict[str, Any]) -> SalesIntakePackage:
        bad = [k for k in overrides if k not in DIM_KEYS]
        if bad:
            raise HTTPException(422, f"engineering may override only {', '.join(DIM_KEYS)} (got {', '.join(bad)})")
        try:
            return apply_answers(pkg, overrides) if overrides else pkg
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/engineering/preview", response_model=PreviewOut)
    def eng_preview(body: PreviewIn) -> PreviewOut:
        """Recalculate wireframe + BOM + quote for hypothetical dimensions.  Persists nothing."""
        with lock:
            pkg = with_overrides(stored_package(body.job_id), body.overrides)
            report = evaluate_intake(pkg, photo_analyzer.analyze(pkg))
            if not dims_valid(pkg):
                return PreviewOut(report=report, package=pkg, error="dimensions missing or implausible")
            try:
                model = solve_for(pkg, body.max_oc_in)
                q = quote_job(bus, pkg.job_id, Slot2Plan(cuts=model.cuts).expand(), margin_pct=body.margin_pct)
            except (ParametricError, RegistryError, ValueError, KeyError) as exc:
                return PreviewOut(report=report, package=pkg, error=str(exc))
            return PreviewOut(report=report, package=pkg, parametric=model, quote=q)

    @app.post("/api/engineering/release", response_model=ReleaseOut)
    def eng_release(body: ReleaseIn) -> ReleaseOut:
        """Verified BOM -> nest compiler -> job cart (10.4.1.x) + executable frame for the saw bench."""
        with lock:
            rec = registry.state.intakes.get(body.job_id)
            if rec is None:
                raise HTTPException(404, f"no intake on file for {body.job_id}")
            live = next((f for f in frames.values() if f.job_id == body.job_id and f.phase.value == "active"), None)
            if "released" in rec and live is not None:
                raise HTTPException(409, f"{body.job_id} is already released (frame {live.frame_id})")
            pkg = with_overrides(stored_package(body.job_id), body.overrides)
            report = assess(pkg)
            if not report.is_ready_for_engineering:
                raise HTTPException(409, detail=report.model_dump(mode="json"))
            try:
                model = solve_for(pkg, body.max_oc_in)
                specs = Slot2Plan(cuts=model.cuts).expand()
            except (ParametricError, RegistryError, ValueError, KeyError) as exc:
                raise HTTPException(422, str(exc)) from exc
            plan = compile_cut_list(bus, body.job_id, specs)
            if plan.unmet:
                buy = compile_cut_list(bus, body.job_id, specs, allow_purchase=True)
                raise HTTPException(409, detail={
                    "message": f"{len(plan.unmet)} piece(s) cannot be cut from current stock",
                    "purchase_sticks": buy.purchased_sticks})
            frame = bus.frame_from_specs(body.job_id, plan.order)     # also opens the job cart
            frames[frame.frame_id] = frame
            cart = registry.state.carts[body.job_id]
            with registry.transaction():
                r = registry.state.intakes[body.job_id]
                r["released"] = {"ts": registry.clock(), "cart_addr": cart.addr, "frame_id": frame.frame_id,
                                 "pieces": plan.pieces, "virgin_pulls": plan.virgin_pulls,
                                 "drops_used": plan.drops_used}
                r.setdefault("revisions", []).append({"ts": registry.clock(), "overrides": body.overrides,
                                                      "max_oc_in": body.max_oc_in})
                registry.log("RELEASED", job_id=body.job_id, cart=cart.addr, frame=frame.frame_id)
            return ReleaseOut(job_id=body.job_id, cart_addr=cart.addr, frame_id=frame.frame_id,
                              plan=plan, report=report)

    @app.get("/api/frames")
    def list_frames() -> List[dict]:
        with lock:
            return [{"frame_id": f.frame_id, "job_id": f.job_id, "phase": f.phase.value,
                     "pending": len(f.pending), "current": f.current.model_dump(mode="json") if f.current else None}
                    for f in frames.values()]

    @app.post("/order/{fid}/flip", response_model=StepOut)
    def order_flip(fid: str, station: Optional[str] = None) -> StepOut:
        with lock:
            frame = frame_or_404(fid)
            try:
                return step_out(frame, bus.flip_current(frame, station))
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.get("/api/inventory")
    def inventory() -> dict:
        with lock:
            s = registry.state
            stock = [{"addr": i.addr, "kind": i.kind.value, "profile": i.profile, "long_point_in": i.long_point_in,
                      "guaranteed_square_in": i.guaranteed_square_in, "end_a": i.end_a.describe(),
                      "end_b": i.end_b.describe(), "bin_location": i.bin_location,
                      "allocated_job_id": i.allocated_job_id, "legacy": i.legacy}
                     for i in sorted(s.stock.values(), key=lambda i: (i.kind.value, i.profile, i.addr))]
            racks = []
            for pn in sorted(s.profiles):
                n, cap, ratio = registry.rack_utilization(pn)
                racks.append({"profile": pn, "racked": n, "slots": cap, "ratio": round(ratio, 3),
                              "full": ratio >= RACK_FULL_RATIO})
            return {"stock": stock, "racks": racks, "hopper_addr": s.hopper_addr,
                    "scrap_total_lb": registry.scrap_total_lb(), "unaccounted": s.unaccounted,
                    "on_order": s.on_order, "dlq": s.dlq[-20:],
                    "carts": [{"job_id": c.job_id, "addr": c.addr, "pieces": len(c.pieces)} for c in s.carts.values()]}

    # ---------------- static viewports ---------------- #
    for route, name in (("/", "index.html"), ("/sales", "sales.html"), ("/engineering", "engineering.html")):
        app.add_api_route(route, (lambda n=name: FileResponse(STATIC_DIR / n)), methods=["GET"],
                          include_in_schema=False)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/media", StaticFiles(directory=media_root), name="media")

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
