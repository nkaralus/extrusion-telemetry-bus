# ---- 1. the shared patch library and the read-only status script
cat << 'LIBEOF' > ~/cascade_ws/cascade_patchlib.py
"""cascade_patchlib.py - the small shared helper for the cascade patch scripts.

A patch is a list of per-file entries. Every file is judged on its own:
  done     the marker is already in the file
  ready    every anchor is found exactly once, so the edits can be applied
  blocked  an anchor is missing or ambiguous (the report names it)
Ready files are applied with a backup, done files are skipped, and a blocked file never stops the others.
Run the patch again after fixing a blocked file and it finishes what is left.
"""
import json, os


def _read(p):
    return open(p, encoding="utf-8").read()


def plan_text(text, marker, edits):
    if marker in text:
        return "done", [], text
    problems, cur = [], text
    for old, new in edits:
        n = cur.count(old)
        if n != 1:
            problems.append(f"anchor found {n} times (expected exactly 1): {old.strip().splitlines()[0][:80]!r}")
            continue
        cur = cur.replace(old, new, 1)
    return ("blocked" if problems else "ready"), problems, cur


def run(ws, name, entries):
    """entries: {"file", "marker", "edits": [(old, new), ...]} for text files, {"file", "done": fn(dict)->bool, "edit": fn(dict)} for json files."""
    ws = os.path.abspath(os.path.expanduser(ws))
    blocked = 0
    print(f"patch {name}")
    for e in entries:
        path = os.path.join(ws, e["file"])
        if not os.path.exists(path):
            print(f"  {e['file']:<24} MISSING  (file not found)")
            blocked += 1
            continue
        if "edits" in e:
            text = _read(path)
            state, problems, new = plan_text(text, e["marker"], e["edits"])
            if state == "ready" and e["file"].endswith(".py"):
                try:
                    compile(new, e["file"], "exec")
                except SyntaxError as ex:
                    state, problems = "blocked", [f"the edited file would not compile: {ex}"]
            if state == "ready":
                open(path + f".before-{name}", "w", encoding="utf-8").write(text)
                open(path, "w", encoding="utf-8").write(new)
                print(f"  {e['file']:<24} applied  (backup {e['file']}.before-{name})")
            elif state == "done":
                print(f"  {e['file']:<24} done     (already has it)")
            else:
                blocked += 1
                print(f"  {e['file']:<24} BLOCKED")
                for p in problems:
                    print("      - " + p)
        else:
            data = json.load(open(path, encoding="utf-8"))
            if e["done"](data):
                print(f"  {e['file']:<24} done     (already has it)")
                continue
            try:
                e["edit"](data)
            except KeyError as ex:
                blocked += 1
                print(f"  {e['file']:<24} BLOCKED")
                print(f"      - the file lacks {ex}")
                continue
            open(path + f".before-{name}", "w", encoding="utf-8").write(_read(path))
            json.dump(data, open(path, "w", encoding="utf-8"), indent=2)
            print(f"  {e['file']:<24} applied  (backup {e['file']}.before-{name})")
    if blocked:
        print(f"  => {blocked} file(s) blocked; the others were applied. Fix the blocked part (or paste this output) and run the patch again.")
    else:
        print("  => every file is in place")
    return blocked
LIBEOF
cat << 'STATUSEOF' > ~/cascade_ws/patch_status.py
#!/usr/bin/env python3
"""patch_status.py - read-only: which cascade patches are in which file of ~/cascade_ws. Changes nothing."""
import json, os, sys

ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
CLOCK, SWEEP, BUILDER = "cascade_clock.py", "cascade_sweep.py", "registry_builder.py"
TEXT = [
    ("step 5  apply to the scene", CLOCK, "def apply_scene_property"), ("step 6  worked example (opt-in)", CLOCK, "def worked_example"),
    ("step 6  prefill", CLOCK, "def ask_prefilled"), ("step 6  prefill: statement head", CLOCK, "stmt_head"), ("step 6  prefill: constructor", CLOCK, "ctor = {"),
    ("step 6  heat recorder", CLOCK, "class Recorder"), ("step 6  slim message", CLOCK, "def mule_view"), ("step 6  ultra message", CLOCK, "ULTRA_VIEW"),
    ("step 6  defaults and retry temperatures", CLOCK, "a.temps"), ("step 7  connect_signal", CLOCK, "def connect_checks"), ("step 7  self. spelling", CLOCK, "def canonical_stmt"),
    ("step 8  add_method", CLOCK, "def add_method_checks"), ("step 9  behavior check and chain", CLOCK, "def run_probe"),
    ("step 6  sweep fixes", SWEEP, "first frame of a process"), ("step 6  sweep message views", SWEEP, "--ultra"), ("step 6  sweep repeat mode", SWEEP, "--only"),
    ("step 3  registry: script field", BUILDER, '"script": script_path'), ("step 7  registry: signals and methods", BUILDER, '"signals": sigs'),
    ("step 9  registry: argument counts", BUILDER, '"signal_args": sig_args'),
]
JSON = [
    ("step 7  spec: connect_signal op", "task_block_spec.json", lambda d: d.get("ops", {}).get("connect_signal", {}).get("status") == "locked"),
    ("step 8  spec: add_method op", "task_block_spec.json", lambda d: d.get("ops", {}).get("add_method", {}).get("status") == "locked" and "fn" in d["slots"] and "add_method" in d["slots"]["op"]["enum"]),
    ("step 7  rag spec: signals, methods", "rag_record_spec.json", lambda d: "signals" in d["slots"] and "methods" in d["slots"]),
    ("step 9  rag spec: argument counts", "rag_record_spec.json", lambda d: "signal_args" in d["slots"] and "method_args" in d["slots"]),
]
missing = []
print(f"patch status for {ws}")
for label, fname, marker in TEXT:
    p = os.path.join(ws, fname)
    ok = os.path.exists(p) and marker in open(p, encoding="utf-8").read()
    print(f"  {'in place' if ok else 'MISSING ':<9} {label:<42} {fname}")
    if not ok:
        missing.append((label, fname))
for label, fname, test in JSON:
    p = os.path.join(ws, fname)
    try:
        ok = os.path.exists(p) and bool(test(json.load(open(p, encoding="utf-8"))))
    except (KeyError, TypeError, ValueError):
        ok = False
    print(f"  {'in place' if ok else 'MISSING ':<9} {label:<42} {fname}")
    if not ok:
        missing.append((label, fname))
reg = os.path.join(ws, "state_registry.json")
if os.path.exists(reg):
    hosts = json.load(open(reg)).get("hosts", {})
    have = all("signal_args" in h and "method_args" in h for h in hosts.values()) and bool(hosts)
    print(f"  {'in place' if have else 'STALE   ':<9} {'registry data has argument counts':<42} state_registry.json" + ("" if have else "   (run registry_builder.py)"))
print("\nnothing was changed." if not missing else f"\n{len(missing)} piece(s) missing. A patch run (they are safe to repeat) fills them in; if one is BLOCKED it names the anchor.")
STATUSEOF

# ---- 2. Step 9 (each file is patched on its own: the output tells you which are applied, done or blocked)
cat << 'PATCHEOF' > ~/cascade_ws/patch_verify.py
import os, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (paste it first)")

# ------------------------------------------------------------------ registry builder: argument counts
BUILDER = [
 (r'''    sigs.sort()
    methods.sort()
''',
  r'''    sigs.sort()
    methods.sort()
    var sig_args := {}
    for s in ClassDB.class_get_signal_list(cls):
        var tl: Array = []
        for x in s["args"]:
            tl.append(type_string(int(x["type"])))
        sig_args[str(s["name"])] = tl
    var method_args := {}
    if scr != null:
        for s in scr.get_script_signal_list():
            var tl2: Array = []
            for x in s["args"]:
                tl2.append(type_string(int(x["type"])))
            sig_args[str(s["name"])] = tl2
        for m in scr.get_script_method_list():
            method_args[str(m["name"])] = m["args"].size()
'''),
 (r'''        "signals": sigs, "methods": methods,
''',
  r'''        "signals": sigs, "methods": methods, "signal_args": sig_args, "method_args": method_args,
'''),
 (r'''"signals": h["signals"], "methods": h["methods"],
''',
  r'''"signals": h["signals"], "methods": h["methods"], "signal_args": h["signal_args"], "method_args": h["method_args"],
'''),
]

def rag_edit(d):
    d["slots"]["signal_args"] = {"type": "map", "key": "string", "value": "array of argument type names", "source": "godot_class_db_and_script"}
    d["slots"]["method_args"] = {"type": "map", "key": "string", "value": "argument count", "source": "godot_script"}

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 9: Slot 5 VERIFY (behavior probe), chain, process-start GI_2
CLOCK_OPS = ("modify_property", "connect_signal", "add_method")

def model_loaded(model):
    b = sys.modules.get("bench")
    return bool(b is not None and getattr(b, "_loaded", {}).get("key") == model)

PROBE_GD = r"""extends SceneTree
# behavior probe. args: out, scene, node name, mode (signal|call), name, method, props (json), argument types (json)
var spy := 0

func _count(_s: String) -> void:
    spy += 1

func _snap(n: Node, props: Array) -> Dictionary:
    var d := {}
    for k in props:
        d[k] = str(n.get(k))
    return d

func _dummy(t: String) -> Variant:
    match t:
        "bool":
            return false
        "int":
            return 0
        "float":
            return 0.0
        "String":
            return ""
        "Vector2":
            return Vector2.ZERO
        "Vector3":
            return Vector3.ZERO
        "Color":
            return Color.BLACK
    return null

func _run(label: String, a: PackedStringArray, props: Array, types: Array, stimulus: bool) -> Dictionary:
    var mode: String = a[3]
    var nm: String = a[4]
    var main: Node = load(a[1]).instantiate()
    var p: Node = main.get_node("%" + a[2])
    var args: Array = []
    for t in types:
        args.append(_dummy(str(t)))
    spy = 0
    if mode == "signal":
        var cb: Callable = Callable(self, "_count").bind(nm)
        p.connect(nm, cb.unbind(types.size()) if types.size() > 0 else cb)
        if not stimulus:
            var h: Callable = Callable(p, a[5])
            if p.is_connected(nm, h):
                p.disconnect(nm, h)
    var res := {"label": label, "pre": _snap(p, props), "explicit": false, "natural_emissions": 0}
    print("VERIFY>> ", label)
    root.add_child(main)
    await process_frame
    if mode == "call":
        if stimulus:
            p.callv(nm, args)
    else:
        root.remove_child(main)
        await process_frame
        res["natural_emissions"] = spy
        if spy == 0:
            res["explicit"] = true
            root.add_child(main)
            await process_frame
            p.callv("emit_signal", [nm] + args)
            root.remove_child(main)
            await process_frame
    res["emissions"] = spy
    res["post"] = _snap(p, props)
    print("VERIFY<< ", label)
    main.free()
    return res

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var props: Array = JSON.parse_string(a[6])
    var types: Array = JSON.parse_string(a[7])
    var normal: Dictionary = await _run("normal", a, props, types, true)
    var control: Dictionary = await _run("control", a, props, types, false)
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify({"normal": normal, "control": control}))
    f.close()
    quit()
"""

def handler_expectation(project, host, block):
    """What the handler is supposed to do, read from its body: ('pass',), ('print', message) or ('unknown',)."""
    if block["op"] == "add_method":
        return ("pass",) if block["body"] == "pass" else ("print", block["message"])
    have = existing_function(open(script_path(project, host), encoding="utf-8").read(), block["method"])
    if not have or len(have) != 2: return ("unknown",)
    if have[1] == "pass": return ("pass",)
    m = re.match(r'^print\((["\'])(.*)\1\)$', have[1])
    return ("print", m.group(2)) if m else ("unknown",)

def _segment(out, label):
    m = re.search(rf"VERIFY>> {label}\n(.*?)VERIFY<< {label}", out, re.S)
    return m.group(1).splitlines() if m else []

def _split_lines(seg):
    errors = [l.strip() for l in seg if l.startswith(("ERROR", "SCRIPT ERROR"))]
    prints = [l for l in seg if l.strip() and not l.startswith(("ERROR", "SCRIPT ERROR")) and not l.strip().startswith("at:")]
    return prints, errors

def run_probe(godot, project, host, block):
    """Slot 5: run the scene headless and compare a normal run with a control run. effect = normal minus control."""
    mode = "signal" if block["op"] == "connect_signal" else "call"
    name = block["signal"] if mode == "signal" else block["fn"]
    method = block["method"] if mode == "signal" else block["fn"]
    types = list((host.get("signal_args") or {}).get(name, [])) if mode == "signal" else [x.split(": ")[1] for x in block["args"]]
    exp = handler_expectation(project, host, block)
    tmp = tempfile.mkdtemp(prefix="cascade_probe_")
    helper, out = os.path.join(tmp, "probe.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(PROBE_GD)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), mode, name, method,
                            json.dumps(sorted(host["props"])), json.dumps(types)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return {"verdict": "FAIL", "mode": mode, "summary": "the probe timed out (60 s)", "warnings": []}
    text = strip_ansi(r.stdout)
    if not os.path.exists(out):
        return {"verdict": "FAIL", "mode": mode, "summary": "the probe could not run: " + (godot_errors(text) or text[-200:].strip()), "warnings": []}
    res = json.load(open(out)); nrm, ctl = res["normal"], res["control"]
    pn, en = _split_lines(_segment(text, "normal")); pc, ec = _split_lines(_segment(text, "control"))
    eff_prints = collections.Counter(pn) - collections.Counter(pc)
    ch = lambda run: {k: [run["pre"][k], run["post"][k]] for k in run["pre"] if run["pre"][k] != run["post"][k]}
    chn, chc = ch(nrm), ch(ctl)
    eff_props = {k: v for k, v in chn.items() if chc.get(k) != v}
    eff_errs = list((collections.Counter(en) - collections.Counter(ec)).elements())
    mine = [e for e in eff_errs if (f"signal '{name}'" in e or f"::{method}" in e)] if mode == "signal" else [e for e in eff_errs if name in e]
    other = [e[:160] for e in eff_errs if e not in mine]
    how = "explicit emit" if nrm.get("explicit") else "natural"
    v = {"mode": mode, "name": name, "natural": not nrm.get("explicit"), "explicit": bool(nrm.get("explicit")), "emissions": nrm["emissions"], "expect": list(exp),
         "effect": {"prints": dict(eff_prints), "props": eff_props}, "errors_mine": [e[:200] for e in mine], "warnings": ["another connection in this scene reports: " + e for e in other]}
    where = f"{mode} {name}" + (f" -> {method}()" if mode == "signal" else "()")
    if mode == "signal" and nrm["emissions"] == 0:
        v.update(verdict="FAIL", summary=f"{where}: the signal never fired, not even when emitted explicitly")
    elif mine:
        v.update(verdict="FAIL", summary=f"{where}: Godot reports an error from this connection: {mine[0][:150]}")
    elif exp[0] == "pass":
        if eff_prints or eff_props: v.update(verdict="FAIL", summary=f"{where}: the body is pass but the run changed something: {dict(eff_prints) or eff_props}")
        else: v.update(verdict="PASS", summary=f"{where}: no effect and no error, as a pass body should ({nrm['emissions']} emission(s), {how})" if mode == "signal" else f"{where}: ran with no effect and no error, as a pass body should")
    elif exp[0] == "print":
        n = eff_prints.get(exp[1], 0)
        if n < 1: v.update(verdict="FAIL", summary=f"{where}: expected the handler to print {exp[1]!r} but the normal run printed nothing more than the control run")
        elif eff_props: v.update(verdict="FAIL", summary=f"{where}: the handler printed {exp[1]!r} but also changed {eff_props}")
        else: v.update(verdict="PASS", summary=f"{where}: printed {exp[1]!r} x{n} (control 0)" + (f"; {nrm['emissions']} emission(s), {how}" if mode == "signal" else ""))
    else:
        v.update(verdict="UNVERIFIED", summary=f"{where}: the handler body is not pass/print, so its effect was not asserted; no error from this connection ({nrm['emissions']} emission(s), {how})")
    return v

def verify_line(v):
    return f"{v['verdict']:<10} {v['summary']}"

OK_STATUS = ("ACK", "APPLIED", "NOOP_ALREADY_CONNECTED", "NOOP_ALREADY_DEFINED", "VERIFY_PASS", "VERIFY_UNVERIFIED")

def run_chain(a):
    """Several blocks in one call: one process, the model stays loaded; the spec, registry and scene are re-read for every block."""
    ws = os.path.abspath(os.path.expanduser(a.ws)); ledger = os.path.join(ws, "ledger.jsonl")
    blocks, rest, prog, rows, first_bad = list(a.block), [x for x in sys.argv[1:] if x not in a.block], sys.argv[0], [], 0
    def lines(): return open(ledger, encoding="utf-8").read().splitlines() if os.path.exists(ledger) else []
    for i, path in enumerate(blocks, 1):
        print(f"=== block {i}/{len(blocks)}: {path}", flush=True)
        before, t0 = len(lines()), time.time(); sys.argv = [prog, path] + rest
        try:
            main(); code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str): print(e.code)
        new = lines()[before:]; row = json.loads(new[-1]) if new else {}
        rows.append((os.path.basename(path), row.get("op", "?"), row.get("status", "STOPPED"), row.get("tries", "-"), time.time() - t0))
        if rows[-1][2] not in OK_STATUS:
            first_bad = first_bad or code or 1
            if not a.keep_going:
                print(f"=== stopping: block {i} did not succeed (--keep-going continues with the next block)"); break
    print("\n=== chain summary")
    for n, o, s, t, sec in rows: print(f"  {n:<28} {o:<16} {s:<24} tries {t}   {sec:5.1f}s")
    sys.exit(first_bad)

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
CLOCK = [
 (MARK, NEW_SECTION + MARK),
 # GI_2: the VRAM measurement is taken once, before the model is loaded
 ("def gi2(spec, mule_dir, model):\n", "def gi2(spec, mule_dir, model, skip_vram=False):\n"),
 (r'''    bad = []
    if vram is not None and vram > lim["vram_gb_max"]:''',
  r'''    if skip_vram: vram = None                       # the model is already loaded in this process: its own memory sits near the limit
    bad = []
    if vram is not None and vram > lim["vram_gb_max"]:'''),
 (r'''    return bad, {"vram_gb_used": None if vram is None else round(vram, 2), "ram_cache_gb": ram}
''',
  r'''    return bad, {"vram_gb_used": None if vram is None else round(vram, 2), "ram_cache_gb": ram, **({"vram_check": "skipped, model already loaded in this process"} if skip_vram else {})}
'''),
 ("    bad, meas = gi2(spec, a.mule_dir, a.model)\n", "    bad, meas = gi2(spec, a.mule_dir, a.model, model_loaded(a.model))\n"),
 # the clock knows which ops it has code for
 (r'''    s, bad = spec["slots"], []
    want = spec["ops"].get(block.get("op"), {}).get("slots") or list(s)
''',
  r'''    s, bad = spec["slots"], []
    if block.get("op") in CLOCK_OPS and spec["ops"].get(block["op"], {}).get("status") != "locked":
        return None, [f"the spec is behind the clock: it has no locked '{block['op']}' op. Run: python3 patch_status.py"]
    want = spec["ops"].get(block.get("op"), {}).get("slots") or list(s)
'''),
 # GI_1: a handler must take as many arguments as the signal carries
 (r'''    return bad

def connection_exists(project, host, block):
''',
  r'''    sa, ma = host.get("signal_args"), host.get("method_args")
    if sa is None or ma is None: bad.append("GI_1 registry has no 'signal_args'/'method_args'; update and re-run registry_builder.py")
    elif block["signal"] in sa and block["method"] in ma and len(sa[block["signal"]]) != ma[block["method"]]:
        bad.append(f"GI_1 arity: {block['signal']} carries {len(sa[block['signal']])} argument(s) but {block['method']}() takes {ma[block['method']]}; Godot only reports this when the signal fires")
    return bad

def connection_exists(project, host, block):
'''),
 # command line
 ('    ap.add_argument("block")\n',
  '    ap.add_argument("block", nargs="+", help="one or more block files; several run as a chain in one process")\n'
  '    ap.add_argument("--no-verify", action="store_true", help="skip Slot 5 (the behavior check)")\n'
  '    ap.add_argument("--verify-only", action="store_true", help="run only the behavior check on a block that is already applied: no mule, nothing written")\n'
  '    ap.add_argument("--keep-going", action="store_true", help="in a chain, continue after a block that did not succeed")\n'),
 ("    if a.ultra: a.slim = False\n", "    if a.ultra: a.slim = False\n    if len(a.block) > 1: return run_chain(a)\n    a.block = a.block[0]\n"),
 # --verify-only
 (r'''    if block["op"] == "connect_signal" and connection_exists(project, host, block):
''',
  r'''    if a.verify_only:
        if block["op"] not in ("connect_signal", "add_method"):
            log(f"RESULT: nothing to verify for op {block['op']} (its value is read back at apply time)"); finish(0, status="VERIFY_NOTHING", uid=host["uid"], **op_fields(block))
        vr = run_probe(a.godot, project, host, block); log("SLOT_5 VERIFY   " + verify_line(vr))
        for w in vr.get("warnings", []): log("                warning: " + w)
        log(f"RESULT: VERIFY {vr['verdict']} (verify only, nothing was written)"); finish(5 if vr["verdict"] == "FAIL" else 0, status="VERIFY_" + vr["verdict"], verify=vr, uid=host["uid"], **op_fields(block))
    if block["op"] == "connect_signal" and connection_exists(project, host, block):
'''),
 # Slot 5 inside the apply path, after the reload check and before the registry refresh
 (r'''                            status, extra = "APPLIED", {"apply": res}
                            if block["op"] == "add_method":
''',
  r'''                            status, extra = "APPLIED", {"apply": res}
                            if not a.no_verify and block["op"] in ("connect_signal", "add_method"):
                                vr = run_probe(a.godot, project, host, block); extra["verify"] = vr
                                log("SLOT_5 VERIFY   " + verify_line(vr))
                                for w in vr.get("warnings", []): log("                warning: " + w)
                                if vr["verdict"] == "FAIL":
                                    shutil.copy2(res["backup"], os.path.join(project, res["file"][len("res://"):]))
                                    log("SLOT_5 VERIFY   FAILED, ROLLED BACK  the edit was undone")
                                    status, code = "ROLLED_BACK_VERIFY", 4
                            if block["op"] == "add_method" and status == "APPLIED":
'''),
]
entries = [
 {"file": "cascade_clock.py", "marker": "def run_probe", "edits": CLOCK},
 {"file": "registry_builder.py", "marker": '"signal_args": sig_args', "edits": BUILDER},
 {"file": "rag_record_spec.json", "done": lambda d: "signal_args" in d["slots"] and "method_args" in d["slots"], "edit": rag_edit},
]
sys.exit(1 if pl.run(ws, "verify", entries) else 0)
PATCHEOF
python3 ~/cascade_ws/patch_verify.py ~/cascade_ws

# ---- 3. rebuild the registry (it now records argument counts) and show the status of every patch
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 registry_builder.py
python3 patch_status.py

# ---- 4. demo blocks: a handler for 'renamed' (a signal that never fires by itself), its connection, and a handler/connection pair that GI-1 must refuse
cat << 'EOF' > block_method_4000.json
{"frame_id":"0x4000","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"add_method","target":"%Player","fn":"_on_player_renamed","args":[],"returns":"void","body":"print","message":"renamed","ref":"10.1.1.10","exclude":["KinematicBody2D","untyped_variables","yield","relative_paths_up_tree","cross_subnet_queries"]}
EOF
cat << 'EOF' > block_connect_4001.json
{"frame_id":"0x4001","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"connect_signal","source":"%Player","target":"%Player","signal":"renamed","method":"_on_player_renamed","ref":"10.1.1.10","exclude":["KinematicBody2D","connect_3arg","untyped_variables","relative_paths_up_tree","cross_subnet_queries"]}
EOF
cat << 'EOF' > block_method_4002.json
{"frame_id":"0x4002","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"add_method","target":"%Player","fn":"_on_player_child_entered_tree","args":[],"returns":"void","body":"print","message":"child","ref":"10.1.1.10","exclude":["KinematicBody2D","untyped_variables","yield","relative_paths_up_tree","cross_subnet_queries"]}
EOF
cat << 'EOF' > block_connect_4003.json
{"frame_id":"0x4003","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"connect_signal","source":"%Player","target":"%Player","signal":"child_entered_tree","method":"_on_player_child_entered_tree","ref":"10.1.1.10","exclude":["KinematicBody2D","connect_3arg","untyped_variables","relative_paths_up_tree","cross_subnet_queries"]}
EOF

# ---- 5. no GPU, nothing written: run the behavior check on the chain you already applied
python3 cascade_clock.py block_method_0001.json --verify-only
python3 cascade_clock.py block_connect_0002.json --verify-only
