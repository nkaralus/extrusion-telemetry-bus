# step16_paste.sh - Step 16: repeating timers (the "every" step, the limit statement, the compounding guard)
# Paste this whole block into a terminal. No GPU, no mule: the last commands are dry runs on a throwaway copy.
set -u
cd ~/cascade_ws || { echo "STOPPED: ~/cascade_ws not found"; exit 1; }
export GODOT="${GODOT:-godot}"

# ---- 1. keep the files this step replaces or edits (backups are made once)
for f in cascade_plan.py cascade_vocab.json harness_protocol.json patch_status.py patch_every.py cascade_clock.py task_block_spec.json live_watch.gd; do
  [ -f "$f" ] && [ ! -f "$f.before-step16" ] && cp "$f" "$f.before-step16"
done

# ---- 2. write patch_every.py
cat << 'PEEOF' > ~/cascade_ws/patch_every.py
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
PEEOF

# ---- 2. write cascade_plan.py
cat << 'PLANEOF' > ~/cascade_ws/cascade_plan.py
#!/usr/bin/env python3
"""cascade_plan.py - Project Cascade, Steps 11 to 16: the harness layer.

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
STEP_KEYS = {"when": {"signal", "then"}, "set_property": {"property", "value"}, "add_function": {"name", "then"}, "connect": {"signal", "handler"}, "change_handler": {"handler", "then"}, "after": {"seconds", "then"}, "every": {"seconds", "then"}}
# Step 14: "from" names the node that emits the signal (the step's node receives it). Step 15: "node" lets one step work on another node than the plan's; "timer" names the Timer an "after" uses.
OPTIONAL_KEYS = {"when": {"from", "node"}, "connect": {"from", "node"}, "set_property": {"node"}, "add_function": {"node"}, "change_handler": {"node"}, "after": {"timer", "node"}, "every": {"timer", "node"}}


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
    if set(form) == {"limit"} and isinstance(form["limit"], dict) and "property" in form["limit"] and len(form["limit"]) == 2 and (set(form["limit"]) - {"property"}) <= {"max", "min", "max_length"}:
        prop, bound = form["limit"]["property"], next(iter(set(form["limit"]) - {"property"}))
        if prop not in host["props"]:
            raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
        t = host["props"][prop]
        if t in ("Vector2", "Vector3"):
            if bound != "max_length":
                raise PlanError(f"{where}: {prop} is a {t}; limit it with max_length (the longest the vector may get)")
        elif t in ("float", "int"):
            if bound not in ("max", "min"):
                raise PlanError(f"{where}: {prop} is a {t}; limit it with max (never above) or min (never below)")
        else:
            raise PlanError(f"{where}: a limit works on float, int, Vector2 and Vector3 properties, but {prop} is {t}")
        return {"kind": "limit", "property": prop, "type": t, "bound": bound, "value": form["limit"][bound]}
    raise PlanError(f"{where}: each 'then' item must be exactly one of {{\"set\": {{\"property\": P, \"value\": V}}}}, {{\"increase\"|\"decrease\"|\"scale\": {{\"property\": P, \"by\": V}}}}, {{\"print\": \"message\"}}, {{\"nothing\": true}}, {{\"limit\": {{\"property\": P, \"max\"|\"min\"|\"max_length\": V}}}}")


def body_from_then_any(then, host, where, force_array=False):
    """A single form keeps the proven single-statement block; a list of two or more (or a replacement) becomes an array body."""
    if isinstance(then, list):
        if not then or len(then) > MAX_STATEMENTS:
            raise PlanError(f"{where}: 'then' as a list holds 1 to {MAX_STATEMENTS} items, it has {len(then)}")
        sts = [statement_from_form(f, host, f"{where}, item {i}") for i, f in enumerate(then, 1)]
        if len(sts) == 1 and sts[0]["kind"] == "limit":
            raise PlanError(f"{where}: a limit needs a relative change (increase, decrease, scale) on the same property before it; a limit by itself bounds nothing")
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


def connect_block(host, signal, method, src=None):
    b = head(host, "connect_signal")
    b.update(source=(src or host)["name"], target=host["name"], signal=signal, method=method, ref=host["addr"], exclude=list(EXC["connect_signal"]))
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
        extra = set(step) - STEP_KEYS[do] - OPTIONAL_KEYS.get(do, set()) - {"do"}
        missing = STEP_KEYS[do] - set(step)
        if extra or missing:
            raise PlanError(f"{where} ({do}): " + (f"unexpected {sorted(extra)} " if extra else "") + (f"missing {sorted(missing)}" if missing else ""))
        hh = ctx.host_named(step["node"]) if "node" in step else host
        src = hh
        if do in ("when", "connect") and "from" in step:
            src = ctx.host_named(step["from"])
            if src["name"] == hh["name"]:
                raise PlanError(f"{where}: 'from' names the plan's own node {hh['name']}; leave 'from' out for a signal of the node itself")
            if src["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {src['name']} is in {src['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
        if do in ("when", "connect") and step["signal"] not in src.get("signals", []):
            close = difflib.get_close_matches(str(step["signal"]), src.get("signals", []), n=3)
            raise PlanError(f"{where}: '{step['signal']}' is not a signal of {src['name']}" + (f" (did you mean {', '.join(close)}?)" if close else ""))
        if do == "when":
            fn = f"_on_{snake(src['name'])}_{step['signal']}"
            body = body_from_then_any(step["then"], hh, where)
            blocks.append(add_method_block(hh, fn, body))
            blocks.append(connect_block(hh, step["signal"], fn, src))
            if ctx.vocab.get("signals", {}).get(step["signal"], {}).get("repeats", True):
                for prop, op, val in body_relatives(body):
                    how = next(h for v, o, h in RELATIVE_VERBS if o == op)
                    notes.append(f"{step['signal']} can fire many times; each time it fires, {prop} {how} {val}, so the change compounds")
            if src is not hh and ctx.vocab.get("signals", {}).get(step["signal"], {}).get("group") == "never":
                notes.append(f"{src['name']}.{step['signal']} never fires by itself: the behavior check emits it, and in a running game something has to start or emit it (an 'after' step starts a Timer)")
        elif do == "change_handler":
            fn = step["handler"]
            if fn not in hh.get("methods", []):
                close = difflib.get_close_matches(str(fn), hh.get("methods", []), n=3)
                raise PlanError(f"{where}: '{fn}' is not a function of {hh['name']}" + (f" (did you mean {', '.join(close)}?)" if close else "") + " (handlers: " + ", ".join(m for m in hh.get("methods", []) if m.startswith("_on_")) + ")")
            if not re.match(r"^_on_[a-z0-9_]+$", fn) or hh.get("method_args", {}).get(fn, 0) != 0:
                raise PlanError(f"{where}: {fn} cannot be replaced: only handlers (_on_...) without arguments can")
            body = body_from_then_any(step["then"], hh, where, force_array=True)
            blocks.append(add_method_block(hh, fn, body, mode="replace"))
            conns = [sg for _, sg in ctx.clock.connections_into(ctx.project, hh, fn)]
            if not conns:
                notes.append(f"{fn} is not connected to any signal, so nothing calls it yet")
            for sig in conns:
                if ctx.vocab.get("signals", {}).get(sig, {}).get("repeats", True):
                    for prop, op, val in body_relatives(body):
                        how = next(h for v, o, h in RELATIVE_VERBS if o == op)
                        notes.append(f"{sig} can fire many times; each time it fires, {prop} {how} {val}, so the change compounds")
        elif do == "after":
            secs = step["seconds"]
            if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not (0.05 <= secs <= 3600):
                raise PlanError(f"{where}: 'seconds' must be a number between 0.05 and 3600")
            timers = [t for t in ctx.hosts() if t["node"] == "Timer"]
            if "timer" in step:
                tm = ctx.host_named(step["timer"])
                if tm["node"] != "Timer":
                    raise PlanError(f"{where}: {tm['name']} is a {tm['node']}, not a Timer")
            elif len(timers) == 1:
                tm = timers[0]
            else:
                raise PlanError(f"{where}: " + ("there is no Timer in the registry" if not timers else "name the timer with 'timer': " + ", ".join(t["name"] for t in timers)))
            if tm["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {tm['name']} is in {tm['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
            for prop, val in (("wait_time", float(secs)), ("one_shot", True), ("autostart", True)):
                if prop not in tm["props"]:
                    raise PlanError(f"{where}: {tm['name']} has no registered property '{prop}' (patch_after.py adds the Timer to props_allow.json; then run registry_builder.py)")
                blocks.append(modify_block(tm, prop, val))
            fn = f"_on_{snake(tm['name'])}_timeout"
            body = body_from_then_any(step["then"], hh, where, force_array=fn in hh.get("methods", []))
            blocks.append(add_method_block(hh, fn, body, mode="replace" if fn in hh.get("methods", []) else None))
            blocks.append(connect_block(hh, "timeout", fn, tm))
            cap = getattr(ctx.clock, "LIVE_CAP_SECONDS", 30.0)
            notes.append(f"after: {tm['name']} counts {secs} s from the moment the scene loads and fires once (one_shot); " + (f"the dry run and the run each wait about {secs} s for the live check" if secs <= cap else f"{secs} s is over the {cap:.0f} s live-check limit, so only the explicit-emit check runs"))
            if fn in hh.get("methods", []):
                notes.append(f"{fn} already exists on {hh['name']}: its whole body is replaced (the dry run shows what it did before)")
        elif do == "every":
            secs = step["seconds"]
            if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not (0.5 <= secs <= 3600):
                raise PlanError(f"{where}: 'seconds' must be a number between 0.5 and 3600 (a repeating handler runs this often for as long as the scene lives)")
            timers = [t for t in ctx.hosts() if t["node"] == "Timer"]
            if "timer" in step:
                tm = ctx.host_named(step["timer"])
                if tm["node"] != "Timer":
                    raise PlanError(f"{where}: {tm['name']} is a {tm['node']}, not a Timer")
            elif len(timers) == 1:
                tm = timers[0]
            else:
                raise PlanError(f"{where}: " + ("there is no Timer in the registry" if not timers else "name the timer with 'timer': " + ", ".join(t["name"] for t in timers)))
            if tm["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {tm['name']} is in {tm['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
            for prop, val in (("wait_time", float(secs)), ("one_shot", False), ("autostart", True)):
                if prop not in tm["props"]:
                    raise PlanError(f"{where}: {tm['name']} has no registered property '{prop}' (patch_after.py adds the Timer to props_allow.json; then run registry_builder.py)")
            fn = f"_on_{snake(tm['name'])}_timeout"
            body = body_from_then_any(step["then"], hh, where, force_array=True)
            problems = ctx.clock.compounding_errors(body["statements"])
            if problems:
                raise PlanError(f"{where} (every): this handler would run every {secs} s for as long as the scene lives, and " + "; ".join(problems))
            exists = fn in hh.get("methods", [])
            blocks.append(add_method_block(hh, fn, body, mode="replace" if exists else None))
            blocks.append(connect_block(hh, "timeout", fn, tm))
            for prop, val in (("wait_time", float(secs)), ("one_shot", False), ("autostart", True)):
                blocks.append(modify_block(tm, prop, val))
            lims = [f"{s['property']} {s['bound']} {s['value']}" for s in body["statements"] if s["kind"] == "limit"]
            cap = getattr(ctx.clock, "LIVE_CAP_SECONDS", 30.0)
            notes.append(f"every: {tm['name']} fires every {secs} s from the moment the scene loads, for as long as the scene lives" + (f"; bounded by: {', '.join(lims)}" if lims else "") + "; "
                         + (f"the dry run and the run each watch about {min(3 * secs, cap):.1f} s (at least 3 firings) in the live check" if secs <= cap else f"{secs} s is over the {cap:.0f} s live-check limit, so only the explicit-emit check runs"))
            if exists:
                notes.append(f"{fn} already exists on {hh['name']}: its whole body is replaced (the dry run shows what it did before)")
        elif do == "add_function":
            blocks.append(add_method_block(hh, step["name"], body_from_then_any(step["then"], hh, where)))
        elif do == "set_property":
            blocks.append(modify_block(hh, step["property"], step["value"]))
        else:
            blocks.append(connect_block(hh, step["signal"], step["handler"], src))
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
            if b["source"] != b["target"]:
                return f"{b['source']}.{b['signal']} -> {b['target']}.{b['method']}"
            return f"{b['signal']} -> {b['method']}"
        return f"{b['target']}.{b['property']} = {c.scene_literal(b['type'], b['value'])}"
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
        stmt = c.cross_stmt(b) if b["source"] != b["target"] else f"{b['signal']}.connect({b['method']})"
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
    if st["kind"] == "limit":
        return f"limits {st['property']}: {c.statement_line(st)}"
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
                    conns.setdefault(at.get("method"), []).append(at.get("signal") if at.get("from") == rel else f"{at.get('from')}.{at.get('signal')}")
        if host.get("script"):
            L.append("  handlers already in the script (change_handler replaces the whole body of a [replaceable] one):")
        else:
            L.append("  no script: it can emit signals for another node's handlers, but it has no handlers of its own")
        for m in host.get("methods", []):
            flag = "[replaceable]" if re.match(r"^_on_[a-z0-9_]+$", m) and host["method_args"].get(m, 0) == 0 else "[not replaceable: engine callback or has arguments]"
            L.append(f"    {m}({host['method_args'].get(m, 0)} args)   {handler_effect(c, host, raw, m)}   connected to: {', '.join(conns[m]) if m in conns else '-'}   {flag}")
    pairs = [(s, t) for t in ctx.hosts() if t.get("script") for s in ctx.hosts() if s["name"] != t["name"] and s["owner_scene"] == t["owner_scene"]]
    if pairs:
        L.append("")
        L.append("CROSS-NODE WIRING (a signal of one node runs a handler on another; {\"do\": \"when\", \"from\": <emitter>, ...} in a plan whose node is the receiver):")
        for s, t in pairs:
            sa = s.get("signal_args", {})
            known = [x for x in sorted(s.get("signals", [])) if not sa.get(x) and x in v["signals"]]
            L.append(f"  from {s['name']} ({s['node']}) into {t['name']}: handler is named _on_{c.snake(s['name'])}_<signal>")
            for x in known:
                ph = v["signals"][x].get("phrases", [])
                quoted = '", "'.join(ph[:4])
                L.append(f"      {x}" + (f'   ("{quoted}")' if ph else "") + ("   [can fire many times]" if v["signals"][x].get("repeats", True) else ""))
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


def touched_files(ctx, blocks):
    byref = {h["addr"]: h for h in ctx.hosts()}
    return sorted({byref[b["ref"]]["script"] if b["op"] == "add_method" else byref[b["ref"]]["owner_scene"] for b in blocks})


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
    files = touched_files(ctx, blocks)
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
    },
    "timeout": {
      "group": "never",
      "fires": "when a Timer runs out; it fires by itself only if the Timer starts (autostart on, which the 'after' step sets); otherwise the behavior check emits it",
      "phrases": [
        "the timer runs out",
        "the timer fires",
        "the timer goes off",
        "the timer finishes",
        "the timer times out"
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
    "change_handler: {\"do\": \"change_handler\", \"handler\": \"_on_player_tree_entered\", \"then\": [{\"set\": {\"property\": \"speed\", \"value\": 450.0}}, {\"print\": \"ready\"}]}   (replaces the WHOLE body of a [replaceable] handler; its connections stay; the dry run shows what is discarded)",
    "when ... from: {\"do\": \"when\", \"from\": \"%Timer\", \"signal\": \"timeout\", \"then\": {\"set\": {\"property\": \"speed\", \"value\": 600.0}}}   (a signal of ANOTHER node runs a new handler on the plan's node; the handler is named _on_<emitter>_<signal> and lives on the plan's node, so \"node\" is the receiver)",
    "connect ... from: {\"do\": \"connect\", \"from\": \"%Timer\", \"signal\": \"timeout\", \"handler\": \"_on_timer_timeout\"}   (connect another node's signal to a handler that already exists on the plan's node; the handler must be named _on_<emitter>_<signal>)",
    "after:        {\"do\": \"after\", \"seconds\": 2.0, \"then\": [{\"set\": {\"property\": \"speed\", \"value\": 600.0}}, {\"print\": \"go\"}]}   (a one-shot %Timer counts the seconds from the moment the scene loads, then the handler _on_timer_timeout on the plan's node runs once; \"timer\" names the Timer if there are several; if that handler already exists its whole body is replaced)",
    "node:         any step may carry \"node\": \"%Timer\" to act on another node than the plan's: {\"do\": \"set_property\", \"node\": \"%Timer\", \"property\": \"wait_time\", \"value\": 3.0}   (a Timer's settable properties are wait_time (0.05 to 3600), one_shot, autostart)",
    "every:        {\"do\": \"every\", \"seconds\": 2.0, \"then\": [{\"increase\": {\"property\": \"speed\", \"by\": 25.0}}, {\"limit\": {\"property\": \"speed\", \"max\": 500.0}}, {\"print\": \"tick\"}]}   (a repeating %Timer fires every N seconds from the moment the scene loads, for as long as the scene lives; N from 0.5 to 3600; the handler _on_timer_timeout on the plan's node runs each time, and if it already exists its whole body is replaced)",
    "limit:        {\"limit\": {\"property\": \"speed\", \"max\": 500.0}}  {\"limit\": {\"property\": \"speed\", \"min\": 0.0}}  {\"limit\": {\"property\": \"velocity\", \"max_length\": 800.0}}   (a list item; it must come AFTER an increase, decrease or scale on the same property; every increase, decrease and scale-over-1 inside an \"every\" must be followed by a limit on that property: max for growth, min for shrinking, max_length for a vector; the user names the number, never invent one)"
  ],
  "values": "float 450.0 (always with a decimal point) | int 7 | bool true/false | String plain text up to 40 characters (letters, digits, space and _ . , : ! ? -) | Vector2 [x, y] | Vector3 [x, y, z] | Color [r, g, b, a] | 'by': a float for float properties, an int for int properties, the same vector for increase/decrease on Vector2/Vector3, a float for scale on a vector; never 0 for increase/decrease or 1 for scale",
  "supported_intents": [
    "when <event>, do one thing or several things in order (set, increase / decrease, scale, print, nothing)",
    "change what an existing handler does: replace the whole body of a [replaceable] _on_ handler with one or several statements",
    "set <property> to <literal> in the scene",
    "add a function that does one or several things",
    "connect <event> to an existing handler",
    "when <event of another node> (from), do one thing or several things on the plan's node; the CROSS-NODE section of the catalog lists the pairs",
    "after <N seconds>, do one thing or several things on the plan's node (a Timer starts when the scene loads and fires once)",
    "set a property of another node (a Timer's wait_time, one_shot or autostart) with set_property and \"node\"",
    "every <N seconds>, do one thing or several things on the plan's node, with a limit on every relative change (a repeating Timer, N from 0.5)"
  ],
  "refusals": [
    "values that come from other properties or expressions: set speed to health plus 10 (a number is fixed when the plan is written)",
    "division, and relative changes on bool, String or Color properties",
    "more than 6 statements in one handler, or if / for / while / other control flow",
    "handlers with arguments, and signals that carry arguments",
    "editing one line of a handler: a change replaces the whole body (say all of it)",
    "replacing engine callbacks (_physics_process, _ready ...), functions with arguments, or deleting a handler",
    "a property, signal or node that is not in the catalog",
    "adding, removing or renaming nodes, spawning or instancing scenes, input, and nodes in another scene",
    "starting or stopping a Timer in the middle of the game, and a wait over 3600 seconds or under 0.05",
    "a statement inside a handler changes only the node that owns the handler; to change another node's property use a set_property step with \"node\"",
    "an 'every' handler whose increase, decrease or scale-over-1 has no limit after it (it would grow or shrink without end), a zero or negative scale factor in an 'every', and 'every' under 0.5 seconds",
    "stopping or pausing a repeating Timer in the middle of the game, and a handler that takes its limit from another property"
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
    "'also', 'and then', 'as well' mean a longer list in the same handler, not a second handler; a list holds at most 6 items and runs in order",
    "a signal of another node (catalog section CROSS-NODE) uses 'from': the plan's node is the RECEIVER, 'from' is the emitter, and the new handler lives on the receiver; the goal sentence names both nodes and says which one changes",
    "a signal the catalog lists as never firing by itself (a Timer's timeout) fires in the game only if the Timer starts; for 'after N seconds' use the after step, which sets wait_time, one_shot and autostart for you",
    "any step may carry 'node' to act on another node than the plan's node; the plan's node is the receiver of handlers",
    "'after', 'in N seconds', 'N seconds after the game starts' mean the after step (once); 'every N seconds', 'keep', 'each N seconds', 'repeatedly' mean the every step (N at least 0.5); the goal sentence says the Timer starts when the scene loads, whether it fires once or repeats for as long as the scene lives, and that the dry run and the run each wait a few seconds for the live check",
    "in an every step each increase, decrease or scale above 1 must be followed by a limit on the same property (max when it grows, min when it shrinks, max_length for a vector); ask the user for the cap if they did not give one, never invent a number; scale below 1 needs no limit; a limit alone is refused"
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
    },
    {
      "user": "when the timer runs out, set the player's speed to 600 and print go",
      "goal_sentence": "When %Timer runs out (timeout), %Player sets its speed to 600.0 and prints 'go'. The handler _on_timer_timeout is added to %Player and wired to %Timer; nothing starts the Timer yet, so the check emits the signal.",
      "plan": {
        "intent": "when the timer runs out, set the player's speed to 600 and print go",
        "node": "%Player",
        "assumptions": [
          "'the timer' = %Timer, the Timer node in the scene",
          "'runs out' = its timeout signal",
          "the speed change belongs to %Player, which owns the handler"
        ],
        "steps": [
          {
            "do": "when",
            "from": "%Timer",
            "signal": "timeout",
            "then": [
              {
                "set": {
                  "property": "speed",
                  "value": 600.0
                }
              },
              {
                "print": "go"
              }
            ]
          }
        ]
      }
    },
    {
      "user": "after 2 seconds, set the player's speed to 600 and print go",
      "goal_sentence": "2.0 seconds after the scene loads, %Timer fires once and %Player sets its speed to 600.0 and prints 'go'. This sets %Timer wait_time 2.0, one_shot true, autostart true, and adds the handler _on_timer_timeout to %Player wired to %Timer's timeout.",
      "plan": {
        "intent": "after 2 seconds, set the player's speed to 600 and print go",
        "node": "%Player",
        "assumptions": [
          "'after 2 seconds' = the after step: a one-shot %Timer that starts when the scene loads",
          "the speed change belongs to %Player, which owns the handler"
        ],
        "steps": [
          {
            "do": "after",
            "seconds": 2.0,
            "then": [
              {
                "set": {
                  "property": "speed",
                  "value": 600.0
                }
              },
              {
                "print": "go"
              }
            ]
          }
        ]
      }
    },
    {
      "user": "every 2 seconds add 25 to the player's speed, never above 500, and print tick",
      "goal_sentence": "Every 2.0 seconds after the scene loads, for as long as it lives, %Player's speed goes up by 25.0 but never above 500.0, and it prints 'tick'. This sets %Timer wait_time 2.0, one_shot false, autostart true and replaces/adds the handler _on_timer_timeout on %Player wired to %Timer's timeout.",
      "plan": {
        "intent": "every 2 seconds add 25 to the player's speed, never above 500, and print tick",
        "node": "%Player",
        "assumptions": [
          "'every 2 seconds' = the every step: a repeating %Timer that starts when the scene loads",
          "'never above 500' = a max limit on speed after the increase",
          "the speed change belongs to %Player, which owns the handler"
        ],
        "steps": [
          {
            "do": "every",
            "seconds": 2.0,
            "then": [
              {
                "increase": {
                  "property": "speed",
                  "by": 25.0
                }
              },
              {
                "limit": {
                  "property": "speed",
                  "max": 500.0
                }
              },
              {
                "print": "tick"
              }
            ]
          }
        ]
      }
    },
    {
      "user": "every second make the player's velocity 1.5 times bigger",
      "refusal": "That would grow without end: a repeating change needs a limit. How long may the velocity get at most? Once you give a number I can plan it as a scale by 1.5 followed by a max_length limit."
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
    ("step 14 cross-node wiring", CLOCK, "def run_probe_cross"),
    ("step 14 plan tool: when ... from", "cascade_plan.py", "OPTIONAL_KEYS"),
    ("step 15 live check and timer ranges", CLOCK, "def run_live_check"),
    ("step 15 plan tool: after, node", "cascade_plan.py", 'elif do == "after"'),
    ("step 16 limit statement and guard", CLOCK, "def compounding_errors"),
    ("step 16 plan tool: every", "cascade_plan.py", 'elif do == "every"'),
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
    ("step 14 spec: cross-node connect", "task_block_spec.json", lambda d: d["ops"]["connect_signal"].get("cross_node") is True),
    ("step 14 topology: %Timer pinned", "topology.json", lambda d: "%Timer" in d.get("pins", {})),
    ("step 16 spec: limit statement", "task_block_spec.json", lambda d: "limit" in d["slots"]["statement"]["kinds"]),
    ("step 16 vocabulary: every step", "cascade_vocab.json", lambda d: any(x.startswith("every:") for x in d["steps"])),
    ("step 15 spec: timer ranges", "task_block_spec.json", lambda d: "ranges" in d["ops"]["modify_property"]),
    ("step 15 props_allow: Timer", "props_allow.json", lambda d: "Timer" in d),
    ("step 15 vocabulary: after step", "cascade_vocab.json", lambda d: any(x.startswith("after:") for x in d["steps"])),
    ("step 14 vocabulary: timeout", "cascade_vocab.json", lambda d: "timeout" in d["signals"]),
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
scn = os.path.join(ws, "engine_test", "scenes", "main.tscn")
ok14 = os.path.exists(scn) and 'type="Timer"' in open(scn, encoding="utf-8").read()
print(f"  {'in place' if ok14 else 'MISSING ':<9} {'step 14 scene: a Timer in main.tscn':<42} engine_test/scenes/main.tscn")
if not ok14:
    missing.append(("step 14 scene", "main.tscn"))
reg = os.path.join(ws, "state_registry.json")
if os.path.exists(reg):
    hosts = json.load(open(reg)).get("hosts", {})
    have = all("signal_args" in h and "method_args" in h for h in hosts.values()) and bool(hosts)
    print(f"  {'in place' if have else 'STALE   ':<9} {'registry data has argument counts':<42} state_registry.json" + ("" if have else "   (run registry_builder.py)"))
print("\nnothing was changed." if not missing else f"\n{len(missing)} piece(s) missing. A patch run (they are safe to repeat) fills them in; if one is BLOCKED it names the anchor.")
STATUSEOF

# ---- 2. write live_watch.gd
cat << 'WATCHEOF' > ~/cascade_ws/live_watch.gd
extends SceneTree
# watch the real scene for a few seconds: does the Timer start by itself, and does the handler run when it fires?
func _initialize() -> void:
    await process_frame
    var main: Node = load("res://scenes/main.tscn").instantiate()
    root.add_child(main)
    var p: Node = main.get_node("%Player")
    var t: Timer = main.get_node("%Timer")
    var secs: float = t.wait_time + 0.5 if t.one_shot else minf(3.0 * t.wait_time, 12.0) + 0.5
    print("WATCH start: speed=", p.speed, "  timer wait=", t.wait_time, " one_shot=", t.one_shot, " running=", not t.is_stopped())
    await create_timer(secs).timeout
    print("WATCH end after ", secs, " s: speed=", p.speed, "  velocity=", p.velocity, "  timer running=", not t.is_stopped())
    quit()
WATCHEOF

# ---- 3. the patch: cascade_clock.py and task_block_spec.json (nothing in the scene or the registry changes)
python3 ~/cascade_ws/patch_every.py ~/cascade_ws
python3 ~/cascade_ws/patch_status.py | grep -E 'step 16|MISSING|STALE'

# ---- 4. the test plans
cat << 'P1EOF' > ~/cascade_ws/plan_every.json
{"intent": "every 2 seconds, add 25 to the player's speed, never above 500, and print tick", "node": "%Player", "assumptions": ["'every 2 seconds' = the every step: a repeating %Timer that starts when the scene loads", "'never above 500' = a max limit on speed after the increase", "the speed change belongs to %Player, which owns the handler"], "steps": [{"do": "every", "seconds": 2.0, "then": [{"increase": {"property": "speed", "by": 25.0}}, {"limit": {"property": "speed", "max": 500.0}}, {"print": "tick"}]}]}
P1EOF
cat << 'P2EOF' > ~/cascade_ws/plan_every_bad.json
{"intent": "every 2 seconds, add 25 to the player's speed and print tick", "node": "%Player", "assumptions": ["x"], "steps": [{"do": "every", "seconds": 2.0, "then": [{"increase": {"property": "speed", "by": 25.0}}, {"print": "tick"}]}]}
P2EOF
cat << 'P3EOF' > ~/cascade_ws/plan_every_vec.json
{"intent": "every second make the player's velocity 1.5 times bigger, never longer than 800, and print grow", "node": "%Player", "assumptions": ["'1.5 times bigger' = scale by 1.5", "'never longer than 800' = a max_length limit on the vector velocity"], "steps": [{"do": "every", "seconds": 1.0, "then": [{"scale": {"property": "velocity", "by": 1.5}}, {"limit": {"property": "velocity", "max_length": 800.0}}, {"print": "grow"}]}]}
P3EOF

# ---- 5. no GPU, nothing real touched. Copy everything from here down and paste it back.
cd ~/cascade_ws
python3 cascade_plan.py catalog | grep -E '^every:|^limit:|every <N|  - an .every'
echo
python3 cascade_plan.py expand plan_every.json | grep -E '"op"|"property"|"kind"|"bound"|"mode"|"value"|NOTE'
echo
echo "=== dry run: plan_every.json (the live check watches about 6 s, twice: this takes about 12 s)"
python3 cascade_plan.py dry-run plan_every.json
echo
echo "=== dry run: plan_every_bad.json (must be refused, naming speed)"
python3 cascade_plan.py dry-run plan_every_bad.json
echo
echo "=== dry run: plan_every_vec.json (optional second test: a Vector2 with limit_length)"
python3 cascade_plan.py dry-run plan_every_vec.json
