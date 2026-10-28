#!/usr/bin/env python3
"""cascade_plan.py - Project Cascade, Steps 11 to 18: the harness layer.

  python3 cascade_plan.py catalog                  what the harness may plan with (paste it into the session)
  python3 cascade_plan.py expand  plan.json        the primitive blocks a compact plan turns into (nothing runs)
  python3 cascade_plan.py dry-run plan.json        the whole pipeline on a throwaway copy of the workspace: no mule, nothing real touched
  python3 cascade_plan.py run     plan.json        dry run first, then the real chain (use the venv python: it loads Gemma)
  python3 cascade_plan.py undo    <plan id>        restore the plan's backups, newest change first (refuses if later work depends on them)
  python3 cascade_plan.py status                   the plans so far

The part that reads English is the harness model. This tool is the deterministic side: it fills in what the registry knows,
runs every guard before anything real happens, and can take a plan back out.
"""
import argparse, atexit, difflib, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time

OK_STATUS = ("ACK", "APPLIED", "NOOP_ALREADY_CONNECTED", "NOOP_ALREADY_DEFINED", "NOOP_ALREADY_PRESENT")
NOOP_STATUS = ("NOOP_ALREADY_CONNECTED", "NOOP_ALREADY_DEFINED", "NOOP_ALREADY_PRESENT")
EXC = {"add_method": ["KinematicBody2D", "untyped_variables", "yield", "relative_paths_up_tree", "cross_subnet_queries"],
       "connect_signal": ["KinematicBody2D", "connect_3arg", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"],
       "modify_property": ["KinematicBody2D", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"],
       "add_node": ["KinematicBody2D", "untyped_variables", "relative_paths_up_tree", "cross_subnet_queries"],
    "set_physics_body": ["KinematicBody2D", "untyped_variables", "move_and_slide_args", "relative_paths_up_tree", "cross_subnet_queries"],
}
NODE_CLASSES = ("Area2D", "Timer", "Marker2D", "Node2D")
MAX_NODES_PER_PLAN = 8
RELATIVE_TYPES = ("float", "int", "Vector2", "Vector3")
RELATIVE_VERBS = (("increase", "+=", "grows by"), ("decrease", "-=", "shrinks by"), ("scale", "*=", "is multiplied by"))
MAX_STATEMENTS = 6
STEP_KEYS = {"when": {"signal", "then"}, "set_property": {"property", "value"}, "add_function": {"name", "then"}, "connect": {"signal", "handler"}, "change_handler": {"handler", "then"}, "after": {"seconds", "then"}, "every": {"seconds", "then"}, "add_node": {"class", "name"}, "add_wall": {"name", "size"}, "add_camera": {"name"}, "add_sprite": {"name", "texture"},
    "move_with_keys": set(),
}
# Step 14: "from" names the node that emits the signal (the step's node receives it). Step 15: "node" lets one step work on another node than the plan's; "timer" names the Timer an "after" uses.
OPTIONAL_KEYS = {"when": {"from", "node", "only"}, "connect": {"from", "node"}, "set_property": {"node"}, "add_function": {"node"}, "change_handler": {"node"}, "after": {"timer", "node"}, "every": {"timer", "node"}, "add_node": {"at", "size", "parent", "node"}, "add_wall": {"at", "parent", "node"}, "add_camera": {"zoom", "smoothing", "speed", "node"}, "add_sprite": {"centered", "node"},
    "move_with_keys": {"node", "template", "left", "right", "up", "down"},
}


class PlanError(Exception):
    pass


def load_json(p):
    return json.load(open(p, encoding="utf-8"))


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest() if os.path.exists(path) else None


class Ctx:
    def __init__(self, a):
        self.a = a
        self.ws = os.path.abspath(os.path.expanduser(a.ws))
        self.project = os.path.join(self.ws, "engine_test")
        if not os.path.exists(os.path.join(self.ws, "cascade_clock.py")):
            sys.exit(f"STOPPED: cascade_clock.py is not in {self.ws}")
        sys.path.insert(0, self.ws)
        import cascade_clock
        self.clock = cascade_clock
        self.reg = load_json(os.path.join(self.ws, "state_registry.json"))
        vp = os.path.join(self.ws, "cascade_vocab.json")
        self.vocab = load_json(vp) if os.path.exists(vp) else {"signals": {}, "steps": [], "supported_intents": [], "refusals": [], "values": ""}
        self._preview = None

    def preview_add_node(self, block):
        """Apply an add_node block to a temporary copy of the workspace and rebuild its registry, so later steps of the same plan can name the new node."""
        c = self.clock
        if self._preview is None:
            tmp = tempfile.mkdtemp(prefix="cascade_preview_"); t = os.path.join(tmp, "ws")
            shutil.copytree(self.ws, t, ignore=shutil.ignore_patterns("sweep", "dead_letter", "plans", "*.bak", "__pycache__", "ledger.jsonl", "*.before-*"))
            self._preview = t
            atexit.register(shutil.rmtree, tmp, True)
        t = self._preview; proj = os.path.join(t, "engine_test")
        reg = load_json(os.path.join(t, "state_registry.json")); spec = load_json(os.path.join(t, "task_block_spec.json"))
        c.set_godot(self.a.godot)
        b = dict(block); b["frame_id"] = "0x7FF0"
        host, bad = c.slot1(b, reg, spec, proj)
        if bad:
            raise PlanError("; ".join(bad))
        if not c.node_matches(proj, host, b):
            try:
                res = c.apply_scene_add_node(self.a.godot, proj, host, b)
            except c.ApplyError as e:
                raise PlanError(f"the node could not be added: {e}")
            if not res.get("verified"):
                raise PlanError(f"the node could not be added: {res.get('problem')}")
            rr = c.refresh_registry_node(t, self.a.godot, b)
            if not rr.get("ok") or not rr.get("listed"):
                raise PlanError("the registry could not list the new node: " + str(rr.get("detail") or "not listed"))
        self.reg = load_json(os.path.join(t, "state_registry.json"))

    def hosts(self):
        return list(self.reg["hosts"].values())

    def host_named(self, node):
        if node is None:
            if len(self.hosts()) != 1:
                raise PlanError("the plan must name its node: " + ", ".join(h["name"] for h in self.hosts()))
            return self.hosts()[0]
        for h in self.hosts():
            if h["name"] == node:
                return h
        raise PlanError(f"node {node} is not in the registry (nodes: {', '.join(h['name'] for h in self.hosts())})")


# ------------------------------------------------------------------ expansion: compact steps -> primitive blocks
def body_from_then(then, host, where):
    if not isinstance(then, dict):
        raise PlanError(f"{where}: 'then' must be an object")
    if set(then) == {"nothing"} and then["nothing"] is True:
        return {"body": "pass", "message": ""}
    if set(then) == {"print"} and isinstance(then["print"], str):
        return {"body": "print", "message": then["print"]}
    if set(then) == {"set"} and isinstance(then["set"], dict) and set(then["set"]) == {"property", "value"}:
        prop = then["set"]["property"]
        if prop not in host["props"]:
            raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
        return {"body": "assign", "message": "", "property": prop, "type": host["props"][prop], "value": then["set"]["value"]}
    for verb, op, _ in RELATIVE_VERBS:
        if set(then) == {verb} and isinstance(then[verb], dict) and set(then[verb]) == {"property", "by"}:
            prop = then[verb]["property"]
            if prop not in host["props"]:
                raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
            if host["props"][prop] not in RELATIVE_TYPES:
                raise PlanError(f"{where}: {verb} works on {', '.join(RELATIVE_TYPES)} properties, but {prop} is {host['props'][prop]}")
            return {"body": "assign", "message": "", "property": prop, "type": host["props"][prop], "operator": op, "value": then[verb]["by"]}
    raise PlanError(f"{where}: 'then' must be exactly one of {{\"set\": {{\"property\": P, \"value\": V}}}}, {{\"increase\"|\"decrease\"|\"scale\": {{\"property\": P, \"by\": V}}}}, {{\"print\": \"message\"}}, {{\"nothing\": true}}")


def statement_from_form(form, host, where):
    """One item of a 'then' list as a statement object, typed from the registry."""
    if not isinstance(form, dict):
        raise PlanError(f"{where}: every item of 'then' must be an object")
    if set(form) == {"nothing"} and form["nothing"] is True:
        return {"kind": "pass"}
    if set(form) == {"print"} and isinstance(form["print"], str):
        return {"kind": "print", "message": form["print"]}
    if set(form) == {"set"} and isinstance(form["set"], dict) and set(form["set"]) == {"property", "value"}:
        prop = form["set"]["property"]
        if prop not in host["props"]:
            raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
        return {"kind": "assign", "property": prop, "type": host["props"][prop], "value": form["set"]["value"]}
    for verb, op, _ in RELATIVE_VERBS:
        if set(form) == {verb} and isinstance(form[verb], dict) and set(form[verb]) == {"property", "by"}:
            prop = form[verb]["property"]
            if prop not in host["props"]:
                raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
            if host["props"][prop] not in RELATIVE_TYPES:
                raise PlanError(f"{where}: {verb} works on {', '.join(RELATIVE_TYPES)} properties, but {prop} is {host['props'][prop]}")
            return {"kind": "relative", "property": prop, "type": host["props"][prop], "operator": op, "value": form[verb]["by"]}
    if set(form) == {"limit"} and isinstance(form["limit"], dict) and "property" in form["limit"] and len(form["limit"]) == 2 and (set(form["limit"]) - {"property"}) <= {"max", "min", "max_length"}:
        prop, bound = form["limit"]["property"], next(iter(set(form["limit"]) - {"property"}))
        if prop not in host["props"]:
            raise PlanError(f"{where}: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
        t = host["props"][prop]
        if t in ("Vector2", "Vector3"):
            if bound != "max_length":
                raise PlanError(f"{where}: {prop} is a {t}; limit it with max_length (the longest the vector may get)")
        elif t in ("float", "int"):
            if bound not in ("max", "min"):
                raise PlanError(f"{where}: {prop} is a {t}; limit it with max (never above) or min (never below)")
        else:
            raise PlanError(f"{where}: a limit works on float, int, Vector2 and Vector3 properties, but {prop} is {t}")
        return {"kind": "limit", "property": prop, "type": t, "bound": bound, "value": form["limit"][bound]}
    if set(form) == {"free"} and isinstance(form["free"], str) and (form["free"] == "self" or re.match(r"^%[A-Za-z_]\w*$", form["free"])):
        return {"kind": "free", "target": form["free"]}
    raise PlanError(f"{where}: each 'then' item must be exactly one of {{\"set\": {{\"property\": P, \"value\": V}}}}, {{\"increase\"|\"decrease\"|\"scale\": {{\"property\": P, \"by\": V}}}}, {{\"print\": \"message\"}}, {{\"nothing\": true}}, {{\"limit\": {{\"property\": P, \"max\"|\"min\"|\"max_length\": V}}}}, {{\"free\": \"%Node\"|\"self\"}}")


def body_from_then_any(then, host, where, force_array=False):
    """A single form keeps the proven single-statement block; a list of two or more (or a replacement) becomes an array body."""
    if isinstance(then, list):
        if not then or len(then) > MAX_STATEMENTS:
            raise PlanError(f"{where}: 'then' as a list holds 1 to {MAX_STATEMENTS} items, it has {len(then)}")
        sts = [statement_from_form(f, host, f"{where}, item {i}") for i, f in enumerate(then, 1)]
        if len(sts) == 1 and sts[0]["kind"] == "limit":
            raise PlanError(f"{where}: a limit needs a relative change (increase, decrease, scale) on the same property before it; a limit by itself bounds nothing")
        if len(sts) == 1 and not force_array and sts[0]["kind"] != "free":
            return body_from_then(then[0], host, where)
        return {"statements": sts}
    if force_array or (isinstance(then, dict) and set(then) == {"free"}):
        return {"statements": [statement_from_form(then, host, where)]}
    return body_from_then(then, host, where)


def body_relatives(body):
    """(property, operator, value) of every relative statement in a body, whichever form it is in."""
    if "statements" in body:
        return [(s["property"], s["operator"], s["value"]) for s in body["statements"] if s["kind"] == "relative"]
    return [(body["property"], body["operator"], body["value"])] if body.get("operator", "=") != "=" else []


def head(host, op):
    return {"frame_id": None, "time_slot": "SLOT_3_EXECUTE", "subnet": host["subnet"], "op": op}


def add_method_block(host, fn, body, mode=None):
    b = head(host, "add_method")
    if "statements" in body:
        b.update(target=host["name"], fn=fn, args=[], returns="void", body=body["statements"], message="")
    else:
        b.update(target=host["name"], fn=fn, args=[], returns="void", body=body["body"], message=body["message"])
        for k in ("property", "type", "operator", "value"):
            if k in body:
                b[k] = body[k]
    if mode:
        b["mode"] = mode
    b.update(ref=host["addr"], exclude=list(EXC["add_method"]))
    return b


def connect_block(host, signal, method, src=None):
    b = head(host, "connect_signal")
    b.update(source=(src or host)["name"], target=host["name"], signal=signal, method=method, ref=host["addr"], exclude=list(EXC["connect_signal"]))
    return b


def modify_block(host, prop, value):
    if prop not in host["props"]:
        raise PlanError(f"set_property: '{prop}' is not a registered property of {host['name']} (registered: {', '.join(sorted(host['props']))})")
    b = head(host, "modify_property")
    b.update(target=host["name"], property=prop, type=host["props"][prop], value=value, ref=host["addr"], exclude=list(EXC["modify_property"]))
    return b


def expand_plan(ctx, plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or not plan["steps"]:
        raise PlanError("a plan is an object with a non-empty 'steps' list")
    if not isinstance(plan.get("intent"), str) or not plan["intent"].strip():
        raise PlanError("a plan needs an 'intent' (the user's words)")
    host = ctx.host_named(plan.get("node"))
    snake, blocks, notes = ctx.clock.snake, [], []
    for n, step in enumerate(plan["steps"], 1):
        where = f"step {n}"
        do = step.get("do") if isinstance(step, dict) else None
        if do not in STEP_KEYS:
            raise PlanError(f"{where}: unknown step '{do}' (steps: {', '.join(STEP_KEYS)})")
        extra = set(step) - STEP_KEYS[do] - OPTIONAL_KEYS.get(do, set()) - {"do"}
        missing = STEP_KEYS[do] - set(step)
        if extra or missing:
            raise PlanError(f"{where} ({do}): " + (f"unexpected {sorted(extra)} " if extra else "") + (f"missing {sorted(missing)}" if missing else ""))
        hh = ctx.host_named(step["node"]) if "node" in step else host
        src = hh
        if do in ("when", "connect") and "from" in step:
            src = ctx.host_named(step["from"])
            if src["name"] == hh["name"]:
                raise PlanError(f"{where}: 'from' names the plan's own node {hh['name']}; leave 'from' out for a signal of the node itself")
            if src["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {src['name']} is in {src['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
        if do in ("when", "connect") and step["signal"] not in src.get("signals", []):
            close = difflib.get_close_matches(str(step["signal"]), src.get("signals", []), n=3)
            raise PlanError(f"{where}: '{step['signal']}' is not a signal of {src['name']}" + (f" (did you mean {', '.join(close)}?)" if close else ""))
        if do == "add_node":
            cls, name = step["class"], step["name"]
            n_added = sum(1 for b0 in blocks if b0["op"] == "add_node") + 1
            if n_added > MAX_NODES_PER_PLAN:
                raise PlanError(f"{where}: a plan adds at most {MAX_NODES_PER_PLAN} nodes")
            if cls not in NODE_CLASSES:
                raise PlanError(f"{where}: class '{cls}' cannot be added (one of {', '.join(NODE_CLASSES)})")
            if not isinstance(name, str):
                raise PlanError(f"{where}: 'name' must be text")
            at, size, parent = step.get("at"), step.get("size"), step.get("parent", ".")
            if cls == "Timer" and at is not None:
                raise PlanError(f"{where}: a Timer has no position; leave 'at' out")
            if cls != "Timer":
                at = [0, 0] if at is None else at
                if not (isinstance(at, list) and len(at) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in at)):
                    raise PlanError(f"{where}: 'at' must be [x, y], two numbers")
            if cls == "Area2D":
                size = [48, 48] if size is None else size
                if not (isinstance(size, list) and len(size) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in size)):
                    raise PlanError(f"{where}: 'size' must be [width, height], two numbers")
            elif size is not None:
                raise PlanError(f"{where}: 'size' is only for an Area2D (the size of its rectangle shape)")
            blk = head(hh, "add_node")
            blk.update(target=hh["name"], ref=hh["addr"], exclude=list(EXC["add_node"]))
            blk.update({"class": cls, "name": name, "parent": parent})
            if cls != "Timer":
                blk["position"] = [float(at[0]), float(at[1])]
            if cls == "Area2D":
                blk["size"] = [float(size[0]), float(size[1])]
            ctx.preview_add_node(blk)
            blocks.append(blk)
            newh = next((h for h in ctx.hosts() if h["name"] == "%" + name), None)
            notes.append(f"add_node: %{name} ({cls}) is added to {hh['owner_scene']}" + (f" and gets the address {newh['addr']}" if newh else "") + f"; later steps in this plan can name it as %{name}")
        elif do == "add_wall":
            if not isinstance(step["name"], str):
                raise PlanError(f"{where}: 'name' must be text")
            for k, v in (("at", step.get("at", [0, 0])), ("size", step["size"])):
                if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
                    raise PlanError(f"{where}: '{k}' must be " + ("[x, y]" if k == "at" else "[width, height]") + ", two numbers")
            blk = head(hh, "add_wall")
            blk.update(
                target=hh["name"],
                ref=hh["addr"],
                exclude=list(EXC.get("add_wall", EXC["add_node"])),
                name=step["name"],
                parent=step.get("parent", "."),
                position=step.get("at", [0, 0]),
                size=step["size"],
            )
            blocks.append(blk)
            notes.append(f"add_wall: %{step['name']} (StaticBody2D) added to {hh['owner_scene']} at {blk['position']}, size {blk['size']}")
        elif do == "add_camera":
            if not isinstance(step["name"], str):
                raise PlanError(f"{where}: 'name' must be text")
            if "zoom" in step and not (isinstance(step["zoom"], list) and len(step["zoom"]) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in step["zoom"])):
                raise PlanError(f"{where}: 'zoom' must be [x, y], two numbers")
            if "smoothing" in step and not isinstance(step["smoothing"], bool):
                raise PlanError(f"{where}: 'smoothing' must be true or false")
            if "speed" in step and (isinstance(step["speed"], bool) or not isinstance(step["speed"], (int, float))):
                raise PlanError(f"{where}: 'speed' must be a number")
            blk = head(hh, "add_camera")
            blk.update(target=hh["name"], ref=hh["addr"], exclude=list(EXC.get("add_camera", EXC["add_node"])), name=step["name"], parent=hh["name"])
            for k, bk in (("zoom", "zoom"), ("smoothing", "smoothing"), ("speed", "smoothing_speed")):
                if k in step: blk[bk] = step[k]
            blocks.append(blk)
            notes.append(f"add_camera: %{step['name']} (Camera2D) follows {hh['name']} in {hh['owner_scene']}")
        elif do == "add_sprite":
            if not isinstance(step["name"], str):
                raise PlanError(f"{where}: 'name' must be text")
            if not isinstance(step["texture"], str):
                raise PlanError(f"{where}: 'texture' must be a res:// path")
            if "centered" in step and not isinstance(step["centered"], bool):
                raise PlanError(f"{where}: 'centered' must be true or false")
            blk = head(hh, "add_sprite")
            blk.update(target=hh["name"], ref=hh["addr"], exclude=list(EXC.get("add_sprite", EXC["add_node"])), name=step["name"], texture=step["texture"])
            if "centered" in step:
                blk["centered"] = step["centered"]
            blocks.append(blk)
            notes.append(f"add_sprite: %{step['name']} (Sprite2D, {step['texture']}) added to {hh['name']} in {hh['owner_scene']}")
        elif do == "when":
            c = ctx.clock
            fn = f"_on_{snake(src['name'])}_{step['signal']}"
            has_args = bool((src.get("signal_args") or {}).get(step["signal"]))
            hargs = []
            if has_args:
                types = c.signal_arg_types(src, step["signal"])
                if types is None:
                    raise PlanError(f"{where}: the registry has no argument classes for {src['name']}.{step['signal']}; run registry_builder.py again")
                if not c.signal_args_supported(types):
                    raise PlanError(f"{where}: {src['name']}.{step['signal']} carries ({', '.join(t or '?' for t in types)}); plans support up to 3 arguments that are Node classes or bool, int, float, String, Vector2, Vector3, Color")
                hargs = c.handler_args_for(step["signal"], types)
            only = step.get("only")
            if only is not None and only != "self":
                raise PlanError(f"{where}: 'only' must be \"self\" (the handler runs only when the argument is the receiver itself)")
            if only and (not hargs or c.arg_class(hargs[0]) not in c.NODE_ARG_CLASSES):
                raise PlanError(f"{where}: 'only' needs a signal whose first argument is a Node (body_entered, area_entered); {src['name']}.{step['signal']} has none")
            has_free = isinstance(step["then"], list) and any(isinstance(f, dict) and "free" in f for f in step["then"]) or (isinstance(step["then"], dict) and "free" in step["then"])
            body = body_from_then_any(step["then"], hh, where, force_array=bool(hargs) or has_free)
            blk = add_method_block(hh, fn, body)
            if hargs:
                blk["args"] = hargs
                if only:
                    blk["guard"] = "self"
            blocks.append(blk)
            blocks.append(connect_block(hh, step["signal"], fn, src))
            sts = c.statements_of(blk)
            emitter_freed = any(s2["kind"] == "free" and s2["target"] == src["name"] for s2 in sts)
            repeating = bool(re.search(c.REPEATING_SIGNAL_RX, step["signal"])) and src is not hh
            if repeating and not emitter_freed:
                problems = c.compounding_errors(sts)
                if problems:
                    raise PlanError(f"{where} (when): {src['name']}.{step['signal']} can fire again every time something enters or leaves, and " + "; ".join(problems) + f"; or end the list with {{\"free\": \"{src['name']}\"}} so it can only happen once")
            if ctx.vocab.get("signals", {}).get(step["signal"], {}).get("repeats", True) and not repeating:
                for prop, op, val in body_relatives(body):
                    how = next(h for v, o, h in RELATIVE_VERBS if o == op)
                    notes.append(f"{step['signal']} can fire many times; each time it fires, {prop} {how} {val}, so the change compounds")
            if hargs:
                notes.append(f"{src['name']}.{step['signal']} hands the handler its argument ({', '.join(c.arg_class(x) for x in hargs)}); the statements do not read it" + ("; the handler runs only when it is the receiver itself" if only else "; any body that enters triggers it (add \"only\": \"self\" to limit it to the receiver)"))
            if emitter_freed:
                notes.append(f"{src['name']} is freed by the handler, so this can happen only once")
            if src is not hh and ctx.vocab.get("signals", {}).get(step["signal"], {}).get("group") == "never":
                notes.append(f"{src['name']}.{step['signal']} never fires by itself: the behavior check emits it, and in a running game something has to start or emit it (an 'after' step starts a Timer)")
        elif do == "change_handler":
            fn = step["handler"]
            if fn not in hh.get("methods", []):
                close = difflib.get_close_matches(str(fn), hh.get("methods", []), n=3)
                raise PlanError(f"{where}: '{fn}' is not a function of {hh['name']}" + (f" (did you mean {', '.join(close)}?)" if close else "") + " (handlers: " + ", ".join(m for m in hh.get("methods", []) if m.startswith("_on_")) + ")")
            raw_h = open(os.path.join(ctx.project, hh["script"][len("res://"):]), encoding="utf-8").read()
            hargs, hguard = handler_signature(ctx.clock, raw_h, fn)
            if not re.match(r"^_on_[a-z0-9_]+$", fn) or (hh.get("method_args", {}).get(fn, 0) != 0 and not hargs):
                raise PlanError(f"{where}: {fn} cannot be replaced: only handlers (_on_...) whose arguments are typed 'name: Type' can")
            body = body_from_then_any(step["then"], hh, where, force_array=True)
            blk = add_method_block(hh, fn, body, mode="replace")
            if hargs:
                blk["args"] = hargs
            if hguard:
                blk["guard"] = "self"
                notes.append(f"{fn} keeps its first line 'if {hargs[0].split(':')[0]} != self: return'; to drop or add the guard, undo its plan and write the plan again")
            blocks.append(blk)
            conns = [sg for _, sg in ctx.clock.connections_into(ctx.project, hh, fn)]
            if not conns:
                notes.append(f"{fn} is not connected to any signal, so nothing calls it yet")
            for sig in conns:
                if ctx.vocab.get("signals", {}).get(sig, {}).get("repeats", True) and not re.search(ctx.clock.REPEATING_SIGNAL_RX, sig):
                    for prop, op, val in body_relatives(body):
                        how = next(h for v, o, h in RELATIVE_VERBS if o == op)
                        notes.append(f"{sig} can fire many times; each time it fires, {prop} {how} {val}, so the change compounds")
        elif do == "after":
            secs = step["seconds"]
            if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not (0.05 <= secs <= 3600):
                raise PlanError(f"{where}: 'seconds' must be a number between 0.05 and 3600")
            timers = [t for t in ctx.hosts() if t["node"] == "Timer"]
            if "timer" in step:
                tm = ctx.host_named(step["timer"])
                if tm["node"] != "Timer":
                    raise PlanError(f"{where}: {tm['name']} is a {tm['node']}, not a Timer")
            elif len(timers) == 1:
                tm = timers[0]
            else:
                raise PlanError(f"{where}: " + ("there is no Timer in the registry" if not timers else "name the timer with 'timer': " + ", ".join(t["name"] for t in timers)))
            if tm["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {tm['name']} is in {tm['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
            for prop, val in (("wait_time", float(secs)), ("one_shot", True), ("autostart", True)):
                if prop not in tm["props"]:
                    raise PlanError(f"{where}: {tm['name']} has no registered property '{prop}' (patch_after.py adds the Timer to props_allow.json; then run registry_builder.py)")
                blocks.append(modify_block(tm, prop, val))
            fn = f"_on_{snake(tm['name'])}_timeout"
            body = body_from_then_any(step["then"], hh, where, force_array=fn in hh.get("methods", []))
            blocks.append(add_method_block(hh, fn, body, mode="replace" if fn in hh.get("methods", []) else None))
            blocks.append(connect_block(hh, "timeout", fn, tm))
            cap = getattr(ctx.clock, "LIVE_CAP_SECONDS", 30.0)
            notes.append(f"after: {tm['name']} counts {secs} s from the moment the scene loads and fires once (one_shot); " + (f"the dry run and the run each wait about {secs} s for the live check" if secs <= cap else f"{secs} s is over the {cap:.0f} s live-check limit, so only the explicit-emit check runs"))
            if fn in hh.get("methods", []):
                notes.append(f"{fn} already exists on {hh['name']}: its whole body is replaced (the dry run shows what it did before)")
        elif do == "every":
            secs = step["seconds"]
            if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not (0.5 <= secs <= 3600):
                raise PlanError(f"{where}: 'seconds' must be a number between 0.5 and 3600 (a repeating handler runs this often for as long as the scene lives)")
            timers = [t for t in ctx.hosts() if t["node"] == "Timer"]
            if "timer" in step:
                tm = ctx.host_named(step["timer"])
                if tm["node"] != "Timer":
                    raise PlanError(f"{where}: {tm['name']} is a {tm['node']}, not a Timer")
            elif len(timers) == 1:
                tm = timers[0]
            else:
                raise PlanError(f"{where}: " + ("there is no Timer in the registry" if not timers else "name the timer with 'timer': " + ", ".join(t["name"] for t in timers)))
            if tm["owner_scene"] != hh["owner_scene"]:
                raise PlanError(f"{where}: {tm['name']} is in {tm['owner_scene']} and {hh['name']} in {hh['owner_scene']}; both nodes must be in the same scene")
            if not hh.get("script"):
                raise PlanError(f"{where}: {hh['name']} has no script, so it cannot receive a signal handler")
            for prop, val in (("wait_time", float(secs)), ("one_shot", False), ("autostart", True)):
                if prop not in tm["props"]:
                    raise PlanError(f"{where}: {tm['name']} has no registered property '{prop}' (patch_after.py adds the Timer to props_allow.json; then run registry_builder.py)")
            fn = f"_on_{snake(tm['name'])}_timeout"
            body = body_from_then_any(step["then"], hh, where, force_array=True)
            problems = ctx.clock.compounding_errors(body["statements"])
            if problems:
                raise PlanError(f"{where} (every): this handler would run every {secs} s for as long as the scene lives, and " + "; ".join(problems))
            exists = fn in hh.get("methods", [])
            blocks.append(add_method_block(hh, fn, body, mode="replace" if exists else None))
            blocks.append(connect_block(hh, "timeout", fn, tm))
            for prop, val in (("wait_time", float(secs)), ("one_shot", False), ("autostart", True)):
                blocks.append(modify_block(tm, prop, val))
            lims = [f"{s['property']} {s['bound']} {s['value']}" for s in body["statements"] if s["kind"] == "limit"]
            cap = getattr(ctx.clock, "LIVE_CAP_SECONDS", 30.0)
            notes.append(f"every: {tm['name']} fires every {secs} s from the moment the scene loads, for as long as the scene lives" + (f"; bounded by: {', '.join(lims)}" if lims else "") + "; "
                         + (f"the dry run and the run each watch about {min(3 * secs, cap):.1f} s (at least 3 firings) in the live check" if secs <= cap else f"{secs} s is over the {cap:.0f} s live-check limit, so only the explicit-emit check runs"))
            if exists:
                notes.append(f"{fn} already exists on {hh['name']}: its whole body is replaced (the dry run shows what it did before)")
        elif do == "add_function":
            blocks.append(add_method_block(hh, step["name"], body_from_then_any(step["then"], hh, where)))
        elif do == "set_property":
            blocks.append(modify_block(hh, step["property"], step["value"]))
        elif do == "move_with_keys":
            if hh.get("node") != "CharacterBody2D" and not hh.get("script"):
                raise PlanError(f"{where}: move_with_keys target {hh['name']} must be CharacterBody2D or have a script")
            blk = head(hh, "set_physics_body")
            blk.update(
                target=hh["name"],
                ref=hh["addr"],
                exclude=list(EXC["set_physics_body"]),
                template=step.get("template", "input_vector_8way"),
                left=step.get("left", "move_left"),
                right=step.get("right", "move_right"),
                up=step.get("up", "move_up"),
                down=step.get("down", "move_down"),
            )
            blocks.append(blk)
            notes.append(f"set_physics_body: {hh['name']} ({hh['node']}) _physics_process replaced with {blk['template']}")
        else:
            blocks.append(connect_block(hh, step["signal"], step["handler"], src))
    return host, blocks, notes


def peek_ids(ws, n):
    p = os.path.join(ws, "frame_counter.json")
    nxt = load_json(p)["next"] if os.path.exists(p) else 0x6000
    return [f"0x{nxt + i:04X}" for i in range(n)], nxt + n


def commit_ids(ws, nxt):
    json.dump({"next": nxt}, open(os.path.join(ws, "frame_counter.json"), "w"))


def describe(ctx, b):
    if b.get("op") == "set_physics_body":
        return f"SET PHYSICS {b['target']}._physics_process ({b.get('template', 'input_vector_8way')})"
    c = ctx.clock
    try:
        if b["op"] == "add_method":
            return ("REPLACE " if b.get("mode") == "replace" else "") + f"{b['fn']}({', '.join(c.arg_class(x) for x in b.get('args') or [])}): {c.body_line(b)}"
        if b["op"] == "add_node":
            return f"ADD %{b['name']} ({b['class']})" + (f" at ({b['position'][0]:g}, {b['position'][1]:g})" if "position" in b else "") + (f", shape {b['size'][0]:g} x {b['size'][1]:g}" if "size" in b else "") + (f" under {b['parent']}" if b["parent"] != "." else "")
        if b["op"] == "add_wall":
            return f"ADD WALL %{b['name']} ({b['size'][0]:g}x{b['size'][1]:g}) at ({b['position'][0]:g}, {b['position'][1]:g})"
        if b["op"] == "add_camera":
            z = b.get("zoom", [1, 1])
            return f"ADD CAMERA %{b['name']} on {b['parent']} (zoom {z[0]:g}x{z[1]:g}, " + (f"smoothing {b.get('smoothing_speed', 5.0):g})" if b.get("smoothing", True) else "no smoothing)")
        if b["op"] == "add_sprite":
            return f"ADD SPRITE %{b['name']} on {b['target']} ({b['texture']}, " + ("centered)" if b.get("centered", True) else "top-left origin)")
        if b["op"] == "connect_signal":
            if b["source"] != b["target"]:
                return f"{b['source']}.{b['signal']} -> {b['target']}.{b['method']}"
            return f"{b['signal']} -> {b['method']}"
        return f"{b['target']}.{b['property']} = {c.scene_literal(b['type'], b['value'])}"
    except (ValueError, TypeError, KeyError):
        return f"{b.get('fn') or b.get('property') or b.get('signal')}: {b.get('value')!r}   (malformed)"


def load_plan(ctx, path):
    if not os.path.exists(path):
        sys.exit(f"STOPPED: {path} not found")
    try:
        plan = load_json(path)
        host, blocks, notes = expand_plan(ctx, plan)
    except (PlanError, ValueError) as e:
        sys.exit(f"PLAN REFUSED: {e}")
    ids, nxt = peek_ids(ctx.ws, len(blocks))
    for b, i in zip(blocks, ids):
        b["frame_id"] = i
    return plan, host, blocks, nxt, notes


# ------------------------------------------------------------------ dry run: the real pipeline on a throwaway copy
def canonical_reply(c, b, project, host):
    if b["op"] == "add_method":
        raw = open(c.script_path(project, host), encoding="utf-8").read()
        stmt = c.signature(b) + "\n" + c.detect_indent(raw) + c.body_line(b)
    elif b["op"] == "connect_signal":
        stmt = c.cross_stmt(b) if b["source"] != b["target"] else f"{b['signal']}.connect({b['method']})"
    else:
        stmt = f"{b['property']} = {c.scene_literal(b['type'], b['value'])}"
    return json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": stmt})


def canonical_replies(c, b, project, host):
    if b.get("op") == "set_physics_body":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
    """One canonical answer per mule call: a body frame asks the mule for one line per statement."""
    if b["op"] == "add_sprite":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
    if b["op"] == "add_camera":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
    if b["op"] == "add_wall":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]
    if b["op"] == "add_node":
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": f"{prop} = {c.scene_literal('Vector2', val)}"}) for prop, val in c.node_values(b)]
    if b["op"] == "add_method" and c.is_body_frame(b):
        raw = open(c.script_path(project, host), encoding="utf-8").read()
        indent, lines, sig = c.detect_indent(raw), c.body_lines(b), c.signature(b)
        g = len(c.guard_prefix(b)) if hasattr(c, "guard_prefix") else 0
        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": sig + "\n" + "\n".join(indent + l for l in lines[:k + 1])}) for k in range(g, len(lines))]
    return [canonical_reply(c, b, project, host)]


def row_result(row):
    v = row.get("verify") or {}
    note = ""
    if row["status"].startswith("REJECT"):
        note = "; ".join(row.get("problems") or [])
    elif row["status"] == "DLQ":
        note = "; ".join(f"try {a['try']}: {a.get('error', a['status'])}" for a in row.get("attempts", []))
    elif row["status"] == "ROLLED_BACK_VERIFY":
        note = v.get("summary", "")
    elif row["status"] == "ROLLED_BACK":
        note = (row.get("apply") or {}).get("problem", "")
    return {"status": row["status"], "verify": v.get("verdict"), "how": ("explicit emit" if v.get("explicit") else "natural") if v.get("mode") == "signal" else None, "note": note}


def dry_run(ctx, blocks):
    c, a = ctx.clock, ctx.a
    tmp = tempfile.mkdtemp(prefix="cascade_dry_")
    t = os.path.join(tmp, "ws")
    shutil.copytree(ctx.ws, t, ignore=shutil.ignore_patterns("sweep", "dead_letter", "plans", "*.bak", "__pycache__", "ledger.jsonl", "*.before-*"))
    results, real_log, real_argv = [], c.log, sys.argv
    c.log = lambda m: None
    try:
        for i, b in enumerate(blocks):
            reg = load_json(os.path.join(t, "state_registry.json"))
            host = next(h for h in reg["hosts"].values() if h["name"] == b["target"])
            bp, sp = os.path.join(t, f"dry_{i}.json"), os.path.join(t, f"dry_{i}_reply.json")
            json.dump(b, open(bp, "w"))
            try:
                replies = canonical_replies(c, b, os.path.join(t, "engine_test"), host)
            except (ValueError, TypeError, KeyError):
                replies = [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "x"})]     # the block is malformed: GI-1 refuses it with its own message before any answer is read
            json.dump(replies, open(sp, "w"))
            sys.argv = ["cascade_clock.py", bp, "--ws", t, "--script", sp, "--godot", a.godot, "--mule-dir", a.mule_dir]
            try:
                c.main()
            except SystemExit as e:
                if isinstance(e.code, str):
                    results.append({"status": "STOPPED", "verify": None, "how": None, "note": e.code}); break
            row = [json.loads(l) for l in open(os.path.join(t, "ledger.jsonl"), encoding="utf-8")][-1]
            results.append(row_result(row))
            if row["status"] not in OK_STATUS:
                break
    finally:
        c.log, sys.argv = real_log, real_argv
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def verdict_of(blocks, results):
    if len(results) < len(blocks) or any(r["status"] not in OK_STATUS for r in results):
        return "REFUSED"
    if all(r["status"] in NOOP_STATUS for r in results):
        return "NOOP"
    return "READY"


def print_dry(ctx, plan, blocks, results, seconds, notes=()):
    print(f"dry run of: {plan['intent']}")
    print(f"  (on a temporary copy of the workspace; no mule, nothing real was touched; {seconds:.1f}s)")
    for assume in plan.get("assumptions", []):
        print(f"  assumes: {assume}")
    for note in notes:
        print(f"  NOTE: {note}")
    print(f"  {'#':<3}{'op':<16}{'what':<58}{'status':<24}verify")
    for i, b in enumerate(blocks):
        r = results[i] if i < len(results) else None
        if r is None:
            print(f"  {i + 1:<3}{b['op']:<16}{describe(ctx, b)[:56]:<58}{'(not reached)':<24}")
            continue
        print(f"  {i + 1:<3}{b['op']:<16}{describe(ctx, b)[:56]:<58}{r['status']:<24}{(r['verify'] or '-') + ('  (' + r['how'] + ')' if r['how'] else '')}")
        if len(describe(ctx, b)) > 56:
            print(f"      full: {describe(ctx, b)}")
        if b.get("mode") == "replace":
            host = ctx.host_named(b["target"])
            old = handler_effect(ctx.clock, host, open(ctx.clock.script_path(ctx.project, host), encoding="utf-8").read(), b["fn"])
            print(f"      replaces: {old}" + ("   <- HAND-WRITTEN, IT WILL BE DISCARDED" if old.startswith("hand-written") else ""))
        if r["note"]:
            print(f"      {r['note']}")
    v = verdict_of(blocks, results)
    print(f"verdict: {v}" + ("   (every block is already in place; nothing to do)" if v == "NOOP" else ""))
    return v


# ------------------------------------------------------------------ catalog
def script_defaults(project, host):
    out = {}
    fp = os.path.join(project, host["script"][len("res://"):]) if host.get("script") else None
    if fp and os.path.exists(fp):
        for l in open(fp, encoding="utf-8").read().splitlines():
            m = re.match(r"^@export\s+var\s+(\w+)\s*(?::\s*(\w+))?\s*(?:=\s*(.+))?$", l)
            if m and m.group(3):
                out[m.group(1)] = m.group(3).strip()
    return out


def scene_values(ctx, host):
    out, fp = {}, os.path.join(ctx.project, host["owner_scene"][len("res://"):])
    if not os.path.exists(fp):
        return out
    lines = open(fp, encoding="utf-8").read().splitlines()
    rel = ctx.clock.node_rel(host)
    try:
        start, end = ctx.clock.find_node_block(lines, rel.split("/")[-1], "." if "/" not in rel else rel.rsplit("/", 1)[0])
    except Exception:
        return out
    for l in lines[start + 1:end]:
        m = re.match(r"^(\w+)\s*=\s*(.+)$", l)
        if m and m.group(1) in host["props"]:
            out[m.group(1)] = m.group(2).strip()
    return out


def handler_signature(c, raw, fn):
    """(typed argument strings, has a guard line) of a handler already in a script."""
    have = c.existing_function(raw, fn)
    m = re.match(r"^func\s+\w+\s*\((.*)\)\s*->\s*void\s*:$", have[0]) if have else None
    hargs = [x.strip() for x in m.group(1).split(",")] if m and m.group(1).strip() else []
    guard = bool(hargs) and hasattr(c, "handler_guard") and c.handler_guard(raw, fn) is not None
    return hargs, guard


def statement_text(c, st):
    if st["kind"] == "pass":
        return "does nothing"
    if st["kind"] == "print":
        return f'prints "{st["message"]}"'
    if st["kind"] == "assign":
        return f"sets {st['property']} = {c.scene_literal(st['type'], st['value'])}"
    if st["kind"] == "limit":
        return f"limits {st['property']}: {c.statement_line(st)}"
    if st["kind"] == "free":
        return f"frees {'itself' if st['target'] == 'self' else st['target']}"
    return f"changes {st['property']}: {c.statement_line(st)}"


def handler_effect(c, host, raw, name):
    have = c.existing_function(raw, name)
    if not have:
        return "unknown"
    if hasattr(c, "parse_statement_line"):
        body = [l for l in have[1:] if l and not l.startswith("#")]
        only = bool(body) and hasattr(c, "GUARD_RX") and bool(c.GUARD_RX.match(body[0]))
        if only: body = body[1:]
        sts = [c.parse_statement_line(l, host) for l in body]
        if sts and all(sts):
            return ("only when the argument is itself: " if only else "") + "; ".join(statement_text(c, st) for st in sts)
        return f"hand-written ({len(body)} line(s))"
    if len(have) == 2:
        b = have[1]
        if b == "pass":
            return "does nothing"
        m = re.match(r'^print\((["\'])(.*)\1\)$', b)
        if m:
            return f'prints "{m.group(2)}"'
        m = re.match(r"^(?:self\.)?([A-Za-z_]\w*)\s*(\+=|-=|\*=)\s*(.+)$", b) or re.match(r"^(?:self\.)?([A-Za-z_]\w*)\s*=\s*(?:self\.)?\1\s*([+\-*])\s*(.+)$", b)
        if m and m.group(1) in host["props"] and host["props"][m.group(1)] in RELATIVE_TYPES:
            return f"changes {m.group(1)}: {m.group(1)} {m.group(2) if m.group(2).endswith('=') else m.group(2) + '='} {m.group(3).strip()}"
        m = re.match(r"^(?:self\.)?([A-Za-z_]\w*)\s*=\s*(.+)$", b)
        if m and m.group(1) in host["props"] and c.parse_literal(host["props"][m.group(1)], m.group(2)) is not None:
            return f"sets {m.group(1)} = {m.group(2).strip()}"
    return f"hand-written ({len(have) - 1} line(s))"


def cmd_catalog(ctx):
    c, v, L = ctx.clock, ctx.vocab, []
    L.append(f"CASCADE CATALOG   workspace {ctx.ws}   (paste this into the harness session)")
    for host in ctx.hosts():
        L.append("")
        L.append(f"NODE {host['name']}   {host['node']}   script {host.get('script') or '(none)'}   scene {host['owner_scene']}   address {host['addr']}")
        defaults, scene = script_defaults(ctx.project, host), scene_values(ctx, host)
        L.append("  properties you may set (type; script default; value stored in the scene):")
        for p, t in sorted(host["props"].items()):
            L.append(f"    {p}: {t};  default {defaults.get(p, '-')};  scene {scene.get(p, '(default)')}")
        sa = host.get("signal_args", {})
        usable = sorted(s for s in host.get("signals", []) if not sa.get(s))
        groups = {"enter": [], "leave": [], "never": [], "other": []}
        for s in usable:
            groups[v["signals"].get(s, {}).get("group", "other")].append(s)
        L.append("  signals you may use (they carry no arguments):")
        titles = {"enter": "fires by itself when the node enters the tree", "leave": "fires by itself when the node leaves the tree",
                  "never": "never fires by itself (the behavior check emits it)", "other": "not in the vocabulary yet (the check emits it if it never fires by itself)"}
        for g in ("enter", "leave", "never", "other"):
            if groups[g]:
                L.append(f"    {titles[g]}:")
                for s in groups[g]:
                    ph = v["signals"].get(s, {}).get("phrases", [])
                    quoted = '", "'.join(ph[:4])
                    rep_note = "   [can fire many times]" if v["signals"].get(s, {}).get("repeats", True) else ""
                    L.append(f"      {s}" + (f'   ("{quoted}")' if ph else "") + rep_note)
        withargs = sorted(s for s in host.get("signals", []) if sa.get(s))
        good = [s for s in withargs if hasattr(c, "signal_arg_types") and c.signal_args_supported(c.signal_arg_types(host, s))]
        if good:
            L.append("  signals that carry arguments (the handler receives them, the statements do not read them; \"only\": \"self\" limits a Node argument to the receiver):")
            for s2 in good:
                ph = v["signals"].get(s2, {}).get("phrases", [])
                quoted = '", "'.join(ph[:4])
                L.append(f"      {s2}({', '.join(c.signal_arg_types(host, s2))})" + (f'   ("{quoted}")' if ph else "") + ("   [can fire many times]" if v["signals"].get(s2, {}).get("repeats", True) else ""))
        out = [s2 for s2 in withargs if s2 not in good]
        if out:
            L.append("  signals out of reach (arguments plans cannot carry): " + ", ".join(f"{s2}({', '.join(c.signal_arg_types(host, s2) or sa[s2]) if hasattr(c, 'signal_arg_types') else ', '.join(sa[s2])})" for s2 in out))
        raw = open(os.path.join(ctx.project, host["script"][len("res://"):]), encoding="utf-8").read() if host.get("script") else ""
        conns = {}
        sp = os.path.join(ctx.project, host["owner_scene"][len("res://"):])
        rel = c.node_rel(host)
        for l in open(sp, encoding="utf-8").read().splitlines() if os.path.exists(sp) else []:
            if l.startswith("[connection "):
                at = dict(re.findall(r'(\w+)="([^"]*)"', l))
                if at.get("to") == rel:
                    conns.setdefault(at.get("method"), []).append(at.get("signal") if at.get("from") == rel else f"{at.get('from')}.{at.get('signal')}")
        if host.get("script"):
            L.append("  handlers already in the script (change_handler replaces the whole body of a [replaceable] one):")
        else:
            L.append("  no script: it can emit signals for another node's handlers, but it has no handlers of its own")
        for m in host.get("methods", []):
            ha = handler_signature(c, raw, m)[0] if re.match(r"^_on_[a-z0-9_]+$", m) else []
            flag = "[replaceable]" if re.match(r"^_on_[a-z0-9_]+$", m) and (host["method_args"].get(m, 0) == 0 or (ha and len(ha) == host["method_args"].get(m, 0) and all(re.match(r"^[a-z_][a-z0-9_]*: \w+$", x) for x in ha))) else "[not replaceable: engine callback or untyped arguments]"
            L.append(f"    {m}({host['method_args'].get(m, 0)} args)   {handler_effect(c, host, raw, m)}   connected to: {', '.join(conns[m]) if m in conns else '-'}   {flag}")
    if hasattr(c, "scene_geometry"):
        c.set_godot(ctx.a.godot)
        L.append("")
        L.append("NODES YOU CAN ADD ({\"do\": \"add_node\", \"class\": ..., \"name\": ..., \"at\": [x, y], \"size\": [w, h], \"parent\": \".\" or \"%Node\"}):")
        L.append("  classes: Area2D (a trigger or pickup; \"size\" is the width and height of its rectangle, 8 to 1024), Marker2D and Node2D (a position), Timer (no position, no size)")
        L.append("  rules: a name of 2 to 24 letters or digits starting with a capital letter that no node in the scene uses; \"at\" within 4096 of the parent; at most 8 per plan and 24 registered nodes per scene; a new Area2D may not overlap any collision shape that is in the scene at load")
        L.append("  ALSO {\"do\": \"add_sprite\", \"name\": ..., \"texture\": \"res://sprites/x.png\", \"centered\": true}: a Sprite2D child of the plan's node (a CharacterBody2D, Area2D, StaticBody2D, Node2D or Marker2D) showing that texture; a .png that is not in the project yet is created as a 32x32 placeholder, .svg/.jpg/.webp must already be there; one sprite per node; \"centered\" is optional")
        L.append("  ALSO {\"do\": \"add_wall\", \"name\": ..., \"at\": [x, y], \"size\": [w, h], \"parent\": \".\" or \"%Node\"}: a solid StaticBody2D wall that stops the mover; \"size\" is the full width and height (8 to 1024); it may not overlap any collision shape; to be proven it must be the first thing the mover meets moving right along its own row")
        L.append("  ALSO {\"do\": \"add_camera\", \"name\": ..., \"zoom\": [x, y], \"smoothing\": true, \"speed\": 5.0}: a Camera2D that follows the player (it becomes a child of the plan's node, which must be the player); zoom 0.1 to 8, speed 0.5 to 30; one camera per scene; every key but \"name\" is optional")
        L.append("  ALSO {\"do\": \"add_wall\", \"name\": ..., \"at\": [x, y], \"size\": [w, h], \"parent\": \".\" or \"%Node\"}: a solid StaticBody2D wall that stops the mover; \"size\" is the full width and height (8 to 1024); it may not overlap any collision shape; to be proven it must be the first thing the mover meets moving right along its own row")
        L.append("  ALSO {\"do\": \"add_camera\", \"name\": ..., \"zoom\": [x, y], \"smoothing\": true, \"speed\": 5.0}: a Camera2D that follows the player (it becomes a child of the plan's node, which must be the player); zoom 0.1 to 8, speed 0.5 to 30; one camera per scene; every key but \"name\" is optional")
        for sc in sorted({h["owner_scene"] for h in ctx.hosts()}):
            inscene = [h["name"] for h in ctx.hosts() if h["owner_scene"] == sc]
            L.append(f"  {sc}: {len(inscene)} of {getattr(c, 'MAX_NODES_PER_SCENE', 24)} registered nodes ({', '.join(inscene)})")
            geo = c.scene_geometry(ctx.project, sc)
            if geo:
                occ = [f"{nm} [{d['rect'][0]:g}, {d['rect'][1]:g} to {d['rect'][2]:g}, {d['rect'][3]:g}]" for nm, d in sorted(geo["nodes"].items()) if d.get("rect")]
                L.append("    collision shapes already in the scene (a new Area2D must stay clear of these): " + (", ".join(occ) if occ else "none"))
    pairs = [(s, t) for t in ctx.hosts() if t.get("script") for s in ctx.hosts() if s["name"] != t["name"] and s["owner_scene"] == t["owner_scene"]]
    if pairs:
        L.append("")
        L.append("CROSS-NODE WIRING (a signal of one node runs a handler on another; {\"do\": \"when\", \"from\": <emitter>, ...} in a plan whose node is the receiver):")
        for s, t in pairs:
            sa = s.get("signal_args", {})
            known = [x for x in sorted(s.get("signals", [])) if not sa.get(x) and x in v["signals"]]
            L.append(f"  from {s['name']} ({s['node']}) into {t['name']}: handler is named _on_{c.snake(s['name'])}_<signal>")
            for x in known:
                ph = v["signals"][x].get("phrases", [])
                quoted = '", "'.join(ph[:4])
                L.append(f"      {x}" + (f'   ("{quoted}")' if ph else "") + ("   [can fire many times]" if v["signals"][x].get("repeats", True) else ""))
            if hasattr(c, "signal_arg_types"):
                for x in sorted(s.get("signals", [])):
                    ty = c.signal_arg_types(s, x)
                    if sa.get(x) and ty is not None and c.signal_args_supported(ty) and x in v["signals"]:
                        ph = v["signals"][x].get("phrases", [])
                        quoted = '", "'.join(ph[:4])
                        L.append(f"      {x}({', '.join(ty)})" + (f'   ("{quoted}")' if ph else "") + "   [carries an argument; can fire many times]")
    L.append("")
    L.append("STEPS (a plan is {\"intent\": ..., \"node\": ..., \"assumptions\": [...], \"steps\": [...]}):")
    L += ["  " + s for s in v.get("steps", [])]
    L.append("VALUES: " + v.get("values", ""))
    L.append("SUPPORTED: " + " | ".join(v.get("supported_intents", [])))
    L.append("REFUSED (say so in plain words, offer the nearest supported plan):")
    L += ["  - " + s for s in v.get("refusals", [])]
    print("\n".join(L))
    return 0


# ------------------------------------------------------------------ run, undo, status
def plans_dir(ctx):
    return os.path.join(ctx.ws, "plans")


def next_plan_id(ctx):
    os.makedirs(plans_dir(ctx), exist_ok=True)
    nums = [int(d[1:]) for d in os.listdir(plans_dir(ctx)) if re.match(r"^p\d{4}$", d)]
    return f"p{(max(nums) + 1) if nums else 1:04d}"


def touched_files(ctx, blocks):
    byref = {h["addr"]: h for h in ctx.hosts()}
    out = set()
    for b in blocks:
        if b.get("op") == "set_physics_body":
            out.add(byref[b["ref"]]["script"])
            out.add("res://project.godot")
        elif b.get("op") == "add_method":
            out.add(byref[b["ref"]]["script"])
        else:
            out.add(byref[b["ref"]]["owner_scene"])
    return sorted(out)

def res_path(ctx, f):
    return os.path.join(ctx.project, f[len("res://"):])


def cmd_dry_run(ctx, path):
    plan, host, blocks, _, notes = load_plan(ctx, path)
    t0 = time.time()
    results = dry_run(ctx, blocks)
    v = print_dry(ctx, plan, blocks, results, time.time() - t0, notes)
    return 0 if v in ("READY", "NOOP") else 2


def cmd_run(ctx, path):
    a = ctx.a
    plan, host, blocks, nxt, notes = load_plan(ctx, path)
    t0 = time.time()
    results = dry_run(ctx, blocks)
    v = print_dry(ctx, plan, blocks, results, time.time() - t0, notes)
    if v == "NOOP":
        return 0
    if v != "READY":
        print("nothing was run.")
        return 2
    commit_ids(ctx.ws, nxt)
    pid = next_plan_id(ctx)
    pdir = os.path.join(plans_dir(ctx), pid)
    os.makedirs(pdir)
    paths = []
    for i, b in enumerate(blocks, 1):
        p = os.path.join(pdir, f"block_{i:02d}.json")
        json.dump(b, open(p, "w"))
        paths.append(p)
    json.dump({"plan_id": pid, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "plan": plan, "blocks": blocks}, open(os.path.join(pdir, "plan.json"), "w"), indent=2)
    files = touched_files(ctx, blocks)
    pre = {f: md5(res_path(ctx, f)) for f in files}
    print(f"\nrunning plan {pid} for real ({len(blocks)} block(s), one model load)\n")
    sys.argv = [sys.argv[0]] + paths + ["--ws", ctx.ws, "--godot", a.godot, "--mule-dir", a.mule_dir, "--model", a.model, "--run-id", pid] + \
        (["--no-verify"] if a.no_verify else []) + (["--keep-going"] if a.keep_going else []) + (["--no-record"] if a.no_record else [])
    try:
        ctx.clock.main()
        code = 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    ledger = os.path.join(ctx.ws, "ledger.jsonl")
    rows = [json.loads(l) for l in open(ledger, encoding="utf-8") if f'"run_id": "{pid}"' in l] if os.path.exists(ledger) else []
    brow = []
    for r in rows:
        ap = r.get("apply") or {}
        brow.append({"frame_id": r["frame_id"], "op": r["op"], "status": r["status"], "tries": r.get("tries"), "file": ap.get("file"), "backup": ap.get("backup")})
        if r.get("op") == "set_physics_body" and ap.get("input", {}).get("backup"):
            brow.append({
                "frame_id": r["frame_id"],
                "op": "ensure_input",
                "status": r["status"],
                "file": "res://project.godot",
                "backup": ap["input"]["backup"],
            })
    statuses = [r["status"] for r in rows]
    state = "applied" if rows and len(rows) == len(blocks) and all(s in OK_STATUS for s in statuses) else ("partial" if "APPLIED" in statuses else "failed")
    res = {"plan_id": pid, "intent": plan["intent"], "state": state, "undone": False, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "files": {f: {"pre": pre[f], "post": md5(res_path(ctx, f))} for f in files}, "blocks": brow}
    json.dump(res, open(os.path.join(pdir, "result.json"), "w"), indent=2)
    print(f"\nplan {pid}: {state}   ({sum(1 for s in statuses if s == 'APPLIED')} applied, {sum(1 for s in statuses if s in NOOP_STATUS)} already there)")
    if any(s == "APPLIED" for s in statuses):
        print(f"take it back with:  python3 cascade_plan.py undo {pid}")
    return code


def load_result(ctx, pid):
    p = os.path.join(plans_dir(ctx), pid, "result.json")
    if not os.path.exists(p):
        sys.exit(f"STOPPED: no saved result for plan {pid} (see: python3 cascade_plan.py status)")
    return p, load_json(p)


def rebuild_registry(ctx):
    r = subprocess.run([sys.executable, os.path.join(ctx.ws, "registry_builder.py"), "--ws", ctx.ws, "--godot", ctx.a.godot], capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def cmd_undo(ctx, pid):
    rp, res = load_result(ctx, pid)
    if res.get("undone"):
        print(f"plan {pid} was already undone ({res.get('undone_at')}).")
        return 0
    changed = {f: d for f, d in res["files"].items() if md5(res_path(ctx, f)) != d["post"]}
    later = []
    for other in sorted(os.listdir(plans_dir(ctx))):
        if other > pid and os.path.exists(os.path.join(plans_dir(ctx), other, "result.json")):
            o = load_json(os.path.join(plans_dir(ctx), other, "result.json"))
            if not o.get("undone") and set(o["files"]) & set(changed):
                later.append(other)
    if changed and not ctx.a.force:
        print(f"REFUSED: {', '.join(changed)} changed after plan {pid}.")
        print(("undo the later plan(s) first, newest first: " + ", ".join(reversed(later))) if later else "something outside the plans changed it; look at it, or use --force to restore anyway (that discards the change).")
        return 2
    todo = [b for b in res["blocks"] if b["status"] == "APPLIED"]
    missing = [b["backup"] for b in todo if not b.get("backup") or not os.path.exists(b["backup"])]
    if missing:
        print("REFUSED: a backup is missing, so nothing was changed: " + ", ".join(map(str, missing)))
        return 2
    for b in reversed(todo):
        shutil.copy2(b["backup"], res_path(ctx, b["file"]))
        print(f"  restored {b['file']} from {os.path.basename(b['backup'])}")
    same = all(md5(res_path(ctx, f)) == d["pre"] for f, d in res["files"].items())
    print("the project is byte-for-byte what it was before the plan." if same else "restored, but the files differ from the pre-plan state (they changed in between).")
    print("registry rebuilt." if rebuild_registry(ctx) else "WARNING: the registry could not be rebuilt; run registry_builder.py.")
    res["undone"], res["undone_at"] = True, time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(res, open(rp, "w"), indent=2)
    if later:
        for other in later:        # their work was discarded along with this restore
            op = os.path.join(plans_dir(ctx), other, "result.json")
            o = load_json(op)
            o["undone"], o["undone_at"], o["discarded_by"] = True, res["undone_at"], pid
            json.dump(o, open(op, "w"), indent=2)
        print(f"plans discarded along with it (marked undone): {', '.join(later)}")
    return 0


def cmd_status(ctx):
    d = plans_dir(ctx)
    ids = sorted(x for x in os.listdir(d) if re.match(r"^p\d{4}$", x)) if os.path.exists(d) else []
    if not ids:
        print("no plans yet.")
        return 0
    print(f"{'plan':<7}{'state':<10}{'undone':<8}{'blocks':<8}intent")
    for pid in ids:
        rp = os.path.join(d, pid, "result.json")
        if not os.path.exists(rp):
            print(f"{pid:<7}{'(no result)':<10}")
            continue
        r = load_json(rp)
        print(f"{pid:<7}{r['state']:<10}{'yes' if r.get('undone') else 'no':<8}{len(r['blocks']):<8}{r['intent'][:70]}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Project Cascade harness layer")
    ap.add_argument("command", choices=["catalog", "expand", "dry-run", "run", "undo", "status"])
    ap.add_argument("target", nargs="?", help="a plan file (expand, dry-run, run) or a plan id (undo)")
    ap.add_argument("--ws", default=os.path.expanduser("~/cascade_ws"))
    ap.add_argument("--mule-dir", default=os.path.expanduser("~/mule_bench"))
    ap.add_argument("--model", default="gemma26b")
    ap.add_argument("--godot", default=os.environ.get("GODOT", "godot"))
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--keep-going", action="store_true")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--force", action="store_true", help="undo: restore even if the files changed after the plan")
    a = ap.parse_args()
    if a.command in ("expand", "dry-run", "run", "undo") and not a.target:
        sys.exit(f"STOPPED: {a.command} needs a {'plan id' if a.command == 'undo' else 'plan file'}")
    ctx = Ctx(a)
    if a.command == "catalog":
        sys.exit(cmd_catalog(ctx))
    if a.command == "status":
        sys.exit(cmd_status(ctx))
    if a.command == "undo":
        sys.exit(cmd_undo(ctx, a.target))
    if a.command == "expand":
        plan, host, blocks, _, notes = load_plan(ctx, a.target)
        print(json.dumps(blocks, indent=1))
        for note in notes:
            print("NOTE: " + note)
        sys.exit(0)
    sys.exit((cmd_dry_run if a.command == "dry-run" else cmd_run)(ctx, a.target))


if __name__ == "__main__":
    main()
