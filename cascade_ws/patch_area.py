import json, os, subprocess, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (it came with the Step 9 script)")
try:
    if "def run_probe_timer" not in open(os.path.join(ws, "cascade_clock.py"), encoding="utf-8").read():
        sys.exit("STOPPED: Step 16 is not in cascade_clock.py yet. Run patch_every.py first.")
except FileNotFoundError:
    sys.exit("STOPPED: cascade_clock.py is not in " + ws)

# ------------------------------------------------------------------ the spec: handler arguments, the guard slot, the free statement
NODE_CLASSES = "Node|Node2D|Node3D|CanvasItem|CollisionObject2D|PhysicsBody2D|CharacterBody2D|RigidBody2D|StaticBody2D|Area2D|CollisionObject3D|PhysicsBody3D|CharacterBody3D|RigidBody3D|StaticBody3D|Area3D"

def spec_edit(d):
    d["slots"]["args"]["item_rx"] = "^[a-z_][a-z0-9_]*: (bool|int|float|String|Vector2|Vector3|Color|" + NODE_CLASSES + ")$"
    d["slots"]["statement"]["kinds"]["free"] = ["target"]
    d["slots"]["statement"]["free"] = ("free: target is self or a registry node in the same scene (%Name); it becomes queue_free() or %Name.queue_free(); at most one per list and it must be the last statement")
    op = d["ops"]["add_method"]
    op.setdefault("optional_slots", [])
    if "guard" not in op["optional_slots"]: op["optional_slots"].append("guard")
    op["v6"] = ("args may be typed with a Node class (the harness writes them from the signal: _body: Node2D); guard 'self' makes the harness write the first line "
                "'if <first argument> != self: return'; the mule never writes a signature or the guard line; statements never read the arguments")

def registry_class_edit(path):
    """registry_builder.py: also record the class name of every signal argument."""
    raw = open(path, encoding="utf-8").read()
    if "signal_arg_classes" in raw:
        print(f"  {'registry_builder.py':<24} done     (already records argument classes)"); return 0
    old1 = '''    var sig_args := {}
    for s in ClassDB.class_get_signal_list(cls):
        var tl: Array = []
        for x in s["args"]:
            tl.append(type_string(int(x["type"])))
        sig_args[str(s["name"])] = tl
'''
    new1 = '''    var sig_args := {}
    var sig_cls := {}
    for s in ClassDB.class_get_signal_list(cls):
        var tl: Array = []
        var cl: Array = []
        for x in s["args"]:
            tl.append(type_string(int(x["type"])))
            cl.append(str(x.get("class_name", "")))
        sig_args[str(s["name"])] = tl
        sig_cls[str(s["name"])] = cl
'''
    old2 = '''            sig_args[str(s["name"])] = tl2
'''
    new2 = '''            sig_args[str(s["name"])] = tl2
            sig_cls[str(s["name"])] = []
            for x in s["args"]:
                sig_cls[str(s["name"])].append(str(x.get("class_name", "")))
'''
    old3 = '''"signals": sigs, "methods": methods, "signal_args": sig_args, "method_args": method_args,
    }'''
    new3 = '''"signals": sigs, "methods": methods, "signal_args": sig_args, "signal_arg_classes": sig_cls, "method_args": method_args,
    }'''
    old4 = '''"signal_args": h["signal_args"], "method_args": h["method_args"],'''
    new4 = '''"signal_args": h["signal_args"], "signal_arg_classes": h.get("signal_arg_classes", {}), "method_args": h["method_args"],'''
    for o in (old1, old2, old3, old4):
        if raw.count(o) != 1:
            print(f"  {'registry_builder.py':<24} BLOCKED  (an expected piece of the file is missing or repeated; nothing was changed)"); return 1
    open(path + ".before-area", "w", encoding="utf-8").write(raw)
    for o, n in ((old1, new1), (old2, new2), (old3, new3), (old4, new4)): raw = raw.replace(o, n)
    open(path, "w", encoding="utf-8").write(raw)
    print(f"  {'registry_builder.py':<24} applied  (backup registry_builder.py.before-area)")
    return 0

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 17: signals that carry arguments (a trigger area, the guard, the free statement)
# Everything below WRAPS or SHADOWS an earlier definition, as in Steps 13 to 16.
NODE_ARG_CLASSES = ("Node", "Node2D", "Node3D", "CanvasItem", "CollisionObject2D", "PhysicsBody2D", "CharacterBody2D", "RigidBody2D", "StaticBody2D", "Area2D",
                    "CollisionObject3D", "PhysicsBody3D", "CharacterBody3D", "RigidBody3D", "StaticBody3D", "Area3D")
PRIMITIVE_ARG_TYPES = ("bool", "int", "float", "String", "Vector2", "Vector3", "Color")
REPEATING_SIGNAL_RX = r"_(entered|exited)$"
GUARD_RX = re.compile(r"^if\s+([A-Za-z_]\w*)\s*!=\s*self\s*:\s*return$")
STATEMENT_KINDS = STATEMENT_KINDS + ("free",)
FREE_EMPTY_NOTE = "a handler that only frees a node is allowed"

# ---- what a signal carries, as the harness writes it into a handler's signature
def signal_arg_types(src, sg):
    """['Node2D'] for body_entered, [] for no arguments, None when the registry has no class names (re-run registry_builder.py)."""
    base = (src.get("signal_args") or {}).get(sg)
    if base is None: return None
    if not base: return []
    cls = (src.get("signal_arg_classes") or {}).get(sg)
    if cls is None or len(cls) != len(base): return None
    return [cls[i] if t == "Object" else t for i, t in enumerate(base)]

def signal_args_supported(types):
    return types is not None and 0 < len(types) <= 3 and all(t in PRIMITIVE_ARG_TYPES or t in NODE_ARG_CLASSES for t in types)

def handler_args_for(sg, types):
    """The signature arguments the harness writes: the first Node argument is named after the signal's first word (_body, _area), the rest _arg1, _arg2 ..."""
    out, used = [], False
    for i, t in enumerate(types):
        if t in NODE_ARG_CLASSES and not used: out.append(f"_{sg.split('_')[0]}: {t}"); used = True
        else: out.append(f"_arg{i + 1}: {t}")
    return out

def arg_class(a): return a.split(":", 1)[1].strip() if ":" in a else ""

def guard_line(block):
    if block.get("guard") != "self" or not block.get("args"): return None
    return f"if {block['args'][0].split(':')[0].strip()} != self: return"

def guard_prefix(block):
    g = guard_line(block) if isinstance(block.get("body"), list) else None
    return [g] if g else []

# ---- statements: free
def free_text(st): return "queue_free()" if st["target"] == "self" else f"{st['target']}.queue_free()"

_step16_statement_line = statement_line

def statement_line(st):
    return free_text(st) if st["kind"] == "free" else _step16_statement_line(st)

_step16_body_lines = body_lines

def body_lines(block):
    lines = _step16_body_lines(block)
    return guard_prefix(block) + lines

_step16_parse_statement_line = parse_statement_line

def parse_statement_line(line, host):
    s = line.strip()
    if re.match(r"^(?:self\.)?queue_free\(\)$", s): return {"kind": "free", "target": "self"}
    m = re.match(r"^(%[A-Za-z_]\w*)\.queue_free\(\)$", s)
    if m: return {"kind": "free", "target": m.group(1)}
    return _step16_parse_statement_line(line, host)

def handler_guard(raw, fn):
    """The argument name a handler's first line guards ('if _body != self: return'), or None."""
    have = existing_function(raw, fn)
    body = [l for l in (have or [])[1:] if l and not l.startswith("#")]
    m = GUARD_RX.match(body[0]) if body else None
    return m.group(1) if m else None

def function_statements(raw, fn, host):
    """The statements of a function, with a leading guard line set aside; None if it is missing or any line is not one of the statement kinds."""
    have = existing_function(raw, fn)
    if not have: return None
    body = [l for l in have[1:] if l and not l.startswith("#")]
    if body and GUARD_RX.match(body[0]): body = body[1:]
    sts = [parse_statement_line(l, host) for l in body]
    return sts if sts and all(sts) else None

_step16_legacy_block = legacy_block

def legacy_block(block, st):
    if st["kind"] != "free": return _step16_legacy_block(block, st)
    b = {k: v for k, v in block.items() if k not in ("body", "message", "property", "type", "operator", "value", "mode", "guard")}
    b.update(body="assign", message="", property="queue_free", type="call", value=st["target"], operator="free")
    return b

def free_line_error(line, st):
    s, want = line.strip(), free_text(st)
    ok = s == want or (st["target"] == "self" and s == "self.queue_free()")
    return None if ok else f"type_match: this line must be {want}" + (" (self. before it is also accepted)" if st["target"] == "self" else "")

_step16_add_method_stmt_error = add_method_stmt_error

def add_method_stmt_error(stmt, block):
    if block.get("body") == "assign" and block.get("operator") == "free":
        lines = stmt.split("\n")
        if len(lines) != 2: return f"type_match: stmt must be exactly two lines (the signature and one indented body line), got {len(lines)}"
        if lines[0] != signature(block): return f"type_match: line 1 must be {signature(block)}"
        if lines[1][:1] not in (" ", "\t"): return "type_match: the body line must be indented"
        return free_line_error(lines[1], {"kind": "free", "target": block["value"]})
    return _step16_add_method_stmt_error(stmt, block)

_step16_limit_head = limit_head

def limit_head(st, indent):
    if st["kind"] == "free": return "" if st["target"] == "self" else indent + st["target"] + "."
    return _step16_limit_head(st, indent)

_step16_statement_errors = statement_errors

def statement_errors(sts, host, spec):
    free = [(i, s) for i, s in enumerate(sts, 1) if isinstance(s, dict) and s.get("kind") == "free"]
    if not free: return _step16_statement_errors(sts, host, spec)
    bad, rest = [], [s for s in sts if not (isinstance(s, dict) and s.get("kind") == "free")]
    if len(sts) > MAX_STATEMENTS: return [f"block.body must hold 1 to {MAX_STATEMENTS} statements, it has {len(sts)}"]
    if len(free) > 1: bad.append("only one free statement is allowed in a list")
    for i, s in free:
        if set(s) != {"kind", "target"}: bad.append(f"statement {i} (free): needs exactly ['kind', 'target']"); continue
        if i != len(sts): bad.append(f"statement {i}: free must be the last statement; anything after a free runs on a node that is already queued for deletion")
        t = s["target"]
        if t != "self":
            h = host_by_name(t) if isinstance(t, str) and t.startswith("%") else None
            if h is None: bad.append(f"statement {i}: free target '{t}' is not self or a registry node (%Name)")
            elif h["owner_scene"] != host["owner_scene"]: bad.append(f"statement {i}: {t} is in {h['owner_scene']} and {host['name']} in {host['owner_scene']}; free works inside one scene")
    if bad: return bad
    return _step16_statement_errors(rest, host, spec) if rest else []

# ---- GI_1: handler arguments and the guard
_step16_add_method_checks = add_method_checks

def add_method_checks(block, host, spec, project):
    bad = _step16_add_method_checks(block, host, spec, project)
    bad = [m.replace("(Type: bool, int, float, String, Vector2, Vector3, Color)", "(Type: bool, int, float, String, Vector2, Vector3, Color, or a Node class such as Node2D or Area2D)") for m in bad]
    args = block.get("args") or []
    if args and block.get("mode") == "replace":
        bad = [m for m in bad if "only handlers without arguments can be replaced" not in m]
    if bad: return bad
    g = block.get("guard")
    if g is not None:
        if g != "self": return [f"block.guard '{g}' not allowed (only 'self')"]
        if not args or arg_class(args[0]) not in NODE_ARG_CLASSES: return ["GI_1 guard: a guard compares the first argument with self, so the handler's first argument must be a Node class"]
        if not isinstance(block.get("body"), list): return ["GI_1 guard: a guard needs a statement-list body"]
    for h in _REG.get("hosts", {}).values():
        pre = f"_on_{snake(h['name'])}_"
        if block["fn"].startswith(pre) and block["fn"][len(pre):] in h.get("signals", []):
            sg = block["fn"][len(pre):]; types = signal_arg_types(h, sg)
            if types is None and (h.get("signal_args") or {}).get(sg):
                bad.append(f"GI_1 registry has no argument classes for {h['name']}.{sg}; re-run registry_builder.py")
            elif types is not None and [arg_class(a) for a in args] != types:
                bad.append(f"GI_1 handler_args: {block['fn']} answers {h['name']}.{sg}, which carries ({', '.join(types) or 'nothing'}), but the block's arguments are ({', '.join(arg_class(a) for a in args) or 'none'})")
            break
    # a handler wired from a physics enter/exit signal repeats: the compounding guard applies unless it frees the emitter
    if not bad and block.get("mode") == "replace" and isinstance(block.get("body"), list):
        for frm, sg in connections_into(project, host, block["fn"]):
            src = host_by_rel(frm)
            if src is not None and src["name"] != host["name"] and src["node"] != "Timer" and re.search(REPEATING_SIGNAL_RX, sg):
                bad += repeating_handler_errors(project, host, block["fn"], f"{src['name']}.{sg}", statements_of(block), emitter=src["name"])
    return bad

# ---- the compounding guard, extended: a free of the emitter makes a handler one-time
_step16_repeating_handler_errors = repeating_handler_errors

def repeating_handler_errors(project, tgt, method, who, sts=None, emitter=None):
    if sts is None: sts = function_statements(open(script_path(project, tgt), encoding="utf-8").read(), method, tgt)
    if sts is not None and emitter and any(s["kind"] == "free" and s["target"] == emitter for s in sts): return []
    return _step16_repeating_handler_errors(project, tgt, method, who, sts)

_step16_connect_checks = connect_checks

def connect_checks(block, host, spec, project):
    bad = _step16_connect_checks(block, host, spec, project)
    if bad or not is_cross(block): return bad
    src = host_by_name(block["source"])
    if src is not None and src["node"] != "Timer" and re.search(REPEATING_SIGNAL_RX, block["signal"]):
        bad += repeating_handler_errors(project, host, block["method"], f"{src['name']}.{block['signal']} (it can fire again every time something enters or leaves)", emitter=src["name"])
    return bad

# ---- Slot 5: probes for handlers that receive arguments
PROBE_ARGS_GD = r"""extends SceneTree
# probe for handlers with arguments. args: out, scene, target, source or -, signal or -, method, props (json), argument classes (json), start values (json), free names (json), run modes (json)
var spy := 0

func _count() -> void:
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

func _arg(t: String, p: Node, wrong: bool, made: Array) -> Variant:
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
    if not wrong and p.is_class(t):
        return p
    if ClassDB.class_exists(t) and ClassDB.can_instantiate(t):
        var n: Variant = ClassDB.instantiate(t)
        made.append(n)
        return n
    return null

func _run(label: String, a: PackedStringArray, props: Array, classes: Array, free_names: Array, mode: String) -> Dictionary:
    var srcn: String = a[3]
    var sg: String = a[4]
    var method: String = a[5]
    var main: Node = load(a[1]).instantiate()
    var p: Node = main.get_node("%" + a[2])
    var src: Node = null
    if srcn != "-":
        src = main.get_node("%" + srcn)
    spy = 0
    p.set_process(false)
    p.set_physics_process(false)
    if src != null:
        if src.has_method("stop"):
            src.call("stop")
        if src.get("autostart") != null:
            src.set("autostart", false)
    for nd in [src, p]:
        if nd == null:
            continue
        for s2 in nd.get_signal_list():
            for cn in nd.get_signal_connection_list(str(s2["name"])):
                var cb2: Callable = cn["callable"]
                if cb2.get_object() == p and str(cb2.get_method()).begins_with("_on_") and not (nd == src and str(s2["name"]) == sg and str(cb2.get_method()) == method):
                    nd.disconnect(str(s2["name"]), cb2)
    var pt: Variant = JSON.parse_string(a[8])
    if pt != null:
        var items: Array = pt if pt is Array else [pt]
        for it in items:
            p.set(str(it["prop"]), _from_j(str(it["type"]), it["value"]))
    var made: Array = []
    var args: Array = []
    for c in classes:
        args.append(_arg(str(c), p, mode == "wrong", made))
    var wired := false
    if src != null:
        var cb: Callable = Callable(self, "_count")
        if classes.size() > 0:
            cb = cb.unbind(classes.size())
        src.connect(sg, cb)
        var h: Callable = Callable(p, method)
        wired = src.is_connected(sg, h)
        if mode == "control" and wired:
            src.disconnect(sg, h)
    var res := {"label": label, "pre": _snap(p, props), "wired": wired, "queued": {}}
    print("VERIFY>> ", label)
    root.add_child(main)
    await process_frame
    if src != null:
        src.callv("emit_signal", [sg] + args)
    elif mode != "control":
        p.callv(method, args)
    for nm in free_names:
        var fn_node: Node = p if nm == "self" else main.get_node_or_null("%" + str(nm))
        res["queued"][str(nm)] = fn_node != null and is_instance_valid(fn_node) and fn_node.is_queued_for_deletion()
    res["emissions"] = spy
    res["post"] = _snap(p, props)
    print("VERIFY<< ", label)
    await process_frame
    root.remove_child(main)
    main.free()
    for n in made:
        if is_instance_valid(n):
            n.free()
    return res

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var props: Array = JSON.parse_string(a[6])
    var classes: Array = JSON.parse_string(a[7])
    var free_names: Array = JSON.parse_string(a[9])
    var modes: Array = JSON.parse_string(a[10])
    var out := {}
    for m in modes:
        out[str(m)] = await _run(str(m), a, props, classes, free_names, str(m))
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(out))
    f.close()
    quit()
"""

def _free_names(sts): return [s["target"].lstrip("%") if s["target"] != "self" else "self" for s in (sts or []) if s["kind"] == "free"]

def run_probe_args(godot, project, host, src, method, sg, classes, sts, guard, label_where):
    """Explicit-stimulus check of a handler with arguments: normal, control (handler unplugged / not called) and, with a guard, a wrong-body run."""
    tmp = tempfile.mkdtemp(prefix="cascade_probe_")
    helper, out = os.path.join(tmp, "probe.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(PROBE_ARGS_GD)
    start = start_values(sts) if sts else {}
    perturb = [{"prop": p, "type": next(s["type"] for s in sts if s.get("property") == p), "value": v} for p, v in start.items()] or None
    modes = ["normal", "control"] + (["wrong"] if guard else [])
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), src["name"].lstrip("%") if src else "-", sg or "-", method,
                            json.dumps(sorted(host["props"])), json.dumps(classes), json.dumps(perturb), json.dumps(_free_names(sts)), json.dumps(modes)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return {"verdict": "FAIL", "mode": "signal" if src else "call", "summary": "the probe timed out (60 s)", "warnings": []}
    text = strip_ansi(r.stdout); mode = "signal" if src else "call"
    if not os.path.exists(out):
        return {"verdict": "FAIL", "mode": mode, "summary": "the probe could not run: " + (godot_errors(text) or text[-200:].strip()), "warnings": []}
    res = json.load(open(out)); nrm, ctl, wrg = res["normal"], res["control"], res.get("wrong")
    pn, en = _split_lines(_segment(text, "normal")); pc, ec = _split_lines(_segment(text, "control"))
    pw, ew = _split_lines(_segment(text, "wrong")) if wrg else ([], [])
    ch = lambda run: {k: [run["pre"][k], run["post"][k]] for k in run["pre"] if run["pre"][k] != run["post"][k]}
    chn, chc, chw = ch(nrm), ch(ctl), (ch(wrg) if wrg else {})
    eff_prints = collections.Counter(pn) - collections.Counter(pc)
    eff_props = {k: v for k, v in chn.items() if chc.get(k) != v}
    eff_errs = list((collections.Counter(en) - collections.Counter(ec)).elements())
    mine = [e for e in eff_errs if (f"signal '{sg}'" in e or f"::{method}" in e)] if sg else [e for e in eff_errs if method in e]
    other = [e[:160] for e in eff_errs if e not in mine]
    v = {"mode": mode, "name": sg, "source": src["name"] if src else None, "explicit": bool(src), "natural": False, "emissions": nrm.get("emissions", 1), "expect": sts or [],
         "effect": {"prints": dict(eff_prints), "props": eff_props}, "errors_mine": [e[:200] for e in mine], "warnings": ["another connection in this scene reports: " + e for e in other]}
    if src is not None and not nrm.get("wired"):
        v.update(verdict="FAIL", summary=f"{label_where}: the scene does not connect them: {method} is not wired to {src['name']}.{sg} when the scene loads"); return v
    if src is not None and nrm.get("emissions", 0) == 0:
        v.update(verdict="FAIL", summary=f"{label_where}: the signal never fired, not even when emitted explicitly"); return v
    if mine:
        v.update(verdict="FAIL", summary=f"{label_where}: Godot reports an error: {mine[0][:170]}"); return v
    if sts is None:
        v.update(verdict="UNVERIFIED", summary=f"{label_where}: ran with no error; the handler body is not plain statements, so its effect was not asserted"); return v
    want_vals, want_prints = simulate(sts, start, 1)
    free_names = _free_names(sts); problems = []
    for p in start:
        if not _close(nrm["post"].get(p), want_vals[p]): problems.append(f"{p} should go {start[p]} -> {want_vals[p]} but the normal run ended at {nrm['post'].get(p)}")
        elif not _close(ctl["post"].get(p), start[p]): problems.append(f"the control run moved {p} from {start[p]} to {ctl['post'].get(p)}, so the change cannot be credited to this handler")
        elif wrg and not _close(wrg["post"].get(p), start[p]): problems.append(f"a wrong body still moved {p} to {wrg['post'].get(p)}: the guard did not hold")
    others = {k: x for k, x in eff_props.items() if k not in start}
    if others: problems.append(f"the run also changed {others}")
    if dict(eff_prints) != dict(want_prints): problems.append(f"expected the prints {dict(want_prints) or 'none'} but the normal run printed {dict(eff_prints) or 'nothing'} more than the control run")
    if wrg and collections.Counter(pw) - collections.Counter(pc): problems.append(f"a wrong body still printed {dict(collections.Counter(pw) - collections.Counter(pc))}: the guard did not hold")
    for nm in free_names:
        if not nrm["queued"].get(nm): problems.append(f"{nm} should be queued for deletion after the handler runs, and it is not")
        elif ctl["queued"].get(nm): problems.append(f"{nm} was queued for deletion in the control run, so the free cannot be credited to this handler")
        elif wrg and wrg["queued"].get(nm): problems.append(f"{nm} was freed by a wrong body: the guard did not hold")
    if problems:
        v.update(verdict="FAIL", summary=f"{label_where}: " + "; ".join(problems)); return v
    parts = [f"{p} {start[p]} -> {nrm['post'].get(p)}" for p in start] + [f"printed {m!r} x{c}" for m, c in want_prints.items()] + [f"{n} queued for deletion" for n in free_names]
    tail = "; the control (handler unplugged) changed none of it" if src else "; the control (handler not called) changed none of it"
    if wrg: tail += "; a wrong body (not the receiver) changed none of it"
    v.update(verdict="PASS", summary=f"{label_where}: " + ("wired; " if src else "") + f"{len(sts)} statement(s) ran: " + (", ".join(parts) or "no effect and no error, as a pass body should") + tail + ("; explicit emit" if src else ""))
    return v

def run_probe_args_call(godot, project, host, block):
    sts = statements_of(block)
    where = f"call {block['fn']}({', '.join(arg_class(a) for a in block['args'])})"
    return run_probe_args(godot, project, host, None, block["fn"], None, [arg_class(a) for a in block["args"]], sts, block.get("guard") == "self", where)

def run_probe_args_cross(godot, project, host, block):
    src = host_by_name(block["source"]); sg, method = block["signal"], block["method"]
    raw = open(script_path(project, host), encoding="utf-8").read()
    sts = function_statements(raw, method, host); guard = handler_guard(raw, method) is not None
    where = f"signal {src['name']}.{sg} -> {host['name']}.{method}()"
    return run_probe_args(godot, project, host, src, method, sg, signal_arg_types(src, sg) or [], sts, guard, where)

# ---- the live physics check: the receiver walks into the trigger and the signal must fire by itself
PHYS_LIVE_GD = r"""extends SceneTree
# live physics check. args: out, scene, target, source, signal, method, props (json), argument classes (json), start values (json), free names (json), run modes (json)
var spy := 0

func _count() -> void:
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

func _run(label: String, a: PackedStringArray, props: Array, classes: Array, free_names: Array, move: bool) -> Dictionary:
    var sg: String = a[4]
    var method: String = a[5]
    var main: Node = load(a[1]).instantiate()
    var p: Node = main.get_node("%" + a[2])
    var src: Node = main.get_node("%" + a[3])
    spy = 0
    p.set_process(false)
    p.set_physics_process(false)
    for nd in [src, p]:
        for s2 in nd.get_signal_list():
            for cn in nd.get_signal_connection_list(str(s2["name"])):
                var cb2: Callable = cn["callable"]
                if cb2.get_object() == p and str(cb2.get_method()).begins_with("_on_") and not (nd == src and str(s2["name"]) == sg and str(cb2.get_method()) == method):
                    nd.disconnect(str(s2["name"]), cb2)
    var pt: Variant = JSON.parse_string(a[8])
    if pt != null:
        var items: Array = pt if pt is Array else [pt]
        for it in items:
            p.set(str(it["prop"]), _from_j(str(it["type"]), it["value"]))
    var cb: Callable = Callable(self, "_count")
    if classes.size() > 0:
        cb = cb.unbind(classes.size())
    src.connect(sg, cb)
    var res := {"label": label, "wired": src.is_connected(sg, Callable(p, method)), "queued": {}}
    print("VERIFY>> ", label)
    root.add_child(main)
    for i in 6:
        await physics_frame
    res["emissions_at_load"] = spy
    res["pre"] = _snap(p, props)
    if move:
        p.global_position = src.global_position
    for i in 30:
        await physics_frame
    res["emissions"] = spy
    for nm in free_names:
        var fn_node: Node = p if nm == "self" else main.get_node_or_null("%" + str(nm))
        res["queued"][str(nm)] = fn_node == null or not is_instance_valid(fn_node) or fn_node.is_queued_for_deletion()
    res["post"] = _snap(p, props)
    print("VERIFY<< ", label)
    root.remove_child(main)
    main.free()
    return res

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var props: Array = JSON.parse_string(a[6])
    var classes: Array = JSON.parse_string(a[7])
    var free_names: Array = JSON.parse_string(a[9])
    await process_frame
    var out := {}
    out["normal"] = await _run("normal", a, props, classes, free_names, true)
    out["control"] = await _run("control", a, props, classes, free_names, false)
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(out))
    f.close()
    quit()
"""

def run_live_physics(godot, project, host, src, block, sts):
    """The receiver is moved onto the emitter in memory and left to the physics engine; the signal must fire by itself and the effects must be the simulated ones. Returns (ok, summary)."""
    sg, method = block["signal"], block["method"]
    classes = signal_arg_types(src, sg) or []
    start = start_values(sts) if sts else {}
    perturb = [{"prop": p, "type": next(s["type"] for s in sts if s.get("property") == p), "value": v} for p, v in start.items()] or None
    tmp = tempfile.mkdtemp(prefix="cascade_physlive_")
    helper, out = os.path.join(tmp, "live.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(PHYS_LIVE_GD)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), src["name"].lstrip("%"), sg, method,
                            json.dumps(sorted(host["props"])), json.dumps(classes), json.dumps(perturb), json.dumps(_free_names(sts)), "[]"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return False, "the live physics run did not finish in 90 s"
    text = strip_ansi(r.stdout)
    if not os.path.exists(out): return False, "the live physics run could not start: " + (godot_errors(text) or text[-200:].strip())
    res = json.load(open(out)); nrm, ctl = res["normal"], res["control"]
    pn, en = _split_lines([l for l in _segment(text, "normal") if not l.startswith("WARNING")]); pc, ec = _split_lines([l for l in _segment(text, "control") if not l.startswith("WARNING")])
    if not nrm.get("wired"): return False, f"{method} is not wired to {src['name']}.{sg} when the scene loads"
    if nrm.get("emissions_at_load", 0) or ctl.get("emissions", 0) or ctl.get("emissions_at_load", 0):
        return False, f"{src['name']}.{sg} fired with the receiver at its start position ({max(nrm.get('emissions_at_load', 0), ctl.get('emissions', 0))} time(s)): the trigger already overlaps it when the scene loads"
    n = nrm.get("emissions", 0)
    if n == 0: return False, f"{src['name']}.{sg} never fired when {host['name']} was placed on it and left to the physics engine for 0.5 s"
    mine = [e for e in en if f"signal '{sg}'" in e or f"::{method}" in e]
    if mine: return False, f"Godot reports an error when the signal fires: {mine[0][:150]}"
    chg = lambda run: {k: [run["pre"][k], run["post"][k]] for k in run["pre"] if run["pre"][k] != run["post"][k]}
    if chg(ctl): return False, f"the control run (receiver left at its start) still changed {chg(ctl)}"
    if sts is None: return True, f"{src['name']}.{sg} fired {n} time(s) by itself when {host['name']} was placed on it; no error; the handler's effect was not asserted (its body is not plain statements)"
    want_vals, want_prints = simulate(sts, start, n); problems = []
    for p in start:
        if not _close(nrm["post"].get(p), want_vals[p]): problems.append(f"{p} should go {start[p]} -> {want_vals[p]} but ended at {nrm['post'].get(p)}")
    others = {k: x for k, x in chg(nrm).items() if k not in start}
    if others: problems.append(f"the run also changed {others}")
    if dict(collections.Counter(pn)) != dict(want_prints): problems.append(f"expected the prints {dict(want_prints) or 'none'} but the run printed {dict(collections.Counter(pn)) or 'nothing'}")
    for nm in _free_names(sts):
        if not nrm["queued"].get(nm): problems.append(f"{nm} was not freed by the handler")
        elif ctl["queued"].get(nm): problems.append(f"{nm} was freed with the receiver left at its start")
    if problems: return False, "; ".join(problems)
    parts = [f"{p} {start[p]} -> {nrm['post'].get(p)}" for p in start] + [f"printed {m!r} x{c}" for m, c in want_prints.items()] + [f"{x} freed" for x in _free_names(sts)]
    return True, f"{src['name']}.{sg} fired {n} time(s) by itself when {host['name']} was placed on it: " + (", ".join(parts) or "no effect and no error, as a pass body should") + "; left at its start, nothing fired"

_step16_run_probe = run_probe

def run_probe(godot, project, host, block):
    if block.get("op") == "add_method" and block.get("args"): return run_probe_args_call(godot, project, host, block)
    if is_cross(block):
        src = host_by_name(block["source"])
        if src is not None and (src.get("signal_args") or {}).get(block["signal"]):
            v = run_probe_args_cross(godot, project, host, block)
            if v.get("verdict") == "PASS" and src["node"] == "Area2D" and block["signal"] == "body_entered":
                sts = function_statements(open(script_path(project, host), encoding="utf-8").read(), block["method"], host)
                ok, summary = run_live_physics(godot, project, host, src, block, sts)
                v["live"] = {"ok": ok, "summary": summary}
                if ok: v["summary"] += "; live: " + summary
                else: v.update(verdict="FAIL", summary=v["summary"] + "; LIVE PHYSICS CHECK FAILED: " + summary)
            return v
    return _step16_run_probe(godot, project, host, block)

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
OLD_ACC = '''    attempts, accepted, calls, clipm = [], [], 0, None
'''
NEW_ACC = '''    attempts, accepted, calls, clipm = [], list(guard_prefix(block)), 0, None
'''
OLD_GI3 = '''            err = gi3_body(reply, block, subs[k], k, accepted, spec)
'''
NEW_GI3 = '''            err = gi3_body(reply, block, subs[k], k + len(guard_prefix(block)), accepted, spec)
'''
CLOCK = [(MARK, NEW_SECTION + MARK), (OLD_ACC, NEW_ACC), (OLD_GI3, NEW_GI3)]

# ------------------------------------------------------------------ the scene fixture: a %Trigger (Area2D) next to %Player and %Timer in main.tscn
SUB_RES = '[sub_resource type="RectangleShape2D" id="RectangleShape2D_trigger"]\nsize = Vector2(64, 64)\n'
TRIGGER_NODES = ('[node name="Trigger" type="Area2D" parent="."]\nunique_name_in_owner = true\nposition = Vector2(300, 0)\n\n'
                 '[node name="CollisionShape2D" type="CollisionShape2D" parent="Trigger"]\nshape = SubResource("RectangleShape2D_trigger")\n')

def fixture():
    path = os.path.join(ws, "engine_test", "scenes", "main.tscn")
    if not os.path.exists(path):
        print(f"  {'main.tscn':<24} MISSING  ({path} not found)"); return 1
    raw = open(path, encoding="utf-8", newline="").read()
    if 'type="Area2D"' in raw:
        print(f"  {'main.tscn':<24} done     (an Area2D is already in the scene)"); return 0
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)
    first_node = next((i for i, l in enumerate(lines) if l.startswith("[node ")), None)
    conn = [i for i, l in enumerate(lines) if l.startswith("[connection ") or l.startswith("[editable ")]
    if first_node is None:
        print(f"  {'main.tscn':<24} BLOCKED  (no [node ...] line found; nothing was changed)"); return 1
    # nodes first (later index), then the sub_resource ahead of all nodes
    at = min(conn) if conn else None
    block = TRIGGER_NODES.split("\n") + [""]
    if at is None:
        while lines and not lines[-1].strip(): lines.pop()
        lines += [""] + block[:-1] + [""]
    else:
        lines[at:at] = block
    lines[first_node:first_node] = SUB_RES.split("\n") + [""]
    text = nl.join(lines)
    import re as _re
    m = _re.search(r"load_steps=(\d+)", text)
    if m: text = text.replace(m.group(0), f"load_steps={int(m.group(1)) + 1}", 1)
    open(path + ".before-area", "w", encoding="utf-8", newline="").write(raw)
    open(path, "w", encoding="utf-8", newline="").write(text)
    print(f"  {'main.tscn':<24} applied  (backup main.tscn.before-area; an Area2D 64 x 64 at (300, 0), away from the Player's start)")
    return 0

def pin():
    path = os.path.join(ws, "topology.json")
    if not os.path.exists(path):
        print(f"  {'topology.json':<24} MISSING"); return 1
    d = json.load(open(path, encoding="utf-8"))
    if "%Trigger" in d.get("pins", {}):
        print(f"  {'topology.json':<24} done     (%Trigger is pinned)"); return 0
    open(path + ".before-area", "w", encoding="utf-8").write(open(path, encoding="utf-8").read())
    d.setdefault("pins", {})["%Trigger"] = "10.1.1.12"
    json.dump(d, open(path, "w", encoding="utf-8"), indent=2)
    print(f"  {'topology.json':<24} applied  (%Trigger -> 10.1.1.12)")
    return 0

entries = [
 {"file": "cascade_clock.py", "marker": "def run_probe_args_cross", "edits": CLOCK},
 {"file": "task_block_spec.json", "done": lambda d: "free" in d["slots"]["statement"]["kinds"], "edit": spec_edit},
]
blocked = pl.run(ws, "area", entries)
blocked += registry_class_edit(os.path.join(ws, "registry_builder.py"))
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
