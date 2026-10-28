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
