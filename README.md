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
| `sales_intake.py` | Phase 3: intake models, prompt cards, readiness report, `apply_answers` |
| `sales_gates.py` | Phase 3: pre-flight gates `GE_1`..`GE_3` |
| `parametric.py` | Phase 3: envelope -> wireframe + deterministic cut list |
| `media.py` | Phase 4: photo validation, storage under `./media/{job_id}/`, resolution + sharpness |
| `vision.py` | Phase 4: pluggable vision analyzer (heuristic default, mock, remote multimodal) |
| `recovery.py` | Phase 5: boot recovery (frames back at their exact step, stale locks freed, older released jobs rebuilt) |
| `telemetry.py` | Phase 5: `/ws/saw/{station}` hub, protocol, blade setup |
| `printer.py` | Phase 5: ESC/POS 2"x1" label rendering, transports, durable outbox worker |
| `batch.py` | Phase 5: multi-job batch nesting compile + release |
| `static/` | Phase 4: zero-dependency SPA: `index.html` (shell, Saw Bench, Inventory, Accounting), `sales.html`, `engineering.html`, `common.js`, `app.css` |
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
pytest                      # 270 tests (browser tests need: playwright install chromium)
extbus demo                 # seeded shop, full order, temp registry
EXTBUS_STATE=/srv/shop/state_registry.json EXTBUS_MEDIA=/srv/shop/media extbus serve     # :8000, open /
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

## Phase 3: sales field intake

`POST /api/sales/intake` runs all three gates at once (no short-circuit: the rep is still on the patio) and returns an `EngineeringReadinessReport`. Each gap is a `MissingItemPrompt` card (`field_key`, `critical_blocker` | `field_verify`, `instructions_for_rep`, `requires_photo`, `input_type` photo/number/choice, `choices`). Ready = zero blockers; `field_verify` cards never block. `confidence_score` is a completeness score: `1 - 0.2*blockers - 0.05*verifies`.

| Gate | Blocks (critical) | Warns (verify) |
|---|---|---|
| `GE_1` envelope | width / projection / house-wall height / front height missing (`MISSING_DIMENSION`) or outside plausible inches, e.g. feet typed as inches (`DIMENSION_OUT_OF_RANGE`) | |
| `GE_2` substrate & pitch | `unknown` substrate; no tie-in photo; tie-in photo under 800 px or sharpness < 0.35 (when the tablet sends those); pitch `(house_h - front_h)/(proj/12)` strictly below 0.5"/ft, including inverted (`INSUFFICIENT_PITCH`) | no wide shot / sketch photo; pitch above 6"/ft; analyzer disagrees with the rep's substrate |
| `GE_3` obstacles | door-swing question unanswered; out-swing door without confirmed clearance against the 36" chair rail | downspouts / steps not acknowledged; perimeter walk not confirmed |

`POST /api/sales/intake/answer` merges the rep's answers (`apply_answers`, keyed by `field_key`) into the stored package and re-runs the gates. Intakes and reports persist in `state_registry.json`.

`POST /api/sales/generate-bom` re-runs the gates (409 + report if blocked), solves the envelope, and dry-runs the quote with drop credits. The returned `parametric.cuts` are `CutLine` items that go unchanged into `/api/quote` and `/api/nest/compile`.

**Parametric rules** (`parametric.py`, inches; x along the house wall, y outward, z up): `theta = arctan(drop/proj)`. Bays = fewest equal bays with spacing <= 48" O.C. `EXT-220-BOX` posts: front line at `front_h`, both side lines at the roof-plane height. `EXT-240-OPEN` rafters: one per front post line, plumb cut at both ends with long points on opposite faces, ordered length `hypot(proj, drop) + W*tan(theta)`. Because the faces alternate, the leftover of one rafter is a native lead for the next (the nesting compiler skips the lead cut). Chair rails (36" AFF) on three walls and front top rails: square, spline out, spanning the clear gap (`spacing - post width`).

**Vision hook.** Photo pixels are not read. `PhotoAnalyzer` (`create_app(analyzer=...)`) is the seam for a local multimodal model that returns substrate / obstacle / door detections; the default `NullAnalyzer` makes the gates rely on the rep's answers. A detected substrate never replaces the rep's choice: it adds a hint or a verify card.

## Phase 4: viewports, media pipeline

Open `http://<server>:8000/`: tabs **Sales | Engineering | Saw Bench | Inventory | Accounting**. `/sales` and `/engineering` are also standalone pages (the phone opens `/sales?job=JOB-123` directly). Plain HTML/CSS/JS, no CDN, no build step, DOM built from text nodes only (a test forbids `innerHTML`).

**Sales (`/sales`)**: three photo cards (wide context, roof tie-in, tape sketch; the camera opens on phones), four envelope inputs (tape strings like `12 1/2` work), and one-tap *action cards* rendered from the `GE_1`..`GE_3` prompts (retake photo, yes/no door swing, substrate picker, pitch fix, acknowledge downspout). The price appears once no blockers remain, with the drop-savings offset and the price a drops-blind bidder would need. Everything the page does goes through `/api/sales/intake/answer` and `/api/sales/upload-photo`; the server is the single source of truth, so a job resumes on any device by job id.

**Engineering (`/engineering`)**: left pane shows the uploaded photos with resolution/sharpness, the rep's substrate next to what vision saw (advisory when confidence < 0.6), obstacles and open items. Right pane has four override inputs, a plan view and a side elevation (posts, rafters, 36" chair rails, bay spacing, pitch arc; click any member for its cut), the BOM and the quote. Overrides recalculate through `POST /api/engineering/preview`, which persists nothing. **Release to Shop Floor** (tap twice to confirm) calls `POST /api/engineering/release`: re-runs the gates with the overrides, solves the envelope, runs the nest compiler, assigns the job cart (`10.4.1.x`), opens the frame the Saw Bench works from, and records the overrides as a revision. It refuses (409) if blockers remain, the job is already released, or stock cannot cover the BOM (the response lists the sticks to buy). Release does not reserve stock; the bench reserves one source at a time.

**Saw Bench**: pick a released job, the bench stages each cut; USB macro-pad keys **1** CUT_OK, **2** FLIP_MITER (`POST /order/{id}/flip`: turns the piece, releases the old reservation, re-sources), **3** MISCUT. Piece-missing and adopt-a-legacy-drop are on the same screen.

**Upload (`POST /api/sales/upload-photo`, multipart `job_id`, `kind`, `file`)**: JPEG/PNG/WEBP up to 15 MB, decoded by Pillow before anything touches disk (renamed executables, GIFs, truncated files and pixel bombs are refused with 415/413). `job_id` is restricted to `[A-Za-z0-9._-]`, filenames are generated (`tie_in_closeup.jpg`), and a retake replaces the file. Stored metadata: width/height (EXIF rotation applied), bytes, SHA-256, original filename and a 0..1 sharpness score (Laplacian variance on a 640 px thumbnail, `v/(v+60)`; the gate blocks a tie-in photo below 0.35 or under 800 px). Files are served at `/media/{job_id}/...` with `nosniff`.

**Vision (`vision.py`)**: `EXTBUS_VISION_API_KEY` set -> `RemoteBackend` (Anthropic Messages format; `EXTBUS_VISION_MODEL`, `EXTBUS_VISION_URL`), otherwise `HeuristicBackend`. The heuristic is a coarse color statistic with confidence capped at 0.5, so it is shown to engineers but can never move a gate. Remote replies must validate against a strict schema (enum substrate/obstacles, clamped confidences); anything else, or any network failure, falls back to the heuristic and is labelled as such. Results are cached per photo. `MockBackend` (filename tokens like `brick`, `downspout`) exists for tests and demos.

## Phase 5: shop-floor hardware, live telemetry, persistence

**Crash recovery.** Every frame (slot, staged step, pending pieces, reservations, station) is saved in `state_registry.json` inside the SAME durable write as the stock and cart changes it describes (`bus._atomic`): after a power cut the bench shows the step it showed before, and a half-finished commit never happened (no double-credited piece, no consumed stick with an un-advanced frame). On boot `recovery.restore()` reloads frames to their exact step, re-stages any whose reserved stock vanished, frees reservations owned by finished jobs, and **rebuilds a frame for jobs released by an older build** (stored BOM minus pieces already on the cart; assumes the BOM has not changed since release). `GET /api/recovery` reports what it did. File durability: fsync of the file and its directory, a hard-linked `.bak` of the previous version, and fallback to it if the main file is unreadable (it never silently starts empty).

**Live saw telemetry.** `WS /ws/saw/{station}` (name or `10.3.1.x` address). Client messages: `attach {frame_id}`, `stage`, `key {key:"1"|"2"|"3"|"4", actual_length_in?}` (1 CUT_OK, 2 FLIP_MITER, 3 MISCUT_NAK, 4 PIECE_MISSING), `sync`, `ping`. Server pushes `state` (frame, staged step = the next drop to pull, gate verdicts, **blade setup**: angle, swing, fence length, lead-prep mode and trim, expected drop), `event` (CUT_OK etc. with the rack/scrap screen and label text), `print`, `error` (sender only). Every broadcast has a per-station `seq`; a new connection gets the current state first, so a reloaded tablet or a second wall display is correct immediately; HTTP actions (`/order/...`) also push to attached displays. The Saw Bench uses the socket and falls back to HTTP if it drops. Swing convention: long point on the spline face = `left`; invert with `EXTBUS_SWING_SPLINE=right`.

**Label printing.** `EXTBUS_PRINTER=net:192.168.1.50:9100` | `file:/dev/usb/lp0` | `memory`. When a remainder is racked (or a legacy drop is adopted) the sticker job is committed in the same transaction as the remnant (transactional outbox), and a worker thread is woken the moment that commit is durable. Offline printer: the job stays queued, retries with backoff (1, 2, 4 ... 60 s, then `failed` after 8 tries; `POST /api/printer/retry`), and prints after a restart. Delivery is at-least-once (a crash between send and mark can reprint one sticker). Label: 2" x 1" at 203 dpi (384 x 203 dots), QR on the left, bin / address / square length / profile / end conditions on the right, one `GS v 0` bitmap. Options: `EXTBUS_LABEL_STYLE=raster|native`, `EXTBUS_PRINTER_FEED=ff|gs_ff|none|lines:N` (how the printer advances to the next label gap; **model dependent, test yours**), `EXTBUS_PRINTER_CUT=1`. Try one without the server: `extbus printer-test --target net:192.168.1.50 --save-png label.png`. Endpoints: `GET /api/printer`, `/api/printer/queue`, `POST /api/printer/reprint {addr}`, `/retry`, `/test`.

**Batch nesting.** In Engineering, open *Shift batch*, tick two or more released jobs, *Compile batch* (`POST /api/engineering/batch-compile`) to see **back-to-back vs batched** (virgin sticks, drops used and created, lead cuts skipped, scrap lb, yield %), then *Release batch* (`.../batch-release`). The compiler tries pooled best-fit-decreasing, pooled grouped-by-miter, and the back-to-back order itself, so the batch is never worse than the baseline it reports. The batch runs as ONE frame (`BATCH-xxxxxx`) that owns stock reservations, while each piece is credited to its own job's cart (`10.4.1.x`) and scrap is attributed to its own job; the member jobs' frames are retired in the same commit. Yield % = piece length / (piece length + scrapped leftover length); racked leftovers count as inventory, not waste. Release is refused if any piece cannot be cut from current stock.

## Assumptions and limits

* **No authentication.** The server is meant for the shop LAN. Anyone who can reach it can upload photos, release jobs and change inventory; put it behind your router, not on the internet.
* The remote vision backend is written to the API format but was **not run against the live API** (no key in development); its request shape, parser and fallback are unit-tested with a fake transport. The sharpness constant was calibrated on synthetic images: check it on a few real driveway photos before treating it as a hard blocker.
* Printing was tested against in-memory, file and real TCP-socket transports, never a physical printer. USB works only as a device node (`/dev/usb/lp0` on Linux); direct libusb access is not implemented. Raster layout assumes a 58 mm head (384 dots) at 203 dpi.
* Browser testing covers Chromium (headless). The pages use only standard DOM/SVG APIs but were not tested in Safari or Firefox.
* Media is stored on the server's disk and is not part of `state_registry.json`; back up `./media` with it. Photos are never deleted by the bus.

* Kerf is a flat 0.125" per cut (no `k/cos(theta)` correction on miters) and truing waste is not weighed; only remainders and ruined pieces hit the scrap ledger.
* Persistence is JSON with atomic replace + fsync + `.bak`, not a write-ahead log: the whole file is rewritten on each acknowledged cut. Fine at shop scale; very large frames (hundreds of pieces) make each write slower. Finished frames are pruned to the last 50.
* One saw operation per piece (trailing end). Lead ends come from the stock; FLIP_MITER is the operator's override.
* Nesting is a heuristic (best-fit decreasing over three orderings), not an optimal solver. A remainder that the rack gate scraps is not held back for later pieces in the same job.
* Burn rate is flat-rate over the trailing window; it does not see scheduled jobs (a pipeline lookahead is not built).
* Prices are `cost_per_ft` x charged length; no scrap-metal salvage credit and no per-stick vendor pricing tiers.
* Parametric: front-wall rails and posts only follow the rules above (no gable, no knee-wall variants, no door framing; the door check only gates release). Post length equals the local roof-plane height with no seat allowance; rafters assume a plumb cut across the profile width. Verify these against your build standard before quoting real jobs.
* SQLite is not implemented.
