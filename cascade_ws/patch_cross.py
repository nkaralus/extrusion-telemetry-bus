import json, os, shutil, subprocess, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (it came with the Step 9 script)")
try:
    if "def statements_of" not in open(os.path.join(ws, "cascade_clock.py"), encoding="utf-8").read():
        sys.exit("STOPPED: Step 13 is not in cascade_clock.py yet. Run patch_multi.py first.")
except FileNotFoundError:
    sys.exit("STOPPED: cascade_clock.py is not in " + ws)

# ------------------------------------------------------------------ the spec: connect_signal may name a source that is not the target
def spec_edit(d):
    op = d["ops"]["connect_signal"]
    op["v2"] = ("source may differ from target: the signal is emitted by source, the handler lives on target (ref is the target's address). Both must be registry hosts in the same scene "
                "and the same subnet; the method is _on_<source>_<signal> and must already exist on the target; the harness writes [connection signal from=<source> to=<target> method]; "
                "the mule only completes the method name after %<source>.<signal>.connect(")
    op["cross_node"] = True

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 14: cross-node wiring (source != target)
# Everything below WRAPS an earlier definition: a connect_signal block whose source equals its target keeps calling the code proven in Steps 7 to 13.
_REG = {"hosts": {}}
_step13_slot1 = slot1

def slot1(block, reg, spec, project):
    global _REG
    _REG = reg
    return _step13_slot1(block, reg, spec, project)

def is_cross(block):
    return block.get("op") == "connect_signal" and block.get("source") != block.get("target")

def host_by_name(name):
    for h in _REG.get("hosts", {}).values():
        if h["name"] == name: return h
    return None

def host_by_rel(rel):
    for h in _REG.get("hosts", {}).values():
        if node_rel(h) == rel: return h
    return None

def cross_method_name(block): return f"_on_{snake(block['source'])}_{block['signal']}"

def cross_stmt(block): return f"%{block['source'].lstrip('%')}.{block['signal']}.connect({block['method']})"

_step13_connect_checks = connect_checks

def connect_checks(block, host, spec, project):
    if not is_cross(block): return _step13_connect_checks(block, host, spec, project)
    bad = []
    for k in ("source", "signal", "method"):
        if not re.match(spec["slots"][k]["rx"], str(block[k])): bad.append(f"block.{k} '{block[k]}' does not match the spec")
    if block["target"] != host["name"]: bad.append(f"GI_1 addr_in_registry: {block['ref']} is {host['name']}, block says target {block['target']}")
    src = host_by_name(block["source"])
    if src is None:
        bad.append(f"GI_1 source: {block['source']} is not in the registry (nodes: {', '.join(sorted(h['name'] for h in _REG.get('hosts', {}).values()))})")
    else:
        if src["subnet"] != host["subnet"]: bad.append(f"GI_1 subnet_boundary: {block['source']} is in {src['subnet']} but {host['name']} is in {host['subnet']}")
        if src["owner_scene"] != host["owner_scene"]: bad.append(f"GI_1 connect_scope: {block['source']} is in {src['owner_scene']} and {host['name']} in {host['owner_scene']}; a connection is written into one scene, so both nodes must be in the same scene")
    for k in ("signals", "methods"):
        if k not in host: bad.append(f"GI_1 registry has no '{k}' field on {host['name']}; update and re-run registry_builder.py")
    if bad: return bad
    if block["signal"] not in src.get("signals", []):
        bad.append(f"GI_1 signal: '{block['signal']}' is not a signal of {src['name']} ({src['node']}); it has {len(src.get('signals', []))}, e.g. {', '.join(src.get('signals', [])[:6])}")
    want = cross_method_name(block)
    if block["method"] != want: bad.append(f"GI_1 method_name: a handler for {src['name']}.{block['signal']} must be called {want} (the _on_<source>_<signal> convention)")
    if block["method"] not in host["methods"]:
        bad.append(f"GI_1 method: '{block['method']}' is not defined in {host.get('script') or 'the node'}; add it first with add_method on {host['name']} (defined: {', '.join(host['methods']) or 'none'})")
    sa, ma = src.get("signal_args"), host.get("method_args")
    if sa is None or ma is None: bad.append("GI_1 registry has no 'signal_args'/'method_args'; update and re-run registry_builder.py")
    elif block["signal"] in sa and block["method"] in ma and len(sa[block["signal"]]) != ma[block["method"]]:
        bad.append(f"GI_1 arity: {block['signal']} carries {len(sa[block['signal']])} argument(s) but {block['method']}() takes {ma[block['method']]}; Godot only reports this when the signal fires")
    return bad

_step13_gi3 = gi3

def gi3(reply, block, spec):
    if not is_cross(block): return _step13_gi3(reply, block, spec)
    if reply is None: return "json_parse: no JSON object with a 'stmt' key in the answer"
    if reply.get("frame_id") != block["frame_id"] or reply.get("target") != block["target"]:
        return "slot_enum_match: frame_id and target must echo the block"
    stmt = reply.get("stmt")
    if not isinstance(stmt, str) or not stmt.strip(): return "slot_enum_match: stmt must be a non-empty string"
    stmt = stmt.strip()
    if "\n" in stmt: return "slot_enum_match: stmt must be exactly one line"
    want = cross_stmt(block)
    if stmt not in (want, want.replace("(" + block["method"], "(self." + block["method"])):
        return f"type_match: stmt must be {want} (self. before the method is also accepted)"
    for name in block["exclude"]:
        chk = spec["exclude_checks"][name]
        if chk["kind"] == "regex" and re.search(chk["rx"], stmt): return f"exclude_hit: {name}"
        if chk["kind"] == "code" and re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", stmt): return f"exclude_hit: {name} (no addresses inside a statement)"
    return None

_step13_canonical_stmt = canonical_stmt

def canonical_stmt(stmt, block):
    if is_cross(block): return cross_stmt(block)
    return _step13_canonical_stmt(stmt, block)

_step13_connection_line = connection_line

def connection_line(block, host):
    if not is_cross(block): return _step13_connection_line(block, host)
    src = host_by_name(block["source"])
    return f'[connection signal="{block["signal"]}" from="{node_rel(src)}" to="{node_rel(host)}" method="{block["method"]}"]'

_step13_connection_exists = connection_exists

def connection_exists(project, host, block):
    if not is_cross(block): return _step13_connection_exists(project, host, block)
    src = host_by_name(block["source"])
    fp = os.path.join(project, host["owner_scene"][len("res://"):])
    if src is None or not os.path.exists(fp): return False
    want = {"signal": block["signal"], "from": node_rel(src), "to": node_rel(host), "method": block["method"]}
    for l in open(fp, encoding="utf-8").read().splitlines():
        if l.startswith("[connection "):
            at = dict(re.findall(r'(\w+)="([^"]*)"', l))
            if all(at.get(k) == v for k, v in want.items()): return True
    return False

RELOAD_CROSS_HELPER = r"""extends SceneTree

func _init() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var res := {"found": false, "connections": []}
    var packed: PackedScene = load(a[1])
    if packed != null:
        var root: Node = packed.instantiate()
        var s: Node = root.get_node_or_null("%" + a[2])
        var t: Node = root.get_node_or_null("%" + a[4])
        if s != null and t != null:
            res["found"] = true
            for c in s.get_signal_connection_list(a[3]):
                var cb: Callable = c["callable"]
                res["connections"].append([str(cb.get_method()), cb.get_object() == t])
        root.free()
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(res))
    f.close()
    quit()
"""

_step13_reload_check_connection = reload_check_connection

def reload_check_connection(godot, project, host, block):
    if not is_cross(block): return _step13_reload_check_connection(godot, project, host, block)
    src = host_by_name(block["source"])
    tmp = tempfile.mkdtemp(prefix="cascade_reload_")
    helper, out = os.path.join(tmp, "reload.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(RELOAD_CROSS_HELPER)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], src["name"].lstrip("%"), block["signal"], host["name"].lstrip("%")],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return False, None, "reload timeout (60 s)"
    if not os.path.exists(out): return False, None, "Godot could not reload the scene:\n" + strip_ansi(r.stdout + r.stderr)[-400:]
    res = json.load(open(out))
    if not res.get("found"): return False, None, f"{src['name']} or {host['name']} was not found in {host['owner_scene']} after the edit"
    got = res["connections"]; ok = any(m == block["method"] and mine for m, mine in got)
    return ok, got, "" if ok else f"the scene loaded but {src['name']}.{block['signal']} is not connected to {host['name']}.{block['method']} (connections: {got})"

_step13_apply_scene = apply_scene

def apply_scene(godot, project, host, block):
    res = _step13_apply_scene(godot, project, host, block)
    if is_cross(block): res["what"] = f"{block['source']}.{block['signal']} -> {host['name']}.{block['method']}"
    return res

def connections_into(project, host, fn):
    """Every scene connection that ends in host.fn: [(from path, signal)], same-node ones included."""
    fp = os.path.join(project, host["owner_scene"][len("res://"):]); out = []
    if not os.path.exists(fp): return out
    rel = node_rel(host)
    for l in open(fp, encoding="utf-8").read().splitlines():
        if l.startswith("[connection "):
            at = dict(re.findall(r'(\w+)="([^"]*)"', l))
            pair = (at.get("from"), at.get("signal"))
            if at.get("to") == rel and at.get("method") == fn and pair not in out: out.append(pair)
    return out

def reverify_block(project, host, block, frm, sg):
    """The connect_signal block that re-checks a handler through one connection it already has, or None when the emitter is not a registry host."""
    src = host if frm == node_rel(host) else host_by_rel(frm)
    if src is None: return None
    return {"op": "connect_signal", "signal": sg, "method": block["fn"], "target": block["target"], "source": src["name"], "ref": block["ref"], "subnet": block["subnet"], "frame_id": block["frame_id"], "time_slot": block["time_slot"], "exclude": []}

PROBE_CROSS_GD = r"""extends SceneTree
# cross-node behavior probe. args: out, scene, target name, source name, signal, method, props (json), argument types (json), start values (json)
var spy := 0

func _count(_s: String) -> void:
    spy += 1

func _j(v: Variant) -> Variant:
    match typeof(v):
        TYPE_VECTOR2:
            return [v.x, v.y]
        TYPE_VECTOR3:
            return [v.x, v.y, v.z]
        TYPE_COLOR:
            return [v.r, v.g, v.b, v.a]
    return v

func _from_j(t: String, v: Variant) -> Variant:
    match t:
        "int":
            return int(v)
        "Vector2":
            return Vector2(v[0], v[1])
        "Vector3":
            return Vector3(v[0], v[1], v[2])
        "Color":
            return Color(v[0], v[1], v[2], v[3])
    return v

func _snap(n: Node, props: Array) -> Dictionary:
    var d := {}
    for k in props:
        d[k] = _j(n.get(k))
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
    var nm: String = a[4]
    var method: String = a[5]
    var main: Node = load(a[1]).instantiate()
    var p: Node = main.get_node("%" + a[2])
    var src: Node = main.get_node("%" + a[3])
    var args: Array = []
    for t in types:
        args.append(_dummy(str(t)))
    spy = 0
    p.set_process(false)
    p.set_physics_process(false)
    if src.has_method("stop"):
        src.call("stop")
    for nd in [src, p]:
        for sg in nd.get_signal_list():
            for cn in nd.get_signal_connection_list(str(sg["name"])):
                var cb2: Callable = cn["callable"]
                if cb2.get_object() == p and str(cb2.get_method()).begins_with("_on_") and not (nd == src and str(sg["name"]) == nm and str(cb2.get_method()) == method):
                    nd.disconnect(str(sg["name"]), cb2)
    var pt: Variant = JSON.parse_string(a[8])
    if pt != null:
        var items: Array = pt if pt is Array else [pt]
        for it in items:
            p.set(str(it["prop"]), _from_j(str(it["type"]), it["value"]))
    var cb: Callable = Callable(self, "_count").bind(nm)
    src.connect(nm, cb.unbind(types.size()) if types.size() > 0 else cb)
    var h: Callable = Callable(p, method)
    var wired: bool = src.is_connected(nm, h)
    if not stimulus and wired:
        src.disconnect(nm, h)
    var res := {"label": label, "pre": _snap(p, props), "explicit": false, "natural_emissions": 0, "wired": wired}
    print("VERIFY>> ", label)
    root.add_child(main)
    await process_frame
    root.remove_child(main)
    await process_frame
    res["natural_emissions"] = spy
    if spy == 0:
        res["explicit"] = true
        root.add_child(main)
        await process_frame
        src.callv("emit_signal", [nm] + args)
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

def run_probe_cross(godot, project, host, block):
    """Slot 5 for a connection whose emitter is another node: emit the source's signal, and compare a normal run (handler wired) with a control run (handler unplugged)."""
    src = host_by_name(block["source"])
    name, method = block["signal"], block["method"]
    types = list((src.get("signal_args") or {}).get(name, []))
    where = f"signal {src['name']}.{name} -> {host['name']}.{method}()"
    sts = function_statements(open(script_path(project, host), encoding="utf-8").read(), method, host)
    start = start_values(sts) if sts else {}
    perturb = [{"prop": p, "type": next(s["type"] for s in sts if s.get("property") == p), "value": v} for p, v in start.items()] or None
    tmp = tempfile.mkdtemp(prefix="cascade_probe_")
    helper, out = os.path.join(tmp, "probe.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(PROBE_CROSS_GD)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), src["name"].lstrip("%"), name, method,
                            json.dumps(sorted(host["props"])), json.dumps(types), json.dumps(perturb)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return {"verdict": "FAIL", "mode": "signal", "summary": "the probe timed out (60 s)", "warnings": []}
    text = strip_ansi(r.stdout)
    if not os.path.exists(out):
        return {"verdict": "FAIL", "mode": "signal", "summary": "the probe could not run: " + (godot_errors(text) or text[-200:].strip()), "warnings": []}
    res = json.load(open(out)); nrm, ctl = res["normal"], res["control"]
    pn, en = _split_lines(_segment(text, "normal")); pc, ec = _split_lines(_segment(text, "control"))
    eff_prints = collections.Counter(pn) - collections.Counter(pc)
    ch = lambda run: {k: [run["pre"][k], run["post"][k]] for k in run["pre"] if run["pre"][k] != run["post"][k]}
    chn, chc = ch(nrm), ch(ctl)
    eff_props = {k: v for k, v in chn.items() if chc.get(k) != v}
    eff_errs = list((collections.Counter(en) - collections.Counter(ec)).elements())
    mine = [e for e in eff_errs if (f"signal '{name}'" in e or f"::{method}" in e)]
    other = [e[:160] for e in eff_errs if e not in mine]
    how = "explicit emit" if nrm.get("explicit") else "natural"
    n = nrm["emissions"]
    v = {"mode": "signal", "name": name, "source": src["name"], "natural": not nrm.get("explicit"), "explicit": bool(nrm.get("explicit")), "emissions": n, "expect": sts or [],
         "effect": {"prints": dict(eff_prints), "props": eff_props}, "errors_mine": [e[:200] for e in mine], "warnings": ["another connection in this scene reports: " + e for e in other]}
    if not nrm.get("wired"):
        v.update(verdict="FAIL", summary=f"{where}: the scene does not connect them: {method} is not wired to {src['name']}.{name} when the scene loads"); return v
    if n == 0:
        v.update(verdict="FAIL", summary=f"{where}: the signal never fired, not even when emitted explicitly"); return v
    if mine:
        v.update(verdict="FAIL", summary=f"{where}: Godot reports an error from this connection: {mine[0][:150]}"); return v
    if sts is None:
        v.update(verdict="UNVERIFIED", summary=f"{where}: wired and fired ({n} emission(s), {how}), no error; the handler body is not pass, print, an assignment or a compound statement, so its effect was not asserted"); return v
    want_vals, want_prints = simulate(sts, start, n)
    problems = []
    for p in start:
        post_n, post_c = nrm["post"].get(p), ctl["post"].get(p)
        if not _close(post_n, want_vals[p]): problems.append(f"{p} should go {start[p]} -> {want_vals[p]} but the normal run ended at {post_n}")
        elif not _close(post_c, start[p]): problems.append(f"the control run moved {p} from {start[p]} to {post_c}, so the change cannot be credited to this handler")
    others = {k: x for k, x in eff_props.items() if k not in start}
    if others: problems.append(f"the run also changed {others}")
    if dict(eff_prints) != dict(want_prints): problems.append(f"expected the prints {dict(want_prints) or 'none'} but the normal run printed {dict(eff_prints) or 'nothing'} more than the control run")
    if problems:
        v.update(verdict="FAIL", summary=f"{where}: {len(sts)} statement(s) x{n}: " + "; ".join(problems)); return v
    parts = [f"{p} {start[p]} -> {nrm['post'].get(p)}" for p in start] + [f"printed {m!r} x{c}" for m, c in want_prints.items()]
    v.update(verdict="PASS", summary=f"{where}: wired; {len(sts)} statement(s) ran {n} time(s): " + (", ".join(parts) or "no effect and no error, as a pass body should") + "; the control (handler unplugged) changed none of it; " + how)
    return v

_step13_run_probe = run_probe

def run_probe(godot, project, host, block):
    if is_cross(block): return run_probe_cross(godot, project, host, block)
    return _step13_run_probe(godot, project, host, block)

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
OLD_HEAD = """    elif block["op"] == "connect_signal": stmt_head = f"{block['signal']}.connect("
"""
NEW_HEAD = """    elif block["op"] == "connect_signal": stmt_head = (f"%{block['source'].lstrip('%')}." if block["source"] != block["target"] else "") + f"{block['signal']}.connect("
"""
OLD_LOOP = """                        for sg in connections_of(project, host, block["fn"]):
                            cb = {"op": "connect_signal", "signal": sg, "method": block["fn"], "target": block["target"], "source": block["target"], "ref": block["ref"], "subnet": block["subnet"], "frame_id": block["frame_id"], "time_slot": block["time_slot"], "exclude": []}
"""
NEW_LOOP = """                        for frm, sg in connections_into(project, host, block["fn"]):
                            cb = reverify_block(project, host, block, frm, sg)
                            if cb is None:
                                log(f"SLOT_5 VERIFY   (the connection from {frm} ({sg}) comes from a node that is not in the registry, so it was not re-checked)"); continue
"""
OLD_FRAME = """    log(f"FRAME {block.get('frame_id')}  {block.get('op')}  {block.get('target')}.{block.get('property') or block.get('signal') or block.get('fn')}")
"""
NEW_FRAME = """    log(f"FRAME {block.get('frame_id')}  {block.get('op')}  " + (f"{block['source']}.{block['signal']} -> " if block.get('op') == 'connect_signal' and block.get('source') != block.get('target') else "") + f"{block.get('target')}.{block.get('property') or (block.get('method') if block.get('op') == 'connect_signal' and block.get('source') != block.get('target') else block.get('signal')) or block.get('fn')}")
"""
CLOCK = [(MARK, NEW_SECTION + MARK), (OLD_HEAD, NEW_HEAD), (OLD_LOOP, NEW_LOOP), (OLD_FRAME, NEW_FRAME)]

# ------------------------------------------------------------------ the scene fixture: a %Timer next to %Player in main.tscn
TIMER_NODE = '[node name="Timer" type="Timer" parent="."]\nunique_name_in_owner = true\nwait_time = 2.0\none_shot = true\n'

def fixture():
    path = os.path.join(ws, "engine_test", "scenes", "main.tscn")
    if not os.path.exists(path):
        print(f"  {'main.tscn':<24} MISSING  ({path} not found)"); return 1
    raw = open(path, encoding="utf-8", newline="").read()
    if 'type="Timer"' in raw:
        print(f"  {'main.tscn':<24} done     (a Timer is already in the scene)"); return 0
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)
    conn = [i for i, l in enumerate(lines) if l.startswith("[connection ")]
    edit = [i for i, l in enumerate(lines) if l.startswith("[editable ")]
    at = min(conn + edit) if (conn or edit) else None
    block = TIMER_NODE.rstrip("\n").split("\n") + [""]
    if at is None:
        while lines and not lines[-1].strip(): lines.pop()
        lines += [""] + block[:-1] + [""]
    else:
        lines[at:at] = block
    open(path + ".before-cross", "w", encoding="utf-8", newline="").write(raw)
    open(path, "w", encoding="utf-8", newline="").write(nl.join(lines))
    print(f"  {'main.tscn':<24} applied  (backup main.tscn.before-cross; a one-shot Timer, not started, 2 s)")
    return 0

def pin():
    path = os.path.join(ws, "topology.json")
    if not os.path.exists(path):
        print(f"  {'topology.json':<24} MISSING"); return 1
    d = json.load(open(path, encoding="utf-8"))
    if "%Timer" in d.get("pins", {}):
        print(f"  {'topology.json':<24} done     (%Timer is pinned)"); return 0
    open(path + ".before-cross", "w", encoding="utf-8").write(open(path, encoding="utf-8").read())
    d.setdefault("pins", {})["%Timer"] = "10.1.1.11"
    json.dump(d, open(path, "w", encoding="utf-8"), indent=2)
    print(f"  {'topology.json':<24} applied  (%Timer -> 10.1.1.11)")
    return 0

entries = [
 {"file": "cascade_clock.py", "marker": "def run_probe_cross", "edits": CLOCK},
 {"file": "task_block_spec.json", "done": lambda d: d["ops"]["connect_signal"].get("cross_node") is True, "edit": spec_edit},
]
blocked = pl.run(ws, "cross", entries)
blocked += fixture()
blocked += pin()
if not blocked:
    godot = os.environ.get("GODOT", "godot")
    r = subprocess.run([sys.executable, os.path.join(ws, "registry_builder.py"), "--ws", ws, "--godot", godot], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip().splitlines()
    print("  registry_builder.py:")
    for l in out[-6:]: print("      " + l)
    if r.returncode != 0:
        blocked += 1
        print("  => the registry was NOT rebuilt (is GODOT set? export GODOT=/path/to/godot). Fix it and run this patch again.")
sys.exit(1 if blocked else 0)
