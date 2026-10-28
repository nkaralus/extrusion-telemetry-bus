#!/usr/bin/env python3
"""
Idempotent patcher for integrating Step 19 (set_physics_body / move_with_keys)
into cascade_plan.py and generating plan_move.json.
"""

import ast
import json
import os
import re
import shutil
import sys
from pathlib import Path

WS_DIR = Path.home() / "cascade_ws"
TARGET_FILE = WS_DIR / "cascade_plan.py"
BACKUP_FILE = WS_DIR / "cascade_plan.py.step19.bak"
PLAN_MOVE_FILE = WS_DIR / "plan_move.json"


def find_dict_block(src: str, dict_name: str):
    m = re.search(rf"^{re.escape(dict_name)}\s*=\s*\{{", src, re.MULTILINE)
    if not m:
        raise RuntimeError(f"Could not locate dictionary definition: {dict_name}")
    start = m.start()
    brace_start = m.end() - 1
    depth = 0
    in_str = None
    escape = False
    for i in range(brace_start, len(src)):
        ch = src[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, brace_start, i + 1
    raise RuntimeError(f"Unclosed brace for dictionary: {dict_name}")


def inject_into_dict(src: str, dict_name: str, key: str, val_expr: str) -> tuple[str, bool]:
    start, brace_start, end = find_dict_block(src, dict_name)
    block = src[start:end]
    pattern = r'["\']' + re.escape(key) + r'["\']\s*:'
    if re.search(pattern, block):
        return src, False

    inner = src[brace_start + 1 : end - 1].rstrip()
    if inner and not inner.endswith(","):
        inner += ","
    new_entry = f'\n    "{key}": {val_expr},\n'
    new_src = src[: brace_start + 1] + inner + new_entry + src[end - 1 :]
    return new_src, True


def patch_expand_plan(src: str) -> tuple[str, bool]:
    if 'do == "move_with_keys"' in src or "do == 'move_with_keys'" in src:
        return src, False

    fn_match = re.search(r"^def expand_plan\b.*?\n(?=def |\Z)", src, re.MULTILINE | re.DOTALL)
    if not fn_match:
        raise RuntimeError("Could not locate def expand_plan")

    fn_body = fn_match.group(0)
    elif_matches = list(re.finditer(r"^([ \t]+)elif\s+do\s*==\s*['\"][^'\"]+['\"]\s*:", fn_body, re.MULTILINE))
    if not elif_matches:
        elif_matches = list(re.finditer(r"^([ \t]+)if\s+do\s*==\s*['\"][^'\"]+['\"]\s*:", fn_body, re.MULTILINE))
    indent = elif_matches[0].group(1)
    b_indent = indent + "    "

    branch_code = (
        f'{indent}elif do == "move_with_keys":\n'
        f'{b_indent}if hh.get("node") != "CharacterBody2D" and not hh.get("script"):\n'
        f'{b_indent}    raise PlanError(f"{{where}}: move_with_keys target {{hh[\'name\']}} must be CharacterBody2D or have a script")\n'
        f'{b_indent}blk = head(hh, "set_physics_body")\n'
        f'{b_indent}blk.update(\n'
        f'{b_indent}    target=hh["name"],\n'
        f'{b_indent}    ref=hh["addr"],\n'
        f'{b_indent}    exclude=list(EXC["set_physics_body"]),\n'
        f'{b_indent}    template=step.get("template", "input_vector_8way"),\n'
        f'{b_indent}    left=step.get("left", "move_left"),\n'
        f'{b_indent}    right=step.get("right", "move_right"),\n'
        f'{b_indent}    up=step.get("up", "move_up"),\n'
        f'{b_indent}    down=step.get("down", "move_down"),\n'
        f'{b_indent})\n'
        f'{b_indent}blocks.append(blk)\n'
        f'{b_indent}notes.append(f"set_physics_body: {{hh[\'name\']}} ({{hh[\'node\']}}) _physics_process replaced with {{blk[\'template\']}}")\n'
    )

    else_match = re.search(rf"^{indent}else\s*:", fn_body, re.MULTILINE)
    if not else_match:
        raise RuntimeError("Could not find else: branch in expand_plan")
    insert_pos = fn_match.start() + else_match.start()
    return src[:insert_pos] + branch_code + src[insert_pos:], True


def patch_describe(src: str) -> tuple[str, bool]:
    fn_match = re.search(r"^def describe\b.*?\n(?=def |\Z)", src, re.MULTILINE | re.DOTALL)
    if not fn_match:
        raise RuntimeError("Could not locate def describe")
    fn_body = fn_match.group(0)
    if '"set_physics_body"' in fn_body or "'set_physics_body'" in fn_body:
        return src, False

    hdr_match = re.search(r"^def describe\([^)]*\)\s*(?:->\s*[^:]+)?:\n", fn_body)
    insert_pos = fn_match.start() + hdr_match.end()
    snippet = (
        '    if b.get("op") == "set_physics_body":\n'
        '        return f"SET PHYSICS {b[\'target\']}._physics_process ({b.get(\'template\', \'input_vector_8way\')})"\n'
    )
    return src[:insert_pos] + snippet + src[insert_pos:], True


def patch_canonical_replies(src: str) -> tuple[str, bool]:
    fn_match = re.search(r"^def canonical_replies\b.*?\n(?=def |\Z)", src, re.MULTILINE | re.DOTALL)
    if not fn_match:
        raise RuntimeError("Could not locate def canonical_replies")
    fn_body = fn_match.group(0)
    if '"set_physics_body"' in fn_body or "'set_physics_body'" in fn_body:
        return src, False

    hdr_match = re.search(r"^def canonical_replies\([^)]*\)\s*(?:->\s*[^:]+)?:\n", fn_body)
    insert_pos = fn_match.start() + hdr_match.end()
    snippet = (
        '    if b.get("op") == "set_physics_body":\n'
        '        return [json.dumps({"frame_id": b["frame_id"], "target": b["target"], "stmt": "harness_rendered"})]\n'
    )
    return src[:insert_pos] + snippet + src[insert_pos:], True


def patch_touched_files(src: str) -> tuple[str, bool]:
    if '"set_physics_body"' in src and 'res://project.godot' in src:
        return src, False
    old_fn = re.search(r"^def touched_files\b.*?\n(?=def |\Z)", src, re.MULTILINE | re.DOTALL)
    if not old_fn:
        raise RuntimeError("Could not locate def touched_files")

    new_fn = (
        'def touched_files(ctx, blocks):\n'
        '    byref = {h["addr"]: h for h in ctx.hosts()}\n'
        '    out = set()\n'
        '    for b in blocks:\n'
        '        if b.get("op") == "set_physics_body":\n'
        '            out.add(byref[b["ref"]]["script"])\n'
        '            out.add("res://project.godot")\n'
        '        elif b.get("op") == "add_method":\n'
        '            out.add(byref[b["ref"]]["script"])\n'
        '        else:\n'
        '            out.add(byref[b["ref"]]["owner_scene"])\n'
        '    return sorted(out)\n\n'
    )
    return src[:old_fn.start()] + new_fn + src[old_fn.end():], True


def patch_cmd_run(src: str) -> tuple[str, bool]:
    fn_match = re.search(r"^def cmd_run\b.*?\n(?=def |\Z)", src, re.MULTILINE | re.DOTALL)
    if not fn_match:
        raise RuntimeError("Could not locate def cmd_run")
    fn_body = fn_match.group(0)
    if '"ensure_input"' in fn_body or "'ensure_input'" in fn_body:
        return src, False

    brow_match = re.search(r"^([ \t]+)brow\.append\((?:[^()\n]|\([^()\n]*\))*\)\n", fn_body, re.MULTILINE)
    if not brow_match:
        brow_match = re.search(r"^([ \t]+)brow\.append\(\s*\{.*?\}\s*\)\n", fn_body, re.MULTILINE | re.DOTALL)
    indent = brow_match.group(1)
    snippet = (
        f'{indent}if r.get("op") == "set_physics_body" and ap.get("input", {{}}).get("backup"):\n'
        f'{indent}    brow.append({{\n'
        f'{indent}        "frame_id": r["frame_id"],\n'
        f'{indent}        "op": "ensure_input",\n'
        f'{indent}        "status": r["status"],\n'
        f'{indent}        "file": "res://project.godot",\n'
        f'{indent}        "backup": ap["input"]["backup"],\n'
        f'{indent}    }})\n'
    )
    insert_pos = fn_match.start() + brow_match.end()
    return src[:insert_pos] + snippet + src[insert_pos:], True


def generate_plan_move() -> None:
    plan_data = {
        "intent": "move the player with arrow keys or wasd",
        "node": "%Player",
        "steps": [
            {
                "do": "move_with_keys",
                "template": "input_vector_8way",
                "left": "move_left",
                "right": "move_right",
                "up": "move_up",
                "down": "move_down"
            }
        ]
    }
    PLAN_MOVE_FILE.write_text(json.dumps(plan_data, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote {PLAN_MOVE_FILE}")


def main() -> int:
    if not TARGET_FILE.exists():
        print(f"[err] target file not found: {TARGET_FILE}", file=sys.stderr)
        return 1

    if not BACKUP_FILE.exists():
        shutil.copy2(TARGET_FILE, BACKUP_FILE)
        print(f"[bak] created {BACKUP_FILE}")
    else:
        print(f"[bak] preserved existing {BACKUP_FILE}")

    src = TARGET_FILE.read_text(encoding="utf-8")
    changes = []

    src, chg = inject_into_dict(
        src,
        "EXC",
        "set_physics_body",
        '["KinematicBody2D", "untyped_variables", "move_and_slide_args", "relative_paths_up_tree", "cross_subnet_queries"]',
    )
    if chg: changes.append("EXC['set_physics_body']")

    src, chg = inject_into_dict(src, "STEP_KEYS", "move_with_keys", "set()")
    if chg: changes.append("STEP_KEYS['move_with_keys']")

    src, chg = inject_into_dict(
        src,
        "OPTIONAL_KEYS",
        "move_with_keys",
        '{"node", "template", "left", "right", "up", "down"}',
    )
    if chg: changes.append("OPTIONAL_KEYS['move_with_keys']")

    src, chg = patch_expand_plan(src)
    if chg: changes.append("expand_plan: move_with_keys")

    src, chg = patch_describe(src)
    if chg: changes.append("describe: set_physics_body")

    src, chg = patch_canonical_replies(src)
    if chg: changes.append("canonical_replies: set_physics_body")

    src, chg = patch_touched_files(src)
    if chg: changes.append("touched_files: set_physics_body")

    src, chg = patch_cmd_run(src)
    if chg: changes.append("cmd_run: ensure_input backup tracking")

    try:
        ast.parse(src, filename=str(TARGET_FILE))
    except SyntaxError as exc:
        print(f"[err] AST syntax validation failed: {exc}", file=sys.stderr)
        return 1

    if changes:
        TARGET_FILE.write_text(src, encoding="utf-8")
        print(f"[ok] patched {TARGET_FILE}: {', '.join(changes)}")
    else:
        print(f"[noop] {TARGET_FILE} already has all Step 19 patches applied")

    generate_plan_move()
    return 0


if __name__ == "__main__":
    sys.exit(main())
