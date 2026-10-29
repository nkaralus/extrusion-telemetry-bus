"""SQLite (WAL) persistence for the registry: incremental, ACID, single-writer.

The registry keeps its in-memory working state (fast reads, the same API every module already uses).
What changes is how it reaches disk.  Instead of serializing and fsyncing the WHOLE state on every
commit, ``SqliteStore.commit`` writes only the rows that changed, inside one SQLite transaction
(``BEGIN IMMEDIATE`` ... ``COMMIT``, WAL, ``synchronous=FULL``), so a commit costs the same whether
the shop has ten finished jobs or ten thousand.

How changes are found (and why a miss cannot corrupt data silently)
    * dict collections (stock, carts, intakes, frames, profiles, stations, users, sessions) are
      ``TrackedDict`` objects.  Any ``[]``/``get``/``pop``/``setitem``/``delitem`` marks the key
      "touched" (reads count: a caller may mutate what it got).  ``StockItem`` also flags itself on
      attribute assignment.  At commit each touched key is compared with the stored row and written
      only if it differs.
    * append-only lists (journal, scrap_ledger, raw_usage, unaccounted, dlq, audit) are written by
      watermark: only elements past the last persisted index.  If a list ever shrinks unexpectedly
      the whole collection is rewritten (safe fallback).
    * small mutable lists (print_queue, on_order) and the scalar meta are compared whole.
Rollback is the inverse: touched rows are re-read from the database, so memory returns to exactly
the last COMMITTED state (not a deep copy of the world, as the JSON engine does).

Single writer: one server process owns a database (``<db>.lock``, POSIX ``lockf``).  Other processes
(``extbus export-json``, backups) may READ safely at any time: WAL readers never block the writer.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from pydantic import BaseModel

try:  # POSIX only; on Windows the single-writer guard is skipped
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
USER_VERSION = 1

DICT_COLLS = ("profiles", "stations", "stock", "carts", "intakes", "frames", "users", "sessions")
APPEND_COLLS = ("journal", "scrap_ledger", "raw_usage", "unaccounted", "dlq", "audit")
SMALL_COLLS = ("print_queue", "on_order")
SCALARS = ("schema_version", "seq", "hopper_addr", "control", "cursors")


def is_sqlite_path(path: os.PathLike) -> bool:
    return str(path).lower().endswith(SQLITE_SUFFIXES)


class StoreError(RuntimeError):
    pass


class TrackedDict(dict):
    """dict that remembers which keys were touched since the last commit/rollback."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._touched: Set[str] = set()

    def _t(self, key) -> None:
        t = self.__dict__.get("_touched")
        if t is not None:
            t.add(key)

    def __getitem__(self, key):
        self._t(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._t(key)
        return super().get(key, default)

    def __setitem__(self, key, value) -> None:
        self._t(key)
        super().__setitem__(key, value)

    def __delitem__(self, key) -> None:
        self._t(key)
        super().__delitem__(key)

    def pop(self, key, *default):
        self._t(key)
        return super().pop(key, *default)

    def setdefault(self, key, default=None):
        self._t(key)
        return super().setdefault(key, default)

    def update(self, *args, **kwargs) -> None:
        for k in dict(*args, **kwargs):
            self._t(k)
        super().update(*args, **kwargs)

    def clear(self) -> None:
        for k in list(self):
            self._t(k)
        super().clear()

    def popitem(self):
        k, v = super().popitem()
        self._t(k)
        return k, v

    # -- store interface (bypasses tracking) --
    def raw_get(self, key):
        return dict.get(self, key)

    def collect(self) -> Set[str]:
        keys = set(self.__dict__.get("_touched", ()))
        for k, v in dict.items(self):
            if getattr(v, "_dirty", False):
                keys.add(k)
        return keys

    def settle(self, keys: Iterable[str]) -> None:
        self._touched.clear()
        for k in keys:
            v = dict.get(self, k)
            if v is not None and hasattr(v, "_dirty"):
                v._dirty = False

    def mark_all(self) -> None:
        self._touched.update(dict.keys(self))


def _ser(v) -> str:
    if isinstance(v, BaseModel):
        return v.model_dump_json()
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _types():
    from .registry import JobCart, Profile, ScrapEntry, Station, StockItem
    return {"profiles": Profile, "stations": Station, "stock": StockItem, "carts": JobCart}, {"scrap_ledger": ScrapEntry}


class SqliteStore:
    def __init__(self, path: os.PathLike, *, take_lock: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lockfd: Optional[int] = None
        if take_lock:
            self._acquire_lock()
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=10.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")        # fsync every commit: a power cut never loses an acknowledged cut
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._init_schema()
        self._base: Dict[str, int] = {}     # absolute index of in-memory element 0
        self._cbase: Dict[str, int] = {}    # same, as last committed
        self._end: Dict[str, int] = {}      # absolute index one past the last persisted element
        self._small: Dict[str, str] = {}
        self._meta: Dict[str, str] = {}
        self._closed = False

    # ---- lifecycle -------------------------------------------------------- #
    def _acquire_lock(self) -> None:
        if fcntl is None:
            return
        fd = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise StoreError(f"{self.path} is already open in another process (single-writer database)") from None
        self._lockfd = fd

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()
        if self._lockfd is not None:
            os.close(self._lockfd)
            self._lockfd = None

    def _init_schema(self) -> None:
        ver = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if ver > USER_VERSION:
            raise StoreError(f"database schema v{ver} is newer than this build (v{USER_VERSION})")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS kv   (coll TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                                             PRIMARY KEY (coll, key)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS rows (coll TEXT NOT NULL, n INTEGER NOT NULL, value TEXT NOT NULL,
                                             PRIMARY KEY (coll, n)) WITHOUT ROWID;
        """)
        if ver == 0:
            self.conn.execute(f"PRAGMA user_version={USER_VERSION}")

    # ---- load ------------------------------------------------------------- #
    def has_data(self) -> bool:
        return self.conn.execute("SELECT 1 FROM meta WHERE key='control'").fetchone() is not None

    def load_state(self):
        """Full state from disk, or None for an empty database."""
        from .registry import RegistryState
        if not self.has_data():
            return None
        dict_types, list_types = _types()
        data: dict = {}
        for k, v in self.conn.execute("SELECT key, value FROM meta"):
            data[k] = json.loads(v)
        for coll in DICT_COLLS:
            rows = self.conn.execute("SELECT key, value FROM kv WHERE coll=?", (coll,)).fetchall()
            data[coll] = {k: json.loads(v) for k, v in rows}
        for coll in APPEND_COLLS + SMALL_COLLS:
            data[coll] = [json.loads(v) for (v,) in self.conn.execute("SELECT value FROM rows WHERE coll=? ORDER BY n", (coll,))]
        return RegistryState.model_validate(data)

    # ---- attach ------------------------------------------------------------ #
    def attach(self, state, *, loaded: bool) -> None:
        """Wrap the state's dict collections in TrackedDict and prime watermarks."""
        for coll in DICT_COLLS:
            td = TrackedDict(getattr(state, coll))
            if loaded:
                td.settle(list(dict.keys(td)))
            else:
                td.mark_all()
            setattr(state, coll, td)
        for coll in APPEND_COLLS:
            lst = getattr(state, coll)
            if loaded:
                lo, hi = self.conn.execute("SELECT MIN(n), MAX(n) FROM rows WHERE coll=?", (coll,)).fetchone()
                base, end = (lo, hi + 1) if lo is not None else (0, 0)
                if end - base != len(lst):               # defensive: inconsistent rows -> rewrite on first commit
                    base, end = 0, 0
            else:
                base = end = 0
            self._base[coll] = self._cbase[coll] = base
            self._end[coll] = end
        if loaded:
            for coll in SMALL_COLLS:
                self._small[coll] = _ser(getattr(state, coll))
            for name in SCALARS:
                self._meta[name] = _ser(getattr(state, name))

    def note_trim(self, coll: str, k: int) -> None:
        """The registry removed the first ``k`` elements of an append-only list (journal cap)."""
        self._base[coll] = self._base.get(coll, 0) + k

    # ---- commit ------------------------------------------------------------ #
    def commit(self, reg) -> int:
        """Persist everything that changed.  Returns the number of rows written/deleted."""
        st = reg.state
        c = self.conn
        puts: List[tuple] = []      # (sql, params)
        touched: Dict[str, Set[str]] = {}
        for coll in DICT_COLLS:
            d: TrackedDict = getattr(st, coll)
            keys = d.collect()
            touched[coll] = keys
            for key in keys:
                present = key in d
                new = _ser(d.raw_get(key)) if present else None
                row = c.execute("SELECT value FROM kv WHERE coll=? AND key=?", (coll, key)).fetchone()
                old = row[0] if row else None
                if new is None and old is not None:
                    puts.append(("DELETE FROM kv WHERE coll=? AND key=?", (coll, key)))
                elif new is not None and new != old:
                    puts.append(("INSERT OR REPLACE INTO kv(coll,key,value) VALUES (?,?,?)", (coll, key, new)))
        new_ends: Dict[str, tuple] = {}
        for coll in APPEND_COLLS:
            lst = getattr(st, coll)
            base, end = self._base[coll], self._end[coll]
            have = base + len(lst)
            if have < end:                                   # shrank unexpectedly: rewrite the whole collection
                puts.append(("DELETE FROM rows WHERE coll=?", (coll,)))
                puts += [("INSERT INTO rows(coll,n,value) VALUES (?,?,?)", (coll, i, _ser(v))) for i, v in enumerate(lst)]
                new_ends[coll] = (0, len(lst))
                continue
            if base > self._cbase[coll]:
                puts.append(("DELETE FROM rows WHERE coll=? AND n<?", (coll, base)))
            puts += [("INSERT INTO rows(coll,n,value) VALUES (?,?,?)", (coll, base + i, _ser(lst[i])))
                     for i in range(end - base, len(lst))]
            new_ends[coll] = (base, have)
        small_new: Dict[str, str] = {}
        for coll in SMALL_COLLS:
            s = _ser(getattr(st, coll))
            if s != self._small.get(coll):
                small_new[coll] = s
                puts.append(("DELETE FROM rows WHERE coll=?", (coll,)))
                puts += [("INSERT INTO rows(coll,n,value) VALUES (?,?,?)", (coll, i, _ser(v)))
                         for i, v in enumerate(getattr(st, coll))]
        meta_new: Dict[str, str] = {}
        for name in SCALARS:
            s = _ser(getattr(st, name))
            if s != self._meta.get(name):
                meta_new[name] = s
                puts.append(("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (name, s)))
        if not puts:
            for coll, keys in touched.items():
                getattr(st, coll).settle(keys)
            return 0
        c.execute("BEGIN IMMEDIATE")
        try:
            for sql, params in puts:
                c.execute(sql, params)
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        for coll, keys in touched.items():
            getattr(st, coll).settle(keys)
        for coll, (b, e) in new_ends.items():
            self._base[coll] = self._cbase[coll] = b
            self._end[coll] = e
        self._small.update(small_new)
        self._meta.update(meta_new)
        return len(puts)

    # ---- rollback ----------------------------------------------------------- #
    def rollback(self, reg) -> None:
        """Return memory to the last committed state by re-reading what the failed transaction touched."""
        st = reg.state
        dict_types, list_types = _types()
        for coll in DICT_COLLS:
            d: TrackedDict = getattr(st, coll)
            keys = d.collect()
            cls = dict_types.get(coll)
            for key in keys:
                row = self.conn.execute("SELECT value FROM kv WHERE coll=? AND key=?", (coll, key)).fetchone()
                if row is None:
                    dict.pop(d, key, None)
                else:
                    dict.__setitem__(d, key, cls.model_validate_json(row[0]) if cls else json.loads(row[0]))
            d.settle(keys)
            for k in keys:                       # freshly loaded items are clean
                v = dict.get(d, k)
                if v is not None and hasattr(v, "_dirty"):
                    v._dirty = False
        for coll in APPEND_COLLS:
            lst = getattr(st, coll)
            cbase, end = self._cbase[coll], self._end[coll]
            expected = end - cbase
            if self._base[coll] == cbase and len(lst) >= expected:
                del lst[expected:]
            else:                                  # trimmed during the failed txn: reload from disk
                cls = list_types.get(coll)
                lst[:] = [cls.model_validate_json(v) if cls else json.loads(v) for (v,) in
                          self.conn.execute("SELECT value FROM rows WHERE coll=? AND n>=? ORDER BY n", (coll, cbase))]
            self._base[coll] = cbase
        for coll in SMALL_COLLS:
            cur = _ser(getattr(st, coll))
            if cur != self._small.get(coll):
                getattr(st, coll)[:] = [json.loads(v) for (v,) in
                                        self.conn.execute("SELECT value FROM rows WHERE coll=? ORDER BY n", (coll,))]
        for name in SCALARS:
            cur = _ser(getattr(st, name))
            if cur != self._meta.get(name):
                row = self.conn.execute("SELECT value FROM meta WHERE key=?", (name,)).fetchone()
                if row is not None:
                    val = json.loads(row[0])
                    if name == "cursors":
                        getattr(st, name).clear()
                        getattr(st, name).update(val)
                    elif name == "control":
                        getattr(st, name).clear()
                        getattr(st, name).update(val)
                    else:
                        setattr(st, name, val)

    # ---- maintenance ---------------------------------------------------------- #
    def backup_to(self, dest: os.PathLike) -> None:
        """Online, consistent copy of the database (safe while the server runs)."""
        out = sqlite3.connect(str(dest))
        try:
            self.conn.backup(out)
        finally:
            out.close()

    def integrity_ok(self) -> bool:
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --------------------------------------------------------------------------- #
# Offline helpers (CLI)
# --------------------------------------------------------------------------- #
def read_state_readonly(db_path: os.PathLike):
    """Load the full state without taking the writer lock (safe while a server is running)."""
    p = Path(db_path)
    if not p.exists():
        raise StoreError(f"no database at {p}")
    store = SqliteStore.__new__(SqliteStore)
    store.path = p
    store._lockfd = None
    store._closed = False
    store.conn = sqlite3.connect(str(p), isolation_level=None, timeout=10.0)
    store.conn.execute("PRAGMA query_only=ON")     # reads only; no writer lock, WAL readers never block the server
    try:
        state = store.load_state()
    finally:
        store.conn.close()
        store._closed = True
    if state is None:
        raise StoreError(f"{p} is empty")
    return state


def export_json(db_path: os.PathLike, out_path: os.PathLike, *, include_sessions: bool = False) -> int:
    """Portable backup: the same JSON document the pre-SQLite registry used (``state_registry.json``)."""
    state = read_state_readonly(db_path)
    if not include_sessions:
        state.sessions = {}                              # login sessions are ephemeral; do not leak token hashes
    out = Path(out_path)
    tmp = out.with_name(out.name + ".tmp")
    data = state.model_dump_json(indent=2)
    with open(tmp, "w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out)
    return len(data)


def import_json(json_path: os.PathLike, db_path: os.PathLike, *, replace: bool = False):
    """Load a ``state_registry.json`` (or an export) into a SQLite database.  Refuses a non-empty DB unless ``replace``."""
    from .registry import Registry, RegistryState
    src = Path(json_path)
    try:
        state = RegistryState.model_validate_json(src.read_text())
    except Exception as exc:
        bak = src.with_name(src.name + ".bak")
        if not bak.exists():
            raise StoreError(f"cannot read {src}: {exc}") from exc
        state = RegistryState.model_validate_json(bak.read_text())
    store = SqliteStore(db_path)
    try:
        if store.has_data() and not replace:
            raise StoreError(f"{db_path} already holds data (use --replace to overwrite)")
        if store.has_data():
            store.conn.execute("DELETE FROM meta"); store.conn.execute("DELETE FROM kv"); store.conn.execute("DELETE FROM rows")
    except BaseException:
        store.close()
        raise
    reg = Registry(state=state, path=db_path, store=store)
    reg.save()
    return reg


def check_db(db_path: os.PathLike) -> dict:
    """Integrity + row counts, read-only (safe while the server runs)."""
    state = read_state_readonly(db_path)
    con = sqlite3.connect(str(db_path), isolation_level=None, timeout=10.0)
    try:
        con.execute("PRAGMA query_only=ON")
        ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()
    counts = {c: len(getattr(state, c)) for c in DICT_COLLS + APPEND_COLLS + SMALL_COLLS}
    return {"integrity_ok": ok, "journal_mode": mode, "counts": counts}
