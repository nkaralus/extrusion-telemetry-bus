import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "ctor = {" in s: sys.exit("cascade_clock.py already prefills the constructor.")
if "stmt_head" not in s: sys.exit("Apply patch_prefill2.py first.")

OLD = '''    stmt_head = f"{block['property']} = " if block["op"] == "modify_property" else ""'''
NEW = '''    ctor = {"Vector2": "Vector2(", "Vector3": "Vector3(", "Color": "Color("}.get(block["type"], "")
    stmt_head = (f"{block['property']} = {ctor}" if ctor else f"{block['property']} =") if block["op"] == "modify_property" else ""'''

if s.count(OLD) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + OLD[:90])
s = s.replace(OLD, NEW, 1)

compile(s, p, "exec")
open(p + ".before-prefill3", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_clock.py patched: the prefill now ends at 'Vector2(' (or 'property =' for plain types) - never on a space (backup: cascade_clock.py.before-prefill3)")
