#!/usr/bin/env python3
"""Step 19 patcher (idempotent, .bak first): adds set_physics_body to task_block_spec.json and wires cascade_step19 into cascade_clock.py."""
import json, os, shutil, sys, argparse
ap = argparse.ArgumentParser(); ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws")); ws = os.path.abspath(os.path.expanduser(ap.parse_args().ws))
OP = "set_physics_body"; ACT = {"type": "string", "rx": "^[a-z][a-z_]{0,31}$"}
SLOTS = ["frame_id", "time_slot", "subnet", "op", "target", "ref", "exclude", "template", "left", "right", "up", "down"]
def bak(p): shutil.copy2(p, p + ".step19.bak")

sp = os.path.join(ws, "task_block_spec.json"); spec = json.load(open(sp))
if OP in spec["slots"]["op"]["enum"] and OP in spec["ops"]: print("spec: already patched")
else:
    bak(sp)
    if OP not in spec["slots"]["op"]["enum"]: spec["slots"]["op"]["enum"].append(OP)
    spec["slots"]["template"] = {"type": "string", "enum": ["input_vector_8way"]}
    for k in ("left", "right", "up", "down"): spec["slots"][k] = dict(ACT)
    spec["ops"][OP] = {"status": "locked", "slots": SLOTS,
        "v1": "the harness replaces the body of the host's _physics_process (only move_and_slide(), pass or an earlier render) with the fixed template input_vector_8way: Input.get_vector over four named actions, velocity = dir * speed, move_and_slide(); no mule call. The harness first adds any missing action to project.godot (A/Left, D/Right, W/Up, S/Down). Host: a CharacterBody2D with speed float and velocity Vector2"}
    json.dump(spec, open(sp, "w"), indent=2); print("spec: patched")

cp = os.path.join(ws, "cascade_clock.py"); src = open(cp, encoding="utf-8").read()
if "cascade_step19" in src: print("clock: already patched")
else:
    A1 = "# ---------------------------------------------------------------- the clock\ndef main():"
    A2 = """    log(f"SLOT_1 FETCH    ok   {host['addr']} {host['name']} {host['uid']}"); log("GI_1            ok")\n"""
    if src.count(A1) != 1 or src.count(A2) != 1: sys.exit("STOPPED: the clock does not have the expected anchors; nothing written")
    B1 = ("# ---------------------------------------------------------------- Step 19: set_physics_body (input-driven movement; see cascade_step19.py)\n"
          "import cascade_step19 as _s19\n_s19.bind(sys.modules[__name__])\n_step18_slot1 = slot1\n\n"
          "def slot1(block, reg, spec, project): return _s19.slot1(_step18_slot1, block, reg, spec, project)\n\n")
    B2 = A2 + '    if block["op"] == "set_physics_body": return _s19.run_frame(a, ws, project, host, block, row, finish)\n'
    bak(cp); open(cp, "w", encoding="utf-8").write(src.replace(A1, B1 + A1).replace(A2, B2)); print("clock: patched")
