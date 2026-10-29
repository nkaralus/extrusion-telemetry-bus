import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from extrusion_bus import registry as R
from extrusion_bus import store as S
from extrusion_bus.batch import release_batch
from extrusion_bus.bus import FabricationBus, GateRejected
from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import End, Face
from extrusion_bus.printer import MemoryTransport, PrinterService
from extrusion_bus.registry import Registry, RegistryState, StockItem
from extrusion_bus.server import create_app
from extrusion_bus.store import SqliteStore, StoreError, TrackedDict, export_json, import_json, read_state_readonly

from conftest import PN, make_proposal
from test_phase4_viewports import make_ready

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_py(code, **kw):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": ROOT}, timeout=60, **kw)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "bus.db"


@pytest.fixture
def sreg(db):
    reg = Registry.load(db)
    yield reg
    reg.close()


def disk_equals_memory(reg):
    disk = read_state_readonly(reg.path)
    mem = reg.state.model_dump()
    d = disk.model_dump()
    for k in mem:
        assert d[k] == mem[k], f"collection {k!r} on disk differs from memory"


class FailOnCommit:
    """Connection proxy: everything works until COMMIT, which fails like a dying disk."""
    def __init__(self, real): self._r, self.armed = real, True
    def execute(self, sql, *a):
        if self.armed and sql.strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._r.execute(sql, *a)
    def __getattr__(self, n): return getattr(self._r, n)


# ============================ engine setup ============================ #
def test_database_is_wal_full_sync_and_versioned(sreg):
    c = sreg.store.conn
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 2            # FULL: fsync per commit
    assert c.execute("PRAGMA user_version").fetchone()[0] == S.USER_VERSION
    assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"meta", "kv", "rows"}
    assert sreg.store.integrity_ok()


def test_a_newer_schema_is_refused(db):
    con = sqlite3.connect(db); con.execute("PRAGMA user_version=99"); con.close()
    with pytest.raises(StoreError):
        Registry.load(db)


def test_json_paths_still_use_the_json_engine(tmp_path):
    reg = Registry.load(tmp_path / "state_registry.json")
    assert reg.store is None and (tmp_path / "state_registry.json").exists()


def test_a_new_state_cannot_overwrite_a_populated_database(db):
    Registry.load(db).close()
    with pytest.raises(StoreError):
        Registry(state=RegistryState(), path=db)


# ============================ dirty tracking ============================ #
def test_trackeddict_marks_every_access_path():
    d = TrackedDict({"a": 1, "b": 2, "c": 3, "d": 4})
    d.settle(list(d))
    assert d.collect() == set()
    _ = d["a"]; d.get("b"); d["x"] = 1; del d["c"]; d.pop("d"); d.setdefault("y", 0); d.update({"z": 1})
    assert d.collect() == {"a", "b", "x", "c", "d", "y", "z"}
    d.settle(d.collect()); d.clear()
    assert d.collect() == {"a", "b", "x", "y", "z"}
    assert "a" not in d and not d.__dict__["_touched"] - d.collect()
    list(TrackedDict({"k": 1}).values())                                   # iteration alone does not dirty anything
    t = TrackedDict({"k": 1}); t.settle(["k"]); list(t.values()); list(t.items()); "k" in t; len(t)
    assert t.collect() == set()


def test_stockitem_flags_itself_on_attribute_assignment():
    it = StockItem(addr="10.2.2.1", uid="u", kind="remnant", profile=PN, long_point_in=60, guaranteed_square_in=60)
    assert it._dirty is True
    d = TrackedDict({"10.2.2.1": it}); d.settle(["10.2.2.1"])
    assert it._dirty is False and d.collect() == set()
    it.allocated_job_id = "J"                                              # in-place mutation, no dict access at all
    assert d.collect() == {"10.2.2.1"}


# ============================ incremental persistence ============================ #
def test_every_collection_survives_a_cold_reload(db):
    reg = Registry.load(db)
    reg.add_raw(PN, 288, count=3)
    rem = reg.add_remnant(PN, 80, End.miter(45, Face.SPLINE))
    reg.lock(rem.addr, "J1"); reg.open_job("J1")
    reg.add_piece("J1", R.PieceRecord(seq=1, profile=PN, length_in=58, miter_deg=0, face="uniform", source_addr=rem.addr))
    reg.log_scrap(PN, 12, "DROP_UNDER_MIN", "J1"); reg.place_order(PN, 4)
    reg.enqueue_print(rem.addr, rem.uid, "BLUE", "AAAA"); reg.push_dlq(code="X")
    reg.save_intake("J1", {"job_id": "J1"}, {"is_ready_for_engineering": False})
    reg.purge_missing(reg.add_raw(PN, 100)[0].addr)
    reg.append_audit(user="u", action="TEST")
    with reg.transaction():
        reg.state.users["bob"] = {"role": "rep", "hash": "x"}
        reg.state.sessions["abc"] = {"user": "bob", "expires": 1.0}
    disk_equals_memory(reg)
    reg2 = Registry.load(db)
    assert reg2.state.model_dump() == reg.state.model_dump()
    assert reg2.state.stock[rem.addr].end_a == End.miter(45, Face.SPLINE) and reg2.state.stock[rem.addr].allocated_job_id == "J1"


def test_a_tiny_commit_writes_a_tiny_number_of_rows_even_with_lots_of_history(db):
    reg = Registry.load(db)
    reg.add_raw(PN, 288, count=40)
    with reg.transaction():                                                # a year of finished jobs
        for j in range(400):
            jid = f"OLD-{j}"
            reg.state.carts[jid] = R.JobCart(addr="10.4.1.1", job_id=jid, pieces=[
                R.PieceRecord(seq=j * 100 + k, profile=PN, length_in=58, miter_deg=0, face="uniform", source_addr="x") for k in range(30)])
            reg.state.intakes[jid] = {"ts": 1.0, "package": {"job_id": jid}, "report": {"ok": True}}
    addr = next(iter(reg.state.stock))
    writes = []
    real = reg.store.commit
    reg.store.commit = lambda r: writes.append(real(r)) or writes[-1]
    reg.lock(addr, "J-NEW")
    n = [w for w in writes if w][-1]
    assert 0 < n <= 6, f"one lock wrote {n} rows"                          # the item, journal entry, seq - not 400 carts
    t = []
    for i in range(10):
        t0 = time.perf_counter(); reg.lock(addr, f"J{i}"); t.append(time.perf_counter() - t0)
    assert sorted(t)[5] < 0.1                                              # and it is fast (JSON measured ~560 ms here)
    disk_equals_memory(reg)


def test_rollback_restores_memory_and_disk_for_every_collection(sreg, monkeypatch):
    reg = sreg
    reg.add_raw(PN, 288, count=2); rem = reg.add_remnant(PN, 80, End.square())
    reg.open_job("J"); reg.place_order(PN, 2); reg.enqueue_print(rem.addr, rem.uid, "T", "AA")
    with reg.transaction():
        reg.state.users["ann"] = {"role": "rep"}
    before = reg.state.model_dump()
    monkeypatch.setattr(R, "JOURNAL_CAP", len(reg.state.journal) + 1)       # make the failing txn also TRIM the journal
    with pytest.raises(RuntimeError):
        with reg.transaction():
            reg.add_raw(PN, 100, count=3)                                    # new rows
            reg.consume(rem.addr)                                            # deleted row
            reg.state.stock[next(iter(reg.state.stock))].allocated_job_id = "X"   # in-place edit
            reg.state.carts["J"].pieces.append(R.PieceRecord(seq=1, profile=PN, length_in=1, miter_deg=0, face="uniform", source_addr="a"))
            reg.state.intakes["Z"] = {"a": 1}
            reg.state.profiles[PN].rack_slots = 5
            reg.log_scrap(PN, 5, "MISCUT"); reg.push_dlq(code="Q"); reg.append_audit(user="x", action="Y")
            reg.state.print_queue[0]["status"] = "sent"; reg.state.on_order.clear()
            reg.state.users["ann"]["role"] = "admin"; reg.state.sessions["s"] = {"user": "ann"}
            reg.log("A"); reg.log("B"); reg.log("C")                         # trims the journal inside the txn
            raise RuntimeError("boom")
    assert reg.state.model_dump() == before
    disk_equals_memory(reg)
    reg.add_raw(PN, 50)                                                      # and the registry is fully usable afterwards
    disk_equals_memory(reg)


def test_commit_failure_leaves_memory_and_disk_untouched_then_recovers(sreg):
    reg = sreg
    reg.add_raw(PN, 288, count=2)
    before = reg.state.model_dump()
    real = reg.store.conn
    reg.store.conn = FailOnCommit(real)
    with pytest.raises(sqlite3.OperationalError):
        reg.add_raw(PN, 100, count=5)
    reg.store.conn = real
    assert reg.state.model_dump() == before
    assert read_state_readonly(reg.path).model_dump() == before
    reg.add_raw(PN, 100, count=1)
    disk_equals_memory(reg)


def test_after_commit_hooks_fire_only_after_the_sqlite_commit(sreg):
    reg = sreg
    seen = []
    with reg.transaction():
        reg.after_commit(lambda: seen.append(len(read_state_readonly(reg.path).stock)))
        reg.add_raw(PN, 240)
        assert seen == []
    assert seen == [1]                                                       # the new stick was already durable when the hook ran
    real = reg.store.conn; reg.store.conn = FailOnCommit(real)
    with pytest.raises(sqlite3.OperationalError):
        with reg.transaction():
            reg.after_commit(lambda: seen.append("GHOST"))
            reg.add_raw(PN, 240)
    reg.store.conn = real
    assert "GHOST" not in seen


def test_mutations_made_outside_a_transaction_are_flushed_by_the_next_one(sreg):
    reg = sreg
    reg.state.profiles[PN].rack_slots = 17                                   # tests and scripts do this
    reg.add_raw(PN, 288)
    assert read_state_readonly(reg.path).profiles[PN].rack_slots == 17
    reg.state.profiles[PN].rack_slots = 18
    with pytest.raises(RuntimeError):
        with reg.transaction():                                              # a later failure must not revert the earlier edit
            raise RuntimeError("x")
    assert reg.state.profiles[PN].rack_slots == 18


def test_append_only_lists_trim_and_persist_by_watermark(db, monkeypatch):
    monkeypatch.setattr(R, "JOURNAL_CAP", 50)
    reg = Registry.load(db)
    for i in range(130):
        with reg.transaction():
            reg.log("E", i=i)
    rows = reg.store.conn.execute("SELECT MIN(n), MAX(n), COUNT(*) FROM rows WHERE coll='journal'").fetchone()
    assert rows[2] == 50 and rows[1] - rows[0] == 49                        # exactly the last 50, contiguous
    disk_equals_memory(reg)
    reg2 = Registry.load(db)
    assert [j["i"] for j in reg2.state.journal[-3:]] == [127, 128, 129]
    with reg2.transaction():
        reg2.log("E", i=130)
    assert reg2.store.conn.execute("SELECT COUNT(*) FROM rows WHERE coll='journal'").fetchone()[0] == 50
    disk_equals_memory(reg2)


def test_an_unexpectedly_shrunk_list_is_rewritten_not_corrupted(sreg):
    reg = sreg
    for i in range(6):
        reg.push_dlq(code=f"C{i}")
    del reg.state.dlq[:2]
    reg.save()
    disk_equals_memory(reg)
    reg.push_dlq(code="C6")
    disk_equals_memory(reg)


# ============================ real crashes ============================ #
def test_process_killed_inside_the_commit_leaves_no_trace(db):
    Registry.load(db).close()
    code = f'''
import os
from extrusion_bus.registry import Registry
reg = Registry.load({str(db)!r})
class Bomb:
    def __init__(self, r): self.r = r
    def execute(self, sql, *a):
        if sql.strip().upper() == "COMMIT":
            os._exit(9)                      # power cut after the writes, before COMMIT
        return self.r.execute(sql, *a)
    def __getattr__(self, n): return getattr(self.r, n)
reg.store.conn = Bomb(reg.store.conn)
reg.add_raw("EXT-240-OPEN", 288, count=50)
'''
    assert run_py(code).returncode == 9
    reg = Registry.load(db)
    assert len(reg.state.stock) == 0 and reg.store.integrity_ok()
    disk_equals_memory(reg)
    reg.add_raw(PN, 288)                                                     # recovers and keeps working
    assert len(Registry.load(db).state.stock) == 1


def test_sigkill_right_after_an_acknowledged_commit_loses_nothing(db):
    Registry.load(db).close()
    code = f'''
import os, signal
from extrusion_bus.registry import Registry
reg = Registry.load({str(db)!r})
reg.add_raw("EXT-240-OPEN", 288, count=7)
with reg.transaction():
    reg.log("ACKED")
os.kill(os.getpid(), signal.SIGKILL)     # no clean shutdown, no checkpoint
'''
    assert run_py(code).returncode == -signal.SIGKILL
    reg = Registry.load(db)
    assert len(reg.state.stock) == 7 and reg.state.journal[-1]["kind"] == "ACKED" and reg.store.integrity_ok()


def test_a_second_process_cannot_open_the_database_for_writing(db):
    reg = Registry.load(db)
    r = run_py(f'''
from extrusion_bus.registry import Registry
from extrusion_bus.store import StoreError
try:
    Registry.load({str(db)!r})
except StoreError as e:
    print("REFUSED", e); raise SystemExit(3)
''')
    assert r.returncode == 3 and "single-writer" in r.stdout and "REFUSED" in r.stdout
    reg.close()                                                              # releasing the lock lets the next process in
    r2 = run_py(f'''
from extrusion_bus.registry import Registry
print("OPENED", len(Registry.load({str(db)!r}).state.stock))
''')
    assert r2.returncode == 0 and "OPENED" in r2.stdout


def test_readers_never_see_a_half_applied_transaction(db):
    reg = Registry.load(db)
    stop, bad = threading.Event(), []

    def reader():
        while not stop.is_set():
            n = len(read_state_readonly(db).stock)
            if n % 2:
                bad.append(n)

    t = threading.Thread(target=reader); t.start()
    try:
        for _ in range(25):
            with reg.transaction():
                reg.add_raw(PN, 100); time.sleep(0.002); reg.add_raw(PN, 100)   # two sticks, atomically
    finally:
        stop.set(); t.join()
    assert bad == [] and len(reg.state.stock) == 50


def test_concurrent_writers_never_lose_an_update(sreg):
    reg = sreg
    sticks = [i.addr for i in reg.add_raw(PN, 288, count=40)]

    def work(k):
        for i in range(25):
            with reg.transaction():
                reg.lock(sticks[k], f"J{k}-{i}")
                reg.log("W", k=k, i=i)

    ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert [reg.state.stock[sticks[k]].allocated_job_id for k in range(8)] == [f"J{k}-24" for k in range(8)]
    assert sum(1 for j in reg.state.journal if j["kind"] == "W") == 200
    disk_equals_memory(reg)


# ============================ the whole application on SQLite ============================ #
def app_on(db, **kw):
    reg = Registry.load(db)
    reg.add_raw(PN, 288, count=20); reg.add_raw("EXT-220-BOX", 288, count=20)
    reg.add_remnant(PN, 120, End.square()); reg.add_remnant("EXT-220-BOX", 100, End.square())
    return reg, create_app(reg, parser=RuleParser(reg), media_dir=db.parent / "media", **kw)


def test_a_full_shift_leaves_disk_identical_to_memory(db):
    mem = MemoryTransport()
    reg = Registry.load(db)
    svc = PrinterService(reg, mem, poll=0.05)
    reg.add_raw(PN, 288, count=20); reg.add_raw("EXT-220-BOX", 288, count=20)
    reg.add_remnant(PN, 120, End.square()); reg.add_remnant("EXT-220-BOX", 100, End.square())
    app = create_app(reg, parser=RuleParser(reg), media_dir=db.parent / "media", printer=svc)
    with TestClient(app) as c:
        make_ready(c, "A1"); make_ready(c, "A2", width_in="120", proj_in="96", house_h_in=108, front_h_in=96)
        for j in ("A1", "A2"):
            assert c.post("/api/engineering/release", json={"job_id": j}).status_code == 200
        plan = c.post("/api/engineering/batch-release", json={"job_ids": ["A1", "A2"]}).json()
        fid = plan["frame_id"]
        c.post(f"/order/{fid}/next")
        for _ in range(6):
            c.post(f"/order/{fid}/ack", json={"result": "CUT_OK"})
        c.post(f"/order/{fid}/ack", json={"result": "MISCUT_NAK", "actual_length_in": 40})
        c.post(f"/order/{fid}/flip")
        c.post(f"/order/{fid}/ack", json={"result": "PIECE_MISSING"})
        c.post("/api/saw/adopt-drop", json={"profile": PN, "tape_in": "74 1/4", "end_a": "miter_45_spline"})
        c.post("/api/procurement/order", json={"profile": PN, "qty": 3})
        deadline = time.time() + 3
        while len(mem.sent) < 3 and time.time() < deadline:
            time.sleep(0.02)
    assert reg.state.carts and reg.state.frames and reg.state.print_queue and reg.state.scrap_ledger
    disk_equals_memory(reg)                                                  # nothing the app did was missed by dirty tracking
    assert Registry.load(db).state.model_dump() == reg.state.model_dump()


def test_server_restart_on_sqlite_restores_the_frame_exactly(db):
    reg, app = app_on(db)
    f = app.state.bus.open_frame("R1", 'four 58" braces square')
    app.state.bus.stage_next_cut(f); app.state.bus.ack(f, "CUT_OK")
    cur = f.current.model_dump()
    reg.close()
    reg2 = Registry.load(db)
    app2 = create_app(reg2, parser=RuleParser(reg2), media_dir=db.parent / "media")
    f2 = app2.state.bus.frames[f.frame_id]
    assert f2.current.model_dump() == cur and len(f2.pending) == 3
    assert app2.state.recovery.restored[0]["frame_id"] == f.frame_id


def test_commit_that_fails_mid_ack_is_all_or_nothing_on_sqlite(db):
    reg, app = app_on(db)
    bus = app.state.bus
    f = bus.open_frame("F1", 'two 58" braces square'); bus.stage_next_cut(f)
    pre = (f.model_dump(), reg.state.model_dump(exclude={"journal", "seq"}))
    real = reg.store.conn; reg.store.conn = FailOnCommit(real)
    with pytest.raises(sqlite3.OperationalError):
        bus.ack(f, "CUT_OK")
    reg.store.conn = real
    assert f.model_dump() == pre[0] and reg.state.model_dump(exclude={"journal", "seq"}) == pre[1]
    assert not reg.state.carts["F1"].pieces
    bus.ack(f, "CUT_OK")
    assert len(reg.state.carts["F1"].pieces) == 1 and len(f.pending) == 1
    disk_equals_memory(reg)


def test_sticker_job_commits_with_its_remnant_or_not_at_all_on_sqlite(db):
    reg = Registry.load(db)
    bus = FabricationBus(reg, print_labels=True)
    woke = []
    bus.on_label_queued = lambda: woke.append(1)
    raw = reg.add_raw(PN, 288)[0]
    real = reg.store.conn; reg.store.conn = FailOnCommit(real)
    with pytest.raises(sqlite3.OperationalError):
        bus.commit(make_proposal(raw.addr, length_in=58))
    reg.store.conn = real
    assert reg.state.print_queue == [] and woke == [] and raw.addr in reg.state.stock
    bus.commit(make_proposal(raw.addr, length_in=58))
    assert len(reg.state.print_queue) == 1 and woke == [1]
    disk_equals_memory(reg)


def test_batch_release_is_atomic_on_sqlite(db):
    from test_phase5_batch import A, B
    reg = Registry.load(db)
    reg.add_raw(PN, 288, count=10)
    bus = FabricationBus(reg)
    fa, fb = bus.frame_from_specs("A", A), bus.frame_from_specs("B", B)
    bus.stage_next_cut(fa)
    pre = ({k: f.model_dump() for k, f in {"A": fa, "B": fb}.items()}, reg.state.model_dump(exclude={"journal", "seq"}))
    real = reg.store.conn; reg.store.conn = FailOnCommit(real)
    with pytest.raises(sqlite3.OperationalError):
        release_batch(bus, ["A", "B"])
    reg.store.conn = real
    assert {"A": fa.model_dump(), "B": fb.model_dump()} == pre[0] and reg.state.model_dump(exclude={"journal", "seq"}) == pre[1]
    assert set(bus.frames) == {fa.frame_id, fb.frame_id}
    release_batch(bus, ["A", "B"])
    disk_equals_memory(reg)


def test_create_app_picks_the_database_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTBUS_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("EXTBUS_STATE", str(tmp_path / "legacy.json"))
    monkeypatch.setenv("EXTBUS_MEDIA", str(tmp_path / "m"))
    app = create_app(parser=lambda s, f: "{}")
    assert isinstance(app.state.bus.reg.store, SqliteStore) and (tmp_path / "env.db").exists()
    assert not (tmp_path / "legacy.json").exists()                            # EXTBUS_DB wins; the JSON file is not touched
    app.state.bus.reg.close()
    monkeypatch.delenv("EXTBUS_DB")
    app2 = create_app(parser=lambda s, f: "{}")
    assert app2.state.bus.reg.store is None and (tmp_path / "legacy.json").exists()


# ============================ export / import ============================ #
def rich(db):
    reg = Registry.load(db)
    reg.add_raw(PN, 288, count=3); rem = reg.add_remnant(PN, 80, End.miter(45, Face.BACK))
    reg.open_job("J"); reg.log_scrap(PN, 9, "MISCUT", "J"); reg.save_intake("J", {"job_id": "J", "customer_name": "Pat"}, {"x": 1})
    reg.append_audit(user="admin", action="SEED")
    with reg.transaction():
        reg.state.users["bob"] = {"role": "operator", "hash": "h"}
        reg.state.sessions["tok"] = {"user": "bob", "expires": 9e9}
    return reg


def test_export_json_is_the_classic_state_document_without_sessions(db, tmp_path):
    reg = rich(db)
    out = tmp_path / "backup.json"
    assert export_json(db, out) > 100                                         # works while the server (reg) holds the lock
    doc = json.loads(out.read_text())
    assert set(doc) >= {"schema_version", "stock", "carts", "frames", "intakes", "journal", "print_queue", "users"}
    assert doc["sessions"] == {} and doc["users"]["bob"]["role"] == "operator"
    assert not (tmp_path / "backup.json.tmp").exists()
    loaded = RegistryState.model_validate_json(out.read_text())               # i.e. the JSON engine can still read it
    assert loaded.stock.keys() == reg.state.stock.keys()
    export_json(db, tmp_path / "b2.json", include_sessions=True)
    assert json.loads((tmp_path / "b2.json").read_text())["sessions"]                # opt-in only


def test_import_then_export_roundtrips_exactly(db, tmp_path):
    reg = rich(db)
    reg.state.sessions.clear(); reg.save()
    export_json(db, tmp_path / "a.json")
    reg.close()
    db2 = tmp_path / "new.db"
    imported = import_json(tmp_path / "a.json", db2)
    assert imported.state.model_dump() == RegistryState.model_validate_json((tmp_path / "a.json").read_text()).model_dump()
    imported.close()
    export_json(db2, tmp_path / "b.json")
    assert json.loads((tmp_path / "a.json").read_text()) == json.loads((tmp_path / "b.json").read_text())
    disk_equals_memory(Registry.load(db2))


def test_import_refuses_a_populated_database_unless_replace(db, tmp_path):
    rich(db).close()
    export_json(db, tmp_path / "a.json")
    with pytest.raises(StoreError):
        import_json(tmp_path / "a.json", db)
    reg = Registry.load(tmp_path / "other.db"); reg.add_raw(PN, 100, count=9); reg.close()
    r = import_json(tmp_path / "a.json", tmp_path / "other.db", replace=True)
    assert len(r.state.stock) == 4 and r.store.integrity_ok()                 # replaced, no leftovers from the 9 sticks


def test_the_json_engines_own_file_imports_into_sqlite_including_older_documents(tmp_path):
    j = Registry.load(tmp_path / "state_registry.json")
    j.add_raw(PN, 288, count=4); j.add_remnant(PN, 70, End.square()); j.open_job("J1")
    doc = json.loads((tmp_path / "state_registry.json").read_text())
    for newer in ("users", "sessions", "audit", "frames", "print_queue", "intakes"):    # an older release had none of these
        doc.pop(newer, None)
    (tmp_path / "old.json").write_text(json.dumps(doc))
    reg = import_json(tmp_path / "old.json", tmp_path / "m.db")
    assert len(reg.state.stock) == 5 and "J1" in reg.state.carts and reg.state.users == {}
    disk_equals_memory(reg)


def test_corrupt_json_import_fails_cleanly_and_falls_back_to_bak(tmp_path):
    bad = tmp_path / "x.json"; bad.write_text("{nope")
    with pytest.raises(StoreError):
        import_json(bad, tmp_path / "n.db")
    good = Registry.load(tmp_path / "g.json"); good.add_raw(PN, 288)
    (tmp_path / "g.json.bak").write_text((tmp_path / "g.json").read_text())
    (tmp_path / "g.json").write_text("{torn")
    assert len(import_json(tmp_path / "g.json", tmp_path / "n2.db").state.stock) == 1


def test_online_backup_is_a_consistent_openable_copy(sreg, tmp_path):
    sreg.add_raw(PN, 288, count=5)
    sreg.store.backup_to(tmp_path / "copy.db")
    copy = Registry.load(tmp_path / "copy.db")
    assert copy.state.model_dump() == sreg.state.model_dump() and copy.store.integrity_ok()


def test_cli_export_import_backup_and_check(db, tmp_path, capsys):
    from extrusion_bus import cli
    rich(db).close()
    assert cli.main(["export-json", "--db", str(db), "--out", str(tmp_path / "e.json")]) == 0
    assert cli.main(["import-json", "--json", str(tmp_path / "e.json"), "--db", str(tmp_path / "i.db")]) == 0
    assert cli.main(["import-json", "--json", str(tmp_path / "e.json"), "--db", str(tmp_path / "i.db")]) == 2          # refuses
    assert cli.main(["import-json", "--json", str(tmp_path / "e.json"), "--db", str(tmp_path / "i.db"), "--replace"]) == 0
    assert cli.main(["backup-db", "--db", str(db), "--out", str(tmp_path / "bk.db")]) == 0
    assert cli.main(["check-db", "--db", str(tmp_path / "bk.db")]) == 0
    out = capsys.readouterr().out
    assert "integrity ok" in out and "stock=4" in out
    assert cli.main(["export-json", "--db", str(tmp_path / "missing.db"), "--out", str(tmp_path / "z.json")]) == 2
