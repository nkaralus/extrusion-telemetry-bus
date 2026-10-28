import sys
clock_p, sweep_p = sys.argv[1], sys.argv[2]
clock, sweep = open(clock_p, encoding="utf-8").read(), open(sweep_p, encoding="utf-8").read()
if "ULTRA_VIEW" in clock: sys.exit("cascade_clock.py already has the ultra-slim view.")
if "def mule_view" not in clock or "--slim" not in sweep: sys.exit("Apply patch_slim.py first.")

CLOCK_EDITS = [
 ('def mule_message(block, host, prev, example=None, slim=False):\n    task, rag = mule_view(block, host, slim)\n',
  '# Ultra-slim: the task fields only. The prefill already carries frame_id, target, property and the constructor.\n'
  'ULTRA_VIEW = {"modify_property": ["op", "property", "type", "value"]}\n\n'
  'def mule_message(block, host, prev, example=None, slim=False, ultra=False):\n'
  '    if ultra and block["op"] in ULTRA_VIEW:\n'
  '        msg = {"task": {k: block[k] for k in ULTRA_VIEW[block["op"]]}}\n'
  '        if prev: msg["nak"] = prev\n'
  '        return json.dumps(msg, separators=(",", ":"))\n'
  '    task, rag = mule_view(block, host, slim)\n'),
 ("        msg = mule_message(block, host, prev, example, a.slim)\n", "        msg = mule_message(block, host, prev, example, a.slim, a.ultra)\n"),
 ('    ap.add_argument("--slim", action="store_true", help="show the mule only the fields it needs for this op (the guards still check everything)")\n',
  '    ap.add_argument("--slim", action="store_true", help="show the mule only the fields it needs for this op (the guards still check everything)")\n'
  '    ap.add_argument("--ultra", action="store_true", help="show the mule the task fields only: no RAG record, no reply skeleton (needs --prefill)")\n'),
 ('    row["slim"] = bool(a.slim)\n    row["message_chars"] = len(mule_message(block, host, None, None, a.slim))\n',
  '    row["slim"] = bool(a.slim)\n    row["ultra"] = bool(a.ultra)\n    row["message_chars"] = len(mule_message(block, host, None, None, a.slim, a.ultra))\n'),
 ('''    log(f"MESSAGE         {'slim' if a.slim else 'full'}: {row['message_chars']} characters" + (f" (full message: {len(mule_message(block, host, None))})" if a.slim else ""))\n''',
  '''    log(f"MESSAGE         {'ultra' if a.ultra else 'slim' if a.slim else 'full'}: {row['message_chars']} characters" + (f" (full message: {len(mule_message(block, host, None))})" if a.slim or a.ultra else ""))\n'''),
]
SWEEP_EDITS = [
 ('(["--slim"] if a.slim else [])\n', '(["--slim"] if a.slim else []) + (["--ultra"] if a.ultra else [])\n'),
 ('ap.add_argument("--slim", action="store_true")\n', 'ap.add_argument("--slim", action="store_true"); ap.add_argument("--ultra", action="store_true")\n'),
 ('"slim" if r.get("slim") else "full")].append(r)\n', '"ultra" if r.get("ultra") else "slim" if r.get("slim") else "full")].append(r)\n'),
 ('        if row["status"] != "ACK": print("        " + "; ".join(row.get("problems") or [row["status"]]), flush=True)\n',
  '        if row["status"] != "ACK":\n'
  '            print("        " + "; ".join(row.get("problems") or [row["status"]]), flush=True)\n'
  '            for at in row.get("attempts") or []: print(f"        try {at[\'try\']}: {at[\'stmt\'][:70]} -> {at.get(\'error\', at[\'status\'])}", flush=True)\n'),
 ("    for (src, msg), rows in sorted(groups.items()):\n", "    for (src, msg), rows in sorted(groups.items()):\n        tk = [r[\"clip\"][\"prompt_tokens\"] for r in rows if r[\"clip\"].get(\"prompt_tokens\")]\n"),
 ("{mean([r['clip'].get('prompt_tokens') or 0 for r in rows]):.0f} prompt tokens, ", "{(format(mean(tk), '.0f') if tk else '?')} prompt tokens, "),
]
for name, text, edits in (("clock", clock, CLOCK_EDITS), ("sweep", sweep, SWEEP_EDITS)):
    for old, new in edits:
        if text.count(old) != 1: sys.exit(f"Could not find the expected text exactly once in the {name}, so I changed nothing:\n" + old[:90])
        text = text.replace(old, new, 1)
    compile(text, name, "exec")
    if name == "clock": clock = text
    else: sweep = text
for path, text in ((clock_p, clock), (sweep_p, sweep)):
    open(path + ".before-ultra", "w", encoding="utf-8").write(open(path, encoding="utf-8").read())
    open(path, "w", encoding="utf-8").write(text)
print("patched: --ultra shows the mule the task fields only; the sweep prints why a frame failed; report headers count only measured prompt tokens (backups: *.before-ultra)")
