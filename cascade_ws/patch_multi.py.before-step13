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
