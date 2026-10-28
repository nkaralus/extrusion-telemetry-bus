import sys
clock_p, sweep_p = sys.argv[1], sys.argv[2]
clock, sweep = open(clock_p, encoding="utf-8").read(), open(sweep_p, encoding="utf-8").read()
if "def mule_view" in clock: sys.exit("cascade_clock.py already has the slim message.")
if "class Recorder" not in clock or "first frame of a process" not in sweep: sys.exit("Apply patch_heat.py and patch_sweep2.py first.")

OLD_MSG = '''def mule_message(block, host, prev, example=None):
    rag = {k: host[k] for k in ("addr", "subnet", "uid", "node", "name", "props", "peering", "exclude")}
    msg = {}
    if example: msg["example"] = example
    msg.update({"task": block, "rag": rag, "reply": {"frame_id": block["frame_id"], "target": block["target"], "stmt": ""}})
'''
NEW_MSG = '''# What the MULE gets to read. The guards still check the full block and the full RAG record; only this view is slimmed.
SLIM_VIEW = {"modify_property": {"task": ["frame_id", "op", "target", "property", "type", "value"], "rag": ["node", "name"]}}

def mule_view(block, host, slim):
    view = SLIM_VIEW.get(block["op"]) if slim else None
    if not view:
        return block, {k: host[k] for k in ("addr", "subnet", "uid", "node", "name", "props", "peering", "exclude")}
    rag = {k: host[k] for k in view["rag"]}
    rag["props"] = {block["property"]: host["props"][block["property"]]}
    return {k: block[k] for k in view["task"]}, rag

def mule_message(block, host, prev, example=None, slim=False):
    task, rag = mule_view(block, host, slim)
    msg = {}
    if example: msg["example"] = example
    msg.update({"task": task, "rag": rag, "reply": {"frame_id": block["frame_id"], "target": block["target"], "stmt": ""}})
'''
CLOCK_EDITS = [
 (OLD_MSG, NEW_MSG),
 ("        msg = mule_message(block, host, prev, example)\n", "        msg = mule_message(block, host, prev, example, a.slim)\n"),
 ('    ap.add_argument("--run-id", help="label stored in ledger.jsonl (cascade_sweep.py uses it)")\n',
  '    ap.add_argument("--run-id", help="label stored in ledger.jsonl (cascade_sweep.py uses it)")\n'
  '    ap.add_argument("--slim", action="store_true", help="show the mule only the fields it needs for this op (the guards still check everything)")\n'),
 ('    if a.run_id: row["run_id"] = a.run_id\n',
  '    if a.run_id: row["run_id"] = a.run_id\n'
  '    row["slim"] = bool(a.slim)\n'
  '    row["message_chars"] = len(mule_message(block, host, None, None, a.slim))\n'
  '    log(f"MESSAGE         {\'slim\' if a.slim else \'full\'}: {row[\'message_chars\']} characters" + (f" (full message: {len(mule_message(block, host, None))})" if a.slim else ""))\n'),
 ("        self.pre_heat = self.all_heat = self.dec_heat = self.s0 = self.s1 = None\n", "        self.pre_heat = self.all_heat = self.dec_heat = self.s0 = self.s1 = self.prompt_tokens = None\n"),
 ("    def mark(self):                                  # called by the generate hook the moment the prompt is done\n        self.pre_heat = collections.Counter(self.clip.STORE.heat); self.s1 = self._snap()\n",
  "    def mark(self, n_tokens=None):                   # called by the generate hook the moment the prompt is done\n        self.pre_heat = collections.Counter(self.clip.STORE.heat); self.s1 = self._snap(); self.prompt_tokens = n_tokens\n"),
 ('            out["unique_prompt"] = len(self.pre_heat); out["unique_decode"] = len(self.dec_heat)\n',
  '            out["unique_prompt"] = len(self.pre_heat); out["unique_decode"] = len(self.dec_heat); out["prompt_tokens"] = self.prompt_tokens\n'),
 ("                    _Mark.fired = True; on_prompt_done()\n",
  "                    _Mark.fired = True; on_prompt_done(input_ids.shape[-1] if hasattr(input_ids, \"shape\") else None)\n"),
 ('''        if m["split"]: return f"prompt {f(m['prompt'])}  |  decode {f(m['decode'])}  |  experts touched {m['unique_experts']}"\n''',
  '''        if m["split"]: return f"prompt ({m.get('prompt_tokens', '?')} tokens) {f(m['prompt'])}  |  decode {f(m['decode'])}  |  experts touched {m['unique_experts']}"\n'''),
]
SWEEP_EDITS = [
 ('    extra = (["--no-record"] if a.no_record else []) + (["--no-toolmap"] if a.no_toolmap else [])\n',
  '    extra = (["--no-record"] if a.no_record else []) + (["--no-toolmap"] if a.no_toolmap else []) + (["--slim"] if a.slim else [])\n'),
 ('''            timing = f"prompt {c['prompt']['seconds']:.1f}s decode''', '''            timing = f"prompt {c.get('prompt_tokens', '?')} tok {c['prompt']['seconds']:.1f}s decode'''),
 ('        groups[c["source"].split(" (")[0]].append(r)\n', '        groups[(c["source"].split(" (")[0], "slim" if r.get("slim") else "full")].append(r)\n'),
 ("    for src, rows in sorted(groups.items()):\n", "    for (src, msg), rows in sorted(groups.items()):\n"),
 ("=== pinned experts from: {src}   (", "=== pinned experts from: {src}, message: {msg}   ({mean([r['clip'].get('prompt_tokens') or 0 for r in rows]):.0f} prompt tokens, "),
 ('ap.add_argument("--no-record", action="store_true"); ap.add_argument("--no-toolmap", action="store_true"); ap.add_argument("--run-id")\n',
  'ap.add_argument("--no-record", action="store_true"); ap.add_argument("--no-toolmap", action="store_true"); ap.add_argument("--run-id"); ap.add_argument("--slim", action="store_true")\n'),
]
for name, text, edits in (("clock", clock, CLOCK_EDITS), ("sweep", sweep, SWEEP_EDITS)):
    for old, new in edits:
        if text.count(old) != 1: sys.exit(f"Could not find the expected text exactly once in the {name}, so I changed nothing:\n" + old[:90])
        text = text.replace(old, new, 1)
    compile(text, name, "exec")
    if name == "clock": clock = text
    else: sweep = text
for path, text in ((clock_p, clock), (sweep_p, sweep)):
    open(path + ".before-slim", "w", encoding="utf-8").write(open(path, encoding="utf-8").read())
    open(path, "w", encoding="utf-8").write(text)
print("patched: --slim shows the mule only what modify_property needs; the prompt token count is now measured and reported (backups: *.before-slim)")
