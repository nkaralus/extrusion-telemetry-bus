"""ESC/POS label printing: render, transport, and a durable outbox worker.

Pipeline (why labels are never lost and never ghosts)::

    bus.commit()  --(same durable transaction)-->  remnant racked  +  print_queue job
    registry commit lands  --(after_commit hook)-->  PrinterService.wake()   (microseconds)
    worker thread  --> transport.send(bytes)  --> mark job sent        (retry w/ backoff if offline)

* The job is written in the SAME transaction as the remnant, so "remnant exists" and "sticker
  queued" cannot disagree, and a printer that is off, out of paper or unplugged just leaves the job
  pending; it prints when the printer returns, even across a server restart.
* Delivery is at-least-once: a crash between sending and marking sent reprints that one sticker
  (a duplicate is better than a missing label on a rack).

Label (default ``raster``): 2" x 1" at 203 dpi = 384 x 203 dots; QR on the left, bin / address /
square length / profile / end conditions on the right, sent as one ``GS v 0`` bitmap.  ``native``
style falls back to the printer's own QR command and fonts (stacked layout).

Targets: ``net:host[:port]`` (raw TCP, usually 9100), ``file:/dev/usb/lp0`` (USB printer class on
Linux, or a serial device), ``memory`` (tests / dry run).  Direct libusb access is not implemented.
"""
from __future__ import annotations

import base64
import os
import socket
import threading
import time
from typing import Callable, List, Optional, Tuple

import segno
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import labels
from .physics import fmt_inches
from .registry import Profile, Registry, StockItem

ESC, GS = b"\x1b", b"\x1d"
FEED_MODES = {"ff": b"\x0c", "gs_ff": b"\x1d\x0c", "none": b""}


class PrinterError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
class LabelSpec:
    """2" x 1" label on a 58 mm head: 384 printable dots wide, 203 dots tall at 203 dpi."""
    def __init__(self, width_dots: int = 384, height_dots: int = 203, quiet_modules: int = 2) -> None:
        if width_dots % 8:
            raise ValueError("width_dots must be a multiple of 8")
        self.width_dots, self.height_dots, self.quiet = width_dots, height_dots, quiet_modules


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int, size: int, min_size: int = 11):
    """Shrink to fit; if it still cannot fit at ``min_size`` truncate with '..' (never clip silently)."""
    while size > min_size and draw.textlength(text, font=_font(size)) > max_w:
        size -= 1
    font = _font(size)
    while len(text) > 3 and draw.textlength(text, font=font) > max_w:
        text = text[:-3].rstrip() + ".."
    return font, text


def end_short(end) -> str:
    """'SQ' | '45\u00b0S' (long point on spline) | '45\u00b0B' (long point on back)."""
    if not end.is_miter:
        return "SQ"
    return f"{end.miter_deg:g}\u00b0{'S' if end.long_point_face.value == 'spline_face' else 'B'}"


def label_lines(item: StockItem, profile: Profile) -> List[Tuple[str, int]]:
    """(text, preferred font size) top to bottom."""
    return [
        (item.bin_location or "UNBINNED", 34),
        (item.addr, 28),
        (f"SQ {fmt_inches(item.guaranteed_square_in)}", 28),
        (f"LONG {fmt_inches(item.long_point_in)}", 20),
        (profile.part_number, 19),
        (f"A:{end_short(item.end_a)}  B:{end_short(item.end_b)}", 19),
    ]


def render_label_image(item: StockItem, profile: Profile, spec: Optional[LabelSpec] = None) -> Image.Image:
    spec = spec or LabelSpec()
    img = Image.new("1", (spec.width_dots, spec.height_dots), 1)           # 1 = white
    draw = ImageDraw.Draw(img)
    qr = segno.make(labels.qr_payload(item), error="m", micro=False)
    modules = qr.symbol_size(border=spec.quiet)[0]
    scale = max(1, (spec.height_dots - 6) // modules)
    mat = [[bool(c) for c in row] for row in qr.matrix_iter(scale=1, border=spec.quiet)]
    qy = (spec.height_dots - modules * scale) // 2
    for y, row in enumerate(mat):
        for x, dark in enumerate(row):
            if dark:
                draw.rectangle([2 + x * scale, qy + y * scale, 2 + (x + 1) * scale - 1, qy + (y + 1) * scale - 1], fill=0)
    x0 = 2 + modules * scale + 8
    y = 4
    for text, size in label_lines(item, profile):
        font, text = _fit(draw, text, spec.width_dots - x0 - 2, size)
        draw.text((x0, y), text, font=font, fill=0)
        y += int(font.size * 1.12) + 1
    return img


def raster_command(img: Image.Image) -> bytes:
    """``GS v 0``: bit 1 = black, rows MSB first, width padded to a byte."""
    img = img.convert("1")
    w, h = img.size
    wb = (w + 7) // 8
    data = ImageOps.invert(img.convert("L")).convert("1").tobytes()        # tobytes(): 1 = white -> invert -> 1 = black
    if len(data) != wb * h:
        raise PrinterError("unexpected raster size")
    return GS + b"v0\x00" + wb.to_bytes(2, "little") + h.to_bytes(2, "little") + data


def render_escpos_label(item: StockItem, profile: Profile, *, style: str = "raster", feed: str = "ff",
                        cut: bool = False, spec: Optional[LabelSpec] = None) -> bytes:
    if style == "native":
        lines = [item.addr, profile.part_number, f"SQ {fmt_inches(item.guaranteed_square_in)}  LONG {fmt_inches(item.long_point_in)}",
                 f"A: {item.end_a.describe()}", f"B: {item.end_b.describe()}"]
        body = labels.render_escpos(item.bin_location or "UNBINNED", lines, labels.qr_payload(item))
        return body
    if style != "raster":
        raise ValueError(f"unknown label style {style!r}")
    if feed.startswith("lines:"):
        feed_cmd = ESC + b"d" + bytes([max(0, min(255, int(feed.split(":", 1)[1])))])
    elif feed in FEED_MODES:
        feed_cmd = FEED_MODES[feed]
    else:
        raise ValueError(f"unknown feed mode {feed!r}")
    out = ESC + b"@" + raster_command(render_label_image(item, profile, spec)) + feed_cmd
    return out + (GS + b"V\x42\x00" if cut else b"")


def label_payload_b64(item: StockItem, profile: Profile) -> Tuple[str, str]:
    style = os.environ.get("EXTBUS_LABEL_STYLE", "raster")
    feed = os.environ.get("EXTBUS_PRINTER_FEED", "ff")
    data = render_escpos_label(item, profile, style=style, feed=feed, cut=os.environ.get("EXTBUS_PRINTER_CUT") == "1")
    return item.bin_location or "UNBINNED", base64.b64encode(data).decode("ascii")


def decode_raster(data: bytes) -> Image.Image:
    """Inverse of ``raster_command`` (used by tests and the printer-test CLI preview)."""
    i = data.index(GS + b"v0\x00")
    wb = int.from_bytes(data[i + 4:i + 6], "little")
    h = int.from_bytes(data[i + 6:i + 8], "little")
    body = data[i + 8:i + 8 + wb * h]
    return ImageOps.invert(Image.frombytes("1", (wb * 8, h), body).convert("L")).convert("1")


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
class MemoryTransport:
    def __init__(self) -> None:
        self.sent: List[Tuple[float, bytes]] = []
        self.fail_next = 0
        self.fail_exc: Exception = OSError("printer offline")

    def describe(self) -> str:
        return "memory"

    def send(self, data: bytes) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise self.fail_exc
        self.sent.append((time.time(), data))


class NetworkTransport:
    """Raw TCP (JetDirect-style 9100).  One short connection per job, so a power-cycled printer just reconnects."""
    def __init__(self, host: str, port: int = 9100, timeout: float = 3.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout

    def describe(self) -> str:
        return f"net:{self.host}:{self.port}"

    def send(self, data: bytes) -> None:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)


class FileTransport:
    """A device node: ``/dev/usb/lp0`` (Linux usblp) or a serial port.  Opened per job."""
    def __init__(self, path: str) -> None:
        self.path = path

    def describe(self) -> str:
        return f"file:{self.path}"

    def send(self, data: bytes) -> None:
        with open(self.path, "wb", buffering=0) as fh:
            fh.write(data)
            fh.flush()


def parse_target(spec: str):
    spec = spec.strip()
    if spec == "memory":
        return MemoryTransport()
    kind, _, rest = spec.partition(":")
    if kind in ("net", "tcp") and rest:
        rest = rest.removeprefix("//")
        host, _, port = rest.partition(":")
        return NetworkTransport(host, int(port) if port else 9100)
    if kind in ("file", "usb") and rest:
        return FileTransport(rest)
    raise PrinterError(f"bad printer target {spec!r} (use net:host[:port] | file:/dev/usb/lp0 | memory)")


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
class PrinterService:
    def __init__(self, registry: Registry, transport, *, max_attempts: int = 8, base_delay: float = 1.0,
                 poll: float = 1.0, on_event: Optional[Callable[[dict], None]] = None) -> None:
        self.reg, self.transport = registry, transport
        self.max_attempts, self.base_delay, self.poll, self.on_event = max_attempts, base_delay, poll, on_event
        self._wake, self._stop = threading.Event(), threading.Event()
        self._drain = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.sent_total = 0
        self.last_error = ""
        self.last_latency_ms: Optional[float] = None

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> "PrinterService":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="extbus-printer", daemon=True)
            self.reg.expedite_prints()                   # labels queued before this boot: try now, no backoff
            self._thread.start()
            self.wake()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.poll)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.drain()
            except Exception as exc:                     # the daemon must outlive any single failure
                self.last_error = f"worker: {exc}"

    def drain(self) -> int:
        """Send every due job once.  Returns how many were delivered."""
        n = 0
        with self._drain:
            for job in self.reg.due_prints():
                try:
                    self.transport.send(base64.b64decode(job["payload_b64"]))
                except Exception as exc:                 # OSError, timeouts, anything a transport raises
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.reg.mark_print_failed(job["id"], self.last_error, self.max_attempts, self.base_delay)
                    self._emit({"type": "print", "addr": job["addr"], "status": "retry", "error": self.last_error})
                    continue
                self.reg.mark_print_sent(job["id"])
                self.sent_total += 1
                self.last_latency_ms = round((self.reg.clock() - job["created_ts"]) * 1000, 2)
                self._emit({"type": "print", "addr": job["addr"], "status": "sent", "latency_ms": self.last_latency_ms})
                n += 1
        return n

    def _emit(self, ev: dict) -> None:
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def status(self) -> dict:
        q = self.reg.state.print_queue
        return {"configured": True, "target": self.transport.describe(),
                "pending": sum(j["status"] == "pending" for j in q), "failed": sum(j["status"] == "failed" for j in q),
                "sent_total": self.sent_total, "last_error": self.last_error,
                "last_latency_ms": self.last_latency_ms, "running": bool(self._thread and self._thread.is_alive())}


def printer_from_env(registry: Registry, on_event=None) -> Optional[PrinterService]:
    target = os.environ.get("EXTBUS_PRINTER", "").strip()
    return PrinterService(registry, parse_target(target), on_event=on_event) if target else None
