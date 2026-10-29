"""Live saw telemetry: a per-station WebSocket hub and the message protocol behind ``/ws/saw/{station}``.

Protocol (JSON text frames)::

    client -> server
      {"type":"sync"}                              resend current state
      {"type":"attach","frame_id":"ab12cd34"}      bind a frame to this station (persisted)
      {"type":"stage"}                             stage the next cut (reserves the source)
      {"type":"key","key":"1"|"2"|"3"|"4"}         macro pad: 1 CUT_OK, 2 FLIP_MITER, 3 MISCUT_NAK, 4 PIECE_MISSING
      {"type":"key","key":"3","actual_length_in":57.5}
      {"type":"ping"}
    server -> clients on that station
      {"type":"state", ...}   frame, staged step (the next drop to pull), blade setup, gate verdicts
      {"type":"event", "event":"CUT_OK"|..., "commit":{...}, "elapsed_ms":n}
      {"type":"print", "addr":..., "status":"sent"|"retry"}
      {"type":"error", "code":..., "message":...}     (to the sender only)

Every broadcast carries ``seq`` (per station, monotonic) and ``ts``, so a client can spot a gap and
send ``sync``.  A new connection always receives the current state first, so a reloaded tablet or a
second wall display is correct immediately.

Blade convention: the long point of a mitered cut on the spline face is reported as ``swing: "left"``
and on the back face as ``"right"``.  That mapping depends on how your saw is set up; invert it with
``EXTBUS_SWING_SPLINE=right``.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Optional, Set, Tuple

from .bus import FabricationBus, GateRejected
from .gates import _plan_for
from .mule_frame import Frame, FramePhase
from .physics import Face

KEYS = {"1": "CUT_OK", "cut_ok": "CUT_OK", "2": "FLIP_MITER", "flip": "FLIP_MITER",
        "3": "MISCUT_NAK", "miscut": "MISCUT_NAK", "4": "PIECE_MISSING", "missing": "PIECE_MISSING"}
SEND_TIMEOUT_S = 2.0
MAX_MESSAGE_BYTES = 4096


class StationHub:
    """Connections per station.  ``broadcast`` is async; ``publish`` is the thread-safe entry for sync code."""

    def __init__(self) -> None:
        self._conns: Dict[str, Set] = {}
        self._seq: Dict[str, int] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def listeners(self, station: str) -> int:
        return len(self._conns.get(station, ()))

    async def register(self, station: str, ws) -> None:
        self.loop = asyncio.get_running_loop()
        self._conns.setdefault(station, set()).add(ws)

    async def unregister(self, station: str, ws) -> None:
        self._conns.get(station, set()).discard(ws)

    async def broadcast(self, station: str, payload: dict) -> None:
        seq = self._seq[station] = self._seq.get(station, 0) + 1
        msg = {**payload, "seq": seq, "ts": round(time.time(), 3), "station": station}
        for ws in list(self._conns.get(station, ())):
            try:
                await asyncio.wait_for(ws.send_json(msg), SEND_TIMEOUT_S)
            except Exception:                                 # slow or dead client: drop it, never stall the bench
                self._conns[station].discard(ws)

    def publish(self, station: Optional[str], payload: dict) -> None:
        """From a worker thread (HTTP handler, printer): schedule onto the event loop."""
        if station and self.loop is not None and self.loop.is_running() and self.listeners(station):
            asyncio.run_coroutine_threadsafe(self.broadcast(station, payload), self.loop)

    def publish_all(self, payload: dict) -> None:
        for station in list(self._conns):
            self.publish(station, payload)


# --------------------------------------------------------------------------- #
def swing_for(face: Face) -> str:
    if face == Face.UNIFORM:
        return "none"
    spline_left = os.environ.get("EXTBUS_SWING_SPLINE", "left") != "right"
    return ("left" if spline_left else "right") if face == Face.SPLINE else ("right" if spline_left else "left")


def blade_for(bus: FabricationBus, frame: Frame) -> Optional[dict]:
    """What the operator sets on the saw for the staged cut, and what the cut should leave behind."""
    cur = frame.current
    if cur is None:
        return None
    item = bus.reg.state.stock.get(cur.source_addr)
    spec = cur.spec
    out = {"angle_deg": spec.miter_deg, "swing": swing_for(spec.face), "fence_in": spec.length_in,
           "prep": {"mode": cur.lead_mode, "angle_deg": spec.lead_miter_deg, "trim_in": 0.0},
           "source": {"addr": cur.source_addr, "bin": cur.source_bin, "kind": cur.source_kind,
                      "long_in": item.long_point_in if item else None},
           "expected_drop_in": None, "expected_square_in": None}
    if item is not None:
        plan = _plan_for(bus.reg, item, spec, bus.kerf)
        out["prep"]["trim_in"] = plan.offset_in
        out["expected_drop_in"], out["expected_square_in"] = plan.leftover_long_in, plan.leftover_guaranteed_in
    return out


def saw_state(bus: FabricationBus, station: str, frame: Optional[Frame]) -> dict:
    if frame is None:
        return {"type": "state", "frame": None, "step": None, "guard": None, "blade": None, "dlq": None}
    return {
        "type": "state",
        "frame": {"frame_id": frame.frame_id, "job_id": frame.job_id, "phase": frame.phase.value,
                  "slot": frame.slot.value if frame.slot else None, "pending": len(frame.pending),
                  "note": frame.note},
        "step": frame.current.model_dump(mode="json") if frame.current else None,
        "guard": [v.model_dump(mode="json") for v in frame.guard.verdicts] if frame.guard and frame.current else None,
        "blade": blade_for(bus, frame),
        "dlq": frame.dlq.model_dump(mode="json") if frame.dlq else None,
    }


def frame_for_station(bus: FabricationBus, station: str) -> Optional[Frame]:
    mine = [f for f in bus.frames.values() if f.station == station and f.phase in (FramePhase.ACTIVE, FramePhase.DLQ)]
    return mine[-1] if mine else None


def event_from_ack(ack, result: str) -> dict:
    ev = {"type": "event", "event": result, "elapsed_ms": ack.elapsed_ms}
    if ack.commit is not None:
        c = ack.commit
        ev["commit"] = {"outcome": c.outcome, "screen": c.screen, "source_addr": c.source_addr,
                        "disposition": c.disposition.value, "leftover_addr": c.leftover_addr,
                        "leftover_bin": c.leftover_bin, "scrap_lb": c.scrap_lb, "scrap_reason": c.scrap_reason,
                        "label_lines": c.label.lines if c.label else None}
    return ev


def _err(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message}


def handle(bus: FabricationBus, station: str, msg: dict) -> Tuple[List[dict], List[dict]]:
    """Apply one client message.  Returns (replies to the sender only, broadcasts to the whole station).

    Synchronous: the server runs it in a worker thread under the bus lock.
    """
    t0 = time.perf_counter()
    kind = msg.get("type")
    frame = frame_for_station(bus, station)

    def state() -> dict:
        return saw_state(bus, station, frame_for_station(bus, station))

    if kind == "ping":
        return [{"type": "pong"}], []
    if kind == "sync":
        return [], [state()]
    if kind == "attach":
        f = bus.frames.get(str(msg.get("frame_id", "")))
        if f is None:
            return [_err("UNKNOWN_FRAME", f"no frame {msg.get('frame_id')!r}")], []
        if f.phase != FramePhase.ACTIVE:
            return [_err("FRAME_NOT_ACTIVE", f"frame is {f.phase.value}")], []
        with bus._atomic(f):
            for other in bus.frames.values():                    # one frame per station
                if other is not f and other.station == station:
                    other.station = None
                    bus._save(other)
            f.station = station
        return [], [state()]
    if frame is None:
        return [_err("NO_FRAME", "attach a frame to this station first")], []

    try:
        if kind == "stage":
            if frame.current is None:
                bus.stage_next_cut(frame, station)
            return [], [state()]
        if kind == "key":
            action = KEYS.get(str(msg.get("key", "")).lower())
            if action is None:
                return [_err("BAD_KEY", f"unknown key {msg.get('key')!r} (use 1, 2, 3 or 4)")], []
            if frame.current is None:
                return [_err("NOTHING_STAGED", "no cut is staged; send stage first")], []
            if action == "FLIP_MITER":
                bus.flip_current(frame, station)
                ev = {"type": "event", "event": "FLIP_MITER", "elapsed_ms": round((time.perf_counter() - t0) * 1000, 3)}
            else:
                actual = msg.get("actual_length_in")
                if actual is not None and not isinstance(actual, (int, float)):
                    return [_err("BAD_LENGTH", "actual_length_in must be a number")], []
                ack = bus.ack(frame, action, actual if action == "MISCUT_NAK" else None, station)
                ev = event_from_ack(ack, action)
            return [], [ev, state()]
    except GateRejected as exc:
        return [_err("GATE_REJECTED", str(exc))], [state()]
    except ValueError as exc:                                    # e.g. flipping a square cut
        return [_err("INVALID", str(exc))], []
    except RuntimeError as exc:
        return [_err("CONFLICT", str(exc))], []
    return [_err("UNKNOWN_TYPE", f"unknown message type {kind!r}")], []
