#!/usr/bin/env python3
"""Step 20 patcher (idempotent, .bak first): adds add_wall to task_block_spec.json and wires cascade_step20 into cascade_clock.py (after Step 19)."""
import json, os, shutil, sys, argparse
ap = argparse.ArgumentParser(); ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws")); ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))
OP = "add_wall"; SLOTS = ["frame_id", "time_slot", "subnet", "op", "target", "ref", "exclude", "name", "parent", "position", "size"]
def bak(p): shutil.copy2(p, p + ".step20.bak")

sp = os.path.join(ws, "task_block_spec.json"); spec = json.load(open(sp))
if any(k not in spec["slots"] for k in ("name", "parent", "position", "size")): sys.exit("STOPPED: the spec lacks the Step 18 slots name/parent/position/size; nothing written")
if OP in spec["slots"]["op"]["enum"] and OP in spec["ops"]: print("spec: already patched")
else:
    bak(sp)
    if OP not in spec["slots"]["op"]["enum"]: spec["slots"]["op"]["enum"].append(OP)
    spec["ops"][OP] = {"status": "locked", "slots": SLOTS,
        "v1": "the harness adds a StaticBody2D (name, parent, position) with a CollisionShape2D and a RectangleShape2D (size is the full width and height) to the scene of the block's host; no mule call. GI_1: the new wall may not overlap any registered collision shape (AABB); the scene needs a CharacterBody2D that reads Input.get_vector. SLOT_5 runs live_collide.gd: the mover is stopped with penetration <= 0.081 px, slides along the wall, and does not tunnel at speed 600"}
    json.dump(spec, open(sp, "w"), indent=2); print("spec: patched")

cp = os.path.join(ws, "cascade_clock.py"); src = open(cp, encoding="utf-8").read()
if "cascade_step20" in src: print("clock: already patched")
else:
    A1 = "# ---------------------------------------------------------------- the clock\ndef main():"
    A2 = '    if block["op"] == "set_physics_body": return _s19.run_frame(a, ws, project, host, block, row, finish)\n'
    if "cascade_step19" not in src: sys.exit("STOPPED: Step 19 is not wired into the clock; run step19_paste.sh first")
    if src.count(A1) != 1 or src.count(A2) != 1: sys.exit("STOPPED: the clock does not have the expected anchors; nothing written")
    B1 = ("# ---------------------------------------------------------------- Step 20: add_wall (StaticBody2D wall; see cascade_step20.py)\n"
          "import cascade_step20 as _s20\n_s20.bind(sys.modules[__name__])\n_step19_slot1 = slot1\n\n"
          "def slot1(block, reg, spec, project): return _s20.slot1(_step19_slot1, block, reg, spec, project)\n\n")
    B2 = A2 + '    if block["op"] == "add_wall": return _s20.run_frame(a, ws, project, host, block, row, finish)\n'
    bak(cp); open(cp, "w", encoding="utf-8").write(src.replace(A1, B1 + A1).replace(A2, B2)); print("clock: patched")
