import os
import time
from pathlib import Path

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


@pytest.fixture(autouse=True)
def _test_backend(monkeypatch):
    """EXTBUS_TEST_BACKEND=sqlite runs the WHOLE suite against the SQLite engine (.json paths become .db).
    Legacy suites predate authentication, so they run with it off; test_phase6_auth turns it on explicitly."""
    monkeypatch.setenv("EXTBUS_AUTH", "off")
    if os.environ.get("EXTBUS_TEST_BACKEND") == "sqlite":
        orig = Registry.load.__func__

        def load(cls, path, clock=time.time):
            p = Path(path)
            return orig(cls, p.with_suffix(".db") if p.suffix == ".json" else p, clock)

        monkeypatch.setattr(Registry, "load", classmethod(load))


# These tests inject failures into the JSON engine itself (patching os.replace, reading/corrupting the
# JSON file, the .bak fallback).  Their SQLite counterparts live in test_phase6_sqlite.py.
JSON_ENGINE_ONLY = {
    "test_transaction_rolls_back_memory_and_file", "test_failed_atomic_write_leaves_old_file_and_rolls_back",
    "test_corrupt_main_file_falls_back_to_the_previous_version", "test_writes_fsync_the_file_and_its_directory",
    "test_after_commit_hooks_run_only_after_a_durable_commit", "test_crash_during_the_commit_write_is_all_or_nothing",
    "test_release_is_atomic_when_the_write_fails", "test_job_commits_with_the_remnant_or_not_at_all",
    # Phase 6 tests that exercise the JSON engine on purpose (the harness would turn their .json into .db)
    "test_json_paths_still_use_the_json_engine", "test_create_app_picks_the_database_from_the_environment",
    "test_the_json_engines_own_file_imports_into_sqlite_including_older_documents",
    "test_corrupt_json_import_fails_cleanly_and_falls_back_to_bak",
}


def pytest_collection_modifyitems(config, items):
    if os.environ.get("EXTBUS_TEST_BACKEND") == "sqlite":
        skip = pytest.mark.skip(reason="JSON-engine failure injection; SQLite counterpart in test_phase6_sqlite.py")
        for item in items:
            if item.name in JSON_ENGINE_ONLY:
                item.add_marker(skip)
