# step13_paste.sh - Step 13: multi-statement handler bodies + atomic handler replacement
# Paste this whole block into a terminal. No GPU, no mule: the last command is a dry run on a throwaway copy.
set -u
cd ~/cascade_ws || { echo "STOPPED: ~/cascade_ws not found"; exit 1; }

# ---- 1. keep the files this step replaces (backups are made once)
for f in cascade_plan.py cascade_vocab.json harness_protocol.json patch_status.py patch_multi.py cascade_clock.py task_block_spec.json; do
  [ -f "$f" ] && [ ! -f "$f.before-step13" ] && cp "$f" "$f.before-step13"
done

# ---- 2. write patch_multi.py
cat << 'PMEOF' > ~/cascade_ws/patch_multi.py
import os, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (it came with the Step 9 script)")

# ------------------------------------------------------------------ the spec: the mode slot, the statement list
def spec_edit(d):
    d["slots"]["mode"] = {"type": "string", "enum": ["add", "replace"], "default": "add"}
    opt = d["ops"]["add_method"]["optional_slots"]
    if "mode" not in opt: opt.append("mode")
    d["slots"]["body"]["also_array"] = "body may be an array of 1 to 6 statement objects: {kind: assign|relative|print|pass, ...}; with an array, message must be empty and property/type/operator/value live inside the statements"
    d["slots"]["statement"] = {"kinds": {"assign": ["property", "type", "value"], "relative": ["property", "type", "operator", "value"], "print": ["message"], "pass": []}, "max_statements": 6,
                               "rules": ["pass only alone", "one = per property", "the net effect on every touched property must differ from its start", "a relative statement may not be overwritten by a later ="]}
    d["ops"]["add_method"]["v4"] = "mode replace swaps the whole body of an existing _on_ handler without arguments (same signature); it needs an array body"

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 13: multi-statement bodies and atomic handler replacement
# Everything below WRAPS or SHADOWS an earlier definition: a legacy single-statement add_method block keeps calling the code that was proven in Steps 8 to 12.
MAX_STATEMENTS = 6
STATEMENT_KINDS = ("assign", "relative", "print", "pass")
REPLACE_NAME_RX = r"^_on_[a-z0-9_]+$"

def is_body_frame(block):
    """A frame the mule fills one statement at a time: an array body, or a replacement."""
    return block.get("op") == "add_method" and (isinstance(block.get("body"), list) or block.get("mode") == "replace")

def statements_of(block):
    """The block's body as a list of statement objects, whatever form it was written in."""
    b = block["body"]
    if isinstance(b, list): return [dict(s) for s in b]
    if b == "pass": return [{"kind": "pass"}]
    if b == "print": return [{"kind": "print", "message": block["message"]}]
    op = block.get("operator", "=")
    st = {"kind": "assign" if op == "=" else "relative", "property": block["property"], "type": block.get("type"), "value": block["value"]}
    if op != "=": st["operator"] = op
    return [st]

def statement_line(st):
    """The canonical text of one statement."""
    if st["kind"] == "pass": return "pass"
    if st["kind"] == "print": return f'print("{st["message"]}")'
    op = "=" if st["kind"] == "assign" else st["operator"]
    return f"{st['property']} {op} {scene_literal(operand_type(st['type'], op), st['value'])}"

_step12_body_line = body_line

def body_lines(block):
    if isinstance(block["body"], list): return [statement_line(s) for s in block["body"]]
    return [_step12_body_line(block)]

def body_line(block):
    if isinstance(block["body"], list): return "; ".join(body_lines(block))
    return _step12_body_line(block)

def canonical_function(block, indent):
    return signature(block) + "\n" + "".join(f"{indent}{l}\n" for l in body_lines(block))

def function_matches(project, host, block):
    fp = script_path(project, host)
    if not os.path.exists(fp): return False
    have = existing_function(open(fp, encoding="utf-8").read(), block["fn"])
    return have is not None and _norm([l for l in have if l]) == _norm([signature(block)] + body_lines(block))

def legacy_block(block, st):
    """A single-statement add_method block for one statement: the shape the mule prompts and the guards were proven on."""
    b = {k: v for k, v in block.items() if k not in ("body", "message", "property", "type", "operator", "value", "mode")}
    if st["kind"] == "pass": b.update(body="pass", message="")
    elif st["kind"] == "print": b.update(body="print", message=st["message"])
    else:
        b.update(body="assign", message="", property=st["property"], type=st["type"], value=st["value"])
        if st["kind"] == "relative": b["operator"] = st["operator"]
    return b

# ---- the function's span in a script: replaced as one piece, nothing else in the file moves
def function_span(raw, fn):
    """(start, end) offsets of 'func fn(...)' and its indented body; trailing blank lines stay in the gap. None if the script does not define it."""
    lines = raw.splitlines(keepends=True); pos = 0
    for i, l in enumerate(lines):
        if re.match(rf"^func\s+{re.escape(fn)}\s*\(", l):
            j = i + 1
            while j < len(lines) and (lines[j].strip() == "" or lines[j][:1] in (" ", "\t")): j += 1
            while j > i + 1 and lines[j - 1].strip() == "": j -= 1
            return pos, sum(len(x) for x in lines[:j])
        pos += len(l)
    return None

def replace_function(raw, fn, new_text):
    sp = function_span(raw, fn)
    return raw[:sp[0]] + new_text + raw[sp[1]:]

def annotation_above(raw, fn):
    lines = raw.splitlines()
    for i, l in enumerate(lines):
        if re.match(rf"^func\s+{re.escape(fn)}\s*\(", l):
            j = i - 1
            while j >= 0 and not lines[j].strip(): j -= 1
            return j >= 0 and lines[j].lstrip().startswith("@")
    return False

def connections_of(project, host, fn):
    fp = os.path.join(project, host["owner_scene"][len("res://"):]); out = []
    if not os.path.exists(fp): return out
    rel = node_rel(host)
    for l in open(fp, encoding="utf-8").read().splitlines():
        if l.startswith("[connection "):
            at = dict(re.findall(r'(\w+)="([^"]*)"', l))
            if at.get("to") == rel and at.get("method") == fn and at.get("signal") not in out: out.append(at.get("signal"))
    return out

# ---- simulation: what a statement list does to the properties it touches
def start_values(sts):
    """The value each touched property starts from in the probe: a known baseline when its first statement is relative, a perturbed value when it is an assignment."""
    out = {}
    for st in sts:
        if st["kind"] in ("assign", "relative") and st["property"] not in out:
            out[st["property"]] = baseline_for(st["type"]) if st["kind"] == "relative" else perturbed_value(st["type"], st["value"])
    return out

def simulate(sts, start, n):
    vals = {k: (list(v) if isinstance(v, list) else v) for k, v in start.items()}; prints = collections.Counter()
    for _ in range(n):
        for st in sts:
            if st["kind"] == "print": prints[st["message"]] += 1
            elif st["kind"] == "assign":
                v = st["value"]
                vals[st["property"]] = (list(v) + [1.0] if st["type"] == "Color" and len(v) == 3 else (list(v) if isinstance(v, list) else v))
            elif st["kind"] == "relative": vals[st["property"]] = apply_op(st["type"], vals[st["property"]], st["operator"], st["value"])
    return vals, prints

# ---- GI_1
def statement_errors(sts, host, spec):
    bad, s = [], spec["slots"]
    if not sts or len(sts) > MAX_STATEMENTS: return [f"block.body must hold 1 to {MAX_STATEMENTS} statements, it has {len(sts)}"]
    for i, st in enumerate(sts, 1):
        w = f"statement {i}"
        if not isinstance(st, dict) or st.get("kind") not in STATEMENT_KINDS:
            bad.append(f"{w}: kind must be one of {', '.join(STATEMENT_KINDS)}"); continue
        want = {"assign": {"kind", "property", "type", "value"}, "relative": {"kind", "property", "type", "operator", "value"}, "print": {"kind", "message"}, "pass": {"kind"}}[st["kind"]]
        if set(st) != want:
            bad.append(f"{w} ({st['kind']}): needs exactly {sorted(want)}" + (f", has {sorted(set(st) - want)} extra" if set(st) - want else "")); continue
        if st["kind"] == "print":
            if not isinstance(st["message"], str) or st["message"] == "" or not re.match(s["message"]["rx"], st["message"]): bad.append(f"{w}: message must be plain text of up to 40 characters (letters, digits, space and _ . , : ! ? -)")
        elif st["kind"] in ("assign", "relative"):
            p, t = st["property"], st["type"]
            if p not in host.get("props", {}): bad.append(f"{w}: '{p}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host.get('props', {}))) or 'none'})"); continue
            if t != host["props"][p]: bad.append(f"{w}: type {t} but the record says {p} is {host['props'][p]}"); continue
            op = "=" if st["kind"] == "assign" else st["operator"]
            if st["kind"] == "relative" and op not in ("+=", "-=", "*="): bad.append(f"{w}: operator '{op}' not allowed for relative (+=, -=, *=)"); continue
            if st["kind"] == "relative" and t not in RELATIVE_TYPES: bad.append(f"{w}: {op} works on {', '.join(RELATIVE_TYPES)} properties, but {p} is {t}"); continue
            if not value_ok(operand_type(t, op), st["value"]): bad.append(f"{w}: value {st['value']} is not a valid {operand_type(t, op)} operand for {op} on {p}"); continue
            if st["kind"] == "relative" and is_identity(op, t, st["value"]): bad.append(f"{w}: {op} {st['value']} changes nothing; a statement that does nothing is not what was asked for"); continue
            if st["kind"] == "assign" and t == "String" and not re.match(s["message"]["rx"], str(st["value"])): bad.append(f"{w}: a String value must use the plain message charset, so the mule can type it safely")
    if bad: return bad
    if len(sts) > 1 and any(st["kind"] == "pass" for st in sts): bad.append("pass is only allowed alone: it does nothing next to other statements")
    seen_assign = {}
    for i, st in enumerate(sts, 1):
        if st["kind"] == "assign":
            if st["property"] in seen_assign: bad.append(f"statement {i}: {st['property']} is assigned (=) twice (statements {seen_assign[st['property']]} and {i}); the second would erase the first")
            seen_assign[st["property"]] = i
    for i, st in enumerate(sts, 1):
        if st["kind"] == "relative":
            later = [j for j, s2 in enumerate(sts, 1) if j > i and s2["kind"] == "assign" and s2["property"] == st["property"]]
            if later: bad.append(f"statement {i}: {st['property']} {st['operator']} {st['value']} is overwritten by the = in statement {later[0]}")
    if bad: return bad
    start = start_values(sts); final, _ = simulate(sts, start, 1)
    for p in start:
        if _close(final[p], start[p]): bad.append(f"the statements leave {p} where it started, so their net effect on it is nothing")
    return bad

_step12_add_method_checks = add_method_checks

def add_method_checks(block, host, spec, project):
    s = spec["slots"]
    if not isinstance(block["body"], list) and block.get("mode", "add") == "add":
        return [m.replace("editing a function is an open slot", "to change its body use mode replace (the change_handler step in a plan)") for m in _step12_add_method_checks(block, host, spec, project)]
    bad = []
    mode = block.get("mode", "add")
    if mode not in spec["slots"]["mode"]["enum"]: return [f"block.mode '{mode}' not allowed (one of {', '.join(spec['slots']['mode']['enum'])})"]
    if not isinstance(block["body"], list): return ["block.mode replace needs block.body to be an array of statements"]
    for k in ("fn", "message"):
        if not re.match(s[k]["rx"], str(block[k])): bad.append(f"block.{k} '{block[k]}' does not match the spec")
    if block["message"] != "": bad.append("block.message must be empty when body is an array")
    extra = [k for k in ("property", "type", "operator", "value") if k in block]
    if extra: bad.append("block." + ", block.".join(extra) + " only belong to a single-statement body; put them inside the statements")
    if block["returns"] not in s["returns"]["enum"]: bad.append(f"block.returns '{block['returns']}' not allowed (v1: void)")
    args = block["args"]
    if not isinstance(args, list) or len(args) > s["args"]["max_items"] or any(not isinstance(x, str) or not re.match(s["args"]["item_rx"], x) for x in args):
        bad.append("block.args must be a list of at most 3 'name: Type' strings (Type: bool, int, float, String, Vector2, Vector3, Color)")
    elif len({x.split(":")[0] for x in args}) != len(args): bad.append("block.args repeats an argument name")
    if bad: return bad
    bad = statement_errors(statements_of(block), host, spec)
    if bad: return bad
    if not host.get("script"): return [f"GI_1 script: {host['name']} has no script, so it cannot get a method (adding a script is an open slot)"]
    fp = script_path(project, host)
    if not os.path.exists(fp): return [f"GI_1 script: {host['script']} not found"]
    for k in ("signals", "methods", "props"):
        if k not in host: bad.append(f"GI_1 registry has no '{k}' field; update and re-run registry_builder.py")
    if bad: return bad
    raw = open(fp, encoding="utf-8").read()
    have = existing_function(raw, block["fn"])
    same = have is not None and _norm([l for l in have if l]) == _norm([signature(block)] + body_lines(block))
    if mode == "add":
        if block["fn"] in host["signals"]: bad.append(f"GI_1 fn: '{block['fn']}' is a signal of {host['name']}")
        if block["fn"] in host["props"]: bad.append(f"GI_1 fn: '{block['fn']}' is a property of {host['name']}")
        if have is not None and not same: bad.append(f"GI_1 fn_exists: {block['fn']} is already defined in {host['script']} with a different body; to change its body use mode replace (the change_handler step in a plan)")
        return bad
    if have is None: return [f"GI_1 fn_missing: mode replace needs {block['fn']} to exist in {host['script']} (use mode add to create it)"]
    if same: return []
    if not re.match(REPLACE_NAME_RX, block["fn"]): bad.append(f"GI_1 fn_not_replaceable: {block['fn']} is not a handler (_on_...); engine callbacks and other functions cannot be replaced")
    if block["args"] != []: bad.append(f"GI_1 fn_not_replaceable: only handlers without arguments can be replaced, the block has {block['args']}")
    if have[0] != signature(block): bad.append(f"GI_1 signature: the script has '{have[0]}', a replacement keeps the signature '{signature(block)}'")
    if annotation_above(raw, block["fn"]): bad.append(f"GI_1 annotation: a line starting with @ sits above {block['fn']}; replacing it is an open slot")
    return bad

# ---- GI_3: one new line against one statement, the lines before it already fixed
def gi3_body(reply, block, sub, k, accepted, spec):
    if reply is None: return "json_parse: no JSON object with a 'stmt' key in the answer"
    if reply.get("frame_id") != block["frame_id"] or reply.get("target") != block["target"]: return "slot_enum_match: frame_id and target must echo the block"
    stmt = reply.get("stmt")
    if not isinstance(stmt, str) or not stmt.strip(): return "slot_enum_match: stmt must be a non-empty string"
    stmt = stmt.strip(); lines = stmt.split("\n")
    if len(lines) != k + 2: return f"type_match: stmt must be exactly {k + 2} lines (the signature, the {k} line(s) already accepted and ONE new indented line), got {len(lines)}"
    if lines[0] != signature(block): return f"type_match: line 1 must be {signature(block)}"
    for j, acc in enumerate(accepted):
        if _norm([lines[1 + j].strip()]) != _norm([acc]): return f"type_match: line {j + 2} must stay {acc}"
    new = lines[k + 1]
    if new[:1] not in (" ", "\t"): return "type_match: the new line must be indented"
    err = add_method_stmt_error(signature(sub) + "\n    " + new.strip(), sub)
    if err: return err.replace("the body must be", "this line must be")
    for name in block["exclude"]:
        chk = spec["exclude_checks"][name]
        if chk["kind"] == "regex" and re.search(chk["rx"], stmt): return f"exclude_hit: {name}"
        if chk["kind"] == "code" and re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", stmt): return f"exclude_hit: {name} (no addresses inside a statement)"
    return None

# ---- Slot 4: judge and apply, with replace
def judge_function(godot, project, host, block):
    """Slot 4 for add_method: the canonical function appended to (or swapped into) a temporary copy of the whole script, then godot --check-only."""
    raw = open(script_path(project, host), "rb").read().decode("utf-8")
    fn_text = canonical_function(block, detect_indent(raw)); base = drop_class_name(raw)
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = replace_function(base, block["fn"], fn_text.replace("\n", nl)) if block.get("mode") == "replace" else append_function_text(base, fn_text)
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

_step12_apply_script_function = apply_script_function

def apply_script_function(godot, project, host, block):
    if block.get("mode") != "replace": return _step12_apply_script_function(godot, project, host, block)
    fp = script_path(project, host)
    if not os.path.exists(fp): raise ApplyError(f"{host['script']} not found")
    raw = open(fp, "rb").read().decode("utf-8")
    sp = function_span(raw, block["fn"])
    if sp is None: raise ApplyError(f"{block['fn']}() is not in {host['script']}")
    nl = "\r\n" if "\r\n" in raw else "\n"
    old = [l.strip() for l in raw[sp[0]:sp[1]].splitlines() if l.strip()]
    new_text = replace_function(raw, block["fn"], canonical_function(block, detect_indent(raw)).replace("\n", nl))
    line_no = raw[:sp[0]].count("\n") + 1
    bak = f"{fp}.{block['frame_id']}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(fp, bak)
    tmp = fp + ".cascade_tmp"
    open(tmp, "wb").write(new_text.encode("utf-8")); os.replace(tmp, fp)
    ok, got, msg = reload_check_function(godot, project, host, block)
    result = {"file": host["script"], "backup": bak, "action": "replaced", "line": line_no, "before": " / ".join(old[1:]),
              "after": f"{signature(block)}  {body_line(block)}", "reloaded_value": got, "verified": ok}
    if not ok:
        shutil.copy2(bak, fp); result["rolled_back"] = True; result["problem"] = msg
    return result

_step12_op_fields = op_fields

def op_fields(block):
    if block.get("op") == "add_method" and (isinstance(block.get("body"), list) or block.get("mode")):
        return {"fn": block["fn"], "args": block["args"], "body": block["body"], "message": block["message"], "mode": block.get("mode", "add")}
    return _step12_op_fields(block)

# ---- Slot 5: the handler's statements, read from the block or back from the script
def parse_statement_line(line, host):
    line = line.strip()
    if line == "pass": return {"kind": "pass"}
    m = re.match(r'^print\((["\'])(.*)\1\)$', line)
    if m: return {"kind": "print", "message": m.group(2)}
    m = re.match(r"^(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*(\+=|-=|\*=)\s*(.+)$", line) or re.match(r"^(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:self\.)?\1\s*([+\-*])\s*(.+)$", line)
    if m and m.group(1) in host["props"] and host["props"][m.group(1)] in RELATIVE_TYPES:
        rop = m.group(2) if m.group(2).endswith("=") else m.group(2) + "="
        val = parse_literal(operand_type(host["props"][m.group(1)], rop), m.group(3))
        if val is not None: return {"kind": "relative", "property": m.group(1), "type": host["props"][m.group(1)], "operator": rop, "value": val}
    m = re.match(r"^(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", line)
    if m and m.group(1) in host["props"]:
        val = parse_literal(host["props"][m.group(1)], m.group(2))
        if val is not None: return {"kind": "assign", "property": m.group(1), "type": host["props"][m.group(1)], "value": val}
    return None

def function_statements(raw, fn, host):
    """The statements of a function in a script, or None if it is missing or any line is not one of the statement kinds."""
    have = existing_function(raw, fn)
    if not have: return None
    body = [l for l in have[1:] if l and not l.startswith("#")]
    sts = [parse_statement_line(l, host) for l in body]
    return sts if sts and all(sts) else None

def probe_statements(project, host, block):
    if block["op"] == "add_method": return statements_of(block)
    return function_statements(open(script_path(project, host), encoding="utf-8").read(), block["method"], host)

_step12_run_probe = run_probe

def run_probe(godot, project, host, block):
    sts = probe_statements(project, host, block)
    if sts is None or len(sts) <= 1:
        if block["op"] == "add_method" and isinstance(block["body"], list): block = legacy_block(block, sts[0])
        return _step12_run_probe(godot, project, host, block)
    return run_probe_multi(godot, project, host, block, sts)

_PROBE_OLD = """    if a.size() > 8:
        var pt: Variant = JSON.parse_string(a[8])
        if pt != null:
            p.set(str(pt["prop"]), _from_j(str(pt["type"]), pt["value"]))
"""
_PROBE_NEW = """    if a.size() > 8:
        var pt: Variant = JSON.parse_string(a[8])
        if pt != null:
            var items: Array = pt if pt is Array else [pt]
            for it in items:
                p.set(str(it["prop"]), _from_j(str(it["type"]), it["value"]))
"""
assert _PROBE_OLD in PROBE_GD, "the probe script is not the Step 12 one"
PROBE_GD = PROBE_GD.replace(_PROBE_OLD, _PROBE_NEW)

def run_probe_multi(godot, project, host, block, sts):
    """Slot 5 for a statement list: simulate the whole list n times from the starting values, then compare the normal run with the control run."""
    mode = "signal" if block["op"] == "connect_signal" else "call"
    name = block["signal"] if mode == "signal" else block["fn"]
    method = block["method"] if mode == "signal" else block["fn"]
    types = list((host.get("signal_args") or {}).get(name, [])) if mode == "signal" else [x.split(": ")[1] for x in block["args"]]
    start = start_values(sts)
    perturb = [{"prop": p, "type": next(s["type"] for s in sts if s.get("property") == p), "value": v} for p, v in start.items()] or None
    tmp = tempfile.mkdtemp(prefix="cascade_probe_")
    helper, out = os.path.join(tmp, "probe.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(PROBE_GD)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), mode, name, method,
                            json.dumps(sorted(host["props"])), json.dumps(types), json.dumps(perturb), "1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return {"verdict": "FAIL", "mode": mode, "summary": "the probe timed out (60 s)", "warnings": []}
    text = strip_ansi(r.stdout)
    if not os.path.exists(out):
        return {"verdict": "FAIL", "mode": mode, "summary": "the probe could not run: " + (godot_errors(text) or text[-200:].strip()), "warnings": []}
    res = json.load(open(out)); nrm, ctl = res["normal"], res["control"]
    pn, en = _split_lines(_segment(text, "normal")); pc, ec = _split_lines(_segment(text, "control"))
    eff_prints = collections.Counter(pn) - collections.Counter(pc)
    ch = lambda run: {k: [run["pre"][k], run["post"][k]] for k in run["pre"] if run["pre"][k] != run["post"][k]}
    chn, chc = ch(nrm), ch(ctl)
    eff_props = {k: v for k, v in chn.items() if chc.get(k) != v}
    eff_errs = list((collections.Counter(en) - collections.Counter(ec)).elements())
    mine = [e for e in eff_errs if (f"signal '{name}'" in e or f"::{method}" in e)] if mode == "signal" else [e for e in eff_errs if name in e]
    other = [e[:160] for e in eff_errs if e not in mine]
    how = "explicit emit" if nrm.get("explicit") else "natural"
    n = 1 if mode == "call" else nrm["emissions"]
    want_vals, want_prints = simulate(sts, start, n)
    v = {"mode": mode, "name": name, "natural": not nrm.get("explicit"), "explicit": bool(nrm.get("explicit")), "emissions": nrm["emissions"], "expect": sts,
         "effect": {"prints": dict(eff_prints), "props": eff_props}, "errors_mine": [e[:200] for e in mine], "warnings": ["another connection in this scene reports: " + e for e in other]}
    where = f"{mode} {name}" + (f" -> {method}()" if mode == "signal" else "()")
    if mode == "signal" and nrm["emissions"] == 0:
        v.update(verdict="FAIL", summary=f"{where}: the signal never fired, not even when emitted explicitly"); return v
    if mine:
        v.update(verdict="FAIL", summary=f"{where}: Godot reports an error from this connection: {mine[0][:150]}"); return v
    problems = []
    for p in start:
        post_n, post_c = nrm["post"].get(p), ctl["post"].get(p)
        if not _close(post_n, want_vals[p]): problems.append(f"{p} should go {start[p]} -> {want_vals[p]} but the normal run ended at {post_n}")
        elif not _close(post_c, start[p]): problems.append(f"the control run moved {p} from {start[p]} to {post_c}, so the change cannot be credited to this handler")
    others = {k: x for k, x in eff_props.items() if k not in start}
    if others: problems.append(f"the run also changed {others}")
    if dict(eff_prints) != dict(want_prints): problems.append(f"expected the prints {dict(want_prints) or 'none'} but the normal run printed {dict(eff_prints) or 'nothing'} more than the control run")
    if problems:
        v.update(verdict="FAIL", summary=f"{where}: {len(sts)} statements x{n}: " + "; ".join(problems)); return v
    parts = [f"{p} {start[p]} -> {nrm['post'].get(p)}" for p in start] + [f"printed {m!r} x{c}" for m, c in want_prints.items()]
    v.update(verdict="PASS", summary=f"{where}: {len(sts)} statements ran {n} time(s): " + ", ".join(parts) + "; the control changed none of it" + (f"; {how}" if mode == "signal" else ""))
    return v

# ---- Slot 3 for a body frame: one mule call per statement, the harness holds the scaffold
def run_body_frame(a, ws, project, host, block, spec, row, ask, set_temp, rec, finish, meas):
    sts = statements_of(block); n = len(sts); mode = block.get("mode", "add")
    if not a.prefill and not a.script: die("a multi-statement frame needs the prefill (drop --no-prefill): the mule only ever completes one line")
    raw = open(script_path(project, host), encoding="utf-8").read(); indent = detect_indent(raw); sig = signature(block)
    head = (json.dumps({"frame_id": block["frame_id"], "target": block["target"]}, separators=(",", ":"))[:-1] + ',"stmt":"') if a.prefill else ""
    subs = [legacy_block(block, st) for st in sts]
    row.update(prefill=bool(a.prefill), slim=bool(a.slim), ultra=bool(a.ultra), temps=a.temps, example=False, statements=n, body_mode=mode)
    if a.run_id: row["run_id"] = a.run_id
    row["message_chars"] = len(mule_message(subs[0], host, None, None, a.slim, a.ultra))
    old = existing_function(raw, block["fn"]) if mode == "replace" else None
    log(f"BODY            {n} statement(s): " + ("replacing the body of " if mode == "replace" else "new function ") + f"{block['fn']}()" + (f"  (now: {'; '.join(l for l in old[1:] if l)})" if old else ""))
    log(f"MESSAGE         {'ultra' if a.ultra else 'slim' if a.slim else 'full'}: {row['message_chars']} characters for statement 1; one mule call per statement")
    attempts, accepted, calls, clipm = [], [], 0, None
    for k, st in enumerate(sts):
        scaffold = sig + "\n" + "".join(indent + l + "\n" for l in accepted)
        prefill = (head + json.dumps(scaffold)[1:-1]) if a.prefill else ""
        if prefill: log(f"PREFILL         ...{prefill[-72:]}  (statement {k + 1}/{n}: the mule only completes one line)")
        prev, line_ok = None, None
        for t in range(1, a.tries + 1):
            msg = mule_message(subs[k], host, prev, None, a.slim, a.ultra)
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
            log(f"SLOT_3 EXECUTE  statement {k + 1}/{n} try {t}/{a.tries}  (T={temp}, {nt} tokens, {dt:.0f}s)  line: {stmt.split(chr(10))[-1].strip()}")
            err = gi3_body(reply, block, subs[k], k, accepted, spec)
            if err:
                log(f"GI_3            NAK  {err}")
                attempts.append({"statement": k + 1, "try": t, "temp": temp, "stmt": stmt, "status": "NAK", "error": err}); prev = {"try": t, "stmt": stmt, "error": err}; continue
            log("GI_3            ok")
            attempts.append({"statement": k + 1, "try": t, "temp": temp, "stmt": stmt, "status": "LINE_OK"}); line_ok = statement_line(st); break
        if line_ok is None:
            dl = os.path.join(ws, "dead_letter"); os.makedirs(dl, exist_ok=True)
            path = os.path.join(dl, f"{block['frame_id']}.json")
            json.dump({"block": block, "rag": host, "failed_statement": k + 1, "accepted_prefix": accepted, "attempts": attempts}, open(path, "w"), indent=2)
            log(f"RESULT: DEAD LETTER statement {k + 1}/{n} failed {a.tries} tries (accepted before it: {len(accepted)}) -> {path}")
            finish(3, status="DLQ", tries=calls, attempts=attempts, dead_letter=path, failed_statement=k + 1)
        accepted.append(line_ok)
    assembled = canonical_function(block, indent).rstrip("\n")
    ok, jerr = judge_function(a.godot, project, host, block)
    if not ok:
        log(f"SLOT_4 JUDGE    NAK  {jerr}")
        dl = os.path.join(ws, "dead_letter"); os.makedirs(dl, exist_ok=True)
        path = os.path.join(dl, f"{block['frame_id']}.json")
        json.dump({"block": block, "rag": host, "judge_error": jerr, "accepted_lines": accepted, "attempts": attempts}, open(path, "w"), indent=2)
        log(f"RESULT: DEAD LETTER the assembled function did not parse -> {path}")
        finish(3, status="DLQ", tries=calls, attempts=attempts, dead_letter=path, judge_error=jerr)
    if rec and clipm: clipm["map_recorded"] = rec.commit(); row["clip"] = clipm
    log("SLOT_4 JUDGE    ACK  godot --check-only exit 0")
    status, extra, code = "ACK", {}, 0
    if not a.no_apply:
        try:
            res = apply_scene(a.godot, project, host, block)
        except ApplyError as e:
            log(f"SLOT_4 APPLY    NOT WRITTEN  {e}"); status, extra, code = "ACK_NOT_APPLIED", {"apply_error": str(e)}, 4
        else:
            log(f"SLOT_4 APPLY    {res['action']} line {res['line']} of {res['file'][len('res://'):]}: {res['after']}")
            if res.get("before"): log(f"                replaced: {res['before']}")
            log(f"                backup: {os.path.relpath(res['backup'], ws)}")
            if res["verified"]:
                log(f"SLOT_4 RELOAD   ok   {res['what']} = {res['reloaded_value']}")
                status, extra = "APPLIED", {"apply": res}
                if not a.no_verify:
                    vr = run_probe(a.godot, project, host, block); extra["verify"] = vr
                    log("SLOT_5 VERIFY   " + verify_line(vr))
                    for w in vr.get("warnings", []): log("                warning: " + w)
                    failed = vr["verdict"] == "FAIL"
                    if mode == "replace" and not failed:
                        cv = []
                        for sg in connections_of(project, host, block["fn"]):
                            cb = {"op": "connect_signal", "signal": sg, "method": block["fn"], "target": block["target"], "source": block["target"], "ref": block["ref"], "subnet": block["subnet"], "frame_id": block["frame_id"], "time_slot": block["time_slot"], "exclude": []}
                            v2 = run_probe(a.godot, project, host, cb); cv.append(v2)
                            log("SLOT_5 VERIFY   " + verify_line(v2) + "   (through the connection it already had)")
                            failed = failed or v2["verdict"] == "FAIL"
                        if not cv: log("SLOT_5 VERIFY   (no scene connection points at this handler, so only the direct call was checked)")
                        extra["verify_connections"] = cv
                    if failed:
                        shutil.copy2(res["backup"], os.path.join(project, res["file"][len("res://"):]))
                        log("SLOT_5 VERIFY   FAILED, ROLLED BACK  the edit was undone"); status, code = "ROLLED_BACK_VERIFY", 4
                if status == "APPLIED" and mode == "add":
                    rr = refresh_registry(ws, a.godot, host, block); extra["registry_refresh"] = rr
                    log(f"REGISTRY       refreshed: {block['fn']} is listed ({rr['methods']} methods now)" if rr.get("ok") and rr.get("listed") else f"REGISTRY       NOT refreshed - run registry_builder.py by hand ({rr.get('detail')})")
            else:
                log(f"SLOT_4 RELOAD   FAILED, ROLLED BACK  {res['problem']}"); status, extra, code = "ROLLED_BACK", {"apply": res}, 4
    log(f"RESULT: {status} in {calls} mule call(s) -> ledger.jsonl")
    finish(code, status=status, tries=calls, stmt=assembled, uid=host["uid"], **op_fields(block), attempts=attempts, gi2=meas, **extra)

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
CLOCK = [
 (MARK, NEW_SECTION + MARK),
 (r"""    example = worked_example(block, host, spec) if a.example else None
""",
  r"""    if is_body_frame(block):
        return run_body_frame(a, ws, project, host, block, spec, row, ask, set_temp, rec, finish, meas)
    example = worked_example(block, host, spec) if a.example else None
"""),
]
entries = [
 {"file": "cascade_clock.py", "marker": "def statements_of", "edits": CLOCK},
 {"file": "task_block_spec.json", "done": lambda d: "mode" in d["slots"] and "mode" in d["ops"]["add_method"].get("optional_slots", []), "edit": spec_edit},
]
sys.exit(1 if pl.run(ws, "multi", entries) else 0)
PMEOF

# ---- 2. write cascade_plan.py
cat << 'PLANEOF' > ~/cascade_ws/cascade_plan.py
#!/usr/bin/env python3
"""cascade_plan.py - Project Cascade, Step 11: the harness layer.

  python3 cascade_plan.py catalog                  what the harness may plan with (paste it into the session)
  python3 cascade_plan.py expand  plan.json        the primitive blocks a compact plan turns into (nothing runs)
  python3 cascade_plan.py dry-run plan.json        the whole pipeline on a throwaway copy of the workspace: no mule, nothing real touched
  python3 cascade_plan.py run     plan.json        dry run first, then the real chain (use the venv python: it loads Gemma)
  python3 cascade_plan.py undo    <plan id>        restore the plan's backups, newest change first (refuses if later work depends on them)
  python3 cascade_plan.py status                   the plans so far

The part that reads English is the harness model. This tool is the deterministic side: it fills in what the registry knows,
runs every guard before anything real happens, and can take a plan back out.
"""
import argparse, difflib, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time

OK_STATUS = ("ACK", "APPLIED", "NOOP_ALREADY_CONNECTED", "NOOP_ALREADY_DEFINED")
NOOP_STATUS = ("NOOP_ALREADY_CONNECTED", "NOOP_ALREADY_DEFINED")
EXC = {"add_method": ["KinematicBody2D", "untyped_variables", "yield", "relative_paths_up_tree", "cross_subnet_queries"],
       "connect_signal": ["KinematicBody2D", "connect_3arg", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"],
       "modify_property": ["KinematicBody2D", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"]}
RELATIVE_TYPES = ("float", "int", "Vector2", "Vector3")
RELATIVE_VERBS = (("increase", "+=", "grows by"), ("decrease", "-=", "shrinks by"), ("scale", "*=", "is multiplied by"))
MAX_STATEMENTS = 6
STEP_KEYS = {"when": {"signal", "then"}, "set_property": {"property", "value"}, "add_function": {"name", "then"}, "connect": {"signal", "handler"}, "change_handler": {"handler", "then"}}


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
    raise PlanError(f"{where}: each 'then' item must be exactly one of {{\"set\": {{\"property\": P, \"value\": V}}}}, {{\"increase\"|\"decrease\"|\"scale\": {{\"property\": P, \"by\": V}}}}, {{\"print\": \"message\"}}, {{\"nothing\": true}}")


def body_from_then_any(then, host, where, force_array=False):
    """A single form keeps the proven single-statement block; a list of two or more (or a replacement) becomes an array body."""
    if isinstance(then, list):
        if not then or len(then) > MAX_STATEMENTS:
            raise PlanError(f"{where}: 'then' as a list holds 1 to {MAX_STATEMENTS} items, it has {len(then)}")
        sts = [statement_from_form(f, host, f"{where}, item {i}") for i, f in enumerate(then, 1)]
        if len(sts) == 1 and not force_array:
            return body_from_then(then[0], host, where)
        return {"statements": sts}
    if force_array:
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


def connect_block(host, signal, method):
    b = head(host, "connect_signal")
    b.update(source=host["name"], target=host["name"], signal=signal, method=method, ref=host["addr"], exclude=list(EXC["connect_signal"]))
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
        extra = set(step) - STEP_KEYS[do] - {"do"}
        missing = STEP_KEYS[do] - set(step)
        if extra or missing:
            raise PlanError(f"{where} ({do}): " + (f"unexpected {sorted(extra)} " if extra else "") + (f"missing {sorted(missing)}" if missing else ""))
        if do in ("when", "connect") and step["signal"] not in host.get("signals", []):
            close = difflib.get_close_matches(str(step["signal"]), host.get("signals", []), n=3)
            raise PlanError(f"{where}: '{step['signal']}' is not a signal of {host['name']}" + (f" (did you mean {', '.join(close)}?)" if close else ""))
        if do == "when":
            fn = f"_on_{snake(host['name'])}_{step['signal']}"
            body = body_from_then_any(step["then"], host, where)
            blocks.append(add_method_block(host, fn, body))
            blocks.append(connect_block(host, step["signal"], fn))
            if ctx.vocab.get("signals", {}).get(step["signal"], {}).get("repeats", True):
                for prop, op, val in body_relatives(body):
                    how = next(h for v, o, h in RELATIVE_VERBS if o == op)
                    notes.append(f"{step['signal']} can fire many times; each time it fires, {prop} {how} {val}, so the change compounds")
        elif do == "change_handler":
            fn = step["handler"]
            if fn not in host.get("methods", []):
                close = difflib.get_close_matches(str(fn), host.get("methods", []), n=3)
                raise PlanError(f"{where}: '{fn}' is not a function of {host['name']}" + (f" (did you mean {', '.join(close)}?)" if close else "") + " (handlers: " + ", ".join(m for m in host.get("methods", []) if m.startswith("_on_")) + ")")
            if not re.match(r"^_on_[a-z0-9_]+$", fn) or host.get("method_args", {}).get(fn, 0) != 0:
                raise PlanError(f"{where}: {fn} cannot be replaced: only handlers (_on_...) without arguments can")
            body = body_from_then_any(step["then"], host, where, force_array=True)
            blocks.append(add_method_block(host, fn, body, mode="replace"))
            conns = ctx.clock.connections_of(ctx.project, host, fn)
            if not conns:
                notes.append(f"{fn} is not connected to any signal, so nothing calls it yet")
            for sig in conns:
                if ctx.vocab.get("signals", {}).get(sig, {}).get("repeats", True):
                    for prop, op, val in body_relatives(body):
                        how = next(h for v, o, h in RELATIVE_VERBS if o == op)
                        notes.append(f"{sig} can fire many times; each time it fires, {prop} {how} {val}, so the change compounds")
        elif do == "add_function":
            blocks.append(add_method_block(host, step["name"], body_from_then_any(step["then"], host, where)))
        elif do == "set_property":
            blocks.append(modify_block(host, step["property"], step["value"]))
        else:
            blocks.append(connect_block(host, step["signal"], step["handler"]))
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
            return ("REPLACE " if b.get("mode") == "replace" else "") + f"{b['fn']}(): {c.body_line(b)}"
        if b["op"] == "connect_signal":
            return f"{b['signal']} -> {b['method']}"
        return f"{b['property']} = {c.scene_literal(b['type'], b['value'])}"
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
        stmt = f"{b['signal']}.connect({b['method']})"
    else:
        stmt = f"{b['property']} = {c.scene_literal(b['type'], b['value'])}"
    return json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": stmt})


def canonical_replies(c, b, project, host):
    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""
    if b["op"] == "add_method" and c.is_body_frame(b):
        raw = open(c.script_path(project, host), encoding="utf-8").read()
        indent, lines, sig = c.detect_indent(raw), c.body_lines(b), c.signature(b)
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": sig + "\n" + "\n".join(indent + l for l in lines[:k + 1])}) for k in range(len(lines))]
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


def statement_text(c, st):
    if st["kind"] == "pass":
        return "does nothing"
    if st["kind"] == "print":
        return f'prints "{st["message"]}"'
    if st["kind"] == "assign":
        return f"sets {st['property']} = {c.scene_literal(st['type'], st['value'])}"
    return f"changes {st['property']}: {c.statement_line(st)}"


def handler_effect(c, host, raw, name):
    have = c.existing_function(raw, name)
    if not have:
        return "unknown"
    if hasattr(c, "parse_statement_line"):
        body = [l for l in have[1:] if l and not l.startswith("#")]
        sts = [c.parse_statement_line(l, host) for l in body]
        if sts and all(sts):
            return "; ".join(statement_text(c, st) for st in sts)
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
        out = sorted(s for s in host.get("signals", []) if sa.get(s))
        if out:
            L.append("  signals out of reach (they carry arguments): " + ", ".join(f"{s}({', '.join(sa[s])})" for s in out))
        raw = open(os.path.join(ctx.project, host["script"][len("res://"):]), encoding="utf-8").read() if host.get("script") else ""
        conns = {}
        sp = os.path.join(ctx.project, host["owner_scene"][len("res://"):])
        rel = c.node_rel(host)
        for l in open(sp, encoding="utf-8").read().splitlines() if os.path.exists(sp) else []:
            if l.startswith("[connection "):
                at = dict(re.findall(r'(\w+)="([^"]*)"', l))
                if at.get("to") == rel:
                    conns.setdefault(at.get("method"), []).append(at.get("signal"))
        L.append("  handlers already in the script (change_handler replaces the whole body of a [replaceable] one):")
        for m in host.get("methods", []):
            flag = "[replaceable]" if re.match(r"^_on_[a-z0-9_]+$", m) and host["method_args"].get(m, 0) == 0 else "[not replaceable: engine callback or has arguments]"
            L.append(f"    {m}({host['method_args'].get(m, 0)} args)   {handler_effect(c, host, raw, m)}   connected to: {', '.join(conns[m]) if m in conns else '-'}   {flag}")
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


def touched_files(host, blocks):
    return sorted({host["script"] if b["op"] == "add_method" else host["owner_scene"] for b in blocks})


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
    files = touched_files(host, blocks)
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
    "change_handler: {\"do\": \"change_handler\", \"handler\": \"_on_player_tree_entered\", \"then\": [{\"set\": {\"property\": \"speed\", \"value\": 450.0}}, {\"print\": \"ready\"}]}   (replaces the WHOLE body of a [replaceable] handler; its connections stay; the dry run shows what is discarded)"
  ],
  "values": "float 450.0 (always with a decimal point) | int 7 | bool true/false | String plain text up to 40 characters (letters, digits, space and _ . , : ! ? -) | Vector2 [x, y] | Vector3 [x, y, z] | Color [r, g, b, a] | 'by': a float for float properties, an int for int properties, the same vector for increase/decrease on Vector2/Vector3, a float for scale on a vector; never 0 for increase/decrease or 1 for scale",
  "supported_intents": [
    "when <event>, do one thing or several things in order (set, increase / decrease, scale, print, nothing)",
    "change what an existing handler does: replace the whole body of a [replaceable] _on_ handler with one or several statements",
    "set <property> to <literal> in the scene",
    "add a function that does one or several things",
    "connect <event> to an existing handler"
  ],
  "refusals": [
    "values that come from other properties or expressions: set speed to health plus 10 (a number is fixed when the plan is written)",
    "division, and relative changes on bool, String or Color properties",
    "more than 6 statements in one handler, or if / for / while / other control flow",
    "handlers with arguments, and signals that carry arguments",
    "editing one line of a handler: a change replaces the whole body (say all of it)",
    "replacing engine callbacks (_physics_process, _ready ...), functions with arguments, or deleting a handler",
    "another node, spawning or instancing scenes, timers, input",
    "a property, signal or node that is not in the catalog"
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
    "'also', 'and then', 'as well' mean a longer list in the same handler, not a second handler; a list holds at most 6 items and runs in order"
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
reg = os.path.join(ws, "state_registry.json")
if os.path.exists(reg):
    hosts = json.load(open(reg)).get("hosts", {})
    have = all("signal_args" in h and "method_args" in h for h in hosts.values()) and bool(hosts)
    print(f"  {'in place' if have else 'STALE   ':<9} {'registry data has argument counts':<42} state_registry.json" + ("" if have else "   (run registry_builder.py)"))
print("\nnothing was changed." if not missing else f"\n{len(missing)} piece(s) missing. A patch run (they are safe to repeat) fills them in; if one is BLOCKED it names the anchor.")
STATUSEOF

# ---- 3. the patch (cascade_clock.py + task_block_spec.json), then the status check
python3 ~/cascade_ws/patch_multi.py ~/cascade_ws
python3 ~/cascade_ws/patch_status.py | grep -E 'step 13|MISSING|STALE'

# ---- 4. the dry-run plan: replace the body of the existing tree_entered handler
cat << 'EOF' > ~/cascade_ws/plan_ready.json
{"intent": "when the player enters the scene, set its speed to 450 and also print ready", "node": "%Player", "assumptions": ["'enters the scene' = the existing tree_entered handler, so its body is replaced rather than a second handler added", "'speed to 450' = speed = 450.0 on the float property speed"], "steps": [{"do": "change_handler", "handler": "_on_player_tree_entered", "then": [{"set": {"property": "speed", "value": 450.0}}, {"print": "ready"}]}]}
EOF

# ---- 5. dry run: no mule, nothing real touched. Copy everything from here down and paste it back.
export GODOT="${GODOT:-godot}"
cd ~/cascade_ws
python3 cascade_plan.py catalog | grep -iE 'tree_entered|replaceable'
echo
python3 cascade_plan.py expand plan_ready.json
echo
python3 cascade_plan.py dry-run plan_ready.json
