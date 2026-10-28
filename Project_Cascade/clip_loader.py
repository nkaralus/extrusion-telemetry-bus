#!/usr/bin/env python3
"""
AMMO CLIP LOADER v0.9.1 (chat memory keeps exact generated tokens) | v0.9 (chat memory, brief-answer instruction, 2048 tokens) | v0.8 (--chat: talk, /run, /fix) | v0.7 (Gemma 4 + Qwen 3.5/3.6 MoE) | v0.6 (--int8) | v0.5.3 (unused guesses -> bounded RAM cache, streamed warm start)  v0.5.1  (v0.2 warm cache | v0.3 predictor | v0.4 batched math + profile | v0.5 one sync per layer + reusable buffers)  (Hugging Face transformers + plain Python, no servers)

For Gemma 4 MoE models. Everything EXCEPT the experts loads onto the GPU once.
The experts never load as a block. When a layer's router picks its 8 experts,
each one is served from:
    1. VRAM cache   (hot)
    2. RAM cache    (warm)
    3. SSD          (cold) - one seek + one read of that expert's exact bytes
Missing experts for a layer are read in parallel (queue depth > 1).
Every pick is logged -> results/heatmap_<tag>.json  (your first heat map).

    python clip_loader.py C:/models/gemma26b-split
    python clip_loader.py C:/models/gemma26b-split --tag code --warm     # learned cache from past runs
    python clip_loader.py C:/models/gemma26b-split --tag code --chat     # interactive coding chat

Options:
    --gpu-cache  GB of VRAM for hot experts   (default 4)
    --ram-cache  GB of RAM for warm experts   (default 6)
    --threads    parallel SSD reads           (default 8)
    --prompt     what to ask
    --tag        label for the heat map file (e.g. code, docs, chat)
"""
import argparse, collections, concurrent.futures as cf, glob, json, os, re, struct, sys, time
import torch
import torch.nn as nn
HEAT_DECODE_ONLY = os.environ.get("MULE_HEAT_DECODE") == "1"   # tool maps: count decode steps only

# ==========================================================================
#  EXPERT STORE: knows where every expert's bytes live on the SSD
# ==========================================================================
DT = {"BF16": (torch.bfloat16, 2), "F16": (torch.float16, 2), "F32": (torch.float32, 4)}

class ExpertStore:
    def __init__(self, folder, gpu_gb, ram_gb, threads, device, int8_dir=None):
        self.device = device
        self.int8 = int8_dir is not None
        self.loc = {}                                    # (layer, "gate_up"/"down") -> info
        for f in sorted(glob.glob(os.path.join(folder, "*.safetensors"))):
            with open(f, "rb") as fh:
                n = struct.unpack("<Q", fh.read(8))[0]
                hdr = json.loads(fh.read(n))
            for name, info in hdr.items():
                if name.startswith("mtp."):                 # Qwen's multi-token-prediction head: not used by transformers
                    continue
                m = re.search(r"layers\.(\d+)\.(?:mlp\.)?experts\.(gate_up_proj|down_proj)$", name)
                if m:
                    dtype, nb = DT[info["dtype"]]
                    E, a, b = info["shape"]
                    self.loc[(int(m.group(1)), m.group(2)[:-5])] = dict(
                        file=f, base=8 + n + info["data_offsets"][0],
                        per=a * b * nb, shape=(a, b), dtype=dtype)
        if not self.loc and not self.int8:
            sys.exit("No stacked expert tensors found - use the full model folder, or a slim folder with --int8")
        if self.int8:                                    # 8-bit experts from quantize_experts.py
            idx = json.load(open(os.path.join(int8_dir, "experts_int8.json")))
            for L, ent in idx["layers"].items():
                for part in ("gate_up", "down"):
                    p = ent[part]; rows, cols = p["shape"]
                    self.loc[(int(L), part)] = dict(file=os.path.join(int8_dir, ent["file"]),
                        q_base=p["q_offset"], s_base=p["s_offset"], q_per=rows * cols, s_per=rows * 4,
                        per=rows * cols + rows * 4, shape=(rows, cols))
        one = sum(v["per"] for k, v in self.loc.items() if k[0] == 0)
        self.expert_bytes = one
        self.gpu_cap = max(1, int(gpu_gb * 1024**3 // one))
        self.ram_cap = max(0, int(ram_gb * 1024**3 // one))
        self.pin_gpu, self.pin_ram = {}, {}              # hot experts loaded from the heat map, never evicted
        self.gpu = collections.OrderedDict()             # (L,e) -> (gate_up, down) on GPU
        self.ram = collections.OrderedDict()             # (L,e) -> (gate_up, down) in RAM
        self.handles = {}
        self.pool = cf.ThreadPoolExecutor(max_workers=threads)
        self.stats = collections.Counter()
        self.heat = collections.Counter()                # (L,e) -> times picked (per token)
        self.ssd_seconds = 0.0                           # time the GPU actually WAITED on the SSD
        self.pending = collections.OrderedDict()         # (L,e) -> Future, started early by the predictor
        self.prefetch_on = True
        print(f"[clip] experts {'8-bit' if self.int8 else '16-bit'}: one expert = {one/1024**2:.1f} MB | VRAM cache {self.gpu_cap} experts | "
              f"RAM cache {self.ram_cap} experts | {threads} read threads", flush=True)

    def _read(self, layer, e):                          # cold path: exact bytes from SSD
        if self.int8:                                   # -> (q_gate_up, s_gate_up, q_down, s_down)
            out = []
            for part in ("gate_up", "down"):
                info = self.loc[(layer, part)]; rows, cols = info["shape"]
                with open(info["file"], "rb") as fh:
                    q = torch.empty(info["q_per"], dtype=torch.uint8)
                    fh.seek(info["q_base"] + e * info["q_per"]); fh.readinto(q.numpy())
                    sc = torch.empty(rows, dtype=torch.float32)
                    fh.seek(info["s_base"] + e * info["s_per"]); fh.readinto(sc.numpy())
                out += [q.view(torch.int8).view(rows, cols), sc]
            return tuple(out)
        out = []
        for part in ("gate_up", "down"):
            info = self.loc[(layer, part)]
            buf = None
            if PIN[0] and not _pin_failed[0] and torch.cuda.is_available():
                try:                                    # pinned RAM: the GPU can pull it ~2x faster
                    t = torch.empty(info["per"], dtype=torch.uint8, pin_memory=True)
                    buf = t.numpy()
                except RuntimeError:
                    _pin_failed[0] = True
                    print("[clip] pinned memory unavailable - falling back to normal RAM", flush=True)
            if buf is None:
                t = torch.empty(info["per"], dtype=torch.uint8); buf = t.numpy()
            with open(info["file"], "rb") as fh:          # own handle per thread
                fh.seek(info["base"] + e * info["per"])
                fh.readinto(buf)
            out.append(t.view(info["dtype"]).view(info["shape"]))
        return tuple(out)

    def prefetch(self, layer, experts):
        """Predictor's guess for an upcoming layer: start reading those experts in the background."""
        if not self.prefetch_on:
            return
        for e in experts:
            k = (layer, e)
            if k in self.pin_gpu or k in self.gpu or k in self.pin_ram or k in self.ram or k in self.pending:
                continue
            self.pending[k] = self.pool.submit(self._read, layer, e)
            self.stats["prefetch_issued"] += 1
        while len(self.pending) > 96:                    # hard cap as a second safety net
            _, f = self.pending.popitem(last=False); f.cancel()

    def get(self, layer, experts):
        """Returns (ready, waiting): ready = {e: weights on GPU}, waiting = {e: Future} still reading."""
        ready, waiting = {}, {}
        for e in experts:
            k = (layer, e)
            if k in self.pin_gpu:
                ready[e] = self.pin_gpu[k]; self.stats["vram"] += 1; self.stats["pinned_vram"] += 1
            elif k in self.gpu:
                self.gpu.move_to_end(k); ready[e] = self.gpu[k]; self.stats["vram"] += 1
            elif k in self.pin_ram:
                self.stats["ram"] += 1; self.stats["pinned_ram"] += 1
                ready[e] = self._to_gpu(k, self.pin_ram[k])
            elif k in self.ram:
                self.ram.move_to_end(k); self.stats["ram"] += 1
                ready[e] = self._to_gpu(k, self.ram[k])
            elif k in self.pending:                      # the predictor guessed right
                waiting[e] = self.pending.pop(k); self.stats["prefetch_hit"] += 1
            else:                                        # nobody saw it coming: read now
                waiting[e] = self.pool.submit(self._read, layer, e); self.stats["ssd"] += 1
        # v0.5.3: unused guesses for this layer move into the RAM cache (bounded by --ram-cache)
        # so later tokens can still use them; reads that haven't started are cancelled.
        for kk in [kk for kk in self.pending if kk[0] == layer]:
            f = self.pending.pop(kk)
            if f.done() and not f.cancelled():
                if self.ram_cap - len(self.pin_ram) > 0:
                    self.ram[kk] = f.result(); self.stats["prefetch_kept"] += 1
                    while len(self.ram) > max(0, self.ram_cap - len(self.pin_ram)): self.ram.popitem(last=False)
            else:
                f.cancel(); self.stats["prefetch_discarded"] += 1
        return ready, waiting

    def finish(self, layer, e, fut):
        t0 = time.time()
        w = fut.result()
        self.ssd_seconds += time.time() - t0
        self.stats["ssd_bytes"] += self.expert_bytes
        if self.ram_cap - len(self.pin_ram) > 0:
            self.ram[(layer, e)] = w
            while len(self.ram) > max(0, self.ram_cap - len(self.pin_ram)): self.ram.popitem(last=False)
        return self._to_gpu((layer, e), w)

    def warm(self, ranked, pin_frac):
        """Pre-load the hottest experts from a heat map: top ones pinned in VRAM, next ones in RAM."""
        n_gpu = int(self.gpu_cap * pin_frac); n_ram = int(self.ram_cap * pin_frac)
        ranked = [k for k in ranked if (k[0], "down") in self.loc][: n_gpu + n_ram]
        t0 = time.time(); i = 0
        for start in range(0, len(ranked), 32):          # v0.5.3: stream in batches of 32 (no 9 GB RAM spike)
            chunk = ranked[start:start + 32]
            for k, w in zip(chunk, self.pool.map(lambda kk: self._read(*kk), chunk)):
                if i < n_gpu: self.pin_gpu[k] = tuple(t.to(self.device) for t in w)
                else:         self.pin_ram[k] = w
                i += 1
        gb = i * self.expert_bytes / 1024**3
        print(f"[clip] warm start: {len(self.pin_gpu)} hot experts pinned in VRAM, {len(self.pin_ram)} in RAM "
              f"({gb:.1f} GB in {time.time()-t0:.0f}s)", flush=True)

    def _to_gpu(self, k, w):
        g = tuple(t.to(self.device, non_blocking=True) for t in w)
        self.gpu[k] = g
        while len(self.gpu) > max(1, self.gpu_cap - len(self.pin_gpu)): self.gpu.popitem(last=False)
        return g

STORE = None
PROF = collections.Counter()                        # section -> seconds
PROFILE = [False]                                   # --profile: sync the GPU around each section for true timings
EXACT = [False]                                     # --exact: old v0.3 loop (default batched path matches stock exactly)
BATCH_MAX = 64                                      # use batched math when tokens*k <= this (i.e. while generating)

class timed:
    def __init__(self, name): self.name = name
    def __enter__(self):
        if PROFILE[0] and torch.cuda.is_available(): torch.cuda.synchronize()
        self.t = time.time()
    def __exit__(self, *a):
        if PROFILE[0] and torch.cuda.is_available(): torch.cuda.synchronize()
        PROF[self.name] += time.time() - self.t

PREFETCH_CAP = 48
PIN = [False]                                       # --pin: try pinned RAM (crashed on Windows from read threads; off by default)
OVERLAP = [True]                                    # --no-overlap: wait for all reads before any math
GUESS = {}                                          # layer -> predictor's guessed expert ids (still on GPU, no sync)
BUFS = {}                                           # reusable stacking buffers, allocated once
_pin_failed = [False]                                   # max guessed experts per layer step
_LAYER_COUNTER = [0]

# ==========================================================================
#  CLIP EXPERTS: drop-in replacement for Gemma4TextExperts (holds no weights)
# ==========================================================================
def deq(w, dtype):
    """16-bit experts pass through; 8-bit experts expand back to 16-bit on the GPU, right before use."""
    if len(w) == 2:
        return w
    qg, sg, qd, sd = w
    return (qg.to(dtype) * sg.to(dtype)[:, None], qd.to(dtype) * sd.to(dtype)[:, None])

def stack_into(name, tensors):
    """torch.stack into a reusable buffer (allocated once per shape)."""
    n, t0 = len(tensors), tensors[0]
    key = (name, tuple(t0.shape), t0.dtype)
    if key not in BUFS or BUFS[key].shape[0] < n:
        BUFS[key] = torch.empty((n,) + tuple(t0.shape), device=t0.device, dtype=t0.dtype)
    return torch.stack(tensors, out=BUFS[key][:n])

class ClipExperts(nn.Module):
    def __init__(self, config):
        super().__init__()
        from transformers.activations import ACT2FN
        self.num_experts = config.num_experts
        self.act_fn = ACT2FN[getattr(config, "hidden_activation", None) or config.hidden_act]
        self.layer_idx = _LAYER_COUNTER[0]; _LAYER_COUNTER[0] += 1

    def forward(self, hidden_states, top_k_index, top_k_weights):
        T, k = top_k_index.shape
        L = self.layer_idx
        with timed("cache lookup + RAM->GPU copies"):
            guess = GUESS.pop(L + 1, None)
            if guess is not None:                       # ONE sync brings back our picks AND next layer's guess
                both = torch.cat([top_k_index.flatten(), guess.flatten()]).tolist()
                flat, guessed = both[:T * k], both[T * k:]
            else:
                flat, guessed = top_k_index.flatten().tolist(), []
            if T == 1 or not HEAT_DECODE_ONLY:
                for e in flat:
                    STORE.heat[(L, e)] += 1
            ready, waiting = STORE.get(L, sorted(set(flat)))
            if guessed:                                 # queue next layer's likely experts AFTER our own reads
                top = [e for e, _ in collections.Counter(guessed).most_common(PREFETCH_CAP)]
                STORE.prefetch(L + 1, top)
        if not OVERLAP[0] and waiting:
            for e, fut in list(waiting.items()):
                ready[e] = STORE.finish(L, e, fut)
            waiting = {}

        if not EXACT[0] and T * k <= BATCH_MAX:
            for e, fut in list(waiting.items()):        # batched path: get everything first (no extra syncs)
                ready[e] = STORE.finish(L, e, fut)
            waiting = {}
            with timed("expert math"):
                n = T * k
                dt_ = hidden_states.dtype
                parts = [stack_into(i, [ready[e][i] for e in flat]) for i in range(len(ready[flat[0]]))]
                if len(parts) == 2:
                    gu, dn = parts
                else:                                   # 8-bit: expand the whole batch at once
                    gu = parts[0].to(dt_) * parts[1].to(dt_)[..., None]
                    dn = parts[2].to(dt_) * parts[3].to(dt_)[..., None]
                x = hidden_states.repeat_interleave(k, dim=0).unsqueeze(-1)
                gate, up = torch.bmm(gu, x).squeeze(-1).chunk(2, dim=-1)
                y = torch.bmm(dn, (self.act_fn(gate) * up).unsqueeze(-1)).squeeze(-1)
                y = y.float() * top_k_weights.reshape(-1, 1).float()
                return y.view(T, k, -1).sum(dim=1).to(hidden_states.dtype)

        # ONE AT A TIME (prefill of long prompts, or --exact)
        for e, fut in list(waiting.items()):
            ready[e] = STORE.finish(L, e, fut)
        with timed("expert math"):
            final = torch.zeros_like(hidden_states)
            for e in sorted(ready):
                gu, dn = deq(ready[e], hidden_states.dtype)
                pos, tok = torch.where(top_k_index.T == e)
                xs = hidden_states[tok]
                gate, up = nn.functional.linear(xs, gu).chunk(2, dim=-1)
                yy = nn.functional.linear(self.act_fn(gate) * up, dn)
                yy = yy * top_k_weights[tok, pos, None]
                final.index_add_(0, tok, yy.to(final.dtype))
            return final

# Which class builds the experts, and what the experts class is called, per model family.
ARCH = {
    "gemma4":      ("transformers.models.gemma4.modeling_gemma4", "Gemma4TextDecoderLayer", "Gemma4TextExperts"),
    "qwen3_5_moe": ("transformers.models.qwen3_5_moe.modeling_qwen3_5_moe", "Qwen3_5MoeSparseMoeBlock", "Qwen3_5MoeExperts"),
}
ARCH["gemma4_text"] = ARCH["gemma4"]; ARCH["qwen3_5_moe_text"] = ARCH["qwen3_5_moe"]

def load_clip_model(path, gpu_gb, ram_gb, threads, device, int8_dir=None):
    global STORE
    import importlib
    cfg = json.load(open(os.path.join(path, "config.json")))
    mtype = cfg.get("model_type")
    if mtype not in ARCH:
        sys.exit(f"clip: model type '{mtype}' not supported yet (supported: {', '.join(ARCH)})")
    mod_name, builder_name, experts_name = ARCH[mtype]
    G = importlib.import_module(mod_name)
    Builder = getattr(G, builder_name)
    original = getattr(G, experts_name)
    orig_init = Builder.__init__
    def layer_init(self, *args, **kwargs):              # swap ONLY while each block is built
        setattr(G, experts_name, ClipExperts)
        try: orig_init(self, *args, **kwargs)
        finally: setattr(G, experts_name, original)
    Builder.__init__ = layer_init
    _LAYER_COUNTER[0] = 0
    STORE = ExpertStore(path, gpu_gb, ram_gb, threads, device, int8_dir)
    from transformers import AutoModelForCausalLM, logging as hf_logging
    hf_logging.set_verbosity_error()                    # hides "unused expert weights" notice
    t0 = time.time()
    try:
        model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16, device_map=device)
    finally:
        Builder.__init__ = orig_init
    model.eval()
    # PREDICTOR: when layer L's router runs, also run layer L+1's router on the same input
    # and start reading its likely experts in the background.
    routers = {}
    for mod in model.modules():
        ex = getattr(mod, "experts", None)
        if isinstance(ex, ClipExperts):
            r = getattr(mod, "router", None) or getattr(mod, "gate", None)
            if r is not None:
                routers[ex.layer_idx] = r
    def make_hook(nxt):
        def hook(module, args):
            if not STORE.prefetch_on:
                return
            with timed("predictor"), torch.no_grad():
                _, _, idx = nxt.forward(args[0])    # .forward() skips nxt's own hook (no chain reaction)
                GUESS[nxt_layer[id(nxt)]] = idx     # stays on the GPU; read back in this layer's existing sync
        return hook
    nxt_layer = {}
    for L, r in routers.items():
        if L + 1 in routers:
            nxt_layer[id(routers[L + 1])] = L + 1
            r.register_forward_pre_hook(make_hook(routers[L + 1]))
    print(f"[clip] predictor wired across {len(nxt_layer)} layer pairs", flush=True)
    print(f"[clip] non-expert weights on {device} in {time.time()-t0:.0f}s, "
          f"{_LAYER_COUNTER[0]} MoE layers wired to the clip", flush=True)
    return model

# ==========================================================================

# ==========================================================================
#  CHAT MODE: talk to the model, run its code, feed errors back
# ==========================================================================
CHAT_HELP = """
  Type a message and press Enter.
  /paste   multi-line input; finish with a line containing only /end
  /run     run the last Python code block the model wrote (30 s limit)
  /fix     send the last /run error back to the model and ask for a fix
  /save F  save the last code block to file F
  /reset   forget the conversation
  /help    this list          /exit   quit (heat map is saved)
"""

def _last_code(history):
    for m in reversed(history):
        if m["role"] == "assistant":
            blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", m["content"], re.S)
            if blocks:
                return blocks[-1]
    return None

def save_heat(heat_path, old, tag):
    cum = collections.Counter(STORE.heat)
    if old:
        for l, e, c in old["picks"]: cum[(l, e)] += c
    os.makedirs(os.path.dirname(heat_path) or ".", exist_ok=True)
    with open(heat_path, "w") as f:
        json.dump({"tag": tag, "runs": (old or {}).get("runs", 0) + 1, "last_prompt": "chat",
                   "picks": [[l, e, c] for (l, e), c in cum.most_common()]}, f)

DEFAULT_SYSTEM = ("You are a precise coding assistant. Put the code FIRST, complete, in one ```python block. "
                  "After the code, give at most three short bullet notes. No sample output unless asked.")

def _start(a):
    return [{"role": "system", "content": a.system}] if a.system else []

def chat_loop(model, tok, device, a, heat_path, old):
    import subprocess, tempfile
    from transformers import TextStreamer
    history, last_err, runs = _start(a), None, 0
    mem = {"cache": None}                               # the model's memory of the conversation so far
    os.makedirs("chat_runs", exist_ok=True)
    print("\n=== CHAT MODE ===" + CHAT_HELP, flush=True)
    while True:
        try:
            msg = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            msg = "/exit"
        if not msg:
            continue
        if msg == "/exit":
            save_heat(heat_path, old, a.tag); print(f"heat map saved -> {heat_path}"); return
        if msg == "/help":
            print(CHAT_HELP); continue
        if msg == "/reset":
            history, last_err = _start(a), None; mem["cache"] = None; print("(conversation cleared)"); continue
        if msg.startswith("/save"):
            code = _last_code(history); name = msg[5:].strip() or "saved_code.py"
            if code: open(name, "w", encoding="utf-8").write(code); print(f"(saved {name})")
            else: print("(no code block yet)")
            continue
        if msg == "/run":
            code = _last_code(history)
            if not code:
                print("(no code block yet)"); continue
            runs += 1; path = os.path.join("chat_runs", f"run_{runs:03d}.py")
            open(path, "w", encoding="utf-8").write(code)
            try:
                r = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=30)
                out, err = r.stdout[-3000:], r.stderr[-3000:]
            except subprocess.TimeoutExpired:
                out, err = "", "TIMEOUT: ran longer than 30 seconds"
            print(f"--- ran {path} ---\n{out}" + (f"\n--- errors ---\n{err}" if err else "\n(no errors)"))
            last_err = err or None
            continue
        if msg == "/fix":
            if not last_err:
                print("(nothing to fix: /run first, and it must have failed)"); continue
            msg = "Running your code gave this error:\n" + last_err + "\nFix the code. Return the full corrected code in one ```python block."
        if msg == "/paste":
            lines = []
            while True:
                ln = input()
                if ln.strip() == "/end": break
                lines.append(ln)
            msg = "\n".join(lines)
        history.append({"role": "user", "content": msg})
        enc = tok.apply_chat_template(history, add_generation_prompt=True, return_tensors="pt",
                                      return_dict=True, enable_thinking=a.think).to(device)
        ids = enc["input_ids"]
        # v0.9.1: keep the EXACT tokens the model generated last turn, and only format what's new
        # (end of its turn + your new message). Re-formatting its answer through the template
        # changes the tokens, which breaks the memory.
        if mem["cache"] is not None and not a.no_memory and len(history) >= 3 and history[-2]["role"] == "assistant":
            full = tok.apply_chat_template(history, add_generation_prompt=True, tokenize=False, enable_thinking=a.think)
            anchor = history[-2]["content"].strip()
            pos = full.rfind(anchor) if anchor else -1
            if pos >= 0:
                prev = mem["ids"][0]
                special = set(tok.all_special_ids)
                end = prev.shape[-1]
                while end > 0 and int(prev[end - 1]) in special: end -= 1     # drop its end-of-turn token(s)
                tail = tok(full[pos + len(anchor):], add_special_tokens=False, return_tensors="pt")["input_ids"]
                ids = torch.cat([prev[:end].unsqueeze(0), tail.to(prev.device)], dim=-1)
        n_in = ids.shape[-1]
        # MEMORY: reuse the cache if the new prompt starts with exactly what the model already processed
        cache, reused = mem["cache"], 0
        if cache is not None and not a.no_memory:
            done = mem["ids"][:, :cache.get_seq_length()]
            L = done.shape[-1]
            if L <= n_in and torch.equal(ids[:, :L], done):
                reused = L
            else:                                           # try to rewind to the common part
                p = 0
                m_ = min(L, n_in)
                while p < m_ and ids[0, p] == done[0, p]: p += 1
                try:
                    cache.crop(p); reused = p
                except Exception:
                    cache, reused = None, 0                 # can't rewind (e.g. Qwen's linear attention): start fresh
        else:
            cache = None
        print("model> ", end="", flush=True)
        streamer = TextStreamer(tok, skip_prompt=True, skip_special_tokens=True)
        t0 = time.time()
        with torch.no_grad():
            out = model.generate(input_ids=ids, attention_mask=torch.ones_like(ids), past_key_values=cache,
                                 max_new_tokens=a.tokens, do_sample=False, streamer=streamer,
                                 return_dict_in_generate=True)
        dt = time.time() - t0; seq = out.sequences; n = seq.shape[-1] - n_in
        mem["cache"], mem["ids"] = out.past_key_values, seq
        reply = tok.decode(seq[0][n_in:], skip_special_tokens=True)
        history.append({"role": "assistant", "content": reply})
        cut = " | HIT THE TOKEN LIMIT (raise --tokens)" if n >= a.tokens else ""
        print(f"\n[{n} tokens in {dt:.0f}s = {n/dt:.2f} tok/s | prompt {n_in} tokens, "
              f"{reused} remembered, {n_in - reused} newly read{cut}]", flush=True)
        save_heat(heat_path, old, a.tag)                # keep learning as you chat

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--gpu-cache", type=float, default=4)
    ap.add_argument("--ram-cache", type=float, default=6)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--tokens", type=int, default=120)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--warm", action="store_true", help="pre-load hot experts from results/heatmap_<tag>.json")
    ap.add_argument("--profile", action="store_true", help="exact per-section timings (adds a little overhead)")
    ap.add_argument("--exact", action="store_true", help="old one-expert-at-a-time loop (v0.3 math; for comparison)")
    ap.add_argument("--chat", action="store_true", help="interactive chat: talk, /run its code, /fix errors")
    ap.add_argument("--system", default=DEFAULT_SYSTEM, help='chat instruction; --system "" turns it off')
    ap.add_argument("--no-memory", action="store_true", help="chat: re-read the whole conversation every turn")
    ap.add_argument("--think", action="store_true", help="let thinking models (Qwen) reason before answering")
    ap.add_argument("--int8", default=None, help="folder made by quantize_experts.py: use 8-bit experts")
    ap.add_argument("--pin", action="store_true", help="EXPERIMENTAL pinned RAM tier (crashed on Windows in v0.5)")
    ap.add_argument("--no-overlap", action="store_true", help="(--exact path only) wait for all reads before math")
    ap.add_argument("--no-prefetch", action="store_true", help="turn the predictor off (for A/B tests)")
    ap.add_argument("--pin-frac", type=float, default=0.8, help="share of each cache reserved for pinned hot experts")
    ap.add_argument("--prompt", default="Write a Python function that merges overlapping intervals. Code only.")
    a = ap.parse_args()
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    PIN[0] = a.pin

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.path)
    model = load_clip_model(a.path, a.gpu_cache, a.ram_cache, a.threads, device, a.int8)
    if torch.cuda.is_available():
        print(f"[clip] VRAM after load: {torch.cuda.memory_allocated()/1024**3:.1f} GB", flush=True)

    STORE.prefetch_on = not a.no_prefetch
    PROFILE[0] = a.profile; EXACT[0] = a.exact; OVERLAP[0] = not a.no_overlap
    GUESS.clear()
    model_name = os.path.basename(os.path.normpath(a.path))
    heat_path = f"results/heatmap_{model_name}_{a.tag}.json"
    if not os.path.exists(heat_path) and os.path.exists(f"results/heatmap_{a.tag}.json") \
            and json.load(open(os.path.join(a.path, "config.json"))).get("model_type") == "gemma4":
        import shutil; shutil.copy(f"results/heatmap_{a.tag}.json", heat_path)   # migrate the earlier Gemma file
    old = json.load(open(heat_path)) if os.path.exists(heat_path) else None
    if a.warm:
        if old:
            STORE.warm([(l, e) for l, e, c in old["picks"]], a.pin_frac)
        else:
            print(f"[clip] no {heat_path} yet - running cold. Run once without --warm first.")

    if a.chat:
        if old and not a.warm:
            STORE.warm([(l, e) for l, e, c in old["picks"]], a.pin_frac)
        if a.tokens == 120: a.tokens = 2048            # chat answers can be longer
        return chat_loop(model, tok, device, a, heat_path, old)

    enc = tok.apply_chat_template([{"role": "user", "content": a.prompt}], add_generation_prompt=True,
                                  return_tensors="pt", return_dict=True,
                                  enable_thinking=a.think).to(device)
    n_in = enc["input_ids"].shape[-1]
    print("[clip] generating...", flush=True)
    t1 = time.time()
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=a.tokens, do_sample=False)
    dt = time.time() - t1
    n = out.shape[-1] - n_in

    for f in STORE.pending.values(): f.cancel()
    s = STORE.stats; picks = s["vram"] + s["ram"] + s["prefetch_hit"] + s["ssd"]
    print("\n" + "-" * 60); print(tok.decode(out[0][n_in:], skip_special_tokens=True)); print("-" * 60)
    print(f"RESULT: {n} tokens in {dt:.0f}s -> {n/dt:.2f} tok/s ({dt/max(n,1):.1f} s/token)")
    print(f"Expert requests: {picks}  |  VRAM {s['vram']/max(picks,1):.0%}  RAM {s['ram']/max(picks,1):.0%}  "
          f"prefetched {s['prefetch_hit']/max(picks,1):.0%}  SSD on-demand {s['ssd']/max(picks,1):.0%}")
    if STORE.prefetch_on:
        need = s["prefetch_hit"] + s["ssd"]
        print(f"PREDICTOR: caught {s['prefetch_hit']/max(need,1):.0%} of cache misses before they were needed; "
              f"{s['prefetch_hit']/max(s['prefetch_issued'],1):.0%} of its guesses were used")
    print(f"Read from SSD: {s['ssd_bytes']/1024**3:.1f} GB  |  GPU waited on SSD {STORE.ssd_seconds:.0f}s of {dt:.0f}s")
    print(f"Unused guesses kept in RAM cache: {s['prefetch_kept']}  cancelled: {s['prefetch_discarded']}  |  "
          f"still pending at end: {len(STORE.pending)} (~{len(STORE.pending)*STORE.expert_bytes/1024**3:.2f} GB)")
    try:
        import psutil
        print(f"Process RAM at end: {psutil.Process().memory_info().rss/1024**3:.1f} GB  |  "
              f"system RAM in use: {psutil.virtual_memory().used/1024**3:.1f} of {psutil.virtual_memory().total/1024**3:.1f} GB")
    except ImportError:
        print("(pip install psutil to have RAM use printed here)")
    print(f"\nTIME BREAKDOWN ({'exact, GPU-synced' if a.profile else 'approximate - add --profile for exact'}):")
    parts = dict(PROF); parts["SSD wait"] = STORE.ssd_seconds
    parts["everything else (attention, shared MLP, sampling)"] = max(0.0, dt - sum(parts.values()))
    for name, sec in sorted(parts.items(), key=lambda kv: -kv[1]):
        print(f"  {name:52s} {sec:6.1f}s  {100*sec/dt:5.1f}%")
    if torch.cuda.is_available():
        print(f"Peak VRAM: {torch.cuda.max_memory_allocated()/1024**3:.1f} GB")

    # heat map: share of experts carrying 80% of picks
    heat = STORE.heat; tot = sum(heat.values()); run = k80 = 0
    for _, v in heat.most_common():
        run += v; k80 += 1
        if run >= 0.8 * tot: break
    total_experts = len({k[0] for k in STORE.loc}) * model.config.get_text_config().num_experts
    print(f"HEAT: {len(heat)} of {total_experts} experts used; {k80} of them carried 80% of picks "
          f"({k80/total_experts:.1%} of all experts)")
    os.makedirs("results", exist_ok=True)
    cum = collections.Counter(heat)                     # heat map ACCUMULATES across runs
    if old:
        for l, e, c in old["picks"]: cum[(l, e)] += c
    with open(heat_path, "w") as f:
        json.dump({"tag": a.tag, "runs": (old or {}).get("runs", 0) + 1, "last_prompt": a.prompt,
                   "picks": [[l, e, c] for (l, e), c in cum.most_common()]}, f)
    with open("results/runs.jsonl", "a") as f:
        f.write(json.dumps({"test": "CLIP", "tag": a.tag, "tok_s": round(n/dt, 3), "tokens": n,
                            "prompt": a.prompt, "profile_flag": a.profile,
                            "gpu_cache_gb": a.gpu_cache, "ram_cache_gb": a.ram_cache,
                            "vram_hit": round(s["vram"]/max(picks,1), 3), "ram_hit": round(s["ram"]/max(picks,1), 3),
                            "prefetch": not a.no_prefetch, "prefetch_hit": round(s["prefetch_hit"]/max(picks,1), 3),
                            "ssd_wait_s": round(STORE.ssd_seconds, 1), "gpu_cache_used": a.gpu_cache,
                            "ssd_gb": round(s["ssd_bytes"]/1024**3, 2), "experts_80pct": k80,
                            "warm": a.warm, "exact": a.exact, "int8": bool(a.int8), "pin": a.pin, "overlap": not a.no_overlap,
                            "pin_fell_back": _pin_failed[0], "profile": {k: round(v, 1) for k, v in PROF.items()}, "pinned_vram": len(STORE.pin_gpu), "pinned_ram": len(STORE.pin_ram)}) + "\n")
    print(f"Heat map updated (cumulative) -> {heat_path}")

if __name__ == "__main__":
    main()
