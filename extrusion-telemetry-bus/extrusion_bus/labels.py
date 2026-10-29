"""Direct-thermal ESC/POS remnant labels (2" printer, USB/Bluetooth raw).

The sticker carries the physical DNS (tape color + length bracket) in large
type, the bus address, and a QR payload the stationary scanner can read back.
"""
from __future__ import annotations

import base64
from typing import List, Optional, Tuple

from pydantic import BaseModel

from .physics import fmt_inches
from .registry import Profile, StockItem

ESC = b"\x1b"
GS = b"\x1d"
QR_PREFIX = "EXTBUS"


class Label(BaseModel):
    addr: str
    uid: str
    bin_location: str
    qr_payload: str
    lines: List[str]
    escpos_b64: str


def qr_payload(item: StockItem) -> str:
    return f"{QR_PREFIX}|{item.addr}|{item.uid}"


def parse_qr_payload(payload: str) -> Optional[Tuple[str, str]]:
    parts = payload.strip().split("|")
    if len(parts) == 3 and parts[0] == QR_PREFIX:
        return parts[1], parts[2]
    return None


def _qr_commands(data: bytes, module_size: int = 6) -> bytes:
    store_len = len(data) + 3
    return (
        GS + b"(k" + bytes([4, 0, 49, 65, 50, 0])               # model 2
        + GS + b"(k" + bytes([3, 0, 49, 67, module_size])        # module size
        + GS + b"(k" + bytes([3, 0, 49, 69, 49])                 # error correction M
        + GS + b"(k" + bytes([store_len & 0xFF, store_len >> 8, 49, 80, 48]) + data
        + GS + b"(k" + bytes([3, 0, 49, 81, 48])                 # print symbol
    )


def render_escpos(title: str, lines: List[str], payload: str) -> bytes:
    def txt(s: str) -> bytes:
        return s.encode("cp437", errors="replace") + b"\n"

    out = ESC + b"@" + ESC + b"a\x01"                            # init, center
    out += GS + b"!\x11" + ESC + b"E\x01" + txt(title)           # 2x size, bold
    out += GS + b"!\x00" + ESC + b"E\x00"
    for ln in lines:
        out += txt(ln)
    out += _qr_commands(payload.encode("ascii"))
    out += b"\n\n\n" + GS + b"V\x42\x00"                         # feed + partial cut
    return out


def build_label(item: StockItem, profile: Profile) -> Label:
    bin_loc = item.bin_location or "UNBINNED"
    lines = [
        item.addr,
        profile.part_number,
        f"SQ {fmt_inches(item.guaranteed_square_in)}  LONG {fmt_inches(item.long_point_in)}",
        f"A: {item.end_a.describe()}",
        f"B: {item.end_b.describe()}",
        profile.alloy,
    ]
    payload = qr_payload(item)
    raw = render_escpos(bin_loc, lines, payload)
    return Label(
        addr=item.addr,
        uid=item.uid,
        bin_location=bin_loc,
        qr_payload=payload,
        lines=[bin_loc] + lines,
        escpos_b64=base64.b64encode(raw).decode("ascii"),
    )
