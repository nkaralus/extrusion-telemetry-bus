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
