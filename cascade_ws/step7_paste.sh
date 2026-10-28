# ---- 0. the stub method you added must be there (the guard will only accept a method that exists)
grep -n "func _on_player_ready" ~/cascade_ws/engine_test/scripts/player.gd || echo "MISSING: add the stub to player.gd first"

# ---- 1. build connect_signal into the clock, the registry builder and both specs (backups: *.before-connect)
cat << 'PEOF' > ~/cascade_ws/patch_connect.py
import json, os, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1]))
paths = {n: os.path.join(ws, n) for n in ("cascade_clock.py", "registry_builder.py", "task_block_spec.json", "rag_record_spec.json")}
for n, p in paths.items():
    if not os.path.exists(p): sys.exit(f"STOPPED: {p} not found")
clock = open(paths["cascade_clock.py"], encoding="utf-8").read()
builder = open(paths["registry_builder.py"], encoding="utf-8").read()
tspec = json.load(open(paths["task_block_spec.json"], encoding="utf-8"))
rspec = json.load(open(paths["rag_record_spec.json"], encoding="utf-8"))
if "def connect_checks" in clock: sys.exit("connect_signal is already built in.")
if "a.temps" not in clock or "ULTRA_VIEW" not in clock: sys.exit("Apply the earlier clock patches first (patch_defaults.py).")
if '"script": script_path' not in builder: sys.exit("Apply patch_builder.py to registry_builder.py first.")

# ------------------------------------------------------------------ registry_builder.py: signals and methods
BUILDER_EDITS = [
 (r'''    var script_path: String = scr.resource_path if scr != null else ""
''',
  r'''    var script_path: String = scr.resource_path if scr != null else ""
    var sigs: Array = []
    for s in ClassDB.class_get_signal_list(cls):
        sigs.append(str(s["name"]))
    var methods: Array = []
    if scr != null:
        for s in scr.get_script_signal_list():
            if not sigs.has(str(s["name"])):
                sigs.append(str(s["name"]))
        for m in scr.get_script_method_list():
            methods.append(str(m["name"]))
    sigs.sort()
    methods.sort()
'''),
 (r'''"tree": str(root.name) + "/" + str(root.get_path_to(n)), "props": props,
''',
  r'''"tree": str(root.name) + "/" + str(root.get_path_to(n)), "props": props,
        "signals": sigs, "methods": methods,
'''),
 (r'''"tree": h["tree"], "props": h["props"],
''',
  r'''"tree": h["tree"], "props": h["props"], "signals": h["signals"], "methods": h["methods"],
'''),
 (r'''    for k, v in s["peering"]["slots"].items():
''',
  r'''    for k in ("signals", "methods"):
        for x in rec.get(k, []):
            if k in s and not re.match(s[k]["item_rx"], x): bad.append(f"{k} '{x}' does not match the spec")
    for k, v in s["peering"]["slots"].items():
'''),
]
for old, new in BUILDER_EDITS:
    if builder.count(old) != 1: sys.exit("Could not find the expected text exactly once in registry_builder.py, so I changed nothing:\n" + old[:90])
    builder = builder.replace(old, new, 1)

# ------------------------------------------------------------------ the two spec files
tspec["slots"]["source"] = {"type": "string", "rx": "^%[A-Za-z_][A-Za-z0-9_]*$", "enum_from": "state_registry.names_in_subnet"}
tspec["slots"]["signal"] = {"type": "string", "rx": "^[A-Za-z_][A-Za-z0-9_]*$", "enum_from": "rag_record.signals"}
tspec["slots"]["method"] = {"type": "string", "rx": "^_on_[a-z0-9_]+$", "enum_from": "rag_record.methods", "must_equal": "_on_<snake(target)>_<signal>"}
tspec["ops"]["connect_signal"] = {
    "status": "locked",
    "slots": ["frame_id", "time_slot", "subnet", "op", "source", "target", "signal", "method", "ref", "exclude"],
    "v1": "source must equal target; the method is _on_<node>_<signal> and must already exist in the node's script; the harness writes the [connection] line, the mule only types the method name",
}
rspec["slots"]["signals"] = {"type": "array", "items": "string", "item_rx": "^[A-Za-z_][A-Za-z0-9_]*$", "source": "godot_class_db_and_script"}
rspec["slots"]["methods"] = {"type": "array", "items": "string", "item_rx": "^[A-Za-z_][A-Za-z0-9_]*$", "source": "godot_script"}

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- connect_signal (Step 7): one [connection] line in the owner scene
def snake(name): return re.sub(r"[^a-z0-9]+", "_", name.lstrip("%").lower()).strip("_")

def op_fields(block):
    if block["op"] == "connect_signal": return {"source": block["source"], "target": block["target"], "signal": block["signal"], "method": block["method"]}
    return {"property": block["property"], "type": block["type"], "value": block["value"]}

def node_rel(host): return host["tree"].split("/", 1)[1] if "/" in host["tree"] else ""

def connection_line(block, host):
    rel = node_rel(host)
    return f'[connection signal="{block["signal"]}" from="{rel}" to="{rel}" method="{block["method"]}"]'

def connect_checks(block, host, spec, project):
    bad = []
    for k in ("source", "signal", "method"):
        if not re.match(spec["slots"][k]["rx"], str(block[k])): bad.append(f"block.{k} '{block[k]}' does not match the spec")
    if block["source"] != host["name"]: bad.append(f"GI_1 addr_in_registry: {block['ref']} is {host['name']}, block says source {block['source']}")
    if block["source"] != block["target"]: bad.append("GI_1 connect_scope: v1 connects a node to its own method, so source must equal target (other pairs are an open slot)")
    for k in ("signals", "methods"):
        if k not in host: bad.append(f"GI_1 registry has no '{k}' field; update and re-run registry_builder.py")
    if bad: return bad
    if block["signal"] not in host["signals"]:
        bad.append(f"GI_1 signal: '{block['signal']}' is not a signal of {host['name']} ({host['node']}); it has {len(host['signals'])}, e.g. {', '.join(host['signals'][:6])}")
    if block["method"] not in host["methods"]:
        bad.append(f"GI_1 method: '{block['method']}' is not defined in {host.get('script') or 'the node'}; add it first (defined: {', '.join(host['methods']) or 'none'})")
    want = f"_on_{snake(block['target'])}_{block['signal']}"
    if block["method"] != want: bad.append(f"GI_1 method_name: the method must be {want} (the _on_<node>_<signal> convention)")
    return bad

def connection_exists(project, host, block):
    fp = os.path.join(project, host["owner_scene"][len("res://"):])
    if not os.path.exists(fp): return False
    rel = node_rel(host); want = {"signal": block["signal"], "from": rel, "to": rel, "method": block["method"]}
    for l in open(fp, encoding="utf-8").read().splitlines():
        if l.startswith("[connection "):
            at = dict(re.findall(r'(\w+)="([^"]*)"', l))
            if all(at.get(k) == v for k, v in want.items()): return True
    return False

def edit_scene_connection(raw, line):
    """Pure text edit: returns (new_text, action, line_number). Connections sit after the nodes and before any [editable] sections."""
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)
    if any(l.strip() == line for l in lines): raise ApplyError("this connection is already in the scene")
    conn = [i for i, l in enumerate(lines) if l.startswith("[connection ")]
    if conn:
        at = conn[-1] + 1; lines.insert(at, line); return nl.join(lines), "inserted", at + 1
    edit = [i for i, l in enumerate(lines) if l.startswith("[editable ")]
    if edit:
        at = edit[0]; lines[at:at] = [line, ""]; return nl.join(lines), "inserted", at + 1
    last = max(i for i, l in enumerate(lines) if l.strip())
    lines[last + 1:last + 1] = ["", line]
    return nl.join(lines), "inserted", last + 3

RELOAD_CONN_HELPER = r"""extends SceneTree

func _init() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var res := {"found": false, "connections": []}
    var packed: PackedScene = load(a[1])
    if packed != null:
        var root: Node = packed.instantiate()
        var n: Node = root.get_node_or_null("%" + a[2])
        if n != null:
            res["found"] = true
            for c in n.get_signal_connection_list(a[3]):
                var cb: Callable = c["callable"]
                res["connections"].append([str(cb.get_method()), cb.get_object() == n])
        root.free()
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(res))
    f.close()
    quit()
"""

def reload_check_connection(godot, project, host, block):
    tmp = tempfile.mkdtemp(prefix="cascade_reload_")
    helper, out = os.path.join(tmp, "reload.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(RELOAD_CONN_HELPER)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), block["signal"]],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return False, None, "reload timeout (60 s)"
    if not os.path.exists(out): return False, None, "Godot could not reload the scene:\n" + strip_ansi(r.stdout + r.stderr)[-400:]
    res = json.load(open(out))
    if not res.get("found"): return False, None, f"{host['name']} was not found in {host['owner_scene']} after the edit"
    got = res["connections"]; ok = any(m == block["method"] and mine for m, mine in got)
    return ok, got, "" if ok else f"the scene loaded but {host['name']}.{block['signal']} is not connected to {block['method']} (connections: {got})"

def apply_scene_connection(godot, project, host, block):
    """Add one [connection] line to the owner scene: .bak backup first, headless reload check after, rollback if the connection is not there."""
    fp = os.path.join(project, host["owner_scene"][len("res://"):])
    if not os.path.exists(fp): raise ApplyError(f"{host['owner_scene']} not found")
    line = connection_line(block, host)
    new_text, action, line_no = edit_scene_connection(open(fp, "rb").read().decode("utf-8"), line)
    bak = f"{fp}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(fp, bak)
    tmp = fp + ".cascade_tmp"
    open(tmp, "wb").write(new_text.encode("utf-8")); os.replace(tmp, fp)
    ok, got, msg = reload_check_connection(godot, project, host, block)
    result = {"file": host["owner_scene"], "backup": bak, "action": action, "line": line_no, "before": None, "after": line, "reloaded_value": got, "verified": ok}
    if not ok:
        shutil.copy2(bak, fp); result["rolled_back"] = True; result["problem"] = msg
    return result

def apply_scene(godot, project, host, block):
    if block["op"] == "connect_signal":
        res = apply_scene_connection(godot, project, host, block); res["what"] = f"{host['name']}.{block['signal']} -> {block['method']}"
    else:
        res = apply_scene_property(godot, project, host, block); res["what"] = f"{host['name']}.{block['property']}"
    return res

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
CLOCK_EDITS = [
 (MARK, NEW_SECTION + MARK),
 # slot 1: required slots come from the op; property checks stay with modify_property
 (r'''    s, bad = spec["slots"], []
    for k in s:
        if k not in block: bad.append(f"block.{k} is missing")
    bad += [f"block.{k} is not a slot in the spec" for k in block if k not in s]
''',
  r'''    s, bad = spec["slots"], []
    want = spec["ops"].get(block.get("op"), {}).get("slots") or list(s)
    for k in want:
        if k not in block: bad.append(f"block.{k} is missing")
    bad += [f"block.{k} is not a slot of op {block.get('op')}" for k in block if k not in want]
'''),
 (r'''    if block["type"] not in s["type"]["enum"]: bad.append(f"block.type '{block['type']}' not allowed")
''',
  r'''    if block["op"] == "modify_property" and block["type"] not in s["type"]["enum"]: bad.append(f"block.type '{block['type']}' not allowed")
'''),
 (r'''    if block["property"] not in host["props"]: bad.append(f"block.property '{block['property']}' is not in the record's props")
''',
  r'''    if block["op"] != "modify_property": bad += connect_checks(block, host, spec, project)
    elif block["property"] not in host["props"]: bad.append(f"block.property '{block['property']}' is not in the record's props")
'''),
 # the worked example is a modify_property thing
 (r'''    t = block["type"]
    if t not in EXAMPLE_VALUES: return None
''',
  r'''    if block.get("op") != "modify_property": return None
    t = block["type"]
    if t not in EXAMPLE_VALUES: return None
'''),
 # what the mule may read
 (r'''SLIM_VIEW = {"modify_property": {"task": ["frame_id", "op", "target", "property", "type", "value"], "rag": ["node", "name"]}}
''',
  r'''SLIM_VIEW = {"modify_property": {"task": ["frame_id", "op", "target", "property", "type", "value"], "rag": ["node", "name"]},
             "connect_signal": {"task": ["frame_id", "op", "target", "signal", "method"], "rag": ["node", "name"]}}
'''),
 (r'''    rag["props"] = {block["property"]: host["props"][block["property"]]}
''',
  r'''    if "property" in block: rag["props"] = {block["property"]: host["props"][block["property"]]}
'''),
 (r'''ULTRA_VIEW = {"modify_property": ["op", "property", "type", "value"]}
''',
  r'''ULTRA_VIEW = {"modify_property": ["op", "property", "type", "value"], "connect_signal": ["op", "signal", "method"]}
'''),
 # GI-3: connect_signal must be exactly <signal>.connect(<method>)
 (r'''    m = re.match(rf"^{re.escape(block['property'])}\s*=\s*(.+)$", stmt)
    if not m: return f"type_match: stmt must be one assignment to {block['property']}"
    err = expr_error(block["type"], m.group(1).strip(), block["value"])
    if err: return "type_match: " + err
''',
  r'''    if block["op"] == "connect_signal":
        want = f"{block['signal']}.connect({block['method']})"
        if stmt != want: return f"type_match: stmt must be exactly {want}"
    else:
        m = re.match(rf"^{re.escape(block['property'])}\s*=\s*(.+)$", stmt)
        if not m: return f"type_match: stmt must be one assignment to {block['property']}"
        err = expr_error(block["type"], m.group(1).strip(), block["value"])
        if err: return "type_match: " + err
'''),
 # main
 (r'''{block.get('target')}.{block.get('property')}")
''',
  r'''{block.get('target')}.{block.get('property') or block.get('signal')}")
'''),
 (r'''    log(f"SLOT_1 FETCH    ok   {host['addr']} {host['name']} {host['uid']}"); log("GI_1            ok")
''',
  r'''    log(f"SLOT_1 FETCH    ok   {host['addr']} {host['name']} {host['uid']}"); log("GI_1            ok")
    if block["op"] == "connect_signal" and connection_exists(project, host, block):
        log("RESULT: NOOP - this connection is already in the scene, so there is nothing to do"); finish(0, status="NOOP_ALREADY_CONNECTED", uid=host["uid"], **op_fields(block))
'''),
 (r'''    ctor = {"Vector2": "Vector2(", "Vector3": "Vector3(", "Color": "Color("}.get(block["type"], "")
    stmt_head = (f"{block['property']} = {ctor}" if ctor else f"{block['property']} =") if block["op"] == "modify_property" else ""
''',
  r'''    ctor = {"Vector2": "Vector2(", "Vector3": "Vector3(", "Color": "Color("}.get(block.get("type"), "")
    if block["op"] == "modify_property": stmt_head = f"{block['property']} = {ctor}" if ctor else f"{block['property']} ="
    elif block["op"] == "connect_signal": stmt_head = f"{block['signal']}.connect("
    else: stmt_head = ""
'''),
 ("res = apply_scene_property(a.godot, project, host, block)", "res = apply_scene(a.godot, project, host, block)"),
 (r'''log(f"SLOT_4 RELOAD   ok   {host['name']}.{block['property']} = {res['reloaded_value']}")''', r'''log(f"SLOT_4 RELOAD   ok   {res['what']} = {res['reloaded_value']}")'''),
 ('uid=host["uid"], property=block["property"], type=block["type"], value=block["value"], attempts=attempts', 'uid=host["uid"], **op_fields(block), attempts=attempts'),
]
for old, new in CLOCK_EDITS:
    if clock.count(old) != 1: sys.exit("Could not find the expected text exactly once in cascade_clock.py, so I changed nothing:\n" + old[:100])
    clock = clock.replace(old, new, 1)
compile(clock, "cascade_clock.py", "exec")

# ------------------------------------------------------------------ write everything (backups first)
for n, text in (("cascade_clock.py", clock), ("registry_builder.py", builder)):
    open(paths[n] + ".before-connect", "w", encoding="utf-8").write(open(paths[n], encoding="utf-8").read())
    open(paths[n], "w", encoding="utf-8").write(text)
for n, obj in (("task_block_spec.json", tspec), ("rag_record_spec.json", rspec)):
    open(paths[n] + ".before-connect", "w", encoding="utf-8").write(open(paths[n], encoding="utf-8").read())
    json.dump(obj, open(paths[n], "w", encoding="utf-8"), indent=2)
print("patched: connect_signal is built in (clock, registry builder and both specs; backups: *.before-connect). Now re-run registry_builder.py")
PEOF
python3 ~/cascade_ws/patch_connect.py ~/cascade_ws

# ---- 2. rebuild the registry: every host now lists its signals and its script's methods
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 registry_builder.py

# ---- 3. the first connect_signal block and a rehearsal answer (what the mule writes after  ready.connect( )
cat << 'EOF' > ~/cascade_ws/block_connect_0001.json
{"frame_id":"0x2000","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"connect_signal","source":"%Player","target":"%Player","signal":"ready","method":"_on_player_ready","ref":"10.1.1.10","exclude":["KinematicBody2D","connect_3arg","untyped_variables","relative_paths_up_tree","cross_subnet_queries"]}
EOF
cat << 'EOF' > ~/cascade_ws/rehearsal_connect_ok.json
["_on_player_ready)\"}"]
EOF

# ---- 4. quick check, no GPU, nothing written to the scene
python3 cascade_clock.py block_connect_0001.json --prefill --script rehearsal_connect_ok.json --no-apply
