#!/usr/bin/env python3
"""
MULE - the work harness.  Task in -> tested code out -> YOU decide.

Run it from inside the project folder you are working on. It keeps everything
in a hidden .mule/ folder there:

    .mule/state.md      one page: goal, decisions, known bugs (pasted into every prompt)
    .mule/queue/        tasks waiting (one .json each)
    .mule/verified/     tasks that PASSED their test: final files + training data
    .mule/failed/       tasks that failed every try
    .mule/handoff/      ready-to-paste packets for Claude / Gemini (failed tasks)
    .mule/log.jsonl     every try: pass/fail, seconds, tokens

Commands
    mule init
    mule add "what to build" --test "python3 check.py" --files a.py b.py [--out a.py]
    mule run   [--model gemma26b] [--tries 3] [--max-minutes 120]
    mule status
    mule show  ID
    mule apply ID [--yes]        (copies verified files into the project)
    mule selftest                (fake model, no GPU: proves the harness works)

Ground truth rule: a task is only "verified" if its test command exits with 0.
No test, no verified success - write the test first.
"""
import argparse, difflib, json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

SKIP_DIRS = {".mule", ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "results", "Intermediate", "Binaries", "Saved"}
MAX_COPY_BYTES = 5 * 1024 * 1024
TRUNC_TOKENS = 1150          # an answer this long was almost certainly cut off
STATE_TEMPLATE = """# Project state (keep it to one page)
Goal:
Current systems:
Decisions made:
Known bugs:
"""

FORMAT = ("Return the COMPLETE contents of every file you create or change. Put each one under a header "
          "line exactly like\n### FILE: path/to/file.py\nfollowed by one fenced code block. "
          "No explanations and no reasoning - only the files.")


# --------------------------------------------------------------------------
#  small helpers
# --------------------------------------------------------------------------
def mdir(root, *p): return os.path.join(root, ".mule", *p)

def read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f: return f.read()
    except OSError:
        return default

def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: f.write(text)

def log_line(root, rec):
    os.makedirs(mdir(root), exist_ok=True)
    with open(mdir(root, "log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

def say(root, msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    os.makedirs(mdir(root), exist_ok=True)
    with open(mdir(root, "run.log"), "a", encoding="utf-8") as f: f.write(line + "\n")

def safe_rel(p):
    p = p.strip().strip("`").replace("\\", "/")
    return p if p and not p.startswith("/") and ".." not in p.split("/") and not re.match(r"^[A-Za-z]:", p) else None


# --------------------------------------------------------------------------
#  prompt + answer parsing
# --------------------------------------------------------------------------
def build_prompt(root, task):
    parts = []
    state = read(mdir(root, "state.md")).strip()
    if state and state != STATE_TEMPLATE.strip():
        parts.append("PROJECT STATE:\n" + state)
    for f in task["files"]:
        body = read(os.path.join(root, f), None)
        parts.append(f"CURRENT FILE {f}:\n```\n{body}\n```" if body is not None else f"FILE {f} does not exist yet (new file).")
    parts.append("TASK:\n" + task["task"])
    parts.append("Your code will be checked by running this command in a copy of the project:\n" + task["test"])
    out = task.get("out") or []
    if out: parts.append("Files you may write: " + ", ".join(out))
    parts.append(FORMAT)
    return "\n\n".join(parts)

def parse_files(text, out_default):
    """-> {relative_path: content}. Accepts '### FILE: path' + code block; falls back to one block + one allowed file."""
    files = {}
    for m in re.finditer(r"###\s*FILE:\s*(.+?)\s*\n+```[a-zA-Z0-9_+-]*\s*\n(.*?)```", text, re.S):
        rel = safe_rel(m.group(1))
        if rel: files[rel] = m.group(2)
    if not files and len(out_default) == 1:
        blocks = re.findall(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", text, re.S)
        if blocks: files[out_default[0]] = max(blocks, key=len)
    return files


# --------------------------------------------------------------------------
#  sandbox test: copy the project, write the model's files, run the test command
# --------------------------------------------------------------------------
def run_in_sandbox(root, files, test, timeout):
    def ignore(d, names):
        skip = {n for n in names if n in SKIP_DIRS}
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.path.getsize(p) > MAX_COPY_BYTES: skip.add(n)
        return skip
    with tempfile.TemporaryDirectory() as tmp:
        box = os.path.join(tmp, "box")
        shutil.copytree(root, box, ignore=ignore)
        for rel, content in files.items():
            write(os.path.join(box, rel), content)
        try:
            r = subprocess.run(test, shell=True, cwd=box, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT after {timeout}s"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, out[-1500:] if out else f"exit code {r.returncode}"


# --------------------------------------------------------------------------
#  saving results
# --------------------------------------------------------------------------
def files_as_text(files):
    return "\n\n".join(f"### FILE: {p}\n```\n{c}\n```" for p, c in files.items())

def save_verified(root, task, prompt, files, history, model, secs):
    d = mdir(root, "verified", task["id"])
    for rel, content in files.items(): write(os.path.join(d, "files", rel), content)
    rec = dict(id=task["id"], task=task["task"], test=task["test"], tries=len(history), model=model,
               seconds=round(secs, 1), failures=[h["error"] for h in history if not h["ok"]], ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
    write(os.path.join(d, "record.json"), json.dumps(rec, indent=1))
    with open(mdir(root, "verified", "dataset.jsonl"), "a", encoding="utf-8") as f:   # future fine-tune data
        f.write(json.dumps({"messages": [{"role": "user", "content": prompt},
                                          {"role": "assistant", "content": files_as_text(files)}], "meta": rec}) + "\n")

def save_handoff(root, task, files, history, model):
    last = history[-1]
    shown = files_as_text(files) if files else "(the model produced no usable files)"
    state = read(mdir(root, "state.md")).strip()
    packet = (("PROJECT STATE:\n" + state + "\n\n") if state else "") + (
        f"TASK: {task['task']}\n"
        f"FILES: {', '.join(task['files']) or '(none)'}  - last attempt below\n{shown}\n\n"
        f"TEST COMMAND: {task['test']}\n"
        f"TEST RESULT: {last['error']}\n"
        f"LAST LAYER SAID: {model} tried {len(history)} times and never passed; its last attempt is above.\n"
        f"ASK: review and fix - return the complete corrected file(s), and say what was wrong.\n")
    write(mdir(root, "handoff", task["id"] + ".txt"), packet)


# --------------------------------------------------------------------------
#  the queue runner
# --------------------------------------------------------------------------
def load_queue(root):
    q = mdir(root, "queue")
    return [json.load(open(os.path.join(q, f), encoding="utf-8")) for f in sorted(os.listdir(q)) if f.endswith(".json")] if os.path.isdir(q) else []

def run_queue(root, ask, model, tries=3, max_minutes=120, only=None):
    started = time.time(); nofile_streak = 0; done = 0
    tasks = [t for t in load_queue(root) if not only or t["id"] == only]
    if not tasks: say(root, "Queue is empty. Add a task with: mule add ..."); return
    say(root, f"Run started: {len(tasks)} task(s), model {model}, up to {tries} tries each, {max_minutes} min cap")
    for task in tasks:
        prompt = build_prompt(root, task); msgs = [{"role": "user", "content": prompt}]
        history, files, ok, t_task = [], {}, False, time.time()
        for n in range(1, tries + 1):
            if time.time() - started > max_minutes * 60:
                say(root, f"Time cap ({max_minutes} min) reached. Stopping; unfinished tasks stay queued."); return
            try:
                text, ntok, secs = ask(msgs)
            except Exception as e:      # e.g. GPU out of memory: stop, do not burn through the queue
                say(root, f"MODEL ERROR on {task['id']}: {type(e).__name__}: {str(e)[:200]}. Stopping; task stays queued."); return
            files = parse_files(text, task.get("out") or [])
            if files:
                nofile_streak = 0
                ok, out = run_in_sandbox(root, files, task["test"], task.get("timeout", 120))
            else:
                nofile_streak += 1
                cut = ntok >= TRUNC_TOKENS
                ok, out = False, ("NO USABLE FILES in the answer" + (f" (cut off at {ntok} tokens - it was reasoning, not answering)" if cut else ""))
            history.append(dict(ok=ok, error="" if ok else out, secs=secs, tokens=ntok))
            log_line(root, dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), id=task["id"], try_no=n, model=model, ok=ok,
                                seconds=round(secs, 1), tokens=ntok, error="" if ok else out[-300:]))
            say(root, f"{task['id']} try {n}/{tries}: {'PASS' if ok else 'fail'}  ({secs:.0f}s, {ntok} tokens)" + ("" if ok else "  " + out.splitlines()[-1][:110]))
            if ok: break
            if nofile_streak >= 3:
                say(root, "Three answers in a row had no usable code (thinking mode on, or the model is rambling). "
                          "Stopping so it cannot burn hours. The task stays queued."); return
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": ("That failed the test. Test output:\n" + out + "\nFix it. Return the complete corrected file(s) in the same format, with no explanation.")
                      if files else ("I could not find any files in your answer. Reply with ONLY the complete file(s), each under a "
                                     "'### FILE: path' header and a fenced code block. No reasoning.")}]
        qfile = mdir(root, "queue", task["id"] + ".json")
        if ok:
            save_verified(root, task, prompt, files, history, model, time.time() - t_task)
            write(mdir(root, "verified", task["id"], "task.json"), json.dumps(task, indent=1)); os.remove(qfile); done += 1
        else:
            save_handoff(root, task, files, history, model)
            write(mdir(root, "failed", task["id"] + ".json"), json.dumps(task, indent=1)); os.remove(qfile)
            say(root, f"{task['id']}: gave up. Paste .mule/handoff/{task['id']}.txt to Claude or Gemini.")
    say(root, f"Run finished: {done}/{len(tasks)} verified in {(time.time() - started) / 60:.1f} min")


# --------------------------------------------------------------------------
#  commands
# --------------------------------------------------------------------------
def cmd_init(root, a):
    for sub in ("queue", "verified", "failed", "handoff"): os.makedirs(mdir(root, sub), exist_ok=True)
    if not os.path.exists(mdir(root, "state.md")): write(mdir(root, "state.md"), STATE_TEMPLATE)
    print(f"Ready: {mdir(root)}\nFill in {mdir(root, 'state.md')} (goal, decisions, known bugs) - it goes into every prompt.")

def cmd_add(root, a):
    os.makedirs(mdir(root, "queue"), exist_ok=True)
    nums = [int(m.group(1)) for d in ("queue", "verified", "failed") if os.path.isdir(mdir(root, d))
            for f in os.listdir(mdir(root, d)) if (m := re.match(r"t(\d+)", f))]
    tid = a.id or f"t{(max(nums) + 1 if nums else 1):03d}"
    for f in a.files + (a.out or []):
        if not safe_rel(f): sys.exit(f"Bad file path: {f} (use paths inside the project)")
    task = dict(id=tid, task=a.task, files=a.files, out=a.out or ([a.files[0]] if len(a.files) == 1 else []),
                test=a.test, timeout=a.timeout, created=time.strftime("%Y-%m-%dT%H:%M:%S"))
    write(mdir(root, "queue", tid + ".json"), json.dumps(task, indent=1))
    print(f"Queued {tid}. It will check itself with: {a.test}")

def cmd_run(root, a):
    sys.path.insert(0, HERE)
    try:
        import bench
        ask_raw = lambda msgs: bench.ask(a.model, msgs)
    except Exception as e:
        sys.exit(f"Could not load the model hook from bench.py ({type(e).__name__}: {e}).\n"
                 "mule.py must sit next to bench.py and config.json.")
    run_queue(root, ask_raw, a.model, a.tries, a.max_minutes, a.only)

def cmd_status(root, a):
    c = lambda d: len([f for f in os.listdir(mdir(root, d)) if not f.startswith("dataset")]) if os.path.isdir(mdir(root, d)) else 0
    print(f"queued {c('queue')} | verified {c('verified')} | failed {c('failed')} (packets in {mdir(root, 'handoff')})")
    rows = [json.loads(l) for l in read(mdir(root, "log.jsonl")).splitlines()[-8:]]
    for r in rows: print(f"  {r['ts']}  {r['id']} try {r['try_no']}  {'PASS' if r['ok'] else 'fail'}  {r['seconds']}s  {r['error'][:70]}")
    ds = read(mdir(root, "verified", "dataset.jsonl")).count("\n")
    print(f"training examples saved so far: {ds}")

def cmd_show(root, a):
    d = mdir(root, "verified", a.id)
    if os.path.isdir(d):
        print(read(os.path.join(d, "record.json")))
        for dp, _, fs in os.walk(os.path.join(d, "files")):
            for f in fs:
                p = os.path.join(dp, f); rel = os.path.relpath(p, os.path.join(d, "files"))
                print(f"\n----- {rel} -----\n" + read(p))
    elif os.path.exists(mdir(root, "handoff", a.id + ".txt")):
        print(read(mdir(root, "handoff", a.id + ".txt")))
    else:
        sys.exit("No verified result or handoff packet with that id.")

def cmd_apply(root, a):
    base = mdir(root, "verified", a.id, "files")
    if not os.path.isdir(base): sys.exit("That task is not verified, so there is nothing to apply.")
    changes = []
    for dp, _, fs in os.walk(base):
        for f in fs:
            src = os.path.join(dp, f); rel = os.path.relpath(src, base)
            new, old = read(src), read(os.path.join(root, rel))
            diff = "".join(difflib.unified_diff(old.splitlines(1), new.splitlines(1), "current/" + rel, "verified/" + rel))
            changes.append((rel, new, diff or "(no change)\n"))
    for rel, _, diff in changes: print(diff)
    if not a.yes and input("Apply these changes to the project? [y/N] ").strip().lower() != "y": print("Nothing changed."); return
    for rel, new, _ in changes: write(os.path.join(root, rel), new)
    print("Applied:", ", ".join(r for r, _, _ in changes))


# --------------------------------------------------------------------------
#  selftest: a scripted fake model proves the whole loop without a GPU
# --------------------------------------------------------------------------
def cmd_selftest(root, a):
    results = []
    def check(name, cond): results.append(bool(cond)); print(("PASS  " if cond else "FAIL  ") + name)
    with tempfile.TemporaryDirectory() as proj:
        write(os.path.join(proj, "calc.py"), "def mul(a, b):\n    raise NotImplementedError\n")
        write(os.path.join(proj, "check.py"), "from calc import mul\nassert mul(3, 4) == 12, 'mul(3,4) gave %r' % mul(3, 4)\nassert mul(0, 9) == 0\nprint('ok')\n")
        cmd_init(proj, None)
        ns = lambda **k: argparse.Namespace(**{**dict(id=None, out=None, timeout=30), **k})
        cmd_add(proj, ns(task="implement mul(a, b) in calc.py", test="python3 check.py", files=["calc.py"]))
        cmd_add(proj, ns(task="impossible task", test="python3 check.py", files=["calc.py"]))
        calls = {"n": 0}
        def fake(msgs):
            calls["n"] += 1; user = msgs[0]["content"]; text = msgs[-1]["content"]
            if "impossible" in user: code = "def mul(a, b):\n    return -1\n"
            elif "That failed" in text: code = "def mul(a, b):\n    return a * b\n"       # fixed after seeing the real error
            else: code = "def mul(a, b):\n    return a + b\n"                          # wrong on the first try
            return "Sure!\n### FILE: calc.py\n```python\n" + code + "```\n", 40, 0.1
        run_queue(proj, fake, "fake", tries=3)
        check("task 1 verified after a retry with the real error", os.path.isdir(mdir(proj, "verified", "t001")) and json.load(open(mdir(proj, "verified", "t001", "record.json")))["tries"] == 2)
        check("task 2 gave up and produced a handoff packet", "TEST RESULT:" in read(mdir(proj, "handoff", "t002.txt")) and os.path.exists(mdir(proj, "failed", "t002.json")))
        check("queue is empty afterwards", not load_queue(proj))
        check("one training example saved", read(mdir(proj, "verified", "dataset.jsonl")).count("\n") == 1)
        check("the real project was NOT touched by the sandbox", "NotImplementedError" in read(os.path.join(proj, "calc.py")))
        cmd_apply(proj, ns(id="t001", yes=True))
        check("apply copies the verified file in; the project test now passes", subprocess.run("python3 check.py", shell=True, cwd=proj, capture_output=True).returncode == 0)
        # runaway guard: a model that only rambles must stop the run after 3 answers
        cmd_add(proj, ns(task="x", test="python3 check.py", files=["calc.py"])); cmd_add(proj, ns(task="y", test="python3 check.py", files=["calc.py"]))
        n0 = [0]
        def rambler(msgs): n0[0] += 1; return "Here's a thinking process: " + "blah " * 200, 1200, 0.1
        run_queue(proj, rambler, "rambler", tries=3)
        check("runaway guard stops after 3 no-code answers and keeps tasks queued", n0[0] == 3 and len(load_queue(proj)) == 2)
        # unsafe path from the model is refused
        check("path traversal in a model answer is ignored", parse_files("### FILE: ../evil.py\n```python\nx=1\n```", []) == {})
        check("single unlabeled code block is accepted when exactly one file is allowed", parse_files("```python\nx=1\n```", ["a.py"]) == {"a.py": "x=1\n"})
        # model errors (GPU out of memory) stop the run cleanly
        def boom(msgs): raise RuntimeError("CUDA out of memory")
        run_queue(proj, boom, "boom", tries=3)
        check("a model error stops the run and leaves the queue intact", len(load_queue(proj)) == 2)
    print("\nALL GOOD" if all(results) else "\nSOMETHING FAILED"); sys.exit(0 if all(results) else 1)


def main():
    ap = argparse.ArgumentParser(description="Mule work harness")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init")
    p = sp.add_parser("add"); p.add_argument("task"); p.add_argument("--test", required=True)
    p.add_argument("--files", nargs="*", default=[]); p.add_argument("--out", nargs="*"); p.add_argument("--id"); p.add_argument("--timeout", type=int, default=120)
    p = sp.add_parser("run"); p.add_argument("--model", default=os.environ.get("MULE_MODEL", "gemma26b"))
    p.add_argument("--tries", type=int, default=3); p.add_argument("--max-minutes", type=int, default=120); p.add_argument("--only")
    sp.add_parser("status")
    p = sp.add_parser("show"); p.add_argument("id")
    p = sp.add_parser("apply"); p.add_argument("id"); p.add_argument("--yes", action="store_true")
    sp.add_parser("selftest")
    a = ap.parse_args()
    {"init": cmd_init, "add": cmd_add, "run": cmd_run, "status": cmd_status, "show": cmd_show, "apply": cmd_apply, "selftest": cmd_selftest}[a.cmd](os.getcwd(), a)

if __name__ == "__main__":
    main()
