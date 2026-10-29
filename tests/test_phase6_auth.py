import io
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from extrusion_bus import auth as A
from extrusion_bus.auth import (
    AuthError, AuthService, allowed, has_explicit_rule, hash_secret, required_permission, token_id, verify_secret,
)
from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import End
from extrusion_bus.registry import Registry
from extrusion_bus.sales_gates import evaluate_intake
from extrusion_bus.sales_intake import SalesIntakePackage
from extrusion_bus.server import create_app

import imgtools as T
from conftest import PN

SECRETS = {"rep1": "rep1-secret-ok", "rep2": "rep2-secret-ok", "eng": "eng-secret-ok", "op": "op-secret-ok"}
ROLE_OF = {"rep1": "rep", "rep2": "rep", "eng": "engineer", "op": "operator"}


class Clock:
    def __init__(self, t=1_700_000_000.0): self.t = t
    def __call__(self): return self.t


@pytest.fixture
def shop(tmp_path):
    clk = Clock()
    reg = Registry.load(tmp_path / "bus.db", clock=clk)
    reg.add_raw(PN, 288, count=8); reg.add_raw("EXT-220-BOX", 288, count=8)
    reg.add_remnant(PN, 120, End.square())
    app = create_app(reg, parser=RuleParser(reg), media_dir=tmp_path / "media", auth=True)
    svc = app.state.auth
    for u, secret in SECRETS.items():
        svc.create_user(u, ROLE_OF[u], secret, by="test")
    yield reg, app, clk
    reg.close()


def client(app, headers=None):
    return TestClient(app, headers={"x-extbus": "1", **(headers or {})})


def login(app, user, secret=None):
    c = client(app)
    r = c.post("/api/auth/login", json={"username": user, "secret": secret or SECRETS.get(user) or app.state.bootstrap_secret})
    assert r.status_code == 200, r.text
    return c, r.json()


def admin(app):
    return login(app, "admin", app.state.bootstrap_secret)[0]


def photo(c, job, kind="tie_in_closeup"):
    return c.post("/api/sales/upload-photo", data={"job_id": job, "kind": kind},
                  files={"file": ("p.jpg", T.jpeg(T.scene(1600, 1200)), "image/jpeg")})


# ============================ primitives ============================ #
def test_scrypt_hashes_are_salted_verifiable_and_never_plaintext():
    a, b = hash_secret("correct horse"), hash_secret("correct horse")
    assert a != b and a.startswith("scrypt$16384$8$1$") and "correct horse" not in a
    assert verify_secret("correct horse", a) and not verify_secret("correct hors", a)
    for broken in ("", "scrypt$x", "plain", "scrypt$1$1$1$!!$!!"):
        assert not verify_secret("anything", broken)
    assert token_id("t") == token_id("t") and len(token_id("t")) == 64 and token_id("t") != token_id("u")


def test_user_validation_rules(shop):
    reg, app, _ = shop
    svc = app.state.auth
    for bad_name in ("a", "Bad Name", "../x", "x" * 40, "-lead"):
        with pytest.raises(AuthError):
            svc.create_user(bad_name, "rep", "long-enough-secret")
    with pytest.raises(AuthError):
        svc.create_user("newbie", "king", "long-enough-secret")
    with pytest.raises(AuthError):
        svc.create_user("newbie", "rep", "short")
    with pytest.raises(AuthError):
        svc.create_user("newbie123", "rep", "NEWBIE123")                       # secret equals username
    with pytest.raises(AuthError) as e:
        svc.create_user("rep1", "rep", "long-enough-secret")
    assert e.value.status == 409
    assert svc.create_user("  NewUser ", "rep", "long-enough-secret")["username"] == "newuser"


def test_policy_is_default_deny_and_role_matrix_is_exact():
    assert required_permission("GET", "/api/brand-new-endpoint") == "admin"
    assert required_permission("POST", "/order/abc/ack") == "bench" and required_permission("GET", "/") == "public"
    assert not allowed(None, "bench") and allowed(None, "public") and allowed("rep", "authed")
    assert allowed("admin", "anything.at.all") and not allowed("operator", "eng") and not allowed("engineer", "bench")
    assert allowed("operator", "bench.adopt") and not allowed("engineer", "sales.write") and not allowed("rep", "frames.read")


def test_every_registered_route_has_an_explicit_policy_rule(shop):
    _, app, _ = shop
    skip = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    missing, seen = [], 0
    for r in app.routes:
        path = getattr(r, "path", "")
        if path in skip or path.startswith("/static"):
            continue
        for m in (getattr(r, "methods", None) or {"GET"}) - {"HEAD", "OPTIONS"}:
            seen += 1
            if not has_explicit_rule(m, path.replace("{", "").replace("}", "")):
                missing.append((m, path))
    assert seen > 55 and missing == [], f"routes with no policy rule (they would be admin-only by accident): {missing}"


# ============================ the role matrix over real HTTP ============================ #
ROUTES = {
    "sales_intake": ("POST", "/api/sales/intake", {"job_id": "MX-1"}),
    "sales_jobs": ("GET", "/api/sales/jobs", None),
    "quote": ("POST", "/api/quote", {"job_id": "Q", "cuts": [{"profile": PN, "qty": 1, "length_in": 58}]}),
    "preview": ("POST", "/api/engineering/preview", {"job_id": "nope"}),
    "release": ("POST", "/api/engineering/release", {"job_id": "nope"}),
    "batch_compile": ("POST", "/api/engineering/batch-compile", {"job_ids": ["a", "b"]}),
    "batchable": ("GET", "/api/engineering/batchable", None),
    "nest_compile": ("POST", "/api/nest/compile", {"job_id": "Q", "cuts": [{"profile": PN, "qty": 1, "length_in": 58}]}),
    "inventory": ("GET", "/api/inventory", None),
    "frames": ("GET", "/api/frames", None),
    "reorder": ("GET", "/api/procurement/reorder", None),
    "po_place": ("POST", "/api/procurement/order", {"profile": PN, "qty": 1}),
    "po_receive": ("POST", "/api/procurement/receive", {"profile": PN, "qty": 1}),
    "scrap": ("GET", "/ledger/scrap", None),
    "recovery": ("GET", "/api/recovery", None),
    "order_open": ("POST", "/order", {"job_id": "X", "scope": "zzz"}),
    "order_next": ("POST", "/order/zzz/next", None),
    "adopt": ("POST", "/api/saw/adopt-drop", {"profile": PN, "tape_in": "??"}),
    "cut_verify": ("POST", "/cut/verify", {}),
    "label": ("GET", "/label/10.2.2.250", None),
    "printer_status": ("GET", "/api/printer", None),
    "printer_reprint": ("POST", "/api/printer/reprint", {"addr": "10.2.2.250"}),
    "printer_retry": ("POST", "/api/printer/retry", None),
    "admin_users": ("GET", "/api/admin/users", None),
    "admin_audit": ("GET", "/api/admin/audit", None),
}
ALLOWED = {
    "rep": {"sales_intake", "sales_jobs", "quote"},
    "engineer": {"sales_jobs", "quote", "preview", "release", "batch_compile", "batchable", "nest_compile", "inventory",
                 "frames", "reorder", "printer_status"},
    "operator": {"order_open", "order_next", "adopt", "cut_verify", "label", "frames", "inventory", "printer_status", "printer_reprint"},
    "admin": set(ROUTES),
}


@pytest.mark.parametrize("who", ["rep1", "eng", "op", "admin"])
def test_each_role_reaches_exactly_its_routes(shop, who):
    reg, app, _ = shop
    c, _ = login(app, who)
    role = ROLE_OF.get(who, "admin")
    wrong = []
    for label, (method, path, body) in ROUTES.items():
        code = c.request(method, path, json=body).status_code
        ok = code not in (401, 403)
        if ok != (label in ALLOWED[role]):
            wrong.append((label, code))
    assert wrong == [], f"{role}: unexpected access decisions {wrong}"


def test_unauthenticated_requests_are_refused_but_the_shells_are_public(shop):
    _, app, _ = shop
    c = TestClient(app)
    for label, (method, path, body) in ROUTES.items():
        assert c.request(method, path, json=body).status_code == 401, label
    for path in ("/", "/sales", "/engineering", "/static/app.css", "/static/common.js"):
        assert c.get(path).status_code == 200, path
    assert c.get("/health").json() == {"ok": True}                           # no inventory counts for strangers
    assert c.get("/api/auth/config").json()["auth"] is True
    assert c.get("/api/auth/me").status_code == 401 and c.get("/openapi.json").status_code == 401


# ============================ login, sessions, cookies ============================ #
def test_login_sets_a_hardened_cookie_and_a_usable_bearer_token(shop):
    reg, app, _ = shop
    c = client(app)
    r = c.post("/api/auth/login", json={"username": "Rep1", "secret": SECRETS["rep1"]})
    assert r.status_code == 200 and r.json()["role"] == "rep"
    cookie = r.headers["set-cookie"].lower()
    assert "extbus_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    me = c.get("/api/auth/me").json()
    assert me["username"] == "rep1" and me["role"] == "rep" and "sales.write" in me["perms"] and me["kind"] == "session"
    b = TestClient(app, headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert b.get("/api/auth/me").json()["username"] == "rep1"
    https = TestClient(app, base_url="https://testserver", headers={"x-extbus": "1"})
    r2 = https.post("/api/auth/login", json={"username": "rep1", "secret": SECRETS["rep1"]})
    assert "secure" in r2.headers["set-cookie"].lower()                      # Secure over TLS


def test_secrets_and_raw_tokens_are_never_stored(shop):
    reg, app, _ = shop
    _, body = login(app, "rep1")
    dump = reg.state.model_dump_json()
    assert SECRETS["rep1"] not in dump and body["token"] not in dump and token_id(body["token"]) in reg.state.sessions
    assert "hash" not in json.dumps(admin(app).get("/api/admin/users").json())      # the API never returns hashes


def test_wrong_and_unknown_users_get_the_same_answer_and_the_same_cpu_cost(shop, monkeypatch):
    _, app, _ = shop
    calls = []
    real = A.verify_secret
    monkeypatch.setattr(A, "verify_secret", lambda s, h: (calls.append(1), real(s, h))[1])
    c = client(app)
    a = c.post("/api/auth/login", json={"username": "rep1", "secret": "wrong-secret-1"})
    b = c.post("/api/auth/login", json={"username": "nobody", "secret": "wrong-secret-1"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json() and len(calls) == 2


def test_lockout_after_repeated_failures_then_recovery(shop):
    reg, app, clk = shop
    c = client(app)
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "rep1", "secret": "nope-nope-1"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "rep1", "secret": SECRETS["rep1"]})
    assert r.status_code == 429 and "try again" in r.json()["detail"]         # even the right secret is refused while locked
    clk.t += 61
    assert c.post("/api/auth/login", json={"username": "rep1", "secret": SECRETS["rep1"]}).status_code == 200


def test_one_ip_cannot_spray_many_usernames(shop):
    _, app, _ = shop
    c = client(app)
    for i in range(5):
        c.post("/api/auth/login", json={"username": f"ghost{i}", "secret": "nope-nope-1"})
    assert c.post("/api/auth/login", json={"username": "eng", "secret": SECRETS["eng"]}).status_code == 429


def test_sessions_expire_logout_ends_them_and_disabling_a_user_is_immediate(shop):
    reg, app, clk = shop
    c, body = login(app, "rep1")
    assert c.get("/api/sales/jobs").status_code == 200
    clk.t += 12 * 3600 + 1
    assert c.get("/api/sales/jobs").status_code == 401                         # absolute expiry
    clk.t -= 12 * 3600 + 1
    c2, _ = login(app, "rep2")
    assert c2.post("/api/auth/logout").status_code == 200 and c2.get("/api/sales/jobs").status_code == 401
    c3, b3 = login(app, "rep1")
    ad = admin(app)
    assert ad.post("/api/admin/users/rep1/active", json={"active": False}).status_code == 200
    assert c3.get("/api/sales/jobs").status_code == 401 and TestClient(app, headers={"Authorization": f"Bearer {b3['token']}"}).get("/api/auth/me").status_code == 401
    assert client(app).post("/api/auth/login", json={"username": "rep1", "secret": SECRETS["rep1"]}).status_code == 401
    ad.post("/api/admin/users/rep1/active", json={"active": True})
    assert client(app).post("/api/auth/login", json={"username": "rep1", "secret": SECRETS["rep1"]}).status_code == 200


def test_authenticate_rechecks_the_user_even_if_a_session_row_outlived_it(shop):
    """Defense in depth: a restored backup or a hand edit can leave a session whose user is disabled, demoted or gone."""
    reg, app, _ = shop
    svc = app.state.auth
    tok = svc.login("rep1", SECRETS["rep1"])[0]
    assert svc.authenticate(tok).username == "rep1"
    reg.state.users["rep1"]["active"] = False                                   # no API call, so the session row is untouched
    assert token_id(tok) in reg.state.sessions and svc.authenticate(tok) is None
    reg.state.users["rep1"]["active"] = True
    assert svc.authenticate(tok).role == "rep"
    reg.state.users["rep1"]["role"] = "engineer"
    assert svc.authenticate(tok).role == "engineer"                             # the CURRENT role, not the one at login
    del reg.state.users["rep1"]
    assert svc.authenticate(tok) is None


def test_a_role_change_takes_effect_on_live_sessions(shop):
    reg, app, _ = shop
    c, _ = login(app, "rep1")
    assert c.get("/api/inventory").status_code == 403
    admin(app).post("/api/admin/users/rep1/role", json={"role": "engineer"})
    assert c.get("/api/inventory").status_code == 200 and c.post("/api/sales/intake", json={"job_id": "Z"}).status_code == 403


def test_secret_reset_and_self_service_change_end_old_sessions(shop):
    reg, app, _ = shop
    c, _ = login(app, "eng")
    assert c.post("/api/auth/password", json={"old": "wrong-wrong-1", "new": "brand-new-secret"}).status_code == 401
    assert c.post("/api/auth/password", json={"old": SECRETS["eng"], "new": "short"}).status_code == 422
    assert c.post("/api/auth/password", json={"old": SECRETS["eng"], "new": "brand-new-secret"}).status_code == 200
    assert c.get("/api/auth/me").status_code == 401                            # old session ended
    assert client(app).post("/api/auth/login", json={"username": "eng", "secret": SECRETS["eng"]}).status_code == 401
    assert client(app).post("/api/auth/login", json={"username": "eng", "secret": "brand-new-secret"}).status_code == 200
    ad = admin(app)
    assert ad.post("/api/admin/users/eng/secret", json={"secret": "admin-reset-secret"}).status_code == 200
    assert client(app).post("/api/auth/login", json={"username": "eng", "secret": "admin-reset-secret"}).status_code == 200


def test_the_last_active_admin_cannot_be_disabled_or_demoted(shop):
    reg, app, _ = shop
    ad = admin(app)
    assert ad.post("/api/admin/users/admin/active", json={"active": False}).status_code == 409
    assert ad.post("/api/admin/users/admin/role", json={"role": "operator"}).status_code == 409
    ad.post("/api/admin/users", json={"username": "boss2", "role": "admin", "secret": "boss2-secret-ok"})
    assert ad.post("/api/admin/users/admin/active", json={"active": False}).status_code == 200      # fine with a second admin
    assert client(app).post("/api/auth/login", json={"username": "boss2", "secret": "boss2-secret-ok"}).status_code == 200


# ============================ CSRF ============================ #
def test_cookie_authenticated_writes_need_the_custom_header_and_a_matching_origin(shop):
    _, app, _ = shop
    c, _ = login(app, "rep1")
    bare = TestClient(app, cookies=c.cookies)                                  # same cookie, but no x-extbus header (a hostile page)
    assert bare.post("/api/sales/intake", json={"job_id": "C1"}).status_code == 403
    assert bare.get("/api/sales/jobs").status_code == 200                       # safe methods are fine
    evil = TestClient(app, cookies=c.cookies, headers={"x-extbus": "1", "origin": "http://evil.example"})
    assert evil.post("/api/sales/intake", json={"job_id": "C1"}).status_code == 403
    same = TestClient(app, cookies=c.cookies, headers={"x-extbus": "1", "origin": "http://testserver"})
    assert same.post("/api/sales/intake", json={"job_id": "C1"}).status_code == 200


# ============================ rep ownership ============================ #
def test_a_rep_only_ever_sees_and_touches_their_own_jobs(shop):
    reg, app, _ = shop
    r1, _ = login(app, "rep1"); r2, _ = login(app, "rep2"); eng, _ = login(app, "eng"); ad = admin(app)
    assert photo(r1, "OWN-1").status_code == 200
    assert r1.post("/api/sales/intake/answer", json={"job_id": "OWN-1", "answers": {"width_in": 156}}).status_code == 200
    assert [j["job_id"] for j in r1.get("/api/sales/jobs").json()] == ["OWN-1"]
    assert r2.get("/api/sales/jobs").json() == []
    assert r2.get("/api/sales/jobs/OWN-1").status_code == 403
    assert r2.post("/api/sales/intake/answer", json={"job_id": "OWN-1", "answers": {"width_in": 1}}).status_code == 403
    assert photo(r2, "OWN-1").status_code == 403
    assert r2.post("/api/sales/intake", json={"job_id": "OWN-1"}).status_code == 403
    assert r2.post("/api/sales/generate-bom", json={"job_id": "OWN-1"}).status_code == 403
    assert r2.get("/api/sales/jobs/OWN-1?create=1").status_code == 403
    assert reg.state.intakes["OWN-1"]["package"]["width_in"] == 156 and reg.state.intakes["OWN-1"]["owner"] == "rep1"
    assert {j["job_id"] for j in eng.get("/api/sales/jobs").json()} == {"OWN-1"} == {j["job_id"] for j in ad.get("/api/sales/jobs").json()}
    assert eng.get("/api/sales/jobs/OWN-1").status_code == 200


def test_job_photos_follow_the_same_ownership(shop):
    reg, app, _ = shop
    r1, _ = login(app, "rep1"); r2, _ = login(app, "rep2"); eng, _ = login(app, "eng"); op, _ = login(app, "op")
    photo(r1, "PH-1")
    url = "/media/PH-1/tie_in_closeup.jpg"
    assert r1.get(url).status_code == 200 and eng.get(url).status_code == 200
    assert r2.get(url).status_code == 403 and op.get(url).status_code == 403 and TestClient(app).get(url).status_code == 401
    assert r1.get(url).headers["x-content-type-options"] == "nosniff"
    for bad in ("/media/PH-1/../../bus.db", "/media/%2e%2e/bus.db", "/media/PH-1/x.exe", "/media/NOPE/tie_in_closeup.jpg"):
        r = r1.get(bad)
        assert r.status_code in (403, 404) and b"SQLite" not in r.content, bad        # default-deny or not found; never served


def test_legacy_ownerless_jobs_are_hidden_from_reps_but_not_from_engineers(shop):
    reg, app, _ = shop
    pkg = SalesIntakePackage(job_id="LEGACY")
    reg.save_intake("LEGACY", pkg.model_dump(mode="json"), evaluate_intake(pkg).model_dump(mode="json"))
    r1, _ = login(app, "rep1"); eng, _ = login(app, "eng")
    assert r1.get("/api/sales/jobs").json() == [] and r1.get("/api/sales/jobs/LEGACY").status_code == 403
    assert eng.get("/api/sales/jobs/LEGACY").status_code == 200


def test_only_a_rep_may_start_a_new_intake(shop):
    _, app, _ = shop
    eng, _ = login(app, "eng")
    assert eng.get("/api/sales/jobs/NEW-E?create=1").status_code == 403
    assert login(app, "rep1")[0].get("/api/sales/jobs/NEW-R?create=1").status_code == 200


# ============================ device tokens ============================ #
def test_device_tokens_for_the_bench_are_scoped_hashed_and_revocable(shop):
    reg, app, clk = shop
    ad = admin(app)
    assert ad.post("/api/admin/tokens", json={"role": "admin", "label": "x", "days": 30}).status_code == 422
    assert ad.post("/api/admin/tokens", json={"role": "operator", "label": " ", "days": 30}).status_code == 422
    assert ad.post("/api/admin/tokens", json={"role": "operator", "label": "bench", "days": 900}).status_code == 422
    tok = ad.post("/api/admin/tokens", json={"role": "operator", "label": "bench-1", "days": 90}).json()
    dev = TestClient(app, headers={"Authorization": f"Bearer {tok['token']}"})
    assert dev.get("/api/auth/me").json()["kind"] == "device" and dev.get("/api/frames").status_code == 200
    assert dev.get("/api/engineering/batchable").status_code == 403 and dev.get("/api/admin/users").status_code == 403
    assert tok["token"] not in reg.state.model_dump_json()
    sid = [s for s in ad.get("/api/admin/sessions").json() if s["kind"] == "device"][0]
    assert dev.post("/api/admin/sessions/x/revoke").status_code == 403
    assert ad.post(f"/api/admin/sessions/{sid['id']}/revoke").json() == {"revoked": 1}
    assert dev.get("/api/auth/me").status_code == 401
    t2 = ad.post("/api/admin/tokens", json={"role": "operator", "label": "bench-2", "days": 1}).json()
    clk.t += 86400 + 5
    assert TestClient(app, headers={"Authorization": f"Bearer {t2['token']}"}).get("/api/auth/me").status_code == 401


# ============================ WebSocket ============================ #
def ws_code(c, headers=None, path="/ws/saw/SAW_1"):
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(path, headers=headers or {}):
            pass
    return e.value.code


def test_websocket_requires_a_session_and_the_bench_permission(shop):
    reg, app, _ = shop
    c = TestClient(app)
    assert ws_code(c) == 4401
    for who in ("rep1", "eng"):
        _, body = login(app, who)
        assert ws_code(c, {"authorization": f"Bearer {body['token']}"}) == 4403, who
    _, op = login(app, "op")
    with c.websocket_connect("/ws/saw/SAW_1", headers={"authorization": f"Bearer {op['token']}"}) as ws:
        assert ws.receive_json()["type"] == "state"
    with c.websocket_connect("/ws/saw/SAW_1", headers={"cookie": f"extbus_session={op['token']}"}) as ws:     # browser path
        assert ws.receive_json()["hello"]
    with c.websocket_connect(f"/ws/saw/SAW_1?token={op['token']}") as ws:                                         # macro-pad daemon path
        assert ws.receive_json()["hello"]


def test_websocket_blocks_cross_site_hijacking_and_ends_with_the_session(shop):
    reg, app, _ = shop
    c = TestClient(app)
    _, op = login(app, "op")
    h = {"authorization": f"Bearer {op['token']}"}
    assert ws_code(c, {**h, "origin": "http://evil.example"}) == 4403
    with c.websocket_connect("/ws/saw/SAW_1", headers={**h, "origin": "http://testserver"}) as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        app.state.auth.revoke(token_id(op["token"])[:12])                      # logged out elsewhere mid-shift
        ws.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401


# ============================ audit ============================ #
def test_consequential_actions_are_audited_with_who_and_what(shop):
    reg, app, _ = shop
    eng, _ = login(app, "eng"); op, _ = login(app, "op"); ad = admin(app)
    client(app).post("/api/auth/login", json={"username": "rep1", "secret": "bad-bad-bad-1"})
    op.get("/api/engineering/batchable")                                        # denied
    op.post("/api/saw/adopt-drop", json={"profile": PN, "tape_in": "74 1/4"})
    ad.post("/api/procurement/order", json={"profile": PN, "qty": 3})
    ad.get("/ledger/scrap")
    ad.post("/api/admin/users", json={"username": "newguy", "role": "rep", "secret": "newguy-secret-ok"})
    rows = ad.get("/api/admin/audit?limit=100").json()
    by = {}
    for r in rows:
        by.setdefault(r["action"], []).append(r)
    assert by["LOGIN_FAILED"][0]["user"] == "rep1" and by["LOGIN"] and by["USER_CREATED"][0]["user"] == "admin"
    created = by["USER_CREATED"][0]                                              # actor stays the actor; the new user is the target
    assert (created["target_user"], created["target_role"]) == ("newguy", "rep")
    den = by["DENIED"][0]
    assert (den["user"], den["role"], den["path"], den["needs"]) == ("op", "operator", "/api/engineering/batchable", "eng")
    assert by["ADOPT_DROP"][0]["user"] == "op" and by["PO_PLACED"][0]["qty"] == 3 and by["SCRAP_LEDGER_VIEWED"]
    assert [r["action"] for r in ad.get("/api/admin/audit?action=PO_PLACED").json()] == ["PO_PLACED"]
    assert {r["user"] for r in ad.get("/api/admin/audit?user=op").json()} == {"op"}
    assert all("secret" not in json.dumps(r).lower() or r["action"] == "LOGIN_FAILED" for r in rows)
    assert rows == sorted(rows, key=lambda r: -r["seq"])                        # newest first


def test_release_is_audited_with_the_engineers_overrides(shop):
    from test_phase4_viewports import make_ready
    reg, app, _ = shop
    r1, _ = login(app, "rep1")
    for k in ("wide_context", "tie_in_closeup", "sketch"):
        photo(r1, "RL-1", k)
    r1.post("/api/sales/intake/answer", json={"job_id": "RL-1", "answers": {
        "width_in": 156, "proj_in": 144, "house_h_in": 120, "front_h_in": 96, "substrate": "brick",
        "door_swing_detected": "no", "obstacles_checked": "yes"}})
    eng, _ = login(app, "eng")
    r = eng.post("/api/engineering/release", json={"job_id": "RL-1", "overrides": {"front_h_in": "90"}})
    assert r.status_code == 200, r.text
    a = [x for x in admin(app).get("/api/admin/audit?action=RELEASE").json()][0]
    assert (a["user"], a["job_id"], a["overrides"], a["cart"].startswith("10.4.1.")) == ("eng", "RL-1", {"front_h_in": "90"}, True)


# ============================ bootstrap + modes ============================ #
def test_first_boot_creates_an_admin_with_a_random_secret_once(tmp_path, capsys):
    reg = Registry.load(tmp_path / "fresh.db")
    app = create_app(reg, parser=lambda s, f: "{}", media_dir=tmp_path / "m", auth=True)
    secret = app.state.bootstrap_secret
    assert secret and len(secret) >= 12 and secret in capsys.readouterr().out
    assert [u["username"] for u in app.state.auth.list_users()] == ["admin"]
    c = client(app)
    assert c.post("/api/auth/login", json={"username": "admin", "secret": secret}).status_code == 200
    app2 = create_app(reg, parser=lambda s, f: "{}", media_dir=tmp_path / "m", auth=True)    # restart: no second bootstrap
    assert app2.state.bootstrap_secret is None and len(app2.state.auth.list_users()) == 1
    reg.close()


def test_auth_is_on_by_default_and_off_only_when_asked(tmp_path, monkeypatch):
    reg = Registry.load(tmp_path / "d.db")
    monkeypatch.delenv("EXTBUS_AUTH", raising=False)
    assert create_app(reg, parser=lambda s, f: "{}", media_dir=tmp_path / "m").state.auth_on is True
    monkeypatch.setenv("EXTBUS_AUTH", "off")
    off = create_app(reg, parser=lambda s, f: "{}", media_dir=tmp_path / "m")
    assert off.state.auth_on is False
    c = TestClient(off)
    assert c.get("/api/inventory").status_code == 200 and c.get("/api/auth/me").json()["role"] == "admin"
    assert c.post("/api/auth/login", json={"username": "x", "secret": "y"}).status_code == 409
    reg.close()


def test_security_headers_on_api_responses(shop):
    _, app, _ = shop
    r = login(app, "eng")[0].get("/api/inventory")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


# ============================ offline user CLI ============================ #
def test_cli_can_create_list_and_reset_users_while_the_server_is_stopped(tmp_path, monkeypatch, capsys):
    from extrusion_bus import cli
    db = tmp_path / "u.db"
    Registry.load(db).close()
    def run(argv, secret):
        monkeypatch.setattr("sys.stdin", io.StringIO(secret + "\n"))
        return cli.main(argv)
    assert run(["user-add", "--db", str(db), "alice", "--role", "operator", "--secret-stdin"], "alice-secret-1") == 0
    assert run(["user-add", "--db", str(db), "alice", "--role", "rep", "--secret-stdin"], "alice-secret-1") == 2      # duplicate
    assert run(["user-add", "--db", str(db), "weak", "--role", "rep", "--secret-stdin"], "short") == 2
    assert cli.main(["user-list", "--db", str(db)]) == 0
    assert "alice" in capsys.readouterr().out
    assert run(["user-passwd", "--db", str(db), "alice", "--secret-stdin"], "alice-secret-2") == 0
    reg = Registry.load(db)
    svc = AuthService(reg)
    assert svc.login("alice", "alice-secret-2")[1]["role"] == "operator"
    with pytest.raises(AuthError):
        svc.login("alice", "alice-secret-1")
    assert any(a["action"] == "CLI_USER_ADD" for a in reg.state.audit)
    reg.close()
