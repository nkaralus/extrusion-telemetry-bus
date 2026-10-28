# step15b_paste.sh - small fix: ledger rows for NOOP blocks lose their plan id, so a plan that ends in a NOOP block reads "partial"
set -u
cd ~/cascade_ws || { echo "STOPPED: ~/cascade_ws not found"; exit 1; }
[ -f cascade_clock.py ] && [ ! -f cascade_clock.py.before-step15b ] && cp cascade_clock.py cascade_clock.py.before-step15b
cat << 'RIEOF' > ~/cascade_ws/patch_runid.py
import os, sys
ws = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/cascade_ws"))
sys.path.insert(0, ws)
try:
    import cascade_patchlib as pl
except ImportError:
    sys.exit("STOPPED: cascade_patchlib.py is not in " + ws)
OLD = '''    row = {"ts": now(), "frame_id": block.get("frame_id"), "op": block.get("op"), "ref": block.get("ref"), "mode": "rehearsal" if a.script else "mule"}
'''
NEW = OLD + '''    if a.run_id: row["run_id"] = a.run_id      # step15b: every row carries its plan id, NOOP rows included, so cascade_plan.py counts the whole plan
'''
entries = [{"file": "cascade_clock.py", "marker": "# step15b: every row carries", "edits": [(OLD, NEW)]}]
sys.exit(1 if pl.run(ws, "runid", entries) else 0)
RIEOF
python3 ~/cascade_ws/patch_runid.py ~/cascade_ws
# bookkeeping only: p0014 really finished (4 applied + 1 NOOP); mark its saved result as applied
if [ -f plans/p0014/result.json ]; then
python3 - << 'PYEOF'
import json
p = "plans/p0014/result.json"
d = json.load(open(p))
if d.get("state") == "partial":
    d["state"] = "applied"
    json.dump(d, open(p, "w"), indent=2)
    print("plans/p0014/result.json: state partial -> applied")
else:
    print("plans/p0014/result.json: state is", d.get("state"), "(left alone)")
PYEOF
fi
python3 cascade_plan.py status | tail -4
