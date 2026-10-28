#!/usr/bin/env python3
"""cascade_sweep.py - Project Cascade, Step 6: seed and measure the per-tool heat map.

  python3 cascade_sweep.py run [--n 60] [--seed 7]    make N varied frames from the registry's real properties and run them through
                                                       the clock in ONE process (Gemma loads once). Nothing is written to the scene.
  python3 cascade_sweep.py report [--run-id ID]       compare prompt / decode cache hits and SSD wait by where the pinned experts came from

Each ACKed frame adds the experts it used to <mule-dir>/results/heatmap_<model>_tool_modify_property.json.
The next process that starts the clock pins that map instead of the old one, so run a fresh sweep with another --seed to test it.
"""
import argparse, collections, json, os, random, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXCLUDE = ["KinematicBody2D", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"]

def vary(rng, lo, hi):
    style = rng.choice(["decimal", "decimal", "whole", "small", "zero"])
    if style == "zero": return 0.0
    if style == "whole": return float(round(rng.uniform(lo, hi)))
    if style == "small": return round(rng.uniform(lo / 10, hi / 10), 2) + 0.0
    return round(rng.uniform(lo, hi), 1) + 0.0

def make_frames(host, n, seed, exclude):
    rng = random.Random(seed)
    props = [(k, t) for k, t in sorted(host["props"].items()) if t in ("Vector2", "float")]
    if not props: sys.exit(f"STOPPED: {host['name']} has no Vector2 or float property to vary")
    frames, seen = [], set()
    while len(frames) < n:
        prop, t = props[len(frames) % len(props)]
        val = [vary(rng, -500, 500), vary(rng, -500, 500)] if t == "Vector2" else abs(vary(rng, 0, 600))
        key = (prop, json.dumps(val))
        if key in seen: continue
        seen.add(key)
        frames.append({"frame_id": f"0x{4096 + len(frames):04X}", "time_slot": "SLOT_3_EXECUTE", "subnet": host["subnet"], "op": "modify_property",
                       "target": host["name"], "property": prop, "type": t, "value": val, "ref": host["addr"], "exclude": exclude})
    return frames

def last_row(ledger):
    with open(ledger, encoding="utf-8") as f:
        lines = f.read().strip().splitlines()
    return json.loads(lines[-1])

def mean(xs): return sum(xs) / len(xs) if xs else 0.0

def k80(counter_rows):
    tot = sum(c for _, _, c in counter_rows); run = k = 0
    for _, _, c in counter_rows:                      # rows are most-common first
        run += c; k += 1
        if run >= 0.8 * tot: break
    return k, tot

def run(a):
    ws = os.path.abspath(os.path.expanduser(a.ws)); mule_dir = os.path.abspath(os.path.expanduser(a.mule_dir))
    sys.path.insert(0, ws)
    import cascade_clock as clock
    reg = json.load(open(os.path.join(ws, "state_registry.json")))
    ref = a.ref or next(r for r, h in reg["hosts"].items() if any(t in ("Vector2", "float") for t in h["props"].values()))
    host = reg["hosts"][ref]
    base = os.path.join(ws, "block_0001.json")
    exclude = json.load(open(base))["exclude"] if os.path.exists(base) else DEFAULT_EXCLUDE
    run_id = f"sweep-{time.strftime('%Y%m%d-%H%M%S')}-s{a.seed}"
    outdir = os.path.join(ws, "sweep", run_id); os.makedirs(outdir)
    frames = make_frames(host, a.n, a.seed, exclude)
    if a.only:
        if a.only > a.n: sys.exit(f"STOPPED: --only {a.only} is past the {a.n} frames of this seed")
        frames = [frames[a.only - 1]] * a.repeat           # the same frame, again and again
    for i, fr in enumerate(frames, 1): json.dump(fr, open(os.path.join(outdir, f"frame_{i:03d}.json"), "w"))
    print(f"{run_id}: {len(frames)} frames for {host['name']} ({', '.join(sorted(host['props']))}), frames saved in {os.path.relpath(outdir, ws)}", flush=True)
    clock.log = lambda msg: None                      # the clock's per-frame lines are summarised below instead
    extra = (["--no-record"] if a.no_record else []) + (["--no-toolmap"] if a.no_toolmap else []) + (["--slim"] if a.slim else []) + (["--ultra"] if a.ultra else []) + (["--full"] if a.full else []) + (["--temps", a.temps] if a.temps else [])
    ledger = os.path.join(ws, "ledger.jsonl"); rows, fails, t_start = [], 0, time.time()
    for i, fr in enumerate(frames, 1):
        if time.time() - t_start > a.max_minutes * 60:
            print(f"STOPPED: time cap of {a.max_minutes} minutes reached after {i - 1} frames"); break
        sys.argv = ["cascade_clock.py", os.path.join(outdir, f"frame_{i:03d}.json"), "--ws", ws, "--mule-dir", mule_dir, "--model", a.model,
                    "--godot", a.godot, "--prefill", "--no-apply", "--run-id", run_id] + extra
        try:
            clock.main(); code = 0
        except SystemExit as e:
            code = e.code
        if isinstance(code, str): sys.exit(code)       # the clock stopped itself (missing file, no Godot, ...)
        row = last_row(ledger); rows.append(row); c = row.get("clip") or {}
        shown = fr["value"] if fr["type"] == "float" else f"{fr['value'][0]}, {fr['value'][1]}"
        if c.get("split"):
            timing = f"prompt {c.get('prompt_tokens', '?')} tok {c['prompt']['seconds']:.1f}s decode {c['decode']['seconds']:.1f}s  SSD wait {c['prompt']['ssd_wait_s'] + c['decode']['ssd_wait_s']:.1f}s"
        else:
            timing = ""
        print(f"[{i:2d}/{len(frames)}] {fr['frame_id']} {fr['property']:<9} {shown!s:<16} {row['status']:<5} try {row.get('tries', '-')}  {timing}", flush=True)
        v = (row.get("gi2") or {}).get("vram_gb_used")
        if v is not None and c.get("split"): print(f"{'':13s}GPU memory in use at the start of this frame: {v:.1f} GB", flush=True)
        if row["status"] != "ACK":
            print("        " + "; ".join(row.get("problems") or [row["status"]]), flush=True)
            for at in row.get("attempts") or []: print(f"        try {at['try']} (T {at.get('temp', '?')}): {at['stmt'][:70]} -> {at.get('error', at['status'])}", flush=True)
        if row["status"] == "ACK" and row.get("tries", 1) > 1:
            for at in row.get("attempts") or []: print(f"        try {at['try']} (T {at.get('temp', '?')}): {at['stmt'][:70]} -> {at.get('error', at['status'])}", flush=True)
        try:
            import torch
            if getattr(torch, "cuda", None) is not None and torch.cuda.is_available(): torch.cuda.empty_cache()   # hand cached GPU memory back between frames
        except ImportError:
            pass
        fails = fails + 1 if row["status"] != "ACK" else 0
        if fails >= 3 and not a.only: print("STOPPED: three frames in a row were not ACKed, so the sweep stops instead of running on"); break   # repeating one frame is allowed to fail
    acked = [r for r in rows if r["status"] == "ACK"]
    print(f"\n=== sweep result: {len(acked)}/{len(rows)} ACK | first-try ACK {sum(1 for r in acked if r.get('tries') == 1)} | needed retries {sum(1 for r in acked if r.get('tries', 1) > 1)} | dead letter {sum(1 for r in rows if r['status'] == 'DLQ')}")
    split = [r["clip"] for r in acked if r is not rows[0] and (r.get("clip") or {}).get("split")]   # the first frame pays one-time start-up cost
    if split:
        print(f"    mean per frame: prompt {mean([c['prompt']['seconds'] for c in split]):.1f}s (SSD wait {mean([c['prompt']['ssd_wait_s'] for c in split]):.1f}s) | "
              f"decode {mean([c['decode']['seconds'] for c in split]):.1f}s (SSD wait {mean([c['decode']['ssd_wait_s'] for c in split]):.1f}s) | "
              f"heat map source: {split[0]['source']}")
    mc = (json.load(open(os.path.join(mule_dir, "config.json"))).get("models", {}).get(a.model) or {}).get("path", "")
    path = os.path.join(mule_dir, "results", f"heatmap_{os.path.basename(os.path.normpath(mc)) if mc else a.model}_tool_modify_property.json")
    if os.path.exists(path):
        m = json.load(open(path)); picks = m["picks"]; kk, tot = k80(picks)
        print(f"\n=== heat map {os.path.basename(path)}: {m['runs']} ACKed frames, {len(picks)} experts seen, {kk} of them carry 80% of the picks")
        for key, name in (("prompt_picks", "prompt"), ("decode_picks", "decode")):
            if key in m: k2, _ = k80(m[key]); print(f"    {name:7s}: {len(m[key])} experts seen, {k2} carry 80%")
        clip = sys.modules.get("clip_loader")
        if clip is not None and getattr(clip, "STORE", None) is not None:
            pf = ((json.load(open(os.path.join(mule_dir, 'config.json'))).get("models", {}).get(a.model) or {}).get("clip") or {}).get("pin_frac", 0.8)
            cap = int(clip.STORE.gpu_cap * pf) + int(clip.STORE.ram_cap * pf)
            cover = sum(c for _, _, c in picks[:cap]) / max(tot, 1)
            print(f"    pin capacity {cap} experts ({int(clip.STORE.gpu_cap * pf)} VRAM + {int(clip.STORE.ram_cap * pf)} RAM): the map's top {cap} cover {cover:.0%} of this tool's recorded picks")
    print(f"\nNext: run it again with a new seed to test the map on frames it has not seen:  python3 cascade_sweep.py run --n 10 --seed 99   then:  python3 cascade_sweep.py report")

def report(a):
    ws = os.path.abspath(os.path.expanduser(a.ws)); groups = collections.defaultdict(list); first_seen = set()
    for line in open(os.path.join(ws, "ledger.jsonl"), encoding="utf-8"):
        r = json.loads(line); c = r.get("clip")
        if a.run_id and r.get("run_id") != a.run_id: continue
        if r.get("run_id") and r["run_id"] not in first_seen: first_seen.add(r["run_id"]); continue   # first frame of a process: one-time start-up cost
        if not c or not c.get("split"): continue
        groups[(c["source"].split(" (")[0], "ultra" if r.get("ultra") else "slim" if r.get("slim") else "full")].append(r)
    if not groups: sys.exit("no frames with cache metrics in the ledger yet (run the sweep first)")
    def share(rows, phase, key):
        tot = sum(r["clip"][phase]["picks"] for r in rows); return sum(r["clip"][phase][key] for r in rows) / max(tot, 1)
    for (src, msg), rows in sorted(groups.items()):
        tk = [r["clip"]["prompt_tokens"] for r in rows if r["clip"].get("prompt_tokens")]
        n = len(rows); ack = sum(1 for r in rows if r["status"] == "ACK")
        print(f"\n=== pinned experts from: {src}, message: {msg}   ({(format(mean(tk), '.0f') if tk else '?')} prompt tokens, {n} frames, {ack} ACK, {sum(1 for r in rows if r.get('tries') == 1 and r['status'] == 'ACK')} on try 1)")
        print(f"    {'':8s}{'seconds':>9s}{'SSD wait':>10s}{'VRAM':>7s}{'RAM':>6s}{'prefetch':>10s}{'SSD':>6s}{'experts touched':>17s}")
        for phase, uniq in (("prompt", "unique_prompt"), ("decode", "unique_decode")):
            print(f"    {phase:8s}{mean([r['clip'][phase]['seconds'] for r in rows]):9.1f}{mean([r['clip'][phase]['ssd_wait_s'] for r in rows]):10.1f}"
                  f"{share(rows, phase, 'vram'):7.0%}{share(rows, phase, 'ram'):6.0%}{share(rows, phase, 'prefetch_hit'):10.0%}{share(rows, phase, 'ssd'):6.0%}"
                  f"{mean([r['clip'][uniq] for r in rows]):17.0f}")
    print("\n(means per frame, the first frame of each sweep left out; VRAM/RAM/prefetch/SSD are the share of expert requests served from each place)")

def main():
    ap = argparse.ArgumentParser(description="Seed and measure the per-tool heat map")
    ap.add_argument("mode", choices=["run", "report"])
    ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws")); ap.add_argument("--mule-dir", default=os.path.expanduser("~/mule_bench"))
    ap.add_argument("--model", default="gemma26b"); ap.add_argument("--godot", default=os.environ.get("GODOT", "godot"))
    ap.add_argument("--n", type=int, default=60); ap.add_argument("--seed", type=int, default=7); ap.add_argument("--ref")
    ap.add_argument("--max-minutes", type=int, default=30)
    ap.add_argument("--no-record", action="store_true"); ap.add_argument("--no-toolmap", action="store_true"); ap.add_argument("--run-id"); ap.add_argument("--slim", action="store_true"); ap.add_argument("--ultra", action="store_true"); ap.add_argument("--full", action="store_true"); ap.add_argument("--only", type=int); ap.add_argument("--repeat", type=int, default=6); ap.add_argument("--temps")
    a = ap.parse_args()
    run(a) if a.mode == "run" else report(a)

if __name__ == "__main__":
    main()
