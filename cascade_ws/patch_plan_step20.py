#!/usr/bin/env python3
"""Step 20 plan layer (idempotent): wires add_wall into cascade_plan.py and writes plan_wall.json."""
import argparse, json, os, shutil, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws"))
ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))

# 1. Always write plan_wall.json
text = ('{\n  "intent": "place a solid wall at 600, 0 to block the player",\n  "node": "%Player",\n  "steps": [\n'
        '    {"do": "add_wall", "name": "Wall", "at": [600, 0], "size": [32, 256]}\n  ]\n}\n')
json.loads(text)
pp = os.path.join(ws, "plan_wall.json")
if not os.path.exists(pp) or open(pp, encoding="utf-8").read() != text:
    open(pp, "w", encoding="utf-8").write(text)
    print("plan_wall.json: written")
else:
    print("plan_wall.json: already there")

# 2. Patch cascade_plan.py
path = os.path.join(ws, "cascade_plan.py")
src = open(path, encoding="utf-8").read()
new = src

SCHEMA = [
    ('"add_node": {"class", "name"}', '"add_node": {"class", "name"}, "add_wall": {"name", "size"}', '"add_wall": {"name", "size"}'),
    ('"add_node": {"at", "size", "parent", "node"}', '"add_node": {"at", "size", "parent", "node"}, "add_wall": {"at", "parent", "node"}', '"add_wall": {"at", "parent", "node"}')
]

EXPAND_ANCHOR = '        elif do == "when":\n'
EXPAND = '''        elif do == "add_wall":
            if not isinstance(step["name"], str):
                raise PlanError(f"{where}: 'name' must be text")
            for k, v in (("at", step.get("at", [0, 0])), ("size", step["size"])):
                if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
                    raise PlanError(f"{where}: '{k}' must be " + ("[x, y]" if k == "at" else "[width, height]") + ", two numbers")
            blk = head(hh, "add_wall")
            blk.update(
                target=hh["name"],
                ref=hh["addr"],
                exclude=list(EXC.get("add_wall", EXC["add_node"])),
                name=step["name"],
                parent=step.get("parent", "."),
                position=step.get("at", [0, 0]),
                size=step["size"],
            )
            blocks.append(blk)
            notes.append(f"add_wall: %{step['name']} (StaticBody2D) added to {hh['owner_scene']} at {blk['position']}, size {blk['size']}")
'''

DESC_ANCHOR = '        if b["op"] == "connect_signal":\n            if b["source"] != b["target"]:\n'
DESC = '''        if b["op"] == "add_wall":
            return f"ADD WALL %{b['name']} ({b['size'][0]:g}x{b['size'][1]:g}) at ({b['position'][0]:g}, {b['position'][1]:g})"
'''

CANON_ANCHOR = '    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""\n'
CANON = '''    if b["op"] == "add_wall":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
'''

def once(text, anchor, what):
    if text.count(anchor) != 1:
        sys.exit(f"STOPPED: cascade_plan.py has {text.count(anchor)} places for '{what}' (expected 1); nothing written")

done = []
for old, rep, mark in SCHEMA:
    if mark in new:
        continue
    once(new, old, mark)
    new = new.replace(old, rep)
    done.append("schema " + mark.split(":")[0])

if 'do == "add_wall"' not in new:
    once(new, EXPAND_ANCHOR, "expand_plan")
    new = new.replace(EXPAND_ANCHOR, EXPAND + EXPAND_ANCHOR)
    done.append("expand_plan")
if "ADD WALL %" not in new:
    once(new, DESC_ANCHOR, "describe")
    new = new.replace(DESC_ANCHOR, DESC + DESC_ANCHOR)
    done.append("describe")
if '"op" == "add_wall"' not in new:
    once(new, CANON_ANCHOR, "canonical_replies")
    new = new.replace(CANON_ANCHOR, CANON_ANCHOR + CANON)
    done.append("canonical_replies")
if "owner_scene" not in new:
    sys.exit("STOPPED: touched_files lacks owner_scene mapping; nothing written")

if new != src:
    compile(new, path, "exec")
    bak = path + ".step20.bak"
    if os.path.exists(bak):
        bak += "." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, bak)
    tmp = path + ".cascade_tmp"
    open(tmp, "w", encoding="utf-8").write(new)
    os.replace(tmp, path)
    print("cascade_plan.py: patched (" + ", ".join(done) + f"); backup {os.path.basename(bak)}")
else:
    print("cascade_plan.py: already patched")
