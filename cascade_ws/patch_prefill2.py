import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "stmt_head" in s: sys.exit("cascade_clock.py already prefills the statement head.")
if "def ask_prefilled" not in s: sys.exit("Apply patch_prefill.py first.")

OLD = '''    prefill = (json.dumps({"frame_id": block["frame_id"], "target": block["target"]}, separators=(",", ":"))[:-1] + ',"stmt":"') if a.prefill else ""'''
NEW = '''    stmt_head = f"{block['property']} = " if block["op"] == "modify_property" else ""
    prefill = (json.dumps({"frame_id": block["frame_id"], "target": block["target"]}, separators=(",", ":"))[:-1] + ',"stmt":"' + stmt_head) if a.prefill else ""'''

OLD2 = "(the mule only completes the statement)"
NEW2 = "(the mule only completes the value)"

for old, new in ((OLD, NEW), (OLD2, NEW2)):
    if s.count(old) != 1: sys.exit("Could not find expected text:\n" + old[:90])
    s = s.replace(old, new, 1)

compile(s, p, "exec")
open(p + ".before-prefill2", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_clock.py patched: prefill extended through assignment.")
