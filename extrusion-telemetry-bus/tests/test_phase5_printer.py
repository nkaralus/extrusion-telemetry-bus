import base64
import socket
import threading
import time

import numpy as np
import pytest
import segno

from extrusion_bus import labels, printer
from extrusion_bus import registry as R
from extrusion_bus.bus import Disposition, FabricationBus, GateRejected
from extrusion_bus.physics import End, Face
from extrusion_bus.printer import (
    FileTransport, MemoryTransport, NetworkTransport, PrinterError, PrinterService, decode_raster, parse_target,
    printer_from_env, raster_command, render_escpos_label, render_label_image,
)
from extrusion_bus.registry import Registry

from conftest import PN, make_proposal


class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t


def rig(tmp_path, transport=None, clock=None, **svc):
    reg = Registry.load(tmp_path / "state_registry.json", **({"clock": clock} if clock else {}))
    bus = FabricationBus(reg, print_labels=True)
    mem = transport or MemoryTransport()
    service = PrinterService(reg, mem, **{"poll": 0.05, **svc})
    bus.on_label_queued = service.wake
    return reg, bus, mem, service


def remnant(reg, long=233.875, a=None, b=None):
    return reg.add_remnant(PN, long, a or End.miter(45, Face.BACK), b or End.square())


# ============================ ESC/POS byte generation ============================ #
def test_raster_label_structure_is_exactly_escpos(reg):
    it = remnant(reg)
    data = render_escpos_label(it, reg.profile(PN))
    assert data[:2] == b"\x1b@"                                          # ESC @ init
    assert data[2:6] == b"\x1d\x76\x30\x00"                              # GS v 0 m=0 (normal density)
    xl, xh, yl, yh = data[6:10]
    assert (xl + xh * 256, yl + yh * 256) == (48, 203)                   # 384 dots = 48 bytes wide, 203 dots = 1"
    assert len(data) == 2 + 4 + 4 + 48 * 203 + 1 and data[-1:] == b"\x0c"  # + form feed to the next label gap


def test_raster_roundtrips_pixel_for_pixel(reg):
    it = remnant(reg)
    img = render_label_image(it, reg.profile(PN))
    assert img.size == (384, 203)
    back = decode_raster(render_escpos_label(it, reg.profile(PN)))
    assert np.array_equal(np.array(img.convert("L")), np.array(back.convert("L")))


def test_polarity_is_correct_in_the_raw_bytes_not_just_in_our_own_decoder(reg):
    """ESC/POS: bit 1 = print (black).  Read the stream directly so an inverted label cannot hide behind decode_raster."""
    it = remnant(reg)
    data = render_escpos_label(it, reg.profile(PN), feed="none")
    body, wb = data[10:], 48
    assert len(body) == wb * 203

    def bit(x, y):
        return (body[y * wb + x // 8] >> (7 - x % 8)) & 1

    assert bit(383, 0) == 0 and bit(383, 202) == 0 and bit(120, 0) == 0         # paper margin stays unprinted
    ref = [[bool(c) for c in row] for row in segno.make(labels.qr_payload(it), error="m", micro=False).matrix_iter(scale=1, border=2)]
    scale = (203 - 6) // len(ref)
    qy = (203 - len(ref) * scale) // 2
    for mx, my in ((2, 2), (len(ref) - 3, 2), (2, len(ref) - 3)):                  # the three finder-pattern corners are dark
        assert ref[my][mx] and bit(2 + mx * scale + scale // 2, qy + my * scale + scale // 2) == 1
    dark = sum(bin(b).count("1") for b in body)
    assert 0.05 < dark / (384 * 203) < 0.45                                        # a label, not a black or blank page


def test_qr_in_the_raster_is_the_exact_scannable_payload(reg):
    """Read the module grid back out of the printed bitmap and compare to the reference QR matrix."""
    it = remnant(reg)
    payload = labels.qr_payload(it)
    bitmap = np.array(decode_raster(render_escpos_label(it, reg.profile(PN))).convert("L")) < 128      # True = black
    ref = [[bool(c) for c in row] for row in segno.make(payload, error="m", micro=False).matrix_iter(scale=1, border=2)]
    n = len(ref)
    scale = (203 - 6) // n
    qy = (203 - n * scale) // 2
    for y in range(n):
        for x in range(n):
            cell = bitmap[qy + y * scale: qy + (y + 1) * scale, 2 + x * scale: 2 + (x + 1) * scale]
            assert cell.all() == ref[y][x] and cell.any() == ref[y][x], (x, y)
    assert payload == f"EXTBUS|{it.addr}|{it.uid}" and labels.parse_qr_payload(payload) == (it.addr, it.uid)


def test_label_stays_inside_the_2x1_and_never_clips_text(reg):
    wide = reg.add_remnant("EXT-HW-BOX", 74.25, End.miter(45, Face.SPLINE), End.miter(45, Face.BACK))
    for it in (remnant(reg), wide):
        a = np.array(render_label_image(it, reg.profile(it.profile)).convert("L")) < 128
        assert a.shape == (203, 384) and not a[:, 380:].any() and a[:, 150:].any()    # text present, margin clear
    lines = printer.label_lines(wide, reg.profile("EXT-HW-BOX"))
    assert lines[0][0] == wide.bin_location and lines[1][0] == wide.addr and lines[-1][0] == "A:45\u00b0S  B:45\u00b0B"


def test_feed_cut_and_style_options(reg):
    it, prof = remnant(reg), reg.profile(PN)
    assert render_escpos_label(it, prof, feed="gs_ff").endswith(b"\x1d\x0c")
    assert render_escpos_label(it, prof, feed="lines:3").endswith(b"\x1b\x64\x03")
    assert render_escpos_label(it, prof, feed="none").endswith(raster_command(render_label_image(it, prof)))
    assert render_escpos_label(it, prof, cut=True).endswith(b"\x0c\x1d\x56\x42\x00")
    native = render_escpos_label(it, prof, style="native")
    assert b"\x1d(k" in native and labels.qr_payload(it).encode() in native and b"\x1d\x76\x30" not in native
    for bad in ({"style": "zpl"}, {"feed": "sideways"}):
        with pytest.raises(ValueError):
            render_escpos_label(it, prof, **bad)


def test_payload_helper_honours_environment(reg, monkeypatch):
    it = remnant(reg)
    monkeypatch.setenv("EXTBUS_LABEL_STYLE", "native"); monkeypatch.setenv("EXTBUS_PRINTER_FEED", "none")
    title, b64 = printer.label_payload_b64(it, reg.profile(PN))
    assert title == it.bin_location and b"\x1d(k" in base64.b64decode(b64)


# ============================ transports ============================ #
def test_target_parsing():
    assert isinstance(parse_target("memory"), MemoryTransport)
    n = parse_target("net:192.168.1.50"); assert (n.host, n.port) == ("192.168.1.50", 9100)
    n = parse_target("tcp://10.0.0.9:9101"); assert (n.host, n.port) == ("10.0.0.9", 9101)
    assert parse_target("file:/dev/usb/lp0").path == "/dev/usb/lp0" and parse_target("usb:/dev/usb/lp1").path == "/dev/usb/lp1"
    for bad in ("", "ftp:x", "net:", "bogus"):
        with pytest.raises(PrinterError):
            parse_target(bad)


def test_network_transport_sends_the_exact_bytes_over_a_real_socket():
    got = []
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1); port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        buf = b""
        while chunk := c.recv(65536):
            buf += chunk
        got.append(buf); c.close()

    t = threading.Thread(target=serve, daemon=True); t.start()
    payload = bytes(range(256)) * 80
    NetworkTransport("127.0.0.1", port).send(payload)
    t.join(3); srv.close()
    assert got == [payload]
    with pytest.raises(OSError):
        NetworkTransport("127.0.0.1", port, timeout=0.3).send(b"x")      # nobody listening: a real connection error


def test_file_transport_writes_to_a_device_node(tmp_path):
    dev = tmp_path / "lp0"
    FileTransport(str(dev)).send(b"\x1b@hello")
    assert dev.read_bytes() == b"\x1b@hello"
    with pytest.raises(OSError):
        FileTransport(str(tmp_path / "missing" / "lp0")).send(b"x")


# ============================ outbox + worker ============================ #
def cut_remnant(reg, bus, length=58):
    raw = reg.add_raw(PN, 288)[0]
    return bus.commit(make_proposal(raw.addr, length_in=length))


def test_racking_a_remnant_queues_and_prints_the_label_immediately(tmp_path):
    reg, bus, mem, svc = rig(tmp_path)
    svc.start()
    try:
        t0 = time.time()
        res = cut_remnant(reg, bus)
        assert res.disposition == Disposition.REMNANT_RACK
        deadline = time.time() + 2
        while not mem.sent and time.time() < deadline:
            time.sleep(0.002)
        assert len(mem.sent) == 1
        assert mem.sent[0][0] - t0 < 0.5                                   # fired as the commit landed, not on a poll
        rem = reg.state.stock[res.leftover_addr]
        assert mem.sent[0][1] == render_escpos_label(rem, reg.profile(PN))  # the sticker for THAT drop
        job = reg.state.print_queue[0]
        assert job["status"] == "sent" and job["addr"] == rem.addr and job["attempts"] == 0
        assert svc.status()["sent_total"] == 1 and svc.last_latency_ms is not None and svc.last_latency_ms < 500
    finally:
        svc.stop()


def test_no_label_for_scrap_or_when_printing_is_off(tmp_path):
    reg, bus, mem, svc = rig(tmp_path)
    short = reg.add_remnant(PN, 60, End.square())
    res = bus.commit(make_proposal(short.addr, length_in=58))              # leaves 1.875": scrap bin, no sticker
    assert res.disposition == Disposition.SCRAP_BIN and reg.state.print_queue == []
    quiet = FabricationBus(reg)                                            # print_labels defaults to off
    quiet.commit(make_proposal(reg.add_raw(PN, 288)[0].addr, length_in=58))
    assert reg.state.print_queue == []


def test_adopted_drop_gets_a_sticker(tmp_path):
    reg, bus, mem, svc = rig(tmp_path)
    res = bus.adopt_drop(PN, "74 1/4", End.miter(45, Face.SPLINE))
    assert [j["addr"] for j in reg.state.print_queue] == [res.addr]
    assert svc.drain() == 1 and mem.sent[0][1] == base64.b64decode(reg.state.print_queue[0]["payload_b64"])


def test_job_commits_with_the_remnant_or_not_at_all(tmp_path, monkeypatch):
    reg, bus, mem, svc = rig(tmp_path)
    woke = []
    bus.on_label_queued = lambda: woke.append(1)
    raw = reg.add_raw(PN, 288)[0]
    monkeypatch.setattr(R.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        bus.commit(make_proposal(raw.addr, length_in=58))
    monkeypatch.undo()
    assert reg.state.print_queue == [] and woke == [] and raw.addr in reg.state.stock     # no ghost sticker
    with pytest.raises(GateRejected):
        bus.commit(make_proposal(raw.addr, length_in=500))
    assert reg.state.print_queue == []
    bus.commit(make_proposal(raw.addr, length_in=58))
    assert len(reg.state.print_queue) == 1 and woke == [1]


def test_one_live_job_per_remnant(tmp_path):
    reg, _, _, _ = rig(tmp_path)
    it = remnant(reg)
    a = reg.enqueue_print(it.addr, it.uid, "T", "AAAA")
    b = reg.enqueue_print(it.addr, it.uid, "T", "AAAA")
    assert a["id"] == b["id"] and len(reg.state.print_queue) == 1


def test_offline_printer_retries_with_backoff_then_delivers(tmp_path):
    clk = Clock()
    mem = MemoryTransport(); mem.fail_next = 2
    reg, bus, mem, svc = rig(tmp_path, mem, clock=clk, base_delay=1.0)
    cut_remnant(reg, bus)
    assert svc.drain() == 0
    job = reg.state.print_queue[0]
    assert job["status"] == "pending" and job["attempts"] == 1 and "printer offline" in job["last_error"]
    assert svc.drain() == 0 and job["attempts"] == 1                       # not due yet: backoff respected
    clk.t += 1.0
    assert svc.drain() == 0 and reg.state.print_queue[0]["attempts"] == 2
    clk.t += 1.9
    assert svc.drain() == 0                                                # second backoff is 2 s, only 1.9 passed
    clk.t += 0.2
    assert svc.drain() == 1 and reg.state.print_queue[0]["status"] == "sent" and len(mem.sent) == 1


def test_gives_up_after_max_attempts_and_can_be_retried(tmp_path):
    clk = Clock()
    mem = MemoryTransport(); mem.fail_next = 99
    reg, bus, mem, svc = rig(tmp_path, mem, clock=clk, max_attempts=3, base_delay=1.0)
    cut_remnant(reg, bus)
    for _ in range(3):
        svc.drain(); clk.t += 100
    j = reg.state.print_queue[0]
    assert j["status"] == "failed" and j["attempts"] == 3 and svc.status()["failed"] == 1
    assert svc.drain() == 0                                                # failed jobs are not retried silently
    mem.fail_next = 0
    assert reg.retry_failed_prints() == 1 and svc.drain() == 1 and reg.state.print_queue[0]["status"] == "sent"


def test_queued_labels_survive_a_restart_and_print_when_the_printer_returns(tmp_path):
    mem = MemoryTransport(); mem.fail_next = 99                            # printer is off
    reg, bus, mem, svc = rig(tmp_path, mem)
    res = cut_remnant(reg, bus); svc.drain()
    assert reg.state.print_queue[0]["status"] == "pending"
    with reg.transaction():                                                # many failures in: the backoff is now an hour long
        reg.state.print_queue[0]["next_try_ts"] = time.time() + 3600
    assert reg.state.print_queue[0]["next_try_ts"] > time.time() + 3000
    reg2 = Registry.load(tmp_path / "state_registry.json")                 # power cycle, printer now on
    good = MemoryTransport()
    svc2 = PrinterService(reg2, good, poll=0.05).start()                   # boot: no waiting out the old backoff
    try:
        deadline = time.time() + 3
        while not good.sent and time.time() < deadline:
            time.sleep(0.01)
    finally:
        svc2.stop()
    assert good.sent[0][1] == render_escpos_label(reg2.state.stock[res.leftover_addr], reg2.profile(PN))
    assert reg2.state.print_queue[0]["status"] == "sent"


def test_crash_after_send_before_mark_reprints_rather_than_losing_the_label(tmp_path, monkeypatch):
    reg, bus, mem, svc = rig(tmp_path)
    cut_remnant(reg, bus)
    real = reg.mark_print_sent
    monkeypatch.setattr(reg, "mark_print_sent", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        svc.drain()
    monkeypatch.setattr(reg, "mark_print_sent", real)
    assert len(mem.sent) == 1 and reg.state.print_queue[0]["status"] == "pending"
    svc.drain()
    assert len(mem.sent) == 2 and reg.state.print_queue[0]["status"] == "sent"      # at-least-once, never zero


def test_worker_thread_survives_odd_transport_errors_and_stops_cleanly(tmp_path):
    class Weird(MemoryTransport):
        def send(self, data):
            if not self.sent and not getattr(self, "boom", False):
                self.boom = True
                raise ValueError("not an OSError")
            super().send(data)
    reg, bus, mem, svc = rig(tmp_path, Weird(), base_delay=0.0)
    svc.start()
    cut_remnant(reg, bus)
    deadline = time.time() + 3
    while not mem.sent and time.time() < deadline:
        time.sleep(0.01)
    assert len(mem.sent) == 1 and svc.status()["running"]
    svc.stop()
    assert not svc.status()["running"]


def test_sent_jobs_are_pruned(tmp_path):
    reg, _, _, _ = rig(tmp_path)
    for i in range(R.MAX_SENT_PRINTS + 10):
        j = reg.enqueue_print(f"10.2.2.{i % 250 + 1}", f"u{i}", "T", "AA")
        reg.mark_print_sent(j["id"])
    assert len(reg.state.print_queue) == R.MAX_SENT_PRINTS


def test_printer_from_env(tmp_path, monkeypatch):
    reg = Registry.load(tmp_path / "s.json")
    monkeypatch.delenv("EXTBUS_PRINTER", raising=False)
    assert printer_from_env(reg) is None
    monkeypatch.setenv("EXTBUS_PRINTER", "net:10.1.1.7:9100")
    assert printer_from_env(reg).transport.describe() == "net:10.1.1.7:9100"
