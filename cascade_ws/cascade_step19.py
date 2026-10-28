"""Step 19: set_physics_body. The harness renders _physics_process from a fixed template (no mule call) and first
makes sure project.godot has the four input actions. Both edits take a .bak and roll back on any failed check.
cascade_clock.py binds this module with bind(); patch_step19.py wires it in."""
import json, os, re, shutil, subprocess, tempfile, time

c = None
def bind(mod):
    global c; c = mod

OP, FN = "set_physics_body", "_physics_process"
ROLES = ("left", "right", "up", "down")
ACTION_RX = r"^[a-z][a-z_]{0,31}$"
KEYS = {"left": (65, 4194319), "right": (68, 4194321), "up": (87, 4194320), "down": (83, 4194322)}   # A/Left, D/Right, W/Up, S/Down
FIRST_RX = re.compile(r'^var dir: Vector2 = Input\.get_vector\("[a-z_]+", "[a-z_]+", "[a-z_]+", "[a-z_]+"\)$')
SECTION_RX = re.compile(r"^\[[^\]\s][^\]]*\]\s*$")

def read(p): return open(p, "rb").read().decode("utf-8")
def write_atomic(p, text):
    tmp = p + ".cascade_tmp"; open(tmp, "wb").write(text.encode("utf-8")); os.replace(tmp, p)
def backup(p, block):
    bak = f"{p}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"; shutil.copy2(p, bak); return bak
def names(block): return [(block[r], r) for r in ROLES]

# ---------------------------------------------------------------- renderer
def body_lines(block, ind):
    a = [block[r] for r in ROLES]
    return [f'{ind}var dir: Vector2 = Input.get_vector("{a[0]}", "{a[1]}", "{a[2]}", "{a[3]}")', f"{ind}velocity = dir * speed", f"{ind}move_and_slide()"]

def body_state(raw):
    """('absent'|'plain'|'render'|'custom', signature line). Only plain or an earlier render may be replaced."""
    sp = c.function_span(raw, FN)
    if sp is None: return "absent", None
    seg = raw[sp[0]:sp[1]].splitlines(); body = [l.strip() for l in seg[1:] if l.strip()]
    if body in (["move_and_slide()"], ["pass"]): return "plain", seg[0]
    if len(body) == 3 and FIRST_RX.match(body[0]) and body[1] == "velocity = dir * speed" and body[2] == "move_and_slide()": return "render", seg[0]
    return "custom", seg[0]

def new_script_text(raw, block):
    nl = "\r\n" if "\r\n" in raw else "\n"; state, head = body_state(raw)
    func = ((head or f"func {FN}(_delta: float) -> void:") + "\n" + "\n".join(body_lines(block, c.detect_indent(raw))) + "\n").replace("\n", nl)
    return c.append_function_text(raw, func) if state == "absent" else c.replace_function(raw, FN, func)

# ---------------------------------------------------------------- project.godot [input]
def key_event(code):
    return ('Object(InputEventKey,"resource_local_to_scene":false,"resource_name":"","device":-1,"window_id":0,"alt_pressed":false,"shift_pressed":false,"ctrl_pressed":false,'
            '"meta_pressed":false,"pressed":false,"keycode":0,"physical_keycode":%d,"key_label":0,"unicode":%d,"echo":false,"script":null)' % (code, code + 32 if 65 <= code <= 90 else 0))

def action_text(name, role): return [f"{name}={{", '"deadzone": 0.5,', '"events": [' + ", ".join(key_event(k) for k in KEYS[role]), "]", "}"]

def input_span(lines):
    s = next((i for i, l in enumerate(lines) if l.strip() == "[input]"), None)
    if s is None: return None
    return s, next((i for i in range(s + 1, len(lines)) if SECTION_RX.match(lines[i])), len(lines))

def missing_actions(raw, block):
    lines = raw.splitlines(); sp = input_span(lines); have = set()
    if sp:
        for l in lines[sp[0] + 1:sp[1]]:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=\{", l)
            if m: have.add(m.group(1))
    return [(n, r) for n, r in names(block) if n not in have]

def add_actions_text(raw, miss):
    nl = "\r\n" if "\r\n" in raw else "\n"; lines = raw.split(nl); add = []
    for n, r in miss: add += action_text(n, r)
    sp = input_span(lines)
    if sp is None:
        while lines and not lines[-1].strip(): lines.pop()
        lines += ["", "[input]", ""] + add + [""]
    else:
        e = sp[1]
        while e > sp[0] + 1 and not lines[e - 1].strip(): e -= 1
        lines[e:e] = ([""] if e == sp[0] + 1 else []) + add
    return nl.join(lines)

INPUT_CHECK_GD = r"""extends SceneTree
func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var res: Dictionary = {}
    for i in range(1, a.size()):
        var n: StringName = StringName(a[i])
        res[a[i]] = InputMap.action_get_events(n).size() if InputMap.has_action(n) else -1
    var f: FileAccess = FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(res))
    f.close()
    quit()
"""

def run_godot_script(godot, project, gd_text_or_path, args, pre=(), timeout=60):
    tmp = tempfile.mkdtemp(prefix="cascade_s19_"); out = os.path.join(tmp, "out.json")
    gd = gd_text_or_path
    if "\n" in gd_text_or_path: gd = os.path.join(tmp, "check.gd"); open(gd, "w").write(gd_text_or_path)
    try: r = subprocess.run([godot, "--headless", "--path", project, *pre, "--script", gd, "--", out, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError: c.die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired: return None, f"timeout ({timeout} s)"
    if not os.path.exists(out): return None, c.strip_ansi(r.stdout + r.stderr)[-300:].strip()
    return json.load(open(out)), ""

def ensure_input(godot, project, block):
    """Add the missing actions to project.godot (.bak first, headless load check after). {'changed','added','backup','file','problem'?}; restored on a failed check."""
    fp = os.path.join(project, "project.godot")
    if not os.path.exists(fp): raise c.ApplyError("project.godot not found")
    raw = read(fp); miss = missing_actions(raw, block); bak = None
    if miss: bak = backup(fp, block); write_atomic(fp, add_actions_text(raw, miss))
    res, err = run_godot_script(godot, project, INPUT_CHECK_GD, [n for n, _ in names(block)])
    new = [n for n, _ in miss]
    bad = [n for n, _ in names(block) if res is None or res.get(n, -1) < (1 if n in new else 0)]
    out = {"changed": bool(miss), "added": new, "backup": bak, "file": "project.godot"}
    if res is None or bad:
        if bak: shutil.copy2(bak, fp); out["rolled_back"] = True
        out["problem"] = err or f"input actions did not load as asked: {bad}"
    return out

# ---------------------------------------------------------------- GI_1 / GI_3
def slot1(prev, block, reg, spec, project):
    if block.get("op") != OP: return prev(block, reg, spec, project)
    s, ops, bad = spec["slots"], spec["ops"].get(OP) or {}, []
    if ops.get("status") != "locked": return None, [f"the spec is behind the clock: it has no locked '{OP}' op. Run: python3 patch_step19.py"]
    bad += [f"block.{k} is missing" for k in ops["slots"] if k not in block] + [f"block.{k} is not a slot of op {OP}" for k in block if k not in ops["slots"]]
    if bad: return None, bad
    for k in ("frame_id", "subnet", "target", "ref") + ROLES:
        if not re.match(s[k]["rx"], str(block[k])): bad.append(f"block.{k} '{block[k]}' does not match the spec")
    if block["time_slot"] not in s["time_slot"]["enum"]: bad.append(f"block.time_slot '{block['time_slot']}' not allowed")
    if block["template"] not in s["template"]["enum"]: bad.append(f"block.template '{block['template']}' not allowed (one of {', '.join(s['template']['enum'])})")
    if len({block[r] for r in ROLES}) < 4: bad.append("block.left/right/up/down must name four different actions")
    if not isinstance(block["exclude"], list) or not block["exclude"] or any(e not in s["exclude"]["enum"] for e in block["exclude"]): bad.append("block.exclude must be a non-empty list of names from the spec")
    host = reg.get("hosts", {}).get(block["ref"])
    if host is None: return None, bad + [f"GI_1 addr_in_registry: {block['ref']} is not in the registry"]
    if not re.match(r"^uid://[a-z0-9]+$", host.get("uid", "")): bad.append("GI_1 uid_in_registry: host has no valid uid")
    elif c.header_uid(project, host["scene"]) != host["uid"]: bad.append(f"GI_1 uid_in_registry: registry is stale ({host['scene']} has a different uid); run registry_builder.py")
    if block["subnet"] != host["subnet"]: bad.append(f"GI_1 subnet_boundary: block subnet {block['subnet']} but {block['ref']} is in {host['subnet']}")
    if block["target"] != host["name"]: bad.append(f"GI_1 addr_in_registry: {block['ref']} is {host['name']}, block says {block['target']}")
    if bad: return host, bad
    if host["node"] != "CharacterBody2D": bad.append(f"GI_1 host: {host['name']} is a {host['node']}; move_and_slide needs a CharacterBody2D")
    for p, t in (("speed", "float"), ("velocity", "Vector2")):
        if host.get("props", {}).get(p) != t: bad.append(f"GI_1 host: {host['name']} has no registered {p}: {t}")
    sp = c.script_path(project, host) if host.get("script") else None
    if not sp or not os.path.exists(sp): bad.append("GI_1 host: the host has no script file")
    if not os.path.exists(os.path.join(project, "project.godot")): bad.append("GI_1 project: project.godot not found")
    if bad: return host, bad
    state, _ = body_state(read(sp))
    if state == "custom": bad.append(f"GI_1 body: {FN}() holds other code; only move_and_slide(), pass or an earlier render of this template may be replaced")
    text = "\n".join(body_lines(block, ""))
    for e in block["exclude"]:
        chk = spec.get("exclude_checks", {}).get(e, {})
        if chk.get("kind") == "regex" and re.search(chk["rx"], text): bad.append(f"GI_3 exclude_hit: the rendered body hits '{e}'")
    return host, bad

def matches(project, host, block):
    sp = c.script_path(project, host); raw = read(sp)
    state, _ = body_state(raw)
    return state != "custom" and new_script_text(raw, block) == raw and not missing_actions(read(os.path.join(project, "project.godot")), block)

# ---------------------------------------------------------------- SLOT_3/4 apply (both files, one rollback)
def check_script(godot, project, host):
    try: r = subprocess.run([godot, "--headless", "--path", project, "--check-only", "--script", host["script"]], capture_output=True, text=True, timeout=60)
    except FileNotFoundError: c.die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired: return "check timeout (60 s)"
    return "" if r.returncode == 0 else (c.godot_errors(r.stdout + r.stderr) or c.strip_ansi(r.stdout + r.stderr)[-300:].strip())

def undo(project, host, res):
    if res.get("backup"): shutil.copy2(res["backup"], c.script_path(project, host))
    if res.get("input", {}).get("backup"): shutil.copy2(res["input"]["backup"], os.path.join(project, "project.godot"))

def apply_all(godot, project, host, block):
    fp = c.script_path(project, host)
    if not os.path.exists(fp): raise c.ApplyError(f"{host['script']} not found")
    inp = ensure_input(godot, project, block)
    res = {"file": host["script"], "input": inp, "backup": None, "action": "replaced", "verified": False}
    if inp.get("problem"): res["problem"] = "project.godot: " + inp["problem"]; return res
    raw = read(fp); state, _ = body_state(raw); new = new_script_text(raw, block)
    res.update(action="inserted" if state == "absent" else "replaced", before=state, after=f"{FN}: " + " / ".join(l.strip() for l in body_lines(block, "")))
    if new != raw:
        res["backup"] = backup(fp, block); write_atomic(fp, new)
    prob = check_script(godot, project, host)
    if prob: res["problem"] = "parse check: " + prob
    else:
        sc = os.path.join(project, host["owner_scene"][len("res://"):])
        r, err = c.run_node_reload(godot, project, host["owner_scene"], host["name"].lstrip("%"), c.scene_connections(read(sc)))
        if r is None: res["problem"] = "scene reload: " + err
        elif not (r["loaded"] and r["found"] and r["class"] == host["node"]): res["problem"] = f"scene reload: {host['name']} did not load as a {host['node']}"
        elif r["bad_connections"]: res["problem"] = f"scene reload: {len(r['bad_connections'])} of {r['n_connections']} connections are no longer wired"
        else: res["reloaded_value"] = f"{r['n_connections']} scene connections still wired"
    res["verified"] = "problem" not in res
    if not res["verified"]: undo(project, host, res); res["rolled_back"] = True
    return res

# ---------------------------------------------------------------- SLOT_5 live check
def verify(godot, project, ws, host, block):
    gd = os.path.join(ws, "live_move.gd")
    if not os.path.exists(gd): return {"verdict": "FAIL", "summary": f"{gd} is missing", "warnings": []}
    r, err = run_godot_script(godot, project, gd, [host["owner_scene"], host["name"].lstrip("%")] + [block[k] for k in ROLES], pre=("--fixed-fps", "60"), timeout=60)
    if r is None: return {"verdict": "FAIL", "summary": "the live check could not run: " + err, "warnings": []}
    bad = [f"{x['name']}: {x['detail']}" for x in r["checks"] if not x["ok"]]
    if r["verdict"] != "PASS": return {"verdict": "FAIL", "summary": "; ".join(bad) or r["note"] or "live check failed", "warnings": [], "checks": r["checks"]}
    return {"verdict": "PASS", "summary": f"{host['name']} moves from input ({len(r['checks'])} checks): idle v=0, right v=(speed,0), diagonal |v|=speed, release v=0", "warnings": [], "checks": r["checks"]}

# ---------------------------------------------------------------- the frame
def run_frame(a, ws, project, host, block, row, finish):
    log = c.log; fields = {"template": block["template"], **{r: block[r] for r in ROLES}}
    row.update(prefill=False, slim=False, ultra=False, temps=[], example=False, statements=0, body_mode="physics")
    log(f"PHYSICS         {block['template']} on {host['name']}: the harness renders {FN}(); no mule call; actions " + ", ".join(block[r] for r in ROLES))
    if a.verify_only:
        vr = verify(a.godot, project, ws, host, block); log("SLOT_5 VERIFY   " + c.verify_line(vr))
        log(f"RESULT: VERIFY {vr['verdict']} (verify only, nothing was written)"); finish(5 if vr["verdict"] == "FAIL" else 0, status="VERIFY_" + vr["verdict"], verify=vr, uid=host["uid"], **fields)
    if matches(project, host, block):
        log(f"RESULT: NOOP - {FN}() and the input actions are already exactly as asked"); finish(0, status="NOOP_ALREADY_DEFINED", uid=host["uid"], **fields)
    log(f"SLOT_2 ROUTE    ok   op={OP}  (no mule call, GI_2 not needed)")
    log("SLOT_3 EXECUTE  " + " \\n ".join(l.strip() for l in body_lines(block, "")))
    log("SLOT_4 JUDGE    ACK  rendered by the harness; the parse check runs on the written script")
    status, extra, code = "ACK", {}, 0
    if not a.no_apply:
        try: res = apply_all(a.godot, project, host, block)
        except c.ApplyError as e: log(f"SLOT_4 APPLY    NOT WRITTEN  {e}"); status, extra, code = "ACK_NOT_APPLIED", {"apply_error": str(e)}, 4
        else:
            i = res["input"]
            if i.get("changed") and not i.get("rolled_back"): log(f"SLOT_3 INPUT    project.godot: added {', '.join(i['added'])}  backup: {os.path.relpath(i['backup'], ws)}")
            if res["verified"]:
                log(f"SLOT_4 APPLY    {res['action']} {FN} in {res['file'][len('res://'):]}" + (f"  backup: {os.path.relpath(res['backup'], ws)}" if res["backup"] else "  (unchanged)"))
                log(f"SLOT_4 RELOAD   ok   {res['reloaded_value']}"); status, extra = "APPLIED", {"apply": res}
                if not a.no_verify:
                    vr = verify(a.godot, project, ws, host, block); extra["verify"] = vr; log("SLOT_5 VERIFY   " + c.verify_line(vr))
                    if vr["verdict"] == "FAIL": undo(project, host, res); log("SLOT_5 VERIFY   FAILED, ROLLED BACK  both edits were undone"); status, code = "ROLLED_BACK_VERIFY", 4
            else: log(f"SLOT_4 RELOAD   FAILED, ROLLED BACK  {res['problem']}"); status, extra, code = "ROLLED_BACK", {"apply": res}, 4
    log(f"RESULT: {status} (no mule call) -> ledger.jsonl")
    finish(code, status=status, tries=0, stmt=f"{FN} {block['template']}", uid=host["uid"], **fields, **extra)
