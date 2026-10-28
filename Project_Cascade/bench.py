#!/usr/bin/env python3
"""
MULE BENCH - fixed test set, honest judge, one JSON line per run.
Loads models directly in Python with Hugging Face Transformers. No server.

  python bench.py --model gemma12b                      # Test 1: baseline
  python bench.py --model gemma12b --mode check --checker qwen9b   # Test 3: draft -> review -> revise
  python bench.py --model gemma12b --mode loop          # judge errors fed back, up to N tries
  python bench.py --model gemma12b --only code          # one category (code / doc / reason)
  python bench.py --dry                                 # no model: proves the judge works
"""
import argparse, json, os, re, subprocess, sys, tempfile, time, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
TASKS = json.load(open(os.path.join(HERE, "tasks", os.environ.get("BENCH_TASKS", "tasks.json"))))
LOG = os.path.join(HERE, "results", "runs.jsonl")

# --------------------------------------------------------------------------
#  MODEL CALL - loads the model straight into Python. No server, no ports.
#  One model in memory at a time; switching unloads the old one first.
# --------------------------------------------------------------------------
_loaded = {"key": None, "tok": None, "model": None}

def _load(key):
    if _loaded["key"] == key:
        return
    import gc, torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    _loaded.update(key=None, tok=None, model=None); gc.collect()
    if torch.cuda.is_available(): torch.cuda.empty_cache()
    m = CFG["models"][key]
    print(f"[loading {key} from {m['path']} ...]", flush=True)
    if m.get("clip"):                                   # MoE bigger than VRAM: serve experts through the clip
        _loaded.update(key=key, tok=AutoTokenizer.from_pretrained(m["path"]), model=_load_clip(m))
        return
    kw = {"torch_dtype": "auto", "device_map": "auto"}
    if m.get("load_in_4bit"):
        from transformers import BitsAndBytesConfig
        kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16)
    tok = AutoTokenizer.from_pretrained(m["path"])
    try:
        model = AutoModelForCausalLM.from_pretrained(m["path"], **kw)
    except ValueError:   # some newer models (e.g. multimodal Gemma) use a different class
        from transformers import AutoModelForImageTextToText
        model = AutoModelForImageTextToText.from_pretrained(m["path"], **kw)
    model.eval()
    _loaded.update(key=key, tok=tok, model=model)

def _load_clip(m):
    # Warm start reads the heat map but never writes it: bench prompts mix code, docs and
    # reasoning, and would blur the per-task heat map.
    import torch, clip_loader as clip
    c = m["clip"]
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    model = clip.load_clip_model(m["path"], c.get("gpu_cache", 4.0), c.get("ram_cache", 6),
                                 c.get("threads", 8), device, c.get("int8"))
    clip.STORE.prefetch_on = True
    clip.GUESS.clear()
    heat = os.path.join(HERE, "results", f"heatmap_{os.path.basename(os.path.normpath(m['path']))}_{c.get('tag', 'code')}.json")
    if c.get("warm", True) and os.path.exists(heat):
        clip.STORE.warm([(l, e) for l, e, _ in json.load(open(heat))["picks"]], c.get("pin_frac", 0.8))
    else:
        print(f"[clip] no warm start ({heat} missing or warm off)", flush=True)
    return model

def ask(model_key, messages):
    import torch
    _load(model_key)
    tok, model = _loaded["tok"], _loaded["model"]
    enc = tok.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=False, return_tensors="pt",
                                  return_dict=True).to(model.device)
    t0 = time.time()
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=CFG["max_tokens"],
                             do_sample=CFG["temperature"] > 0, temperature=max(CFG["temperature"], 1e-5))
    dt = time.time() - t0
    new = out[0][enc["input_ids"].shape[-1]:]
    return tok.decode(new, skip_special_tokens=True), int(new.shape[-1]), dt

# --------------------------------------------------------------------------
#  JUDGES  (ground truth - no model involved)
# --------------------------------------------------------------------------
def extract_code(text):
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return max(blocks, key=len) if blocks else text

def judge_code(task, text):
    code = extract_code(text)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "run.py")
        open(p, "w").write(code + "\n\n# ---- tests ----\n" + task["tests"] + "\nprint('ALL_PASS')\n")
        try:
            r = subprocess.run([sys.executable, p], capture_output=True, text=True,
                               timeout=CFG["judge_timeout_s"])
        except subprocess.TimeoutExpired:
            return False, "timeout"
    ok = "ALL_PASS" in r.stdout
    err = "" if ok else (r.stderr.strip().splitlines() or ["failed"])[-1][:300]
    return ok, err

def judge_doc(task, text):
    low = text.lower()
    ok = any(a.lower() in low for a in task["accept"])
    return ok, "" if ok else "expected one of " + str(task["accept"])

def judge_reason(task, text):
    want = task["answer"].lower()
    found = re.findall(r"[a-z]+|\d+(?:\.\d+)?", text.lower())
    ok = bool(found) and (want in found[-5:])   # answer must appear near the end
    return ok, "" if ok else f"expected {want}"

JUDGE = {"code": judge_code, "doc": judge_doc, "reason": judge_reason}

# --------------------------------------------------------------------------
#  PROMPTS
# --------------------------------------------------------------------------
def load_docs():
    d = os.path.join(HERE, "docs")
    return "\n\n".join(open(os.path.join(d, f), encoding="utf-8", errors="ignore").read()
                       for f in sorted(os.listdir(d)) if f.endswith((".md", ".txt")))

DOCS = load_docs()

def first_prompt(task):
    if task["type"] == "code":
        return task["prompt"] + "\nReturn only the code in one ```python block. No explanation."
    if task["type"] == "doc":
        return ("Answer using ONLY these documents. Be brief.\n\n--- DOCUMENTS ---\n"
                + DOCS + "\n--- END ---\n\nQuestion: " + task["prompt"])
    return task["prompt"]

def feedback(task, err):
    # Code: the real error is fair game (a compiler would show it too).
    # Doc/reason: NEVER reveal the expected answer - only say it was wrong.
    if task["type"] == "code":
        return "That failed the test: " + err + "\nFix it. Same output format as before."
    return "That answer is wrong. Think it through again step by step, then give the final answer in the same format."

# --------------------------------------------------------------------------
#  MODES
# --------------------------------------------------------------------------
def run_task(task, args):
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "test": args.test, "mode": args.mode, "model": args.model,
           "checker": args.checker if args.mode == "check" else None,
           "task": task["id"], "type": task["type"], "notes": args.notes}
    judge = JUDGE[task["type"]]
    msgs = [{"role": "user", "content": first_prompt(task)}]
    tokens = secs = 0.0
    tries = 0

    def call(key, m):
        nonlocal tokens, secs
        t, n, s = ask(key, m)
        tokens += n; secs += s
        return t

    text = call(args.model, msgs); tries = 1
    ok, err = judge(task, text)

    if args.mode == "check":
        review = call(args.checker, [{"role": "user", "content":
            "You are a strict reviewer. Find concrete mistakes in this answer to the task. "
            "If it is correct, reply exactly: LGTM.\n\nTASK:\n" + first_prompt(task)
            + "\n\nANSWER:\n" + text}])
        rec["checker_said_lgtm"] = review.strip().upper().startswith("LGTM")
        if not rec["checker_said_lgtm"]:
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "A reviewer said:\n" + review +
                      "\n\nFix the answer. Same output format as before."}]
            text = call(args.model, msgs); tries = 2
            ok, err = judge(task, text)

    elif args.mode == "loop":
        while not ok and tries < CFG["loop_max_tries"]:
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": feedback(task, err)}]
            text = call(args.model, msgs); tries += 1
            ok, err = judge(task, text)

    rec.update({"pass": ok, "tries": tries, "error": err, "answer": text[:20000],
                "tok_s": round(tokens / secs, 1) if secs else None,
                "seconds": round(secs, 1), "tokens": int(tokens)})
    return rec

# --------------------------------------------------------------------------
#  CHECK MODE, batched by model: all drafts -> all reviews -> all fixes.
#  Each model loads once per phase instead of swapping on every task.
# --------------------------------------------------------------------------
def run_check_batch(tasks, args):
    rows = []
    for t in tasks:                                   # phase 1: drafts
        msgs = [{"role": "user", "content": first_prompt(t)}]
        text, n, s = ask(args.model, msgs)
        rows.append({"t": t, "msgs": msgs, "text": text, "tok": n, "sec": s})
    for r in rows:                                    # phase 2: reviews
        review, n, s = ask(args.checker, [{"role": "user", "content":
            "You are a strict reviewer. Find concrete mistakes in this answer to the task. "
            "If it is correct, reply exactly: LGTM.\n\nTASK:\n" + first_prompt(r["t"])
            + "\n\nANSWER:\n" + r["text"]}])
        r.update(review=review, lgtm=review.strip().upper().startswith("LGTM"),
                 tok=r["tok"] + n, sec=r["sec"] + s)
    recs = []
    for r in rows:                                    # phase 3: fixes + judge
        t, tries, text = r["t"], 1, r["text"]
        if not r["lgtm"]:
            msgs = r["msgs"] + [{"role": "assistant", "content": text},
                                {"role": "user", "content": "A reviewer said:\n" + r["review"] +
                                 "\n\nFix the answer. Same output format as before."}]
            text, n, s = ask(args.model, msgs); tries = 2
            r["tok"] += n; r["sec"] += s
        ok, err = JUDGE[t["type"]](t, text)
        recs.append({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                     "test": args.test, "mode": "check", "model": args.model, "checker": args.checker,
                     "task": t["id"], "type": t["type"], "notes": args.notes,
                     "checker_said_lgtm": r["lgtm"], "pass": ok, "tries": tries, "error": err,
                     "answer": text[:20000], "review": r["review"][:1500],
                     "tok_s": round(r["tok"] / r["sec"], 1) if r["sec"] else None,
                     "seconds": round(r["sec"], 1), "tokens": int(r["tok"])})
    return recs

# --------------------------------------------------------------------------
def dry_run():
    """Proves the judges work with known-good and known-bad answers. No model needed."""
    good = {"code_01": "```python\ndef fizzbuzz(n):\n    return ['FizzBuzz' if i%15==0 else 'Fizz' if i%3==0 else 'Buzz' if i%5==0 else str(i) for i in range(1,n+1)]\n```",
            "doc_02": "The after-hours fee is $95.", "reason_06": "0.9^3 = 0.729, so 72.9"}
    bad = {"code_01": "```python\ndef fizzbuzz(n):\n    return []\n```",
           "doc_02": "It is free.", "reason_06": "About 70"}
    byid = {t["id"]: t for t in TASKS}
    for k in good:
        g = JUDGE[byid[k]["type"]](byid[k], good[k])[0]
        b = JUDGE[byid[k]["type"]](byid[k], bad[k])[0]
        print(f"{k:10} good->{'PASS' if g else 'FAIL'}  bad->{'PASS' if b else 'FAIL'}  "
              f"{'OK' if g and not b else 'JUDGE BROKEN'}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma12b")
    ap.add_argument("--mode", choices=["baseline", "check", "loop"], default="baseline")
    ap.add_argument("--checker", default="qwen9b")
    ap.add_argument("--only", choices=["code", "doc", "reason"])
    ap.add_argument("--test", default="T1", help="label, e.g. T1 / T3")
    ap.add_argument("--notes", default="", help="free text saved with every row")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    if args.dry:
        return dry_run()
    tasks = [t for t in TASKS if not args.only or t["type"] == args.only]
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if args.mode == "check":
        recs = run_check_batch(tasks, args)
        with open(LOG, "a") as f:
            for r in recs: f.write(json.dumps(r) + "\n")
        for r in recs:
            print(f"{r['task']:10} {'PASS' if r['pass'] else 'fail':4}  lgtm={r['checker_said_lgtm']}  tries={r['tries']}  {r['error'][:60]}")
        print(f"\n{args.model} checked by {args.checker}: {sum(r['pass'] for r in recs)}/{len(recs)} passed")
        return
    passed = 0
    for t in tasks:
        try:
            rec = run_task(t, args)
        except (OSError, ImportError) as e:
            sys.exit(f"\nCouldn't load the model ({e}). Check the 'path' in config.json and that transformers/torch are installed.")
        except Exception as e:
            rec = {"task": t["id"], "type": t["type"], "model": args.model, "mode": args.mode,
                   "test": args.test, "pass": False, "error": f"RUN ERROR: {type(e).__name__}: {e}"}
        passed += bool(rec.get("pass"))
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"{t['id']:10} {'PASS' if rec.get('pass') else 'fail':4}  "
              f"tries={rec.get('tries','-')}  tok/s={rec.get('tok_s','-')}  {rec.get('error','')[:60]}")
    print(f"\n{args.model} [{args.mode}]  {passed}/{len(tasks)} passed  -> logged to results/runs.jsonl")

if __name__ == "__main__":
    main()
