"""cascade_patchlib.py - the small shared helper for the cascade patch scripts.

A patch is a list of per-file entries. Every file is judged on its own:
  done     the marker is already in the file
  ready    every anchor is found exactly once, so the edits can be applied
  blocked  an anchor is missing or ambiguous (the report names it)
Ready files are applied with a backup, done files are skipped, and a blocked file never stops the others.
Run the patch again after fixing a blocked file and it finishes what is left.
"""
import json, os


def _read(p):
    return open(p, encoding="utf-8").read()


def plan_text(text, marker, edits):
    if marker in text:
        return "done", [], text
    problems, cur = [], text
    for old, new in edits:
        n = cur.count(old)
        if n != 1:
            problems.append(f"anchor found {n} times (expected exactly 1): {old.strip().splitlines()[0][:80]!r}")
            continue
        cur = cur.replace(old, new, 1)
    return ("blocked" if problems else "ready"), problems, cur


def run(ws, name, entries):
    """entries: {"file", "marker", "edits": [(old, new), ...]} for text files, {"file", "done": fn(dict)->bool, "edit": fn(dict)} for json files."""
    ws = os.path.abspath(os.path.expanduser(ws))
    blocked = 0
    print(f"patch {name}")
    for e in entries:
        path = os.path.join(ws, e["file"])
        if not os.path.exists(path):
            print(f"  {e['file']:<24} MISSING  (file not found)")
            blocked += 1
            continue
        if "edits" in e:
            text = _read(path)
            state, problems, new = plan_text(text, e["marker"], e["edits"])
            if state == "ready" and e["file"].endswith(".py"):
                try:
                    compile(new, e["file"], "exec")
                except SyntaxError as ex:
                    state, problems = "blocked", [f"the edited file would not compile: {ex}"]
            if state == "ready":
                open(path + f".before-{name}", "w", encoding="utf-8").write(text)
                open(path, "w", encoding="utf-8").write(new)
                print(f"  {e['file']:<24} applied  (backup {e['file']}.before-{name})")
            elif state == "done":
                print(f"  {e['file']:<24} done     (already has it)")
            else:
                blocked += 1
                print(f"  {e['file']:<24} BLOCKED")
                for p in problems:
                    print("      - " + p)
        else:
            data = json.load(open(path, encoding="utf-8"))
            if e["done"](data):
                print(f"  {e['file']:<24} done     (already has it)")
                continue
            try:
                e["edit"](data)
            except KeyError as ex:
                blocked += 1
                print(f"  {e['file']:<24} BLOCKED")
                print(f"      - the file lacks {ex}")
                continue
            open(path + f".before-{name}", "w", encoding="utf-8").write(_read(path))
            json.dump(data, open(path, "w", encoding="utf-8"), indent=2)
            print(f"  {e['file']:<24} applied  (backup {e['file']}.before-{name})")
    if blocked:
        print(f"  => {blocked} file(s) blocked; the others were applied. Fix the blocked part (or paste this output) and run the patch again.")
    else:
        print("  => every file is in place")
    return blocked
