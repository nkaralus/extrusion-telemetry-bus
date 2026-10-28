# ---- 1. make the example opt-in and add --prefill (backup: cascade_clock.py.before-prefill)
cat << 'PEOF' > ~/cascade_ws/patch_prefill.py
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "def ask_prefilled" in s: sys.exit("cascade_clock.py already has --prefill.")
if "def worked_example" not in s: sys.exit("Apply patch_example.py first.")

NEW_FUNCS = '''# ---------------------------------------------------------------- prefill: start the mule's answer for it
def complete_prefilled(prefill, cont):
    """The mule continues after the prefill; close the JSON string at its first unescaped quote (anything after is ignored)."""
    m = re.search(r'(?<!\\\\)"', cont)
    return prefill + (cont[:m.start()] if m else cont.strip()) + '"}'

def ask_prefilled(bench, model_key, message, prefill, max_new=120):
    """Same model and sampling as bench.ask, but the answer already starts with `prefill`. Returns only the continuation."""
    import torch
    bench._load(model_key)
    tok, model = bench._loaded["tok"], bench._loaded["model"]
    text = tok.apply_chat_template([{"role": "user", "content": message}], add_generation_prompt=True, tokenize=False) + prefill
    enc = tok(text, return_tensors="pt", add_special_tokens=False).to(model.device)
    t0 = time.time()
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=max_new, do_sample=bench.CFG["temperature"] > 0,
                             temperature=max(bench.CFG["temperature"], 1e-5))
    dt = time.time() - t0
    new = out[0][enc["input_ids"].shape[-1]:]
    return tok.decode(new, skip_special_tokens=True), int(new.shape[-1]), dt

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
EDITS = [
 (MARK, NEW_FUNCS + MARK),
 ('    ap.add_argument("--no-example", action="store_true", help="leave the worked example out of the mule message")\n',
  '    ap.add_argument("--example", action="store_true", help="put one worked example in the mule message (off by default: Gemma copied it)")\n'
  '    ap.add_argument("--prefill", action="store_true", help="start the mule\'s answer for it, so it only completes the statement (with --script, the script holds the continuations)")\n'),
 ("    example = None if a.no_example else worked_example(block, host, spec)\n", "    example = worked_example(block, host, spec) if a.example else None\n"),
 ('    row["example"] = bool(example)\n',
  '    row["example"] = bool(example)\n'
  '    prefill = (json.dumps({"frame_id": block["frame_id"], "target": block["target"]}, separators=(",", ":"))[:-1] + \',"stmt":"\') if a.prefill else ""\n'
  '    row["prefill"] = bool(prefill)\n'
  '    if prefill: log(f"PREFILL         {prefill}  (the mule only completes the statement)")\n'),
 ('        def ask(msg):\n            try: return next(replies), 0, 0.0\n', '        def ask(msg, prefill=""):\n            try: return next(replies), 0, 0.0\n'),
 ('        def ask(msg): return bench.ask(a.model, [{"role": "user", "content": msg}])\n',
  '        def ask(msg, prefill=""):\n            if prefill: return ask_prefilled(bench, a.model, msg, prefill)\n            return bench.ask(a.model, [{"role": "user", "content": msg}])\n'),
 ("        try: text, nt, dt = ask(msg)\n", "        try: text, nt, dt = ask(msg, prefill)\n"),
 ("        reply = parse_reply(text)\n", "        if prefill: text = complete_prefilled(prefill, text)\n        reply = parse_reply(text)\n"),
]
for old, new in EDITS:
    if s.count(old) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + old[:90])
    s = s.replace(old, new, 1)
compile(s, p, "exec")
open(p + ".before-prefill", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_clock.py patched: worked example is now opt-in (--example), new option --prefill (backup: cascade_clock.py.before-prefill)")
PEOF
python3 ~/cascade_ws/patch_prefill.py ~/cascade_ws/cascade_clock.py

# ---- 2. a rehearsal file: with --prefill the script holds what the mule writes AFTER the prefill
cat << 'EOF' > ~/cascade_ws/rehearsal_prefill_ok.json
["velocity = Vector2(0.0, -400.0)\"}"]
EOF

# ---- 3. quick check, no GPU
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 cascade_clock.py block_0001.json --prefill --script rehearsal_prefill_ok.json --no-apply
