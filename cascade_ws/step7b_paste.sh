# ---- 1. accept self.<method> (judge the bare form), and give a clear message when the wrong python runs the mule
cat << 'PEOF' > ~/cascade_ws/patch_selfform.py
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "def canonical_stmt" in s: sys.exit("cascade_clock.py already accepts the self. form.")
if "def connect_checks" not in s: sys.exit("Apply patch_connect.py first.")
EDITS = [
 # GI-3: the mule's own spelling, self.<method>, is the same connection
 (r'''        want = f"{block['signal']}.connect({block['method']})"
        if stmt != want: return f"type_match: stmt must be exactly {want}"
''',
  r'''        want = f"{block['signal']}.connect({block['method']})"
        if stmt not in (want, f"{block['signal']}.connect(self.{block['method']})"): return f"type_match: stmt must be {want} (self. before the method is also accepted)"
'''),
 # Slot 4 judges the canonical spelling: Godot's parse check does not see a missing method behind self.
 (r'''# ---------------------------------------------------------------- SLOT 4
def judge(godot, project, host, stmt):
''',
  r'''# ---------------------------------------------------------------- SLOT 4
def canonical_stmt(stmt, block):
    """The statement Godot is asked to judge. For connect_signal that is the bare form: the parse check reports a missing method there, but not behind self."""
    if block["op"] == "connect_signal": return f"{block['signal']}.connect({block['method']})"
    return stmt

def judge(godot, project, host, stmt):
'''),
 ("ok, jerr = judge(a.godot, project, host, stmt)", "ok, jerr = judge(a.godot, project, host, canonical_stmt(stmt, block))"),
 # a clear message instead of a traceback when the wrong python runs the real mule
 (r'''    bench._load(a.model)                             # the model loads here, so frame timings exclude it
''',
  r'''    try: bench._load(a.model)                        # the model loads here, so frame timings exclude it
    except ModuleNotFoundError as e: die(f"the python module '{e.name}' is missing for the python running this. Run the real mule with ~/mule_bench/.venv/bin/python")
'''),
]
for old, new in EDITS:
    if s.count(old) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + old[:100])
    s = s.replace(old, new, 1)
compile(s, p, "exec")
open(p + ".before-selfform", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("cascade_clock.py patched: connect_signal accepts self.<method>, Slot 4 judges the bare form, and a wrong python gives a clear message (backup: cascade_clock.py.before-selfform)")
PEOF
python3 ~/cascade_ws/patch_selfform.py ~/cascade_ws/cascade_clock.py

# ---- 2. quick check, no GPU: Gemma's own spelling now passes
cd ~/cascade_ws
export GODOT="${GODOT:-godot}"
cat << 'EOF' > ~/cascade_ws/rehearsal_self.json
["self._on_player_ready)\"}"]
EOF
python3 cascade_clock.py block_connect_0001.json --prefill --script rehearsal_self.json --no-apply --tries 1
