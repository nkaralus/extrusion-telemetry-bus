"""SLOT_2 parsers.  A parser maps raw scope text -> JSON string for ``Slot2Plan``.

The model never touches the registry; it only fills the schema.  Anything that
fails validation is NAK'd by the bus (3 strikes -> DLQ).

* ``OllamaParser``  local 3B model (Qwen2.5-3B / Llama-3.2-3B) with JSON-schema grammar
* ``RuleParser``    deterministic regex fallback (works with zero model, zero network)
* ``ChainParser``   primary, falling back only when the primary is *unreachable*
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable, List, Optional

from .mule_frame import CutLine, Slot2Plan
from .registry import Registry

PlanParser = Callable[[str, Optional[str]], str]


class ParserUnavailable(RuntimeError):
    """Transport-level failure (model server down).  Not a NAK."""


class OllamaParser:
    def __init__(self, registry: Registry, model: str = "qwen2.5:3b",
                 host: str = "http://127.0.0.1:11434", timeout: float = 20.0) -> None:
        self.registry, self.model, self.host, self.timeout = registry, model, host.rstrip("/"), timeout

    def _system(self) -> str:
        cat = "; ".join(
            f"{p.part_number} = {p.description} (aliases: {', '.join(p.aliases) or 'none'})"
            for p in self.registry.state.profiles.values()
        )
        return (
            "You convert a fabrication job scope into JSON for the schema provided. "
            "Output JSON only. Use ONLY these profile part numbers: " + cat + ". "
            "length_in is the overall long-point length in inches (decimal). "
            "miter_deg is 0 for square, 45 for a 45 degree miter. "
            "face is spline_face or back_face (which face carries the miter long point)."
        )

    def __call__(self, scope: str, feedback: Optional[str] = None) -> str:
        prompt = scope if not feedback else f"{scope}\n\nPrevious attempt rejected: {feedback}\nFix and re-emit."
        body = json.dumps({
            "model": self.model, "system": self._system(), "prompt": prompt, "stream": False,
            "format": Slot2Plan.model_json_schema(), "options": {"temperature": 0},
        }).encode()
        req = urllib.request.Request(f"{self.host}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())["response"]
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ParserUnavailable(str(exc)) from exc


_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "eighteen": 18, "twenty": 20,
}
_LEN_RE = re.compile(
    r'(?P<qty>\d+|[a-z]+)\s*(?:x\s*)?'
    r'(?P<whole>\d+)(?:[\s-]+(?P<num>\d+)/(?P<den>\d+))?(?:\.(?P<dec>\d+))?'
    r'\s*(?:"|”|in\b|inch(?:es)?\b)', re.I)
_DEG_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:°|deg)', re.I)


class RuleParser:
    """Deterministic scope parser.  One cut line per clause (split on ; newline 'and then')."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def _profile_for(self, clause: str, last: Optional[str]) -> str:
        low = clause.lower()
        for p in self.registry.state.profiles.values():
            tokens = [p.part_number.lower(), *[a.lower() for a in p.aliases]]
            if any(t in low for t in tokens):
                return p.part_number
        if last:
            return last
        raise ValueError(f"no known profile named in clause: {clause!r}")

    def __call__(self, scope: str, feedback: Optional[str] = None) -> str:
        cuts: List[CutLine] = []
        last: Optional[str] = None
        for clause in re.split(r"[;\n]|\band then\b", scope):
            m = _LEN_RE.search(clause)
            if not m:
                continue
            q = m["qty"].lower()
            qty = int(q) if q.isdigit() else _NUM_WORDS.get(q, 1)
            length = float(m["whole"])
            if m["num"]:
                length += int(m["num"]) / int(m["den"])
            if m["dec"]:
                length = float(f'{m["whole"]}.{m["dec"]}')
            profile = last = self._profile_for(clause, last)
            low = clause.lower()
            dm = _DEG_RE.search(clause)
            if dm:
                miter = float(dm.group(1))
            elif "miter" in low and "square" not in low:
                miter = 45.0
            else:
                miter = 0.0
            face = "back_face" if ("back" in low or "smooth" in low) else "spline_face"
            fields = dict(profile=profile, length_in=length, qty=qty, miter_deg=miter, face=face)
            if "both ends" in low and miter > 0:
                fields.update(lead_miter_deg=miter, lead_face=face)
            cuts.append(CutLine(**fields))
        if not cuts:
            raise ValueError("no cut lines found in scope")
        return Slot2Plan(cuts=cuts).model_dump_json()


class ChainParser:
    """Use ``primary``; if its server is unreachable, fall back to ``fallback``."""

    def __init__(self, primary: PlanParser, fallback: PlanParser) -> None:
        self.primary, self.fallback = primary, fallback

    def __call__(self, scope: str, feedback: Optional[str] = None) -> str:
        try:
            return self.primary(scope, feedback)
        except ParserUnavailable:
            return self.fallback(scope, feedback)
