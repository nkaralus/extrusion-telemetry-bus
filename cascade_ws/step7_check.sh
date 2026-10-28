cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 - << 'EOF'
import json, os, sys
ws = os.path.expanduser("~/cascade_ws")
def read(n): return open(os.path.join(ws, n), encoding="utf-8").read()
clock, builder = read("cascade_clock.py"), read("registry_builder.py")
reg = json.load(open(os.path.join(ws, "state_registry.json")))["hosts"]
host = next((h for h in reg.values() if h["name"] == "%Player"), None)
scene, gd = read("engine_test/scenes/main.tscn"), read("engine_test/scripts/player.gd")
tspec = json.load(open(os.path.join(ws, "task_block_spec.json")))
checks = [
 ("clock knows connect_signal (GI-1, GI-3, apply)", "def connect_checks" in clock and "def apply_scene_connection" in clock),
 ("registry builder lists signals and methods", '"signals": sigs' in builder),
 ("registry has signals and methods for %Player", bool(host) and "signals" in host and "methods" in host),
 ("spec has the connect_signal op locked", tspec.get("ops", {}).get("connect_signal", {}).get("status") == "locked"),
 ("player.gd has the stub _on_player_ready", "func _on_player_ready" in gd),
]
for label, ok in checks: print(("  ok       " if ok else "  MISSING  ") + label)
print("  info     retry temperatures are the constant 0.2:", 'default="0.2,0.2,0.2"' in clock)
print("  info     main.tscn already has the ready connection:", '[connection signal="ready"' in scene)
if host and "methods" in host: print("  info     %Player has", len(host.get("signals", [])), "signals; script methods:", host["methods"])
sys.exit(0 if all(ok for _, ok in checks) else 1)
EOF
if [ $? -ne 0 ]; then
  echo
  echo "Something is missing (see above). Stop here and paste this output."
else
  echo
  cat << 'EOF' > ~/cascade_ws/block_connect_0001.json
{"frame_id":"0x2000","time_slot":"SLOT_3_EXECUTE","subnet":"10.1.1.0/24","op":"connect_signal","source":"%Player","target":"%Player","signal":"ready","method":"_on_player_ready","ref":"10.1.1.10","exclude":["KinematicBody2D","connect_3arg","untyped_variables","relative_paths_up_tree","cross_subnet_queries"]}
EOF
  cat << 'EOF' > ~/cascade_ws/rehearsal_connect_ok.json
["_on_player_ready)\"}"]
EOF
  echo "block and rehearsal files written; rehearsal (no GPU, nothing written to the scene):"
  python3 cascade_clock.py block_connect_0001.json --prefill --script rehearsal_connect_ok.json --no-apply
fi
