#!/usr/bin/env bash
# Step 21: add_camera (Camera2D that follows the player) + live_camera.gd + catalog lines.
set -euo pipefail
WS="${WS:-$HOME/cascade_ws}"; GODOT="${GODOT:-godot}"; cd "$WS"; mkdir -p blocks

cat > cascade_step21.py << 'PY1'
"""Step 21: add_camera. The harness injects a Camera2D (child of the mover) into the host's scene,
then proves with live_camera.gd that it is the active camera and tracks the player within its smoothing lag.
Scene edit: timestamped .bak, restore on any failed check. cascade_clock.py binds this module with bind();
patch_step21.py wires it in. No mule call."""
import json, os, re, shutil, subprocess, tempfile, time

c = None
def bind(mod):
    global c; c = mod

OP, CLS = "add_camera", "Camera2D"
DEFAULTS = {"zoom": [1.0, 1.0], "smoothing": True, "smoothing_speed": 5.0}
ZOOM_MIN, ZOOM_MAX, SPEED_MIN, SPEED_MAX = 0.1, 8.0, 0.5, 30.0
GETVEC_RX = re.compile(r'Input\.get_vector\("([a-z_]+)",\s*"([a-z_]+)",\s*"([a-z_]+)",\s*"([a-z_]+)"\)')

def read(p): return open(p, "rb").read().decode("utf-8")
def write_atomic(p, text):
    tmp = p + ".cascade_tmp"; open(tmp, "wb").write(text.encode("utf-8")); os.replace(tmp, p)
def scene_file(project, host): return os.path.join(project, host["owner_scene"][len("res://"):])
def eff(block): return {k: block.get(k, d) for k, d in DEFAULTS.items()}
def num2(v): return isinstance(v, list) and len(v) == 2 and all(c._num(x) for x in v)

def find_mover(reg, project, host):
    """(mover host, [left, right, up, down]) for a CharacterBody2D of the same scene whose script reads Input.get_vector, else None."""
    for h in reg["hosts"].values():
        if h["owner_scene"] != host["owner_scene"] or h["node"] != "CharacterBody2D" or not h.get("script"): continue
        sp = c.script_path(project, h)
        m = GETVEC_RX.search(read(sp)) if os.path.exists(sp) else None
        if m: return h, list(m.groups())
    return None

# ---------------------------------------------------------------- the .tscn injector
def camera_text(raw, block):
    nl = "\r\n" if "\r\n" in raw else "\n"; lines = raw.split(nl); e = eff(block)
    name, pp = block["name"], c.node_parent_path(block["parent"])
    node = [f'[node name="{name}" type="{CLS}" parent="{pp}"]', "unique_name_in_owner = true", "enabled = true", "zoom = " + c.vec_text(e["zoom"]),
            "position_smoothing_enabled = " + ("true" if e["smoothing"] else "false"), "position_smoothing_speed = " + repr(float(e["smoothing_speed"])), ""]
    if not any(l.startswith("[node ") for l in lines): raise c.ApplyError("the scene has no [node ...] section")
    conn = [i for i, l in enumerate(lines) if l.startswith("[connection ") or l.startswith("[editable ")]
    if conn: lines[min(conn):min(conn)] = node
    else:
        while lines and not lines[-1].strip(): lines.pop()
        lines += [""] + node
    text = nl.join(lines)
    return text, next(i for i, l in enumerate(text.split(nl)) if l.startswith(f'[node name="{name}" ')) + 1

def matches(project, host, block):
    fp = scene_file(project, host)
    if not os.path.exists(fp): return False
    nodes = c.scene_node_blocks(read(fp)); pp = c.node_parent_path(block["parent"]); e = eff(block)
    for i, at, props in nodes:
        if at.get("name") != block["name"] or at.get("parent") != pp: continue
        if at.get("type") != CLS or props.get("unique_name_in_owner") != "true" or props.get("enabled", "true") != "true": return False
        zm = re.search(r"\((.*)\)", props.get("zoom", "")); z = [float(x) for x in re.findall(c.NUM, zm.group(1))] if zm else [1.0, 1.0]
        try: sp = float(props.get("position_smoothing_speed", "5.0"))
        except ValueError: return False
        return len(z) == 2 and c._close(z, [float(x) for x in e["zoom"]]) and (props.get("position_smoothing_enabled", "false") == "true") == e["smoothing"] and abs(sp - float(e["smoothing_speed"])) < 1e-6
    return False

# ---------------------------------------------------------------- GI_1 / GI_3
def slot1(prev, block, reg, spec, project):
    if block.get("op") != OP: return prev(block, reg, spec, project)
    s, ops, bad = spec["slots"], spec["ops"].get(OP) or {}, []
    if ops.get("status") != "locked": return None, [f"the spec is behind the clock: it has no locked '{OP}' op. Run: python3 patch_step21.py"]
    allowed = ops["slots"] + ops.get("optional_slots", [])
    bad += [f"block.{k} is missing" for k in ops["slots"] if k not in block] + [f"block.{k} is not a slot of op {OP}" for k in block if k not in allowed]
    if bad: return None, bad
    c._REG = reg
    for k in ("frame_id", "subnet", "target", "ref"):
        if not re.match(s[k]["rx"], str(block[k])): bad.append(f"block.{k} '{block[k]}' does not match the spec")
    if block["time_slot"] not in s["time_slot"]["enum"]: bad.append(f"block.time_slot '{block['time_slot']}' not allowed")
    if not isinstance(block["exclude"], list) or not block["exclude"] or any(e not in s["exclude"]["enum"] for e in block["exclude"]): bad.append("block.exclude must be a non-empty list of names from the spec")
    host = reg.get("hosts", {}).get(block["ref"])
    if host is None: return None, bad + [f"GI_1 addr_in_registry: {block['ref']} is not in the registry"]
    if not re.match(r"^uid://[a-z0-9]+$", host.get("uid", "")): bad.append("GI_1 uid_in_registry: host has no valid uid")
    elif c.header_uid(project, host["scene"]) != host["uid"]: bad.append(f"GI_1 uid_in_registry: registry is stale ({host['scene']} has a different uid); run registry_builder.py")
    if block["subnet"] != host["subnet"]: bad.append(f"GI_1 subnet_boundary: block subnet {block['subnet']} but {block['ref']} is in {host['subnet']}")
    if block["target"] != host["name"]: bad.append(f"GI_1 addr_in_registry: {block['ref']} is {host['name']}, block says {block['target']}")
    name, parent = block["name"], block["parent"]
    if not isinstance(name, str) or not re.match(c.NODE_NAME_RX, name): bad.append(f"block.name '{name}' must be 2 to 24 letters or digits and start with a capital letter")
    if not isinstance(parent, str) or not re.match(s["parent"]["rx"], parent): bad.append(f"block.parent '{parent}' must be a registered node like %Name")
    if "zoom" in block and not (num2(block["zoom"]) and all(ZOOM_MIN <= x <= ZOOM_MAX for x in block["zoom"])): bad.append(f"GI_1 range: block.zoom must be [x, y] between {ZOOM_MIN:g} and {ZOOM_MAX:g}")
    if "smoothing" in block and not isinstance(block["smoothing"], bool): bad.append("block.smoothing must be true or false")
    if "smoothing_speed" in block and not (c._num(block["smoothing_speed"]) and SPEED_MIN <= block["smoothing_speed"] <= SPEED_MAX): bad.append(f"GI_1 range: block.smoothing_speed must be between {SPEED_MIN:g} and {SPEED_MAX:g}")
    if bad: return host, bad
    mover = find_mover(reg, project, host)
    if mover is None: return host, [f"GI_1 mover: {host['owner_scene']} has no CharacterBody2D whose script reads Input.get_vector; the camera cannot be proven (run Step 19 first)"]
    if parent != mover[0]["name"]: return host, [f"GI_1 parent: the camera follows by being a child of the mover; parent must be {mover[0]['name']}, not {parent} (a camera elsewhere would need a follow script, which this op does not write)"]
    if matches(project, host, block): return host, []
    nodes = c.scene_node_blocks(read(scene_file(project, host))); used = [at.get("name") for _, at, _ in nodes]
    other = [at.get("name") for _, at, _ in nodes if at.get("type") == CLS and at.get("name") != name]
    if other: bad.append(f"GI_1 camera: {host['owner_scene']} already has a Camera2D ({', '.join(other)}); a scene gets one")
    if name in used: bad.append(f"GI_1 name: a node named {name} already exists in {host['owner_scene']} (nodes: {', '.join(sorted(set(used)))})")
    if f"%{name}" in {h["name"] for h in reg["hosts"].values()}: bad.append(f"GI_1 name: %{name} is already in the registry")
    n_scene = sum(1 for h in reg["hosts"].values() if h["owner_scene"] == host["owner_scene"])
    if n_scene + 1 > c.MAX_NODES_PER_SCENE: bad.append(f"GI_1 scene_limit: {host['owner_scene']} already holds {n_scene} registered nodes; the limit is {c.MAX_NODES_PER_SCENE}")
    return host, bad

# ---------------------------------------------------------------- SLOT_3/4 apply (one file, one .bak)
def apply_camera(godot, project, host, block):
    fp = scene_file(project, host)
    if not os.path.exists(fp): raise c.ApplyError(f"{host['owner_scene']} not found")
    raw = read(fp); new, line_no = camera_text(raw, block); e = eff(block)
    bak = f"{fp}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"; shutil.copy2(fp, bak); write_atomic(fp, new)
    res, err = c.run_node_reload(godot, project, host["owner_scene"], block["name"], c.scene_connections(new))
    problem = err if res is None else c.check_node_result(res, {"name": block["name"], "class": CLS, "position": [0.0, 0.0]})
    out = {"file": host["owner_scene"], "backup": bak, "action": "inserted", "line": line_no, "verified": not problem,
           "after": f"{CLS} {block['name']} under {block['parent']}, zoom {c.vec_text(e['zoom'])}, smoothing {'on, speed ' + repr(float(e['smoothing_speed'])) if e['smoothing'] else 'off'}"}
    if problem: shutil.copy2(bak, fp); out["rolled_back"] = True; out["problem"] = problem
    else: out["reloaded_value"] = f"{res['n_connections']} scene connections still wired"
    return out

def undo(project, host, res): shutil.copy2(res["backup"], scene_file(project, host))

# ---------------------------------------------------------------- SLOT_5 live check
def run_gd(godot, project, gd, args, timeout=90):
    out = os.path.join(tempfile.mkdtemp(prefix="cascade_s21_"), "out.json")
    try: r = subprocess.run([godot, "--headless", "--path", project, "--fixed-fps", "60", "--script", gd, "--", out, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError: c.die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired: return None, f"timeout ({timeout} s)"
    if not os.path.exists(out): return None, c.strip_ansi(r.stdout + r.stderr)[-300:].strip()
    return json.load(open(out)), ""

def verify(godot, project, ws, host, block, mover):
    gd = os.path.join(ws, "live_camera.gd")
    if not os.path.exists(gd): return {"verdict": "FAIL", "summary": f"{gd} is missing", "warnings": []}
    mh, acts = mover; e = eff(block)
    r, err = run_gd(godot, project, gd, [host["owner_scene"], mh["name"].lstrip("%"), block["name"]] + acts + [repr(float(e["zoom"][0])), repr(float(e["zoom"][1])), "1" if e["smoothing"] else "0", repr(float(e["smoothing_speed"]))])
    if r is None: return {"verdict": "FAIL", "summary": "the live check could not run: " + err, "warnings": []}
    return {"verdict": r["verdict"], "summary": r["summary"], "warnings": [], "checks": r["checks"]}

# ---------------------------------------------------------------- the frame
def run_frame(a, ws, project, host, block, row, finish):
    log = c.log; e = eff(block); fields = {"class": CLS, "name": block["name"], "parent": block["parent"], "zoom": e["zoom"], "smoothing": e["smoothing"], "smoothing_speed": e["smoothing_speed"]}
    reg = json.load(open(os.path.join(ws, "state_registry.json"))); mover = find_mover(reg, project, host)
    row.update(prefill=False, slim=False, ultra=False, temps=[], example=False, statements=0, body_mode="camera")
    log(f"CAMERA          add {CLS} %{block['name']} under {block['parent']}: zoom {e['zoom']}, smoothing {e['smoothing']}" + (f" at speed {e['smoothing_speed']}" if e["smoothing"] else "") + "; the harness writes the scene text; no mule call")
    if a.verify_only:
        vr = verify(a.godot, project, ws, host, block, mover); log("SLOT_5 VERIFY   " + c.verify_line(vr))
        log(f"RESULT: VERIFY {vr['verdict']} (verify only, nothing was written)"); finish(5 if vr["verdict"] == "FAIL" else 0, status="VERIFY_" + vr["verdict"], verify=vr, uid=host["uid"], **fields)
    if matches(project, host, block):
        log(f"RESULT: NOOP - %{block['name']} ({CLS}) is already in {host['owner_scene']} exactly as asked"); finish(0, status="NOOP_ALREADY_DEFINED", uid=host["uid"], **fields)
    log(f"SLOT_2 ROUTE    ok   op={OP}  (no mule call, GI_2 not needed)")
    log("SLOT_3 EXECUTE  " + f"[node name=\"{block['name']}\" type=\"{CLS}\" parent=\"{c.node_parent_path(block['parent'])}\"] enabled, zoom {c.vec_text(e['zoom'])}, smoothing {e['smoothing']}, speed {e['smoothing_speed']}")
    log("SLOT_4 JUDGE    ACK  GI_1 passed (mover found, one camera per scene); the scene reload below is the parse check")
    status, extra, code = "ACK", {}, 0
    if not a.no_apply:
        try: res = apply_camera(a.godot, project, host, block)
        except c.ApplyError as ex: log(f"SLOT_4 APPLY    NOT WRITTEN  {ex}"); status, extra, code = "ACK_NOT_APPLIED", {"apply_error": str(ex)}, 4
        else:
            log(f"SLOT_4 APPLY    {res['action']} line {res['line']} of {res['file'][len('res://'):]}: {res['after']}"); log(f"                backup: {os.path.relpath(res['backup'], ws)}")
            if res["verified"]:
                log(f"SLOT_4 RELOAD   ok   {res['reloaded_value']}"); status, extra = "APPLIED", {"apply": res}
                if not a.no_verify:
                    vr = verify(a.godot, project, ws, host, block, mover); extra["verify"] = vr; log("SLOT_5 VERIFY   " + c.verify_line(vr))
                    if vr["verdict"] == "FAIL": undo(project, host, res); log("SLOT_5 VERIFY   FAILED, ROLLED BACK  the edit was undone"); status, code = "ROLLED_BACK_VERIFY", 4
                if status == "APPLIED":
                    rr = c.refresh_registry_node(ws, a.godot, block); extra["registry_refresh"] = rr
                    log(f"REGISTRY       refreshed: %{block['name']} is listed at {rr['addr']} ({rr['nodes']} nodes in the registry now)" if rr.get("ok") and rr.get("listed") else f"REGISTRY       NOT refreshed - run registry_builder.py by hand ({rr.get('detail') or 'the node is not listed'})")
            else: log(f"SLOT_4 RELOAD   FAILED, ROLLED BACK  {res['problem']}"); status, extra, code = "ROLLED_BACK", {"apply": res}, 4
    log(f"RESULT: {status} (no mule call) -> ledger.jsonl")
    finish(code, status=status, tries=0, stmt=f"add {CLS} {block['name']} under {block['parent']}", uid=host["uid"], **fields, **extra)
PY1

cat > live_camera.gd << 'GD1'
extends SceneTree
const MAX_TICKS: int = 1500
const DRIVE_TICKS: int = 30
var _out: String = ""
var _acts: Array[String] = []
var _body: CharacterBody2D = null
var _cam: Camera2D = null
var _smooth: bool = true
var _speed: float = 5.0
var _settle: int = 96
var _phase: int = 0
var _pf: int = 0
var _ticks: int = 0
var _p0: Vector2 = Vector2.ZERO
var _c0: Vector2 = Vector2.ZERO
var _stats: Dictionary = {}
var _checks: Array[Dictionary] = []

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    _out = a[0]
    for i in range(4, 8):
        _acts.append(a[i])
    var zoom: Vector2 = Vector2(float(a[8]), float(a[9]))
    _smooth = a[10] == "1"
    _speed = float(a[11])
    _settle = int(clampf(8.0 / _speed * 60.0, 60.0, 1200.0))
    var packed: PackedScene = load(a[1])
    if packed == null:
        _finish("scene did not load")
        return
    var root: Node = packed.instantiate()
    root.name = "LiveRoot"
    get_root().add_child(root)
    _body = root.get_node_or_null("%" + a[2]) as CharacterBody2D
    var wall: StaticBody2D = root.get_node_or_null("%Wall") as StaticBody2D
    if wall != null:
        wall.position.x = 99999.0   # keep the wall from Step 20 out of the camera tracking drive
    _cam = root.get_node_or_null("%" + a[3]) as Camera2D
    if _body == null or _cam == null:
        _finish("%" + a[2] + " (CharacterBody2D) or %" + a[3] + " (Camera2D) is missing from the scene")
        return
    var t: Timer = root.get_node_or_null("%Timer") as Timer
    if t != null:
        t.stop()
    for n in _acts:
        _check("action " + n, InputMap.has_action(n), "defined" if InputMap.has_action(n) else "missing from InputMap")
    _check("camera settings match the block", _cam.zoom.is_equal_approx(zoom) and _cam.position_smoothing_enabled == _smooth and absf(_cam.position_smoothing_speed - _speed) < 0.001,
        "zoom %s, smoothing %s, speed %s" % [_cam.zoom, _cam.position_smoothing_enabled, _cam.position_smoothing_speed])
    if not _all_ok():
        _finish("setup failed")

func _check(label: String, ok: bool, detail: String) -> void:
    _checks.append({"name": label, "ok": ok, "detail": detail})

func _all_ok() -> bool:
    for c in _checks:
        if not c["ok"]:
            return false
    return true

func _press(names: Array[String]) -> void:
    for n in _acts:
        Input.action_release(n)
    for n in names:
        Input.action_press(n)

func _physics_process(_delta: float) -> bool:
    if _body == null or _cam == null or _checks.is_empty():
        return true
    _ticks += 1
    _pf += 1
    var center: Vector2 = _cam.get_screen_center_position()
    var pos: Vector2 = _body.global_position
    match _phase:
        0:
            if _pf == 1:
                _press([])
            if _pf == 4:
                _check("camera is enabled", _cam.enabled, "enabled=%s" % _cam.enabled)
                _check("camera is the active viewport camera", _cam.is_current() and get_root().get_camera_2d() == _cam, "is_current=%s, viewport camera=%s" % [_cam.is_current(), get_root().get_camera_2d()])
                _check("idle: camera is centred on the player", center.distance_to(pos) <= 1.0, "camera %s, player %s" % [center, pos])
                _p0 = pos
                _c0 = center
                _press([_acts[1]])
                _next()
        1:
            if _pf == DRIVE_TICKS:
                var v: float = _body.velocity.length()
                var lag: float = pos.distance_to(center)
                var tol: float = 1.3 * v / _speed + 2.0 if _smooth else 2.0
                _stats["lag"] = lag
                _stats["tol"] = tol
                _stats["v"] = v
                _check("player is driven by input", pos.x - _p0.x > 0.5 * v * DRIVE_TICKS / 60.0, "player moved %.1f px at %.0f px/s" % [pos.x - _p0.x, v])
                _check("camera follows the player", center.x - _c0.x > 0.25 * (pos.x - _p0.x), "camera moved %.1f px, player %.1f px" % [center.x - _c0.x, pos.x - _p0.x])
                _check("camera within smoothing tolerance", lag <= tol, "lag %.1f px, tolerance %.1f px" % [lag, tol])
                _check("camera is not ahead of the player", pos.x - center.x >= -1.0, "behind by %.1f px" % (pos.x - center.x))
                if _smooth:
                    _check("smoothing is visible while moving", lag >= 0.25 * v / _speed, "lag %.1f px" % lag)
                _press([])
                _next()
        2:
            if _pf == _settle:
                var err: float = pos.distance_to(center)
                _stats["err"] = err
                _check("camera settles on the player after it stops", err <= 1.0, "off by %.3f px after %d ticks" % [err, _settle])
                _finish("")
                return true
    if _ticks > MAX_TICKS:
        _finish("timeout")
        return true
    return false

func _next() -> void:
    _phase += 1
    _pf = 0

func _finish(note: String) -> void:
    for n in _acts:
        Input.action_release(n)
    var ok: bool = note == "" and _all_ok() and _checks.size() > 0
    var failed: Array[String] = []
    for c in _checks:
        if not c["ok"]:
            failed.append(str(c["name"]))
    var summary: String = ""
    if ok:
        summary = "camera tracks the player: lag %.1f px at %.0f px/s (tolerance %.1f), settles within %.3f px after stopping; zoom %s, smoothing %s (speed %s)" % [float(_stats.get("lag", 0.0)), float(_stats.get("v", 0.0)), float(_stats.get("tol", 0.0)), float(_stats.get("err", 0.0)), _cam.zoom, _smooth, _speed]
    elif _checks.is_empty() or not failed:
        summary = note
    else:
        summary = (note + "; " if note != "" else "") + "%d of %d checks failed: %s" % [failed.size(), _checks.size(), ", ".join(failed)]
    var f: FileAccess = FileAccess.open(_out, FileAccess.WRITE)
    f.store_string(JSON.stringify({"verdict": "PASS" if ok else "FAIL", "checks": _checks, "summary": summary}))
    f.close()
    quit(0 if ok else 1)
GD1

cat > patch_step21.py << 'PY2'
#!/usr/bin/env python3
"""Step 21 patcher (idempotent): adds add_camera to task_block_spec.json, wires cascade_step21 into cascade_clock.py,
and updates cascade_plan.py."""
import argparse, json, os, shutil, sys, time
ap = argparse.ArgumentParser(); ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws")); ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))
OP = "add_camera"; BASE = ["frame_id", "time_slot", "subnet", "op", "target", "ref", "exclude", "name", "parent"]; OPT = ["zoom", "smoothing", "smoothing_speed"]

def backup(p, tag):
    bak = f"{p}.{tag}.bak"
    if os.path.exists(bak): bak += "." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(p, bak); return os.path.basename(bak)
def put(p, text):
    compile(text, p, "exec") if p.endswith(".py") else None
    tmp = p + ".cascade_tmp"; open(tmp, "w", encoding="utf-8").write(text); os.replace(tmp, p)

# ---- spec
sp = os.path.join(ws, "task_block_spec.json"); spec = json.load(open(sp))
if OP in spec["slots"]["op"]["enum"] and OP in spec["ops"]: print("spec: already patched")
else:
    backup(sp, "step21")
    if OP not in spec["slots"]["op"]["enum"]: spec["slots"]["op"]["enum"].append(OP)
    spec["slots"].update({"zoom": {"type": "array", "items": "number", "length": 2, "range": [0.1, 8.0]}, "smoothing": {"type": "bool"}, "smoothing_speed": {"type": "float", "range": [0.5, 30.0]}})
    spec["ops"][OP] = {"status": "locked", "slots": BASE, "optional_slots": OPT,
        "v1": "the harness adds a Camera2D (name, parent) to the scene of the block's host; no mule call. parent must be the mover."}
    json.dump(spec, open(sp, "w"), indent=2); print("spec: patched")

# ---- clock
cp = os.path.join(ws, "cascade_clock.py"); src = open(cp, encoding="utf-8").read()
if "cascade_step21" in src: print("clock: already patched")
else:
    A1 = "# ---------------------------------------------------------------- the clock\ndef main():"
    A2 = '    if block["op"] == "add_wall": return _s20.run_frame(a, ws, project, host, block, row, finish)\n'
    if "cascade_step20" not in src: sys.exit("STOPPED: Step 20 is not wired into the clock; run step20_paste.sh first")
    if src.count(A1) != 1 or src.count(A2) != 1: sys.exit("STOPPED: the clock does not have the expected anchors; nothing written")
    B1 = ("# ---------------------------------------------------------------- Step 21: add_camera (Camera2D that follows the player; see cascade_step21.py)\n"
          "import cascade_step21 as _s21\n_s21.bind(sys.modules[__name__])\n_step20_slot1 = slot1\n\n"
          "def slot1(block, reg, spec, project): return _s21.slot1(_step20_slot1, block, reg, spec, project)\n\n")
    B2 = A2 + '    if block["op"] == "add_camera": return _s21.run_frame(a, ws, project, host, block, row, finish)\n'
    backup(cp, "step21"); put(cp, src.replace(A1, B1 + A1).replace(A2, B2)); print("clock: patched")

# ---- plan
pp = os.path.join(ws, "cascade_plan.py")
if os.path.exists(pp):
    psrc = new = open(pp, encoding="utf-8").read(); done = []
    SCHEMA = [
        ('"add_wall": {"name", "size"}', '"add_wall": {"name", "size"}, "add_camera": {"name"}', '"add_camera": {"name"}'),
        ('"add_wall": {"at", "parent", "node"}', '"add_wall": {"at", "parent", "node"}, "add_camera": {"zoom", "smoothing", "speed", "node"}', '"add_camera": {"zoom", "smoothing", "speed", "node"}')
    ]
    for old, rep, mark in SCHEMA:
        if mark not in new and old in new:
            new = new.replace(old, rep); done.append("schema " + mark.split(":")[0])
    EXPAND_ANCHOR = '        elif do == "when":\n'
    EXPAND = '''        elif do == "add_camera":
            if not isinstance(step["name"], str):
                raise PlanError(f"{where}: 'name' must be text")
            if "zoom" in step and not (isinstance(step["zoom"], list) and len(step["zoom"]) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in step["zoom"])):
                raise PlanError(f"{where}: 'zoom' must be [x, y], two numbers")
            if "smoothing" in step and not isinstance(step["smoothing"], bool):
                raise PlanError(f"{where}: 'smoothing' must be true or false")
            if "speed" in step and (isinstance(step["speed"], bool) or not isinstance(step["speed"], (int, float))):
                raise PlanError(f"{where}: 'speed' must be a number")
            blk = head(hh, "add_camera")
            blk.update(target=hh["name"], ref=hh["addr"], exclude=list(EXC.get("add_camera", EXC["add_node"])), name=step["name"], parent=hh["name"])
            for k, bk in (("zoom", "zoom"), ("smoothing", "smoothing"), ("speed", "smoothing_speed")):
                if k in step: blk[bk] = step[k]
            blocks.append(blk)
            notes.append(f"add_camera: %{step['name']} (Camera2D) follows {hh['name']} in {hh['owner_scene']}")
'''
    DESC_ANCHOR = '        if b["op"] == "connect_signal":\n            if b["source"] != b["target"]:\n'
    DESC = '''        if b["op"] == "add_camera":
            z = b.get("zoom", [1, 1])
            return f"ADD CAMERA %{b['name']} on {b['parent']} (zoom {z[0]:g}x{z[1]:g}, " + (f"smoothing {b.get('smoothing_speed', 5.0):g})" if b.get("smoothing", True) else "no smoothing)")
'''
    CANON_ANCHOR = '    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""\n'
    CANON = '''    if b["op"] == "add_camera":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
'''
    if 'do == "add_camera"' not in new and EXPAND_ANCHOR in new:
        new = new.replace(EXPAND_ANCHOR, EXPAND + EXPAND_ANCHOR); done.append("expand_plan")
    if "ADD CAMERA %" not in new and DESC_ANCHOR in new:
        new = new.replace(DESC_ANCHOR, DESC + DESC_ANCHOR); done.append("describe")
    if 'b["op"] == "add_camera"' not in new and CANON_ANCHOR in new:
        new = new.replace(CANON_ANCHOR, CANON_ANCHOR + CANON); done.append("canonical_replies")
    if new != psrc:
        bak = backup(pp, "step21"); put(pp, new); print("cascade_plan.py: patched (" + ", ".join(done) + f"); backup {bak}")
    else: print("cascade_plan.py: already patched")

pf = os.path.join(ws, "plan_camera.json")
text = ('{\n  "intent": "make the camera follow the player",\n  "node": "%Player",\n  "steps": [\n'
        '    {"do": "add_camera", "name": "Camera", "zoom": [1, 1], "smoothing": true, "speed": 5.0}\n  ]\n}\n')
if not os.path.exists(pf) or open(pf, encoding="utf-8").read() != text:
    open(pf, "w", encoding="utf-8").write(text); print("plan_camera.json: written")

PY2

cat > blocks/p21_camera.json << 'JS'
{"frame_id":"0x2101","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"add_camera","target":"%Player","ref":"10.1.1.10",
"exclude":["KinematicBody2D","untyped_variables","relative_paths_up_tree","cross_subnet_queries"],
"name":"Camera","parent":"%Player","zoom":[1.0,1.0],"smoothing":true,"smoothing_speed":5.0}
JS

python3 patch_step21.py --ws "$WS"
python3 cascade_clock.py blocks/p21_camera.json --ws "$WS" --godot "$GODOT" --no-apply
python3 cascade_clock.py blocks/p21_camera.json --ws "$WS" --godot "$GODOT"
