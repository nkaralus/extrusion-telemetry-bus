"""extbus demo | extbus serve"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from .bus import FabricationBus
from .llm import RuleParser
from .physics import End, Face, fmt_inches
from .registry import Registry, StockKind

DEFAULT_SCOPE = (
    'We need twelve 58" screen braces for Job 104 with spline facing out, 45 degree miter; '
    'also two 54 3/8" braces square'
)


def seed_demo(reg: Registry) -> None:
    """Three virgin 24' sticks, one 20', plus two hand-cataloged drops."""
    reg.add_raw("EXT-240-OPEN", 288.0, count=3)
    reg.add_raw("EXT-240-OPEN", 240.0, count=1)
    # the drop from the design schema: 74.25" long, 45deg miter (long point on spline face) on end A
    reg.add_remnant("EXT-240-OPEN", 74.25, End.miter(45, Face.SPLINE), End.square(), origin="seed")
    reg.add_remnant("EXT-240-OPEN", 60.0, End.square(), End.square(), origin="seed")


def run_demo(state_path: Path, scope: str, job_id: str) -> int:
    reg = Registry.load(state_path)
    if not reg.state.stock:
        seed_demo(reg)
    bus = FabricationBus(reg, parser=RuleParser(reg))
    print(f"== state: {state_path}")
    print(f"== scope: {scope}\n")
    frame = bus.open_frame(job_id, scope)
    if frame.dlq:
        print(f"DLQ [{frame.dlq.code}] {frame.dlq.message}")
        return 2
    print(f"SLOT_2_PLAN -> {len(frame.pending)} pieces, naks={frame.naks}\n")
    bus.stage_next_cut(frame)
    while frame.current is not None:
        step = frame.current
        verdicts = " ".join(f"{v.gate}:{v.code}" for v in frame.guard.verdicts)
        print(f"#{step.step_id:02d} [{verdicts}]\n    {step.instruction}")
        ack = bus.ack(frame, "CUT_OK")  # key 1
        c = ack.commit
        extra = f" -> {c.leftover_addr}" if c.leftover_addr else ""
        print(f"    CUT_OK  {c.screen}{extra}  (drop {fmt_inches(c.plan.leftover_long_in)}, "
              f"sq {fmt_inches(c.plan.leftover_guaranteed_in)})")
    if frame.dlq:
        print(f"\nDLQ [{frame.dlq.code}] {frame.dlq.message}")
        return 2
    s = reg.state
    print(f"\n== job {job_id}: {len(s.carts[job_id].pieces)} pieces on cart {s.carts[job_id].addr}")
    print("== remnant racks (10.2.2.0/24):")
    for i in reg.stock_of("EXT-240-OPEN", StockKind.REMNANT):
        print(f"   {i.addr:<10} {i.bin_location:<13} sq {fmt_inches(i.guaranteed_square_in):>10}  "
              f"A={i.end_a.describe()} B={i.end_b.describe()}")
    print(f"== scrap hopper ({s.hopper_addr}): {reg.scrap_total_lb()} lb in {len(s.scrap_ledger)} entries")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="extbus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="seed a shop and run a cut order end to end")
    d.add_argument("--state", type=Path, default=None, help="state_registry.json (default: temp file)")
    d.add_argument("--scope", default=DEFAULT_SCOPE)
    d.add_argument("--job", default="JOB-104")
    sub.add_parser("serve", help="run the FastAPI bus (EXTBUS_STATE, EXTBUS_PORT, EXTBUS_PARSER)")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        from .server import main as serve
        serve()
        return 0
    state = args.state or Path(tempfile.mkdtemp(prefix="extbus_")) / "state_registry.json"
    return run_demo(state, args.scope, args.job)


if __name__ == "__main__":
    sys.exit(main())
