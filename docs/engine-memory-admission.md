# Engine: memory, admission and context sizing

How the engine decides what fits on a unified-memory Mac: the GPU ceiling, load-time preflight, auto-context,
prefill chunk width, the admission line for a long prompt, and why under-billing is fatal. Read this before touching
any `*Bytes` bill, `Scheduler.init`, the preflight, or the admission path.

Index: [CLAUDE.md](../CLAUDE.md#docs-index). Related: [engine-kv-cache](engine-kv-cache.md),
[engine-qsa-long-context](engine-qsa-long-context.md#admission-and-load-time-bills),
[engine-prefix-cache](engine-prefix-cache.md#budget), [engine-expert-streaming](engine-expert-streaming.md#budget),
[arch-mimo-v2](arch-mimo-v2.md#bills-the-bill-follows-the-storage-in-the-same-commit).

## Metal OOM

- **Metal OOM is UNCATCHABLE and Metal at the working-set edge returns ZEROS before it aborts**: all-zero logits from
  healthy inputs = MEMORY symptom.
- `currentGpuMemoryCeiling` must see EXTERNAL pressure; under-billing is a Metal OOM, so a bill goes down only where
  the bytes are gone.
- The box: M5 Max 128 GB; the default wired limit admits about 120 GB; a resident MiMo EXL3 pack is over 90 GB, so two
  heavy GPU jobs at once risk an OOM for both (and concurrent conversions have died together in a GPU reset).

## Load-time preflight

- The preflight's available figure is free RAM CAPPED at Metal's working-set limit (`effectiveAvailableBytes`): a
  lowered `iogpu.wired_limit_mb` binds below free RAM, and a load past it failed warmup, then every request.
- Preflight refusals → `InsufficientMemory` → 503 + entry reset to `.unloaded`. A refusal quotes the number it
  COMPARED (`loadRequirementBytes`) and the flag that would admit (`--wired-margin-gib`, `--skip-mem-preflight`,
  `iogpu.wired_limit_mb`).
- Resident Flash-Next or MiMo EXL3 with an explicit context bills weights plus min(flat headroom, 2 GiB load/warmup
  scratch + `sizerCtxKvBytes`); auto context, other layouts/architectures, streamed loads, sidecars and ANE keep flat
  headroom (min(weights/8, 6 GiB) + 1 GiB). This is a load gate, not the request admission bill.
- `modelDiskBytes` bills the shards the INDEX names; an index that names NO shard on disk is STALE (every shard
  loads, one warning). Every size sum stats THROUGH symlinks (HF-cache models).
- Load-time bills run INSIDE `Scheduler.init` ([engine-qsa-long-context](engine-qsa-long-context.md)).
- **The kernel unwires a freed Metal buffer asynchronously** (~0.5 s for 50 GB): an unload and an eviction-before-load
  wait until most of the freed bytes left the wired set (`waitForUnwire`, bounded at 3 s), or the next preflight reads
  them as taken (45 GB free where 95 GB was a moment later).
- A ready entry's `bytes_resident` (the registry's resident-memory gate, `/v1/models`) is the weights the preflight
  billed (`residentWeightBytes`): a boot `--model` entry has no discovery `bytes_on_disk`, so it measures the shards.
- **A resident MiMo cold load reserves its load preflight's own requirement** (`mimoColdLoadBillBytes`: the shared
  `mimoResidentLoadBytes` plus `preflightCtxBytes`), never the 1.1x disk-size guess (105.9 GB against a 96.65 GB bill
  for the 2.3bpw pack). The auto resident cap bounds co-residence only ([server-lifecycle](server-lifecycle.md)).

### Explicit-context warmup envelope

`ad4a3ce0` plus the context-bill change, ReleaseFast binary mtime 2026-09-26 15:26:43 local, M5 Max 128 GB:
`test_load_context_preflight.sh`, `--ctx-size 1248 --kv-quant 8`, vision enabled, MTP as below. Each row is the
larger pre-request `/props` peak across startup and cold `/v1/load-model`; both paths then completed a short chat.
QoS `taskpolicy -a`, one `load-context-<pid>` GPU lock per run, conversion concurrent (memory validation, not timing).
The baseline is the existing flat formula, not an old-binary rerun.

| Pack | MTP | Old requirement (GiB) | Context requirement (GiB) | Load/warmup peak (GiB) |
|---|---|---:|---:|---:|
| Sushi-3bpw | on | 56.33 | 51.35 | 49.9073 |
| Sushi-3bpw | off | 56.33 | 51.35 | 47.6341 |
| Sushi-4bpw | on | 70.68 | 65.70 | 64.2628 |
| Sushi-4bpw | off | 70.68 | 65.70 | 61.6967 |

MiMo-V2.6-Flash-Sushi-2.3bpw, `--ctx-size 1248 --kv-quant 8`, MTP and vision on (1-8-row verify warm-up and the three
heads), `taskpolicy -a`, GPU lock, one boot per row, 2026-10-01:

| Mode | Binary | Billed weights (GiB) | Requirement (GiB) | Pre-request `/props` peak (GiB) |
|---|---|---:|---:|---:|
| startup, flat headroom | gap/mimo-g1b e425a1a2, built 10:17:14 | 89.65 | 96.65 | 89.88 |
| cold `/v1/load-model`, context term | gap/mimo-g1b c5f8f3ac, built 10:46:23 | 89.65 | 91.76 | 89.88 |

Both peaks were unchanged after a short chat. The 2 GiB allowance leaves ~1.9 GiB unused, so MiMo takes the same term.

The 2 GiB allowance covers load/warmup scratch and fixed state outside the context bill, not arbitrary prompt
activations. Separate MTP sidecar files, assistant drafters and ANE retain flat headroom; other expert layouts and
architectures need their own measured envelope. These runs do not simulate a 64 GB host or establish a timing result.

## Context and chunk

- **Auto-context is PINNED at load** (`pinAutoContext`, 85% margin on the memory ceiling); ask
  `getEffectiveContextLength`. It bills KV at the CONFIGURED width and activations ONCE.
- The prefill CHUNK is a machine decision (`resolvePrefillChunk`, ladder 8192→512 at ≤ a quarter of the serving
  budget). `--prefill-chunk` pins it off the per-request ladder; on the ladder it is the widest rung. `prefillMemoryNeeded` takes STORED and SCORED widths as two parameters.
- A per-request arch (`perRequestPrefillChunk`: qwen4_exp and the ringed mimo_v2) re-picks the width for every
  request: the widest rung whose admission bill fits live memory (`chooseRequestPrefillChunk`), stepping down per
  chunk under pressure; the load-time pin is only the fallback. `boundedPrefillChunk` still caps the rung per arch
  (qk 192: 2048 by default, up to 4096 with an explicit `--prefill-chunk`).
- **The load line names a per-request arch's pin as the fallback** (`prefillChunkLoadLine`: "per request, up to N at
  a short prompt; load-time fallback M"; Flash-Next's bound narrows as the context grows). MiMo's pin swings 512-2048 between boots with the memory active at load (the ungated cap,
  (ceiling - active - hot-cache ask) / 4, is ~4 GiB beside a 3.6 GiB 2048 reserve), while every request up to 256k
  prefills at 2048 (bill 4.7 GiB at 64k, 7.5 GiB at 256k, against ~17.9 GiB available).
- An explicit `--ctx-size` outranks auto-context and `model-settings.json` `ctx_size`.
- Disconnect cancellation takes effect at the next prefill chunk boundary; a wall-time cancellation test must bound
  its chunk size rather than assume the auto-sized chunk fits a fixed deadline.

<a id="recipe-64gb"></a>
### The 48 GB and 64 GB recipe contexts

The README's 64 GB `--ctx-size` values are checked with the engine's own full-context admission bill
(`prefillNeededAtChunk` through the per-request ladder) at a 59,000 MB ceiling: a prompt that fills the context, MTP
on, `--mtp-head-kv-quant`, the 1 GiB hot cache pinned (not evictable). No live boot: the wired limit has only test
seams (`wired_limit_mb_override`, `static_ceiling_override`), so the bill was computed in a scratch test at `ad5e6be8`.

| pack, KV | `--ctx-size` | bill (MiB) | width | available (MiB) | weights + bill (GiB) | largest admitted |
|---|---:|---:|---:|---:|---:|---:|
| Sushi-2.6bpw, kv8 | 250000 | 11866 | 4096 | 12975 | 55.5 | 470000 |
| Sushi-2.6bpw, kv4 | 450000 | 12719 | 4096 | 12975 | 56.4 | 786000 |
| Sushi-3bpw, kv8 | 128000 | 7329 | 2048 | 7463 | 56.5 | 200000 |
| Sushi-3bpw, kv4 | 248000 | 6987 | 1024 | 7463 | 56.2 | 322000 |

The 48 GB recipe is the same check at a 43,000 MB ceiling (probe at `eab60060`, weights 37,552,413,730 bytes):

| pack, KV | `--ctx-size` | bill (MiB) | width | available (MiB) | weights + bill (GiB) | largest admitted |
|---|---:|---:|---:|---:|---:|---:|
| Sushi-2bpw, kv8 | 131072 | 5905 | 512 | 6163 | 40.7 | 141072 |

The README memory table's "needed" row is the weights plus this full-context bill at the 512 rung plus a 1 GiB hot
cache. At `eab60060` the bill is the same for every Flash-Next pack and for `--max-tokens` 32000 or 64000: 5905 /
8825 / 14025 / 24425 MiB at 128k / 256k / 512k / 1M tokens (KV 2080 / 4160 / 8320 / 16640 of it).
Its max-context column is the largest multiple of 8192 tokens whose weights + 512-rung bill + 1 GiB hot cache + 256
MiB spare fits each wired limit, capped at 1M (same probe, kv8 and kv4).

The ladder widens the chunk until the bill nearly fills what is available, so the spare in a row is small by
construction; "largest admitted" (2000-token steps) is where even the 512 rung stops fitting. The ceiling assumes
the full limit is reachable: on a real 64 GB Mac the free-RAM term can bind lower (`currentGpuMemoryCeiling`).

## Admission

- One `[admission] needed=… available=… reclaimable=… width=… verdict=…` line per decision.
- An explicit `--prefill-chunk N` caps the per-request ladder, never pins it: N if it fits, else the widest rung
  below N that fits, down to 512; refused only when that floor does not fit. `requestPrefillPick` is the one rule for
  the bill and the scheduler, `generate.requestPrefillChunk` the width both run; the hot-cache clamp and a streamed
  load prove that floor (`perRequestFloorWidth`). `SUSHI_PREFILL_CHUNK_PER_REQUEST=0` restores the pin.
- A long prefill evicts the hot cache on the INFERENCE thread to be admitted (`evictLruToAdmit`) on every arch where
  `admissionEvictsHotCache` holds (qwen4_exp and the ringed mimo_v2; the connection thread's `creditedAdmissionBill`
  and the scheduler's `admissionPassArmed` read that one predicate), crediting only
  provably reclaimable bytes; `PrefillDoesNotFit` → 400 by name. A warm share that does not fit is first taken
  over (`checkoutRestored`: its append donates, so the restored rows are not billed twice).
- qwen4_exp bills a warm request AFTER its restore, so a disk-restored buffer is live memory at the bill. The SSD
  tier fills buffers the slot owns (`LookupResult.slot_owned`), so its rows are credited like a checkout's: the
  first append grows each layer and its old rows are freed at that layer's eval window, which is all the bill keeps
  (`grow_coexist_bytes`). MiMo's ring restore is not credited. The restore itself runs unbilled, so it holds the
  restored KV plus one chunk ([engine-prefix-cache](engine-prefix-cache.md#basics)).
- Measured (Sushi-3bpw, kv8, `--ctx-size 262144 --prefix-cache-disk 20GB --prefix-cache-entries 1`, MTP on, a
  140,565-token SSD restore, one boot, `taskpolicy -a`, lock held, 2026-10-01): the peak over live memory at the bill
  is 1,534 MiB with a 2,328-token tail and 3,072 MiB with 9,144, against a credited bill of 9,425 / 9,641 MiB (11,430
  / 11,647 MiB uncredited). The grown buffers alone exceed the restored rows' 1,750 MiB, so those rows were freed
  before the peak.
- A warm restore whose buffers hold the prompt but not the reservation (seq <= C < R) is grown to R before the
  prefill's first chunk (`KVCache.growToReservation`), one KV layer per eval, so it bills one window of old rows
  (`oldBuffersInEvalWindow`) for a donated restore and nothing for a share. At a 128k entry, kv8: +170 MiB on
  qwen4_exp and +212.5 MiB on mimo_v2 over the bill without the grow; growing at the first decode step instead
  held every layer's old rows at once.
- The eviction pass drains the GPU stream before it reads live memory: a command buffer in flight holds its inputs'
  buffers, so an eviction read early frees nothing and trips the shared-entry stop.
- **Concurrent arrivals are each billed against the SAME free memory** on their connection threads. The gated arch
  (qwen4_exp) re-bills live memory before each prefill in `runPrefill`; an ungated one (mimo_v2) is re-billed at the
  pending drain (`admitsWithinMemory`: live requests plus this tick's earlier admits). One that does not fit beside
  company waits in `pending` (`[admission] held`); alone it proceeds.
- The hot-cache budget is clamped at load and follows residency ([engine-prefix-cache](engine-prefix-cache.md#budget)).
- Context-overflow 400s name BOTH counts.
- **A freed reserved-KV slot goes back to the OS, not MLX's pool** (`deinitSlotsReturningPool`, and the prefill-end
  clear, both on `reservesKvCapacity`): the request-end clear runs before the slot is freed, so its KV parked there
  (MiMo: 1.6 GiB after a 128k request, 3.2 after 256k) and the next admission read it as spent.
- MiMo MTP adds a constant per-request and load-time reserve (`mimo_mtp.State.billedBytes`) for all three sliding head KVs, retained hiddens, and catch-up/concatenation buffers; it is zero with MTP off and never scales with context.
- **A vision encode is billed before it runs** (`towerFitFault`, `server.visionEncodeBill`): the largest block's tower
  scratch (`qwen_vision.encodeScratchBytes`, fitted >= 25% over the measured peak) plus every block's float32 pixels
  and three bf16 copies of its soft-token rows (group outputs, video concatenation, request concatenation); past what
  the GPU has left it is a named 400. The tower evaluates per block, so the peak is one block's f32 score sheet
  (heads x N^2) and rows; table in [arch-qwen4exp](arch-qwen4exp.md#vision-tower).
- **A video's block is ONE temporal group**, never the whole video: `forwardVideo` encodes and evaluates each group
  alone, so the bill grows linearly with the group count (the old N^2 over all groups billed 79 GB at 8x46x82).
  Measured peak (pixel upload to evaluated output) = one group's scratch + all pixels + the earlier groups' rows:

  | video (t x h x w patches) | peak | old bill | bill | bill / peak |
  |---|---|---|---|---|
  | 1 x 46x82 | 1249 MB | 1626 MB | 1658 MB | 1.33x |
  | 2 x 46x82 | 1277 MB | 5.6 GB | 1696 MB | 1.33x |
  | 4 x 46x82 | 1333 MB | 20.6 GB | 1771 MB | 1.33x |
  | 8 x 46x82 | 1445 MB | 79.1 GB | 1922 MB | 1.33x |
  | 2 x 24x42 | 172 MB | 590 MB | 246 MB | 1.43x |
  | 8 x 24x42 | 217 MB | 6.3 GB | 306 MB | 1.41x |
  | 2 x 96x96 (1536² cap) | 6237 MB | 30.3 GB | 8270 MB | 1.33x |
  | 8 x 96x96 (1536² cap) | 6648 MB | 460 GB | 8822 MB | 1.33x |

  `qwen vision ubench` on `242a5545` plus this change, Sushi-3bpw tower, random pixels, 3 passes (the image rows'
  peaks reproduced to the MB), `taskpolicy -a`, GPU lock `video-bill`, 2026-09-25; pinned by `visionEncodeBill covers
  each measured video peak`.

## Observing memory

`/props` reports `active_bytes`, `memory.cache_bytes`, `batching`; RSS is blind to Metal.
