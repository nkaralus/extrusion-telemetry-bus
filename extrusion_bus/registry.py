"""Virtual subnet allocator + persistent state registry (state_registry.json).

Every physical asset gets an IPv4 address inside 10.0.0.0/8:

    10.1.0.0/24  CONTROL  bus controller, local 3B model agent
    10.2.1.0/24  RAW      virgin 20'/24' sticks
    10.2.2.0/24  REMNANT  rack drops (guaranteed square >= 36")
    10.3.1.0/24  SAW      chop / miter saw stations
    10.4.1.0/24  CART     job carts (WIP kits)
    10.5.1.0/24  SCRAP    scrap hopper / recycling ledger

Writes are atomic: mutate in memory inside ``transaction()``, then
tmp-file + fsync + os.replace.  Any exception rolls memory back.
"""
from __future__ import annotations

import ipaddress
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .physics import KERF_IN, End, guaranteed_square

SUBNETS: Dict[str, str] = {
    "CONTROL": "10.1.0.0/24",
    "RAW": "10.2.1.0/24",
    "REMNANT": "10.2.2.0/24",
    "SAW": "10.3.1.0/24",
    "CART": "10.4.1.0/24",
    "SCRAP": "10.5.1.0/24",
}
_NETS = {k: ipaddress.ip_network(v) for k, v in SUBNETS.items()}
JOURNAL_CAP = 5000


class RegistryError(Exception):
    pass


class UnknownProfile(RegistryError):
    pass


class SubnetExhausted(RegistryError):
    pass


def subnet_of(addr: str) -> Optional[str]:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    for key, net in _NETS.items():
        if ip in net:
            return key
    return None


def in_cidr(addr: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(addr) in ipaddress.ip_network(cidr)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class Profile(BaseModel):
    part_number: str
    description: str = ""
    alloy: str = "6063-T6"
    width_in: float = Field(gt=0)
    depth_in: float = Field(gt=0)
    key_feature: Optional[str] = "spline_groove"
    lb_per_ft: float = Field(1.0, gt=0)
    bay_color: str = "BLUE"
    aliases: List[str] = Field(default_factory=list)
    cost_per_ft: float = Field(0.0, ge=0)        # virgin material cost, $/ft
    std_stick_in: float = Field(288.0, gt=0)     # standard purchase length (24')
    lead_time_days: float = Field(5.0, ge=0)     # vendor shipping latency
    min_stock_sticks: int = Field(0, ge=0)       # hard floor of virgin sticks on the rack
    rack_slots: int = Field(40, gt=0)            # remnant rack capacity for this profile


RACK_FULL_RATIO = 0.85


def default_profiles() -> List[Profile]:
    return [
        Profile(part_number="EXT-240-OPEN", description="2x4 Open Screen Extrusion",
                width_in=4.0, depth_in=2.0, lb_per_ft=1.05, bay_color="BLUE", cost_per_ft=4.10,
                aliases=["2x4", "2 x 4", "screen brace", "brace", "open screen"]),
        Profile(part_number="EXT-220-OPEN", description="2x2 Open Screen Extrusion",
                width_in=2.0, depth_in=2.0, lb_per_ft=0.62, bay_color="BLUE", cost_per_ft=2.35,
                aliases=["2x2", "2 x 2", "upright", "post"]),
        Profile(part_number="EXT-HW-BOX", description="Heavy-wall structural box tube",
                width_in=3.0, depth_in=3.0, key_feature=None, alloy="6061-T6",
                lb_per_ft=2.4, bay_color="GREEN", cost_per_ft=7.80, aliases=["box tube", "heavy wall"]),
    ]


class StockKind(str, Enum):
    RAW = "raw"
    REMNANT = "remnant"


class StockItem(BaseModel):
    addr: str
    uid: str
    kind: StockKind
    profile: str
    long_point_in: float
    guaranteed_square_in: float
    end_a: End = Field(default_factory=End.square)  # working end (the end the saw sees first)
    end_b: End = Field(default_factory=End.square)
    bin_location: Optional[str] = None
    allocated_job_id: Optional[str] = None
    legacy: bool = False  # adopted from the boneyard (lazy-load onboarding)


class Station(BaseModel):
    addr: str
    name: str
    kind: str = "chop_saw"
    reach: List[str] = Field(default_factory=lambda: [SUBNETS["RAW"], SUBNETS["REMNANT"]])


class PieceRecord(BaseModel):
    seq: int
    profile: str
    length_in: float
    miter_deg: float
    face: str
    source_addr: str
    status: str = "good"  # good | miscut


class JobCart(BaseModel):
    addr: str
    job_id: str
    pieces: List[PieceRecord] = Field(default_factory=list)


class ScrapEntry(BaseModel):
    seq: int
    hopper_addr: str
    profile: str
    length_in: float
    weight_lb: float
    reason: str  # DROP_UNDER_MIN | MISCUT | ...
    job_id: Optional[str] = None


class RegistryState(BaseModel):
    schema_version: int = 1
    seq: int = 0
    cursors: Dict[str, int] = Field(default_factory=dict)
    control: Dict[str, str] = Field(default_factory=dict)
    hopper_addr: str = ""
    profiles: Dict[str, Profile] = Field(default_factory=dict)
    stations: Dict[str, Station] = Field(default_factory=dict)
    stock: Dict[str, StockItem] = Field(default_factory=dict)
    carts: Dict[str, JobCart] = Field(default_factory=dict)
    scrap_ledger: List[ScrapEntry] = Field(default_factory=list)
    unaccounted: List[dict] = Field(default_factory=list)
    dlq: List[dict] = Field(default_factory=list)
    journal: List[dict] = Field(default_factory=list)
    raw_usage: List[dict] = Field(default_factory=list)   # virgin pulls: {ts, profile, length_in}
    on_order: List[dict] = Field(default_factory=list)    # open vendor orders


def rack_slot(profile: Profile, guaranteed_in: float) -> str:
    """Physical DNS: tape color + length bracket, e.g. 'BLUE-MEDIUM'."""
    if guaranteed_in < 60:
        size = "SHORT"
    elif guaranteed_in < 96:
        size = "MEDIUM"
    else:
        size = "LONG"
    return f"{profile.bay_color.upper()}-{size}"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class Registry:
    def __init__(
        self,
        state: Optional[RegistryState] = None,
        path: Optional[os.PathLike] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path: Optional[Path] = Path(path) if path else None
        self.clock = clock
        self._lock = threading.RLock()
        self._depth = 0
        self.state = state or RegistryState()
        if not self.state.control:
            self._bootstrap()

    # ---- lifecycle ------------------------------------------------------- #
    def _bootstrap(self) -> None:
        self.state.control = {
            "bus": self.allocate_addr("CONTROL"),
            "model_agent": self.allocate_addr("CONTROL"),
        }
        self.state.hopper_addr = self.allocate_addr("SCRAP")
        for p in default_profiles():
            self.state.profiles[p.part_number] = p
        self.add_station("SAW_1", kind="chop_saw")
        self.add_station("SAW_2", kind="miter_saw")

    @classmethod
    def load(cls, path: os.PathLike, clock: Callable[[], float] = time.time) -> "Registry":
        p = Path(path)
        if p.exists():
            return cls(RegistryState.model_validate_json(p.read_text()), path=p, clock=clock)
        reg = cls(path=p, clock=clock)
        reg.save()
        return reg

    def save(self) -> None:
        with self._lock:
            self._write()

    def _write(self) -> None:
        if self.path is None:
            return
        data = self.state.model_dump_json(indent=2)
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @contextmanager
    def transaction(self):
        """Atomic unit of work: all mutations land together or not at all."""
        with self._lock:
            if self._depth > 0:  # nested: outermost transaction owns commit/rollback
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            snapshot = self.state.model_copy(deep=True)
            self._depth = 1
            try:
                yield self
                self._write()
            except BaseException:
                self.state = snapshot
                raise
            finally:
                self._depth = 0

    # ---- bookkeeping ----------------------------------------------------- #
    def _next_seq(self) -> int:
        self.state.seq += 1
        return self.state.seq

    def log(self, kind: str, **data) -> None:
        self.state.journal.append({"seq": self._next_seq(), "ts": self.clock(), "kind": kind, **data})
        if len(self.state.journal) > JOURNAL_CAP:
            del self.state.journal[: len(self.state.journal) - JOURNAL_CAP]

    def _used_addrs(self) -> set:
        s = self.state
        used = set(s.stock) | {st.addr for st in s.stations.values()}
        used |= {c.addr for c in s.carts.values()} | set(s.control.values())
        if s.hopper_addr:
            used.add(s.hopper_addr)
        return used

    def allocate_addr(self, subnet_key: str) -> str:
        net = _NETS[subnet_key]
        hosts = [str(h) for h in net.hosts()]
        used = self._used_addrs()
        start = self.state.cursors.get(subnet_key, 0)
        for i in range(len(hosts)):
            idx = (start + i) % len(hosts)
            if hosts[idx] not in used:
                self.state.cursors[subnet_key] = (idx + 1) % len(hosts)
                return hosts[idx]
        raise SubnetExhausted(f"{SUBNETS[subnet_key]} is full")

    # ---- catalog / stations --------------------------------------------- #
    def add_profile(self, profile: Profile) -> None:
        with self.transaction():
            self.state.profiles[profile.part_number] = profile

    def profile(self, part_number: str) -> Profile:
        try:
            return self.state.profiles[part_number]
        except KeyError:
            raise UnknownProfile(part_number) from None

    def add_station(self, name: str, kind: str = "chop_saw", reach: Optional[List[str]] = None) -> Station:
        with self.transaction():
            st = Station(addr=self.allocate_addr("SAW"), name=name, kind=kind)
            if reach is not None:
                st.reach = reach
            self.state.stations[st.addr] = st
            return st

    def find_station(self, ref: str) -> Optional[Station]:
        if ref in self.state.stations:
            return self.state.stations[ref]
        for st in self.state.stations.values():
            if st.name == ref:
                return st
        return None

    # ---- stock ----------------------------------------------------------- #
    def stock_of(self, profile: str, kind: StockKind) -> List[StockItem]:
        return sorted(
            (i for i in self.state.stock.values() if i.profile == profile and i.kind == kind),
            key=lambda i: i.addr,
        )

    def add_raw(self, part_number: str, length_in: float, count: int = 1) -> List[StockItem]:
        with self.transaction():
            self.profile(part_number)
            out = []
            for _ in range(count):
                addr = self.allocate_addr("RAW")
                item = StockItem(
                    addr=addr,
                    uid=f"uid://inv/raw/{part_number}_{self._next_seq():06d}",
                    kind=StockKind.RAW,
                    profile=part_number,
                    long_point_in=length_in,
                    guaranteed_square_in=length_in,
                    bin_location="RAW-YARD",
                )
                self.state.stock[addr] = item
                self.log("RAW_RECEIVED", addr=addr, profile=part_number, length_in=length_in)
                out.append(item)
            return out

    def add_remnant(
        self,
        part_number: str,
        long_point_in: float,
        end_a: End,
        end_b: Optional[End] = None,
        origin: str = "cut",
        legacy: bool = False,
    ) -> StockItem:
        with self.transaction():
            prof = self.profile(part_number)
            end_b = end_b or End.square()
            gsq = guaranteed_square(long_point_in, end_a, end_b, prof.width_in, KERF_IN)
            addr = self.allocate_addr("REMNANT")
            item = StockItem(
                addr=addr,
                uid=f"uid://inv/remnant/{part_number}_{self._next_seq():06d}",
                kind=StockKind.REMNANT,
                profile=part_number,
                long_point_in=long_point_in,
                guaranteed_square_in=gsq,
                end_a=end_a,
                end_b=end_b,
                bin_location=rack_slot(prof, gsq),
                legacy=legacy,
            )
            self.state.stock[addr] = item
            self.log("REMNANT_RACKED", addr=addr, profile=part_number, long_in=long_point_in,
                     guaranteed_in=gsq, origin=origin)
            return item

    def rack_utilization(self, profile: str) -> Tuple[int, int, float]:
        """(drops racked, slots, ratio) for a profile's remnant rack."""
        cap = self.profile(profile).rack_slots
        n = len(self.stock_of(profile, StockKind.REMNANT))
        return n, cap, n / cap

    def place_order(self, profile: str, qty: int, eta_days: Optional[float] = None) -> dict:
        with self.transaction():
            prof = self.profile(profile)
            now = self.clock()
            eta = now + (prof.lead_time_days if eta_days is None else eta_days) * 86400
            order = {"id": self._next_seq(), "profile": profile, "qty": qty, "ordered_ts": now, "eta_ts": eta}
            self.state.on_order.append(order)
            self.log("ORDER_PLACED", profile=profile, qty=qty)
            return order

    def receive(self, profile: str, qty: int, length_in: Optional[float] = None) -> List[StockItem]:
        """Vendor delivery: register virgin sticks and retire the oldest open orders (FIFO)."""
        with self.transaction():
            items = self.add_raw(profile, length_in or self.profile(profile).std_stick_in, count=qty)
            left = qty
            for o in sorted((o for o in self.state.on_order if o["profile"] == profile),
                            key=lambda o: o["eta_ts"]):
                take = min(left, o["qty"])
                o["qty"] -= take
                left -= take
                if left == 0:
                    break
            self.state.on_order = [o for o in self.state.on_order if o["qty"] > 0]
            return items

    def lock(self, addr: str, job_id: str) -> None:
        with self.transaction():
            self.state.stock[addr].allocated_job_id = job_id
            self.log("LOCK", addr=addr, job_id=job_id)

    def unlock(self, addr: str) -> None:
        with self.transaction():
            if addr in self.state.stock:
                self.state.stock[addr].allocated_job_id = None
                self.log("UNLOCK", addr=addr)

    def consume(self, addr: str, reason: str = "CUT") -> StockItem:
        with self.transaction():
            item = self.state.stock.pop(addr)
            if item.kind == StockKind.RAW:
                self.state.raw_usage.append({"ts": self.clock(), "profile": item.profile,
                                             "length_in": item.long_point_in})
            self.log("CONSUMED", addr=addr, reason=reason)
            return item

    def purge_missing(self, addr: str, reported_by: str = "operator") -> Optional[StockItem]:
        """Ghost-drop bypass: remove from active registry, log as Unaccounted."""
        with self.transaction():
            item = self.state.stock.pop(addr, None)
            if item is not None:
                self.state.unaccounted.append(
                    {"seq": self._next_seq(), "addr": addr, "profile": item.profile,
                     "long_point_in": item.long_point_in, "reported_by": reported_by}
                )
                self.log("UNACCOUNTED", addr=addr)
            return item

    # ---- carts / scrap / dlq -------------------------------------------- #
    def open_job(self, job_id: str) -> JobCart:
        with self.transaction():
            cart = self.state.carts.get(job_id)
            if cart is None:
                cart = JobCart(addr=self.allocate_addr("CART"), job_id=job_id)
                self.state.carts[job_id] = cart
                self.log("JOB_OPEN", job_id=job_id, addr=cart.addr)
            return cart

    def add_piece(self, job_id: str, rec: PieceRecord) -> None:
        with self.transaction():
            self.open_job(job_id)
            self.state.carts[job_id].pieces.append(rec)

    def next_piece_seq(self) -> int:
        return self._next_seq()

    def log_scrap(self, profile: str, length_in: float, reason: str, job_id: Optional[str] = None) -> ScrapEntry:
        with self.transaction():
            prof = self.profile(profile)
            entry = ScrapEntry(
                seq=self._next_seq(),
                hopper_addr=self.state.hopper_addr,
                profile=profile,
                length_in=length_in,
                weight_lb=round(prof.lb_per_ft * length_in / 12.0, 3),
                reason=reason,
                job_id=job_id,
            )
            self.state.scrap_ledger.append(entry)
            self.log("SCRAP", profile=profile, length_in=length_in, reason=reason)
            return entry

    def scrap_total_lb(self) -> float:
        return round(sum(e.weight_lb for e in self.state.scrap_ledger), 3)

    def push_dlq(self, **entry) -> None:
        with self.transaction():
            self.state.dlq.append({"seq": self._next_seq(), **entry})
