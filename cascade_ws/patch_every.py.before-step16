import json, os, subprocess, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws + " (it came with the Step 9 script)")
try:
    if "def run_live_check" not in open(os.path.join(ws, "cascade_clock.py"), encoding="utf-8").read():
        sys.exit("STOPPED: Step 15 is not in cascade_clock.py yet. Run patch_after.py first.")
except FileNotFoundError:
    sys.exit("STOPPED: cascade_clock.py is not in " + ws)

# ------------------------------------------------------------------ the spec: the limit statement
def spec_edit(d):
    st = d["slots"]["statement"]
    st["kinds"]["limit"] = ["property", "type", "bound", "value"]
    st["limit"] = ("limit: float or int property, bound max (speed = minf(speed, 500.0)) or min (maxf); Vector2/Vector3 property, bound max_length (velocity = velocity.limit_length(800.0)); "
                   "a limit must come after a relative statement on the same property; in a handler wired to a repeating Timer every += -= and *= (factor over 1) needs a limit after it")
    d["ops"]["add_method"]["v5"] = "a statement may be a limit; GI_1 refuses an unbounded relative change in a handler wired to a repeating Timer (one_shot false)"

# ------------------------------------------------------------------ cascade_clock.py
NEW_SECTION = r'''# ---------------------------------------------------------------- Step 16: repeating timers (the limit statement, the compounding guard)
# Everything below WRAPS or SHADOWS an earlier definition, as in Steps 13 to 15.
import math
STATEMENT_KINDS = ("assign", "relative", "print", "pass", "limit")
LIMIT_OPS = {"limit_max": "max", "limit_min": "min", "limit_length": "max_length"}      # the operator the mule's message shows for a limit statement
LIMIT_FROM_BOUND = {v: k for k, v in LIMIT_OPS.items()}
VECTOR_TYPES = ("Vector2", "Vector3")

_step15_close = _close

def _close(a, b):
    """Equality for the probes; a relative slack of 1e-5 covers the 32-bit floats inside Godot's vectors."""
    if isinstance(a, list) and isinstance(b, list): return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= 1e-6 + 1e-5 * max(abs(float(a)), abs(float(b)))
    return a == b

def limit_func(st):
    return {"float": {"max": "minf", "min": "maxf"}, "int": {"max": "mini", "min": "maxi"}}[st["type"]][st["bound"]]

def limit_text(st):
    p = st["property"]
    if st["type"] in VECTOR_TYPES: return f"{p} = {p}.limit_length({fnum(st['value'])})"
    return f"{p} = {limit_func(st)}({p}, {fnum(st['value']) if st['type'] == 'float' else int(st['value'])})"

def limit_head(st, indent):
    """The start of a limit line, given to the mule in the prefill so it only completes the number."""
    if st["kind"] != "limit": return ""
    p = st["property"]
    return indent + (f"{p} = {p}.limit_length(" if st["type"] in VECTOR_TYPES else f"{p} = {limit_func(st)}({p}, ")

def apply_limit(st, cur):
    t, v, b = st["type"], st["value"], st["bound"]
    if t in ("float", "int"):
        r = min(cur, v) if b == "max" else max(cur, v)
        return int(r) if t == "int" else float(r)
    n = math.sqrt(sum(x * x for x in cur))
    return list(cur) if n <= v or n == 0 else [x * (v / n) for x in cur]

def limit_line_error(line, st):
    s, p, t, b, v = line.strip(), re.escape(st["property"]), st["type"], st["bound"], st["value"]
    want = limit_text(st)
    msg = f"type_match: this line must be {want} (self. before the property is also accepted" + ("" if t in VECTOR_TYPES else ", and min / max for minf / maxf") + ")"
    if t in VECTOR_TYPES:
        m = re.match(rf"^(?:self\.)?{p}\s*=\s*(?:self\.)?{p}\.limit_length\(\s*({NUM})\s*\)$", s)
        return None if m and float(m.group(1)) == float(v) else msg
    fname, suf = {"max": "min", "min": "max"}[b], {"float": "f", "int": "i"}[t]
    m = (re.match(rf"^(?:self\.)?{p}\s*=\s*(?:self\.)?{fname}(?:{suf})?\(\s*(?:self\.)?{p}\s*,\s*({NUM})\s*\)$", s)
         or re.match(rf"^(?:self\.)?{p}\s*=\s*(?:self\.)?{fname}(?:{suf})?\(\s*({NUM})\s*,\s*(?:self\.)?{p}\s*\)$", s))
    if not m: return msg
    txt = m.group(1)
    if t == "int": return None if re.match(r"^[-+]?\d+$", txt) and int(txt) == int(v) else msg
    return None if float(txt) == float(v) else msg

def parse_limit_line(line, host):
    s = line.strip()
    m = re.match(rf"^(?:self\.)?([A-Za-z_]\w*)\s*=\s*(?:self\.)?\1\.limit_length\(\s*({NUM})\s*\)$", s)
    if m and host["props"].get(m.group(1)) in VECTOR_TYPES:
        return {"kind": "limit", "property": m.group(1), "type": host["props"][m.group(1)], "bound": "max_length", "value": float(m.group(2))}
    m = (re.match(rf"^(?:self\.)?([A-Za-z_]\w*)\s*=\s*(min|max)(?:f|i)?\(\s*(?:self\.)?\1\s*,\s*({NUM})\s*\)$", s)
         or re.match(rf"^(?:self\.)?([A-Za-z_]\w*)\s*=\s*(min|max)(?:f|i)?\(\s*({NUM})\s*,\s*(?:self\.)?\1\s*\)$", s))
    if m and host["props"].get(m.group(1)) in ("float", "int"):
        t = host["props"][m.group(1)]
        if t == "int" and not re.match(r"^[-+]?\d+$", m.group(3)): return None
        return {"kind": "limit", "property": m.group(1), "type": t, "bound": "max" if m.group(2) == "min" else "min", "value": int(m.group(3)) if t == "int" else float(m.group(3))}
    return None

_step15_statement_line = statement_line

def statement_line(st):
    return limit_text(st) if st["kind"] == "limit" else _step15_statement_line(st)

_step15_parse_statement_line = parse_statement_line

def parse_statement_line(line, host):
    st = parse_limit_line(line, host)
    return st if st is not None else _step15_parse_statement_line(line, host)

_step15_legacy_block = legacy_block

def legacy_block(block, st):
    if st["kind"] != "limit": return _step15_legacy_block(block, st)
    b = {k: v for k, v in block.items() if k not in ("body", "message", "property", "type", "operator", "value", "mode")}
    b.update(body="assign", message="", property=st["property"], type=st["type"], value=st["value"], operator=LIMIT_FROM_BOUND[st["bound"]])
    return b

_step15_add_method_stmt_error = add_method_stmt_error

def add_method_stmt_error(stmt, block):
    if block.get("body") == "assign" and block.get("operator") in LIMIT_OPS:
        lines = stmt.split("\n")
        if len(lines) != 2: return f"type_match: stmt must be exactly two lines (the signature and one indented body line), got {len(lines)}"
        if lines[0] != signature(block): return f"type_match: line 1 must be {signature(block)}"
        if lines[1][:1] not in (" ", "\t"): return "type_match: the body line must be indented"
        st = {"kind": "limit", "property": block["property"], "type": block["type"], "bound": LIMIT_OPS[block["operator"]], "value": block["value"]}
        return limit_line_error(lines[1], st)
    return _step15_add_method_stmt_error(stmt, block)

def simulate(sts, start, n):
    vals = {k: (list(v) if isinstance(v, list) else v) for k, v in start.items()}; prints = collections.Counter()
    for _ in range(n):
        for st in sts:
            if st["kind"] == "print": prints[st["message"]] += 1
            elif st["kind"] == "assign":
                v = st["value"]
                vals[st["property"]] = (list(v) + [1.0] if st["type"] == "Color" and len(v) == 3 else (list(v) if isinstance(v, list) else v))
            elif st["kind"] == "relative": vals[st["property"]] = apply_op(st["type"], vals[st["property"]], st["operator"], st["value"])
            elif st["kind"] == "limit": vals[st["property"]] = apply_limit(st, vals[st["property"]])
    return vals, prints

# ---- where a guarded property starts in the probe: chosen so the bound engages on the SECOND firing, which shows the guard works and not only that it exists
def _vlen(v): return math.sqrt(sum(x * x for x in v))

def _one_firing(rels, x):
    for st in rels: x = apply_op(st["type"], x, st["operator"], st["value"])
    return x

def engage_start(sts, p):
    rels = [s for s in sts if s["kind"] == "relative" and s["property"] == p]
    lims = [s for s in sts if s["kind"] == "limit" and s["property"] == p]
    if not rels or not lims: return None
    t = rels[0]["type"]; base = baseline_for(t)
    if t in VECTOR_TYPES:
        lim = next((s for s in lims if s["bound"] == "max_length"), None)
        if lim is None: return None
        L, u = float(lim["value"]), [x / _vlen(base) for x in base]
        make, size = (lambda d: [x * (L - d) for x in u]), _vlen
        ok = lambda s: size(_one_firing(rels, s)) < L < size(_one_firing(rels, _one_firing(rels, s)))
        scale = max(L, 1.0)
    else:
        up = _one_firing(rels, base) > base
        lim = next((s for s in lims if s["bound"] == ("max" if up else "min")), None)
        if lim is None: return None
        B = lim["value"]
        make = (lambda d: B - d) if up else (lambda d: B + d)
        if t == "int":
            _mk = make; make = lambda d: int(round(_mk(d)))
        ok = (lambda s: _one_firing(rels, s) < B < _one_firing(rels, _one_firing(rels, s))) if up else (lambda s: _one_firing(rels, s) > B > _one_firing(rels, _one_firing(rels, s)))
        scale = max(abs(float(B)), 1.0)
    good = []
    for k in range(-120, 121):
        s = make(scale * (1.15 ** k))
        if t == "float": s = round(s, 2)
        elif t in VECTOR_TYPES: s = [round(x, 2) for x in s]
        if ok(s) and s not in good: good.append(s)
    return good[len(good) // 2] if good else None

_step15_start_values = start_values

def start_values(sts):
    out = _step15_start_values([s for s in sts if s["kind"] != "limit"])
    for p in list(out):
        es = engage_start(sts, p)
        if es is not None: out[p] = es
    return out

# ---- the compounding guard
def _needs_guard(rels):
    return any(st["operator"] in ("+=", "-=") or (st["operator"] == "*=" and float(st["value"]) > 1.0) for st in rels)

def compounding_errors(sts):
    """Why a statement list must not run on every firing of a repeating signal: a relative change that nothing bounds grows or shrinks without end."""
    out = []
    for p in dict.fromkeys(s["property"] for s in sts if s["kind"] == "relative"):
        rels = [(i, s) for i, s in enumerate(sts) if s["kind"] == "relative" and s["property"] == p]
        for _, s in rels:
            if s["operator"] == "*=" and float(s["value"]) <= 0:
                out.append(f"{p} *= {s['value']}: a zero or negative factor flips or erases the value on every firing; use a positive factor")
        if out or not _needs_guard([s for _, s in rels]): continue
        t = rels[0][1]["type"]
        if t in VECTOR_TYPES: need = "max_length"
        else: need = "max" if _one_firing([s for _, s in rels], baseline_for(t)) > baseline_for(t) else "min"
        last = max(i for i, _ in rels)
        if not any(s["kind"] == "limit" and s["property"] == p and s["bound"] == need and i > last for i, s in enumerate(sts)):
            ex = {"max": f'{{"limit": {{"property": "{p}", "max": 500.0}}}}', "min": f'{{"limit": {{"property": "{p}", "min": 0.0}}}}', "max_length": f'{{"limit": {{"property": "{p}", "max_length": 800.0}}}}'}[need]
            ops = ", ".join(sorted({f"{s['operator']} {s['value']}" for _, s in rels}))
            out.append(f"{p} is changed by {ops} on every firing and nothing bounds it ({need} limit missing after the last change); add a limit after the change, for example {ex}")
    return out

def scene_timer_handlers(project, host):
    """[(target host, signal, method)] for every cross connection that starts at this node in its owner scene."""
    fp = os.path.join(project, host["owner_scene"][len("res://"):]); out = []
    if not os.path.exists(fp): return out
    rel = node_rel(host)
    for l in open(fp, encoding="utf-8").read().splitlines():
        if l.startswith("[connection "):
            at = dict(re.findall(r'(\w+)="([^"]*)"', l))
            if at.get("from") == rel and at.get("to") != rel:
                tgt = host_by_rel(at.get("to"))
                if tgt is not None: out.append((tgt, at.get("signal"), at.get("method")))
    return out

def repeating_handler_errors(project, tgt, method, who, sts=None):
    if sts is None:
        sts = function_statements(open(script_path(project, tgt), encoding="utf-8").read(), method, tgt)
        if sts is None: return [f"GI_1 compounding: {method} on {tgt['name']} is not a plain statement list, so the harness cannot check that it is safe to run on every firing of {who}"]
    return [f"GI_1 compounding: {method} would run on every firing of {who}: {e}" for e in compounding_errors(sts)]

# ---- GI_1: statement lists with limits
def statement_errors(sts, host, spec):
    bad, s = [], spec["slots"]
    if not sts or len(sts) > MAX_STATEMENTS: return [f"block.body must hold 1 to {MAX_STATEMENTS} statements, it has {len(sts)}"]
    for i, st in enumerate(sts, 1):
        w = f"statement {i}"
        if not isinstance(st, dict) or st.get("kind") not in STATEMENT_KINDS:
            bad.append(f"{w}: kind must be one of {', '.join(STATEMENT_KINDS)}"); continue
        want = {"assign": {"kind", "property", "type", "value"}, "relative": {"kind", "property", "type", "operator", "value"}, "print": {"kind", "message"}, "pass": {"kind"}, "limit": {"kind", "property", "type", "bound", "value"}}[st["kind"]]
        if set(st) != want:
            bad.append(f"{w} ({st['kind']}): needs exactly {sorted(want)}" + (f", has {sorted(set(st) - want)} extra" if set(st) - want else "")); continue
        if st["kind"] == "print":
            if not isinstance(st["message"], str) or st["message"] == "" or not re.match(s["message"]["rx"], st["message"]): bad.append(f"{w}: message must be plain text of up to 40 characters (letters, digits, space and _ . , : ! ? -)")
        elif st["kind"] in ("assign", "relative", "limit"):
            p, t = st["property"], st["type"]
            if p not in host.get("props", {}): bad.append(f"{w}: '{p}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host.get('props', {}))) or 'none'})"); continue
            if t != host["props"][p]: bad.append(f"{w}: type {t} but the record says {p} is {host['props'][p]}"); continue
            if st["kind"] == "limit":
                if t not in ("float", "int") + VECTOR_TYPES: bad.append(f"{w}: a limit works on float, int, Vector2 and Vector3 properties, but {p} is {t}"); continue
                okb = ("max_length",) if t in VECTOR_TYPES else ("max", "min")
                if st["bound"] not in okb: bad.append(f"{w}: a limit on a {t} property uses bound {' or '.join(okb)}, not '{st['bound']}'"); continue
                vt = "float" if t in VECTOR_TYPES else t
                if not value_ok(vt, st["value"]): bad.append(f"{w}: limit value {st['value']} is not a valid {vt} for {p}"); continue
                if t in VECTOR_TYPES and st["value"] <= 0: bad.append(f"{w}: max_length must be above 0"); continue
                continue
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
        if st["kind"] == "limit":
            if not any(s2["kind"] == "relative" and s2["property"] == st["property"] for s2 in sts[:i - 1]):
                bad.append(f"statement {i}: a limit on {st['property']} needs a relative change (+=, -=, *=) on {st['property']} before it; a limit by itself bounds nothing")
            if any(s2["kind"] == "limit" and s2["property"] == st["property"] and s2["bound"] == st["bound"] for s2 in sts[:i - 1]):
                bad.append(f"statement {i}: {st['property']} already has a {st['bound']} limit earlier in the list")
    lim = {}
    for st in sts:
        if st["kind"] == "limit": lim.setdefault(st["property"], {})[st["bound"]] = st["value"]
    for p, d in lim.items():
        if "max" in d and "min" in d and not d["min"] < d["max"]: bad.append(f"{p}: the min limit {d['min']} must be below the max limit {d['max']}")
    if bad: return bad
    start = start_values(sts); final, _ = simulate(sts, start, 1)
    for p in start:
        if _close(final[p], start[p]): bad.append(f"the statements leave {p} where it started, so their net effect on it is nothing")
    return bad

# ---- GI_1 where it matters: wiring a handler to a repeating Timer, starting one, replacing a wired handler
_step15_connect_checks = connect_checks

def connect_checks(block, host, spec, project):
    bad = _step15_connect_checks(block, host, spec, project)
    if bad or not is_cross(block): return bad
    src = host_by_name(block["source"])
    if src is not None and src["node"] == "Timer":
        try: cfg = timer_config(project, src)
        except Exception: return bad
        if not cfg["one_shot"]: bad += repeating_handler_errors(project, host, block["method"], f"{src['name']} (one_shot is false)")
    return bad

_step15_add_method_checks = add_method_checks

def add_method_checks(block, host, spec, project):
    bad = _step15_add_method_checks(block, host, spec, project)
    if bad or block.get("mode") != "replace" or not isinstance(block.get("body"), list): return bad
    for src in _REG.get("hosts", {}).values():
        if src["node"] != "Timer" or src["owner_scene"] != host["owner_scene"]: continue
        try: cfg = timer_config(project, src)
        except Exception: continue
        if cfg["one_shot"]: continue
        if any(t["name"] == host["name"] and m == block["fn"] for t, _, m in scene_timer_handlers(project, src)):
            bad += repeating_handler_errors(project, host, block["fn"], f"{src['name']} (one_shot is false)", statements_of(block))
    return bad

_step15_slot1 = slot1

def slot1(block, reg, spec, project):
    host, bad = _step15_slot1(block, reg, spec, project)
    if not bad and host is not None and block.get("op") == "modify_property" and host["node"] == "Timer" and block["property"] == "one_shot" and block.get("value") is False:
        for tgt, sg, m in scene_timer_handlers(project, host):
            bad += repeating_handler_errors(project, tgt, m, f"{host['name']} once its one_shot is false")
    return host, bad

# ---- the live check for a repeating Timer: at least three firings, so the second one shows the bound
def live_window(cfg):
    return cfg["wait_time"] if cfg["one_shot"] else min(3 * cfg["wait_time"], LIVE_CAP_SECONDS)

def run_live_check(godot, project, host, src, block, sts, start, cfg):
    """The scene runs by itself for the live window: the Timer must fire on its own and the handler's effect must be the simulated one. Returns (ok, summary)."""
    name, method, wait, window = block["signal"], block["method"], cfg["wait_time"], live_window(cfg)
    types = list((src.get("signal_args") or {}).get(name, []))
    perturb = [{"prop": p, "type": next(s["type"] for s in sts if s.get("property") == p), "value": v} for p, v in start.items()] or None
    tmp = tempfile.mkdtemp(prefix="cascade_live_")
    helper, out = os.path.join(tmp, "live.gd"), os.path.join(tmp, "out.json")
    open(helper, "w").write(LIVE_GD)
    try:
        r = subprocess.run([godot, "--headless", "--path", project, "--script", helper, "--", out, host["owner_scene"], host["name"].lstrip("%"), src["name"].lstrip("%"), name, method,
                            json.dumps(sorted(host["props"])), json.dumps(types), json.dumps(perturb), repr(window)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=window + 40)
    except FileNotFoundError:
        die(f"Godot not found as '{godot}'. Use --godot /path/to/godot or: export GODOT=/path/to/godot")
    except subprocess.TimeoutExpired:
        return False, f"the live run did not finish in {window + 40:.0f} s"
    text = strip_ansi(r.stdout)
    if not os.path.exists(out): return False, "the live run could not start: " + (godot_errors(text) or text[-200:].strip())
    res = json.load(open(out))
    lines = [l for l in _segment(text, "live") if not l.startswith("WARNING")]
    prints, errs = _split_lines(lines)
    n = res["emissions"]
    if not res.get("wired"): return False, f"{method} is not wired to {src['name']}.{name} when the scene loads"
    if n == 0: return False, f"{src['name']} never fired in {window + 0.3:.1f} s although its autostart is on"
    n_min = 1 if cfg["one_shot"] else max(1, int(window / wait) - 1)
    if n < n_min: return False, f"{src['name']} fired {n} time(s) in {window + 0.3:.1f} s but a {wait} s repeating Timer should have fired at least {n_min}"
    mine = [e for e in errs if f"signal '{name}'" in e or f"::{method}" in e]
    if mine: return False, f"Godot reports an error when the Timer fires: {mine[0][:150]}"
    when = f"{n} time(s) on its own after {wait} s" if cfg["one_shot"] else f"{n} times in {window:.1f} s, {wait} s apart"
    if sts is None: return True, f"{src['name']} fired {when}; no error; the handler's effect was not asserted (its body is not a plain statement list)"
    want_vals, want_prints = simulate(sts, start, n)
    problems = []
    for p in start:
        if not _close(res["post"].get(p), want_vals[p]): problems.append(f"{p} should go {start[p]} -> {want_vals[p]} but ended at {res['post'].get(p)}")
    others = {k: [res["pre"][k], res["post"][k]] for k in res["pre"] if k not in start and res["pre"][k] != res["post"][k]}
    if others: problems.append(f"the run also changed {others}")
    if dict(collections.Counter(prints)) != dict(want_prints): problems.append(f"expected the prints {dict(want_prints) or 'none'} but the run printed {dict(collections.Counter(prints)) or 'nothing'}")
    if problems: return False, "; ".join(problems)
    parts = [f"{p} {start[p]} -> {res['post'].get(p)}" for p in start] + [f"printed {m!r} x{c}" for m, c in want_prints.items()]
    return True, f"{src['name']} fired {when}: " + (", ".join(parts) or "no effect and no error, as a pass body should")

# ---- starting a Timer that drives handlers: the live check runs when autostart goes on, with every handler already in place
def timer_start(block, host):
    return block.get("op") == "modify_property" and host["node"] == "Timer" and block["property"] == "autostart" and block.get("value") is True

def run_probe_timer(godot, project, host, block):
    cfg = timer_config(project, host)
    wired = scene_timer_handlers(project, host)
    if not wired: return {"verdict": "PASS", "mode": "timer", "summary": f"{host['name']} starts, but no handler is wired to it yet, so there is nothing to check", "warnings": []}
    if cfg["wait_time"] > LIVE_CAP_SECONDS: return {"verdict": "PASS", "mode": "timer", "summary": f"{host['name']} waits {cfg['wait_time']} s, over the {LIVE_CAP_SECONDS:.0f} s live-check limit: not run", "warnings": []}
    done, ok_all = [], True
    for tgt, sg, m in wired:
        blk = {"op": "connect_signal", "signal": sg, "method": m, "source": host["name"], "target": tgt["name"]}
        sts = function_statements(open(script_path(project, tgt), encoding="utf-8").read(), m, tgt)
        ok, summary = run_live_check(godot, project, tgt, host, blk, sts, start_values(sts) if sts else {}, cfg)
        ok_all = ok_all and ok; done.append(f"{tgt['name']}.{m}: {summary}")
    return {"verdict": "PASS" if ok_all else "FAIL", "mode": "timer", "summary": "live: " + "; ".join(done), "warnings": []}

_step15_run_probe = run_probe

def run_probe(godot, project, host, block):
    if block.get("op") == "modify_property": return run_probe_timer(godot, project, host, block)
    return _step15_run_probe(godot, project, host, block)

'''
MARK = "# ---------------------------------------------------------------- the clock\n"
OLD_PRE = '''        scaffold = sig + "\\n" + "".join(indent + l + "\\n" for l in accepted)
'''
NEW_PRE = '''        scaffold = sig + "\\n" + "".join(indent + l + "\\n" for l in accepted) + limit_head(st, indent)
'''
OLD_HOOK = '''if not a.no_verify and block["op"] in ("connect_signal", "add_method"):'''
NEW_HOOK = '''if not a.no_verify and (block["op"] in ("connect_signal", "add_method") or timer_start(block, host)):'''
CLOCK = [(MARK, NEW_SECTION + MARK), (OLD_PRE, NEW_PRE), (OLD_HOOK, NEW_HOOK)]

entries = [
 {"file": "cascade_clock.py", "marker": "def run_probe_timer", "edits": CLOCK},
 {"file": "task_block_spec.json", "done": lambda d: "limit" in d["slots"]["statement"]["kinds"], "edit": spec_edit},
]
blocked = pl.run(ws, "every", entries)
sys.exit(1 if blocked else 0)
