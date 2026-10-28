import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "first frame of a process" in s: sys.exit("cascade_sweep.py already has these fixes.")

EDITS = [
 ('        fails = fails + 1 if row["status"] != "ACK" else 0\n',
  '        v = (row.get("gi2") or {}).get("vram_gb_used")\n'
  '        if v is not None and c.get("split"): print(f"{\'\':13s}GPU memory in use at the start of this frame: {v:.1f} GB", flush=True)\n'
  '        if row["status"] != "ACK": print("        " + "; ".join(row.get("problems") or [row["status"]]), flush=True)\n'
  '        try:\n'
  '            import torch\n'
  '            if getattr(torch, "cuda", None) is not None and torch.cuda.is_available(): torch.cuda.empty_cache()   # hand cached GPU memory back between frames\n'
  '        except ImportError:\n'
  '            pass\n'
  '        fails = fails + 1 if row["status"] != "ACK" else 0\n'),
 ('    split = [r["clip"] for r in acked if (r.get("clip") or {}).get("split")]\n',
  '    split = [r["clip"] for r in acked if r is not rows[0] and (r.get("clip") or {}).get("split")]   # the first frame pays one-time start-up cost\n'),
 ('    ws = os.path.abspath(os.path.expanduser(a.ws)); groups = collections.defaultdict(list)\n',
  '    ws = os.path.abspath(os.path.expanduser(a.ws)); groups = collections.defaultdict(list); first_seen = set()\n'),
 ('        r = json.loads(line); c = r.get("clip")\n        if not c or not c.get("split"): continue\n        if a.run_id and r.get("run_id") != a.run_id: continue\n',
  '        r = json.loads(line); c = r.get("clip")\n'
  '        if a.run_id and r.get("run_id") != a.run_id: continue\n'
  '        if r.get("run_id") and r["run_id"] not in first_seen: first_seen.add(r["run_id"]); continue   # first frame of a process: one-time start-up cost\n'
  '        if not c or not c.get("split"): continue\n'),
 ('    print("\\n(seconds and SSD wait are means per frame; VRAM/RAM/prefetch/SSD are the share of expert requests served from each place)")\n',
  '    print("\\n(means per frame, the first frame of each sweep left out; VRAM/RAM/prefetch/SSD are the share of expert requests served from each place)")\n'),
]

for old, new in EDITS:
    if s.count(old) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + old[:90])
    s = s.replace(old, new, 1)

compile(s, p, "exec")
open(p + ".before-fixes", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_sweep.py patched: shows why a frame was rejected, GPU memory per frame, frees cached GPU memory between frames, skips the start-up frame in means")
