# step18_paste.sh - Step 18: add_node (the plans can put a trigger, pickup, marker or timer in the scene and then act on it)
# Paste this whole block into a terminal. No GPU, no mule: the last commands are dry runs on a throwaway copy.
set -u
cd ~/cascade_ws || { echo "STOPPED: ~/cascade_ws not found"; exit 1; }
export GODOT="${GODOT:-godot}"

# ---- 1. keep the files this step replaces or edits (backups are made once)
for f in cascade_plan.py cascade_vocab.json harness_protocol.json patch_status.py patch_node.py cascade_clock.py task_block_spec.json live_touch.gd; do
  [ -f "$f" ] && [ ! -f "$f.before-step18" ] && cp "$f" "$f.before-step18"
done

# ---- 2. write patch_node.py
cat << 'PNEOF' > ~/cascade_ws/patch_node.py
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
PNEOF

# ---- 2. write cascade_plan.py
cat << 'PLANEOF' > ~/cascade_ws/cascade_plan.py
#!/usr/bin/env python3
"""cascade_plan.py - Project Cascade, Steps 11 to 18: the harness layer.

  python3 cascade_plan.py catalog                  what the harness may plan with (paste it into the session)
  python3 cascade_plan.py expand  plan.json        the primitive blocks a compact plan turns into (nothing runs)
  python3 cascade_plan.py dry-run plan.json        the whole pipeline on a throwaway copy of the workspace: no mule, nothing real touched
  python3 cascade_plan.py run     plan.json        dry run first, then the real chain (use the venv python: it loads Gemma)
  python3 cascade_plan.py undo    <plan id>        restore the plan's backups, newest change first (refuses if later work depends on them)
  python3 cascade_plan.py status                   the plans so far

The part that reads English is the harness model. This tool is the deterministic side: it fills in what the registry knows,
runs every guard before anything real happens, and can take a plan back out.
"""
import argparse, atexit, difflib, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time

OK_STATUS = ("ACK", "APPLIED", "NOOP_ALREADY_CONNECTED", "NOOP_ALREADY_DEFINED", "NOOP_ALREADY_PRESENT")
NOOP_STATUS = ("NOOP_ALREADY_CONNECTED", "NOOP_ALREADY_DEFINED", "NOOP_ALREADY_PRESENT")
EXC = {"add_method": ["KinematicBody2D", "untyped_variables", "yield", "relative_paths_up_tree", "cross_subnet_queries"],
       "connect_signal": ["KinematicBody2D", "connect_3arg", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"],
       "modify_property": ["KinematicBody2D", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"],
       "add_node": ["KinematicBody2D", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"]}
NODE_CLASSES = ("Area2D", "Timer", "Marker2D", "Node2D")
MAX_NODES_PER_PLAN = 8
RELATIVE_TYPES = ("float", "int", "Vector2", "Vector3")
RELATIVE_VERBS = (("increase", "+=", "grows by"), ("decrease", "-=", "shrinks by"), ("scale", "*=", "is multiplied by"))
MAX_STATEMENTS = 6
STEP_KEYS = {"when": {"signal", "then"}, "set_property": {"property", "value"}, "add_function": {"name", "then"}, "connect": {"signal", "handler"}, "change_handler": {"handler", "then"}, "after": {"seconds", "then"}, "every": {"seconds", "then"}, "add_node": {"class", "name"}}
# Step 14: "from" names the node that emits the signal (the step's node receives it). Step 15: "node" lets one step work on another node than the plan's; "timer" names the Timer an "after" uses.
OPTIONAL_KEYS = {"when": {"from", "node", "only"}, "connect": {"from", "node"}, "set_property": {"node"}, "add_function": {"node"}, "change_handler": {"node"}, "after": {"timer", "node"}, "every": {"timer", "node"}, "add_node": {"at", "size", "parent", "node"}}


class PlanError(Exception):
    pass


def load_json(p):
    return json.load(open(p, encoding="utf-8"))


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest() if os.path.exists(path) else None


class Ctx:
    def __init__(self, a):
        self.a = a
        self.ws = os.path.abspath(os.path.expanduser(a.ws))
        self.project = os.path.join(self.ws, "engine_test")
        if not os.path.exists(os.path.join(self.ws, "cascade_clock.py")):
            sys.exit(f"STOPPED: cascade_clock.py is not in {self.ws}")
        sys.path.insert(0, self.ws)
        import cascade_clock
        self.clock = cascade_clock
        self.reg = load_json(os.path.join(self.ws, "state_registry.json"))
        vp = os.path.join(self.ws, "cascade_vocab.json")
        self.vocab = load_json(vp) if os.path.exists(vp) else {"signals": {}, "steps": [], "supported_intents": [], "refusals": [], "values": ""}
        self._preview = None

    def preview_add_node(self, block):
        """Apply an add_node block to a temporary copy of the workspace and rebuild its registry, so later steps of the same plan can name the new node."""
        c = self.clock
        if self._preview is None:
            tmp = tempfile.mkdtemp(prefix="cascade_preview_"); t = os.path.join(tmp, "ws")
            shutil.copytree(self.ws, t, ignore=shutil.ignore_patterns("sweep", "dead_letter", "plans", "*.bak", "__pycache__", "ledger.jsonl", "*.before-*"))
            self._preview = t
            atexit.register(shutil.rmtree, tmp, True)
        t = self._preview; proj = os.path.join(t, "engine_test")
        reg = load_json(os.path.join(t, "state_registry.json")); spec = load_json(os.path.join(t, "task_block_spec.json"))
        c.set_godot(self.a.godot)
        b = dict(block); b["frame_id"] = "0x7FF0"
        host, bad = c.slot1(b, reg, spec, proj)
        if bad:
            raise PlanError("; ".join(bad))
        if not c.node_matches(proj, host, b):
            try:
                res = c.apply_scene_add_node(self.a.godot, proj, host, b)
            except c.ApplyError as e:
                raise PlanError(f"the node could not be added: {e}")
            if not res.get("verified"):
                raise PlanError(f"the node could not be added: {res.get('problem')}")
            rr = c.refresh_registry_node(t, self.a.godot, b)
            if not rr.get("ok") or not rr.get("listed"):
                raise PlanError("the registry could not list the new node: " + str(rr.get("detail") or "not listed"))
        self.reg = load_json(os.path.join(t, "state_registry.json"))

    def hosts(self):
        return list(self.reg["hosts"].values())

    def host_named(self, node):
        if node is None:
            if len(self.hosts()) != 1:
                raise PlanError("the plan must name its node: " + ", ".join(h["name"] for h in self.hosts()))
            return self.hosts()[0]
        for h in self.hosts():
            if h["name"] == node:
                return h
        raise PlanError(f"node {node} is not in the registry (nodes: {', '.join(h['name'] for h in self.hosts())})")


# ------------------------------------------------------------------ expansion: compact steps -> primitive blocks
def body_from_then(then, host, where):
    if not isinstance(then, dict):
        raise PlanError(f"{where}: 'then' must be an object")
    if set(then) == {"nothing"} and then["nothing"] is True:
        return {"body": "pass", "message": ""}
    if set(then) == {"print"} and isinstance(then["print"], str):
        return {"body": "print", "message": then["print"]}
    if set(then) == {"set"} and isinstance(then["set"], dict) and set(then["set"]) == {"property", "value"}:
        prop = then["set"]["property"]
        if prop not in host["props"]:
            raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
        return {"body": "assign", "message": "", "property": prop, "type": host["props"][prop], "value": then["set"]["value"]}
    for verb, op, _ in RELATIVE_VERBS:
        if set(then) == {verb} and isinstance(then[verb], dict) and set(then[verb]) == {"property", "by"}:
            prop = then[verb]["property"]
            if prop not in host["props"]:
                raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
            if host["props"][prop] not in RELATIVE_TYPES:
                raise PlanError(f"{where}: {verb} works on {', '.join(RELATIVE_TYPES)} properties, but {prop} is {host['props'][prop]}")
            return {"body": "assign", "message": "", "property": prop, "type": host["props"][prop], "operator": op, "value": then[verb]["by"]}
    raise PlanError(f"{where}: 'then' must be exactly one of {{\"set\": {{\"property\": P, \"value\": V}}}}, {{\"increase\"|\"decrease\"|\"scale\": {{\"property\": P, \"by\": V}}}}, {{\"print\": \"message\"}}, {{\"nothing\": true}}")


def statement_from_form(form, host, where):
    """One item of a 'then' list as a statement object, typed from the registry."""
    if not isinstance(form, dict):
        raise PlanError(f"{where}: every item of 'then' must be an object")
    if set(form) == {"nothing"} and form["nothing"] is True:
        return {"kind": "pass"}
    if set(form) == {"print"} and isinstance(form["print"], str):
        return {"kind": "print", "message": form["print"]}
    if set(form) == {"set"} and isinstance(form["set"], dict) and set(form["set"]) == {"property", "value"}:
        prop = form["set"]["property"]
        if prop not in host["props"]:
            raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
        return {"kind": "assign", "property": prop, "type": host["props"][prop], "value": form["set"]["value"]}
    for verb, op, _ in RELATIVE_VERBS:
        if set(form) == {verb} and isinstance(form[verb], dict) and set(form[verb]) == {"property", "by"}:
            prop = form[verb]["property"]
            if prop not in host["props"]:
                raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
            if host["props"][prop] not in RELATIVE_TYPES:
                raise PlanError(f"{where}: {verb} works on {', '.join(RELATIVE_TYPES)} properties, but {prop} is {host['props'][prop]}")
            return {"kind": "relative", "property": prop, "type": host["props"][prop], "operator": op, "value": form[verb]["by"]}
    if set(form) == {"limit"} and isinstance(form["limit"], dict) and "property" in form["limit"] and len(form["limit"]) == 2 and (set(form["limit"]) - {"property"}) <= {"max", "min", "max_length"}:
        prop, bound = form["limit"]["property"], next(iter(set(form["limit"]) - {"property"}))
        if prop not in host["props"]:
            raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
        t = host["props"][prop]
        if t in ("Vector2", "Vector3"):
            if bound != "max_length":
                raise PlanError(f"{where}: {prop} is a {t}; limit it with max_length (the longest the vector may get)")
        elif t in ("float", "int"):
            if bound not in ("max", "min"):
                raise PlanError(f"{where}: {prop} is a {t}; limit it with max (never above) or min (never below)")
        else:
            raise PlanError(f"{where}: a limit works on float, int, Vector2 and Vector3 properties, but {prop} is {t}")
        return {"kind": "limit", "property": prop, "type": t, "bound": bound, "value": form["limit"][bound]}
    if set(form) == {"free"} and isinstance(form["free"], str) and (form["free"] == "self" or re.match(r"^%[A-Za-z_]\w*$", form["free"])):
        return {"kind": "free", "target": form["free"]}
    raise PlanError(f"{where}: each 'then' item must be exactly one of {{\"set\": {{\"property\": P, \"value\": V}}}}, {{\"increase\"|\"decrease\"|\"scale\": {{\"property\": P, \"by\": V}}}}, {{\"print\": \"message\"}}, {{\"nothing\": true}}, {{\"limit\": {{\"property\": P, \"max\"|\"min\"|\"max_length\": V}}}}, {{\"free\": \"%Node\"|\"self\"}}")


def body_from_then_any(then, host, where, force_array=False):
    """A single form keeps the proven single-statement block; a list of two or more (or a replacement) becomes an array body."""
    if isinstance(then, list):
        if not then or len(then) > MAX_STATEMENTS:
            raise PlanError(f"{where}: 'then' as a list holds 1 to {MAX_STATEMENTS} items, it has {len(then)}")
        sts = [statement_from_form(f, host, f"{where}, item {i}") for i, f in enumerate(then, 1)]
        if len(sts) == 1 and sts[0]["kind"] == "limit":
            raise PlanError(f"{where}: a limit needs a relative change (increase, decrease, scale) on the same property before it; a limit by itself bounds nothing")
        if len(sts) == 1 and not force_array and sts[0]["kind"] != "free":
            return body_from_then(then[0], host, where)
        return {"statements": sts}
    if force_array or (isinstance(then, dict) and set(then) == {"free"}):
        return {"statements": [statement_from_form(then, host, where)]}
    return body_from_then(then, host, where)


def body_relatives(body):
    """(property, operator, value) of every relative statement in a body, whichever form it is in."""
    if "statements" in body:
        return [(s["property"], s["operator"], s["value"]) for s in body["statements"] if s["kind"] == "relative"]
    return [(body["property"], body["operator"], body["value"])] if body.get("operator", "=") != "=" else []


def head(host, op):
    return {"frame_id": None, "time_slot": "SLOT_3_EXECUTE", "subnet": host["subnet"], "op": op}


def add_method_block(host, fn, body, mode=None):
    b = head(host, "add_method")
    if "statements" in body:
        b.update(target=host["name"], fn=fn, args=[], returns="void", body=body["statements"], message="")
    else:
        b.update(target=host["name"], fn=fn, args=[], returns="void", body=body["body"], message=body["message"])
        for k in ("property", "type", "operator", "value"):
            if k in body:
                b[k] = body[k]
    if mode:
        b["mode"] = mode
    b.update(ref=host["addr"], exclude=list(EXC["add_method"]))
    return b


def connect_block(host, signal, method, src=None):
    b = head(host, "connect_signal")
    b.update(source=(src or host)["name"], target=host["name"], signal=signal, method=method, ref=host["addr"], exclude=list(EXC["connect_signal"]))
    return b


def modify_block(host, prop, value):
    if prop not in host["props"]:
        raise PlanError(f"set_property: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
    b = head(host, "modify_property")
    b.update(target=host["name"], property=prop, type=host["props"][prop], value=value, ref=host["addr"], exclude=list(EXC["modify_property"]))
    return b


def expand_plan(ctx, plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or not plan["steps"]:
        raise PlanError("a plan is an object with a non-empty 'steps' list")
    if not isinstance(plan.get("intent"), str) or not plan["intent"].strip():
        raise PlanError("a plan needs an 'intent' (the user's words)")
    host = ctx.host_named(plan.get("node"))
    snake, blocks, notes = ctx.clock.snake, [], []
    for n, step in enumerate(plan["steps"], 1):
        where = f"step {n}"
        do = step.get("do") if isinstance(step, dict) else None
        if do not in STEP_KEYS:
            raise PlanError(f"{where}: unknown step '{do}' (steps: {', '.join(STEP_KEYS)})")
        extra = set(step) - STEP_KEYS[do] - OPTIONAL_KEYS.get(do, set()) - {"do"}
        missing = STEP_KEYS[do] - set(step)
        if extra or missing:
            raise PlanError(f"{where} ({do}): " + (f"unexpected {sorted(extra)} " if extra else "") + (f"missing {sorted(missing)}" if missing else ""))
        hh = ctx.host_named(step["node"]) if "node" in step else host
        src = hh
        if do in ("when", "connect") and "from" in step:
            src = ctx.host_named(step["from"])
            if src["name"] == hh["name"]:
                raise PlanError(f"{where}: 'from' names the plan's own node {hh['name']}; leave 'from' out for a signal of the node itself")
            if src["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {src['name']} is in {src['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
        if do in ("when", "connect") and step["signal"] not in src.get("signals", []):
            close = difflib.get_close_matches(str(step["signal"]), src.get("signals", []), n=3)
            raise PlanError(f"{where}: '{step['signal']}' is not a signal of {src['name']}" + (f" (did you mean {', '.join(close)}?)" if close else ""))
        if do == "add_node":
            cls, name = step["class"], step["name"]
            n_added = sum(1 for b0 in blocks if b0["op"] == "add_node") + 1
            if n_added > MAX_NODES_PER_PLAN:
                raise PlanError(f"{where}: a plan adds at most {MAX_NODES_PER_PLAN} nodes")
            if cls not in NODE_CLASSES:
                raise PlanError(f"{where}: class '{cls}' cannot be added (one of {', '.join(NODE_CLASSES)})")
            if not isinstance(name, str):
                raise PlanError(f"{where}: 'name' must be text")
            at, size, parent = step.get("at"), step.get("size"), step.get("parent", ".")
            if cls == "Timer" and at is not None:
                raise PlanError(f"{where}: a Timer has no position; leave 'at' out")
            if cls != "Timer":
                at = [0, 0] if at is None else at
                if not (isinstance(at, list) and len(at) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in at)):
                    raise PlanError(f"{where}: 'at' must be [x, y], two numbers")
            if cls == "Area2D":
                size = [48, 48] if size is None else size
                if not (isinstance(size, list) and len(size) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in size)):
                    raise PlanError(f"{where}: 'size' must be [width, height], two numbers")
            elif size is not None:
                raise PlanError(f"{where}: 'size' is only for an Area2D (the size of its rectangle shape)")
            blk = head(hh, "add_node")
            blk.update(target=hh["name"], ref=hh["addr"], exclude=list(EXC["add_node"]))
            blk.update({"class": cls, "name": name, "parent": parent})
            if cls != "Timer":
                blk["position"] = [float(at[0]), float(at[1])]
            if cls == "Area2D":
                blk["size"] = [float(size[0]), float(size[1])]
            ctx.preview_add_node(blk)
            blocks.append(blk)
            newh = next((h for h in ctx.hosts() if h["name"] == "%" + name), None)
            notes.append(f"add_node: %{name} ({cls}) is added to {hh['owner_scene']}" + (f" and gets the address {newh['addr']}" if newh else "") + f"; later steps in this plan can name it as %{name}")
        elif do == "when":
            c = ctx.clock
            fn = f"_on_{snake(src['name'])}_{step['signal']}"
            has_args = bool((src.get("signal_args") or {}).get(step["signal"]))
            hargs = []
            if has_args:
                types = c.signal_arg_types(src, step["signal"])
                if types is None:
                    raise PlanError(f"{where}: the registry has no argument classes for {src['name']}.{step['signal']}; run registry_builder.py again")
                if not c.signal_args_supported(types):
                    raise PlanError(f"{where}: {src['name']}.{step['signal']} carries ({', '.join(t or '?' for t in types)}); plans support up to 3 arguments that are Node classes or bool, int, float, String, Vector2, Vector3, Color")
                hargs = c.handler_args_for(step["signal"], types)
            only = step.get("only")
            if only is not None and only != "self":
                raise PlanError(f"{where}: 'only' must be \"self\" (the handler runs only when the argument is the receiver itself)")
            if only and (not hargs or c.arg_class(hargs[0]) not in c.NODE_ARG_CLASSES):
                raise PlanError(f"{where}: 'only' needs a signal whose first argument is a Node (body_entered, area_entered); {src['name']}.{step['signal']} has none")
            has_free = isinstance(step["then"], list) and any(isinstance(f, dict) and "free" in f for f in step["then"]) or (isinstance(step["then"], dict) and "free" in step["then"])
            body = body_from_then_any(step["then"], hh, where, force_array=bool(hargs) or has_free)
            blk = add_method_block(hh, fn, body)
            if hargs:
                blk["args"] = hargs
                if only:
                    blk["guard"] = "self"
            blocks.append(blk)
            blocks.append(connect_block(hh, step["signal"], fn, src))
            sts = c.statements_of(blk)
            emitter_freed = any(s2["kind"] == "free" and s2["target"] == src["name"] for s2 in sts)
            repeating = bool(re.search(c.REPEATING_SIGNAL_RX, step["signal"])) and src is not hh
            if repeating and not emitter_freed:
                problems = c.compounding_errors(sts)
                if problems:
                    raise PlanError(f"{where} (when): {src['name']}.{step['signal']} can fire again every time something enters or leaves, and " + "; ".join(problems) + f"; or end the list with {{\"free\": \"{src['name']}\"}} so it can only happen once")
            if ctx.vocab.get("signals", {}).get(step["signal"], {}).get("repeats", True) and not repeating:
                for prop, op, val in body_relatives(body):
                    how = next(h for v, o, h in RELATIVE_VERBS if o == op)
                    notes.append(f"{step['signal']} can fire many times; each time it fires, {prop} {how} {val}, so the change compounds")
            if hargs:
                notes.append(f"{src['name']}.{step['signal']} hands the handler its argument ({', '.join(c.arg_class(x) for x in hargs)}); the statements do not read it" + ("; the handler runs only when it is the receiver itself" if only else "; any body that enters triggers it (add \"only\": \"self\" to limit it to the receiver)"))
            if emitter_freed:
                notes.append(f"{src['name']} is freed by the handler, so this can happen only once")
            if src is not hh and ctx.vocab.get("signals", {}).get(step["signal"], {}).get("group") == "never":
                notes.append(f"{src['name']}.{step['signal']} never fires by itself: the behavior check emits it, and in a running game something has to start or emit it (an 'after' step starts a Timer)")
        elif do == "change_handler":
            fn = step["handler"]
            if fn not in hh.get("methods", []):
                close = difflib.get_close_matches(str(fn), hh.get("methods", []), n=3)
                raise PlanError(f"{where}: '{fn}' is not a function of {hh['name']}" + (f" (did you mean {', '.join(close)}?)" if close else "") + " (handlers: " + ", ".join(m for m in hh.get("methods", []) if m.startswith("_on_")) + ")")
            raw_h = open(os.path.join(ctx.project, hh["script"][len("res://"):]), encoding="utf-8").read()
            hargs, hguard = handler_signature(ctx.clock, raw_h, fn)
            if not re.match(r"^_on_[a-z0-9_]+$", fn) or (hh.get("method_args", {}).get(fn, 0) != 0 and not hargs):
                raise PlanError(f"{where}: {fn} cannot be replaced: only handlers (_on_...) whose arguments are typed 'name: Type' can")
            body = body_from_then_any(step["then"], hh, where, force_array=True)
            blk = add_method_block(hh, fn, body, mode="replace")
            if hargs:
                blk["args"] = hargs
            if hguard:
                blk["guard"] = "self"
                notes.append(f"{fn} keeps its first line 'if {hargs[0].split(':')[0]} != self: return'; to drop or add the guard, undo its plan and write the plan again")
            blocks.append(blk)
            conns = [sg for _, sg in ctx.clock.connections_into(ctx.project, hh, fn)]
            if not conns:
                notes.append(f"{fn} is not connected to any signal, so nothing calls it yet")
            for sig in conns:
                if ctx.vocab.get("signals", {}).get(sig, {}).get("repeats", True) and not re.search(ctx.clock.REPEATING_SIGNAL_RX, sig):
                    for prop, op, val in body_relatives(body):
                        how = next(h for v, o, h in RELATIVE_VERBS if o == op)
                        notes.append(f"{sig} can fire many times; each time it fires, {prop} {how} {val}, so the change compounds")
        elif do == "after":
            secs = step["seconds"]
            if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not (0.05 <= secs <= 3600):
                raise PlanError(f"{where}: 'seconds' must be a number between 0.05 and 3600")
            timers = [t for t in ctx.hosts() if t["node"] == "Timer"]
            if "timer" in step:
                tm = ctx.host_named(step["timer"])
                if tm["node"] != "Timer":
                    raise PlanError(f"{where}: {tm['name']} is a {tm['node']}, not a Timer")
            elif len(timers) == 1:
                tm = timers[0]
            else:
                raise PlanError(f"{where}: " + ("there is no Timer in the registry" if not timers else "name the timer with 'timer': " + ", ".join(t["name"] for t in timers)))
            if tm["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {tm['name']} is in {tm['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
            for prop, val in (("wait_time", float(secs)), ("one_shot", True), ("autostart", True)):
                if prop not in tm["props"]:
                    raise PlanError(f"{where}: {tm['name']} has no registered property '{prop}' (patch_after.py adds the Timer to props_allow.json; then run registry_builder.py)")
                blocks.append(modify_block(tm, prop, val))
            fn = f"_on_{snake(tm['name'])}_timeout"
            body = body_from_then_any(step["then"], hh, where, force_array=fn in hh.get("methods", []))
            blocks.append(add_method_block(hh, fn, body, mode="replace" if fn in hh.get("methods", []) else None))
            blocks.append(connect_block(hh, "timeout", fn, tm))
            cap = getattr(ctx.clock, "LIVE_CAP_SECONDS", 30.0)
            notes.append(f"after: {tm['name']} counts {secs} s from the moment the scene loads and fires once (one_shot); " + (f"the dry run and the run each wait about {secs} s for the live check" if secs <= cap else f"{secs} s is over the {cap:.0f} s live-check limit, so only the explicit-emit check runs"))
            if fn in hh.get("methods", []):
                notes.append(f"{fn} already exists on {hh['name']}: its whole body is replaced (the dry run shows what it did before)")
        elif do == "every":
            secs = step["seconds"]
            if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not (0.5 <= secs <= 3600):
                raise PlanError(f"{where}: 'seconds' must be a number between 0.5 and 3600 (a repeating handler runs this often for as long as the scene lives)")
            timers = [t for t in ctx.hosts() if t["node"] == "Timer"]
            if "timer" in step:
                tm = ctx.host_named(step["timer"])
                if tm["node"] != "Timer":
                    raise PlanError(f"{where}: {tm['name']} is a {tm['node']}, not a Timer")
            elif len(timers) == 1:
                tm = timers[0]
            else:
                raise PlanError(f"{where}: " + ("there is no Timer in the registry" if not timers else "name the timer with 'timer': " + ", ".join(t["name"] for t in timers)))
            if tm["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {tm['name']} is in {tm['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
            for prop, val in (("wait_time", float(secs)), ("one_shot", False), ("autostart", True)):
                if prop not in tm["props"]:
                    raise PlanError(f"{where}: {tm['name']} has no registered property '{prop}' (patch_after.py adds the Timer to props_allow.json; then run registry_builder.py)")
            fn = f"_on_{snake(tm['name'])}_timeout"
            body = body_from_then_any(step["then"], hh, where, force_array=True)
            problems = ctx.clock.compounding_errors(body["statements"])
            if problems:
                raise PlanError(f"{where} (every): this handler would run every {secs} s for as long as the scene lives, and " + "; ".join(problems))
            exists = fn in hh.get("methods", [])
            blocks.append(add_method_block(hh, fn, body, mode="replace" if exists else None))
            blocks.append(connect_block(hh, "timeout", fn, tm))
            for prop, val in (("wait_time", float(secs)), ("one_shot", False), ("autostart", True)):
                blocks.append(modify_block(tm, prop, val))
            lims = [f"{s['property']} {s['bound']} {s['value']}" for s in body["statements"] if s["kind"] == "limit"]
            cap = getattr(ctx.clock, "LIVE_CAP_SECONDS", 30.0)
            notes.append(f"every: {tm['name']} fires every {secs} s from the moment the scene loads, for as long as the scene lives" + (f"; bounded by: {', '.join(lims)}" if lims else "") + "; "
                         + (f"the dry run and the run each watch about {min(3 * secs, cap):.1f} s (at least 3 firings) in the live check" if secs <= cap else f"{secs} s is over the {cap:.0f} s live-check limit, so only the explicit-emit check runs"))
            if exists:
                notes.append(f"{fn} already exists on {hh['name']}: its whole body is replaced (the dry run shows what it did before)")
        elif do == "add_function":
            blocks.append(add_method_block(hh, step["name"], body_from_then_any(step["then"], hh, where)))
        elif do == "set_property":
            blocks.append(modify_block(hh, step["property"], step["value"]))
        else:
            blocks.append(connect_block(hh, step["signal"], step["handler"], src))
    return host, blocks, notes


def peek_ids(ws, n):
    p = os.path.join(ws, "frame_counter.json")
    nxt = load_json(p)["next"] if os.path.exists(p) else 0x6000
    return [f"0x{nxt + i:04X}" for i in range(n)], nxt + n


def commit_ids(ws, nxt):
    json.dump({"next": nxt}, open(os.path.join(ws, "frame_counter.json"), "w"))


def describe(ctx, b):
    c = ctx.clock
    try:
        if b["op"] == "add_method":
            return ("REPLACE " if b.get("mode") == "replace" else "") + f"{b['fn']}({', '.join(c.arg_class(x) for x in b.get('args') or [])}): {c.body_line(b)}"
        if b["op"] == "add_node":
            return f"ADD %{b['name']} ({b['class']})" + (f" at ({b['position'][0]:g}, {b['position'][1]:g})" if "position" in b else "") + (f", shape {b['size'][0]:g} x {b['size'][1]:g}" if "size" in b else "") + (f" under {b['parent']}" if b["parent"] != "." else "")
        if b["op"] == "connect_signal":
            if b["source"] != b["target"]:
                return f"{b['source']}.{b['signal']} -> {b['target']}.{b['method']}"
            return f"{b['signal']} -> {b['method']}"
        return f"{b['target']}.{b['property']} = {c.scene_literal(b['type'], b['value'])}"
    except (ValueError, TypeError, KeyError):
        return f"{b.get('fn') or b.get('property') or b.get('signal')}: {b.get('value')!r}   (malformed)"


def load_plan(ctx, path):
    if not os.path.exists(path):
        sys.exit(f"STOPPED: {path} not found")
    try:
        plan = load_json(path)
        host, blocks, notes = expand_plan(ctx, plan)
    except (PlanError, ValueError) as e:
        sys.exit(f"PLAN REFUSED: {e}")
    ids, nxt = peek_ids(ctx.ws, len(blocks))
    for b, i in zip(blocks, ids):
        b["frame_id"] = i
    return plan, host, blocks, nxt, notes


# ------------------------------------------------------------------ dry run: the real pipeline on a throwaway copy
def canonical_reply(c, b, project, host):
    if b["op"] == "add_method":
        raw = open(c.script_path(project, host), encoding="utf-8").read()
        stmt = c.signature(b) + "\n" + c.detect_indent(raw) + c.body_line(b)
    elif b["op"] == "connect_signal":
        stmt = c.cross_stmt(b) if b["source"] != b["target"] else f"{b['signal']}.connect({b['method']})"
    else:
        stmt = f"{b['property']} = {c.scene_literal(b['type'], b['value'])}"
    return json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": stmt})


def canonical_replies(c, b, project, host):
    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""
    if b["op"] == "add_node":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": f"{prop} = {c.scene_literal('Vector2', val)}"}) for prop, val in c.node_values(b)]
    if b["op"] == "add_method" and c.is_body_frame(b):
        raw = open(c.script_path(project, host), encoding="utf-8").read()
        indent, lines, sig = c.detect_indent(raw), c.body_lines(b), c.signature(b)
        g = len(c.guard_prefix(b)) if hasattr(c, "guard_prefix") else 0
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": sig + "\n" + "\n".join(indent + l for l in lines[:k + 1])}) for k in range(g, len(lines))]
    return [canonical_reply(c, b, project, host)]


def row_result(row):
    v = row.get("verify") or {}
    note = ""
    if row["status"].startswith("REJECT"):
        note = "; ".join(row.get("problems") or [])
    elif row["status"] == "DLQ":
        note = "; ".join(f"try {a['try']}: {a.get('error', a['status'])}" for a in row.get("attempts", []))
    elif row["status"] == "ROLLED_BACK_VERIFY":
        note = v.get("summary", "")
    elif row["status"] == "ROLLED_BACK":
        note = (row.get("apply") or {}).get("problem", "")
    return {"status": row["status"], "verify": v.get("verdict"), "how": ("explicit emit" if v.get("explicit") else "natural") if v.get("mode") == "signal" else None, "note": note}


def dry_run(ctx, blocks):
    c, a = ctx.clock, ctx.a
    tmp = tempfile.mkdtemp(prefix="cascade_dry_")
    t = os.path.join(tmp, "ws")
    shutil.copytree(ctx.ws, t, ignore=shutil.ignore_patterns("sweep", "dead_letter", "plans", "*.bak", "__pycache__", "ledger.jsonl", "*.before-*"))
    results, real_log, real_argv = [], c.log, sys.argv
    c.log = lambda m: None
    try:
        for i, b in enumerate(blocks):
            reg = load_json(os.path.join(t, "state_registry.json"))
            host = next(h for h in reg["hosts"].values() if h["name"] == b["target"])
            bp, sp = os.path.join(t, f"dry_{i}.json"), os.path.join(t, f"dry_{i}_reply.json")
            json.dump(b, open(bp, "w"))
            try:
                replies = canonical_replies(c, b, os.path.join(t, "engine_test"), host)
            except (ValueError, TypeError, KeyError):
                replies = [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "x"})]     # the block is malformed: GI-1 refuses it with its own message before any answer is read
            json.dump(replies, open(sp, "w"))
            sys.argv = ["cascade_clock.py", bp, "--ws", t, "--script", sp, "--godot", a.godot, "--mule-dir", a.mule_dir]
            try:
                c.main()
            except SystemExit as e:
                if isinstance(e.code, str):
                    results.append({"status": "STOPPED", "verify": None, "how": None, "note": e.code}); break
            row = [json.loads(l) for l in open(os.path.join(t, "ledger.jsonl"), encoding="utf-8")][-1]
            results.append(row_result(row))
            if row["status"] not in OK_STATUS:
                break
    finally:
        c.log, sys.argv = real_log, real_argv
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def verdict_of(blocks, results):
    if len(results) < len(blocks) or any(r["status"] not in OK_STATUS for r in results):
        return "REFUSED"
    if all(r["status"] in NOOP_STATUS for r in results):
        return "NOOP"
    return "READY"


def print_dry(ctx, plan, blocks, results, seconds, notes=()):
    print(f"dry run of: {plan['intent']}")
    print(f"  (on a temporary copy of the workspace; no mule, nothing real was touched; {seconds:.1f}s)")
    for assume in plan.get("assumptions", []):
        print(f"  assumes: {assume}")
    for note in notes:
        print(f"  NOTE: {note}")
    print(f"  {'#':<3}{'op':<16}{'what':<58}{'status':<24}verify")
    for i, b in enumerate(blocks):
        r = results[i] if i < len(results) else None
        if r is None:
            print(f"  {i + 1:<3}{b['op']:<16}{describe(ctx, b)[:56]:<58}{'(not reached)':<24}")
            continue
        print(f"  {i + 1:<3}{b['op']:<16}{describe(ctx, b)[:56]:<58}{r['status']:<24}{(r['verify'] or '-') + ('  (' + r['how'] + ')' if r['how'] else '')}")
        if len(describe(ctx, b)) > 56:
            print(f"      full: {describe(ctx, b)}")
        if b.get("mode") == "replace":
            host = ctx.host_named(b["target"])
            old = handler_effect(ctx.clock, host, open(ctx.clock.script_path(ctx.project, host), encoding="utf-8").read(), b["fn"])
            print(f"      replaces: {old}" + ("   <- HAND-WRITTEN, IT WILL BE DISCARDED" if old.startswith("hand-written") else ""))
        if r["note"]:
            print(f"      {r['note']}")
    v = verdict_of(blocks, results)
    print(f"verdict: {v}" + ("   (every block is already in place; nothing to do)" if v == "NOOP" else ""))
    return v


# ------------------------------------------------------------------ catalog
def script_defaults(project, host):
    out = {}
    fp = os.path.join(project, host["script"][len("res://"):]) if host.get("script") else None
    if fp and os.path.exists(fp):
        for l in open(fp, encoding="utf-8").read().splitlines():
            m = re.match(r"^@export\s+var\s+(\w+)\s*(?::\s*(\w+))?\s*(?:=\s*(.+))?$", l)
            if m and m.group(3):
                out[m.group(1)] = m.group(3).strip()
    return out


def scene_values(ctx, host):
    out, fp = {}, os.path.join(ctx.project, host["owner_scene"][len("res://"):])
    if not os.path.exists(fp):
        return out
    lines = open(fp, encoding="utf-8").read().splitlines()
    rel = ctx.clock.node_rel(host)
    try:
        start, end = ctx.clock.find_node_block(lines, rel.split("/")[-1], "." if "/" not in rel else rel.rsplit("/", 1)[0])
    except Exception:
        return out
    for l in lines[start + 1:end]:
        m = re.match(r"^(\w+)\s*=\s*(.+)$", l)
        if m and m.group(1) in host["props"]:
            out[m.group(1)] = m.group(2).strip()
    return out


def handler_signature(c, raw, fn):
    """(typed argument strings, has a guard line) of a handler already in a script."""
    have = c.existing_function(raw, fn)
    m = re.match(r"^func\s+\w+\s*\((.*)\)\s*->\s*void\s*:$", have[0]) if have else None
    hargs = [x.strip() for x in m.group(1).split(",")] if m and m.group(1).strip() else []
    guard = bool(hargs) and hasattr(c, "handler_guard") and c.handler_guard(raw, fn) is not None
    return hargs, guard


def statement_text(c, st):
    if st["kind"] == "pass":
        return "does nothing"
    if st["kind"] == "print":
        return f'prints "{st["message"]}"'
    if st["kind"] == "assign":
        return f"sets {st['property']} = {c.scene_literal(st['type'], st['value'])}"
    if st["kind"] == "limit":
        return f"limits {st['property']}: {c.statement_line(st)}"
    if st["kind"] == "free":
        return f"frees {'itself' if st['target'] == 'self' else st['target']}"
    return f"changes {st['property']}: {c.statement_line(st)}"


def handler_effect(c, host, raw, name):
    have = c.existing_function(raw, name)
    if not have:
        return "unknown"
    if hasattr(c, "parse_statement_line"):
        body = [l for l in have[1:] if l and not l.startswith("#")]
        only = bool(body) and hasattr(c, "GUARD_RX") and bool(c.GUARD_RX.match(body[0]))
        if only: body = body[1:]
        sts = [c.parse_statement_line(l, host) for l in body]
        if sts and all(sts):
            return ("only when the argument is itself: " if only else "") + "; ".join(statement_text(c, st) for st in sts)
        return f"hand-written ({len(body)} line(s))"
    if len(have) == 2:
        b = have[1]
        if b == "pass":
            return "does nothing"
        m = re.match(r'^print\((["\'])(.*)\1\)$', b)
        if m:
            return f'prints "{m.group(2)}"'
        m = re.match(r"^(?:self\.)?([A-Za-z_]\w*)\s*(\+=|-=|\*=)\s*(.+)$", b) or re.match(r"^(?:self\.)?([A-Za-z_]\w*)\s*=\s*(?:self\.)?\1\s*([+\-*])\s*(.+)$", b)
        if m and m.group(1) in host["props"] and host["props"][m.group(1)] in RELATIVE_TYPES:
            return f"changes {m.group(1)}: {m.group(1)} {m.group(2) if m.group(2).endswith('=') else m.group(2) + '='} {m.group(3).strip()}"
        m = re.match(r"^(?:self\.)?([A-Za-z_]\w*)\s*=\s*(.+)$", b)
        if m and m.group(1) in host["props"] and c.parse_literal(host["props"][m.group(1)], m.group(2)) is not None:
            return f"sets {m.group(1)} = {m.group(2).strip()}"
    return f"hand-written ({len(have) - 1} line(s))"


def cmd_catalog(ctx):
    c, v, L = ctx.clock, ctx.vocab, []
    L.append(f"CASCADE CATALOG   workspace {ctx.ws}   (paste this into the harness session)")
    for host in ctx.hosts():
        L.append("")
        L.append(f"NODE {host['name']}   {host['node']}   script {host.get('script') or '(none)'}   scene {host['owner_scene']}   address {host['addr']}")
        defaults, scene = script_defaults(ctx.project, host), scene_values(ctx, host)
        L.append("  properties you may set (type; script default; value stored in the scene):")
        for p, t in sorted(host["props"].items()):
            L.append(f"    {p}: {t};  default {defaults.get(p, '-')};  scene {scene.get(p, '(default)')}")
        sa = host.get("signal_args", {})
        usable = sorted(s for s in host.get("signals", []) if not sa.get(s))
        groups = {"enter": [], "leave": [], "never": [], "other": []}
        for s in usable:
            groups[v["signals"].get(s, {}).get("group", "other")].append(s)
        L.append("  signals you may use (they carry no arguments):")
        titles = {"enter": "fires by itself when the node enters the tree", "leave": "fires by itself when the node leaves the tree",
                  "never": "never fires by itself (the behavior check emits it)", "other": "not in the vocabulary yet (the check emits it if it never fires by itself)"}
        for g in ("enter", "leave", "never", "other"):
            if groups[g]:
                L.append(f"    {titles[g]}:")
                for s in groups[g]:
                    ph = v["signals"].get(s, {}).get("phrases", [])
                    quoted = '", "'.join(ph[:4])
                    rep_note = "   [can fire many times]" if v["signals"].get(s, {}).get("repeats", True) else ""
                    L.append(f"      {s}" + (f'   ("{quoted}")' if ph else "") + rep_note)
        withargs = sorted(s for s in host.get("signals", []) if sa.get(s))
        good = [s for s in withargs if hasattr(c, "signal_arg_types") and c.signal_args_supported(c.signal_arg_types(host, s))]
        if good:
            L.append("  signals that carry arguments (the handler receives them, the statements do not read them; \"only\": \"self\" limits a Node argument to the receiver):")
            for s2 in good:
                ph = v["signals"].get(s2, {}).get("phrases", [])
                quoted = '", "'.join(ph[:4])
                L.append(f"      {s2}({', '.join(c.signal_arg_types(host, s2))})" + (f'   ("{quoted}")' if ph else "") + ("   [can fire many times]" if v["signals"].get(s2, {}).get("repeats", True) else ""))
        out = [s2 for s2 in withargs if s2 not in good]
        if out:
            L.append("  signals out of reach (arguments plans cannot carry): " + ", ".join(f"{s2}({', '.join(c.signal_arg_types(host, s2) or sa[s2]) if hasattr(c, 'signal_arg_types') else ', '.join(sa[s2])})" for s2 in out))
        raw = open(os.path.join(ctx.project, host["script"][len("res://"):]), encoding="utf-8").read() if host.get("script") else ""
        conns = {}
        sp = os.path.join(ctx.project, host["owner_scene"][len("res://"):])
        rel = c.node_rel(host)
        for l in open(sp, encoding="utf-8").read().splitlines() if os.path.exists(sp) else []:
            if l.startswith("[connection "):
                at = dict(re.findall(r'(\w+)="([^"]*)"', l))
                if at.get("to") == rel:
                    conns.setdefault(at.get("method"), []).append(at.get("signal") if at.get("from") == rel else f"{at.get('from')}.{at.get('signal')}")
        if host.get("script"):
            L.append("  handlers already in the script (change_handler replaces the whole body of a [replaceable] one):")
        else:
            L.append("  no script: it can emit signals for another node's handlers, but it has no handlers of its own")
        for m in host.get("methods", []):
            ha = handler_signature(c, raw, m)[0] if re.match(r"^_on_[a-z0-9_]+$", m) else []
            flag = "[replaceable]" if re.match(r"^_on_[a-z0-9_]+$", m) and (host["method_args"].get(m, 0) == 0 or (ha and len(ha) == host["method_args"].get(m, 0) and all(re.match(r"^[a-z_][a-z0-9_]*: \w+$", x) for x in ha))) else "[not replaceable: engine callback or untyped arguments]"
            L.append(f"    {m}({host['method_args'].get(m, 0)} args)   {handler_effect(c, host, raw, m)}   connected to: {', '.join(conns[m]) if m in conns else '-'}   {flag}")
    if hasattr(c, "scene_geometry"):
        c.set_godot(ctx.a.godot)
        L.append("")
        L.append("NODES YOU CAN ADD ({\"do\": \"add_node\", \"class\": ..., \"name\": ..., \"at\": [x, y], \"size\": [w, h], \"parent\": \".\" or \"%Node\"}):")
        L.append("  classes: Area2D (a trigger or pickup; \"size\" is the width and height of its rectangle, 8 to 1024), Marker2D and Node2D (a position), Timer (no position, no size)")
        L.append("  rules: a name of 2 to 24 letters or digits starting with a capital letter that no node in the scene uses; \"at\" within 4096 of the parent; at most 8 per plan and 24 registered nodes per scene; a new Area2D may not overlap any collision shape that is in the scene at load")
        for sc in sorted({h["owner_scene"] for h in ctx.hosts()}):
            inscene = [h["name"] for h in ctx.hosts() if h["owner_scene"] == sc]
            L.append(f"  {sc}: {len(inscene)} of {getattr(c, 'MAX_NODES_PER_SCENE', 24)} registered nodes ({', '.join(inscene)})")
            geo = c.scene_geometry(ctx.project, sc)
            if geo:
                occ = [f"{nm} [{d['rect'][0]:g}, {d['rect'][1]:g} to {d['rect'][2]:g}, {d['rect'][3]:g}]" for nm, d in sorted(geo["nodes"].items()) if d.get("rect")]
                L.append("    collision shapes already in the scene (a new Area2D must stay clear of these): " + (", ".join(occ) if occ else "none"))
    pairs = [(s, t) for t in ctx.hosts() if t.get("script") for s in ctx.hosts() if s["name"] != t["name"] and s["owner_scene"] == t["owner_scene"]]
    if pairs:
        L.append("")
        L.append("CROSS-NODE WIRING (a signal of one node runs a handler on another; {\"do\": \"when\", \"from\": <emitter>, ...} in a plan whose node is the receiver):")
        for s, t in pairs:
            sa = s.get("signal_args", {})
            known = [x for x in sorted(s.get("signals", [])) if not sa.get(x) and x in v["signals"]]
            L.append(f"  from {s['name']} ({s['node']}) into {t['name']}: handler is named _on_{c.snake(s['name'])}_<signal>")
            for x in known:
                ph = v["signals"][x].get("phrases", [])
                quoted = '", "'.join(ph[:4])
                L.append(f"      {x}" + (f'   ("{quoted}")' if ph else "") + ("   [can fire many times]" if v["signals"][x].get("repeats", True) else ""))
            if hasattr(c, "signal_arg_types"):
                for x in sorted(s.get("signals", [])):
                    ty = c.signal_arg_types(s, x)
                    if sa.get(x) and ty is not None and c.signal_args_supported(ty) and x in v["signals"]:
                        ph = v["signals"][x].get("phrases", [])
                        quoted = '", "'.join(ph[:4])
                        L.append(f"      {x}({', '.join(ty)})" + (f'   ("{quoted}")' if ph else "") + "   [carries an argument; can fire many times]")
    L.append("")
    L.append("STEPS (a plan is {\"intent\": ..., \"node\": ..., \"assumptions\": [...], \"steps\": [...]}):")
    L += ["  " + s for s in v.get("steps", [])]
    L.append("VALUES: " + v.get("values", ""))
    L.append("SUPPORTED: " + " | ".join(v.get("supported_intents", [])))
    L.append("REFUSED (say so in plain words, offer the nearest supported plan):")
    L += ["  - " + s for s in v.get("refusals", [])]
    print("\n".join(L))
    return 0


# ------------------------------------------------------------------ run, undo, status
def plans_dir(ctx):
    return os.path.join(ctx.ws, "plans")


def next_plan_id(ctx):
    os.makedirs(plans_dir(ctx), exist_ok=True)
    nums = [int(d[1:]) for d in os.listdir(plans_dir(ctx)) if re.match(r"^p\d{4}$", d)]
    return f"p{(max(nums) + 1) if nums else 1:04d}"


def touched_files(ctx, blocks):
    byref = {h["addr"]: h for h in ctx.hosts()}
    return sorted({byref[b["ref"]]["script"] if b["op"] == "add_method" else byref[b["ref"]]["owner_scene"] for b in blocks})


def res_path(ctx, f):
    return os.path.join(ctx.project, f[len("res://"):])


def cmd_dry_run(ctx, path):
    plan, host, blocks, _, notes = load_plan(ctx, path)
    t0 = time.time()
    results = dry_run(ctx, blocks)
    v = print_dry(ctx, plan, blocks, results, time.time() - t0, notes)
    return 0 if v in ("READY", "NOOP") else 2


def cmd_run(ctx, path):
    a = ctx.a
    plan, host, blocks, nxt, notes = load_plan(ctx, path)
    t0 = time.time()
    results = dry_run(ctx, blocks)
    v = print_dry(ctx, plan, blocks, results, time.time() - t0, notes)
    if v == "NOOP":
        return 0
    if v != "READY":
        print("nothing was run.")
        return 2
    commit_ids(ctx.ws, nxt)
    pid = next_plan_id(ctx)
    pdir = os.path.join(plans_dir(ctx), pid)
    os.makedirs(pdir)
    paths = []
    for i, b in enumerate(blocks, 1):
        p = os.path.join(pdir, f"block_{i:02d}.json")
        json.dump(b, open(p, "w"))
        paths.append(p)
    json.dump({"plan_id": pid, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "plan": plan, "blocks": blocks}, open(os.path.join(pdir, "plan.json"), "w"), indent=2)
    files = touched_files(ctx, blocks)
    pre = {f: md5(res_path(ctx, f)) for f in files}
    print(f"\nrunning plan {pid} for real ({len(blocks)} block(s), one model load)\n")
    sys.argv = [sys.argv[0]] + paths + ["--ws", ctx.ws, "--godot", a.godot, "--mule-dir", a.mule_dir, "--model", a.model, "--run-id", pid] + \
        (["--no-verify"] if a.no_verify else []) + (["--keep-going"] if a.keep_going else []) + (["--no-record"] if a.no_record else [])
    try:
        ctx.clock.main()
        code = 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    ledger = os.path.join(ctx.ws, "ledger.jsonl")
    rows = [json.loads(l) for l in open(ledger, encoding="utf-8") if f'"run_id": "{pid}"' in l] if os.path.exists(ledger) else []
    brow = []
    for r in rows:
        ap = r.get("apply") or {}
        brow.append({"frame_id": r["frame_id"], "op": r["op"], "status": r["status"], "tries": r.get("tries"), "file": ap.get("file"), "backup": ap.get("backup")})
    statuses = [r["status"] for r in rows]
    state = "applied" if rows and len(rows) == len(blocks) and all(s in OK_STATUS for s in statuses) else ("partial" if "APPLIED" in statuses else "failed")
    res = {"plan_id": pid, "intent": plan["intent"], "state": state, "undone": False, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "files": {f: {"pre": pre[f], "post": md5(res_path(ctx, f))} for f in files}, "blocks": brow}
    json.dump(res, open(os.path.join(pdir, "result.json"), "w"), indent=2)
    print(f"\nplan {pid}: {state}   ({sum(1 for s in statuses if s == 'APPLIED')} applied, {sum(1 for s in statuses if s in NOOP_STATUS)} already there)")
    if any(s == "APPLIED" for s in statuses):
        print(f"take it back with:  python3 cascade_plan.py undo {pid}")
    return code


def load_result(ctx, pid):
    p = os.path.join(plans_dir(ctx), pid, "result.json")
    if not os.path.exists(p):
        sys.exit(f"STOPPED: no saved result for plan {pid} (see: python3 cascade_plan.py status)")
    return p, load_json(p)


def rebuild_registry(ctx):
    r = subprocess.run([sys.executable, os.path.join(ctx.ws, "registry_builder.py"), "--ws", ctx.ws, "--godot", ctx.a.godot], capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def cmd_undo(ctx, pid):
    rp, res = load_result(ctx, pid)
    if res.get("undone"):
        print(f"plan {pid} was already undone ({res.get('undone_at')}).")
        return 0
    changed = {f: d for f, d in res["files"].items() if md5(res_path(ctx, f)) != d["post"]}
    later = []
    for other in sorted(os.listdir(plans_dir(ctx))):
        if other > pid and os.path.exists(os.path.join(plans_dir(ctx), other, "result.json")):
            o = load_json(os.path.join(plans_dir(ctx), other, "result.json"))
            if not o.get("undone") and set(o["files"]) & set(changed):
                later.append(other)
    if changed and not ctx.a.force:
        print(f"REFUSED: {', '.join(changed)} changed after plan {pid}.")
        print(("undo the later plan(s) first, newest first: " + ", ".join(reversed(later))) if later else "something outside the plans changed it; look at it, or use --force to restore anyway (that discards the change).")
        return 2
    todo = [b for b in res["blocks"] if b["status"] == "APPLIED"]
    missing = [b["backup"] for b in todo if not b.get("backup") or not os.path.exists(b["backup"])]
    if missing:
        print("REFUSED: a backup is missing, so nothing was changed: " + ", ".join(map(str, missing)))
        return 2
    for b in reversed(todo):
        shutil.copy2(b["backup"], res_path(ctx, b["file"]))
        print(f"  restored {b['file']} from {os.path.basename(b['backup'])}")
    same = all(md5(res_path(ctx, f)) == d["pre"] for f, d in res["files"].items())
    print("the project is byte-for-byte what it was before the plan." if same else "restored, but the files differ from the pre-plan state (they changed in between).")
    print("registry rebuilt." if rebuild_registry(ctx) else "WARNING: the registry could not be rebuilt; run registry_builder.py.")
    res["undone"], res["undone_at"] = True, time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(res, open(rp, "w"), indent=2)
    if later:
        for other in later:        # their work was discarded along with this restore
            op = os.path.join(plans_dir(ctx), other, "result.json")
            o = load_json(op)
            o["undone"], o["undone_at"], o["discarded_by"] = True, res["undone_at"], pid
            json.dump(o, open(op, "w"), indent=2)
        print(f"plans discarded along with it (marked undone): {', '.join(later)}")
    return 0


def cmd_status(ctx):
    d = plans_dir(ctx)
    ids = sorted(x for x in os.listdir(d) if re.match(r"^p\d{4}$", x)) if os.path.exists(d) else []
    if not ids:
        print("no plans yet.")
        return 0
    print(f"{'plan':<7}{'state':<10}{'undone':<8}{'blocks':<8}intent")
    for pid in ids:
        rp = os.path.join(d, pid, "result.json")
        if not os.path.exists(rp):
            print(f"{pid:<7}{'(no result)':<10}")
            continue
        r = load_json(rp)
        print(f"{pid:<7}{r['state']:<10}{'yes' if r.get('undone') else 'no':<8}{len(r['blocks']):<8}{r['intent'][:70]}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Project Cascade harness layer")
    ap.add_argument("command", choices=["catalog", "expand", "dry-run", "run", "undo", "status"])
    ap.add_argument("target", nargs="?", help="a plan file (expand, dry-run, run) or a plan id (undo)")
    ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws"))
    ap.add_argument("--mule-dir", default=os.path.expanduser("~/mule_bench"))
    ap.add_argument("--model", default="gemma26b")
    ap.add_argument("--godot", default=os.environ.get("GODOT", "godot"))
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--keep-going", action="store_true")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--force", action="store_true", help="undo: restore even if the files changed after the plan")
    a = ap.parse_args()
    if a.command in ("expand", "dry-run", "run", "undo") and not a.target:
        sys.exit(f"STOPPED: {a.command} needs a {'plan id' if a.command == 'undo' else 'plan file'}")
    ctx = Ctx(a)
    if a.command == "catalog":
        sys.exit(cmd_catalog(ctx))
    if a.command == "status":
        sys.exit(cmd_status(ctx))
    if a.command == "undo":
        sys.exit(cmd_undo(ctx, a.target))
    if a.command == "expand":
        plan, host, blocks, _, notes = load_plan(ctx, a.target)
        print(json.dumps(blocks, indent=1))
        for note in notes:
            print("NOTE: " + note)
        sys.exit(0)
    sys.exit((cmd_dry_run if a.command == "dry-run" else cmd_run)(ctx, a.target))


if __name__ == "__main__":
    main()
PLANEOF

# ---- 2. write cascade_vocab.json
cat << 'VOCABEOF' > ~/cascade_ws/cascade_vocab.json
{
  "version": 1,
  "note": "Editable. The harness reads phrases from here; the registry decides which signals are usable. 'group' says how the behavior check sees the signal: enter = fires by itself when the node enters the tree, leave = fires by itself when it leaves, never = it never fires by itself so the check emits it. 'repeats' is true for signals that can fire many times in one life (a relative change on them compounds); unknown signals count as repeating.",
  "signals": {
    "tree_entered": {
      "group": "enter",
      "fires": "by itself when the node enters the tree",
      "phrases": [
        "enters the scene",
        "enters the tree",
        "is added to the scene",
        "spawns",
        "appears in the scene"
      ],
      "repeats": false
    },
    "ready": {
      "group": "enter",
      "fires": "by itself, once, when the node is ready (right after it enters the tree)",
      "phrases": [
        "is ready",
        "starts",
        "begins",
        "starts up"
      ],
      "repeats": false
    },
    "visibility_changed": {
      "group": "enter",
      "fires": "by itself when it enters the tree, and whenever its visibility changes",
      "phrases": [
        "becomes visible or hidden",
        "visibility changes",
        "is shown or hidden"
      ],
      "repeats": true
    },
    "draw": {
      "group": "enter",
      "fires": "by itself each time it is drawn (it repeats on every redraw)",
      "phrases": [
        "is drawn",
        "gets redrawn"
      ],
      "repeats": true
    },
    "tree_exiting": {
      "group": "leave",
      "fires": "by itself when the node is about to leave the tree",
      "phrases": [
        "leaves the scene",
        "leaves the tree",
        "is removed",
        "exits the scene",
        "despawns",
        "quits",
        "is about to leave"
      ],
      "repeats": false
    },
    "tree_exited": {
      "group": "leave",
      "fires": "by itself right after the node has left the tree",
      "phrases": [
        "has left the scene",
        "is gone",
        "has been removed"
      ],
      "repeats": false
    },
    "hidden": {
      "group": "never",
      "fires": "never by itself in this scene (the behavior check emits it)",
      "phrases": [
        "is hidden",
        "hides",
        "becomes hidden"
      ],
      "repeats": true
    },
    "renamed": {
      "group": "never",
      "fires": "never by itself (the behavior check emits it)",
      "phrases": [
        "is renamed",
        "gets a new name"
      ],
      "repeats": true
    },
    "script_changed": {
      "group": "never",
      "fires": "never by itself (the behavior check emits it)",
      "phrases": [
        "gets a different script"
      ],
      "repeats": true
    },
    "child_order_changed": {
      "group": "never",
      "fires": "never by itself (the behavior check emits it)",
      "phrases": [
        "its children are reordered"
      ],
      "repeats": true
    },
    "item_rect_changed": {
      "group": "never",
      "fires": "never by itself (the behavior check emits it)",
      "phrases": [
        "its rectangle changes"
      ],
      "repeats": true
    },
    "property_list_changed": {
      "group": "never",
      "fires": "never by itself (the behavior check emits it)",
      "phrases": [
        "its property list changes"
      ],
      "repeats": true
    },
    "mouse_entered": {
      "group": "never",
      "fires": "never by itself in a headless run (the behavior check emits it)",
      "phrases": [
        "the mouse enters it"
      ],
      "repeats": true
    },
    "mouse_exited": {
      "group": "never",
      "fires": "never by itself in a headless run (the behavior check emits it)",
      "phrases": [
        "the mouse leaves it"
      ],
      "repeats": true
    },
    "timeout": {
      "group": "never",
      "fires": "when a Timer runs out; it fires by itself only if the Timer starts (autostart on, which the 'after' step sets); otherwise the behavior check emits it",
      "phrases": [
        "the timer runs out",
        "the timer fires",
        "the timer goes off",
        "the timer finishes",
        "the timer times out"
      ],
      "repeats": true
    },
    "body_entered": {
      "group": "physics",
      "fires": "when a physics body (a CharacterBody2D, RigidBody2D, ...) overlaps the Area2D; it carries the body that entered",
      "phrases": [
        "touches",
        "walks into",
        "enters the area",
        "steps on"
      ],
      "repeats": true
    },
    "body_exited": {
      "group": "physics",
      "fires": "when a physics body stops overlapping the Area2D",
      "phrases": [
        "leaves the area",
        "walks out of",
        "steps off"
      ],
      "repeats": true
    },
    "area_entered": {
      "group": "physics",
      "fires": "when another Area2D overlaps this Area2D",
      "phrases": [
        "an area enters",
        "overlaps"
      ],
      "repeats": true
    },
    "area_exited": {
      "group": "physics",
      "fires": "when another Area2D stops overlapping this Area2D",
      "phrases": [
        "an area leaves"
      ],
      "repeats": true
    }
  },
  "steps": [
    "when:         {\"do\": \"when\", \"signal\": \"tree_entered\", \"then\": {\"set\": {\"property\": \"speed\", \"value\": 450.0}}}   (a handler plus its connection)",
    "              then is ONE form  {\"set\": {\"property\": P, \"value\": V}}  {\"increase\"|\"decrease\"|\"scale\": {\"property\": P, \"by\": V}}  {\"print\": \"message\"}  {\"nothing\": true}",
    "              or a LIST of 1 to 6 of those forms, run in order: \"then\": [{\"set\": {\"property\": \"speed\", \"value\": 450.0}}, {\"print\": \"ready\"}]   (nothing alone in a list; = at most once per property; the net effect on each property must not be zero)",
    "              or  {\"increase\": {\"property\": \"speed\", \"by\": 50.0}}  {\"decrease\": {\"property\": \"speed\", \"by\": 10.0}}  {\"scale\": {\"property\": \"velocity\", \"by\": 2.0}}   (+=, -=, *= on float, int, Vector2, Vector3 properties)",
    "set_property: {\"do\": \"set_property\", \"property\": \"velocity\", \"value\": [0.0, -400.0]}   (the starting value stored in the scene)",
    "add_function: {\"do\": \"add_function\", \"name\": \"reset_speed\", \"then\": {\"set\": {\"property\": \"speed\", \"value\": 300.0}}}   (a function, not connected)",
    "connect:      {\"do\": \"connect\", \"signal\": \"hidden\", \"handler\": \"_on_player_hidden\"}   (to a handler that already exists)",
    "change_handler: {\"do\": \"change_handler\", \"handler\": \"_on_player_tree_entered\", \"then\": [{\"set\": {\"property\": \"speed\", \"value\": 450.0}}, {\"print\": \"ready\"}]}   (replaces the WHOLE body of a [replaceable] handler; its connections stay; the dry run shows what is discarded)",
    "when ... from: {\"do\": \"when\", \"from\": \"%Timer\", \"signal\": \"timeout\", \"then\": {\"set\": {\"property\": \"speed\", \"value\": 600.0}}}   (a signal of ANOTHER node runs a new handler on the plan's node; the handler is named _on_<emitter>_<signal> and lives on the plan's node, so \"node\" is the receiver)",
    "connect ... from: {\"do\": \"connect\", \"from\": \"%Timer\", \"signal\": \"timeout\", \"handler\": \"_on_timer_timeout\"}   (connect another node's signal to a handler that already exists on the plan's node; the handler must be named _on_<emitter>_<signal>)",
    "after:        {\"do\": \"after\", \"seconds\": 2.0, \"then\": [{\"set\": {\"property\": \"speed\", \"value\": 600.0}}, {\"print\": \"go\"}]}   (a one-shot %Timer counts the seconds from the moment the scene loads, then the handler _on_timer_timeout on the plan's node runs once; \"timer\" names the Timer if there are several; if that handler already exists its whole body is replaced)",
    "node:         any step may carry \"node\": \"%Timer\" to act on another node than the plan's: {\"do\": \"set_property\", \"node\": \"%Timer\", \"property\": \"wait_time\", \"value\": 3.0}   (a Timer's settable properties are wait_time (0.05 to 3600), one_shot, autostart)",
    "every:        {\"do\": \"every\", \"seconds\": 2.0, \"then\": [{\"increase\": {\"property\": \"speed\", \"by\": 25.0}}, {\"limit\": {\"property\": \"speed\", \"max\": 500.0}}, {\"print\": \"tick\"}]}   (a repeating %Timer fires every N seconds from the moment the scene loads, for as long as the scene lives; N from 0.5 to 3600; the handler _on_timer_timeout on the plan's node runs each time, and if it already exists its whole body is replaced)",
    "limit:        {\"limit\": {\"property\": \"speed\", \"max\": 500.0}}  {\"limit\": {\"property\": \"speed\", \"min\": 0.0}}  {\"limit\": {\"property\": \"velocity\", \"max_length\": 800.0}}   (a list item; it must come AFTER an increase, decrease or scale on the same property; every increase, decrease and scale-over-1 inside an \"every\" must be followed by a limit on that property: max for growth, min for shrinking, max_length for a vector; the user names the number, never invent one)",
    "when ... only: {\"do\": \"when\", \"from\": \"%Trigger\", \"signal\": \"body_entered\", \"only\": \"self\", \"then\": [{\"increase\": {\"property\": \"speed\", \"by\": 10.0}}, {\"limit\": {\"property\": \"speed\", \"max\": 400.0}}, {\"free\": \"%Trigger\"}]}   (a signal that carries an argument: the handler is written with it, for example _on_trigger_body_entered(_body: Node2D), and the statements ignore it; \"only\": \"self\" makes the handler run only when that argument is the plan's own node, so a stray body cannot trigger it; the plan's node is the receiver)",
    "free:         {\"free\": \"%Trigger\"}  {\"free\": \"self\"}   (a list item: queue_free() of a registry node in the same scene, or of the node that owns the handler; it must be the LAST item and at most one; a pickup is: change a value, then free the item)",
    "add_node:     {\"do\": \"add_node\", \"class\": \"Area2D\", \"name\": \"Coin\", \"at\": [420, 0], \"size\": [48, 48]}   (puts a new node in the scene; classes Area2D, Marker2D, Node2D, Timer; \"parent\" is \".\" or a registered %Node; later steps in the SAME plan can name it, for example {\"do\": \"when\", \"from\": \"%Coin\", \"signal\": \"body_entered\", ...}; the node is added to the scene of the plan's node)"
  ],
  "values": "float 450.0 (always with a decimal point) | int 7 | bool true/false | String plain text up to 40 characters (letters, digits, space and _ . , : ! ? -) | Vector2 [x, y] | Vector3 [x, y, z] | Color [r, g, b, a] | 'by': a float for float properties, an int for int properties, the same vector for increase/decrease on Vector2/Vector3, a float for scale on a vector; never 0 for increase/decrease or 1 for scale",
  "supported_intents": [
    "when <event>, do one thing or several things in order (set, increase / decrease, scale, print, nothing)",
    "change what an existing handler does: replace the whole body of a [replaceable] _on_ handler with one or several statements",
    "set <property> to <literal> in the scene",
    "add a function that does one or several things",
    "connect <event> to an existing handler",
    "when <event of another node> (from), do one thing or several things on the plan's node; the CROSS-NODE section of the catalog lists the pairs",
    "after <N seconds>, do one thing or several things on the plan's node (a Timer starts when the scene loads and fires once)",
    "set a property of another node (a Timer's wait_time, one_shot or autostart) with set_property and \"node\"",
    "every <N seconds>, do one thing or several things on the plan's node, with a limit on every relative change (a repeating Timer, N from 0.5)",
    "when <a body or area enters / leaves a trigger area> (from the Area2D, with \"only\": \"self\" if only the plan's node should count), do things on the plan's node, and optionally free a node (pickups, doors, triggers)",
    "put a new trigger, pickup, marker, empty node or timer in the scene at a position (add_node), then act on it in the same plan"
  ],
  "refusals": [
    "values that come from other properties or expressions: set speed to health plus 10 (a number is fixed when the plan is written)",
    "division, and relative changes on bool, String or Color properties",
    "more than 6 statements in one handler, or if / for / while / other control flow",
    "editing one line of a handler: a change replaces the whole body (say all of it)",
    "replacing engine callbacks (_physics_process, _ready ...), functions that are not _on_ handlers, handlers with untyped arguments, or deleting a handler",
    "a property, signal or node that is not in the catalog",
    "starting or stopping a Timer in the middle of the game, and a wait over 3600 seconds or under 0.05",
    "a statement inside a handler changes only the node that owns the handler; to change another node's property use a set_property step with \"node\"",
    "an 'every' handler whose increase, decrease or scale-over-1 has no limit after it (it would grow or shrink without end), a zero or negative scale factor in an 'every', and 'every' under 0.5 seconds",
    "stopping or pausing a repeating Timer in the middle of the game, and a handler that takes its limit from another property",
    "signals with RID, InputEvent or other non-Node object arguments (body_shape_entered, input_event), more than 3 arguments, and statements that read the argument (what entered, its name, its properties)",
    "a handler on a physics enter/exit signal whose increase, decrease or scale-over-1 has no limit and does not free the emitter (it can fire again every time something enters)",
    "freeing a node in another scene, freeing before the last statement, and more than one free",
    "removing, renaming or moving existing nodes, adding a class that is not Area2D, Marker2D, Node2D or Timer (sprites, bodies, cameras, scenes need assets), a node with a script, shapes other than a rectangle, and spawning or instancing scenes",
    "a new Area2D that overlaps a collision shape already in the scene (it would fire at load), a name that is taken, a position beyond 4096 or a size outside 8 to 1024, more than 8 new nodes in a plan or 24 registered nodes in a scene"
  ]
}
VOCABEOF

# ---- 2. write harness_protocol.json
cat << 'PROTOEOF' > ~/cascade_ws/harness_protocol.json
{
  "role": "harness",
  "talk_chain": "user -> harness -> mule; the mule never sees user language",
  "mule": "local Gemma on the ammo clip, reached only through cascade_plan.py run",
  "session_start": [
    "ask the user to paste:  python3 ~/cascade_ws/cascade_plan.py catalog",
    "work only from that catalog: its nodes, properties, signals, handlers and limits are the whole world"
  ],
  "for_every_intent": [
    "1. say the goal back in one sentence and wait for a yes before any work",
    "2. if the intent is outside the supported list, refuse in plain words and offer the nearest supported plan (never guess, never approximate silently)",
    "3. write one compact plan (intent, node, assumptions, steps) using only names from the catalog; state every interpretation under assumptions",
    "4. give the user the dry-run command and wait for the output; if it says REFUSED, explain the guard's message in plain words and fix the plan or refuse",
    "5. when the dry run says READY, show the plan as a short table and wait for a go",
    "6. give the run command (venv python), then read the result back",
    "7. offer undo with the plan id"
  ],
  "rules": [
    "do only what the user names; one plan at a time; stop when done",
    "the user's call is final; say a risk once, then follow",
    "never invent a signal, property, node or handler: if it is not in the catalog it does not exist",
    "when two signals fit the words (ready vs tree_entered), pick one, say so under assumptions, and let the user override",
    "floats always carry a decimal point; vectors are [x, y]; strings are plain text up to 40 characters",
    "if a dry run or run output is missing from the user's message, say so and ask for it; do not assume it passed",
    "after any run, the catalog is stale: ask for a fresh one before the next plan",
    "a relative change (increase, decrease, scale) on a signal the catalog marks [can fire many times] compounds each time; say so in plain words before the user says go, and offer a once-per-life signal or an absolute value as the alternative",
    "'double', 'halve', 'twice', '1.5 times' are scale steps; 'add', 'plus', 'more', 'increase by' are increase; 'minus', 'less', 'reduce by' are decrease; never turn them into an absolute number you computed yourself",
    "to change an existing handler use change_handler, and only on a handler the catalog marks [replaceable]; the goal sentence names the handler, what it does now (as the catalog lists it) and what it will do instead, then waits for a yes; the new body is the WHOLE body, so anything the old one did that should stay must be listed again",
    "if the catalog shows the old body as hand-written, say plainly that the replacement discards code a person wrote, and ask once before going on",
    "'also', 'and then', 'as well' mean a longer list in the same handler, not a second handler; a list holds at most 6 items and runs in order",
    "a signal of another node (catalog section CROSS-NODE) uses 'from': the plan's node is the RECEIVER, 'from' is the emitter, and the new handler lives on the receiver; the goal sentence names both nodes and says which one changes",
    "a signal the catalog lists as never firing by itself (a Timer's timeout) fires in the game only if the Timer starts; for 'after N seconds' use the after step, which sets wait_time, one_shot and autostart for you",
    "any step may carry 'node' to act on another node than the plan's node; the plan's node is the receiver of handlers",
    "'after', 'in N seconds', 'N seconds after the game starts' mean the after step (once); 'every N seconds', 'keep', 'each N seconds', 'repeatedly' mean the every step (N at least 0.5); the goal sentence says the Timer starts when the scene loads, whether it fires once or repeats for as long as the scene lives, and that the dry run and the run each wait a few seconds for the live check",
    "in an every step each increase, decrease or scale above 1 must be followed by a limit on the same property (max when it grows, min when it shrinks, max_length for a vector); ask the user for the cap if they did not give one, never invent a number; scale below 1 needs no limit; a limit alone is refused",
    "a trigger, pickup or touch-sensor is the Area2D's body_entered: 'node' is the receiver (the player), 'from' is the Area2D, the handler ignores the entering body; use 'only': 'self' when only the plan's own node should count; say in the goal sentence what is freed and that the trigger fires once",
    "a pickup ends with {'free': '%Item'}: change a value on the receiver, then free the item (last statement); a repeating trigger (no free) needs a limit after every increase, decrease or scale above 1, like every",
    "a node that does not exist yet is added with add_node before any step that names it; give it the position and size the user said, never invent coordinates (ask, or say which clear spot the catalog's occupied-shapes line leaves); a pickup is add_node (Area2D) then a when ... from it with 'only': 'self' and a free of itself"
  ],
  "commands": {
    "catalog": "python3 ~/cascade_ws/cascade_plan.py catalog",
    "dry_run": "python3 ~/cascade_ws/cascade_plan.py dry-run ~/cascade_ws/plan.json",
    "run": "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True ~/mule_bench/.venv/bin/python ~/cascade_ws/cascade_plan.py run ~/cascade_ws/plan.json",
    "undo": "python3 ~/cascade_ws/cascade_plan.py undo <plan id>",
    "status": "python3 ~/cascade_ws/cascade_plan.py status"
  },
  "worked_examples": [
    {
      "user": "when the player enters the scene, set its speed to 450",
      "goal_sentence": "When %Player enters the scene, its speed becomes 450.0.",
      "plan": {
        "intent": "when the player enters the scene, set its speed to 450",
        "node": "%Player",
        "assumptions": [
          "'enters the scene' = the tree_entered signal",
          "speed is a float property"
        ],
        "steps": [
          {
            "do": "when",
            "signal": "tree_entered",
            "then": {
              "set": {
                "property": "speed",
                "value": 450.0
              }
            }
          }
        ]
      }
    },
    {
      "user": "make the player say bye when it leaves",
      "goal_sentence": "When %Player is about to leave the scene, it prints 'bye'.",
      "plan": {
        "intent": "make the player say bye when it leaves",
        "node": "%Player",
        "assumptions": [
          "'leaves' = the tree_exiting signal (not tree_exited)",
          "'say' = print to the console"
        ],
        "steps": [
          {
            "do": "when",
            "signal": "tree_exiting",
            "then": {
              "print": "bye"
            }
          }
        ]
      }
    },
    {
      "user": "double the speed when the player is hidden",
      "goal_sentence": "Each time %Player is hidden, its speed is multiplied by 2.0.",
      "plan": {
        "intent": "double the speed when the player is hidden",
        "node": "%Player",
        "assumptions": [
          "'doubles' = scale by 2.0",
          "'is hidden' = the hidden signal"
        ],
        "steps": [
          {
            "do": "when",
            "signal": "hidden",
            "then": {
              "scale": {
                "property": "speed",
                "by": 2.0
              }
            }
          }
        ]
      }
    },
    {
      "user": "when it enters the scene, set the speed to the health plus 10",
      "refusal": "That takes its value from another property, which plans cannot express: a plan's number is fixed when it is written. I can set the speed to a fixed number on tree_entered, or increase it by a fixed amount. Which number?"
    },
    {
      "user": "when its item rect changes, increase the speed by 50",
      "goal_sentence": "Each time %Player's rectangle changes, its speed goes up by 50.0.",
      "plan": {
        "intent": "when the player's item rect changes, increase its speed by 50",
        "node": "%Player",
        "assumptions": [
          "'item rect changes' = the item_rect_changed signal",
          "'by 50' = += 50.0 on the float property speed"
        ],
        "steps": [
          {
            "do": "when",
            "signal": "item_rect_changed",
            "then": {
              "increase": {
                "property": "speed",
                "by": 50.0
              }
            }
          }
        ]
      },
      "say_before_go": "item_rect_changed can fire many times, so every change adds another 50. If you want it only once, I can use one of the once-per-life signals (ready, tree_entered, tree_exiting, tree_exited) that the catalog shows without a handler; if all of them are taken, say so."
    },
    {
      "user": "when it enters the scene, also print 'ready'",
      "goal_sentence": "Replace the body of _on_player_tree_entered (now: sets speed = 450.0) so that on entering the scene it sets speed = 450.0 and prints 'ready'.",
      "plan": {
        "intent": "when the player enters the scene, also print 'ready'",
        "node": "%Player",
        "assumptions": [
          "'also' = keep the speed assignment and add a print",
          "tree_entered already has a handler, so this replaces its whole body"
        ],
        "steps": [
          {
            "do": "change_handler",
            "handler": "_on_player_tree_entered",
            "then": [
              {
                "set": {
                  "property": "speed",
                  "value": 450.0
                }
              },
              {
                "print": "ready"
              }
            ]
          }
        ]
      }
    },
    {
      "user": "when the timer runs out, set the player's speed to 600 and print go",
      "goal_sentence": "When %Timer runs out (timeout), %Player sets its speed to 600.0 and prints 'go'. The handler _on_timer_timeout is added to %Player and wired to %Timer; nothing starts the Timer yet, so the check emits the signal.",
      "plan": {
        "intent": "when the timer runs out, set the player's speed to 600 and print go",
        "node": "%Player",
        "assumptions": [
          "'the timer' = %Timer, the Timer node in the scene",
          "'runs out' = its timeout signal",
          "the speed change belongs to %Player, which owns the handler"
        ],
        "steps": [
          {
            "do": "when",
            "from": "%Timer",
            "signal": "timeout",
            "then": [
              {
                "set": {
                  "property": "speed",
                  "value": 600.0
                }
              },
              {
                "print": "go"
              }
            ]
          }
        ]
      }
    },
    {
      "user": "after 2 seconds, set the player's speed to 600 and print go",
      "goal_sentence": "2.0 seconds after the scene loads, %Timer fires once and %Player sets its speed to 600.0 and prints 'go'. This sets %Timer wait_time 2.0, one_shot true, autostart true, and adds the handler _on_timer_timeout to %Player wired to %Timer's timeout.",
      "plan": {
        "intent": "after 2 seconds, set the player's speed to 600 and print go",
        "node": "%Player",
        "assumptions": [
          "'after 2 seconds' = the after step: a one-shot %Timer that starts when the scene loads",
          "the speed change belongs to %Player, which owns the handler"
        ],
        "steps": [
          {
            "do": "after",
            "seconds": 2.0,
            "then": [
              {
                "set": {
                  "property": "speed",
                  "value": 600.0
                }
              },
              {
                "print": "go"
              }
            ]
          }
        ]
      }
    },
    {
      "user": "every 2 seconds add 25 to the player's speed, never above 500, and print tick",
      "goal_sentence": "Every 2.0 seconds after the scene loads, for as long as it lives, %Player's speed goes up by 25.0 but never above 500.0, and it prints 'tick'. This sets %Timer wait_time 2.0, one_shot false, autostart true and replaces/adds the handler _on_timer_timeout on %Player wired to %Timer's timeout.",
      "plan": {
        "intent": "every 2 seconds add 25 to the player's speed, never above 500, and print tick",
        "node": "%Player",
        "assumptions": [
          "'every 2 seconds' = the every step: a repeating %Timer that starts when the scene loads",
          "'never above 500' = a max limit on speed after the increase",
          "the speed change belongs to %Player, which owns the handler"
        ],
        "steps": [
          {
            "do": "every",
            "seconds": 2.0,
            "then": [
              {
                "increase": {
                  "property": "speed",
                  "by": 25.0
                }
              },
              {
                "limit": {
                  "property": "speed",
                  "max": 500.0
                }
              },
              {
                "print": "tick"
              }
            ]
          }
        ]
      }
    },
    {
      "user": "every second make the player's velocity 1.5 times bigger",
      "refusal": "That would grow without end: a repeating change needs a limit. How long may the velocity get at most? Once you give a number I can plan it as a scale by 1.5 followed by a max_length limit."
    },
    {
      "user": "when the player touches the trigger, speed up by 10, never above 400, and remove the trigger",
      "goal_sentence": "When %Player touches %Trigger (body_entered), its speed goes up by 10.0 but never above 400.0, and %Trigger is freed so it fires once. The handler _on_trigger_body_entered(_body: Node2D) is added to %Player and runs only when the entering body is %Player itself.",
      "plan": {
        "intent": "when the player touches the trigger, speed up by 10, never above 400, and remove the trigger",
        "node": "%Player",
        "assumptions": [
          "'touches the trigger' = %Trigger's body_entered, only when the entering body is the player",
          "'remove the trigger' = free %Trigger as the last statement"
        ],
        "steps": [
          {
            "do": "when",
            "from": "%Trigger",
            "signal": "body_entered",
            "only": "self",
            "then": [
              {
                "increase": {
                  "property": "speed",
                  "by": 10.0
                }
              },
              {
                "limit": {
                  "property": "speed",
                  "max": 400.0
                }
              },
              {
                "free": "%Trigger"
              }
            ]
          }
        ]
      }
    },
    {
      "user": "put a coin at 420, 0 that speeds the player up by 25 when touched, never above 600, and disappears",
      "goal_sentence": "A new %Coin (an Area2D, 48 by 48) is added at (420, 0). When %Player touches it, its speed goes up by 25.0 but never above 600.0 and %Coin is freed, so it can only be picked up once. The handler _on_coin_body_entered(_body: Node2D) is added to %Player and runs only when the entering body is %Player itself.",
      "plan": {
        "intent": "put a coin at 420, 0 that speeds the player up by 25 when touched, never above 600, and disappears",
        "node": "%Player",
        "assumptions": [
          "'a coin' = an Area2D named Coin, 48 by 48 because the user gave no size",
          "'touched' = its body_entered, only when the entering body is the player",
          "'disappears' = free %Coin as the last statement"
        ],
        "steps": [
          {
            "do": "add_node",
            "class": "Area2D",
            "name": "Coin",
            "at": [
              420,
              0
            ],
            "size": [
              48,
              48
            ]
          },
          {
            "do": "when",
            "from": "%Coin",
            "signal": "body_entered",
            "only": "self",
            "then": [
              {
                "increase": {
                  "property": "speed",
                  "by": 25.0
                }
              },
              {
                "limit": {
                  "property": "speed",
                  "max": 600.0
                }
              },
              {
                "free": "%Coin"
              }
            ]
          }
        ]
      }
    }
  ]
}
PROTOEOF

# ---- 2. write patch_status.py
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
    ("step 10 assignment bodies", CLOCK, "def parse_literal"),
    ("step 12 relative math", CLOCK, "def apply_op"),
    ("step 12 plan tool: relative steps", "cascade_plan.py", "RELATIVE_VERBS"),
    ("step 13 multi-statement bodies and replace", CLOCK, "def statements_of"),
    ("step 13 plan tool: change_handler", "cascade_plan.py", "change_handler"),
    ("step 14 cross-node wiring", CLOCK, "def run_probe_cross"),
    ("step 14 plan tool: when ... from", "cascade_plan.py", "OPTIONAL_KEYS"),
    ("step 15 live check and timer ranges", CLOCK, "def run_live_check"),
    ("step 15 plan tool: after, node", "cascade_plan.py", 'elif do == "after"'),
    ("step 16 limit statement and guard", CLOCK, "def compounding_errors"),
    ("step 16 plan tool: every", "cascade_plan.py", 'elif do == "every"'),
    ("step 17 signals with arguments", CLOCK, "def run_probe_args_cross"),
    ("step 17 plan tool: only, free", "cascade_plan.py", "emitter_freed"),
    ("step 18 add_node in the clock", CLOCK, "def run_node_frame"),
    ("step 18 plan tool: add_node", "cascade_plan.py", "preview_add_node"),
    ("step 6  sweep fixes", SWEEP, "first frame of a process"), ("step 6  sweep message views", SWEEP, "--ultra"), ("step 6  sweep repeat mode", SWEEP, "--only"),
    ("step 3  registry: script field", BUILDER, '"script": script_path'), ("step 7  registry: signals and methods", BUILDER, '"signals": sigs'),
    ("step 9  registry: argument counts", BUILDER, '"signal_args": sig_args'),
]
JSON = [
    ("step 7  spec: connect_signal op", "task_block_spec.json", lambda d: d.get("ops", {}).get("connect_signal", {}).get("status") == "locked"),
    ("step 8  spec: add_method op", "task_block_spec.json", lambda d: d.get("ops", {}).get("add_method", {}).get("status") == "locked" and "fn" in d["slots"] and "add_method" in d["slots"]["op"]["enum"]),
    ("step 7  rag spec: signals, methods", "rag_record_spec.json", lambda d: "signals" in d["slots"] and "methods" in d["slots"]),
    ("step 9  rag spec: argument counts", "rag_record_spec.json", lambda d: "signal_args" in d["slots"] and "method_args" in d["slots"]),
    ("step 10 spec: assign body", "task_block_spec.json", lambda d: "assign" in d["slots"]["body"]["enum"] and "optional_slots" in d["ops"]["add_method"]),
    ("step 13 spec: mode slot", "task_block_spec.json", lambda d: "mode" in d["slots"] and "mode" in d["ops"]["add_method"].get("optional_slots", [])),
    ("step 14 spec: cross-node connect", "task_block_spec.json", lambda d: d["ops"]["connect_signal"].get("cross_node") is True),
    ("step 14 topology: %Timer pinned", "topology.json", lambda d: "%Timer" in d.get("pins", {})),
    ("step 18 spec: add_node op", "task_block_spec.json", lambda d: "add_node" in d["ops"]),
    ("step 18 vocabulary: add_node step", "cascade_vocab.json", lambda d: any(x.startswith("add_node:") for x in d["steps"])),
    ("step 17 spec: free statement and guard", "task_block_spec.json", lambda d: "free" in d["slots"]["statement"]["kinds"]),
    ("step 17 vocabulary: body_entered", "cascade_vocab.json", lambda d: "body_entered" in d["signals"]),
    ("step 16 spec: limit statement", "task_block_spec.json", lambda d: "limit" in d["slots"]["statement"]["kinds"]),
    ("step 16 vocabulary: every step", "cascade_vocab.json", lambda d: any(x.startswith("every:") for x in d["steps"])),
    ("step 15 spec: timer ranges", "task_block_spec.json", lambda d: "ranges" in d["ops"]["modify_property"]),
    ("step 15 props_allow: Timer", "props_allow.json", lambda d: "Timer" in d),
    ("step 15 vocabulary: after step", "cascade_vocab.json", lambda d: any(x.startswith("after:") for x in d["steps"])),
    ("step 14 vocabulary: timeout", "cascade_vocab.json", lambda d: "timeout" in d["signals"]),
    ("step 12 spec: operator slot", "task_block_spec.json", lambda d: "operator" in d["slots"] and "operator" in d["ops"]["add_method"].get("optional_slots", [])),
    ("step 12 vocabulary: repeats flags", "cascade_vocab.json", lambda d: all("repeats" in v for v in d["signals"].values())),
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
scn = os.path.join(ws, "engine_test", "scenes", "main.tscn")
ok14 = os.path.exists(scn) and 'type="Timer"' in open(scn, encoding="utf-8").read()
print(f"  {'in place' if ok14 else 'MISSING ':<9} {'step 14 scene: a Timer in main.tscn':<42} engine_test/scenes/main.tscn")
if not ok14:
    missing.append(("step 14 scene", "main.tscn"))
scn17 = os.path.join(ws, "engine_test", "scenes", "main.tscn")
ok17 = os.path.exists(scn17) and 'type="Area2D"' in open(scn17, encoding="utf-8").read()
print(f"  {'in place' if ok17 else 'MISSING ':<9} {'step 17 scene: an Area2D in main.tscn':<42} engine_test/scenes/main.tscn")
if not ok17:
    missing.append(("step 17 scene", "main.tscn"))
reg = os.path.join(ws, "state_registry.json")
if os.path.exists(reg):
    hosts = json.load(open(reg)).get("hosts", {})
    have = all("signal_args" in h and "method_args" in h for h in hosts.values()) and bool(hosts)
    print(f"  {'in place' if have else 'STALE   ':<9} {'registry data has argument counts':<42} state_registry.json" + ("" if have else "   (run registry_builder.py)"))
print("\nnothing was changed." if not missing else f"\n{len(missing)} piece(s) missing. A patch run (they are safe to repeat) fills them in; if one is BLOCKED it names the anchor.")
STATUSEOF

# ---- 2. write live_touch.gd
cat << 'WATCHEOF' > ~/cascade_ws/live_touch.gd
extends SceneTree
# touch a node in the real scene: the player is placed on it and left to the physics engine.
# usage: godot --headless --path engine_test --script live_touch.gd -- Coin
func _initialize() -> void:
    var args: PackedStringArray = OS.get_cmdline_user_args()
    var target: String = args[0] if args.size() > 0 else "Trigger"
    await process_frame
    var main: Node = load("res://scenes/main.tscn").instantiate()
    root.add_child(main)
    var p: Node = main.get_node("%Player")
    var t: Node = main.get_node_or_null("%" + target)
    if t == null:
        print("WATCH: there is no %", target, " in the scene")
        quit()
        return
    p.set_physics_process(false)
    for i in 6:
        await physics_frame
    print("WATCH before: speed=", p.speed, "  ", target, " present=", main.get_node_or_null("%" + target) != null)
    p.global_position = t.global_position
    for i in 30:
        await physics_frame
    print("WATCH after touching ", target, ": speed=", p.speed, "  ", target, " present=", main.get_node_or_null("%" + target) != null)
    quit()
WATCHEOF

# ---- 3. the patch: cascade_clock.py and task_block_spec.json (the scene, the registry and the other files are not touched)
python3 ~/cascade_ws/patch_node.py ~/cascade_ws
python3 ~/cascade_ws/patch_status.py | grep -E 'step 18|MISSING|STALE'

# ---- 4. the test plans
cat << 'P1EOF' > ~/cascade_ws/plan_coin.json
{"intent": "put a coin at 420, 0 that speeds the player up by 25 when touched, never above 600, and disappears", "node": "%Player", "assumptions": ["'a coin' = an Area2D named Coin, 48 by 48 because no size was given", "'touched' = its body_entered, only when the entering body is the player", "'disappears' = free %Coin as the last statement"], "steps": [{"do": "add_node", "class": "Area2D", "name": "Coin", "at": [420, 0], "size": [48, 48]}, {"do": "when", "from": "%Coin", "signal": "body_entered", "only": "self", "then": [{"increase": {"property": "speed", "by": 25.0}}, {"limit": {"property": "speed", "max": 600.0}}, {"free": "%Coin"}]}]}
P1EOF
cat << 'P2EOF' > ~/cascade_ws/plan_coin_bad.json
{"intent": "put a coin right on top of the trigger", "node": "%Player", "assumptions": ["x"], "steps": [{"do": "add_node", "class": "Area2D", "name": "Coin", "at": [300, 0], "size": [48, 48]}]}
P2EOF
cat << 'P3EOF' > ~/cascade_ws/plan_marker.json
{"intent": "put an empty marker at 0, 200 and a second one at 0, 400", "node": "%Player", "assumptions": ["x"], "steps": [{"do": "add_node", "class": "Marker2D", "name": "SpawnA", "at": [0, 200]}, {"do": "add_node", "class": "Marker2D", "name": "SpawnB", "at": [0, 400]}]}
P3EOF

# ---- 5. no GPU, nothing real touched. Copy everything from here down and paste it back.
cd ~/cascade_ws
python3 cascade_plan.py catalog | grep -A4 'NODES YOU CAN ADD'
echo
python3 cascade_plan.py expand plan_coin.json | grep -E '"op"|"class"|"name"|"parent"|"position"|"size"|"guard"|NOTE'
echo
echo "=== dry run: plan_coin.json (the node, its handler and its connection; about 6 s with the live physics check)"
python3 cascade_plan.py dry-run plan_coin.json
echo
echo "=== dry run: plan_coin_bad.json (must be refused: it would overlap the trigger)"
python3 cascade_plan.py dry-run plan_coin_bad.json
echo
echo "=== dry run: plan_marker.json (two nodes, no handlers)"
python3 cascade_plan.py dry-run plan_marker.json
