import os, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (it came with the Step 9 script)")

# ------------------------------------------------------------------ the spec: the operator slot
def spec_edit(d):
    d["slots"]["operator"] = {"type": "string", "enum": ["=", "+=", "-=", "*="], "default": "="}
    opt = d["ops"]["add_method"]["optional_slots"]
    if "operator" not in opt: opt.append("operator")
    d["ops"]["add_method"]["v3"] = "with body assign, operator may be = (default), +=, -= or *=; value is then the operand (float or int for numbers, the same vector for + -, a float for vector *)"

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 12: relative math in handlers (+=, -=, *=)
RELATIVE_TYPES = ("float", "int", "Vector2", "Vector3")

def operand_type(t, op):
    """The type of the operand: a vector times a scalar takes a float."""
    return "float" if (op == "*=" and t in ("Vector2", "Vector3")) else t

def is_identity(op, t, v):
    """An operand that changes nothing: += 0, -= 0, *= 1."""
    if op in ("+=", "-="): return all(float(x) == 0.0 for x in (v if isinstance(v, list) else [v]))
    return op == "*=" and float(v) == 1.0

def apply_op(t, cur, op, operand):
    if t in ("float", "int"):
        r = cur + operand if op == "+=" else cur - operand if op == "-=" else cur * operand
        return int(r) if t == "int" else float(r)
    if op == "*=": return [x * operand for x in cur]
    return [x + y if op == "+=" else x - y for x, y in zip(cur, operand)]

def fold(t, base, op, operand, n):
    cur = base
    for _ in range(n): cur = apply_op(t, cur, op, operand)
    return cur

def baseline_for(t):
    return {"float": 100.0, "int": 100, "Vector2": [10.0, 20.0], "Vector3": [10.0, 20.0, 30.0]}[t]

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
CLOCK = [
 (MARK, NEW_SECTION + MARK),
 # the harness writes the canonical compound form
 (r'''def body_line(block):
    if block["body"] == "pass": return "pass"
    if block["body"] == "assign": return f"{block['property']} = {scene_literal(block['type'], block['value'])}"
    return f'print("{block["message"]}")'
''',
  r'''def body_line(block):
    if block["body"] == "pass": return "pass"
    if block["body"] == "assign":
        op = block.get("operator", "=")
        return f"{block['property']} {op} {scene_literal(operand_type(block['type'], op), block['value'])}"
    return f'print("{block["message"]}")'
'''),
 # GI-1: operator and operand rules
 (r'''        elif not value_ok(block["type"], block["value"]): bad.append(f"block.value {block['value']} is not a valid {block['type']}")
        elif block["type"] == "String" and not re.match(spec["slots"]["message"]["rx"], str(block["value"])): bad.append("block.value for a String must use the plain message charset (letters, digits, space and _ . , : ! ? -), so the mule can type it safely")
    else:
        extra = [k for k in ("property", "type", "value") if k in block]
''',
  r'''        elif block.get("operator", "=") not in spec["slots"]["operator"]["enum"]: bad.append(f"block.operator '{block.get('operator')}' not allowed (one of {', '.join(spec['slots']['operator']['enum'])})")
        elif block.get("operator", "=") != "=" and block["type"] not in RELATIVE_TYPES: bad.append(f"block.operator {block['operator']} works on {', '.join(RELATIVE_TYPES)} properties, but {block['property']} is {block['type']}")
        elif not value_ok(operand_type(block["type"], block.get("operator", "=")), block["value"]): bad.append(f"block.value {block['value']} is not a valid {operand_type(block['type'], block.get('operator', '='))} operand for {block.get('operator', '=')} on {block['property']}")
        elif block.get("operator", "=") != "=" and is_identity(block["operator"], block["type"], block["value"]): bad.append(f"block.value {block['value']} with {block['operator']} changes nothing; a handler that does nothing is not what was asked for")
        elif block.get("operator", "=") == "=" and block["type"] == "String" and not re.match(spec["slots"]["message"]["rx"], str(block["value"])): bad.append("block.value for a String must use the plain message charset (letters, digits, space and _ . , : ! ? -), so the mule can type it safely")
    else:
        extra = [k for k in ("property", "type", "value", "operator") if k in block]
'''),
 # GI-3: compound, self. and long forms
 (r'''        m = re.match(rf"^(?:self\.)?{re.escape(block['property'])}\s*=\s*(.+)$", lines[1].strip())
        if not m: return f"type_match: the body must be one assignment to {block['property']}: {want} (self. before the property is also accepted)"
''',
  r'''        op, prop, s1 = block.get("operator", "="), re.escape(block["property"]), lines[1].strip()
        ot = operand_type(block["type"], op)
        if op == "=": m = re.match(rf"^(?:self\.)?{prop}\s*=\s*(.+)$", s1)
        else: m = re.match(rf"^(?:self\.)?{prop}\s*{re.escape(op)}\s*(.+)$", s1) or re.match(rf"^(?:self\.)?{prop}\s*=\s*(?:self\.)?{prop}\s*{re.escape(op[0])}\s*(.+)$", s1)
        if not m: return f"type_match: the body must be one statement on {block['property']}: {want} (self. before the property is also accepted" + (f", and the long form {block['property']} = {block['property']} {op[0]} operand" if op != "=" else "") + ")"
'''),
 (r'''        err = expr_error(block["type"], expr, block["value"])
        return ("type_match: " + err) if err else None
''',
  r'''        err = expr_error(ot, expr, block["value"])
        return ("type_match: " + err) if err else None
'''),
 # what the mule is shown, and what the ledger says
 (r'''"add_method": ["op", "fn", "body", "message", "property", "type", "value"]}''', r'''"add_method": ["op", "fn", "body", "message", "property", "type", "value", "operator"]}'''),
 (r'''"add_method": {"task": ["frame_id", "op", "target", "fn", "args", "body", "message", "property", "type", "value"], "rag": ["node", "name"]}}''',
  r'''"add_method": {"task": ["frame_id", "op", "target", "fn", "args", "body", "message", "property", "type", "value", "operator"], "rag": ["node", "name"]}}'''),
 (r'''"message": block["message"], **{k: block[k] for k in ("property", "type", "value") if k in block}}''',
  r'''"message": block["message"], **{k: block[k] for k in ("property", "type", "value", "operator") if k in block}}'''),
 # the expectation of a handler
 (r'''        if block["body"] == "assign": return ("assign", block["property"], block["type"], block["value"])
''',
  r'''        if block["body"] == "assign":
            if block.get("operator", "=") != "=": return ("relative", block["property"], block["type"], block["operator"], block["value"])
            return ("assign", block["property"], block["type"], block["value"])
'''),
 (r'''    m = re.match(r"^(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", have[1])
    if m and m.group(1) in host["props"]:
''',
  r'''    m = re.match(r"^(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*(\+=|-=|\*=)\s*(.+)$", have[1]) or re.match(r"^(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:self\.)?\1\s*([+\-*])\s*(.+)$", have[1])
    if m and m.group(1) in host["props"] and host["props"][m.group(1)] in RELATIVE_TYPES:
        rop = m.group(2) if m.group(2).endswith("=") else m.group(2) + "="
        rval = parse_literal(operand_type(host["props"][m.group(1)], rop), m.group(3))
        if rval is not None: return ("relative", m.group(1), host["props"][m.group(1)], rop, rval)
    m = re.match(r"^(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", have[1])
    if m and m.group(1) in host["props"]:
'''),
 # the probe: a known baseline and isolation for relative handlers
 (r'''    perturb = {"prop": exp[1], "type": exp[2], "value": perturbed_value(exp[2], exp[3])} if exp[0] == "assign" else None
''',
  r'''    if exp[0] == "assign": perturb = {"prop": exp[1], "type": exp[2], "value": perturbed_value(exp[2], exp[3])}
    elif exp[0] == "relative": perturb = {"prop": exp[1], "type": exp[2], "value": baseline_for(exp[2])}
    else: perturb = None
'''),
 (r'''json.dumps(types), json.dumps(perturb)], stdout=subprocess.PIPE''', r'''json.dumps(types), json.dumps(perturb), "1" if exp[0] == "relative" else "0"], stdout=subprocess.PIPE'''),
 (r'''    spy = 0
    if a.size() > 8:
        var pt: Variant = JSON.parse_string(a[8])
''',
  r'''    spy = 0
    if a.size() > 9 and a[9] == "1":
        p.set_process(false)
        p.set_physics_process(false)
        for sg in p.get_signal_list():
            for cn in p.get_signal_connection_list(str(sg["name"])):
                var cb2: Callable = cn["callable"]
                if cb2.get_object() == p and str(cb2.get_method()).begins_with("_on_") and not (mode == "signal" and str(sg["name"]) == nm and str(cb2.get_method()) == a[5]):
                    p.disconnect(str(sg["name"]), cb2)
    if a.size() > 8:
        var pt: Variant = JSON.parse_string(a[8])
'''),
 (r'''    elif exp[0] == "assign":
        prop, want = exp[1], exp[3]
''',
  r'''    elif exp[0] == "relative":
        prop, rop, operand = exp[1], exp[3], exp[4]
        base = baseline_for(exp[2]); n = 1 if mode == "call" else nrm["emissions"]
        want = fold(exp[2], base, rop, operand, n)
        post_n, post_c = nrm["post"].get(prop), ctl["post"].get(prop)
        others = {k: x for k, x in eff_props.items() if k != prop}
        what = f"{prop} {rop} {operand} x{n}"
        if not _close(post_n, want): v.update(verdict="FAIL", summary=f"{where}: from {base}, {what} should give {want} but the normal run ended at {post_n} (control {post_c})")
        elif not _close(post_c, base): v.update(verdict="FAIL", summary=f"{where}: the control run moved {prop} from {base} to {post_c}, so the change cannot be credited to this handler")
        elif others: v.update(verdict="FAIL", summary=f"{where}: {prop} changed as expected but the run also changed {others}")
        else: v.update(verdict="PASS", summary=f"{where}: {prop} was set to {base} first, {rop} {operand} ran {n} time(s) and gave {post_n}; the control stayed at {post_c}" + (f"; {how}" if mode == "signal" else ""))
    elif exp[0] == "assign":
        prop, want = exp[1], exp[3]
'''),
 (r"""the handler body is not pass/print/assign, so its effect was not asserted;""", r"""the handler body is not pass, print, an assignment or a compound statement (+=, -=, *=), so its effect was not asserted;"""),
]
entries = [
 {"file": "cascade_clock.py", "marker": "def apply_op", "edits": CLOCK},
 {"file": "task_block_spec.json", "done": lambda d: "operator" in d["slots"] and "operator" in d["ops"]["add_method"].get("optional_slots", []), "edit": spec_edit},
]
sys.exit(1 if pl.run(ws, "relative", entries) else 0)
