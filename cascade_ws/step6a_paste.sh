# ---- 1. add the worked example to the clock (backup: cascade_clock.py.before-example)
cat << 'PEOF' > ~/cascade_ws/patch_example.py
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "def worked_example" in s: sys.exit("cascade_clock.py already has the worked example.")
if "def apply_scene_property" not in s: sys.exit("Apply Step 5 first (patch_step5.py).")

OLD_MSG = '''def mule_message(block, host, prev):
    rag = {k: host[k] for k in ("addr", "subnet", "uid", "node", "name", "props", "peering", "exclude")}
    msg = {"task": block, "rag": rag, "reply": {"frame_id": block["frame_id"], "target": block["target"], "stmt": ""}}
    if prev: msg["nak"] = prev
    return json.dumps(msg, separators=(",", ":"))
'''
NEW_MSG = '''EXAMPLE_VALUES = {"Vector2": [[120.0, -35.5], [60.0, 10.0]], "Vector3": [[1.5, 0.0, -2.0], [3.0, 1.0, 0.5]],
                  "Color": [[0.2, 0.4, 0.6, 1.0], [0.9, 0.1, 0.3, 1.0]], "bool": [True, False], "int": [7, 3],
                  "float": [250.0, 125.0], "String": ["idle", "walk"]}

def worked_example(block, host, spec):
    """One completed reference frame: same shape as the task, a different value. None if no valid example can be made."""
    t = block["type"]
    if t not in EXAMPLE_VALUES: return None
    val = next((v for v in EXAMPLE_VALUES[t] if v != block["value"]), None)
    prop = next((k for k, pt in host["props"].items() if pt == t and k != block["property"]), block["property"])
    ex = dict(block, frame_id="0x0000", property=prop, type=t, value=val)
    reply = {"frame_id": "0x0000", "target": block["target"], "stmt": f"{prop} = {scene_literal(t, val)}"}
    if not value_ok(t, val) or gi3(reply, ex, spec) is not None: return None
    return {"task": ex, "reply": reply}

def mule_message(block, host, prev, example=None):
    rag = {k: host[k] for k in ("addr", "subnet", "uid", "node", "name", "props", "peering", "exclude")}
    msg = {}
    if example: msg["example"] = example
    msg.update({"task": block, "rag": rag, "reply": {"frame_id": block["frame_id"], "target": block["target"], "stmt": ""}})
    if prev: msg["nak"] = prev
    return json.dumps(msg, separators=(",", ":"))
'''
OLD_LOOP = '''    attempts, prev = [], None
    for t in range(1, a.tries + 1):
        msg = mule_message(block, host, prev)
'''
NEW_LOOP = '''    example = None if a.no_example else worked_example(block, host, spec)
    row["example"] = bool(example)
    log(f"EXAMPLE         {example['reply']['stmt']}  (one worked frame, JSON only)" if example else "EXAMPLE         none")
    attempts, prev = [], None
    for t in range(1, a.tries + 1):
        msg = mule_message(block, host, prev, example)
'''
OLD_ARG = '    ap.add_argument("--no-apply", action="store_true", help="check only: do not write the scene")\n'
NEW_ARG = OLD_ARG + '    ap.add_argument("--no-example", action="store_true", help="leave the worked example out of the mule message")\n'
for old, new in ((OLD_MSG, NEW_MSG), (OLD_LOOP, NEW_LOOP), (OLD_ARG, NEW_ARG)):
    if s.count(old) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + old[:90])
    s = s.replace(old, new, 1)
compile(s, p, "exec")
open(p + ".before-example", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_clock.py patched with the worked example (backup: cascade_clock.py.before-example)")
PEOF
python3 ~/cascade_ws/patch_example.py ~/cascade_ws/cascade_clock.py

# ---- 2. quick check with no GPU: the example is in the message and the clock still passes
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 cascade_clock.py block_0001.json --script rehearsal_ok.json --no-apply
