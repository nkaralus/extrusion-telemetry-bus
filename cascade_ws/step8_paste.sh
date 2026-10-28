# ---- 0. clear the stale dead letter from the failed connect_signal run
rm -f ~/cascade_ws/dead_letter/0x2000.json

# ---- 1. build add_method into the clock and the task spec (backups: *.before-method)
cat << 'PEOF' > ~/cascade_ws/patch_method.py
import json, os, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1]))
p_clock, p_tspec = os.path.join(ws, "cascade_clock.py"), os.path.join(ws, "task_block_spec.json")
for p in (p_clock, p_tspec):
    if not os.path.exists(p): sys.exit(f"STOPPED: {p} not found")
clock = open(p_clock, encoding="utf-8").read()
tspec = json.load(open(p_tspec, encoding="utf-8"))
if "def add_method_checks" in clock: sys.exit("add_method is already built in.")
if "def canonical_stmt" not in clock: sys.exit("Apply patch_connect.py and patch_selfform.py first.")

# ------------------------------------------------------------------ the spec: five new slots and the op
if "add_method" not in tspec["slots"]["op"]["enum"]: tspec["slots"]["op"]["enum"].append("add_method")
tspec["slots"]["fn"] = {"type": "string", "rx": "^[a-z_][a-z0-9_]{0,39}$"}
tspec["slots"]["args"] = {"type": "array", "items": "string", "max_items": 3, "item_rx": "^[a-z_][a-z0-9_]*: (bool|int|float|String|Vector2|Vector3|Color)$"}
tspec["slots"]["returns"] = {"type": "string", "enum": ["void"]}
tspec["slots"]["body"] = {"type": "string", "enum": ["pass", "print"]}
tspec["slots"]["message"] = {"type": "string", "rx": "^[A-Za-z0-9 _.,:!?-]{0,40}$"}
tspec["ops"]["add_method"] = {
    "status": "locked",
    "slots": ["frame_id", "time_slot", "subnet", "op", "target", "fn", "args", "returns", "body", "message", "ref", "exclude"],
    "v1": "append one function with a one-line body (pass or print) to the host node's script; the harness writes the canonical text, the mule only completes the body line",
}

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- add_method (Step 8): one function appended to the host's script
def signature(block): return f"func {block['fn']}({', '.join(block['args'])}) -> {block['returns']}:"

def body_line(block): return "pass" if block["body"] == "pass" else f'print("{block["message"]}")'

def script_path(project, host): return os.path.join(project, host["script"][len("res://"):])

def detect_indent(raw):
    """The script's own indent: tabs or the first indented line's spaces; 4 spaces if it has none."""
    for l in raw.splitlines():
        if l.strip() and l[:1] in (" ", "\t"):
            lead = l[:len(l) - len(l.lstrip())]
            return "\t" if "\t" in lead else " " * len(lead)
    return "    "

def canonical_function(block, indent): return f"{signature(block)}\n{indent}{body_line(block)}\n"

def drop_class_name(raw): return "\n".join(l for l in raw.split("\n") if not l.startswith("class_name "))

def append_function_text(raw, func_text):
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw.rstrip("\r\n") + nl + nl + func_text.replace("\n", nl)

def existing_function(raw, fn):
    """The function's lines (trimmed) if the script defines it at the top level, else None."""
    lines = raw.splitlines()
    for i, l in enumerate(lines):
        if re.match(rf"^func\s+{re.escape(fn)}\s*\(", l):
            body = [l.strip()]
            for m in lines[i + 1:]:
                if m.strip() and m[:1] not in (" ", "\t"): break
                body.append(m.strip())
            while body and not body[-1]: body.pop()
            return body
    return None

def _norm(lines): return [x.replace("'", '"') for x in lines]

def function_matches(project, host, block):
    fp = script_path(project, host)
    if not os.path.exists(fp): return False
    have = existing_function(open(fp, encoding="utf-8").read(), block["fn"])
    return have is not None and _norm(have) == _norm([signature(block), body_line(block)])

def add_method_checks(block, host, spec, project):
    bad, s = [], spec["slots"]
    for k in ("fn", "message"):
        if not re.match(s[k]["rx"], str(block[k])): bad.append(f"block.{k} '{block[k]}' does not match the spec")
    if block["returns"] not in s["returns"]["enum"]: bad.append(f"block.returns '{block['returns']}' not allowed (v1: void)")
    if block["body"] not in s["body"]["enum"]: bad.append(f"block.body '{block['body']}' not allowed (pass or print)")
    args = block["args"]
    if not isinstance(args, list) or len(args) > s["args"]["max_items"] or any(not isinstance(x, str) or not re.match(s["args"]["item_rx"], x) for x in args):
        bad.append("block.args must be a list of at most 3 'name: Type' strings (Type: bool, int, float, String, Vector2, Vector3, Color)")
    elif len({x.split(":")[0] for x in args}) != len(args): bad.append("block.args repeats an argument name")
    if block["body"] == "pass" and block["message"] != "": bad.append("block.message must be empty when body is pass")
    if block["body"] == "print" and block["message"] == "": bad.append("block.message must not be empty when body is print")
    if bad: return bad
    if not host.get("script"): return [f"GI_1 script: {host['name']} has no script, so it cannot get a method (adding a script is an open slot)"]
    fp = script_path(project, host)
    if not os.path.exists(fp): return [f"GI_1 script: {host['script']} not found"]
    for k in ("signals", "methods", "props"):
        if k not in host: bad.append(f"GI_1 registry has no '{k}' field; update and re-run registry_builder.py")
    if bad: return bad
    if block["fn"] in host["signals"]: bad.append(f"GI_1 fn: '{block['fn']}' is a signal of {host['name']}")
    if block["fn"] in host["props"]: bad.append(f"GI_1 fn: '{block['fn']}' is a property of {host['name']} (Godot would accept it, and it would shadow the property)")
    have = existing_function(open(fp, encoding="utf-8").read(), block["fn"])
    if have is not None and _norm(have) != _norm([signature(block), body_line(block)]):
        bad.append(f"GI_1 fn_exists: {block['fn']} is already defined in {host['script']} with a different body; editing a function is an open slot")
    return bad

def add_method_stmt_error(stmt, block):
    lines = stmt.split("\n")
    if len(lines) != 2: return f"type_match: stmt must be exactly two lines (the signature and one indented body line), got {len(lines)}"
    if lines[0] != signature(block): return f"type_match: line 1 must be {signature(block)}"
    if lines[1][:1] not in (" ", "\t"): return "type_match: the body line must be indented"
    want = body_line(block)
    if lines[1].strip() not in (want, want.replace('"', "'")): return f"type_match: the body must be {want} (single quotes are also accepted)"
    return None

def judge_function(godot, project, host, block):
    """Slot 4 for add_method: the canonical function appended to a temporary copy of the whole script, then godot --check-only."""
    raw = open(script_path(project, host), "rb").read().decode("utf-8")
    text = append_function_text(drop_class_name(raw), canonical_function(block, detect_indent(raw)))
    path = os.path.join(tempfile.mkdtemp(prefix="cascade_judge_"), "frame.gd")
    open(path, "w", encoding="utf-8", newline="").write(text)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--check-only", "--script", path], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return False, "judge timeout (60 s)"
    if r.returncode == 0: return True, ""
    lines = strip_ansi(r.stdout + r.stderr).splitlines(); errs = []
    for i, l in enumerate(lines):
        if l.startswith("SCRIPT ERROR:"):
            at = lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1].strip().startswith("at:") else ""
            errs.append((l[len("SCRIPT ERROR:"):].strip() + " " + at.replace(path, "frame.gd")).strip())
    return False, " | ".join(errs[:3]) or "\n".join(lines[-4:])

def godot_errors(text):
    return " | ".join(l[len("SCRIPT ERROR:"):].strip() for l in strip_ansi(text).splitlines() if l.startswith("SCRIPT ERROR:"))[:300]

def judge_op(godot, project, host, block, stmt):
    if block["op"] == "add_method": return judge_function(godot, project, host, block)
    return judge(godot, project, host, canonical_stmt(stmt, block))

RELOAD_FN_HELPER = r"""extends SceneTree

func _init() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var res := {"found": false, "can_instantiate": false, "has_method": false, "arg_count": -1}
    var packed: PackedScene = load(a[1])
    if packed != null:
        var root: Node = packed.instantiate()
        var n: Node = root.get_node_or_null("%" + a[2])
        if n != null:
            res["found"] = true
            var s: Script = n.get_script()
            res["can_instantiate"] = s != null and s.can_instantiate()
            res["has_method"] = n.has_method(a[3])
            if s != null:
                for m in s.get_script_method_list():
                    if str(m["name"]) == a[3]:
                        res["arg_count"] = m["args"].size()
        root.free()
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(res))
    f.close()
    quit()
"""

def reload_check_function(godot, project, host, block):
    tmp = tempfile.mkdtemp(prefix="cascade_reload_")
    helper, out = os.path.join(tmp, "reload.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(RELOAD_FN_HELPER)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), block["fn"]],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return False, None, "reload timeout (60 s)"
    if not os.path.exists(out): return False, None, "Godot could not reload the scene:\n" + strip_ansi(r.stdout + r.stderr)[-400:]
    res = json.load(open(out))
    if not res.get("found"): return False, res, f"{host['name']} was not found in {host['owner_scene']} after the edit"
    if not res["can_instantiate"]: return False, res, f"the script no longer compiles ({host['script']}): " + (godot_errors(r.stdout + r.stderr) or strip_ansi(r.stdout + r.stderr)[-300:].strip())
    if not res["has_method"]: return False, res, f"{host['name']} does not have {block['fn']}() after the edit"
    if res["arg_count"] != len(block["args"]): return False, res, f"{block['fn']}() has {res['arg_count']} arguments, expected {len(block['args'])}"
    return True, res, ""

def apply_script_function(godot, project, host, block):
    """Append the canonical function to the host's script: .bak backup first, headless reload check after, rollback if it fails."""
    fp = script_path(project, host)
    if not os.path.exists(fp): raise ApplyError(f"{host['script']} not found")
    raw = open(fp, "rb").read().decode("utf-8")
    func_text = canonical_function(block, detect_indent(raw))
    new_text = append_function_text(raw, func_text)
    line_no = max(i for i, l in enumerate(new_text.splitlines()) if l.startswith(f"func {block['fn']}(")) + 1
    bak = f"{fp}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(fp, bak)
    tmp = fp + ".cascade_tmp"
    open(tmp, "wb").write(new_text.encode("utf-8")); os.replace(tmp, fp)
    ok, got, msg = reload_check_function(godot, project, host, block)
    result = {"file": host["script"], "backup": bak, "action": "appended", "line": line_no, "before": None,
              "after": f"{signature(block)}  {body_line(block)}", "reloaded_value": got, "verified": ok}
    if not ok:
        shutil.copy2(bak, fp); result["rolled_back"] = True; result["problem"] = msg
    return result

def refresh_registry(ws, godot, host, block):
    """After a function was added the registry is stale: re-run the builder, then confirm the new function is listed."""
    try:
        r = subprocess.run([sys.executable, os.path.join(ws, "registry_builder.py"), "--ws", ws, "--godot", godot], capture_output=True, text=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    if r.returncode != 0: return {"ok": False, "detail": strip_ansi(r.stdout + r.stderr)[-300:].strip()}
    h = json.load(open(os.path.join(ws, "state_registry.json")))["hosts"].get(host["addr"]) or {}
    return {"ok": True, "listed": block["fn"] in h.get("methods", []), "methods": len(h.get("methods", []))}

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
CLOCK_EDITS = [
 (MARK, NEW_SECTION + MARK),
 # GI-1: dispatch by op
 (r'''    if block["op"] != "modify_property": bad += connect_checks(block, host, spec, project)
''',
  r'''    if block["op"] == "connect_signal": bad += connect_checks(block, host, spec, project)
    elif block["op"] == "add_method": bad += add_method_checks(block, host, spec, project)
'''),
 # GI-3: add_method stmt has two lines
 (r'''    if "\n" in stmt: return "slot_enum_match: stmt must be exactly one line"
''',
  r'''    if "\n" in stmt and block["op"] != "add_method": return "slot_enum_match: stmt must be exactly one line"
'''),
 (r'''    else:
        m = re.match(rf"^{re.escape(block['property'])}\s*=\s*(.+)$", stmt)
''',
  r'''    elif block["op"] == "add_method":
        err = add_method_stmt_error(stmt, block)
        if err: return err
    else:
        m = re.match(rf"^{re.escape(block['property'])}\s*=\s*(.+)$", stmt)
'''),
 # Slot 4
 ("ok, jerr = judge(a.godot, project, host, canonical_stmt(stmt, block))", "ok, jerr = judge_op(a.godot, project, host, block, stmt)"),
 # what the ledger and the log say
 (r'''    return {"property": block["property"], "type": block["type"], "value": block["value"]}
''',
  r'''    if block["op"] == "add_method": return {"fn": block["fn"], "args": block["args"], "body": block["body"], "message": block["message"]}
    return {"property": block["property"], "type": block["type"], "value": block["value"]}
'''),
 (r'''    else:
        res = apply_scene_property(godot, project, host, block); res["what"] = f"{host['name']}.{block['property']}"
''',
  r'''    elif block["op"] == "add_method":
        res = apply_script_function(godot, project, host, block); res["what"] = f"{host['name']}.{block['fn']}()"
    else:
        res = apply_scene_property(godot, project, host, block); res["what"] = f"{host['name']}.{block['property']}"
'''),
 ("{block.get('target')}.{block.get('property') or block.get('signal')}", "{block.get('target')}.{block.get('property') or block.get('signal') or block.get('fn')}"),
 # nothing to do when the same function is already there
 (r'''        log("RESULT: NOOP - this connection is already in the scene, so there is nothing to do"); finish(0, status="NOOP_ALREADY_CONNECTED", uid=host["uid"], **op_fields(block))
''',
  r'''        log("RESULT: NOOP - this connection is already in the scene, so there is nothing to do"); finish(0, status="NOOP_ALREADY_CONNECTED", uid=host["uid"], **op_fields(block))
    if block["op"] == "add_method" and function_matches(project, host, block):
        log(f"RESULT: NOOP - {block['fn']}() is already in {host['script']} exactly as asked, so there is nothing to do"); finish(0, status="NOOP_ALREADY_DEFINED", uid=host["uid"], **op_fields(block))
'''),
 # the prefill lays down the signature
 (r'''    elif block["op"] == "connect_signal": stmt_head = f"{block['signal']}.connect("
''',
  r'''    elif block["op"] == "connect_signal": stmt_head = f"{block['signal']}.connect("
    elif block["op"] == "add_method": stmt_head = signature(block) + "\\n"
'''),
 # what the mule may read
 (r'''ULTRA_VIEW = {"modify_property": ["op", "property", "type", "value"], "connect_signal": ["op", "signal", "method"]}
''',
  r'''ULTRA_VIEW = {"modify_property": ["op", "property", "type", "value"], "connect_signal": ["op", "signal", "method"], "add_method": ["op", "fn", "body", "message"]}
'''),
 (r'''             "connect_signal": {"task": ["frame_id", "op", "target", "signal", "method"], "rag": ["node", "name"]}}
''',
  r'''             "connect_signal": {"task": ["frame_id", "op", "target", "signal", "method"], "rag": ["node", "name"]},
             "add_method": {"task": ["frame_id", "op", "target", "fn", "args", "body", "message"], "rag": ["node", "name"]}}
'''),
 # after a verified apply: refresh the registry
 (r'''                            status, extra = "APPLIED", {"apply": res}
''',
  r'''                            status, extra = "APPLIED", {"apply": res}
                            if block["op"] == "add_method":
                                rr = refresh_registry(ws, a.godot, host, block); extra["registry_refresh"] = rr
                                log(f"REGISTRY       refreshed: {block['fn']} is listed ({rr['methods']} methods now)" if rr.get("ok") and rr.get("listed") else f"REGISTRY       NOT refreshed - run registry_builder.py by hand ({rr.get('detail') or 'function not listed'})")
'''),
]
for old, new in CLOCK_EDITS:
    if clock.count(old) != 1: sys.exit("Could not find the expected text exactly once in cascade_clock.py, so I changed nothing:\n" + old[:100])
    clock = clock.replace(old, new, 1)
compile(clock, "cascade_clock.py", "exec")

# ------------------------------------------------------------------ write (backups first)
open(p_clock + ".before-method", "w", encoding="utf-8").write(open(p_clock, encoding="utf-8").read())
open(p_clock, "w", encoding="utf-8").write(clock)
open(p_tspec + ".before-method", "w", encoding="utf-8").write(open(p_tspec, encoding="utf-8").read())
json.dump(tspec, open(p_tspec, "w", encoding="utf-8"), indent=2)
print("patched: add_method is built in (clock and task_block_spec.json; backups: *.before-method)")
PEOF
python3 ~/cascade_ws/patch_method.py ~/cascade_ws

# ---- 2. the two demo blocks and a rehearsal answer (what the mule writes after the laid-down signature)
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
cat << 'EOF' > block_method_0001.json
{"frame_id":"0x3000","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"add_method","target":"%Player","fn":"_on_player_tree_exiting","args":[],"returns":"void","body":"print","message":"bye","ref":"10.1.1.10","exclude":["KinematicBody2D","untyped_variables","yield","relative_paths_up_tree","cross_subnet_queries"]}
EOF
cat << 'EOF' > block_connect_0002.json
{"frame_id":"0x3001","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"connect_signal","source":"%Player","target":"%Player","signal":"tree_exiting","method":"_on_player_tree_exiting","ref":"10.1.1.10","exclude":["KinematicBody2D","connect_3arg","untyped_variables","relative_paths_up_tree","cross_subnet_queries"]}
EOF
cat << 'EOF' > rehearsal_method_ok.json
["    print('bye')\"}"]
EOF

# ---- 3. quick check, no GPU, nothing written
python3 cascade_clock.py block_method_0001.json --prefill --script rehearsal_method_ok.json --no-apply
