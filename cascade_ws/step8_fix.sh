cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 - << 'EOF'
import json, os, subprocess, sys
ws = os.path.expanduser("~/cascade_ws")
def state():
    clock = open(os.path.join(ws, "cascade_clock.py"), encoding="utf-8").read()
    t = json.load(open(os.path.join(ws, "task_block_spec.json"), encoding="utf-8"))
    code = "def add_method_checks" in clock
    spec = t.get("ops", {}).get("add_method", {}).get("status") == "locked" and "fn" in t["slots"] and "add_method" in t["slots"]["op"]["enum"]
    return code, spec, t
code, spec, t = state()
print("  clock has add_method code:", code)
print("  spec has the add_method op:", spec)
if not code:
    pm = os.path.join(ws, "patch_method.py")
    if not os.path.exists(pm): sys.exit("STOPPED: the clock has no add_method code and patch_method.py is not in ~/cascade_ws; paste the step 8 script first")
    print("  the clock does not have add_method yet, so patch_method.py runs now; its output:")
    r = subprocess.run([sys.executable, pm, ws], capture_output=True, text=True); print("    " + (r.stdout + r.stderr).strip().replace("\n", "\n    "))
    code, spec, t = state()
if code and not spec:
    p = os.path.join(ws, "task_block_spec.json")
    open(p + ".before-method-spec", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
    if "add_method" not in t["slots"]["op"]["enum"]: t["slots"]["op"]["enum"].append("add_method")
    t["slots"]["fn"] = {"type": "string", "rx": "^[a-z_][a-z0-9_]{0,39}$"}
    t["slots"]["args"] = {"type": "array", "items": "string", "max_items": 3, "item_rx": "^[a-z_][a-z0-9_]*: (bool|int|float|String|Vector2|Vector3|Color)$"}
    t["slots"]["returns"] = {"type": "string", "enum": ["void"]}
    t["slots"]["body"] = {"type": "string", "enum": ["pass", "print"]}
    t["slots"]["message"] = {"type": "string", "rx": "^[A-Za-z0-9 _.,:!?-]{0,40}$"}
    t["ops"]["add_method"] = {
        "status": "locked",
        "slots": ["frame_id", "time_slot", "subnet", "op", "target", "fn", "args", "returns", "body", "message", "ref", "exclude"],
        "v1": "append one function with a one-line body (pass or print) to the host node's script; the harness writes the canonical text, the mule only completes the body line",
    }
    json.dump(t, open(p, "w", encoding="utf-8"), indent=2)
    print("  task_block_spec.json updated (backup: task_block_spec.json.before-method-spec)")
    code, spec, t = state()
print("  RESULT: clock", "ok" if code else "MISSING", "| spec", "ok" if spec else "MISSING")
sys.exit(0 if code and spec else 1)
EOF
if [ $? -ne 0 ]; then
  echo
  echo "Still not complete (see above). Stop here and paste this whole output."
else
  echo
  echo "rehearsal, no GPU, nothing written:"
  python3 cascade_clock.py block_method_0001.json --prefill --script rehearsal_method_ok.json --no-apply
fi
