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
