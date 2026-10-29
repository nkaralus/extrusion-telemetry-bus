import os

import pytest

from extrusion_bus import registry as R
from extrusion_bus.physics import End, Face, guaranteed_square
from extrusion_bus.registry import Registry, StockKind, SubnetExhausted, UnknownProfile, rack_slot, subnet_of

PN = "EXT-240-OPEN"


def test_bootstrap_places_assets_in_their_subnets(reg):
    s = reg.state
    assert all(subnet_of(a) == "CONTROL" for a in s.control.values())
    assert subnet_of(s.hopper_addr) == "SCRAP"
    assert {subnet_of(a) for a in s.stations} == {"SAW"}


def test_allocations_land_in_correct_subnets_and_are_unique(reg):
    raws = reg.add_raw(PN, 288, count=3)
    rem = reg.add_remnant(PN, 74.25, End.miter(45, Face.SPLINE))
    cart = reg.open_job("J1")
    assert all(subnet_of(r.addr) == "RAW" for r in raws)
    assert subnet_of(rem.addr) == "REMNANT" and rem.kind == StockKind.REMNANT
    assert subnet_of(cart.addr) == "CART"
    assert len({r.addr for r in raws} | {rem.addr}) == 4
    assert reg.open_job("J1").addr == cart.addr  # idempotent


def test_remnant_guaranteed_square_is_computed_from_physics(reg):
    rem = reg.add_remnant(PN, 74.25, End.miter(45, Face.SPLINE))
    assert rem.guaranteed_square_in == guaranteed_square(74.25, rem.end_a, rem.end_b, 4.0) == 70.125


def test_rack_slot_brackets_and_color_bays(reg):
    blue, green = reg.profile(PN), reg.profile("EXT-HW-BOX")
    assert rack_slot(blue, 40) == "BLUE-SHORT"
    assert rack_slot(blue, 70) == "BLUE-MEDIUM"
    assert rack_slot(blue, 100) == "BLUE-LONG"
    assert rack_slot(green, 70) == "GREEN-MEDIUM"


def test_persistence_roundtrip(reg, tmp_path):
    reg.add_raw(PN, 288, count=2)
    reg.add_remnant(PN, 60, End.square())
    reg.open_job("J1")
    reg.log_scrap(PN, 12, "DROP_UNDER_MIN", "J1")
    again = Registry.load(tmp_path / "state_registry.json")
    assert again.state.model_dump() == reg.state.model_dump()
    assert again.add_raw(PN, 240)[0].addr not in {i for i in reg.state.stock}  # cursor persisted


def test_transaction_rolls_back_memory_and_file(reg, tmp_path):
    path = tmp_path / "state_registry.json"
    reg.add_raw(PN, 288)
    before_disk, before_mem = path.read_text(), reg.state.model_dump()
    with pytest.raises(RuntimeError):
        with reg.transaction():
            reg.add_raw(PN, 288, count=5)
            raise RuntimeError("boom")
    assert reg.state.model_dump() == before_mem
    assert path.read_text() == before_disk


def test_failed_atomic_write_leaves_old_file_and_rolls_back(reg, tmp_path, monkeypatch):
    path = tmp_path / "state_registry.json"
    reg.add_raw(PN, 288)
    before_disk, before_mem = path.read_text(), reg.state.model_dump()

    def explode(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(R.os, "replace", explode)
    with pytest.raises(OSError):
        reg.add_raw(PN, 288)
    monkeypatch.undo()
    assert path.read_text() == before_disk
    assert reg.state.model_dump() == before_mem
    assert not [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]  # no tmp litter


def test_subnet_exhaustion_is_atomic(reg):
    with pytest.raises(SubnetExhausted):
        reg.add_raw(PN, 240, count=255)         # /24 has 254 hosts
    assert not reg.state.stock                   # nothing half-applied
    assert len(reg.add_raw(PN, 240, count=254)) == 254


def test_addresses_are_not_reissued_while_live(reg):
    a, b, c = reg.add_raw(PN, 288, count=3)
    reg.consume(b.addr)
    d = reg.add_raw(PN, 288)[0]
    assert d.addr not in {a.addr, c.addr}


def test_scrap_weight_and_total(reg):
    e = reg.log_scrap(PN, 12, "DROP_UNDER_MIN", "J1")
    assert e.weight_lb == 1.05 and e.hopper_addr == reg.state.hopper_addr
    reg.log_scrap(PN, 24, "MISCUT", "J1")
    assert reg.scrap_total_lb() == 3.15


def test_purge_missing_logs_unaccounted(reg):
    raw = reg.add_raw(PN, 288)[0]
    assert reg.purge_missing(raw.addr).addr == raw.addr
    assert raw.addr not in reg.state.stock
    assert reg.state.unaccounted[0]["addr"] == raw.addr
    assert reg.purge_missing(raw.addr) is None


def test_unknown_profile(reg):
    with pytest.raises(UnknownProfile):
        reg.add_raw("EXT-NOPE", 288)
