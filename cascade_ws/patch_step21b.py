#!/usr/bin/env python3
"""Step 21b (idempotent, .bak first): adds what a trimmed patch_step21.py can leave out of cascade_plan.py -
the add_camera branch of canonical_replies and the catalog lines for add_wall and add_camera."""
import argparse, os, shutil, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws"))
ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))

pp = os.path.join(ws, "cascade_plan.py")
src = new = open(pp, encoding="utf-8").read()
done = []

CANON_ANCHOR = '    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""\n'
CANON = '    if b["op"] == "add_camera":\n        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]\n'
CAT_WALL = r'''        L.append("  ALSO {\"do\": \"add_wall\", \"name\": ..., \"at\": [x, y], \"size\": [w, h], \"parent\": \".\" or \"%Node\"}: a solid StaticBody2D wall that stops the mover; \"size\" is the full width and height (8 to 1024); it may not overlap any collision shape; to be proven it must be the first thing the mover meets moving right along its own row")'''
CAT_CAM = r'''        L.append("  ALSO {\"do\": \"add_camera\", \"name\": ..., \"zoom\": [x, y], \"smoothing\": true, \"speed\": 5.0}: a Camera2D that follows the player (it becomes a child of the plan's node, which must be the player); zoom 0.1 to 8, speed 0.5 to 30; one camera per scene; every key but \"name\" is optional")'''
RULES = '        L.append("  rules: a name of 2 to 24 letters or digits'

def section(text, fn):
    return text.split("def " + fn)[1].split("\ndef ")[0]

for need in ("def canonical_replies", "def cmd_catalog", '"add_camera": {"name"}'):
    if need not in new:
        sys.exit(f"STOPPED: cascade_plan.py lacks '{need}' (run patch_step21.py first); nothing written")

if 'b["op"] == "add_camera"' not in section(new, "canonical_replies"):
    if new.count(CANON_ANCHOR) != 1:
        sys.exit("STOPPED: canonical_replies anchor not found once; nothing written")
    new = new.replace(CANON_ANCHOR, CANON_ANCHOR + CANON)
    done.append("canonical_replies")

lines = new.split("\n")
ri = [n for n, l in enumerate(lines) if l.startswith(RULES)]
if len(ri) != 1:
    sys.exit(f"STOPPED: {len(ri)} catalog 'rules:' lines (expected 1); nothing written")

cat = section(new, "cmd_catalog")
add = []
if '"add_wall": {"name", "size"}' in new:
    if 'do\": \"add_wall' not in cat:
        add.append(CAT_WALL)
else:
    print("catalog: add_wall has no plan step here (run patch_plan_step20.py); its line was not added")

if 'do\": \"add_camera' not in cat:
    add.append(CAT_CAM)

if add:
    lines[ri[0] + 1 : ri[0] + 1] = add
    done.append("catalog")
    new = "\n".join(lines)

if new == src:
    print("cascade_plan.py: nothing to add")
else:
    compile(new, pp, "exec")
    bak = pp + ".step21b.bak"
    if os.path.exists(bak):
        bak += "." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(pp, bak)
    tmp = pp + ".cascade_tmp"
    open(tmp, "w", encoding="utf-8").write(new)
    os.replace(tmp, pp)
    print("cascade_plan.py: patched (" + ", ".join(done) + f"); backup {os.path.basename(bak)}")
