# ---- 1. add a script field to the registry builder, then rebuild the registry
cat << 'PEOF' > ~/cascade_ws/patch_builder.py
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if '"script": script_path' in s: sys.exit("registry_builder.py already has the script field.")
EDITS = [
 ('    var own_scene: String = n.scene_file_path if n.scene_file_path != "" else scene_path\n',
  '    var own_scene: String = n.scene_file_path if n.scene_file_path != "" else scene_path\n'
  '    var scr: Script = n.get_script()\n'
  '    var script_path: String = scr.resource_path if scr != null else ""\n'),
 ('"scene": own_scene, "owner_scene": scene_path,\n', '"scene": own_scene, "script": script_path, "owner_scene": scene_path,\n'),
 ('"scene": h["scene"], "owner_scene": h["owner_scene"],', '"scene": h["scene"], "script": h["script"], "owner_scene": h["owner_scene"],'),
]
for old, new in EDITS:
    if s.count(old) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + old[:80])
    s = s.replace(old, new, 1)
compile(s, p, "exec")
open(p + ".before-script-field", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("registry_builder.py patched (backup: registry_builder.py.before-script-field)")
PEOF
python3 ~/cascade_ws/patch_builder.py ~/cascade_ws/registry_builder.py
export GODOT="${GODOT:-godot}"
python3 ~/cascade_ws/registry_builder.py

# ---- 2. the clock
cat << 'PYEOF' > ~/cascade_ws/cascade_clock.py
#!/usr/bin/env python3
"""cascade_clock.py - Project Cascade, Step 4: the 4-slot harness clock.

  python3 cascade_clock.py BLOCK.json                    run one frame with the real mule (Gemma through bench.py)
  python3 cascade_clock.py BLOCK.json --script R.json    rehearsal: the mule's answers come from R.json (a JSON list of strings), no GPU

One frame = one task block (task_block_spec.json format), one pass through four slots:
  SLOT_1 FETCH    look up the block's address in state_registry.json            + GI_1 (address, uid, conflicts, block shape)
  SLOT_2 ROUTE    load the op definition                                        + GI_2 (VRAM and RAM-cache limits)
  SLOT_3 EXECUTE  the mule sees ONLY json: the task block and its one RAG record + GI_3 (reply shape, value, exclude list)
  SLOT_4 JUDGE    the statement is wrapped in a throwaway script and checked with godot --headless --check-only
ACK  -> one row in ledger.jsonl.   NAK -> Godot's exact error goes back into the next block, up to 3 tries.
3 NAKs -> dead_letter/<frame_id>.json.   Nothing is written to the Godot project in this step.
Exit codes: 0 ACK, 2 rejected before the mule, 3 dead letter, 1 stopped (missing file, model error, no Godot).
"""
import argparse, json, os, re, subprocess, sys, tempfile, time

def now(): return time.strftime("%Y-%m-%dT%H:%M:%S")
def log(msg): print(msg, flush=True)
def die(msg): sys.exit("STOPPED: " + msg)
def strip_ansi(s): return re.sub(r"\x1b\[[0-9;]*m", "", s)

def load(path):
    if not os.path.exists(path):
        die(f"missing {path}")
    return json.load(open(path, encoding="utf-8"))

def header_uid(project, res_path):
    fp = os.path.join(project, res_path[len("res://"):])
    if not os.path.exists(fp):
        return None
    m = re.search(r'^\[gd_scene[^\]]*\suid="(uid://[a-z0-9]+)"', open(fp, encoding="utf-8").read(), re.M)
    return m.group(1) if m else None

NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"

def value_ok(t, v):
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    if t == "bool": return isinstance(v, bool)
    if t == "int": return isinstance(v, int) and not isinstance(v, bool)
    if t == "float": return num(v)
    if t == "String": return isinstance(v, str)
    if t in ("Vector2", "Vector3", "Color"):
        n = {"Vector2": (2,), "Vector3": (3,), "Color": (3, 4)}[t]
        return isinstance(v, list) and len(v) in n and all(num(x) for x in v)
    return False

# ---------------------------------------------------------------- SLOT 1 + GI_1
def slot1(block, reg, spec, project):
    s, bad = spec["slots"], []
    for k in s:
        if k not in block: bad.append(f"block.{k} is missing")
    bad += [f"block.{k} is not a slot in the spec" for k in block if k not in s]
    if bad: return None, bad
    for k in ("frame_id", "subnet", "target", "ref"):
        if not re.match(s[k]["rx"], str(block[k])): bad.append(f"block.{k} '{block[k]}' does not match the spec")
    if block["time_slot"] not in s["time_slot"]["enum"]: bad.append(f"block.time_slot '{block['time_slot']}' not allowed")
    if block["op"] not in s["op"]["enum"]: bad.append(f"block.op '{block['op']}' not allowed")
    elif spec["ops"].get(block["op"], {}).get("status") != "locked": bad.append(f"op {block['op']} is open: its slots are not defined yet")
    if block["type"] not in s["type"]["enum"]: bad.append(f"block.type '{block['type']}' not allowed")
    if not isinstance(block["exclude"], list) or not block["exclude"] or any(e not in s["exclude"]["enum"] for e in block["exclude"]):
        bad.append("block.exclude must be a non-empty list of names from the spec")
    host = reg.get("hosts", {}).get(block["ref"])
    if host is None:
        return None, bad + [f"GI_1 addr_in_registry: {block['ref']} is not in the registry"]
    if not re.match(r"^uid://[a-z0-9]+$", host.get("uid", "")): bad.append("GI_1 uid_in_registry: host has no valid uid")
    else:
        cur = header_uid(project, host["scene"])
        if cur != host["uid"]:
            bad.append(f"GI_1 uid_in_registry: registry is stale ({host['scene']} has {cur}, registry has {host['uid']}); run registry_builder.py")
    if "script" not in host: bad.append("GI_1 registry has no 'script' field; update and re-run registry_builder.py")
    if block["subnet"] != host["subnet"]: bad.append(f"GI_1 subnet_boundary: block subnet {block['subnet']} but {block['ref']} is in {host['subnet']}")
    if block["target"] != host["name"]: bad.append(f"GI_1 addr_in_registry: {block['ref']} is {host['name']}, block says {block['target']}")
    if block["property"] not in host["props"]: bad.append(f"block.property '{block['property']}' is not in the record's props")
    elif block["type"] != host["props"][block["property"]]: bad.append(f"block.type {block['type']} but the record says {block['property']} is {host['props'][block['property']]}")
    elif not value_ok(block["type"], block["value"]): bad.append(f"block.value {block['value']} is not a valid {block['type']}")
    base = host["subnet"].split("/")[0].rsplit(".", 1)[0]
    seen = {}
    for a, h in reg["hosts"].items():
        if not a.startswith(base + ".") and h["subnet"] == host["subnet"]: bad.append(f"GI_1 no_addr_conflict: {a} is outside {host['subnet']}")
        if h["subnet"] == host["subnet"]:
            if h["name"] in seen: bad.append(f"GI_1 no_addr_conflict: name {h['name']} at {seen[h['name']]} and {a}")
            seen[h["name"]] = a
    return host, bad

# ---------------------------------------------------------------- SLOT 2 + GI_2
def gi2(spec, mule_dir, model):
    lim = spec["guards"]["GI_2"]; vram = ram = None
    cfg = os.path.join(mule_dir, "config.json")
    if os.path.exists(cfg):
        ram = json.load(open(cfg)).get("models", {}).get(model, {}).get("clip", {}).get("ram_cache")
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0: vram = float(r.stdout.strip().splitlines()[0]) / 1024
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        pass
    bad = []
    if vram is not None and vram > lim["vram_gb_max"]: bad.append(f"GI_2 vram: {vram:.1f} GB in use, limit {lim['vram_gb_max']}")
    if ram is not None and float(ram) > lim["ram_cache_gb_max"]: bad.append(f"GI_2 ram_cache: {ram} GB in config, limit {lim['ram_cache_gb_max']}")
    return bad, {"vram_gb_used": None if vram is None else round(vram, 2), "ram_cache_gb": ram}

# ---------------------------------------------------------------- SLOT 3 + GI_3
def mule_message(block, host, prev):
    rag = {k: host[k] for k in ("addr", "subnet", "uid", "node", "name", "props", "peering", "exclude")}
    msg = {"task": block, "rag": rag, "reply": {"frame_id": block["frame_id"], "target": block["target"], "stmt": ""}}
    if prev: msg["nak"] = prev
    return json.dumps(msg, separators=(",", ":"))

def parse_reply(text):
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try: obj, _ = dec.raw_decode(text[i:])
            except ValueError: continue
            if isinstance(obj, dict) and "stmt" in obj: return obj
    return None

def expr_error(t, expr, value):
    if t in ("Vector2", "Vector3", "Color"):
        m = re.match(rf"^{t}\(\s*({NUM}(?:\s*,\s*{NUM})*)\s*\)$", expr)
        if not m: return f"write the value as {t}(...) with number literals only"
        got, want = [float(x) for x in re.split(r"\s*,\s*", m.group(1))], [float(x) for x in value]
        if t == "Color":
            got += [1.0] * (4 - len(got)); want += [1.0] * (4 - len(want))
        if len(got) != len(want) or any(abs(a - b) > 1e-9 for a, b in zip(got, want)): return f"value must equal {want}, got {got}"
        return None
    if t == "bool": return None if expr == ("true" if value else "false") else f"value must be {'true' if value else 'false'}"
    if t == "int": return None if re.match(r"^[-+]?\d+$", expr) and int(expr) == value else f"value must be the integer {value}"
    if t == "float": return None if re.match(rf"^{NUM}$", expr) and float(expr) == float(value) else f"value must be the number {value}"
    if t == "String": return None if expr == json.dumps(value, ensure_ascii=False) else f"value must be the string {json.dumps(value)}"
    return f"type {t} not supported"

def gi3(reply, block, spec):
    if reply is None: return "json_parse: no JSON object with a 'stmt' key in the answer"
    if reply.get("frame_id") != block["frame_id"] or reply.get("target") != block["target"]:
        return "slot_enum_match: frame_id and target must echo the block"
    stmt = reply.get("stmt")
    if not isinstance(stmt, str) or not stmt.strip(): return "slot_enum_match: stmt must be a non-empty string"
    stmt = stmt.strip()
    if "\n" in stmt: return "slot_enum_match: stmt must be exactly one line"
    m = re.match(rf"^{re.escape(block['property'])}\s*=\s*(.+)$", stmt)
    if not m: return f"type_match: stmt must be one assignment to {block['property']}"
    err = expr_error(block["type"], m.group(1).strip(), block["value"])
    if err: return "type_match: " + err
    for name in block["exclude"]:
        chk = spec["exclude_checks"][name]
        if chk["kind"] == "regex" and re.search(chk["rx"], stmt): return f"exclude_hit: {name}"
        if chk["kind"] == "code" and re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", stmt): return f"exclude_hit: {name} (no addresses inside a statement)"
    return None

# ---------------------------------------------------------------- SLOT 4
def judge(godot, project, host, stmt):
    tmp = tempfile.mkdtemp(prefix="cascade_judge_")
    path = os.path.join(tmp, "frame.gd")
    base = f'"{host["script"]}"' if host.get("script") else host["node"]
    open(path, "w").write(f"extends {base}\n\nfunc cascade_frame() -> void:\n    {stmt}\n")
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

# ---------------------------------------------------------------- the clock
def main():
    ap = argparse.ArgumentParser(description="Run one Project Cascade frame through the 4-slot clock")
    ap.add_argument("block")
    ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws"))
    ap.add_argument("--mule-dir", default=os.path.expanduser("~/mule_bench"))
    ap.add_argument("--model", default="gemma26b")
    ap.add_argument("--godot", default=os.environ.get("GODOT", "godot"))
    ap.add_argument("--script", help="rehearsal: JSON list of mule answers, no GPU")
    ap.add_argument("--tries", type=int, default=3)
    a = ap.parse_args()
    ws = os.path.abspath(os.path.expanduser(a.ws)); project = os.path.join(ws, "engine_test")
    block = load(a.block); spec = load(os.path.join(ws, "task_block_spec.json")); reg = load(os.path.join(ws, "state_registry.json"))
    ledger = os.path.join(ws, "ledger.jsonl")
    row = {"ts": now(), "frame_id": block.get("frame_id"), "op": block.get("op"), "ref": block.get("ref"), "mode": "rehearsal" if a.script else "mule"}
    def finish(code, **kw):
        row.update(kw); open(ledger, "a", encoding="utf-8").write(json.dumps(row) + "\n"); sys.exit(code)

    log(f"FRAME {block.get('frame_id')}  {block.get('op')}  {block.get('target')}.{block.get('property')}")
    host, bad = slot1(block, reg, spec, project)
    if bad:
        log("SLOT_1 FETCH    REJECT"); [log("  " + b) for b in bad]; finish(2, status="REJECT_GI1", problems=bad)
    log(f"SLOT_1 FETCH    ok   {host['addr']} {host['name']} {host['uid']}"); log("GI_1            ok")
    bad, meas = gi2(spec, a.mule_dir, a.model)
    if bad:
        log("SLOT_2 ROUTE    REJECT"); [log("  " + b) for b in bad]; finish(2, status="REJECT_GI2", problems=bad, gi2=meas)
    log(f"SLOT_2 ROUTE    ok   op={block['op']}  (heat map: default tag; per-tool maps come later)"); log(f"GI_2            ok   {meas}")

    if a.script:
        replies = iter(load(a.script))
        def ask(msg):
            try: return next(replies), 0, 0.0
            except StopIteration: raise RuntimeError("rehearsal script ran out of answers")
    else:
        sys.path.insert(0, os.path.abspath(os.path.expanduser(a.mule_dir)))
        try: import bench
        except ImportError as e: die(f"cannot import bench.py from {a.mule_dir}: {e}")
        bench.CFG["max_tokens"] = 400
        def ask(msg): return bench.ask(a.model, [{"role": "user", "content": msg}])

    attempts, prev = [], None
    for t in range(1, a.tries + 1):
        msg = mule_message(block, host, prev)
        try: text, nt, dt = ask(msg)
        except Exception as e:
            log(f"MODEL ERROR {type(e).__name__}: {str(e)[:200]}"); finish(1, status="STOP_MODEL_ERROR", attempts=attempts, error=str(e)[:300])
        reply = parse_reply(text)
        stmt = reply["stmt"].strip() if reply and isinstance(reply.get("stmt"), str) else text.strip()[:200]
        log(f"SLOT_3 EXECUTE  try {t}/{a.tries}  ({nt} tokens, {dt:.0f}s)  stmt: {stmt.replace(chr(10), chr(92) + chr(110))}")
        err = gi3(reply, block, spec)
        if err:
            log(f"GI_3            NAK  {err}")
        else:
            log("GI_3            ok")
            ok, jerr = judge(a.godot, project, host, stmt)
            if ok:
                attempts.append({"try": t, "stmt": stmt, "status": "ACK"})
                log(f"SLOT_4 JUDGE    ACK  godot --check-only exit 0")
                log(f"RESULT: ACK in {t} {'try' if t == 1 else 'tries'} -> ledger.jsonl")
                finish(0, status="ACK", tries=t, stmt=stmt, uid=host["uid"], property=block["property"], type=block["type"], value=block["value"], attempts=attempts, gi2=meas)
            err = "judge: " + jerr
            log(f"SLOT_4 JUDGE    NAK  {jerr}")
        attempts.append({"try": t, "stmt": stmt, "status": "NAK", "error": err})
        prev = {"try": t, "stmt": stmt, "error": err}
    dl = os.path.join(ws, "dead_letter"); os.makedirs(dl, exist_ok=True)
    path = os.path.join(dl, f"{block['frame_id']}.json")
    json.dump({"block": block, "rag": host, "attempts": attempts}, open(path, "w"), indent=2)
    log(f"RESULT: DEAD LETTER after {a.tries} NAKs -> {path}")
    finish(3, status="DLQ", tries=a.tries, attempts=attempts, dead_letter=path)

if __name__ == "__main__":
    main()
PYEOF

# ---- 3. one test block and three rehearsal scripts for the mule's answers
cat << 'EOF' > ~/cascade_ws/block_0001.json
{
  "frame_id": "0x0001",
  "time_slot": "SLOT_3_EXECUTE",
  "subnet": "10.1.1.0/24",
  "op": "modify_property",
  "target": "%Player",
  "property": "velocity",
  "type": "Vector2",
  "value": [0.0, -400.0],
  "ref": "10.1.1.10",
  "exclude": ["KinematicBody2D", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"]
}
EOF
cat << 'EOF' > ~/cascade_ws/rehearsal_ok.json
[
  "{\"frame_id\":\"0x0001\",\"target\":\"%Player\",\"stmt\":\"velocity = Vector2(0.0, -400.0)\"}"
]
EOF
cat << 'EOF' > ~/cascade_ws/rehearsal_retry.json
[
  "Sure! You should set velocity to a vector.",
  "{\"frame_id\":\"0x0001\",\"target\":\"%Player\",\"stmt\":\"velocity = Vector2(0.0, -450.0)\"}",
  "{\"frame_id\":\"0x0001\",\"target\":\"%Player\",\"stmt\":\"velocity = Vector2(0.0, -400.0)\"}"
]
EOF
cat << 'EOF' > ~/cascade_ws/rehearsal_dead.json
[
  "no json here",
  "{\"frame_id\":\"0x9999\",\"target\":\"%Player\",\"stmt\":\"velocity = Vector2(0.0, -400.0)\"}",
  "{\"frame_id\":\"0x0001\",\"target\":\"%Player\",\"stmt\":\"velocity = Vector2(0.0, -400.0)\\nvar x = 1\"}"
]
EOF

# ---- 4. rehearsals (no GPU): first-try ACK, recovery after 2 NAKs, dead letter
cd ~/cascade_ws
python3 cascade_clock.py block_0001.json --script rehearsal_ok.json
python3 cascade_clock.py block_0001.json --script rehearsal_retry.json
python3 cascade_clock.py block_0001.json --script rehearsal_dead.json
