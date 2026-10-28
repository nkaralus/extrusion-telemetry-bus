# ---- 1. keep the files this step replaces
cd ~/cascade_ws
for f in cascade_plan.py cascade_vocab.json harness_protocol.json patch_status.py; do [ -f $f ] && [ ! -f $f.before-step12 ] && cp $f $f.before-step12; done

# ---- 2. the patch (clock and spec, per file), the replacement plan tool, vocabulary, protocol and status script
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
cat << 'PATCHEOF' > ~/cascade_ws/patch_relative.py
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
PATCHEOF
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
STEP_KEYS = {"when": {"signal", "then"}, "set_property": {"property", "value"}, "add_function": {"name", "then"}, "connect": {"signal", "handler"}}


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


def head(host, op):
    return {"frame_id": None, "time_slot": "SLOT_3_EXECUTE", "subnet": host["subnet"], "op": op}


def add_method_block(host, fn, body):
    b = head(host, "add_method")
    b.update(target=host["name"], fn=fn, args=[], returns="void", body=body["body"], message=body["message"])
    for k in ("property", "type", "operator", "value"):
        if k in body:
            b[k] = body[k]
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
            body = body_from_then(step["then"], host, where)
            blocks.append(add_method_block(host, fn, body))
            blocks.append(connect_block(host, step["signal"], fn))
            if body.get("operator", "=") != "=" and ctx.vocab.get("signals", {}).get(step["signal"], {}).get("repeats", True):
                how = next(h for v, o, h in RELATIVE_VERBS if o == body["operator"])
                notes.append(f"{step['signal']} can fire many times; each time it fires, {body['property']} {how} {body['value']}, so the change compounds")
        elif do == "add_function":
            blocks.append(add_method_block(host, step["name"], body_from_then(step["then"], host, where)))
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
            return f"{b['fn']}(): {c.body_line(b)}"
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
                reply = canonical_reply(c, b, os.path.join(t, "engine_test"), host)
            except (ValueError, TypeError, KeyError):
                reply = json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "x"})     # the block is malformed: GI-1 refuses it with its own message before any answer is read
            json.dump([reply], open(sp, "w"))
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


def handler_effect(c, host, raw, name):
    have = c.existing_function(raw, name)
    if not have:
        return "unknown"
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
        L.append("  handlers already in the script (a handler's body cannot be changed by a plan):")
        for m in host.get("methods", []):
            L.append(f"    {m}({host['method_args'].get(m, 0)} args)   {handler_effect(c, host, raw, m)}   connected to: {', '.join(conns[m]) if m in conns else '-'}")
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
    "              then is exactly one of  {\"set\": {\"property\": P, \"value\": V}}  {\"print\": \"message\"}  {\"nothing\": true}",
    "              or  {\"increase\": {\"property\": \"speed\", \"by\": 50.0}}  {\"decrease\": {\"property\": \"speed\", \"by\": 10.0}}  {\"scale\": {\"property\": \"velocity\", \"by\": 2.0}}   (+=, -=, *= on float, int, Vector2, Vector3 properties)",
    "set_property: {\"do\": \"set_property\", \"property\": \"velocity\", \"value\": [0.0, -400.0]}   (the starting value stored in the scene)",
    "add_function: {\"do\": \"add_function\", \"name\": \"reset_speed\", \"then\": {\"set\": {\"property\": \"speed\", \"value\": 300.0}}}   (a function, not connected)",
    "connect:      {\"do\": \"connect\", \"signal\": \"hidden\", \"handler\": \"_on_player_hidden\"}   (to a handler that already exists)"
  ],
  "values": "float 450.0 (always with a decimal point) | int 7 | bool true/false | String plain text up to 40 characters (letters, digits, space and _ . , : ! ? -) | Vector2 [x, y] | Vector3 [x, y, z] | Color [r, g, b, a] | 'by': a float for float properties, an int for int properties, the same vector for increase/decrease on Vector2/Vector3, a float for scale on a vector; never 0 for increase/decrease or 1 for scale",
  "supported_intents": [
    "when <event>, set <property> to <literal>",
    "when <event>, increase / decrease <property> by <number or vector>",
    "when <event>, scale <property> by <factor> (double, halve, 1.5 times ...)",
    "when <event>, print <message>",
    "when <event>, do nothing (a placeholder handler)",
    "set <property> to <literal> in the scene",
    "add a function that sets, changes, prints or does nothing",
    "connect <event> to an existing handler"
  ],
  "refusals": [
    "several actions in one handler (multi-statement bodies are an open slot)",
    "values that come from other properties or expressions: set speed to health plus 10, half of the current speed written as a formula (use scale 0.5 instead)",
    "division, and relative changes on bool, String or Color properties",
    "handlers with arguments, and signals that carry arguments",
    "another node, spawning or instancing scenes, timers, input",
    "a property, signal or node that is not in the catalog",
    "changing the body of a handler that already exists (editing is an open slot)"
  ]
}
VOCABEOF
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
    "never edit an existing handler's body: say it is an open slot",
    "if a dry run or run output is missing from the user's message, say so and ask for it; do not assume it passed",
    "after any run, the catalog is stale: ask for a fresh one before the next plan",
    "a relative change (increase, decrease, scale) on a signal the catalog marks [can fire many times] compounds each time; say so in plain words before the user says go, and offer a once-per-life signal or an absolute value as the alternative",
    "'double', 'halve', 'twice', '1.5 times' are scale steps; 'add', 'plus', 'more', 'increase by' are increase; 'minus', 'less', 'reduce by' are decrease; never turn them into an absolute number you computed yourself"
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
      "user": "when it enters the scene, set the speed to 450 and print 'fast'",
      "refusal": "That is two actions in one handler, which is an open slot. I can do one of them on tree_entered, or put them on two different signals (for example set the speed on tree_entered and print on ready)."
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
    }
  ]
}
PROTOEOF
python3 ~/cascade_ws/patch_relative.py ~/cascade_ws
python3 ~/cascade_ws/patch_status.py | grep -E 'step 12|MISSING|STALE'

# ---- 3. your intent, and a second plan, as compact plans
export GODOT="${GODOT:-godot}"
cat << 'EOF' > plan_rect.json
{"intent": "when the player's item rect changes, increase its speed by 50", "node": "%Player", "assumptions": ["'item rect changes' = the item_rect_changed signal", "'by 50' = += 50.0 on the float property speed"], "steps": [{"do": "when", "signal": "item_rect_changed", "then": {"increase": {"property": "speed", "by": 50.0}}}]}
EOF
cat << 'EOF' > plan_more.json
{"intent": "double the velocity when the mouse enters, and lower the speed by 10 when it leaves", "node": "%Player", "assumptions": ["'double' = scale by 2.0", "'the mouse enters / leaves' = mouse_entered / mouse_exited"], "steps": [{"do": "when", "signal": "mouse_entered", "then": {"scale": {"property": "velocity", "by": 2.0}}}, {"do": "when", "signal": "mouse_exited", "then": {"decrease": {"property": "speed", "by": 10.0}}}]}
EOF

# ---- 4. no GPU, nothing real touched: a full dry run of your intent, then of the second plan
python3 cascade_plan.py dry-run plan_rect.json
echo
python3 cascade_plan.py dry-run plan_more.json
