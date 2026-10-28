import json, os, subprocess, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (it came with the Step 9 script)")
try:
    if "def run_probe_args_cross" not in open(os.path.join(ws, "cascade_clock.py"), encoding="utf-8").read():
        sys.exit("STOPPED: Step 17 is not in cascade_clock.py yet. Run patch_area.py first.")
except FileNotFoundError:
    sys.exit("STOPPED: cascade_clock.py is not in " + ws)

# ------------------------------------------------------------------ the spec: the add_node op
def spec_edit(d):
    if "add_node" not in d["slots"]["op"]["enum"]: d["slots"]["op"]["enum"].append("add_node")
    d["slots"]["class"] = {"type": "string", "enum": ["Area2D", "Timer", "Marker2D", "Node2D"]}
    d["slots"]["name"] = {"type": "string", "rx": "^[A-Z][A-Za-z0-9]{1,23}$"}
    d["slots"]["parent"] = {"type": "string", "rx": "^(\\.|%[A-Za-z_][A-Za-z0-9_]*)$"}
    d["slots"]["position"] = {"type": "array", "items": "number", "length": 2, "range": [-4096, 4096]}
    d["slots"]["size"] = {"type": "array", "items": "number", "length": 2, "range": [8, 1024]}
    d["ops"]["add_node"] = {
        "status": "locked",
        "slots": ["frame_id", "time_slot", "subnet", "op", "target", "ref", "exclude", "class", "name", "parent"],
        "optional_slots": ["position", "size"],
        "v1": ("add one node to the scene of the block's host (target/ref only name which scene): class Area2D (with a RectangleShape2D, size required), Timer, Marker2D or Node2D; "
               "the harness writes the scene text and unique_name_in_owner; the mule only completes position and size, one value per call; position is not allowed for a Timer, size only for an Area2D"),
        "v2": ("GI_1: name 2 to 24 characters starting with a capital letter and unused by any node in the scene; parent is . (the scene root) or a registered node of the same scene; "
               "position within 4096, size 8 to 1024; at most 8 per plan and 24 registered nodes per scene; a new Area2D may not overlap any registered collision shape at load"),
    }

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 18: add_node (a node the plans can then act on)
# Everything below WRAPS or SHADOWS an earlier definition, as in Steps 13 to 17.
NODE_CLASSES18 = ("Area2D", "Timer", "Marker2D", "Node2D")
NODE_NAME_RX = r"^[A-Z][A-Za-z0-9]{1,23}$"
NODE_POS_LIMIT, NODE_SIZE_MIN, NODE_SIZE_MAX = 4096.0, 8.0, 1024.0
MAX_NODES_PER_SCENE = 24
_GODOT = [os.environ.get("GODOT", "godot")]

def set_godot(path): _GODOT[0] = path

def scene_node_blocks(raw):
    """[(line index, attrs, props)] for every [node ...] section of a scene text; props maps 'name' to the value text."""
    lines = raw.splitlines(); out = []
    for i, l in enumerate(lines):
        if l.startswith("[node "):
            at = dict(re.findall(r'(\w+)="([^"]*)"', l)); props = {}; j = i + 1
            while j < len(lines) and not lines[j].startswith("["):
                m = re.match(r"^(\w+)\s*=\s*(.+)$", lines[j])
                if m: props[m.group(1)] = m.group(2).strip()
                j += 1
            out.append((i, at, props))
    return out

def scene_sub_resources(raw):
    """{id: {'type': .., 'size': 'Vector2(..)' or None}} for every sub_resource of a scene text."""
    lines = raw.splitlines(); out = {}
    for i, l in enumerate(lines):
        if l.startswith("[sub_resource "):
            at = dict(re.findall(r'(\w+)="([^"]*)"', l)); size = None; j = i + 1
            while j < len(lines) and not lines[j].startswith("["):
                m = re.match(r"^size\s*=\s*(.+)$", lines[j])
                if m: size = m.group(1).strip()
                j += 1
            out[at.get("id")] = {"type": at.get("type"), "size": size}
    return out

def node_parent_path(parent):
    """The scene path a [node ... parent=] attribute holds for the block's parent field."""
    if parent == ".": return "."
    h = host_by_name(parent)
    return node_rel(h) if h is not None else None

def vec_text(v): return scene_literal("Vector2", [float(v[0]), float(v[1])])

def node_matches(project, host, block):
    """True when the scene already has exactly this node (same class, parent, position and shape size)."""
    fp = os.path.join(project, host["owner_scene"][len("res://"):])
    if not os.path.exists(fp): return False
    raw = open(fp, encoding="utf-8").read(); nodes = scene_node_blocks(raw); subs = scene_sub_resources(raw)
    pp = node_parent_path(block["parent"])
    for i, at, props in nodes:
        if at.get("name") != block["name"] or at.get("parent") != pp: continue
        if at.get("type") != block["class"] or props.get("unique_name_in_owner") != "true": return False
        if block["class"] != "Timer":
            want = vec_text(block["position"]); have = props.get("position")
            if have != want and not (have is None and [float(x) for x in block["position"]] == [0.0, 0.0]): return False
        if block["class"] == "Area2D":
            child_path = block["name"] if pp == "." else pp + "/" + block["name"]
            for j, cat, cprops in nodes:
                if cat.get("parent") == child_path and cat.get("type") == "CollisionShape2D":
                    m = re.search(r'SubResource\("([^"]+)"\)', cprops.get("shape", ""))
                    if m and subs.get(m.group(1), {}).get("type") == "RectangleShape2D" and subs[m.group(1)]["size"] == vec_text(block["size"]): return True
            return False
        return True
    return False

GEOM_GD = r"""extends SceneTree
# geometry of the unique-name nodes of a scene, without running it. args: out, scene
func _gpos(n: Node) -> Vector2:
    var p := Vector2.ZERO
    var c: Node = n
    while c != null:
        if c is Node2D:
            p += (c as Node2D).position
        c = c.get_parent()
    return p

func _walk(n: Node, root: Node, res: Dictionary) -> void:
    if n != root and n.unique_name_in_owner and n.owner == root:
        var d := {"pos": null, "rect": null}
        if n is Node2D:
            var gp: Vector2 = _gpos(n)
            d["pos"] = [gp.x, gp.y]
            for c in n.get_children():
                if c is CollisionShape2D and (c as CollisionShape2D).shape is RectangleShape2D:
                    var sz: Vector2 = ((c as CollisionShape2D).shape as RectangleShape2D).size
                    var cp: Vector2 = gp + (c as CollisionShape2D).position
                    d["rect"] = [cp.x - sz.x / 2.0, cp.y - sz.y / 2.0, cp.x + sz.x / 2.0, cp.y + sz.y / 2.0]
        res["nodes"]["%" + str(n.name)] = d
    for c in n.get_children():
        _walk(c, root, res)

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var res := {"root": [0.0, 0.0], "nodes": {}}
    var packed: PackedScene = load(a[1])
    if packed != null:
        var root: Node = packed.instantiate()
        var rp: Vector2 = _gpos(root)
        res["root"] = [rp.x, rp.y]
        _walk(root, root, res)
        root.free()
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(res))
    f.close()
    quit()
"""

def scene_geometry(project, scene):
    tmp = tempfile.mkdtemp(prefix="cascade_geom_")
    helper, out = os.path.join(tmp, "geom.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(GEOM_GD)
    try:
        subprocess.run([_GODOT[0], "--headless", "--path", project, "--script", helper, "--", out, scene], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{_GODOT[0]}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return None
    return json.load(open(out)) if os.path.exists(out) else None

def _num(v): return isinstance(v, (int, float)) and not isinstance(v, bool)

def add_node_checks(block, host, spec, project, reg):
    bad = []; cls, name, parent = block["class"], block["name"], block["parent"]
    if cls not in NODE_CLASSES18: bad.append(f"block.class '{cls}' not allowed (one of {', '.join(NODE_CLASSES18)})")
    if not isinstance(name, str) or not re.match(NODE_NAME_RX, name): bad.append(f"block.name '{name}' must be 2 to 24 letters or digits and start with a capital letter")
    if not isinstance(parent, str) or not re.match(spec["slots"]["parent"]["rx"], parent): bad.append(f"block.parent '{parent}' must be . (the scene root) or a registered node like %Name")
    elif parent != ".":
        ph = host_by_name(parent)
        if ph is None: bad.append(f"GI_1 parent: {parent} is not in the registry (nodes: {', '.join(sorted(h['name'] for h in reg['hosts'].values()))})")
        elif ph["owner_scene"] != host["owner_scene"]: bad.append(f"GI_1 parent: {parent} is in {ph['owner_scene']}, not in {host['owner_scene']}")
    if cls == "Timer":
        if "position" in block: bad.append("block.position is not allowed for a Timer (it has none)")
    elif "position" not in block: bad.append("block.position is missing")
    if cls == "Area2D":
        if "size" not in block: bad.append("block.size is missing (an Area2D needs the size of its rectangle shape)")
    elif "size" in block: bad.append(f"block.size is only for an Area2D, not for a {cls}")
    for k, lo, hi in (("position", -NODE_POS_LIMIT, NODE_POS_LIMIT), ("size", NODE_SIZE_MIN, NODE_SIZE_MAX)):
        if k in block:
            v = block[k]
            if not (isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v)): bad.append(f"block.{k} must be a list of two numbers"); continue
            if not all(lo <= x <= hi for x in v): bad.append(f"GI_1 range: block.{k} {v} must stay between {lo:g} and {hi:g}")
    if bad: return bad
    if node_matches(project, host, block): return []
    fp = os.path.join(project, host["owner_scene"][len("res://"):])
    raw = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    used = [at.get("name") for _, at, _ in scene_node_blocks(raw)]
    if name in used: bad.append(f"GI_1 name: a node named {name} already exists in {host['owner_scene']}; names must be unused (nodes: {', '.join(sorted(set(used)))})")
    if f"%{name}" in {h["name"] for h in reg["hosts"].values()}: bad.append(f"GI_1 name: %{name} is already in the registry")
    n_scene = sum(1 for h in reg["hosts"].values() if h["owner_scene"] == host["owner_scene"])
    if n_scene + 1 > MAX_NODES_PER_SCENE: bad.append(f"GI_1 scene_limit: {host['owner_scene']} already holds {n_scene} registered nodes; the limit is {MAX_NODES_PER_SCENE}")
    if bad: return bad
    if cls == "Area2D":
        geo = scene_geometry(project, host["owner_scene"])
        if geo is None: return bad + ["GI_1 overlap: Godot could not read the scene's geometry, so the overlap guard could not run"]
        if parent == ".": base = geo["root"]
        else: base = (geo["nodes"].get(parent) or {}).get("pos") or [0.0, 0.0]
        cx, cy = base[0] + block["position"][0], base[1] + block["position"][1]; hw, hh = block["size"][0] / 2.0, block["size"][1] / 2.0
        mine = [cx - hw, cy - hh, cx + hw, cy + hh]
        for nm, d in sorted(geo["nodes"].items()):
            r = d.get("rect")
            if r and mine[0] < r[2] and r[0] < mine[2] and mine[1] < r[3] and r[1] < mine[3]:
                bad.append(f"GI_1 overlap: the new {name} would cover [{mine[0]:g}, {mine[1]:g}] to [{mine[2]:g}, {mine[3]:g}], which overlaps the collision shape of {nm} at [{r[0]:g}, {r[1]:g}] to [{r[2]:g}, {r[3]:g}]; it would fire when the scene loads. Move it or make it smaller")
    return bad

_step17_slot1 = slot1

def slot1(block, reg, spec, project):
    if block.get("op") != "add_node": return _step17_slot1(block, reg, spec, project)
    global _REG
    _REG = reg
    s, bad = spec["slots"], []
    ops = spec["ops"].get("add_node") or {}
    if ops.get("status") != "locked": return None, ["the spec is behind the clock: it has no locked 'add_node' op. Run: python3 patch_status.py"]
    want, optional = ops["slots"], ops.get("optional_slots", [])
    bad += [f"block.{k} is missing" for k in want if k not in block]
    bad += [f"block.{k} is not a slot of op add_node" for k in block if k not in want and k not in optional]
    if bad: return None, bad
    for k in ("frame_id", "subnet", "target", "ref"):
        if not re.match(s[k]["rx"], str(block[k])): bad.append(f"block.{k} '{block[k]}' does not match the spec")
    if block["time_slot"] not in s["time_slot"]["enum"]: bad.append(f"block.time_slot '{block['time_slot']}' not allowed")
    if not isinstance(block["exclude"], list) or not block["exclude"] or any(e not in s["exclude"]["enum"] for e in block["exclude"]):
        bad.append("block.exclude must be a non-empty list of names from the spec")
    host = reg.get("hosts", {}).get(block["ref"])
    if host is None: return None, bad + [f"GI_1 addr_in_registry: {block['ref']} is not in the registry"]
    if not re.match(r"^uid://[a-z0-9]+$", host.get("uid", "")): bad.append("GI_1 uid_in_registry: host has no valid uid")
    elif header_uid(project, host["scene"]) != host["uid"]: bad.append(f"GI_1 uid_in_registry: registry is stale ({host['scene']} has a different uid than the registry); run registry_builder.py")
    if block["subnet"] != host["subnet"]: bad.append(f"GI_1 subnet_boundary: block subnet {block['subnet']} but {block['ref']} is in {host['subnet']}")
    if block["target"] != host["name"]: bad.append(f"GI_1 addr_in_registry: {block['ref']} is {host['name']}, block says {block['target']}")
    if bad: return host, bad
    return host, add_node_checks(block, host, spec, project, reg)

# ---- the scene edit: the harness writes the text, the values are the mule's
def add_node_text(raw, block):
    """The scene text with the new node (and, for an Area2D, its CollisionShape2D and RectangleShape2D) added; returns (text, 1-based line of the node)."""
    nl = "\r\n" if "\r\n" in raw else "\n"; lines = raw.split(nl)
    name, cls, pp = block["name"], block["class"], node_parent_path(block["parent"])
    node = [f'[node name="{name}" type="{cls}" parent="{pp}"]', "unique_name_in_owner = true"]
    if cls != "Timer": node.append("position = " + vec_text(block["position"]))
    node.append("")
    sub = []
    if cls == "Area2D":
        sid = f"RectangleShape2D_{name.lower()}"; used = set(scene_sub_resources(raw))
        while sid in used: sid += "_n"
        child = name if pp == "." else pp + "/" + name
        node += [f'[node name="CollisionShape2D" type="CollisionShape2D" parent="{child}"]', f'shape = SubResource("{sid}")', ""]
        sub = [f'[sub_resource type="RectangleShape2D" id="{sid}"]', "size = " + vec_text(block["size"]), ""]
    first_node = next((i for i, l in enumerate(lines) if l.startswith("[node ")), None)
    if first_node is None: raise ApplyError("the scene has no [node ...] section")
    conn = [i for i, l in enumerate(lines) if l.startswith("[connection ") or l.startswith("[editable ")]
    if conn: lines[min(conn):min(conn)] = node
    else:
        while lines and not lines[-1].strip(): lines.pop()
        lines += [""] + node
    if sub: lines[first_node:first_node] = sub
    text = nl.join(lines)
    m = re.search(r"load_steps=(\d+)", text)
    if m and sub: text = text.replace(m.group(0), f"load_steps={int(m.group(1)) + 1}", 1)
    line_no = next(i for i, l in enumerate(text.split(nl)) if l.startswith(f'[node name="{name}" ')) + 1
    return text, line_no

RELOAD_NODE_GD = r"""extends SceneTree
# loads a scene and reports one node and the scene's connections. args: out, scene, node name, connections (json: [[from, signal, to, method], ...])
func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var res := {"found": false, "class": "", "unique": false, "position": null, "size": null, "n_connections": 0, "bad_connections": [], "loaded": false}
    var packed: PackedScene = load(a[1])
    if packed != null:
        var root: Node = packed.instantiate()
        res["loaded"] = true
        var n: Node = root.get_node_or_null("%" + a[2])
        if n != null:
            res["found"] = true
            res["class"] = n.get_class()
            res["unique"] = n.unique_name_in_owner
            if n is Node2D:
                res["position"] = [(n as Node2D).position.x, (n as Node2D).position.y]
            for c in n.get_children():
                if c is CollisionShape2D and (c as CollisionShape2D).shape is RectangleShape2D:
                    var sz: Vector2 = ((c as CollisionShape2D).shape as RectangleShape2D).size
                    res["size"] = [sz.x, sz.y]
        var conns: Array = JSON.parse_string(a[3])
        for c in conns:
            res["n_connections"] += 1
            var s: Node = root.get_node_or_null(NodePath(str(c[0])))
            var d: Node = root.get_node_or_null(NodePath(str(c[2])))
            if s == null or d == null or not s.is_connected(str(c[1]), Callable(d, str(c[3]))):
                res["bad_connections"].append(c)
        root.free()
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(res))
    f.close()
    quit()
"""

def scene_connections(raw):
    out = []
    for l in raw.splitlines():
        if l.startswith("[connection "):
            at = dict(re.findall(r'(\w+)="([^"]*)"', l)); out.append([at.get("from"), at.get("signal"), at.get("to"), at.get("method")])
    return out

def run_node_reload(godot, project, scene, name, conns):
    tmp = tempfile.mkdtemp(prefix="cascade_nodecheck_")
    helper, out = os.path.join(tmp, "check.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(RELOAD_NODE_GD)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, scene, name, json.dumps(conns)], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return None, "timeout (60 s)"
    if not os.path.exists(out): return None, strip_ansi(r.stdout + r.stderr)[-300:].strip()
    return json.load(open(out)), ""

def check_node_result(res, block):
    """'' when the loaded scene shows the node exactly as the block says, else the problem."""
    if not res.get("loaded"): return "the scene did not load"
    if not res.get("found"): return f"%{block['name']} is not in the loaded scene"
    if res["class"] != block["class"]: return f"%{block['name']} loaded as a {res['class']}, not a {block['class']}"
    if not res["unique"]: return f"%{block['name']} has no unique name in the scene"
    if block["class"] != "Timer" and (res["position"] is None or not _close(res["position"], [float(x) for x in block["position"]])): return f"position loaded as {res['position']}, the block says {block['position']}"
    if block["class"] == "Area2D" and (res["size"] is None or not _close(res["size"], [float(x) for x in block["size"]])): return f"shape size loaded as {res['size']}, the block says {block['size']}"
    if res.get("bad_connections"): return f"{len(res['bad_connections'])} of the scene's {res['n_connections']} connections are no longer wired: {res['bad_connections'][:3]}"
    return ""

def apply_scene_add_node(godot, project, host, block):
    """Add the node to the owner scene, with a .bak backup first and a headless reload check after. Rolls back if the check fails."""
    fp = os.path.join(project, host["owner_scene"][len("res://"):])
    if not os.path.exists(fp): raise ApplyError(f"{host['owner_scene']} not found")
    raw = open(fp, "rb").read().decode("utf-8")
    new_text, line_no = add_node_text(raw, block)
    bak = f"{fp}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(fp, bak)
    tmp = fp + ".cascade_tmp"
    open(tmp, "wb").write(new_text.encode("utf-8")); os.replace(tmp, fp)
    res, err = run_node_reload(godot, project, host["owner_scene"], block["name"], scene_connections(new_text))
    problem = err if res is None else check_node_result(res, block)
    what = f"%{block['name']} ({block['class']})"
    desc = (f"at {vec_text(block['position'])}" if block["class"] != "Timer" else "with its defaults") + (f", shape {vec_text(block['size'])}" if block["class"] == "Area2D" else "")
    result = {"file": host["owner_scene"], "backup": bak, "action": "inserted", "line": line_no, "before": None, "after": f"node {block['name']} ({block['class']}) under {block['parent'] if block['parent'] != '.' else 'the scene root'} {desc}",
              "reloaded_value": desc + f"; {res['n_connections']} scene connections still wired" if res else None, "verified": not problem, "what": what}
    if problem:
        shutil.copy2(bak, fp); result["rolled_back"] = True; result["problem"] = problem
    return result

def node_values(block):
    """The values the mule completes, one per call: [(property, [x, y])]."""
    vals = []
    if block["class"] != "Timer": vals.append(("position", [float(x) for x in block["position"]]))
    if block["class"] == "Area2D": vals.append(("size", [float(x) for x in block["size"]]))
    return vals

_step17_op_fields = op_fields

def op_fields(block):
    if block.get("op") == "add_node":
        return {k: block[k] for k in ("class", "name", "parent", "position", "size") if k in block}
    return _step17_op_fields(block)

def refresh_registry_node(ws, godot, block):
    try:
        r = subprocess.run([sys.executable, os.path.join(ws, "registry_builder.py"), "--ws", ws, "--godot", godot], capture_output=True, text=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    if r.returncode != 0: return {"ok": False, "detail": strip_ansi(r.stdout + r.stderr)[-300:].strip()}
    hosts = json.load(open(os.path.join(ws, "state_registry.json")))["hosts"]
    mine = next((a for a, h in hosts.items() if h["name"] == "%" + block["name"]), None)
    return {"ok": True, "listed": mine is not None, "addr": mine, "nodes": len(hosts)}

def verify_node_scene(godot, project, host, block):
    fp = os.path.join(project, host["owner_scene"][len("res://"):])
    conns = scene_connections(open(fp, encoding="utf-8").read())
    res, err = run_node_reload(godot, project, host["owner_scene"], block["name"], conns)
    if res is None: return {"verdict": "FAIL", "mode": "scene", "summary": "the scene check could not run: " + err, "warnings": []}
    problem = check_node_result(res, block)
    if problem: return {"verdict": "FAIL", "mode": "scene", "summary": problem, "warnings": []}
    desc = (f"at {vec_text(res['position'])}" if block["class"] != "Timer" else "with its defaults") + (f", shape {vec_text(res['size'])}" if block["class"] == "Area2D" else "")
    return {"verdict": "PASS", "mode": "scene", "summary": f"%{block['name']} ({res['class']}) is in the loaded scene {desc}, with a unique name; all {res['n_connections']} earlier scene connections are still wired", "warnings": []}

def run_node_frame(a, ws, project, host, block, spec, row, ask, set_temp, rec, finish, meas):
    vals = node_values(block); n = len(vals)
    head = (json.dumps({"frame_id": block["frame_id"], "target": block["target"]}, separators=(",", ":"))[:-1] + ',"stmt":"') if a.prefill else ""
    where = f"under {block['parent']}" if block["parent"] != "." else "under the scene root"
    row.update(prefill=bool(a.prefill), slim=bool(a.slim), ultra=bool(a.ultra), temps=a.temps, example=False, statements=n, body_mode="node")
    if a.run_id: row["run_id"] = a.run_id
    log(f"NODE            adding {block['class']} {block['name']} {where}: " + (f"{n} value(s) for the mule, one call each ({', '.join(p for p, _ in vals)})" if n else "no values for the mule (a Timer takes its defaults; later steps set wait_time, one_shot, autostart)"))
    attempts, calls, clipm = [], 0, None
    for k, (prop, val) in enumerate(vals):
        sub = {"op": "modify_property", "target": block["target"], "property": prop, "type": "Vector2", "value": val, "frame_id": block["frame_id"], "time_slot": block["time_slot"],
               "subnet": block["subnet"], "ref": block["ref"], "exclude": block["exclude"]}
        prefill = (head + f"{prop} = Vector2(") if a.prefill else ""
        if k == 0: row["message_chars"] = len(mule_message(sub, host, None, None, a.slim, a.ultra)); log(f"MESSAGE         {'ultra' if a.ultra else 'slim' if a.slim else 'full'}: {row['message_chars']} characters for value 1; one mule call per value")
        if prefill: log(f"PREFILL         ...{prefill[-60:]}  (value {k + 1}/{n}: the mule only completes the number pair)")
        prev, ok_line = None, None
        for t in range(1, a.tries + 1):
            msg = mule_message(sub, host, prev, None, a.slim, a.ultra)
            temp = a.temps[min(t, len(a.temps)) - 1]; set_temp(temp)
            if rec: rec.begin()
            try: text, nt, dt = ask(msg, prefill, rec.mark if rec else None)
            except Exception as e:
                log(f"MODEL ERROR {type(e).__name__}: {str(e)[:200]}"); finish(1, status="STOP_MODEL_ERROR", attempts=attempts, error=str(e)[:300])
            calls += 1
            clipm = rec.end() if rec else None
            if clipm:
                row["clip"] = clipm; log("CLIP            " + rec.line(clipm))
            if prefill: text = complete_prefilled(prefill, text)
            reply = parse_reply(text)
            stmt = reply["stmt"].strip() if reply and isinstance(reply.get("stmt"), str) else text.strip()[:200]
            log(f"SLOT_3 EXECUTE  value {k + 1}/{n} try {t}/{a.tries}  (T={temp}, {nt} tokens, {dt:.0f}s)  line: {stmt.replace(chr(10), chr(92) + 'n')}")
            err = gi3(reply, sub, spec)
            if err:
                log(f"GI_3            NAK  {err}")
                attempts.append({"value": k + 1, "property": prop, "try": t, "temp": temp, "stmt": stmt, "status": "NAK", "error": err}); prev = {"try": t, "stmt": stmt, "error": err}; continue
            log("GI_3            ok")
            attempts.append({"value": k + 1, "property": prop, "try": t, "temp": temp, "stmt": stmt, "status": "LINE_OK"}); ok_line = stmt; break
        if ok_line is None:
            dl = os.path.join(ws, "dead_letter"); os.makedirs(dl, exist_ok=True)
            path = os.path.join(dl, f"{block['frame_id']}.json")
            json.dump({"block": block, "rag": host, "failed_value": k + 1, "property": prop, "attempts": attempts}, open(path, "w"), indent=2)
            log(f"RESULT: DEAD LETTER value {k + 1}/{n} ({prop}) failed {a.tries} tries -> {path}")
            finish(3, status="DLQ", tries=calls, attempts=attempts, dead_letter=path, failed_value=k + 1)
    if rec and clipm and vals: clipm["map_recorded"] = rec.commit(); row["clip"] = clipm
    log("SLOT_4 JUDGE    ACK  " + (f"every value checked exactly by GI_3; the scene reload below is the parse check" if vals else "no mule values; the scene reload below is the parse check"))
    status, extra, code = "ACK", {}, 0
    if not a.no_apply:
        try:
            res = apply_scene_add_node(a.godot, project, host, block)
        except ApplyError as e:
            log(f"SLOT_4 APPLY    NOT WRITTEN  {e}"); status, extra, code = "ACK_NOT_APPLIED", {"apply_error": str(e)}, 4
        else:
            scene = res["file"][len("res://"):]
            log(f"SLOT_4 APPLY    {res['action']} line {res['line']} of {scene}: {res['after']}")
            log(f"                backup: {os.path.relpath(res['backup'], ws)}")
            if res["verified"]:
                log(f"SLOT_4 RELOAD   ok   {res['what']} {res['reloaded_value']}")
                status, extra = "APPLIED", {"apply": res}
                if not a.no_verify:
                    vr = verify_node_scene(a.godot, project, host, block); extra["verify"] = vr
                    log("SLOT_5 VERIFY   " + verify_line(vr))
                    if vr["verdict"] == "FAIL":
                        shutil.copy2(res["backup"], os.path.join(project, res["file"][len("res://"):]))
                        log("SLOT_5 VERIFY   FAILED, ROLLED BACK  the edit was undone"); status, code = "ROLLED_BACK_VERIFY", 4
                if status == "APPLIED":
                    rr = refresh_registry_node(ws, a.godot, block); extra["registry_refresh"] = rr
                    log(f"REGISTRY       refreshed: %{block['name']} is listed at {rr['addr']} ({rr['nodes']} nodes in the registry now)" if rr.get("ok") and rr.get("listed") else f"REGISTRY       NOT refreshed - run registry_builder.py by hand ({rr.get('detail') or 'the node is not listed'})")
            else:
                log(f"SLOT_4 RELOAD   FAILED, ROLLED BACK  {res['problem']}"); status, extra, code = "ROLLED_BACK", {"apply": res}, 4
    log(f"RESULT: {status} in {calls} mule call(s) -> ledger.jsonl")
    finish(code, status=status, tries=calls, stmt=f"add {block['class']} {block['name']} {where}", uid=host["uid"], **op_fields(block), attempts=attempts, gi2=meas, **extra)

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
OLD_DISPATCH = '''    if is_body_frame(block):
        return run_body_frame(a, ws, project, host, block, spec, row, ask, set_temp, rec, finish, meas)
'''
NEW_DISPATCH = '''    if block["op"] == "add_node":
        return run_node_frame(a, ws, project, host, block, spec, row, ask, set_temp, rec, finish, meas)
    if is_body_frame(block):
        return run_body_frame(a, ws, project, host, block, spec, row, ask, set_temp, rec, finish, meas)
'''
OLD_NOOP = '''        log(f"RESULT: NOOP - {block['fn']}() is already in {host['script']} exactly as asked, so there is nothing to do"); finish(0, status="NOOP_ALREADY_DEFINED", uid=host["uid"], **op_fields(block))
'''
NEW_NOOP = OLD_NOOP + '''    if block["op"] == "add_node" and node_matches(project, host, block):
        log(f"RESULT: NOOP - %{block['name']} ({block['class']}) is already in {host['owner_scene']} exactly as asked, so there is nothing to do"); finish(0, status="NOOP_ALREADY_PRESENT", uid=host["uid"], **op_fields(block))
'''
OLD_ARGS = '''    a.temps = [float(x) for x in a.temps.split(",")]
'''
NEW_ARGS = '''    a.temps = [float(x) for x in a.temps.split(",")]
    set_godot(a.godot)
'''
OLD_FRAME = """    log(f"FRAME {block.get('frame_id')}  {block.get('op')}  " + (f"{block['source']}.{block['signal']} -> " if block.get('op') == 'connect_signal' and block.get('source') != block.get('target') else "") + f"{block.get('target')}."""
NEW_FRAME = """    log(f"FRAME {block.get('frame_id')}  {block.get('op')}  " + (f"%{block['name']} ({block['class']}) in the scene of {block['target']}" if block.get('op') == 'add_node' else "") + (f"{block['source']}.{block['signal']} -> " if block.get('op') == 'connect_signal' and block.get('source') != block.get('target') else "") + ("" if block.get('op') == 'add_node' else f"{block.get('target')}."""
OLD_FRAME_END = """or block.get('fn')}")
"""
NEW_FRAME_END = """or block.get('fn')}"))
"""
CLOCK = [(MARK, NEW_SECTION + MARK), (OLD_DISPATCH, NEW_DISPATCH), (OLD_NOOP, NEW_NOOP), (OLD_ARGS, NEW_ARGS), (OLD_FRAME, NEW_FRAME), (OLD_FRAME_END, NEW_FRAME_END)]

entries = [
 {"file": "cascade_clock.py", "marker": "def run_node_frame", "edits": CLOCK},
 {"file": "task_block_spec.json", "done": lambda d: "add_node" in d["ops"], "edit": spec_edit},
]
blocked = pl.run(ws, "node", entries)
sys.exit(1 if blocked else 0)
