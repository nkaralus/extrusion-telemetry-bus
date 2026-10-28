#!/usr/bin/env bash
# Step 20: add_wall (StaticBody2D) + live_collide.gd. Needs Step 19 applied. WS and GODOT override the defaults.
set -euo pipefail
WS="${WS:-$HOME/cascade_ws}"; GODOT="${GODOT:-godot}"; cd "$WS"; mkdir -p blocks

cat > cascade_step20.py << 'PY1'
"""Step 20: add_wall. The harness injects a StaticBody2D wall (with a RectangleShape2D, size = full width/height) into the host's scene
after an AABB overlap check, then proves with live_collide.gd that the mover is stopped. Scene edit: timestamped .bak, restore on any failed check.
cascade_clock.py binds this module with bind(); patch_step20.py wires it in. No mule call."""
import json, os, re, shutil, subprocess, tempfile, time

c = None
def bind(mod):
    global c; c = mod

OP, CLS = "add_wall", "StaticBody2D"
SLOTS = ["frame_id", "time_slot", "subnet", "op", "target", "ref", "exclude", "name", "parent", "position", "size"]
GETVEC_RX = re.compile(r'Input\.get_vector\("([a-z_]+)",\s*"([a-z_]+)",\s*"([a-z_]+)",\s*"([a-z_]+)"\)')

def read(p): return open(p, "rb").read().decode("utf-8")
def write_atomic(p, text):
    tmp = p + ".cascade_tmp"; open(tmp, "wb").write(text.encode("utf-8")); os.replace(tmp, p)
def scene_file(project, host): return os.path.join(project, host["owner_scene"][len("res://"):])

# ---------------------------------------------------------------- geometry
def wall_rect(block, geo):
    base = geo["root"] if block["parent"] == "." else (geo["nodes"].get(block["parent"]) or {}).get("pos") or [0.0, 0.0]
    cx, cy = base[0] + block["position"][0], base[1] + block["position"][1]; hw, hh = block["size"][0] / 2.0, block["size"][1] / 2.0
    return [cx - hw, cy - hh, cx + hw, cy + hh]

def overlaps(a, b): return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def find_mover(reg, project, host):
    """(mover host, [left, right, up, down]) for a CharacterBody2D of the same scene whose script reads Input.get_vector, else None."""
    for h in reg["hosts"].values():
        if h["owner_scene"] != host["owner_scene"] or h["node"] != "CharacterBody2D" or not h.get("script"): continue
        sp = c.script_path(project, h)
        m = GETVEC_RX.search(read(sp)) if os.path.exists(sp) else None
        if m: return h, list(m.groups())
    return None

# ---------------------------------------------------------------- the .tscn injector
def wall_text(raw, block):
    """(scene text with the wall, its CollisionShape2D and RectangleShape2D added, 1-based line of the wall)."""
    nl = "\r\n" if "\r\n" in raw else "\n"; lines = raw.split(nl)
    name, pp = block["name"], c.node_parent_path(block["parent"])
    sid = f"RectangleShape2D_{name.lower()}"; used = set(c.scene_sub_resources(raw))
    while sid in used: sid += "_n"
    child = name if pp == "." else pp + "/" + name
    node = [f'[node name="{name}" type="{CLS}" parent="{pp}"]', "unique_name_in_owner = true", "position = " + c.vec_text(block["position"]), "",
            f'[node name="CollisionShape2D" type="CollisionShape2D" parent="{child}"]', f'shape = SubResource("{sid}")', ""]
    sub = [f'[sub_resource type="RectangleShape2D" id="{sid}"]', "size = " + c.vec_text(block["size"]), ""]
    first = next((i for i, l in enumerate(lines) if l.startswith("[node ")), None)
    if first is None: raise c.ApplyError("the scene has no [node ...] section")
    conn = [i for i, l in enumerate(lines) if l.startswith("[connection ") or l.startswith("[editable ")]
    if conn: lines[min(conn):min(conn)] = node
    else:
        while lines and not lines[-1].strip(): lines.pop()
        lines += [""] + node
    lines[first:first] = sub
    text = nl.join(lines); m = re.search(r"load_steps=(\d+)", text)
    if m: text = text.replace(m.group(0), f"load_steps={int(m.group(1)) + 1}", 1)
    return text, next(i for i, l in enumerate(text.split(nl)) if l.startswith(f'[node name="{name}" ')) + 1

def matches(project, host, block):
    """True when the scene already has exactly this wall (type, parent, unique name, position, rectangle size)."""
    fp = scene_file(project, host)
    if not os.path.exists(fp): return False
    raw = read(fp); nodes = c.scene_node_blocks(raw); subs = c.scene_sub_resources(raw); pp = c.node_parent_path(block["parent"])
    for i, at, props in nodes:
        if at.get("name") != block["name"] or at.get("parent") != pp: continue
        if at.get("type") != CLS or props.get("unique_name_in_owner") != "true": return False
        have, want = props.get("position"), c.vec_text(block["position"])
        if have != want and not (have is None and [float(x) for x in block["position"]] == [0.0, 0.0]): return False
        child = block["name"] if pp == "." else pp + "/" + block["name"]
        for j, cat, cprops in nodes:
            if cat.get("parent") == child and cat.get("type") == "CollisionShape2D":
                m = re.search(r'SubResource\("([^"]+)"\)', cprops.get("shape", ""))
                if m and subs.get(m.group(1), {}).get("type") == "RectangleShape2D" and subs[m.group(1)]["size"] == c.vec_text(block["size"]): return True
        return False
    return False

# ---------------------------------------------------------------- GI_1 / GI_3
def slot1(prev, block, reg, spec, project):
    if block.get("op") != OP: return prev(block, reg, spec, project)
    s, ops, bad = spec["slots"], spec["ops"].get(OP) or {}, []
    if ops.get("status") != "locked": return None, [f"the spec is behind the clock: it has no locked '{OP}' op. Run: python3 patch_step20.py"]
    bad += [f"block.{k} is missing" for k in ops["slots"] if k not in block] + [f"block.{k} is not a slot of op {OP}" for k in block if k not in ops["slots"]]
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
    if not isinstance(parent, str) or not re.match(s["parent"]["rx"], parent): bad.append(f"block.parent '{parent}' must be . (the scene root) or a registered node like %Name")
    elif parent != ".":
        ph = c.host_by_name(parent)
        if ph is None: bad.append(f"GI_1 parent: {parent} is not in the registry")
        elif ph["owner_scene"] != host["owner_scene"]: bad.append(f"GI_1 parent: {parent} is in {ph['owner_scene']}, not in {host['owner_scene']}")
    for k, lo, hi in (("position", -c.NODE_POS_LIMIT, c.NODE_POS_LIMIT), ("size", c.NODE_SIZE_MIN, c.NODE_SIZE_MAX)):
        v = block[k]
        if not (isinstance(v, list) and len(v) == 2 and all(c._num(x) for x in v)): bad.append(f"block.{k} must be a list of two numbers")
        elif not all(lo <= x <= hi for x in v): bad.append(f"GI_1 range: block.{k} {v} must stay between {lo:g} and {hi:g}")
    if bad: return host, bad
    if find_mover(reg, project, host) is None: bad.append(f"GI_1 mover: {host['owner_scene']} has no CharacterBody2D whose script reads Input.get_vector; the wall cannot be proven (run Step 19 first)")
    if bad or matches(project, host, block): return host, bad
    raw = read(scene_file(project, host)); used = [at.get("name") for _, at, _ in c.scene_node_blocks(raw)]
    if name in used: bad.append(f"GI_1 name: a node named {name} already exists in {host['owner_scene']} (nodes: {', '.join(sorted(set(used)))})")
    if f"%{name}" in {h["name"] for h in reg["hosts"].values()}: bad.append(f"GI_1 name: %{name} is already in the registry")
    n_scene = sum(1 for h in reg["hosts"].values() if h["owner_scene"] == host["owner_scene"])
    if n_scene + 1 > c.MAX_NODES_PER_SCENE: bad.append(f"GI_1 scene_limit: {host['owner_scene']} already holds {n_scene} registered nodes; the limit is {c.MAX_NODES_PER_SCENE}")
    if bad: return host, bad
    geo = c.scene_geometry(project, host["owner_scene"])
    if geo is None: return host, ["GI_1 overlap: Godot could not read the scene's geometry, so the overlap guard could not run"]
    mine = wall_rect(block, geo)
    for nm, d in sorted(geo["nodes"].items()):
        r = d.get("rect")
        if r and overlaps(mine, r):
            bad.append(f"GI_1 overlap: the wall would cover [{mine[0]:g}, {mine[1]:g}] to [{mine[2]:g}, {mine[3]:g}], which overlaps the collision shape of {nm} at [{r[0]:g}, {r[1]:g}] to [{r[2]:g}, {r[3]:g}]. Move it or make it smaller")
    return host, bad

# ---------------------------------------------------------------- SLOT_3/4 apply (one file, one .bak)
def apply_wall(godot, project, host, block):
    fp = scene_file(project, host)
    if not os.path.exists(fp): raise c.ApplyError(f"{host['owner_scene']} not found")
    raw = read(fp); new, line_no = wall_text(raw, block)
    bak = f"{fp}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"; shutil.copy2(fp, bak); write_atomic(fp, new)
    res, err = c.run_node_reload(godot, project, host["owner_scene"], block["name"], c.scene_connections(new))
    view = {"name": block["name"], "class": CLS, "position": block["position"]}
    problem = err if res is None else c.check_node_result(res, view)
    if not problem and (res["size"] is None or not c._close(res["size"], [float(x) for x in block["size"]])): problem = f"shape size loaded as {res['size']}, the block says {block['size']}"
    out = {"file": host["owner_scene"], "backup": bak, "action": "inserted", "line": line_no, "verified": not problem,
           "after": f"{CLS} {block['name']} under {block['parent'] if block['parent'] != '.' else 'the scene root'} at {c.vec_text(block['position'])}, shape size {c.vec_text(block['size'])}"}
    if problem: shutil.copy2(bak, fp); out["rolled_back"] = True; out["problem"] = problem
    else: out["reloaded_value"] = f"{res['n_connections']} scene connections still wired"
    return out

def undo(project, host, res): shutil.copy2(res["backup"], scene_file(project, host))

# ---------------------------------------------------------------- SLOT_5 live check
def run_gd(godot, project, gd, args, timeout=60):
    out = os.path.join(tempfile.mkdtemp(prefix="cascade_s20_"), "out.json")
    try: r = subprocess.run([godot, "--headless", "--path", project, "--fixed-fps", "60", "--script", gd, "--", out, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError: c.die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired: return None, f"timeout ({timeout} s)"
    if not os.path.exists(out): return None, c.strip_ansi(r.stdout + r.stderr)[-300:].strip()
    return json.load(open(out)), ""

def verify(godot, project, ws, host, block, mover):
    gd = os.path.join(ws, "live_collide.gd")
    if not os.path.exists(gd): return {"verdict": "FAIL", "summary": f"{gd} is missing", "warnings": []}
    mh, acts = mover
    r, err = run_gd(godot, project, gd, [host["owner_scene"], mh["name"].lstrip("%"), block["name"]] + acts)
    if r is None: return {"verdict": "FAIL", "summary": "the live check could not run: " + err, "warnings": []}
    return {"verdict": r["verdict"], "summary": r["summary"], "warnings": [], "checks": r["checks"]}

# ---------------------------------------------------------------- the frame
def run_frame(a, ws, project, host, block, row, finish):
    log = c.log; fields = {"class": CLS, **{k: block[k] for k in ("name", "parent", "position", "size")}}
    reg = json.load(open(os.path.join(ws, "state_registry.json"))); mover = find_mover(reg, project, host)
    row.update(prefill=False, slim=False, ultra=False, temps=[], example=False, statements=0, body_mode="wall")
    where = f"under {block['parent']}" if block["parent"] != "." else "under the scene root"
    log(f"WALL            add {CLS} %{block['name']} {where} at {block['position']}, shape size {block['size']}; the harness writes the scene text; no mule call")
    if a.verify_only:
        vr = verify(a.godot, project, ws, host, block, mover); log("SLOT_5 VERIFY   " + c.verify_line(vr))
        log(f"RESULT: VERIFY {vr['verdict']} (verify only, nothing was written)"); finish(5 if vr["verdict"] == "FAIL" else 0, status="VERIFY_" + vr["verdict"], verify=vr, uid=host["uid"], **fields)
    if matches(project, host, block):
        log(f"RESULT: NOOP - %{block['name']} ({CLS}) is already in {host['owner_scene']} exactly as asked"); finish(0, status="NOOP_ALREADY_DEFINED", uid=host["uid"], **fields)
    log(f"SLOT_2 ROUTE    ok   op={OP}  (no mule call, GI_2 not needed)")
    log("SLOT_3 EXECUTE  " + f"[node name=\"{block['name']}\" type=\"{CLS}\"] + CollisionShape2D + RectangleShape2D size = {c.vec_text(block['size'])}")
    log("SLOT_4 JUDGE    ACK  AABB overlap guard passed (GI_1); the scene reload below is the parse check")
    status, extra, code = "ACK", {}, 0
    if not a.no_apply:
        try: res = apply_wall(a.godot, project, host, block)
        except c.ApplyError as e: log(f"SLOT_4 APPLY    NOT WRITTEN  {e}"); status, extra, code = "ACK_NOT_APPLIED", {"apply_error": str(e)}, 4
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
    finish(code, status=status, tries=0, stmt=f"add {CLS} {block['name']} {where}", uid=host["uid"], **fields, **extra)
PY1

cat > live_collide.gd << 'GD1'
extends SceneTree
# Live collision check. Run: godot --headless --path <project> --fixed-fps 60 --script live_collide.gd -- <out.json> <scene> <player> <wall> <left> <right> <up> <down>
const MAX_TICKS: int = 400
const PEN_MAX: float = 0.081   # CharacterBody2D safe_margin is 0.08 in Godot 4.3
const SPEED_HI: float = 600.0
var _out: String = ""
var _acts: Array[String] = []
var _body: CharacterBody2D = null
var _pcs: CollisionShape2D = null
var _wcs: CollisionShape2D = null
var _phase: int = 0
var _pf: int = 0
var _ticks: int = 0
var _y0: float = 0.0
var _max_pen: float = -INF
var _tunnel: bool = false
var _stats: Dictionary = {}
var _checks: Array[Dictionary] = []

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    _out = a[0]
    for i in range(4, 8):
        _acts.append(a[i])
    var packed: PackedScene = load(a[1])
    if packed == null:
        _finish("scene did not load")
        return
    var root: Node = packed.instantiate()
    root.name = "LiveRoot"
    get_root().add_child(root)
    _body = root.get_node_or_null("%" + a[2]) as CharacterBody2D
    var wall: StaticBody2D = root.get_node_or_null("%" + a[3]) as StaticBody2D
    if _body == null or wall == null:
        _finish("%" + a[2] + " (CharacterBody2D) or %" + a[3] + " (StaticBody2D) is missing from the scene")
        return
    _pcs = _shape_of(_body)
    _wcs = _shape_of(wall)
    if _pcs == null or _wcs == null:
        _finish("the player or the wall has no RectangleShape2D")
        return
    var t: Timer = root.get_node_or_null("%Timer") as Timer
    if t != null:
        t.stop()   # keep the speed clamps of the timer handler out of the measurement
    for n in _acts:
        _check("action " + n, InputMap.has_action(n), "defined" if InputMap.has_action(n) else "missing from InputMap")
    var wr: Rect2 = _rect(_wcs)
    _check("wall is a StaticBody2D with a RectangleShape2D", true, "x %.2f..%.2f, y %.2f..%.2f, size %s" % [wr.position.x, wr.end.x, wr.position.y, wr.end.y, wr.size])
    if not _all_ok():
        _finish("setup failed")

func _shape_of(n: Node) -> CollisionShape2D:
    for ch in n.get_children():
        if ch is CollisionShape2D and (ch as CollisionShape2D).shape is RectangleShape2D:
            return ch as CollisionShape2D
    return null

func _rect(cs: CollisionShape2D) -> Rect2:
    var sz: Vector2 = (cs.shape as RectangleShape2D).size
    return Rect2(cs.global_position - sz / 2.0, sz)

func _check(label: String, ok: bool, detail: String) -> void:
    _checks.append({"name": label, "ok": ok, "detail": detail})

func _all_ok() -> bool:
    for ch in _checks:
        if not ch["ok"]:
            return false
    return true

func _press(names: Array[String]) -> void:
    for n in _acts:
        Input.action_release(n)
    for n in names:
        Input.action_press(n)

func _track() -> void:
    var pr: Rect2 = _rect(_pcs)
    var wr: Rect2 = _rect(_wcs)
    if pr.end.y > wr.position.y and pr.position.y < wr.end.y:
        if pr.get_center().x >= wr.get_center().x:
            _tunnel = true
        elif pr.end.x - wr.position.x > _max_pen:
            _max_pen = pr.end.x - wr.position.x

func _physics_process(_delta: float) -> bool:
    if _body == null or _checks.is_empty():
        return true
    _ticks += 1
    _pf += 1
    _track()
    var pr: Rect2 = _rect(_pcs)
    var wr: Rect2 = _rect(_wcs)
    match _phase:
        0:
            if _pf == 1:
                _press([])
            if _pf == 4:
                _press([_acts[1]])
                _next()
        1:
            if (_body.is_on_wall() and _pf >= 5) or _pf == 110:
                var gap: float = wr.position.x - pr.end.x
                _stats["gap"] = gap
                _check("right: stopped by the wall", _body.is_on_wall(), "is_on_wall=%s, x=%.2f" % [_body.is_on_wall(), _body.position.x])
                _check("right: penetration <= %.3f px" % PEN_MAX, _max_pen <= PEN_MAX and not _tunnel, "max penetration %.4f px, gap %.4f px" % [_max_pen, gap])
                _check("right: touching, not stopped short", gap <= 1.0, "gap %.4f px" % gap)
                _check("right: velocity.x blocked", absf(_body.velocity.x) < 1.0, str(_body.velocity))
                _y0 = _body.position.y
                _press([_acts[1], _acts[3]])
                _next()
        2:
            if _pf == 12:
                var slid: float = _body.position.y - _y0
                _stats["slid"] = slid
                _check("slide: moves along the wall", slid > 10.0, "y moved %.1f px" % slid)
                _check("slide: penetration <= %.3f px" % PEN_MAX, _max_pen <= PEN_MAX and not _tunnel, "max penetration %.4f px" % _max_pen)
                _press([])
                _body.velocity = Vector2.ZERO
                _body.set("speed", SPEED_HI)
                _body.global_position = Vector2(wr.position.x - 100.0 - pr.size.x / 2.0, wr.get_center().y)
                _max_pen = -INF
                _tunnel = false
                _press([_acts[1]])
                _next()
        3:
            if _pf == 30:
                _check("speed %.0f: wall holds, no tunnelling" % SPEED_HI, _body.is_on_wall() and _max_pen <= PEN_MAX and not _tunnel, "max penetration %.4f px, on_wall=%s, tunnelled=%s" % [_max_pen, _body.is_on_wall(), _tunnel])
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
    for ch in _checks:
        if not ch["ok"]:
            failed.append(str(ch["name"]))
    var summary: String = ""
    if ok:
        summary = "wall holds: stopped with gap %.3f px, max penetration <= %.3f px, slid %.1f px along it, no tunnelling at speed %.0f" % [float(_stats.get("gap", 0.0)), PEN_MAX, float(_stats.get("slid", 0.0)), SPEED_HI]
    elif _checks.is_empty() or not failed:
        summary = note
    else:
        summary = (note + "; " if note != "" else "") + "%d of %d checks failed: %s" % [failed.size(), _checks.size(), ", ".join(failed)]
    var f: FileAccess = FileAccess.open(_out, FileAccess.WRITE)
    f.store_string(JSON.stringify({"verdict": "PASS" if ok else "FAIL", "checks": _checks, "summary": summary}))
    f.close()
    quit(0 if ok else 1)
GD1

cat > patch_step20.py << 'PY2'
#!/usr/bin/env python3
"""Step 20 patcher (idempotent, .bak first): adds add_wall to task_block_spec.json and wires cascade_step20 into cascade_clock.py (after Step 19)."""
import json, os, shutil, sys, argparse
ap = argparse.ArgumentParser(); ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws")); ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))
OP = "add_wall"; SLOTS = ["frame_id", "time_slot", "subnet", "op", "target", "ref", "exclude", "name", "parent", "position", "size"]
def bak(p): shutil.copy2(p, p + ".step20.bak")

sp = os.path.join(ws, "task_block_spec.json"); spec = json.load(open(sp))
if any(k not in spec["slots"] for k in ("name", "parent", "position", "size")): sys.exit("STOPPED: the spec lacks the Step 18 slots name/parent/position/size; nothing written")
if OP in spec["slots"]["op"]["enum"] and OP in spec["ops"]: print("spec: already patched")
else:
    bak(sp)
    if OP not in spec["slots"]["op"]["enum"]: spec["slots"]["op"]["enum"].append(OP)
    spec["ops"][OP] = {"status": "locked", "slots": SLOTS,
        "v1": "the harness adds a StaticBody2D (name, parent, position) with a CollisionShape2D and a RectangleShape2D (size is the full width and height) to the scene of the block's host; no mule call. GI_1: the new wall may not overlap any registered collision shape (AABB); the scene needs a CharacterBody2D that reads Input.get_vector. SLOT_5 runs live_collide.gd: the mover is stopped with penetration <= 0.081 px, slides along the wall, and does not tunnel at speed 600"}
    json.dump(spec, open(sp, "w"), indent=2); print("spec: patched")

cp = os.path.join(ws, "cascade_clock.py"); src = open(cp, encoding="utf-8").read()
if "cascade_step20" in src: print("clock: already patched")
else:
    A1 = "# ---------------------------------------------------------------- the clock\ndef main():"
    A2 = '    if block["op"] == "set_physics_body": return _s19.run_frame(a, ws, project, host, block, row, finish)\n'
    if "cascade_step19" not in src: sys.exit("STOPPED: Step 19 is not wired into the clock; run step19_paste.sh first")
    if src.count(A1) != 1 or src.count(A2) != 1: sys.exit("STOPPED: the clock does not have the expected anchors; nothing written")
    B1 = ("# ---------------------------------------------------------------- Step 20: add_wall (StaticBody2D wall; see cascade_step20.py)\n"
          "import cascade_step20 as _s20\n_s20.bind(sys.modules[__name__])\n_step19_slot1 = slot1\n\n"
          "def slot1(block, reg, spec, project): return _s20.slot1(_step19_slot1, block, reg, spec, project)\n\n")
    B2 = A2 + '    if block["op"] == "add_wall": return _s20.run_frame(a, ws, project, host, block, row, finish)\n'
    bak(cp); open(cp, "w", encoding="utf-8").write(src.replace(A1, B1 + A1).replace(A2, B2)); print("clock: patched")
PY2

cat > blocks/p20_wall.json << 'JS'
{"frame_id":"0x2001","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"add_wall","target":"%Player","ref":"10.1.1.10",
"exclude":["KinematicBody2D","untyped_variables","move_and_slide_args","relative_paths_up_tree","cross_subnet_queries"],
"name":"Wall","parent":".","position":[600,0],"size":[32,256]}
JS

python3 patch_step20.py --ws "$WS"
python3 cascade_clock.py blocks/p20_wall.json --ws "$WS" --godot "$GODOT" --no-apply
python3 cascade_clock.py blocks/p20_wall.json --ws "$WS" --godot "$GODOT"
