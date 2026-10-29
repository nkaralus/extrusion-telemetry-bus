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


def run_user_command(args) -> int:
    import getpass
    from .auth import AuthError, AuthService
    from .store import StoreError
    if not args.db:
        print("error: give --db or set EXTBUS_DB")
        return 2
    try:
        reg = Registry.load(args.db)
    except (StoreError, OSError) as exc:
        print(f"error: {exc}")
        return 2
    try:
        svc = AuthService(reg)
        if args.cmd == "user-list":
            for u in svc.list_users():
                print(f"{u['username']:<20} {u['role']:<9} {'active' if u['active'] else 'DISABLED'}")
            return 0
        secret = sys.stdin.readline().rstrip("\n") if args.secret_stdin else getpass.getpass("secret: ")
        if args.cmd == "user-add":
            svc.create_user(args.username, args.role, secret, by="cli")
            print(f"created {args.username.strip().lower()} ({args.role})")
        else:
            svc.set_secret(args.username.strip().lower(), secret)
            print(f"secret reset for {args.username.strip().lower()} (their sessions were ended)")
        reg.append_audit(user="(cli)", role="admin", ip="", action="CLI_" + args.cmd.upper().replace("-", "_"), target=args.username)
        return 0
    except AuthError as exc:
        print(f"error: {exc}")
        return 2
    finally:
        reg.close()


def run_db_command(args) -> int:
    from .store import SqliteStore, StoreError, check_db, export_json, import_json
    if not args.db:
        print("error: give --db or set EXTBUS_DB")
        return 2
    try:
        if args.cmd == "export-json":
            n = export_json(args.db, args.out, include_sessions=args.include_sessions)
            print(f"exported {args.db} -> {args.out} ({n} bytes)")
        elif args.cmd == "import-json":
            reg = import_json(args.json, args.db, replace=args.replace)
            print(f"imported {args.json} -> {args.db} (stock={len(reg.state.stock)}, jobs={len(reg.state.carts)})")
            reg.close()
        elif args.cmd == "backup-db":
            store = SqliteStore(args.db, take_lock=False)
            try:
                store.backup_to(args.out)
            finally:
                store.close()
            print(f"backup {args.db} -> {args.out}")
        else:
            r = check_db(args.db)
            print(("integrity ok" if r["integrity_ok"] else "INTEGRITY FAILED") + f" ({r['journal_mode']})")
            print(" ".join(f"{k}={v}" for k, v in r["counts"].items()))
            return 0 if r["integrity_ok"] else 2
    except (StoreError, OSError) as exc:
        print(f"error: {exc}")
        return 2
    return 0


def run_printer_test(target: str, style: str, feed: str, png: Optional[str]) -> int:
    from .printer import parse_target, render_escpos_label, render_label_image
    from .registry import StockItem
    reg = Registry()
    item = reg.add_remnant("EXT-240-OPEN", 233.875, End.miter(45, Face.BACK), End.square())
    prof = reg.profile("EXT-240-OPEN")
    data = render_escpos_label(item, prof, style=style, feed=feed)
    if png:
        render_label_image(item, prof).save(png)
    transport = parse_target(target)
    try:
        transport.send(data)
    except OSError as exc:
        print(f"FAILED {transport.describe()}: {exc}")
        return 2
    print(f"sent {len(data)} bytes to {transport.describe()} ({style}, feed={feed})")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="extbus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="seed a shop and run a cut order end to end")
    d.add_argument("--state", type=Path, default=None, help="state_registry.json (default: temp file)")
    d.add_argument("--scope", default=DEFAULT_SCOPE)
    d.add_argument("--job", default="JOB-104")
    sub.add_parser("serve", help="run the FastAPI bus (EXTBUS_STATE, EXTBUS_PORT, EXTBUS_PARSER, EXTBUS_PRINTER)")
    for name, helptext in (("user-add", "create a user (offline; the server must be stopped)"),
                           ("user-list", "list users (offline)"),
                           ("user-passwd", "reset a user's secret (offline)")):
        up = sub.add_parser(name, help=helptext)
        up.add_argument("--db", default=os.environ.get("EXTBUS_DB") or os.environ.get("EXTBUS_STATE"),
                        help="database or state file (default: $EXTBUS_DB, then $EXTBUS_STATE)")
        if name != "user-list":
            up.add_argument("username")
            up.add_argument("--secret-stdin", action="store_true", help="read the secret from stdin instead of prompting")
        if name == "user-add":
            up.add_argument("--role", required=True, choices=["rep", "engineer", "operator", "admin"])
    ex = sub.add_parser("export-json", help="portable backup: write the SQLite database as a state_registry.json document")
    ex.add_argument("--db", default=os.environ.get("EXTBUS_DB"), help="database (default: $EXTBUS_DB)")
    ex.add_argument("--out", required=True)
    ex.add_argument("--include-sessions", action="store_true", help="also export login sessions (off by default)")
    im = sub.add_parser("import-json", help="load a state_registry.json (or an export) into a SQLite database")
    im.add_argument("--json", required=True)
    im.add_argument("--db", default=os.environ.get("EXTBUS_DB"))
    im.add_argument("--replace", action="store_true", help="overwrite a database that already holds data")
    bk = sub.add_parser("backup-db", help="online, consistent copy of the database (safe while the server runs)")
    bk.add_argument("--db", default=os.environ.get("EXTBUS_DB"))
    bk.add_argument("--out", required=True)
    ck = sub.add_parser("check-db", help="integrity check and row counts (read-only)")
    ck.add_argument("--db", default=os.environ.get("EXTBUS_DB"))
    pt = sub.add_parser("printer-test", help="send a sample 2x1 label to a printer target")
    pt.add_argument("--target", default="memory", help="net:host[:port] | file:/dev/usb/lp0 | memory")
    pt.add_argument("--style", default="raster", choices=["raster", "native"])
    pt.add_argument("--feed", default="ff", help="ff | gs_ff | none | lines:N")
    pt.add_argument("--save-png", help="also write the rendered label bitmap to this path")
    args = ap.parse_args(argv)
    if args.cmd in ("user-add", "user-list", "user-passwd"):
        return run_user_command(args)
    if args.cmd in ("export-json", "import-json", "backup-db", "check-db"):
        return run_db_command(args)
    if args.cmd == "printer-test":
        return run_printer_test(args.target, args.style, args.feed, args.save_png)
    if args.cmd == "serve":
        from .server import main as serve
        serve()
        return 0
    state = args.state or Path(tempfile.mkdtemp(prefix="extbus_")) / "state_registry.json"
    return run_demo(state, args.scope, args.job)


if __name__ == "__main__":
    sys.exit(main())
