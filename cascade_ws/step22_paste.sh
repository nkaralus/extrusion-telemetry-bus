#!/usr/bin/env bash
# Step 22: add_sprite (Sprite2D with a texture) + live_sprite.gd + plan step + catalog line. Needs Steps 19-21 applied. WS and GODOT override the defaults.
set -euo pipefail
WS="${WS:-$HOME/cascade_ws}"; GODOT="${GODOT:-godot}"; cd "$WS"; mkdir -p blocks
cat > cascade_step22.py <<'PY1'
"""Step 22: add_sprite. The harness adds a Sprite2D (texture, centered) as a child of the target node, writing a 32x32 placeholder PNG when the .png is
missing and importing it headlessly, then proves with live_sprite.gd that the sprite exists, is visible and holds a loaded texture.
Scene edit: timestamped .bak; files this frame created are removed on any failed check. cascade_clock.py binds this module with bind(); patch_step22.py wires it in. No mule call."""
import hashlib, json, os, re, shutil, struct, subprocess, tempfile, time, zlib

c = None
def bind(mod):
    global c; c = mod

OP, CLS = "add_sprite", "Sprite2D"
PARENT_CLASSES = ("CharacterBody2D", "Area2D", "StaticBody2D", "Node2D", "Marker2D")
TEX_RX = r"^res://[A-Za-z0-9_][A-Za-z0-9_./-]*\.(png|svg|jpg|jpeg|webp)$"

def read(p): return open(p, "rb").read().decode("utf-8")
def write_atomic(p, text):
    tmp = p + ".cascade_tmp"; open(tmp, "wb").write(text.encode("utf-8")); os.replace(tmp, p)
def scene_file(project, host): return os.path.join(project, host["owner_scene"][len("res://"):])
def tex_file(project, tex): return os.path.join(project, tex[len("res://"):])
def centered(block): return block.get("centered", True)

def placeholder_png(name, size=32):
    h = hashlib.md5(name.encode()).digest(); fill = tuple(64 + h[i] % 160 for i in range(3)); edge = tuple(x // 2 for x in fill)
    rows = b"".join(b"\x00" + b"".join(bytes(edge if x in (0, size - 1) or y in (0, size - 1) else fill) + b"\xff" for x in range(size)) for y in range(size))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

def imported_ok(project, tp):
    ip = tp + ".import"
    if not os.path.exists(ip): return False
    m = re.search(r'^path="res://([^"]+)"', read(ip), re.M)
    return bool(m) and os.path.exists(os.path.join(project, m.group(1)))

def import_uid(tp):
    m = re.search(r'^uid="(uid://[^"]+)"', read(tp + ".import"), re.M) if os.path.exists(tp + ".import") else None
    return m.group(1) if m else None

# ---------------------------------------------------------------- the .tscn injector
def sprite_text(raw, block, uid):
    """(scene text with the Sprite2D and its Texture2D ext_resource added, 1-based line of the node)."""
    nl = "\r\n" if "\r\n" in raw else "\n"; lines = raw.split(nl)
    name, pp, tex = block["name"], c.node_parent_path(block["target"]), block["texture"]
    used = set(re.findall(r'\bid="([^"]+)"', raw)); rid = f"{len(used) + 1}_{name.lower()}"
    while rid in used: rid += "_n"
    ext = f'[ext_resource type="Texture2D"' + (f' uid="{uid}"' if uid else "") + f' path="{tex}" id="{rid}"]'
    for attrs in re.findall(r"\[ext_resource ([^\]]*)\]", raw):                # the scene already loads this texture: reuse its entry
        d = dict(re.findall(r'(\w+)="([^"]*)"', attrs))
        if d.get("type") == "Texture2D" and d.get("path") == tex: rid, ext = d["id"], None; break
    node = [f'[node name="{name}" type="{CLS}" parent="{pp}"]', "unique_name_in_owner = true", f'texture = ExtResource("{rid}")'] + ([] if centered(block) else ["centered = false"]) + [""]
    if not any(l.startswith("[node ") for l in lines): raise c.ApplyError("the scene has no [node ...] section")
    conn = [i for i, l in enumerate(lines) if l.startswith("[connection ") or l.startswith("[editable ")]
    if conn: lines[min(conn):min(conn)] = node
    else:
        while lines and not lines[-1].strip(): lines.pop()
        lines += [""] + node
    exts = [i for i, l in enumerate(lines) if l.startswith("[ext_resource ")]
    if ext is None: pass
    elif exts: lines.insert(exts[-1] + 1, ext)
    else:
        first = next(i for i, l in enumerate(lines) if l.startswith("[sub_resource ") or l.startswith("[node "))
        lines[first:first] = [ext, ""]
    text = nl.join(lines); m = re.search(r"load_steps=(\d+)", text)
    if m and ext is not None: text = text.replace(m.group(0), f"load_steps={int(m.group(1)) + 1}", 1)
    return text, next(i for i, l in enumerate(text.split(nl)) if l.startswith(f'[node name="{name}" ')) + 1

def matches(project, host, block):
    """True when the scene already has exactly this sprite (type, parent, unique name, texture path, centered, visible)."""
    fp = scene_file(project, host)
    if not os.path.exists(fp): return False
    raw = read(fp); nodes = c.scene_node_blocks(raw); pp = c.node_parent_path(block["target"])
    exts = {}
    for attrs in re.findall(r"\[ext_resource ([^\]]*)\]", raw):
        d = dict(re.findall(r'(\w+)="([^"]*)"', attrs)); exts[d.get("id")] = d.get("path")
    for i, at, props in nodes:
        if at.get("name") != block["name"] or at.get("parent") != pp: continue
        if at.get("type") != CLS or props.get("unique_name_in_owner") != "true": return False
        m = re.search(r'ExtResource\("([^"]+)"\)', props.get("texture", ""))
        return bool(m) and exts.get(m.group(1)) == block["texture"] and (props.get("centered", "true") == "true") == centered(block) and props.get("visible", "true") == "true"
    return False

# ---------------------------------------------------------------- GI_1 / GI_3
def slot1(prev, block, reg, spec, project):
    if block.get("op") != OP: return prev(block, reg, spec, project)
    s, ops, bad = spec["slots"], spec["ops"].get(OP) or {}, []
    if ops.get("status") != "locked": return None, [f"the spec is behind the clock: it has no locked '{OP}' op. Run: python3 patch_step22.py"]
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
    name, tex = block["name"], block["texture"]
    if not isinstance(name, str) or not re.match(c.NODE_NAME_RX, name): bad.append(f"block.name '{name}' must be 2 to 24 letters or digits and start with a capital letter")
    if not isinstance(tex, str) or not re.match(s["texture"]["rx"], tex) or ".." in tex: bad.append(f"block.texture '{tex}' must be a res:// path to a .png, .svg, .jpg, .jpeg or .webp file, without ..")
    if "centered" in block and not isinstance(block["centered"], bool): bad.append("block.centered must be true or false")
    if bad: return host, bad
    if host["node"] not in PARENT_CLASSES: return host, [f"GI_1 parent: a sprite goes on a {', '.join(PARENT_CLASSES)}; {host['name']} is a {host['node']}"]
    tp = tex_file(project, tex)
    if not os.path.isfile(tp) and not tex.endswith(".png"): bad.append(f"GI_1 texture: {tex} is not in the project; only a missing .png is created (as a placeholder)")
    if matches(project, host, block): return host, bad
    nodes = c.scene_node_blocks(read(scene_file(project, host))); used = [at.get("name") for _, at, _ in nodes]; pp = c.node_parent_path(block["target"])
    other = [at.get("name") for _, at, _ in nodes if at.get("type") == CLS and at.get("parent") == pp and at.get("name") != name]
    if other: bad.append(f"GI_1 sprite: {host['name']} already has a Sprite2D ({', '.join(other)}); a node gets one")
    if name in used: bad.append(f"GI_1 name: a node named {name} already exists in {host['owner_scene']} (nodes: {', '.join(sorted(set(used)))})")
    if f"%{name}" in {h["name"] for h in reg["hosts"].values()}: bad.append(f"GI_1 name: %{name} is already in the registry")
    n_scene = sum(1 for h in reg["hosts"].values() if h["owner_scene"] == host["owner_scene"])
    if n_scene + 1 > c.MAX_NODES_PER_SCENE: bad.append(f"GI_1 scene_limit: {host['owner_scene']} already holds {n_scene} registered nodes; the limit is {c.MAX_NODES_PER_SCENE}")
    return host, bad

# ---------------------------------------------------------------- SLOT_3/4 apply (scene .bak; created files removed on failure)
def run_import(godot, project):
    try: subprocess.run([godot, "--headless", "--path", project, "--import"], capture_output=True, text=True, timeout=180)
    except FileNotFoundError: c.die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired: return "the headless import timed out (180 s)"
    return ""

def undo(project, host, res):
    if res.get("backup") and os.path.exists(res["backup"]): shutil.copy2(res["backup"], scene_file(project, host))
    for rp in res.get("created", []):
        p = tex_file(project, rp)
        if os.path.exists(p): os.remove(p)

def apply_sprite(godot, project, host, block):
    fp, tex = scene_file(project, host), block["texture"]; tp = tex_file(project, tex)
    if not os.path.exists(fp): raise c.ApplyError(f"{host['owner_scene']} not found")
    res = {"file": host["owner_scene"], "backup": None, "created": [], "action": "inserted", "verified": False, "texture": tex}
    if not os.path.isfile(tp):
        os.makedirs(os.path.dirname(tp), exist_ok=True); open(tp, "wb").write(placeholder_png(block["name"])); res["created"].append(tex); res["placeholder"] = True
    if not imported_ok(project, tp):
        had = os.path.exists(tp + ".import"); prob = run_import(godot, project)
        if not prob and not imported_ok(project, tp): prob = f"Godot did not import {tex} (no usable {os.path.basename(tp)}.import)"
        if os.path.exists(tp + ".import") and not had: res["created"].append(tex + ".import")
        if prob: res["problem"] = prob; undo(project, host, res); res["rolled_back"] = True; return res
    raw = read(fp); new, line_no = sprite_text(raw, block, import_uid(tp))
    res["backup"] = f"{fp}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"; shutil.copy2(fp, res["backup"]); write_atomic(fp, new)
    r, err = c.run_node_reload(godot, project, host["owner_scene"], block["name"], c.scene_connections(new))
    problem = err if r is None else c.check_node_result(r, {"name": block["name"], "class": CLS, "position": [0.0, 0.0]})
    res.update(line=line_no, verified=not problem, after=f"{CLS} {block['name']} on {block['target']}, texture {tex}, {'centered' if centered(block) else 'not centered'}")
    if problem: res["problem"] = problem; undo(project, host, res); res["rolled_back"] = True
    else: res["reloaded_value"] = f"{r['n_connections']} scene connections still wired"
    return res

# ---------------------------------------------------------------- SLOT_5 live check
def run_gd(godot, project, gd, args, timeout=90):
    out = os.path.join(tempfile.mkdtemp(prefix="cascade_s22_"), "out.json")
    try: r = subprocess.run([godot, "--headless", "--path", project, "--fixed-fps", "60", "--script", gd, "--", out, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError: c.die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired: return None, f"timeout ({timeout} s)"
    if not os.path.exists(out): return None, c.strip_ansi(r.stdout + r.stderr)[-300:].strip()
    return json.load(open(out)), ""

def verify(godot, project, ws, host, block):
    gd = os.path.join(ws, "live_sprite.gd")
    if not os.path.exists(gd): return {"verdict": "FAIL", "summary": f"{gd} is missing", "warnings": []}
    r, err = run_gd(godot, project, gd, [host["owner_scene"], host["name"].lstrip("%"), block["name"], block["texture"], "1" if centered(block) else "0"])
    if r is None: return {"verdict": "FAIL", "summary": "the live check could not run: " + err, "warnings": []}
    return {"verdict": r["verdict"], "summary": r["summary"], "warnings": [], "checks": r["checks"]}

# ---------------------------------------------------------------- the frame
def run_frame(a, ws, project, host, block, row, finish):
    log = c.log; fields = {"class": CLS, "name": block["name"], "parent": block["target"], "texture": block["texture"], "centered": centered(block)}
    row.update(prefill=False, slim=False, ultra=False, temps=[], example=False, statements=0, body_mode="sprite")
    log(f"SPRITE          add {CLS} %{block['name']} on {block['target']}: texture {block['texture']}, {'centered' if centered(block) else 'not centered'}; the harness writes the scene text; no mule call")
    if a.verify_only:
        vr = verify(a.godot, project, ws, host, block); log("SLOT_5 VERIFY   " + c.verify_line(vr))
        log(f"RESULT: VERIFY {vr['verdict']} (verify only, nothing was written)"); finish(5 if vr["verdict"] == "FAIL" else 0, status="VERIFY_" + vr["verdict"], verify=vr, uid=host["uid"], **fields)
    if matches(project, host, block):
        log(f"RESULT: NOOP - %{block['name']} ({CLS}) is already on {block['target']} exactly as asked"); finish(0, status="NOOP_ALREADY_DEFINED", uid=host["uid"], **fields)
    log(f"SLOT_2 ROUTE    ok   op={OP}  (no mule call, GI_2 not needed)")
    log("SLOT_3 EXECUTE  " + f"[node name=\"{block['name']}\" type=\"{CLS}\" parent=\"{c.node_parent_path(block['target'])}\"] texture = {block['texture']}" + ("" if centered(block) else ", centered = false"))
    log("SLOT_4 JUDGE    ACK  GI_1 passed; the import and the scene reload below are the parse check")
    status, extra, code = "ACK", {}, 0
    if not a.no_apply:
        try: res = apply_sprite(a.godot, project, host, block)
        except c.ApplyError as ex: log(f"SLOT_4 APPLY    NOT WRITTEN  {ex}"); status, extra, code = "ACK_NOT_APPLIED", {"apply_error": str(ex)}, 4
        else:
            for rp in res["created"]: log(f"SLOT_3 TEXTURE   created {rp}" + ("  (32x32 placeholder)" if res.get("placeholder") and rp == block["texture"] else ""))
            if res["backup"]: log(f"SLOT_4 APPLY    {res['action']} line {res.get('line', '?')} of {res['file'][len('res://'):]}: {res.get('after', '')}"); log(f"                backup: {os.path.relpath(res['backup'], ws)}")
            if res["verified"]:
                log(f"SLOT_4 RELOAD   ok   {res['reloaded_value']}"); status, extra = "APPLIED", {"apply": res}
                if not a.no_verify:
                    vr = verify(a.godot, project, ws, host, block); extra["verify"] = vr; log("SLOT_5 VERIFY   " + c.verify_line(vr))
                    if vr["verdict"] == "FAIL": undo(project, host, res); log("SLOT_5 VERIFY   FAILED, ROLLED BACK  the edit was undone and the files this frame created were removed"); status, code = "ROLLED_BACK_VERIFY", 4
                if status == "APPLIED":
                    rr = c.refresh_registry_node(ws, a.godot, block); extra["registry_refresh"] = rr
                    log(f"REGISTRY       refreshed: %{block['name']} is listed at {rr['addr']} ({rr['nodes']} nodes in the registry now)" if rr.get("ok") and rr.get("listed") else f"REGISTRY       NOT refreshed - run registry_builder.py by hand ({rr.get('detail') or 'the node is not listed'})")
            else: log(f"SLOT_4 APPLY    FAILED, ROLLED BACK  {res['problem']}"); status, extra, code = "ROLLED_BACK", {"apply": res}, 4
    log(f"RESULT: {status} (no mule call) -> ledger.jsonl")
    finish(code, status=status, tries=0, stmt=f"add {CLS} {block['name']} on {block['target']}", uid=host["uid"], **fields, **extra)
PY1
cat > live_sprite.gd <<'GD1'
extends SceneTree
# Live sprite check. Run: godot --headless --path <project> --fixed-fps 60 --script live_sprite.gd -- <out.json> <scene> <parent> <sprite> <texture> <centered 0|1>
const MAX_TICKS: int = 200
const SETTLE_TICKS: int = 6
var _out: String = ""
var _parent: Node2D = null
var _sprite: Sprite2D = null
var _tex_path: String = ""
var _centered: bool = true
var _ticks: int = 0
var _checks: Array[Dictionary] = []

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    _out = a[0]
    _tex_path = a[4]
    _centered = a[5] == "1"
    var packed: PackedScene = load(a[1])
    if packed == null:
        _finish("scene did not load")
        return
    var root: Node = packed.instantiate()
    root.name = "LiveRoot"
    get_root().add_child(root)
    _parent = root.get_node_or_null("%" + a[2]) as Node2D
    _sprite = root.get_node_or_null("%" + a[3]) as Sprite2D
    if _parent == null or _sprite == null:
        _finish("%" + a[2] + " (Node2D) or %" + a[3] + " (Sprite2D) is missing from the scene")

func _check(label: String, ok: bool, detail: String) -> void:
    _checks.append({"name": label, "ok": ok, "detail": detail})

func _physics_process(_delta: float) -> bool:
    if _sprite == null or _parent == null:
        return true
    _ticks += 1
    if _ticks == SETTLE_TICKS:
        var tex: Texture2D = _sprite.texture
        _check("sprite is a Sprite2D child of the target", _sprite.get_parent() == _parent, "parent is %s" % _sprite.get_parent().name)
        _check("sprite is visible", _sprite.visible and _sprite.is_visible_in_tree() and _sprite.modulate.a > 0.0, "visible=%s, in tree=%s, alpha=%.2f" % [_sprite.visible, _sprite.is_visible_in_tree(), _sprite.modulate.a])
        _check("sprite has a texture", tex != null, "texture=%s" % tex)
        if tex != null:
            var size: Vector2 = tex.get_size()
            _check("texture is the requested resource", tex.resource_path == _tex_path, "resource_path=%s" % tex.resource_path)
            _check("texture is loaded with a real size", size.x > 0.0 and size.y > 0.0, "size %s" % size)
            _check("texture loads from disk by itself", ResourceLoader.exists(_tex_path) and load(_tex_path) is Texture2D, "ResourceLoader.exists=%s" % ResourceLoader.exists(_tex_path))
            var r: Rect2 = _sprite.get_rect()
            if _centered:
                _check("centered: image is centred on the sprite origin", _sprite.centered and r.get_center().length() < 0.01 and r.size.is_equal_approx(size), "rect %s" % r)
            else:
                _check("not centered: image starts at the sprite origin", (not _sprite.centered) and r.position.length() < 0.01 and r.size.is_equal_approx(size), "rect %s" % r)
        _check("sprite sits on the target's origin", _sprite.global_position.is_equal_approx(_parent.global_position), "sprite %s, target %s" % [_sprite.global_position, _parent.global_position])
    if _ticks == SETTLE_TICKS + 10:
        _check("sprite is still there and visible", is_instance_valid(_sprite) and _sprite.texture != null and _sprite.is_visible_in_tree(), "after %d ticks" % _ticks)
        _finish("")
        return true
    if _ticks > MAX_TICKS:
        _finish("timeout")
        return true
    return false

func _finish(note: String) -> void:
    var failed: Array[String] = []
    for c in _checks:
        if not c["ok"]:
            failed.append(str(c["name"]))
    var ok: bool = note == "" and failed.is_empty() and _checks.size() > 0
    var summary: String = ""
    if ok:
        summary = "sprite %s on %s: visible, texture %s loaded (%s), %s" % [_sprite.name, _parent.name, _tex_path, _sprite.texture.get_size(), "centered" if _centered else "origin at top-left"]
    elif _checks.is_empty() or failed.is_empty():
        summary = note
    else:
        summary = (note + "; " if note != "" else "") + "%d of %d checks failed: %s" % [failed.size(), _checks.size(), ", ".join(failed)]
    var f: FileAccess = FileAccess.open(_out, FileAccess.WRITE)
    f.store_string(JSON.stringify({"verdict": "PASS" if ok else "FAIL", "checks": _checks, "summary": summary}))
    f.close()
    quit(0 if ok else 1)
GD1
cat > patch_step22.py <<'PY2'
#!/usr/bin/env python3
"""Step 22 patcher (idempotent, .bak first): adds add_sprite to task_block_spec.json, wires cascade_step22 into cascade_clock.py (after Step 21),
and, when cascade_plan.py is there, adds the add_sprite plan step and its catalog line. Writes plan_sprite.json."""
import argparse, json, os, shutil, sys, time
ap = argparse.ArgumentParser(); ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws")); ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))
OP = "add_sprite"; BASE = ["frame_id", "time_slot", "subnet", "op", "target", "ref", "exclude", "name", "texture"]; OPT = ["centered"]
TEX_RX = r"^res://[A-Za-z0-9_][A-Za-z0-9_./-]*\.(png|svg|jpg|jpeg|webp)$"

def backup(p, tag):
    bak = f"{p}.{tag}.bak"
    if os.path.exists(bak): bak += "." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(p, bak); return os.path.basename(bak)
def put(p, text):
    if p.endswith(".py"): compile(text, p, "exec")
    tmp = p + ".cascade_tmp"; open(tmp, "w", encoding="utf-8").write(text); os.replace(tmp, p)

# ---- spec
sp = os.path.join(ws, "task_block_spec.json"); spec = json.load(open(sp))
if "name" not in spec["slots"]: sys.exit("STOPPED: the spec lacks the Step 18 slot name; nothing written")
if OP in spec["slots"]["op"]["enum"] and OP in spec["ops"]: print("spec: already patched")
else:
    backup(sp, "step22")
    if OP not in spec["slots"]["op"]["enum"]: spec["slots"]["op"]["enum"].append(OP)
    spec["slots"].update({"texture": {"type": "string", "rx": TEX_RX}, "centered": {"type": "bool"}})
    spec["ops"][OP] = {"status": "locked", "slots": BASE, "optional_slots": OPT,
        "v1": "the harness adds a Sprite2D (name, texture, centered; default true) as a child of the block's target node (a CharacterBody2D, Area2D, StaticBody2D, Node2D or Marker2D); no mule call. texture is a res:// .png, .svg, .jpg, .jpeg or .webp; a missing .png is created as a 32x32 placeholder and imported headlessly. GI_1: one Sprite2D per node, name unused in the scene. SLOT_5 runs live_sprite.gd: the sprite is a child of the target, visible, holds the requested loaded texture with a real size, is centred as asked and sits on the target's origin"}
    json.dump(spec, open(sp, "w"), indent=2); print("spec: patched")

# ---- clock
cp = os.path.join(ws, "cascade_clock.py"); src = open(cp, encoding="utf-8").read()
if "cascade_step22" in src: print("clock: already patched")
else:
    A1 = "# ---------------------------------------------------------------- the clock\ndef main():"
    A2 = '    if block["op"] == "add_camera": return _s21.run_frame(a, ws, project, host, block, row, finish)\n'
    if "cascade_step21" not in src: sys.exit("STOPPED: Step 21 is not wired into the clock; run step21_paste.sh first")
    if src.count(A1) != 1 or src.count(A2) != 1: sys.exit("STOPPED: the clock does not have the expected anchors; nothing written")
    B1 = ("# ---------------------------------------------------------------- Step 22: add_sprite (Sprite2D with a texture; see cascade_step22.py)\n"
          "import cascade_step22 as _s22\n_s22.bind(sys.modules[__name__])\n_step21_slot1 = slot1\n\n"
          "def slot1(block, reg, spec, project): return _s22.slot1(_step21_slot1, block, reg, spec, project)\n\n")
    B2 = A2 + '    if block["op"] == "add_sprite": return _s22.run_frame(a, ws, project, host, block, row, finish)\n'
    backup(cp, "step22"); put(cp, src.replace(A1, B1 + A1).replace(A2, B2)); print("clock: patched")

# ---- plan layer
EXPAND_ANCHOR = '        elif do == "when":\n'
EXPAND = '''        elif do == "add_sprite":
            if not isinstance(step["name"], str):
                raise PlanError(f"{where}: 'name' must be text")
            if not isinstance(step["texture"], str):
                raise PlanError(f"{where}: 'texture' must be a res:// path")
            if "centered" in step and not isinstance(step["centered"], bool):
                raise PlanError(f"{where}: 'centered' must be true or false")
            blk = head(hh, "add_sprite")
            blk.update(target=hh["name"], ref=hh["addr"], exclude=list(EXC.get("add_sprite", EXC["add_node"])), name=step["name"], texture=step["texture"])
            if "centered" in step:
                blk["centered"] = step["centered"]
            blocks.append(blk)
            notes.append(f"add_sprite: %{step['name']} (Sprite2D, {step['texture']}) added to {hh['name']} in {hh['owner_scene']}")
'''
DESC_ANCHOR = '        if b["op"] == "connect_signal":\n            if b["source"] != b["target"]:\n'
DESC = '''        if b["op"] == "add_sprite":
            return f"ADD SPRITE %{b['name']} on {b['target']} ({b['texture']}, " + ("centered)" if b.get("centered", True) else "top-left origin)")
'''
CANON_ANCHOR = '    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""\n'
CANON = '''    if b["op"] == "add_sprite":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
'''
CAT_SPRITE = r'''        L.append("  ALSO {\"do\": \"add_sprite\", \"name\": ..., \"texture\": \"res://sprites/x.png\", \"centered\": true}: a Sprite2D child of the plan's node (a CharacterBody2D, Area2D, StaticBody2D, Node2D or Marker2D) showing that texture; a .png that is not in the project yet is created as a 32x32 placeholder, .svg/.jpg/.webp must already be there; one sprite per node; \"centered\" is optional")'''
RULES = '        L.append("  rules: a name of 2 to 24 letters or digits'
def section(text, fn): return text.split("def " + fn)[1].split("\ndef ")[0]

pp = os.path.join(ws, "cascade_plan.py")
if not os.path.exists(pp): print("cascade_plan.py: not in the workspace, plan layer skipped")
else:
    psrc = new = open(pp, encoding="utf-8").read(); done = []
    def once(anchor, what):
        if new.count(anchor) != 1: sys.exit(f"STOPPED: cascade_plan.py has {new.count(anchor)} places for '{what}' (expected 1); nothing written")
    lines = new.split("\n")
    for key, add in (("STEP_KEYS = {", ', "add_sprite": {"name", "texture"}}'), ("OPTIONAL_KEYS = {", ', "add_sprite": {"centered", "node"}}')):
        i = next((n for n, l in enumerate(lines) if l.startswith(key)), None)
        if i is None or not lines[i].rstrip().endswith("}"): sys.exit(f"STOPPED: cannot find the {key.split(' ')[0]} line; nothing written")
        if '"add_sprite"' not in lines[i]: lines[i] = lines[i].rstrip()[:-1] + add; done.append(key.split(" ")[0])
    new = "\n".join(lines)
    if 'do == "add_sprite"' not in section(new, "expand_plan"): once(EXPAND_ANCHOR, "expand_plan"); new = new.replace(EXPAND_ANCHOR, EXPAND + EXPAND_ANCHOR); done.append("expand_plan")
    if "ADD SPRITE %" not in section(new, "describe"): once(DESC_ANCHOR, "describe"); new = new.replace(DESC_ANCHOR, DESC + DESC_ANCHOR); done.append("describe")
    if 'b["op"] == "add_sprite"' not in section(new, "canonical_replies"): once(CANON_ANCHOR, "canonical_replies"); new = new.replace(CANON_ANCHOR, CANON_ANCHOR + CANON); done.append("canonical_replies")
    lines = new.split("\n"); ri = [n for n, l in enumerate(lines) if l.startswith(RULES)]
    if len(ri) != 1: sys.exit(f"STOPPED: cascade_plan.py has {len(ri)} catalog 'rules:' lines (expected 1); nothing written")
    if 'do\\": \\"add_sprite' not in section(new, "cmd_catalog"): lines.insert(ri[0] + 1, CAT_SPRITE); done.append("catalog"); new = "\n".join(lines)
    if new != psrc: bak = backup(pp, "step22"); put(pp, new); print("cascade_plan.py: patched (" + ", ".join(done) + f"); backup {bak}")
    else: print("cascade_plan.py: already patched")
    text = ('{\n  "intent": "give the coin a visible sprite",\n  "node": "%Coin",\n  "steps": [\n    {"do": "add_sprite", "name": "CoinSprite", "texture": "res://sprites/coin.png"}\n  ]\n}\n')
    pf = os.path.join(ws, "plan_sprite.json")
    if not os.path.exists(pf) or open(pf, encoding="utf-8").read() != text: open(pf, "w", encoding="utf-8").write(text); print("plan_sprite.json: written")
PY2
cat > blocks/p22_sprite.json <<'JS'
{"frame_id":"0x2201","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"add_sprite","target":"%Coin","ref":"10.1.1.13",
 "exclude":["KinematicBody2D","untyped_variables","relative_paths_up_tree","cross_subnet_queries"],
 "name":"CoinSprite","texture":"res://sprites/coin.png","centered":true}
JS
python3 patch_step22.py --ws "$WS"
python3 cascade_clock.py blocks/p22_sprite.json --ws "$WS" --godot "$GODOT" --no-apply
python3 cascade_clock.py blocks/p22_sprite.json --ws "$WS" --godot "$GODOT"
