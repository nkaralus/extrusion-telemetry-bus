# ---- 1. real runs default to prefill + ultra-slim; retries sample at 0.2 / 0.6 / 0.9; sweep gets --only/--repeat/--temps
cat << 'PEOF' > ~/cascade_ws/patch_defaults.py
import sys
clock_p, sweep_p = sys.argv[1], sys.argv[2]
clock, sweep = open(clock_p, encoding="utf-8").read(), open(sweep_p, encoding="utf-8").read()
if "a.temps" in clock: sys.exit("cascade_clock.py already has the defaults and retry temperatures.")
if "ULTRA_VIEW" not in clock or "--ultra" not in sweep: sys.exit("Apply patch_ultra.py first.")

CLOCK_EDITS = [
 ('    ap.add_argument("--ultra", action="store_true", help="show the mule the task fields only: no RAG record, no reply skeleton (needs --prefill)")\n',
  '    ap.add_argument("--ultra", action="store_true", help="show the mule the task fields only: no RAG record, no reply skeleton (needs --prefill)")\n'
  '    ap.add_argument("--full", action="store_true", help="show the mule the full message (opt out of the ultra-slim default)")\n'
  '    ap.add_argument("--no-prefill", action="store_true", help="do not start the mule\'s answer for it (the old behaviour; the ultra-slim default goes with it)")\n'
  '    ap.add_argument("--temps", default="0.2,0.6,0.9", help="sampling temperature for try 1, 2, 3: a retry that repeats a NAKed answer needs a different roll")\n'),
 ("    a = ap.parse_args()\n",
  "    a = ap.parse_args()\n"
  "    a.temps = [float(x) for x in a.temps.split(\",\")]\n"
  "    a.prefill = a.prefill or (not a.script and not a.no_prefill)       # the prefill is the standard way to run the real mule\n"
  "    a.ultra = a.ultra or (a.prefill and not a.full and not a.slim)     # ... and with it the ultra-slim view is the default (--full or --slim opt out)\n"
  "    if a.ultra and not a.prefill: die(\"--ultra needs the prefill (drop --no-prefill)\")\n"
  "    if a.ultra: a.slim = False\n"),
 ('    row["ultra"] = bool(a.ultra)\n', '    row["ultra"] = bool(a.ultra)\n    row["temps"] = a.temps\n'),
 ("        rec = None\n        replies = iter(load(a.script))\n", "        rec = None\n        set_temp = lambda tmp: None\n        replies = iter(load(a.script))\n"),
 ('        bench.CFG["max_tokens"] = 400\n', '        bench.CFG["max_tokens"] = 400\n        set_temp = lambda tmp: bench.CFG.__setitem__("temperature", tmp)\n'),
 ("        if rec: rec.begin()\n", "        temp = a.temps[min(t, len(a.temps)) - 1]; set_temp(temp)\n        if rec: rec.begin()\n"),
 ("try {t}/{a.tries}  ({nt} tokens, {dt:.0f}s)", "try {t}/{a.tries}  (T={temp}, {nt} tokens, {dt:.0f}s)"),
 ('{"try": t, "stmt": stmt, "status": "ACK"}', '{"try": t, "temp": temp, "stmt": stmt, "status": "ACK"}'),
 ('{"try": t, "stmt": stmt, "status": "NAK", "error": err}', '{"try": t, "temp": temp, "stmt": stmt, "status": "NAK", "error": err}'),
]
SWEEP_EDITS = [
 ("    frames = make_frames(host, a.n, a.seed, exclude)\n",
  "    frames = make_frames(host, a.n, a.seed, exclude)\n"
  "    if a.only:\n"
  "        if a.only > a.n: sys.exit(f\"STOPPED: --only {a.only} is past the {a.n} frames of this seed\")\n"
  "        frames = [frames[a.only - 1]] * a.repeat           # the same frame, again and again\n"),
 ('(["--ultra"] if a.ultra else [])\n', '(["--ultra"] if a.ultra else []) + (["--full"] if a.full else []) + (["--temps", a.temps] if a.temps else [])\n'),
 ('ap.add_argument("--ultra", action="store_true")\n',
  'ap.add_argument("--ultra", action="store_true"); ap.add_argument("--full", action="store_true"); ap.add_argument("--only", type=int); ap.add_argument("--repeat", type=int, default=6); ap.add_argument("--temps")\n'),
 ('        if fails >= 3: print("STOPPED: three frames in a row were not ACKed, so the sweep stops instead of running on"); break\n',
  '        if fails >= 3 and not a.only: print("STOPPED: three frames in a row were not ACKed, so the sweep stops instead of running on"); break   # repeating one frame is allowed to fail\n'),
 ("            for at in row.get(\"attempts\") or []: print(f\"        try {at['try']}: {at['stmt'][:70]} -> {at.get('error', at['status'])}\", flush=True)\n",
  "            for at in row.get(\"attempts\") or []: print(f\"        try {at['try']} (T {at.get('temp', '?')}): {at['stmt'][:70]} -> {at.get('error', at['status'])}\", flush=True)\n"
  "        if row[\"status\"] == \"ACK\" and row.get(\"tries\", 1) > 1:\n"
  "            for at in row.get(\"attempts\") or []: print(f\"        try {at['try']} (T {at.get('temp', '?')}): {at['stmt'][:70]} -> {at.get('error', at['status'])}\", flush=True)\n"),
]
for name, text, edits in (("clock", clock, CLOCK_EDITS), ("sweep", sweep, SWEEP_EDITS)):
    for old, new in edits:
        if text.count(old) != 1: sys.exit(f"Could not find the expected text exactly once in the {name}, so I changed nothing:\n" + old[:90])
        text = text.replace(old, new, 1)
    compile(text, name, "exec")
    if name == "clock": clock = text
    else: sweep = text
for path, text in ((clock_p, clock), (sweep_p, sweep)):
    open(path + ".before-defaults", "w", encoding="utf-8").write(open(path, encoding="utf-8").read())
    open(path, "w", encoding="utf-8").write(text)
print("patched: real runs now use the prefill and the ultra-slim message by default (--full, --slim, --no-prefill opt out); retries sample at 0.2 / 0.6 / 0.9 (--temps); sweep gets --only N --repeat R (backups: *.before-defaults)")
PEOF
python3 ~/cascade_ws/patch_defaults.py ~/cascade_ws/cascade_clock.py ~/cascade_ws/cascade_sweep.py

# ---- 2. quick check, no GPU (rehearsals keep their old behaviour unless you add --prefill)
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 cascade_clock.py block_0001.json --script rehearsal_retry.json --no-apply
python3 cascade_clock.py block_0001.json --prefill --script rehearsal_prefill_ok.json --no-apply
