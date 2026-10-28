import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "def apply_scene_property" in s: sys.exit("cascade_clock.py already has Step 5.")

APPLY = r'''# ---------------------------------------------------------------- APPLY (Step 5): one-line edit of the owner scene
class ApplyError(Exception): pass

RELOAD_HELPER = r"""extends SceneTree
func _init() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    var res := {"found": false}
    var packed: PackedScene = load(a[1])
    if packed != null:
        var root: Node = packed.instantiate()
        var n: Node = root.get_node_or_null("%" + a[2])
        if n != null:
            res["found"] = true
            var v: Variant = n.get(a[3])
            match typeof(v):
                TYPE_VECTOR2: res["value"] = [v.x, v.y]
                TYPE_VECTOR3: res["value"] = [v.x, v.y, v.z]
                TYPE_COLOR:   res["value"] = [v.r, v.g, v.b, v.a]
                _:            res["value"] = v
        root.free()
    var f := FileAccess.open(a[0], FileAccess.WRITE)
    f.store_string(JSON.stringify(res))
    f.close()
    quit()
"""

def fnum(x): return repr(float(x))

def scene_literal(t, v):
    if t == "bool": return "true" if v else "false"
    if t == "int": return str(int(v))
    if t == "float": return fnum(v)
    if t == "String": return json.dumps(v, ensure_ascii=False)
    if t == "Color" and len(v) == 3: v = list(v) + [1.0]
    return f"{t}(" + ", ".join(fnum(x) for x in v) + ")"

def find_node_block(lines, name, parent):
    hits = [i for i, l in enumerate(lines)
            if l.startswith("[node ") and re.search(r'\bname="' + re.escape(name) + '"', l)
            and re.search(r'\bparent="' + re.escape(parent) + '"', l)]
    if len(hits) != 1:
        raise ApplyError(f"found {len(hits)} node sections for {name} with parent '{parent}' (expected exactly 1)")
    end = next((j for j in range(hits[0] + 1, len(lines)) if lines[j].startswith("[")), len(lines))
    return hits[0], end

def edit_scene_text(raw, tree, prop, literal):
    """returns (new_text, action, line_number, before). Pure text edit, nothing is written here."""
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)
    rel = tree.split("/", 1)[1] if "/" in tree else ""
    if not rel: raise ApplyError(f"tree '{tree}' has no node below the scene root")
    name = rel.split("/")[-1]; parent = "." if "/" not in rel else rel.rsplit("/", 1)[0]
    start, end = find_node_block(lines, name, parent)
    new_line = f"{prop} = {literal}"
    for k in range(start + 1, end):
        if re.match(rf"^{re.escape(prop)}\s*=", lines[k]):
            v = lines[k].split("=", 1)[1]
            if v.count("(") != v.count(")") or v.count("[") != v.count("]") or v.count("{") != v.count("}") or v.count('"') % 2:
                raise ApplyError(f"{prop} is written over several lines in the scene; not editing it automatically")
            before = lines[k]; lines[k] = new_line
            return nl.join(lines), "replaced", k + 1, before
    last = max(j for j in range(start, end) if lines[j].strip())
    lines.insert(last + 1, new_line)
    return nl.join(lines), "inserted", last + 2, None

def reload_check(godot, project, host, block):
    tmp = tempfile.mkdtemp(prefix="cascade_reload_")
    helper, out = os.path.join(tmp, "reload.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(RELOAD_HELPER)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out,
                            host["owner_scene"], host["name"].lstrip("%"), block["property"]],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return False, None, "reload timeout (60 s)"
    if not os.path.exists(out):
        return False, None, "Godot could not reload the scene:\n" + strip_ansi(r.stdout + r.stderr)[-400:]
    res = json.load(open(out))
    if not res.get("found"):
        return False, None, f"{host['name']} was not found in {host['owner_scene']} after the edit"
    got, want, t = res["value"], block["value"], block["type"]
    if t in ("Vector2", "Vector3", "Color"):
        got, want = [float(x) for x in got], [float(x) for x in want]
        if t == "Color": want += [1.0] * (4 - len(want))
        same = len(got) == len(want) and all(abs(a - b) < 1e-6 for a, b in zip(got, want))
    elif t in ("float", "int"):
        same = abs(float(got) - float(want)) < 1e-6
    else:
        same = got == want
    return same, res["value"], "" if same else f"reloaded value {res['value']} does not equal {block['value']}"

def apply_scene_property(godot, project, host, block):
    """Edit one line of the owner scene, with a .bak backup first and a headless reload check after. Rolls back if the check fails."""
    fp = os.path.join(project, host["owner_scene"][len("res://"):])
    if not os.path.exists(fp): raise ApplyError(f"{host['owner_scene']} not found")
    raw = open(fp, "rb").read().decode("utf-8")
    new_text, action, line_no, before = edit_scene_text(raw, host["tree"], block["property"], scene_literal(block["type"], block["value"]))
    bak = f"{fp}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(fp, bak)
    tmp = fp + ".cascade_tmp"
    open(tmp, "wb").write(new_text.encode("utf-8")); os.replace(tmp, fp)
    ok, got, msg = reload_check(godot, project, host, block)
    result = {"file": host["owner_scene"], "backup": bak, "action": action, "line": line_no,
              "before": before, "after": f"{block['property']} = {scene_literal(block['type'], block['value'])}",
              "reloaded_value": got, "verified": ok}
    if not ok:
        shutil.copy2(bak, fp); result["rolled_back"] = True; result["problem"] = msg
    return result
'''

EDITS = [
    ("import argparse, json, os, re, subprocess, sys, tempfile, time\n",
     "import argparse, json, os, re, shutil, subprocess, sys, tempfile, time\n"),
    ("3 NAKs -> dead_letter/<frame_id>.json.   Nothing is written to the Godot project in this step.\n",
     "3 NAKs -> dead_letter/<frame_id>.json.\nAn ACK is then written into the owner scene as ONE line (backup .bak first, headless reload check after, rollback if the value did not take).\n--no-apply skips that write.\n"),
    ("Exit codes: 0 ACK, 2 rejected before the mule, 3 dead letter, 1 stopped (missing file, model error, no Godot).\n",
     "Exit codes: 0 ACK or APPLIED, 2 rejected before the mule, 3 dead letter, 4 ACK but not applied or rolled back, 1 stopped (missing file, model error, no Godot).\n"),
    ("# ---------------------------------------------------------------- the clock\n",
     APPLY + "# ---------------------------------------------------------------- the clock\n"),
    ('    ap.add_argument("--tries", type=int, default=3)\n',
     '    ap.add_argument("--tries", type=int, default=3)\n    ap.add_argument("--no-apply", action="store_true", help="check only: do not write the scene")\n'),
    ('''                log(f"SLOT_4 JUDGE    ACK  godot --check-only exit 0")
                log(f"RESULT: ACK in {t} {'try' if t == 1 else 'tries'} -> ledger.jsonl")
                finish(0, status="ACK", tries=t, stmt=stmt, uid=host["uid"], property=block["property"], type=block["type"], value=block["value"], attempts=attempts, gi2=meas)
''',
     '''                log("SLOT_4 JUDGE    ACK  godot --check-only exit 0")
                status, extra, code = "ACK", {}, 0
                if not a.no_apply:
                    try:
                        res = apply_scene_property(a.godot, project, host, block)
                    except ApplyError as e:
                        log(f"SLOT_4 APPLY    NOT WRITTEN  {e}")
                        status, extra, code = "ACK_NOT_APPLIED", {"apply_error": str(e)}, 4
                    else:
                        scene = res["file"][len("res://"):]
                        log(f"SLOT_4 APPLY    {res['action']} line {res['line']} of {scene}: {res['after']}")
                        log(f"                backup: {os.path.relpath(res['backup'], ws)}")
                        if res["verified"]:
                            log(f"SLOT_4 RELOAD   ok   {host['name']}.{block['property']} = {res['reloaded_value']}")
                            status, extra = "APPLIED", {"apply": res}
                        else:
                            log(f"SLOT_4 RELOAD   FAILED, ROLLED BACK  {res['problem']}")
                            status, extra, code = "ROLLED_BACK", {"apply": res}, 4
                log(f"RESULT: {status} in {t} {'try' if t == 1 else 'tries'} -> ledger.jsonl")
                finish(code, status=status, tries=t, stmt=stmt, uid=host["uid"], property=block["property"], type=block["type"], value=block["value"], attempts=attempts, gi2=meas, **extra)
'''),
]

for old, new in EDITS:
    if s.count(old) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + old[:90])
    s = s.replace(old, new, 1)

compile(s, p, "exec")
open(p + ".before-step5", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_clock.py patched with Step 5 (backup: cascade_clock.py.before-step5)")
