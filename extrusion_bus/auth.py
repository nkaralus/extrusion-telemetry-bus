"""LAN authentication and role-based access control.

Roles and what they may do
    rep       /sales intake, photo uploads, customer quotes - and ONLY their own jobs
    engineer  /engineering: overrides, wireframe, BOM, release, batches; read all intakes; inventory
    operator  /bench saw controls, macro keys over the WebSocket, legacy-drop adoption, sticker reprints
    admin     everything: users, device tokens, audit log, scrap ledger, procurement orders, printer admin

Design choices (and what they do NOT claim)
    * Secrets: stdlib scrypt, per-user random salt, constant-time compare; an unknown username burns the
      same CPU as a wrong password (no user enumeration by timing).
    * Session tokens: 256 random bits, shown once, stored only as SHA-256.  Browser pages carry them in an
      HttpOnly SameSite=Strict cookie; scripts and macro-pad daemons use ``Authorization: Bearer``.
    * Device tokens: long-lived tokens an admin mints for a bench tablet (never admin role).
    * Lockout: 5 failures per username (and per IP) in 5 minutes -> 60 s lockout.
    * Policy is a single default-deny table (``POLICY``): a route nobody listed is admin-only.
    * This is access control for a shop LAN.  It is NOT transport security: serve it behind TLS (or only on a
      trusted network) if secrets must not cross the wire in clear text.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from typing import Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel

ROLES = ("rep", "engineer", "operator", "admin")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_SECRET_LEN = 8
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
COOKIE = "extbus_session"

ROLE_PERMS: Dict[str, frozenset] = {
    "rep": frozenset({"sales.write", "sales.read", "quote"}),
    "engineer": frozenset({"sales.read", "quote", "eng", "frames.read", "inventory.read", "procurement.read", "printer.read"}),
    "operator": frozenset({"bench", "bench.adopt", "frames.read", "inventory.read", "printer.read", "printer.reprint"}),
    "admin": frozenset(),   # admin is allowed everything (see ``allowed``)
}

# (method or "*", path regex, permission).  First match wins.  Anything unlisted below falls through to "admin".
POLICY: List[Tuple[str, str, str]] = [
    ("GET", r"^/(sales|engineering)?$", "public"),                       # HTML shells only; every API call is checked
    ("GET", r"^/static/", "public"),
    ("GET", r"^/health$", "public"),
    ("POST", r"^/api/auth/login$", "public"),
    ("GET", r"^/api/auth/config$", "public"),
    ("*", r"^/api/auth/(me|logout|password)$", "authed"),
    ("*", r"^/api/admin/", "admin"),
    ("GET", r"^/ledger/scrap$", "admin"),
    ("GET", r"^/api/recovery$", "admin"),
    # sales
    ("POST", r"^/api/sales/(intake|intake/answer|generate-bom|upload-photo)$", "sales.write"),
    ("GET", r"^/api/sales/jobs(/[^/]+)?$", "sales.read"),
    ("GET", r"^/media/", "sales.read"),
    ("POST", r"^/api/quote$", "quote"),
    # engineering
    ("*", r"^/api/engineering/", "eng"),
    ("POST", r"^/api/nest/(compile|commit)$", "eng"),
    # procurement
    ("GET", r"^/api/procurement/(reorder|lookahead|po-draft)$", "procurement.read"),
    ("POST", r"^/api/procurement/(order|receive)$", "procurement.write"),
    # inventory / frames / printer
    ("GET", r"^/api/inventory$", "inventory.read"),
    ("GET", r"^/api/frames$", "frames.read"),
    ("GET", r"^/api/printer(/queue)?$", "printer.read"),
    ("POST", r"^/api/printer/reprint$", "printer.reprint"),
    ("POST", r"^/api/printer/(retry|test)$", "admin"),
    # the saw bench
    ("POST", r"^/api/saw/adopt-drop$", "bench.adopt"),
    ("*", r"^/(order|cut|drop|label)(/|$)", "bench"),
    ("*", r"^/ws/saw/", "bench"),
]
_COMPILED = [(m, re.compile(p), perm) for m, p, perm in POLICY]


def has_explicit_rule(method: str, path: str) -> bool:
    """True if some POLICY row names this route (as opposed to it falling through to the admin default)."""
    return any((m == "*" or m == method.upper()) and rx.search(path) for m, rx, _ in _COMPILED)


def required_permission(method: str, path: str) -> str:
    for m, rx, perm in _COMPILED:
        if (m == "*" or m == method.upper()) and rx.search(path):
            return perm
    return "admin"                                                       # default deny


def allowed(role: Optional[str], perm: str) -> bool:
    if perm == "public":
        return True
    if role is None:
        return False
    if perm == "authed" or role == "admin":
        return True
    return perm in ROLE_PERMS.get(role, frozenset())


class Principal(BaseModel):
    username: str
    role: str
    kind: str = "session"          # session | device | off
    session_id: str = ""


AUTH_OFF = Principal(username="(auth off)", role="admin", kind="off")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
def hash_secret(secret: str, *, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode(), salt=salt, n=SCRYPT["n"], r=SCRYPT["r"], p=SCRYPT["p"], dklen=32)
    return "scrypt${n}${r}${p}${s}${h}".format(**SCRYPT, s=base64.b64encode(salt).decode(), h=base64.b64encode(dk).decode())


def verify_secret(secret: str, stored: str) -> bool:
    try:
        _, n, r, p, s, h = stored.split("$")
        dk = hashlib.scrypt(secret.encode(), salt=base64.b64decode(s), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate_secret(username: str, secret: str) -> None:
    if len(secret) < MIN_SECRET_LEN:
        raise AuthError(f"secret must be at least {MIN_SECRET_LEN} characters", 422)
    if secret.lower() == username.lower():
        raise AuthError("secret must not equal the username", 422)


_DUMMY_HASH = hash_secret("x" * 16)


class AuthService:
    def __init__(self, registry, *, session_hours: float = 12.0, max_failures: int = 5, lockout_s: float = 60.0,
                 window_s: float = 300.0, clock: Optional[Callable[[], float]] = None) -> None:
        self.reg = registry
        self.session_s = session_hours * 3600.0
        self.max_failures, self.lockout_s, self.window_s = max_failures, lockout_s, window_s
        self.clock = clock or registry.clock
        self._fails: Dict[str, List[float]] = {}

    # ---- users ---------------------------------------------------------------------------- #
    def has_users(self) -> bool:
        return bool(self.reg.state.users)

    def active_admins(self) -> List[str]:
        return [u for u, r in self.reg.state.users.items() if r["role"] == "admin" and r.get("active", True)]

    def create_user(self, username: str, role: str, secret: str, by: str = "system") -> dict:
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise AuthError("username: 2-32 chars of a-z 0-9 . _ - (start with a letter or digit)", 422)
        if role not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}", 422)
        validate_secret(username, secret)
        with self.reg.transaction():
            if username in self.reg.state.users:
                raise AuthError(f"user {username!r} already exists", 409)
            rec = {"role": role, "hash": hash_secret(secret), "active": True, "created_ts": self.clock(),
                   "created_by": by, "last_login_ts": None}
            self.reg.state.users[username] = rec
        return self.public_user(username, rec)

    @staticmethod
    def public_user(username: str, rec: dict) -> dict:
        return {"username": username, "role": rec["role"], "active": rec.get("active", True),
                "created_ts": rec.get("created_ts"), "created_by": rec.get("created_by"),
                "last_login_ts": rec.get("last_login_ts")}

    def list_users(self) -> List[dict]:
        return [self.public_user(u, r) for u, r in sorted(self.reg.state.users.items())]

    def set_secret(self, username: str, secret: str) -> None:
        with self.reg.transaction():
            rec = self.reg.state.users.get(username)
            if rec is None:
                raise AuthError("no such user", 404)
            validate_secret(username, secret)
            rec["hash"] = hash_secret(secret)
            self._revoke_user_sessions(username)

    def set_active(self, username: str, active: bool) -> None:
        with self.reg.transaction():
            rec = self.reg.state.users.get(username)
            if rec is None:
                raise AuthError("no such user", 404)
            if not active and rec["role"] == "admin" and self.active_admins() == [username]:
                raise AuthError("cannot disable the last active admin", 409)
            rec["active"] = active
            if not active:
                self._revoke_user_sessions(username)

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}", 422)
        with self.reg.transaction():
            rec = self.reg.state.users.get(username)
            if rec is None:
                raise AuthError("no such user", 404)
            if rec["role"] == "admin" and role != "admin" and self.active_admins() == [username]:
                raise AuthError("cannot demote the last active admin", 409)
            rec["role"] = role

    def _revoke_user_sessions(self, username: str) -> int:
        dead = [t for t, s in self.reg.state.sessions.items() if s.get("user") == username]
        for t in dead:
            del self.reg.state.sessions[t]
        return len(dead)

    # ---- login throttling -------------------------------------------------------------------- #
    def _locked(self, key: str) -> float:
        now = self.clock()
        hits = [t for t in self._fails.get(key, []) if now - t < self.window_s]
        self._fails[key] = hits
        if len(hits) >= self.max_failures:
            return max(0.0, hits[-1] + self.lockout_s - now)
        return 0.0

    def _fail(self, *keys: str) -> None:
        now = self.clock()
        for k in keys:
            self._fails.setdefault(k, []).append(now)

    # ---- sessions ------------------------------------------------------------------------------ #
    def login(self, username: str, secret: str, ip: str = "") -> Tuple[str, dict]:
        username = (username or "").strip().lower()
        wait = max(self._locked("u:" + username), self._locked("ip:" + ip) if ip else 0.0)
        if wait > 0:
            raise AuthError(f"too many failed attempts; try again in {int(wait) + 1} s", 429)
        rec = self.reg.state.users.get(username)
        ok = verify_secret(secret or "", rec["hash"] if rec else _DUMMY_HASH)     # same cost either way
        if not rec or not ok or not rec.get("active", True):
            self._fail("u:" + username, *(["ip:" + ip] if ip else []))
            raise AuthError("invalid username or secret", 401)
        self._fails.pop("u:" + username, None)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        sess = {"user": username, "role": rec["role"], "kind": "session", "label": "", "created_ts": now,
                "expires_ts": now + self.session_s, "ip": ip}
        with self.reg.transaction():
            self.reg.state.sessions[token_id(token)] = sess
            self.reg.state.users[username]["last_login_ts"] = now
            self._purge_expired()
        return token, sess

    def mint_device_token(self, role: str, label: str, days: float, by: str) -> Tuple[str, dict]:
        if role not in ROLES or role == "admin":
            raise AuthError("device tokens may be rep, engineer or operator (never admin)", 422)
        if not (0 < days <= 365):
            raise AuthError("days must be between 0 and 365", 422)
        label = label.strip()[:60]
        if not label:
            raise AuthError("give the device a label (e.g. bench-1)", 422)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        sess = {"user": f"device:{label}", "role": role, "kind": "device", "label": label, "created_ts": now,
                "expires_ts": now + days * 86400.0, "ip": "", "created_by": by}
        with self.reg.transaction():
            self.reg.state.sessions[token_id(token)] = sess
        return token, sess

    def authenticate(self, token: Optional[str]) -> Optional[Principal]:
        if not token:
            return None
        tid = token_id(token)
        sess = self.reg.state.sessions.get(tid)
        if sess is None or sess["expires_ts"] <= self.clock():
            return None
        if sess["kind"] == "session":                       # role and active flag come from the CURRENT user record
            rec = self.reg.state.users.get(sess["user"])
            if rec is None or not rec.get("active", True):
                return None
            return Principal(username=sess["user"], role=rec["role"], kind="session", session_id=tid)
        return Principal(username=sess["user"], role=sess["role"], kind="device", session_id=tid)

    def logout(self, token: str) -> None:
        with self.reg.transaction():
            self.reg.state.sessions.pop(token_id(token), None)

    def revoke(self, session_id_prefix: str) -> int:
        with self.reg.transaction():
            hit = [t for t in self.reg.state.sessions if t.startswith(session_id_prefix)] if len(session_id_prefix) >= 8 else []
            for t in hit:
                del self.reg.state.sessions[t]
            return len(hit)

    def list_sessions(self) -> List[dict]:
        now = self.clock()
        return [{"id": t[:12], "user": s["user"], "role": s["role"], "kind": s["kind"], "label": s.get("label", ""),
                 "expires_ts": s["expires_ts"], "ip": s.get("ip", "")}
                for t, s in sorted(self.reg.state.sessions.items(), key=lambda kv: kv[1]["created_ts"])
                if s["expires_ts"] > now]

    def _purge_expired(self) -> None:
        now = self.clock()
        for t in [t for t, s in self.reg.state.sessions.items() if s["expires_ts"] <= now]:
            del self.reg.state.sessions[t]


def bearer_token(headers, cookies, query_token: Optional[str] = None) -> Optional[str]:
    h = headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return cookies.get(COOKIE) or query_token
