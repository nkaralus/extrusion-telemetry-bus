# ---- 1. prefill through the constructor: the answer now starts at  velocity = Vector2(
cat << 'PEOF' > ~/cascade_ws/patch_prefill3.py
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "ctor = {" in s: sys.exit("cascade_clock.py already prefills the constructor.")
if "stmt_head" not in s: sys.exit("Apply patch_prefill2.py first.")
OLD = '''    stmt_head = f"{block['property']} = " if block["op"] == "modify_property" else ""
'''
NEW = '''    ctor = {"Vector2": "Vector2(", "Vector3": "Vector3(", "Color": "Color("}.get(block["type"], "")
    stmt_head = (f"{block['property']} = {ctor}" if ctor else f"{block['property']} =") if block["op"] == "modify_property" else ""
'''
if s.count(OLD) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + OLD[:90])
s = s.replace(OLD, NEW, 1)
compile(s, p, "exec")
open(p + ".before-prefill3", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_clock.py patched: the prefill now ends at 'Vector2(' (or 'property =' for plain types) - never on a space (backup: cascade_clock.py.before-prefill3)")
PEOF
python3 ~/cascade_ws/patch_prefill3.py ~/cascade_ws/cascade_clock.py

# ---- 2. rehearsal file: with --prefill the script holds what the mule writes AFTER 'velocity = Vector2('
cat << 'EOF' > ~/cascade_ws/rehearsal_prefill_ok.json
["0.0, -400.0)\"}"]
EOF

# ---- 3. quick check, no GPU
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
python3 cascade_clock.py block_0001.json --prefill --script rehearsal_prefill_ok.json --no-apply
