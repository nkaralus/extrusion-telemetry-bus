# ---- 1. hook the clock to the Ammo Clip (backup: cascade_clock.py.before-heat)
cat << 'PEOF' > ~/cascade_ws/patch_heat.py
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "class Recorder" in s: sys.exit("cascade_clock.py already has the Ammo Clip recorder.")
if "ctor = {" not in s: sys.exit("Apply patch_prefill3.py first.")

NEW_SECTION = '''# ---------------------------------------------------------------- AMMO CLIP recorder (Step 6): cache metrics + per-tool heat map
class Recorder:
    """Hooks the clip. Splits every frame into PROMPT and DECODE, reports cache hits and SSD wait for each, and - only for an
    answer Godot ACKs - adds the experts picked to results/heatmap_<model>_tool_<op>.json in the mule folder."""
    def __init__(self, clip, path, tag, source, record):
        self.clip, self.path, self.tag, self.source, self.record = clip, path, tag, source, record
        self.keep_flag = getattr(clip, "HEAT_DECODE_ONLY", None)
        self.pre_heat = self.all_heat = self.dec_heat = self.s0 = self.s1 = None

    def _snap(self):
        st = self.clip.STORE
        return {"stats": dict(st.stats), "ssd": float(st.ssd_seconds), "t": time.time()}

    @staticmethod
    def _phase(a, b):
        d = {k: b["stats"].get(k, 0) - a["stats"].get(k, 0) for k in ("vram", "ram", "prefetch_hit", "ssd")}
        d["picks"] = sum(d.values()); d["ssd_wait_s"] = round(b["ssd"] - a["ssd"], 2); d["seconds"] = round(b["t"] - a["t"], 2)
        return d

    def begin(self):
        self.clip.HEAT_DECODE_ONLY = False           # count the prompt too; the split happens in mark()
        self.clip.STORE.heat.clear(); self.pre_heat = None; self.s1 = None; self.s0 = self._snap()

    def mark(self):                                  # called by the generate hook the moment the prompt is done
        self.pre_heat = collections.Counter(self.clip.STORE.heat); self.s1 = self._snap()

    def end(self):
        s2 = self._snap(); self.all_heat = collections.Counter(self.clip.STORE.heat)
        if self.keep_flag is not None: self.clip.HEAT_DECODE_ONLY = self.keep_flag
        out = {"source": self.source, "split": self.s1 is not None, "unique_experts": len(self.all_heat), "total": self._phase(self.s0, s2)}
        self.dec_heat = None
        if self.s1 is not None:
            self.dec_heat = self.all_heat - self.pre_heat
            out["prompt"] = self._phase(self.s0, self.s1); out["decode"] = self._phase(self.s1, s2)
            out["unique_prompt"] = len(self.pre_heat); out["unique_decode"] = len(self.dec_heat)
        return out

    def line(self, m):
        def f(d):
            n = max(d["picks"], 1)
            return f"{d['seconds']:.1f}s, SSD wait {d['ssd_wait_s']:.1f}s, VRAM {d['vram']/n:.0%} RAM {d['ram']/n:.0%} prefetch {d['prefetch_hit']/n:.0%} SSD {d['ssd']/n:.0%}"
        if m["split"]: return f"prompt {f(m['prompt'])}  |  decode {f(m['decode'])}  |  experts touched {m['unique_experts']}"
        return f"whole frame {f(m['total'])}  |  experts touched {m['unique_experts']}"

    def commit(self):
        """Merge this attempt's picks into the tool's cumulative map. Called only after Godot said ACK."""
        if not self.record: return False
        old = json.load(open(self.path)) if os.path.exists(self.path) else {}
        def merge(key, add):
            cum = collections.Counter({(l, e): c for l, e, c in old.get(key, [])}); cum.update(add or {})
            return [[l, e, c] for (l, e), c in cum.most_common()]
        data = {"tag": self.tag, "runs": old.get("runs", 0) + 1, "last_prompt": "cascade_clock", "picks": merge("picks", self.all_heat)}
        for key, add in (("prompt_picks", self.pre_heat), ("decode_picks", self.dec_heat)):
            if add is not None: data[key] = merge(key, add)
            elif key in old: data[key] = old[key]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"; json.dump(data, open(tmp, "w")); os.replace(tmp, self.path)
        return True

def make_recorder(bench, a, block, mule_dir):
    mcfg = bench.CFG["models"].get(a.model) or {}
    clipcfg = mcfg.get("clip")
    if not clipcfg: return None
    tag = f"tool_{block['op']}"
    path = os.path.join(mule_dir, "results", f"heatmap_{os.path.basename(os.path.normpath(mcfg['path']))}_{tag}.json")
    first_load = bench._loaded.get("key") != a.model
    use_map = first_load and not a.no_toolmap and os.path.exists(path)
    if use_map: clipcfg["warm"] = False              # in memory only (config.json is not touched): do not pin the old map as well
    bench._load(a.model)                             # the model loads here, so frame timings exclude it
    clip = sys.modules["clip_loader"]
    if first_load:
        source = "config" if clipcfg.get("warm", True) else "none"
        if use_map:
            ranked = [(l, e) for l, e, c in json.load(open(path))["picks"]]
            clip.STORE.warm(ranked, clipcfg.get("pin_frac", 0.8))
            source = f"tool map ({len(ranked)} experts)"
        bench._cascade_source = source
    return Recorder(clip, path, tag, getattr(bench, "_cascade_source", "config"), not a.no_record)

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
OLD_GEN = '''    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=max_new, do_sample=bench.CFG["temperature"] > 0,
                             temperature=max(bench.CFG["temperature"], 1e-5))
'''
NEW_GEN = '''    extra = {}
    if on_prompt_done:
        from transformers import LogitsProcessor, LogitsProcessorList
        class _Mark(LogitsProcessor):
            fired = False
            def __call__(self, input_ids, scores):
                if not _Mark.fired:
                    _Mark.fired = True; on_prompt_done()
                return scores
        extra["logits_processor"] = LogitsProcessorList([_Mark()])
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=max_new, do_sample=bench.CFG["temperature"] > 0,
                             temperature=max(bench.CFG["temperature"], 1e-5), **extra)
'''
EDITS = [
 ("import argparse, json, os, re, shutil, subprocess, sys, tempfile, time\n", "import argparse, collections, json, os, re, shutil, subprocess, sys, tempfile, time\n"),
 (MARK, NEW_SECTION + MARK),
 ("def ask_prefilled(bench, model_key, message, prefill, max_new=120):\n", "def ask_prefilled(bench, model_key, message, prefill, max_new=120, on_prompt_done=None):\n"),
 (OLD_GEN, NEW_GEN),
 ('    ap.add_argument("--tries", type=int, default=3)\n',
  '    ap.add_argument("--tries", type=int, default=3)\n'
  '    ap.add_argument("--no-record", action="store_true", help="do not add this frame\'s experts to the tool\'s heat map")\n'
  '    ap.add_argument("--no-toolmap", action="store_true", help="do not pin the tool\'s heat map at load (keep what config.json does)")\n'
  '    ap.add_argument("--run-id", help="label stored in ledger.jsonl (cascade_sweep.py uses it)")\n'),
 ('    row["prefill"] = bool(prefill)\n', '    row["prefill"] = bool(prefill)\n    if a.run_id: row["run_id"] = a.run_id\n'),
 ('        replies = iter(load(a.script))\n', '        rec = None\n        replies = iter(load(a.script))\n'),
 ('        def ask(msg, prefill=""):\n            try: return next(replies), 0, 0.0\n', '        def ask(msg, prefill="", mark=None):\n            try: return next(replies), 0, 0.0\n'),
 ('        bench.CFG["max_tokens"] = 400\n',
  '        bench.CFG["max_tokens"] = 400\n'
  '        rec = make_recorder(bench, a, block, os.path.abspath(os.path.expanduser(a.mule_dir)))\n'
  '        if rec: log(f"CLIP            heat map source: {rec.source}  |  " + (f"recording ACKed frames into {os.path.basename(rec.path)}" if rec.record else "not recording (--no-record)"))\n'),
 ('        def ask(msg, prefill=""):\n            if prefill: return ask_prefilled(bench, a.model, msg, prefill)\n',
  '        def ask(msg, prefill="", mark=None):\n            if prefill: return ask_prefilled(bench, a.model, msg, prefill, on_prompt_done=mark)\n'),
 ("        try: text, nt, dt = ask(msg, prefill)\n", "        if rec: rec.begin()\n        try: text, nt, dt = ask(msg, prefill, rec.mark if rec else None)\n"),
 ("        if prefill: text = complete_prefilled(prefill, text)\n",
  "        clipm = rec.end() if rec else None\n"
  "        if clipm:\n"
  "            row[\"clip\"] = clipm; log(\"CLIP            \" + rec.line(clipm))\n"
  "        if prefill: text = complete_prefilled(prefill, text)\n"),
 ('                attempts.append({"try": t, "stmt": stmt, "status": "ACK"})\n',
  '                attempts.append({"try": t, "stmt": stmt, "status": "ACK"})\n'
  '                if rec and clipm:\n'
  '                    clipm["map_recorded"] = rec.commit(); row["clip"] = clipm\n'),
]
for old, new in EDITS:
    if s.count(old) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + old[:90])
    s = s.replace(old, new, 1)
compile(s, p, "exec")
open(p + ".before-heat", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_clock.py patched with the Ammo Clip recorder (backup: cascade_clock.py.before-heat)")
PEOF
python3 ~/cascade_ws/patch_heat.py ~/cascade_ws/cascade_clock.py

# ---- 2. the sweep script
cat << 'SWEEPEOF' > ~/cascade_ws/cascade_sweep.py
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
    for i, fr in enumerate(frames, 1): json.dump(fr, open(os.path.join(outdir, f"frame_{i:03d}.json"), "w"))
    print(f"{run_id}: {len(frames)} frames for {host['name']} ({', '.join(sorted(host['props']))}), frames saved in {os.path.relpath(outdir, ws)}", flush=True)
    clock.log = lambda msg: None                      # the clock's per-frame lines are summarised below instead
    extra = (["--no-record"] if a.no_record else []) + (["--no-toolmap"] if a.no_toolmap else [])
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
            timing = f"prompt {c['prompt']['seconds']:.1f}s decode {c['decode']['seconds']:.1f}s  SSD wait {c['prompt']['ssd_wait_s'] + c['decode']['ssd_wait_s']:.1f}s"
        else:
            timing = ""
        print(f"[{i:2d}/{len(frames)}] {fr['frame_id']} {fr['property']:<9} {shown!s:<16} {row['status']:<5} try {row.get('tries', '-')}  {timing}", flush=True)
        fails = fails + 1 if row["status"] != "ACK" else 0
        if fails >= 3: print("STOPPED: three frames in a row were not ACKed, so the sweep stops instead of running on"); break
    acked = [r for r in rows if r["status"] == "ACK"]
    print(f"\n=== sweep result: {len(acked)}/{len(rows)} ACK | first-try ACK {sum(1 for r in acked if r.get('tries') == 1)} | needed retries {sum(1 for r in acked if r.get('tries', 1) > 1)} | dead letter {sum(1 for r in rows if r['status'] == 'DLQ')}")
    split = [r["clip"] for r in acked if (r.get("clip") or {}).get("split")]
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
    ws = os.path.abspath(os.path.expanduser(a.ws)); groups = collections.defaultdict(list)
    for line in open(os.path.join(ws, "ledger.jsonl"), encoding="utf-8"):
        r = json.loads(line); c = r.get("clip")
        if not c or not c.get("split"): continue
        if a.run_id and r.get("run_id") != a.run_id: continue
        groups[c["source"].split(" (")[0]].append(r)
    if not groups: sys.exit("no frames with cache metrics in the ledger yet (run the sweep first)")
    def share(rows, phase, key):
        tot = sum(r["clip"][phase]["picks"] for r in rows); return sum(r["clip"][phase][key] for r in rows) / max(tot, 1)
    for src, rows in sorted(groups.items()):
        n = len(rows); ack = sum(1 for r in rows if r["status"] == "ACK")
        print(f"\n=== pinned experts from: {src}   ({n} frames, {ack} ACK, {sum(1 for r in rows if r.get('tries') == 1 and r['status'] == 'ACK')} on try 1)")
        print(f"    {'':8s}{'seconds':>9s}{'SSD wait':>10s}{'VRAM':>7s}{'RAM':>6s}{'prefetch':>10s}{'SSD':>6s}{'experts touched':>17s}")
        for phase, uniq in (("prompt", "unique_prompt"), ("decode", "unique_decode")):
            print(f"    {phase:8s}{mean([r['clip'][phase]['seconds'] for r in rows]):9.1f}{mean([r['clip'][phase]['ssd_wait_s'] for r in rows]):10.1f}"
                  f"{share(rows, phase, 'vram'):7.0%}{share(rows, phase, 'ram'):6.0%}{share(rows, phase, 'prefetch_hit'):10.0%}{share(rows, phase, 'ssd'):6.0%}"
                  f"{mean([r['clip'][uniq] for r in rows]):17.0f}")
    print("\n(seconds and SSD wait are means per frame; VRAM/RAM/prefetch/SSD are the share of expert requests served from each place)")

def main():
    ap = argparse.ArgumentParser(description="Seed and measure the per-tool heat map")
    ap.add_argument("mode", choices=["run", "report"])
    ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws")); ap.add_argument("--mule-dir", default=os.path.expanduser("~/mule_bench"))
    ap.add_argument("--model", default="gemma26b"); ap.add_argument("--godot", default=os.environ.get("GODOT", "godot"))
    ap.add_argument("--n", type=int, default=60); ap.add_argument("--seed", type=int, default=7); ap.add_argument("--ref")
    ap.add_argument("--max-minutes", type=int, default=30)
    ap.add_argument("--no-record", action="store_true"); ap.add_argument("--no-toolmap", action="store_true"); ap.add_argument("--run-id")
    a = ap.parse_args()
    run(a) if a.mode == "run" else report(a)

if __name__ == "__main__":
    main()
SWEEPEOF

# ---- 3. quick check, no GPU: single-frame rehearsals still work
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 cascade_clock.py block_0001.json --prefill --script rehearsal_prefill_ok.json --no-apply
