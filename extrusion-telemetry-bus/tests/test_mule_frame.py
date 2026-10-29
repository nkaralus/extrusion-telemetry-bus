import json

import pytest
from pydantic import ValidationError

from extrusion_bus.mule_frame import Frame, FramePhase, Slot2Plan, SlotId, SlotOrderError

GOOD = {"op": "allocate_cuts", "cuts": [{"profile": "EXT-240-OPEN", "qty": 3, "length_in": 58.0,
                                           "face": "spline_out", "miter_deg": 45}]}


def test_slot2_plan_accepts_strict_json_and_expands():
    plan = Slot2Plan.model_validate_json(json.dumps(GOOD))
    pieces = plan.expand()
    assert len(pieces) == 3 and pieces[0].miter_deg == 45 and pieces[0].face.value == "spline_face"


def test_slot2_plan_rejects_everything_off_grammar():
    bad = [
        {**GOOD, "op": "delete_everything"},
        {**GOOD, "note": "free-form chatter"},
        {"op": "allocate_cuts", "cuts": []},
        {"op": "allocate_cuts", "cuts": [{**GOOD["cuts"][0], "qty": 0}]},
        {"op": "allocate_cuts", "cuts": [{**GOOD["cuts"][0], "length_in": -4}]},
        {"op": "allocate_cuts", "cuts": [{**GOOD["cuts"][0], "colour": "red"}]},
        {"op": "allocate_cuts", "cuts": [{**GOOD["cuts"][0], "miter_deg": 45, "face": "uniform"}]},
    ]
    for payload in bad:
        with pytest.raises(ValidationError):
            Slot2Plan.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError):
        Slot2Plan.model_validate_json("not json at all")


def test_slot_clock_enforces_tdma_order():
    f = Frame(job_id="J")
    for s in (SlotId.SLOT_1_TASK, SlotId.SLOT_2_PLAN, SlotId.SLOT_3_EXECUTE, SlotId.SLOT_4_GUARD,
              SlotId.SLOT_5_VERIFY, SlotId.SLOT_3_EXECUTE):  # V wraps to E for the next piece
        f.advance(s)
    assert f.slot == SlotId.SLOT_3_EXECUTE


def test_slot_clock_rejects_skips_and_allows_guard_retry():
    f = Frame(job_id="J")
    with pytest.raises(SlotOrderError):
        f.advance(SlotId.SLOT_3_EXECUTE)           # cannot start at SLOT_3
    f.advance(SlotId.SLOT_1_TASK)
    with pytest.raises(SlotOrderError):
        f.advance(SlotId.SLOT_4_GUARD)             # cannot skip SLOT_2/3
    f.advance(SlotId.SLOT_2_PLAN); f.advance(SlotId.SLOT_3_EXECUTE); f.advance(SlotId.SLOT_4_GUARD)
    f.advance(SlotId.SLOT_3_EXECUTE)               # gate rejection -> re-source
    f.advance(SlotId.SLOT_4_GUARD)
    with pytest.raises(SlotOrderError):
        f.advance(SlotId.SLOT_2_PLAN)


def test_dlq_freezes_the_frame():
    f = Frame(job_id="J")
    f.advance(SlotId.SLOT_1_TASK)
    dl = f.trip_dlq(SlotId.SLOT_2_PLAN, "PLAN_NAK_LIMIT", "x")
    assert f.phase == FramePhase.DLQ and dl.frame_id == f.frame_id
    with pytest.raises(SlotOrderError):
        f.advance(SlotId.SLOT_2_PLAN)
