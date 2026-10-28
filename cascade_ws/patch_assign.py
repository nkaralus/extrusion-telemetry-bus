import os, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (it came with the Step 9 script)")

# ------------------------------------------------------------------ the spec: body may be assign, with three optional slots
def spec_edit(d):
    if "assign" not in d["slots"]["body"]["enum"]: d["slots"]["body"]["enum"].append("assign")
    d["ops"]["add_method"]["optional_slots"] = ["property", "type", "value"]
    d["ops"]["add_method"]["v2"] = "body may also be assign: <property> = <literal> for a registered property; property, type and value are then required, and forbidden for pass and print"

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 10: assignment bodies (<property> = <literal>)
def parse_literal(t, expr):
    """The typed value of a plain GDScript literal of type t, or None."""
    expr = expr.strip()
    if t in ("Vector2", "Vector3", "Color"):
        m = re.match(rf"^{t}\(\s*({NUM}(?:\s*,\s*{NUM})*)\s*\)$", expr)
        if not m: return None
        got = [float(x) for x in re.split(r"\s*,\s*", m.group(1))]
        return got + [1.0] * (4 - len(got)) if t == "Color" else got
    if t == "bool": return {"true": True, "false": False}.get(expr)
    if t == "int": return int(expr) if re.match(r"^[-+]?\d+$", expr) else None
    if t == "float": return float(expr) if re.match(rf"^{NUM}$", expr) else None
    if t == "String":
        try: v = json.loads(expr)
        except ValueError: return None
        return v if isinstance(v, str) else None
    return None

def perturbed_value(t, v):
    """A value of the same type that differs from v: the probe sets it before the stimulus, so a scene that already holds the target cannot hide an assignment."""
    if t == "bool": return not v
    if t == "int": return int(v) + 1
    if t == "float": return float(v) + 1.0
    if t == "String": return str(v) + "x"
    if t in ("Vector2", "Vector3"): return [float(x) + 1.0 for x in v]
    c = [float(x) for x in v] + [1.0] * (4 - len(v))
    return [c[0] + 0.25 if c[0] <= 0.5 else c[0] - 0.25] + c[1:]

def _close(a, b):
    if isinstance(a, list) and isinstance(b, list): return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool): return abs(float(a) - float(b)) < 1e-6
    return a == b

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
CLOCK = [
 (MARK, NEW_SECTION + MARK),
 # the harness writes the canonical body
 (r'''def body_line(block): return "pass" if block["body"] == "pass" else f'print("{block["message"]}")'
''',
  r'''def body_line(block):
    if block["body"] == "pass": return "pass"
    if block["body"] == "assign": return f"{block['property']} = {scene_literal(block['type'], block['value'])}"
    return f'print("{block["message"]}")'
'''),
 ('''def _norm(lines): return [x.replace("'", '"') for x in lines]\n''', '''def _norm(lines): return [re.sub(r"^self\\.", "", x.replace("'", '"')) for x in lines]\n'''),
 # GI-1: the three optional slots belong to assign and to nothing else
 (r'''    if block["body"] == "print" and block["message"] == "": bad.append("block.message must not be empty when body is print")
    if bad: return bad
    if not host.get("script")''',
  r'''    if block["body"] == "print" and block["message"] == "": bad.append("block.message must not be empty when body is print")
    if block["body"] == "assign":
        if block["message"] != "": bad.append("block.message must be empty when body is assign")
        miss = [k for k in ("property", "type", "value") if k not in block]
        if miss: bad.append("block." + ", block.".join(miss) + " must be given when body is assign")
        elif block["property"] not in host.get("props", {}): bad.append(f"block.property '{block['property']}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host.get('props', {}))) or 'none'})")
        elif block["type"] != host["props"][block["property"]]: bad.append(f"block.type {block['type']} but the record says {block['property']} is {host['props'][block['property']]}")
        elif not value_ok(block["type"], block["value"]): bad.append(f"block.value {block['value']} is not a valid {block['type']}")
        elif block["type"] == "String" and not re.match(spec["slots"]["message"]["rx"], str(block["value"])): bad.append("block.value for a String must use the plain message charset (letters, digits, space and _ . , : ! ? -), so the mule can type it safely")
    else:
        extra = [k for k in ("property", "type", "value") if k in block]
        if extra: bad.append("block." + ", block.".join(extra) + " only belong to body assign")
    if bad: return bad
    if not host.get("script")'''),
 # GI-3: exactly one assignment, typed and exact; self. accepted
 (r'''    want = body_line(block)
    if lines[1].strip() not in (want, want.replace('"', "'")): return f"type_match: the body must be {want} (single quotes are also accepted)"
    return None
''',
  r'''    want = body_line(block)
    if block["body"] == "assign":
        m = re.match(rf"^(?:self\.)?{re.escape(block['property'])}\s*=\s*(.+)$", lines[1].strip())
        if not m: return f"type_match: the body must be one assignment to {block['property']}: {want} (self. before the property is also accepted)"
        expr = m.group(1).strip()
        if block["type"] == "String" and re.match(r"^'[^'\\\"]*'$", expr): expr = '"' + expr[1:-1] + '"'     # single quotes are the same string; the harness writes double quotes
        err = expr_error(block["type"], expr, block["value"])
        return ("type_match: " + err) if err else None
    if lines[1].strip() not in (want, want.replace('"', "'")): return f"type_match: the body must be {want} (single quotes are also accepted)"
    return None
'''),
 # the mule is shown the assignment slots when there are any
 (r'''ULTRA_VIEW = {"modify_property": ["op", "property", "type", "value"], "connect_signal": ["op", "signal", "method"], "add_method": ["op", "fn", "body", "message"]}
''',
  r'''ULTRA_VIEW = {"modify_property": ["op", "property", "type", "value"], "connect_signal": ["op", "signal", "method"], "add_method": ["op", "fn", "body", "message", "property", "type", "value"]}
'''),
 (r'''             "add_method": {"task": ["frame_id", "op", "target", "fn", "args", "body", "message"], "rag": ["node", "name"]}}
''',
  r'''             "add_method": {"task": ["frame_id", "op", "target", "fn", "args", "body", "message", "property", "type", "value"], "rag": ["node", "name"]}}
'''),
 (r'''        msg = {"task": {k: block[k] for k in ULTRA_VIEW[block["op"]]}}
''', r'''        msg = {"task": {k: block[k] for k in ULTRA_VIEW[block["op"]] if k in block}}
'''),
 (r'''    return {k: block[k] for k in view["task"]}, rag
''', r'''    return {k: block[k] for k in view["task"] if k in block}, rag
'''),
 (r'''    if block["op"] == "add_method": return {"fn": block["fn"], "args": block["args"], "body": block["body"], "message": block["message"]}
''',
  r'''    if block["op"] == "add_method": return {"fn": block["fn"], "args": block["args"], "body": block["body"], "message": block["message"], **{k: block[k] for k in ("property", "type", "value") if k in block}}
'''),
 # slot 1: optional slots are allowed, not required
 (r'''    bad += [f"block.{k} is not a slot of op {block.get('op')}" for k in block if k not in want]
''',
  r'''    optional = spec["ops"].get(block.get("op"), {}).get("optional_slots", [])
    bad += [f"block.{k} is not a slot of op {block.get('op')}" for k in block if k not in want and k not in optional]
'''),
 # the probe: typed snapshots and the perturbation
 (r'''func _snap(n: Node, props: Array) -> Dictionary:
    var d := {}
    for k in props:
        d[k] = str(n.get(k))
    return d
''',
  r'''func _j(v: Variant) -> Variant:
    match typeof(v):
        TYPE_VECTOR2:
            return [v.x, v.y]
        TYPE_VECTOR3:
            return [v.x, v.y, v.z]
        TYPE_COLOR:
            return [v.r, v.g, v.b, v.a]
    return v

func _from_j(t: String, v: Variant) -> Variant:
    match t:
        "int":
            return int(v)
        "Vector2":
            return Vector2(v[0], v[1])
        "Vector3":
            return Vector3(v[0], v[1], v[2])
        "Color":
            return Color(v[0], v[1], v[2], v[3])
    return v

func _snap(n: Node, props: Array) -> Dictionary:
    var d := {}
    for k in props:
        d[k] = _j(n.get(k))
    return d
'''),
 (r'''    for t in types:
        args.append(_dummy(str(t)))
    spy = 0
    if mode == "signal":
''',
  r'''    for t in types:
        args.append(_dummy(str(t)))
    spy = 0
    if a.size() > 8:
        var pt: Variant = JSON.parse_string(a[8])
        if pt != null:
            p.set(str(pt["prop"]), _from_j(str(pt["type"]), pt["value"]))
    if mode == "signal":
'''),
 (r'''    exp = handler_expectation(project, host, block)
''',
  r'''    exp = handler_expectation(project, host, block)
    perturb = {"prop": exp[1], "type": exp[2], "value": perturbed_value(exp[2], exp[3])} if exp[0] == "assign" else None
'''),
 (r'''json.dumps(sorted(host["props"])), json.dumps(types)], stdout=subprocess.PIPE''', r'''json.dumps(sorted(host["props"])), json.dumps(types), json.dumps(perturb)], stdout=subprocess.PIPE'''),
 # the handler body expectation for connect_signal: an assignment is read back from the script
 (r'''        return ("pass",) if block["body"] == "pass" else ("print", block["message"])
''',
  r'''        if block["body"] == "assign": return ("assign", block["property"], block["type"], block["value"])
        return ("pass",) if block["body"] == "pass" else ("print", block["message"])
'''),
 (r'''    m = re.match(r'^print\((["\'])(.*)\1\)$', have[1])
    return ("print", m.group(2)) if m else ("unknown",)
''',
  r'''    m = re.match(r'^print\((["\'])(.*)\1\)$', have[1])
    if m: return ("print", m.group(2))
    m = re.match(r"^(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", have[1])
    if m and m.group(1) in host["props"]:
        val = parse_literal(host["props"][m.group(1)], m.group(2))
        if val is not None: return ("assign", m.group(1), host["props"][m.group(1)], val)
    return ("unknown",)
'''),
 # the verdict for an assignment
 (r'''    else:
        v.update(verdict="UNVERIFIED", summary=f"{where}: the handler body is not pass/print, so its effect was not asserted;''',
  r'''    elif exp[0] == "assign":
        prop, want = exp[1], exp[3]
        if exp[2] == "Color" and len(want) == 3: want = list(want) + [1.0]
        pre_n, post_n, post_c = nrm["pre"].get(prop), nrm["post"].get(prop), ctl["post"].get(prop)
        others = {k: x for k, x in eff_props.items() if k != prop}
        if not _close(post_n, want): v.update(verdict="FAIL", summary=f"{where}: expected {prop} to become {want} but the normal run ended at {post_n} (control {post_c})")
        elif _close(post_c, want): v.update(verdict="FAIL", summary=f"{where}: the control run also ended with {prop} = {want}, so the change cannot be credited to this handler")
        elif others: v.update(verdict="FAIL", summary=f"{where}: {prop} became {want} but the run also changed {others}")
        else: v.update(verdict="PASS", summary=f"{where}: {prop} was set to {pre_n} first (so an unchanged value cannot pass), ended at {post_n} in the normal run and stayed {post_c} in the control" + (f"; {nrm['emissions']} emission(s), {how}" if mode == "signal" else ""))
    else:
        v.update(verdict="UNVERIFIED", summary=f"{where}: the handler body is not pass/print/assign, so its effect was not asserted;'''),
]
entries = [
 {"file": "cascade_clock.py", "marker": "def parse_literal", "edits": CLOCK},
 {"file": "task_block_spec.json", "done": lambda d: "assign" in d["slots"]["body"]["enum"] and "optional_slots" in d["ops"]["add_method"], "edit": spec_edit},
]
sys.exit(1 if pl.run(ws, "assign", entries) else 0)
