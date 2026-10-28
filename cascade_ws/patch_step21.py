#!/usr/bin/env python3
"""Step 21 patcher (idempotent): adds add_camera to task_block_spec.json, wires cascade_step21 into cascade_clock.py,
and updates cascade_plan.py."""
import argparse, json, os, shutil, sys, time
ap = argparse.ArgumentParser(); ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws")); ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))
OP = "add_camera"; BASE = ["frame_id", "time_slot", "subnet", "op", "target", "ref", "exclude", "name", "parent"]; OPT = ["zoom", "smoothing", "smoothing_speed"]

def backup(p, tag):
    bak = f"{p}.{tag}.bak"
    if os.path.exists(bak): bak += "." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(p, bak); return os.path.basename(bak)
def put(p, text):
    compile(text, p, "exec") if p.endswith(".py") else None
    tmp = p + ".cascade_tmp"; open(tmp, "w", encoding="utf-8").write(text); os.replace(tmp, p)

# ---- spec
sp = os.path.join(ws, "task_block_spec.json"); spec = json.load(open(sp))
if OP in spec["slots"]["op"]["enum"] and OP in spec["ops"]: print("spec: already patched")
else:
    backup(sp, "step21")
    if OP not in spec["slots"]["op"]["enum"]: spec["slots"]["op"]["enum"].append(OP)
    spec["slots"].update({"zoom": {"type": "array", "items": "number", "length": 2, "range": [0.1, 8.0]}, "smoothing": {"type": "bool"}, "smoothing_speed": {"type": "float", "range": [0.5, 30.0]}})
    spec["ops"][OP] = {"status": "locked", "slots": BASE, "optional_slots": OPT,
        "v1": "the harness adds a Camera2D (name, parent) to the scene of the block's host; no mule call. parent must be the mover."}
    json.dump(spec, open(sp, "w"), indent=2); print("spec: patched")

# ---- clock
cp = os.path.join(ws, "cascade_clock.py"); src = open(cp, encoding="utf-8").read()
if "cascade_step21" in src: print("clock: already patched")
else:
    A1 = "# ---------------------------------------------------------------- the clock\ndef main():"
    A2 = '    if block["op"] == "add_wall": return _s20.run_frame(a, ws, project, host, block, row, finish)\n'
    if "cascade_step20" not in src: sys.exit("STOPPED: Step 20 is not wired into the clock; run step20_paste.sh first")
    if src.count(A1) != 1 or src.count(A2) != 1: sys.exit("STOPPED: the clock does not have the expected anchors; nothing written")
    B1 = ("# ---------------------------------------------------------------- Step 21: add_camera (Camera2D that follows the player; see cascade_step21.py)\n"
          "import cascade_step21 as _s21\n_s21.bind(sys.modules[__name__])\n_step20_slot1 = slot1\n\n"
          "def slot1(block, reg, spec, project): return _s21.slot1(_step20_slot1, block, reg, spec, project)\n\n")
    B2 = A2 + '    if block["op"] == "add_camera": return _s21.run_frame(a, ws, project, host, block, row, finish)\n'
    backup(cp, "step21"); put(cp, src.replace(A1, B1 + A1).replace(A2, B2)); print("clock: patched")

# ---- plan
pp = os.path.join(ws, "cascade_plan.py")
if os.path.exists(pp):
    psrc = new = open(pp, encoding="utf-8").read(); done = []
    SCHEMA = [
        ('"add_wall": {"name", "size"}', '"add_wall": {"name", "size"}, "add_camera": {"name"}', '"add_camera": {"name"}'),
        ('"add_wall": {"at", "parent", "node"}', '"add_wall": {"at", "parent", "node"}, "add_camera": {"zoom", "smoothing", "speed", "node"}', '"add_camera": {"zoom", "smoothing", "speed", "node"}')
    ]
    for old, rep, mark in SCHEMA:
        if mark not in new and old in new:
            new = new.replace(old, rep); done.append("schema " + mark.split(":")[0])
    EXPAND_ANCHOR = '        elif do == "when":\n'
    EXPAND = '''        elif do == "add_camera":
            if not isinstance(step["name"], str):
                raise PlanError(f"{where}: 'name' must be text")
            if "zoom" in step and not (isinstance(step["zoom"], list) and len(step["zoom"]) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in step["zoom"])):
                raise PlanError(f"{where}: 'zoom' must be [x, y], two numbers")
            if "smoothing" in step and not isinstance(step["smoothing"], bool):
                raise PlanError(f"{where}: 'smoothing' must be true or false")
            if "speed" in step and (isinstance(step["speed"], bool) or not isinstance(step["speed"], (int, float))):
                raise PlanError(f"{where}: 'speed' must be a number")
            blk = head(hh, "add_camera")
            blk.update(target=hh["name"], ref=hh["addr"], exclude=list(EXC.get("add_camera", EXC["add_node"])), name=step["name"], parent=hh["name"])
            for k, bk in (("zoom", "zoom"), ("smoothing", "smoothing"), ("speed", "smoothing_speed")):
                if k in step: blk[bk] = step[k]
            blocks.append(blk)
            notes.append(f"add_camera: %{step['name']} (Camera2D) follows {hh['name']} in {hh['owner_scene']}")
'''
    DESC_ANCHOR = '        if b["op"] == "connect_signal":\n            if b["source"] != b["target"]:\n'
    DESC = '''        if b["op"] == "add_camera":
            z = b.get("zoom", [1, 1])
            return f"ADD CAMERA %{b['name']} on {b['parent']} (zoom {z[0]:g}x{z[1]:g}, " + (f"smoothing {b.get('smoothing_speed', 5.0):g})" if b.get("smoothing", True) else "no smoothing)")
'''
    CANON_ANCHOR = '    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""\n'
    CANON = '''    if b["op"] == "add_camera":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
'''
    if 'do == "add_camera"' not in new and EXPAND_ANCHOR in new:
        new = new.replace(EXPAND_ANCHOR, EXPAND + EXPAND_ANCHOR); done.append("expand_plan")
    if "ADD CAMERA %" not in new and DESC_ANCHOR in new:
        new = new.replace(DESC_ANCHOR, DESC + DESC_ANCHOR); done.append("describe")
    if 'b["op"] == "add_camera"' not in new and CANON_ANCHOR in new:
        new = new.replace(CANON_ANCHOR, CANON_ANCHOR + CANON); done.append("canonical_replies")
    if new != psrc:
        bak = backup(pp, "step21"); put(pp, new); print("cascade_plan.py: patched (" + ", ".join(done) + f"); backup {bak}")
    else: print("cascade_plan.py: already patched")

pf = os.path.join(ws, "plan_camera.json")
text = ('{\n  "intent": "make the camera follow the player",\n  "node": "%Player",\n  "steps": [\n'
        '    {"do": "add_camera", "name": "Camera", "zoom": [1, 1], "smoothing": true, "speed": 5.0}\n  ]\n}\n')
if not os.path.exists(pf) or open(pf, encoding="utf-8").read() != text:
    open(pf, "w", encoding="utf-8").write(text); print("plan_camera.json: written")

