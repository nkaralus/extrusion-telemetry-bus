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
