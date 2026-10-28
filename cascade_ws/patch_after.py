import json, os, subprocess, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (it came with the Step 9 script)")
try:
    if "def run_probe_cross" not in open(os.path.join(ws, "cascade_clock.py"), encoding="utf-8").read():
        sys.exit("STOPPED: Step 14 is not in cascade_clock.py yet. Run patch_cross.py first.")
except FileNotFoundError:
    sys.exit("STOPPED: cascade_clock.py is not in " + ws)

# ------------------------------------------------------------------ the spec: value ranges for modify_property
def spec_edit(d):
    d["ops"]["modify_property"]["ranges"] = {"Timer.wait_time": [0.05, 3600.0]}
    d["ops"]["modify_property"]["v2"] = ("a block may also target a node without a script (a %Timer): bool and float properties allowed by props_allow.json; GI_1 refuses a value outside "
                                         "the range listed for that class and property")

def props_edit(d):
    d["Timer"] = ["wait_time", "autostart", "one_shot"]

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 15: a Timer that starts (value ranges, probe isolation, the live check)
# Everything below WRAPS an earlier definition, as in Steps 13 and 14.
RANGES = {("Timer", "wait_time"): (0.05, 3600.0)}
LIVE_CAP_SECONDS = 30.0
_step14_slot1 = slot1

def slot1(block, reg, spec, project):
    host, bad = _step14_slot1(block, reg, spec, project)
    if not bad and host is not None and block.get("op") == "modify_property":
        r, v = RANGES.get((host["node"], block["property"])), block.get("value")
        if r and isinstance(v, (int, float)) and not isinstance(v, bool) and not (r[0] <= v <= r[1]):
            bad = [f"GI_1 range: {host['name']}.{block['property']} must be between {r[0]} and {r[1]}, the block says {v}"]
    return host, bad

# the cross probe puts a Timer's autostart off in its own in-memory copy, so the explicit-emit verdict stays clean
_OLD_SRC = '    var src: Node = main.get_node("%" + a[3])\n'
assert _OLD_SRC in PROBE_CROSS_GD, "the probe script is not the Step 14 one"
PROBE_CROSS_GD = PROBE_CROSS_GD.replace(_OLD_SRC, _OLD_SRC + '    if src.get("autostart") != null:\n        src.set("autostart", false)\n')

def scene_node_props(project, host):
    """The 'name = value' lines of a node's section in its owner scene, as text."""
    fp = os.path.join(project, host["owner_scene"][len("res://"):]); out = {}
    lines = open(fp, encoding="utf-8").read().splitlines()
    rel = node_rel(host); name = rel.split("/")[-1]; parent = "." if "/" not in rel else rel.rsplit("/", 1)[0]
    s, e = find_node_block(lines, name, parent)
    for l in lines[s + 1:e]:
        m = re.match(r"^(\w+)\s*=\s*(.+)$", l)
        if m: out[m.group(1)] = m.group(2).strip()
    return out

def timer_config(project, host):
    p = scene_node_props(project, host)
    try: wait = float(p.get("wait_time", "1.0"))
    except ValueError: wait = 1.0
    return {"autostart": p.get("autostart") == "true", "wait_time": wait, "one_shot": p.get("one_shot") == "true"}

LIVE_GD = r"""extends SceneTree
# live check: the scene runs by itself until the Timer fires. args: out, scene, target name, source name, signal, method, props (json), argument types (json), start values (json), wait seconds
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

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var nm: String = a[4]
    var method: String = a[5]
    var props: Array = JSON.parse_string(a[6])
    var types: Array = JSON.parse_string(a[7])
    var wait: float = float(a[9])
    await process_frame
    var main: Node = load(a[1]).instantiate()
    var p: Node = main.get_node("%" + a[2])
    var src: Node = main.get_node("%" + a[3])
    p.set_process(false)
    p.set_physics_process(false)
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
    var res := {"pre": _snap(p, props), "wired": src.is_connected(nm, Callable(p, method))}
    print("VERIFY>> live")
    root.add_child(main)
    await create_timer(wait + 0.3).timeout
    res["emissions"] = spy
    res["post"] = _snap(p, props)
    print("VERIFY<< live")
    root.remove_child(main)
    main.free()
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(res))
    f.close()
    quit()
"""

def run_live_check(godot, project, host, src, block, sts, start, cfg):
    """The scene runs by itself for the Timer's wait_time: the Timer must fire on its own and the handler's effect must be the simulated one. Returns (ok, summary)."""
    name, method, wait = block["signal"], block["method"], cfg["wait_time"]
    types = list((src.get("signal_args") or {}).get(name, []))
    perturb = [{"prop": p, "type": next(s["type"] for s in sts if s.get("property") == p), "value": v} for p, v in start.items()] or None
    tmp = tempfile.mkdtemp(prefix="cascade_live_")
    helper, out = os.path.join(tmp, "live.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(LIVE_GD)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), src["name"].lstrip("%"), name, method,
                            json.dumps(sorted(host["props"])), json.dumps(types), json.dumps(perturb), repr(wait)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=wait + 40)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return False, f"the live run did not finish in {wait + 40:.0f} s"
    text = strip_ansi(r.stdout)
    if not os.path.exists(out): return False, "the live run could not start: " + (godot_errors(text) or text[-200:].strip())
    res = json.load(open(out))
    lines = [l for l in _segment(text, "live") if not l.startswith("WARNING")]
    prints, errs = _split_lines(lines)
    n = res["emissions"]
    if not res.get("wired"): return False, f"{method} is not wired to {src['name']}.{name} when the scene loads"
    if n == 0: return False, f"{src['name']} never fired in {wait + 0.3:.1f} s although its autostart is on"
    mine = [e for e in errs if f"signal '{name}'" in e or f"::{method}" in e]
    if mine: return False, f"Godot reports an error when the Timer fires: {mine[0][:150]}"
    if sts is None: return True, f"{src['name']} fired {n} time(s) on its own after {wait} s; no error; the handler's effect was not asserted (its body is not a plain statement list)"
    want_vals, want_prints = simulate(sts, start, n)
    problems = []
    for p in start:
        if not _close(res["post"].get(p), want_vals[p]): problems.append(f"{p} should go {start[p]} -> {want_vals[p]} but ended at {res['post'].get(p)}")
    others = {k: [res["pre"][k], res["post"][k]] for k in res["pre"] if k not in start and res["pre"][k] != res["post"][k]}
    if others: problems.append(f"the run also changed {others}")
    if dict(collections.Counter(prints)) != dict(want_prints): problems.append(f"expected the prints {dict(want_prints) or 'none'} but the run printed {dict(collections.Counter(prints)) or 'nothing'}")
    if problems: return False, "; ".join(problems)
    parts = [f"{p} {start[p]} -> {res['post'].get(p)}" for p in start] + [f"printed {m!r} x{c}" for m, c in want_prints.items()]
    return True, f"{src['name']} fired {n} time(s) on its own after {wait} s: " + (", ".join(parts) or "no effect and no error, as a pass body should")

_step14_run_probe_cross = run_probe_cross

def run_probe_cross(godot, project, host, block):
    v = _step14_run_probe_cross(godot, project, host, block)
    src = host_by_name(block["source"])
    if v.get("verdict") != "PASS" or src is None or src["node"] != "Timer": return v
    try: cfg = timer_config(project, src)
    except Exception as e:
        v.setdefault("warnings", []).append(f"live check skipped: could not read {src['name']} from the scene ({e})"); return v
    if not cfg["autostart"]: return v
    if cfg["wait_time"] > LIVE_CAP_SECONDS:
        v.setdefault("warnings", []).append(f"live check skipped: {src['name']} waits {cfg['wait_time']} s, over the {LIVE_CAP_SECONDS:.0f} s limit; only the explicit-emit check ran"); return v
    sts = function_statements(open(script_path(project, host), encoding="utf-8").read(), block["method"], host)
    ok, summary = run_live_check(godot, project, host, src, block, sts, start_values(sts) if sts else {}, cfg)
    v["live"] = {"ok": ok, "summary": summary, "wait_time": cfg["wait_time"]}
    if ok: v["summary"] += "; live: " + summary
    else: v.update(verdict="FAIL", summary=v["summary"] + "; LIVE CHECK FAILED: " + summary)
    return v

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
CLOCK = [(MARK, NEW_SECTION + MARK)]

def registry():
    godot = os.environ.get("GODOT", "godot")
    r = subprocess.run([sys.executable, os.path.join(ws, "registry_builder.py"), "--ws", ws, "--godot", godot], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip().splitlines()
    print("  registry_builder.py:")
    for l in out[-6:]: print("      " + l)
    if r.returncode != 0:
        print("  => the registry was NOT rebuilt (is GODOT set? export GODOT=/path/to/godot). Fix it and run this patch again.")
        return 1
    return 0

entries = [
 {"file": "cascade_clock.py", "marker": "def run_live_check", "edits": CLOCK},
 {"file": "task_block_spec.json", "done": lambda d: "ranges" in d["ops"]["modify_property"], "edit": spec_edit},
 {"file": "props_allow.json", "done": lambda d: "Timer" in d, "edit": props_edit},
]
blocked = pl.run(ws, "after", entries)
if not blocked: blocked += registry()
sys.exit(1 if blocked else 0)
