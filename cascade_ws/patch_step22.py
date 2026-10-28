#!/usr/bin/env python3
"""Step 22 patcher: wires add_sprite into cascade_plan.py and writes plan_sprite.json."""
import argparse, json, os, shutil, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws"))
ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))

pp = os.path.join(ws, "cascade_plan.py")
if not os.path.exists(pp):
    sys.exit("cascade_plan.py not found in workspace")

src = new = open(pp, encoding="utf-8").read()
done = []

def backup(p, tag):
    bak = f"{p}.{tag}.bak"
    if os.path.exists(bak):
        bak += "." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(p, bak)
    return os.path.basename(bak)

def put(p, text):
    if p.endswith(".py"):
        compile(text, p, "exec")
    tmp = p + ".cascade_tmp"
    open(tmp, "w", encoding="utf-8").write(text)
    os.replace(tmp, p)

# 1. Schema anchors matching Step 21 style
SCHEMA = [
    ('"add_camera": {"name"}', '"add_camera": {"name"}, "add_sprite": {"name", "texture"}', '"add_sprite": {"name", "texture"}'),
    ('"add_camera": {"zoom", "smoothing", "speed", "node"}', '"add_camera": {"zoom", "smoothing", "speed", "node"}, "add_sprite": {"centered", "node"}', '"add_sprite": {"centered", "node"}')
]

for old, rep, mark in SCHEMA:
    if mark not in new and old in new:
        new = new.replace(old, rep)
        done.append("schema " + mark.split(":")[0])

# 2. AST Expansion
EXPAND_ANCHOR = '        elif do == "when":\n'
EXPAND = '''        elif do == "add_sprite":
            if not isinstance(step["name"], str):
                raise PlanError(f"{where}: 'name' must be text")
            if not isinstance(step["texture"], str):
                raise PlanError(f"{where}: 'texture' must be a res:// path")
            if "centered" in step and not isinstance(step["centered"], bool):
                raise PlanError(f"{where}: 'centered' must be true or false")
            blk = head(hh, "add_sprite")
            blk.update(target=hh["name"], ref=hh["addr"], exclude=list(EXC.get("add_sprite", EXC["add_node"])), name=step["name"], texture=step["texture"])
            if "centered" in step:
                blk["centered"] = step["centered"]
            blocks.append(blk)
            notes.append(f"add_sprite: %{step['name']} (Sprite2D, {step['texture']}) added to {hh['name']} in {hh['owner_scene']}")
'''

# 3. Formatter & Mock Reply
DESC_ANCHOR = '        if b["op"] == "connect_signal":\n            if b["source"] != b["target"]:\n'
DESC = '''        if b["op"] == "add_sprite":
            return f"ADD SPRITE %{b['name']} on {b['target']} ({b['texture']}, " + ("centered)" if b.get("centered", True) else "top-left origin)")
'''

CANON_ANCHOR = '    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""\n'
CANON = '''    if b["op"] == "add_sprite":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
'''

CAT_SPRITE = r'''        L.append("  ALSO {\"do\": \"add_sprite\", \"name\": ..., \"texture\": \"res://sprites/x.png\", \"centered\": true}: a Sprite2D child of the plan's node (a CharacterBody2D, Area2D, StaticBody2D, Node2D or Marker2D) showing that texture; a .png that is not in the project yet is created as a 32x32 placeholder, .svg/.jpg/.webp must already be there; one sprite per node; \"centered\" is optional")'''
RULES = '        L.append("  rules: a name of 2 to 24 letters or digits'

def section(text, fn):
    return text.split("def " + fn)[1].split("\ndef ")[0]

if 'do == "add_sprite"' not in section(new, "expand_plan") and EXPAND_ANCHOR in new:
    new = new.replace(EXPAND_ANCHOR, EXPAND + EXPAND_ANCHOR)
    done.append("expand_plan")

if "ADD SPRITE %" not in section(new, "describe") and DESC_ANCHOR in new:
    new = new.replace(DESC_ANCHOR, DESC + DESC_ANCHOR)
    done.append("describe")

if 'b["op"] == "add_sprite"' not in section(new, "canonical_replies") and CANON_ANCHOR in new:
    new = new.replace(CANON_ANCHOR, CANON_ANCHOR + CANON)
    done.append("canonical_replies")

lines = new.split("\n")
ri = [n for n, l in enumerate(lines) if l.startswith(RULES)]
if len(ri) == 1 and 'do\": \"add_sprite' not in section(new, "cmd_catalog"):
    lines.insert(ri[0] + 1, CAT_SPRITE)
    done.append("catalog")
    new = "\n".join(lines)

if new != src:
    bak = backup(pp, "step22")
    put(pp, new)
    print("cascade_plan.py: patched (" + ", ".join(done) + f"); backup {bak}")
else:
    print("cascade_plan.py: already patched")

# 4. Plan fixture
text = ('{\n  "intent": "give the coin a visible sprite",\n  "node": "%Coin",\n  "steps": [\n'
        '    {"do": "add_sprite", "name": "CoinSprite", "texture": "res://sprites/coin.png"}\n  ]\n}\n')
pf = os.path.join(ws, "plan_sprite.json")
if not os.path.exists(pf) or open(pf, encoding="utf-8").read() != text:
    open(pf, "w", encoding="utf-8").write(text)
    print("plan_sprite.json: written")
else:
    print("plan_sprite.json: already there")
