import pytest

from extrusion_bus.bus import FabricationBus
from extrusion_bus.gates import CutProposal
from extrusion_bus.llm import RuleParser
from extrusion_bus.physics import CutSpec
from extrusion_bus.registry import Registry

PN = "EXT-240-OPEN"  # 2x4 open screen, width 4.0", 1.05 lb/ft


@pytest.fixture
def reg(tmp_path):
    return Registry.load(tmp_path / "state_registry.json")


@pytest.fixture
def bus(reg):
    return FabricationBus(reg, parser=RuleParser(reg))


def make_proposal(addr, job="JOB-1", station="SAW_1", profile=PN, **spec):
    spec.setdefault("length_in", 58.0)
    return CutProposal(addr=addr, station=station, job_id=job, spec=CutSpec(profile=profile, **spec))
