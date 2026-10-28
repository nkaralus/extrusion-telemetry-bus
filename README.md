# extrusion-telemetry-bus

Local-first fabrication & remnant inventory bus for aluminum extrusion shops.
Sales quote -> BOM nesting -> saw table -> scrap ledger, as one deterministic closed loop.
Ports the architectures of `engine-telemetry-bus` (virtual subnets + 3-gate firewall) and
`mule-protocol` (TDMA clock + schema-constrained 3B model) to physical aluminum.

No cloud. No subscription. FastAPI + `state_registry.json` on a Pi / office PC / shop LAN.

## Layout

| Module | Role |
|---|---|
| `physics.py` | Miter loss `dL = W*tan(theta)`, kerf, guaranteed square, 1/16" shop rounding, anisotropic face tracking, `plan_cut` |
| `registry.py` | 10.x subnet allocator, persistent registry, atomic save/load, rollback transactions |
| `gates.py` | `GI_1` identity/allocation, `GI_2` subnet + face alignment, `GI_3` bounds + kerf -> `GateVerdict(ok, gate, code, message)` |
| `mule_frame.py` | 5-slot TDMA frame: strict Pydantic slot schemas, enforced slot order, DLQ |
| `bus.py` | Orchestrator: drops-before-raw sourcing, atomic commit, NAK loops |
| `llm.py` | Slot-2 parsers: Ollama 3B (JSON-schema grammar), deterministic rule parser, chain |
| `nesting.py` | Phase 2: best-fit-decreasing cut-list compiler (dry-runs the real bus on a scratch registry) |
| `quoting.py` | Phase 2: material pricing from the nest plan, reclaimed drops at $0 |
| `procurement.py` | Phase 2: burn rate vs vendor lead time, reorder alerts |
| `labels.py` | ESC/POS label + QR for racked remnants |
| `server.py` | FastAPI endpoints |
| `cli.py` | `extbus demo`, `extbus serve` |

## Subnets (10.0.0.0/8)

```
10.1.0.0/24 control plane + 3B agent     10.4.1.0/24 job carts (WIP kits)
10.2.1.0/24 raw stock yard               10.5.1.0/24 scrap hopper / recycling ledger
10.2.2.0/24 remnant racks (sq >= 36")
10.3.1.0/24 saw stations
```

## Physics (the contract)

* `miter_deg` is deflection from square: 0 = square, 45 = miter. `dL = W*tan(theta)`, rounded **up** to 1/16".
* `long_point_in` is extreme-to-extreme. `guaranteed_square = long - sum(dL + kerf)` over mitered ends, rounded **down**.
* One planar cut makes complementary ends: piece long point on face F -> remaining stick's long point on `F.opposite`.
* Lead handling on a stick: `native` (end already matches, cut skipped, offset 0) | `miter_lead` (square end gets a miter, offset = kerf) | `true_square` (mitered end trimmed, offset = `dL + kerf`). A mitered end that is the wrong angle/face is `GEOMETRY_MISMATCH` (flip or choose other stock).
* Gate 3: `(length + kerf) <= stock_long - far_end_dL - lead_offset`.
* Remainder: `stock_long - (offset + length - trail_dL + kerf)`. Remainder with `guaranteed_square >= 36"` is racked and gets a QR sticker, otherwise `SCRAP BIN` and its weight goes to the salvage ledger.

## Gate codes

`UNKNOWN_TARGET` `REF_MISMATCH` `ALREADY_ALLOCATED` (GI_1) | `CROSS_SUBNET` `GEOMETRY_MISMATCH` (GI_2) | `BOUNDS_OVERFLOW` (GI_3)

## TDMA frame

`SLOT_1_TASK` ingest -> `SLOT_2_PLAN` model fills `Slot2Plan` (`extra=forbid`) -> per piece: `SLOT_3_EXECUTE` (10.2.2.0/24 first, best-fit, native match preferred) -> `SLOT_4_GUARD` (rejected candidate wraps back to 3) -> `SLOT_5_VERIFY` (atomic commit).
Invalid JSON / unknown part number -> NAK with feedback, retry; 3 strikes -> DLQ. `NO_STOCK` also trips the DLQ with zero state change.

## Shop-floor keys (frame flow)

| Key | Call |
|---|---|
| 1 CUT_OK | `POST /order/{id}/ack {"result":"CUT_OK"}` |
| 2 FLIP_MITER | `POST /cut/flip` (flips every long point, re-verifies) |
| 3 MISCUT_NAK | `POST /order/{id}/ack {"result":"MISCUT_NAK","actual_length_in":57.5}` -> drop recalculated from the actual cut, piece to scrap, replacement staged in the same response (`elapsed_ms`) |
| ghost-drop button | `{"result":"PIECE_MISSING"}` -> purged to `unaccounted`, next source returned |

Also: `POST /cut/verify`, `/cut/complete`, `/cut/miscut`, `/drop/missing`, `GET /label/{addr}`, `GET /ledger/scrap`, `GET /health`.

## Run

```bash
pip install -e ".[dev]"
pytest                      # 118 tests
extbus demo                 # seeded shop, full order, temp registry
EXTBUS_STATE=/srv/shop/state_registry.json extbus serve     # :8000
```

Parser: `EXTBUS_PARSER=auto|ollama|rules` (default `auto`: Ollama `qwen2.5:3b` at `127.0.0.1:11434`, falling back to the rule parser only if the model server is unreachable; malformed model output is NAK'd, never silently replaced). Override with `EXTBUS_MODEL`, `EXTBUS_OLLAMA`.

### Example run

```
== scope: three 58" screen braces for Job 104, spline out, 45 degree miter; one 54 3/8" brace square

SLOT_2_PLAN -> 4 pieces, naks=0

#01 [GI_1:OK GI_2:OK GI_3:OK]
    PULL DROP 10.2.2.2 (BLUE-MEDIUM) | END A AS-IS | CUT 58" 45deg LONG PT SPLINE
    CUT_OK  SCRAP BIN  (drop 5 7/8", sq 1 3/4")
#02 [GI_1:OK GI_2:OK GI_3:OK]
    PULL DROP 10.2.2.1 (BLUE-MEDIUM) | TRUE END A SQUARE (-4 1/8") | CUT 58" 45deg LONG PT SPLINE
    CUT_OK  SCRAP BIN  (drop 16", sq 11 7/8")
#03 [GI_1:OK GI_2:OK GI_3:OK]
    PULL RAW STICK 10.2.1.4 (RAW-YARD) | END A AS-IS | CUT 58" 45deg LONG PT SPLINE
    CUT_OK  RACK BLUE-LONG -> 10.2.2.3  (drop 185 7/8", sq 181 3/4")
#04 [GI_1:OK GI_2:OK GI_3:OK]
    PULL DROP 10.2.2.3 (BLUE-LONG) | TRUE END A SQUARE (-4 1/8") | CUT 54 3/8" SQUARE
    CUT_OK  RACK BLUE-LONG -> 10.2.2.4  (drop 127 1/4", sq 127 1/4")

== job JOB-104: 4 pieces on cart 10.4.1.1
== remnant racks (10.2.2.0/24):
   10.2.2.4   BLUE-LONG     sq   127 1/4"  A=SQ B=SQ
== scrap hopper (10.5.1.1): 1.914 lb in 2 entries
```

## Phase 2

**Adopt Legacy Drop** (`POST /api/saw/adopt-drop`). The operator logs profile, tape measurement (`74.25` or `"74 1/4"`, rounded down to 1/16") and end condition once (`square`, `miter_45_spline`, `miter_45_back`). The bus assigns a 10.2.2.x address, bins it, prints the sticker, and optionally locks it to a job. Passing `frame_id` for a frame parked on `NO_STOCK` resumes it and stages the next cut against the new drop. Adoption is never blocked by a full rack: the piece is already in the operator's hands.

**Nesting** (`POST /api/nest/compile`, `POST /api/nest/commit`). Simulates three piece orderings (best-fit decreasing, grouped by miter/face, as given) on a scratch copy of the registry and keeps the plan with the fewest unmet pieces, then fewest virgin pulls, then least scrap, then most skipped lead cuts. Because the plan is a dry run of the real bus, execution reproduces it step for step while the registry is unchanged. `commit` opens a frame on the compiled order (409 if anything is unmet). `allow_purchase` reports the shortfall in sticks instead of failing.

**Quoting** (`POST /api/quote`). Billing rule: a piece from a pre-existing drop (or a drop derived from one) bills $0. A piece from a virgin stick, or from an offcut the job itself created, bills the length that did not survive as a racked remnant, so scrapped remainders are charged to the job that made them. Response includes `virgin_only_cost` / `price_virgin_only` (the same job with empty racks), `savings_from_drops`, shortfall sticks, and `ready_in_days` (vendor lead time when something must be ordered).

**Procurement** (`GET /api/procurement/reorder`, `POST .../order`, `POST .../receive`). `burn = virgin pulls in window / days observed`; open orders count toward cover only if they land before the rack empties. `STOCKOUT_RISK` = cover < lead time; `REORDER_NOW` = cover <= lead + safety, or below `min_stock_sticks`. Suggested qty covers lead + safety + 7 review days.

**Rack capacity gate.** A remainder with guaranteed square >= 36" is racked only while that profile's rack is **strictly under 85%** of `rack_slots` (checked after the source drop leaves the rack). Otherwise it goes to scrap with reason `RACK_FULL` (screen: `SCRAP BIN (RACK FULL)`); under-36" remainders keep reason `DROP_UNDER_MIN`.

## Assumptions and limits

* Kerf is a flat 0.125" per cut (no `k/cos(theta)` correction on miters) and truing waste is not weighed; only remainders and ruined pieces hit the scrap ledger.
* Frames are held in server memory; registry, carts, scrap, usage history, orders, unaccounted and DLQ are persisted. A restart drops open frames (reservations stay locked until unlocked).
* One saw operation per piece (trailing end). Lead ends come from the stock; FLIP_MITER is the operator's override.
* Nesting is a heuristic (best-fit decreasing over three orderings), not an optimal solver. A remainder that the rack gate scraps is not held back for later pieces in the same job.
* Burn rate is flat-rate over the trailing window; it does not see scheduled jobs (a pipeline lookahead is not built).
* Prices are `cost_per_ft` x charged length; no scrap-metal salvage credit and no per-stick vendor pricing tiers.
* Storage is JSON with atomic replace; SQLite is not implemented.
