# Performance baselines

The recorded speed numbers for the served packs on this box, the roofline they are
judged against, and the levers already ruled out. Before any A/B, find the matching baseline here and INHERIT it
(Team process in CLAUDE.md); every new number lands here, with its commit, binary stamp, QoS and lock, in the same
landing.

Index: [CLAUDE.md](../CLAUDE.md#docs-index). Related: [quality-kld](quality-kld.md),
[engine-kernels](engine-kernels.md), [engine-exl3-experts](engine-exl3-experts.md),
[arch-qwen4exp](arch-qwen4exp.md), [arch-mimo-v2](arch-mimo-v2.md), `benchmarks.md` (release columns).

## How to read these tables

- Box: M5 Max 128 GB (`Mac17,6`), macOS 27, AC power; the desktop takes a small share of the GPU under every cell.
- Only same-session, same-methodology cells compare. MTP cells are variance (sample across boots). Absolute numbers
  drift up to 15% between sessions with no code change (one K3 pack's no-MTP decode read 58.6 and 54.8 in two
  sessions).
- Tools: `./tests/bench.sh` / llmprobe `--bench-only --full` (median of 3 per rung); `/bench` skill for methodology.
- Every row names its binary commit. A row without one is history, not a baseline.

## Roofline

Measured peaks (mlx 0.32.2, binary a73713d):

| probe | median | best |
|---|---|---|
| streaming read kernel, 1 GiB | 538 GB/s | 558 GB/s |
| streaming read kernel, 64-256 MiB | 519-528 GB/s | 536-556 GB/s |
| copy (read+write) | 547 GB/s | |
| bf16 GEMV 16x(16384x4096) | 560 GB/s | |
| bf16 GEMM 4096 / 8192 | 45 / 51 TFLOPS | 59 / 62 TFLOPS |

The chip is a 600 GB/s class part that delivers ~530-557 GB/s to a read kernel. Decode ceilings below use 600 GB/s,
so real ceilings are ~10% lower.

| pack | bytes per decoded token | ceiling at 600 GB/s | measured | share |
|---|---|---|---|---|
| Flash-Next MCG K3 | 5.58 GB (trunk 4.01 affine-8, lm_head 0.66, routed 0.91) | 107 tok/s | 61.8 | 58% |

<a id="exl3"></a>
## EXL3 decode and prefill attribution (Flash-Next)

- EXL3 K4 resident, kv8 (2026-09-22): decode forward 18.35 ms (~56 tok/s live), prefill ~1600 tok/s at 9.6k.
  Decode is ~860 kernels per token with only ~9.8 ms of kernel time; the rest is dispatch gaps (~7 us per boundary).
  Delivered: pair GEMV ~313 GB/s, down fused ~275 GB/s, trunk 8-bit qmv ~600 GB/s.
- Prefill per 2048-token chunk: the three trellis GEMMs ~520 ms (~0.4 TFLOPS, latency-bound serial k-loop, whole
  expert re-decoded per 32-row window); rest of layer ~650-980 ms.
- Landed since: pair prepare fused into pair GEMV + simd tile reduce (decode 18.35 → 17.45 ms); half4 activation
  loads + one window table per layer (prefill 1600 → 1850 tok/s at 9.6k); the n=48 lane funnel for every non-MUL1
  codebook (197395d).
- Ruled out (do not retry without a new reason): 64-row GEMM windows (+10%, registers), k-loop unroll/prefetch, ALU
  trims in the MUL1 decode, more split-K, `MLX_MAX_OPS_PER_BUFFER`, `MLX_METAL_FAST_SYNCH`. Decode loads (630 GB/s
  alone) and decode ALU do not overlap.

## M1 Max: Sushi-2bpw streamed decode GPU attribution

2026-09-30, base `677722d`, ReleaseFast, EXL3 MCG n32/window15, 512 experts/top-10, SSD budget 20 GB,
wired margin 5 GiB, ctx 8192, kv8. M1/G13 uses the SIMD decode kernels, without NAX.

A temporary fixed-expert replay removed host routing reads and SSD fills. Block stand-ins estimated MoE at
~13 ms, GDN at ~9 ms, HC at ~5 ms and full attention at ~3 ms per token. These are graph ablations, not additive
kernel timestamps. The per-block forward profiler synchronizes between blocks and distorts the decode chain.

The shared-add finish reduction preserved bf16, f16 and f32 output bits. A same-process replay meter reset KV/SSM
before every arm and alternated A/B four times, 80 forwards per arm:

| GPU evaluation, ms/forward | A: separate add | B: folded shared add |
|---|---:|---:|
| pair 1 | 31.668 | 31.377 |
| pair 2 | 31.696 | 31.405 |
| pair 3 | 31.711 | 31.371 |
| pair 4 | 31.658 | 31.444 |
| mean | 31.683 | 31.399 |

The cut is 0.284 ms (0.90% of GPU evaluation); graph construction was 2.753 vs 2.776 ms. The meter used the real
model with replayed expert IDs; it is attribution, not a generation-quality test. A compiler started during the
last arm, but all four pairs improved. Subsequent measurements require builds to take the box lock too.

Normal greedy boot arms (warm-up plus three 300-token requests per arm, same hash-map/C prompt):

| arm | tok/s, three requests |
|---|---|
| A1 | 17.450, 17.417, 17.501 |
| B1 | 18.019, 18.034, 18.015 |
| A2 | 17.990, 18.022, 17.393 |
| B2 | 15.802, 15.805, 15.683 |

All 12 responses were byte-identical. These boots show substantial box drift; they do not establish a live tok/s
win. The controlled GPU ablation establishes the small kernel gain. One-row GDN verify-fold reuse and narrower
EXL3 lane funnels were also tried and discarded without a measured win.

## Flash-Next K3, serial (no MTP)

llmprobe `--bench-only --full`, ctx 65536, KV unquantized, MTP verified off (1.01 tok/step), one server at a time.

| pack | binary | decode 192 tok | prefill 2k | 4k | 8k | 16k | 32k | 64k | first token at 64k |
|---|---|---|---|---|---|---|---|---|---|
| affine 4/8 control | a05d15f | 65.7 | 1741 | 57.5 | | 59.4 | | 56.6 | 34.9 s |
| affine 4/8 control | 28d7fab | 63.6 | 1618 | 57.3 | | 59.9 | | 56.4 | 33.9 s |
| MCG K3 (clean paired run) | 28d7fab | 60.0 → 62.4 | 1779 | 57.1 | 56.5 | 54.8 | 56.0 | 55.7 | 34.5 s |
| affine 4/8, MTP on (depth 6) | a05d15f | 93.4 (3.4 tok/step) | 1707 | 88.6 | | 82.3 | | 79.0 | 32.9 s |

<a id="mtp"></a>
## Flash-Next MCG K3 with MTP

Binary eb458ad, one session, live cost table, llmprobe short bench (192-token decode, 2k prefill):

| cell | MTP off | MTP |
|---|---|---|
| decode tok/s | 47.2 | 70.2 |
| predictable text | 47.3 | 90.6 |
| novel text | 48.7 | 60.3 |
| tokens per step | 1.02 | 3.69 |
| prefill 2k | 1733 | 1577 |

Verify is 89-92% of a round's wall; forced-depth round ms on code: depth 1 31.2, 2 36.0, 3 42.4. The absolute
off-MTP figures sit below the serial table's: a different session, so only the within-session pairs compare. MCG
verify ms 28.2 / ~33.5 / ~37.5 at 2/3/4 rows: ~4.6 ms per extra row.

## Flash-Next Sushi-3bpw, 1M context ladder (725b76ca)

Pack `Qwen3.8-Flash-Next-Sushi-3bpw`, `--ctx-size 1048576 --kv-quant 8 --mtp`, llmprobe `--bench-only --rungs
4k,8k,16k,32k,64k,128k,256k,512k,980k`, `taskpolicy -a`, lock `bench-qwen-1m-clean`, quiet box (load average < 1), fans at
max with 4 min idle before boot (the thermal protocol in [process-measurement](process-measurement.md)). Headline cells:
decode 93.8 tok/s, prefill 1906 tok/s at 2k, MTP 3.62 tokens per step (predictable 119.9, novel 82.9), and llmprobe
saw an 11.9% sustained-load slide over the 38 min run.

| context | decode tok/s | prefill tok/s | first token | tokens per step |
|---|---|---|---|---|
| 4k | 96.1 | 1702 | 2.5 s | 3.37 |
| 8k | 92.5 | 1940 | 4.2 s | 3.31 |
| 16k | 93.2 | 1949 | 8.4 s | 3.20 |
| 33k | 96.2 | 1961 | 16.8 s | 3.00 |
| 66k | 80.7 | 1945 | 33.7 s | 2.56 |
| 131k | 80.9 | 1900 | 69.0 s | 2.63 |
| 262k | 73.4 | 1826 | 143.7 s | 3.20 |
| 524k | 55.1 | 1686 | 311.1 s | 3.37 |
| 1004k | 56.6 | 1465 | 685.2 s | 3.31 |

The same ladder on a338ca2, run hot after 2 h of other GPU work with workers computing alongside, read 20-33% lower
prefill at every rung (1358 at 2k, 1127 at 1004k) and 71.8 decode at 4k. Prefill code did not change between the two
binaries, so that gap is the box. Decode also gained from d72178a (the MTP regime gate). Never compare a ladder cell
across a thermal state.

## Release 1.0.4, Sushi-3bpw

Owner-scoped Sushi-3bpw run (the default release gate uses 4bpw): `ad4a3ce0` plus the context-bill change,
ReleaseFast binary SHA-256 `2aeee2e678521727e66994d75260c25cd4cffd0d05ecb797c73210a2b0ea9704`,
mtime 2026-09-26 15:26:43 +0700. M5 Max 128 GB, `--ctx-size 1048576 --kv-quant 8 --mtp`,
`tests/bench.sh --url` with llmprobe 0.6.12 `--bench-only` (default ladder, median-of-3 headline cells).
QoS `taskpolicy -a`, GPU lock `release-104-quiet-sushi3bpw`, conversion stopped, no other test running;
fans at max and 10 s idle from 51.9 °C, fans restored to auto afterward.

| decode tok/s | prefill tok/s (2042 tokens) | first token | tokens per step | sustained decode drift |
|---:|---:|---:|---:|---:|
| 98.4 (97.5–100.0) MTP | 1671 (1665.6–1707.4) | 280 ms | 2.87 | -4.5% |

MTP engaged in 37 logged requests; the measured n-gram pool arm engaged and the table warmed fully. Relative to
the inherited `725b76ca` headline cells above, decode is +4.9% and prefill -12.3%; no old binary was rerun.
This is not a paired speedup/regression claim: sessions differ, the old prompt was 2041 tokens, and the old loader
billed a duplicate vision shard (50.17 versus 49.33 GiB). There is no prior measured 3bpw release-column cell.

<a id="hc-row-group"></a>
## Flash-Next Sushi-3bpw: row-grouped HC read on the M5 Max (0fbb74ca vs #6)

A = 0fbb74ca, B = 0fbb74ca + #6 (PR head 3f97e4f), ReleaseFast, M5 Max 128 GB, a fresh boot for every arm run, A B B A
then B A A B, `taskpolicy -a`, GPU lock `pr6-ab` per boot, fans at max and 10 s idle from 55 °C, restored to auto after;
NOT a quiet box (a system daemon at ~100% of one core). Greedy, 4 prompts x 256 tokens (code / list / prose / story),
`--prefix-cache-entries 0`, `SUSHI_ROUND_COST_PERSIST=0`, kv8 and MTP at their defaults. Decode tok/s, mean of 4 boots:

| cell | code | list | prose | story | sum |
|---|---|---|---|---|---|
| forced depth 3 | 103.05 -> 105.49 | 85.14 -> 87.94 | 80.73 -> 83.89 | 67.49 -> 69.82 | +3.2% |
| default adaptive MTP | 98.45 -> 102.25 | 78.65 -> 83.48 | 79.96 -> 80.62 | 77.33 -> 78.90 | +3.2% |
| same boots, `enable_mtp: false` | 65.62 -> 64.76 | 65.22 -> 64.32 | 65.00 -> 64.54 | 64.82 -> 64.74 | -0.9% |

- Forced depth 3 is faster in 16/16 adjacent A/B pairs, adaptive in 11/16. Outputs are identical between the arms in
  84/84 comparisons, MTP equals serial in 32/32, and `test_mtp_equivalence.sh` passes 11/11 on B.
- Decode meter (`SUSHI_DECODE_FWD_UBENCH=100`, 4096 keys), ms per forward at 1 / 2 / 4 / 7 rows, 4 boots per arm:
  18.63 / 22.64 / 30.32 / 46.30 -> 18.68 / 22.37 / 28.68 / 43.90 (-5.4% and -5.2% at 4 and 7 rows). One row alone,
  300 forwards, 4 more boots per arm: 18.81 -> 18.72. Over those 8 boots per arm one row reads 18.72 -> 18.70 ms:
  unchanged, so the MTP-off cell above is boot noise.
- `[spec-warmup]` over 16 boots per arm: 1.20-1.34 s on B and 1.23-1.73 s on A, apart from one ~2.2 s boot in each
  (B's first, 2290 ms; A 2171 ms). The per-width D/U kernel variants add no measurable load time.
- Follow-ups on the M2 Max 64 GB (2f1e4bf vs + change, decode meter at 4096 keys, `taskpolicy -a`, lock per boot, one
  binary per arm, A B C C B A; ms per forward at 1 / 2 / 4 / 7 rows). Configs keyed on (rows, inject, pending write):
  33.67 / 48.35 / 76.69 / 126.84 -> 34.08 / 48.79 / 78.49 / 128.27, and in a second A B B A 33.78 / 48.43 / 76.36 /
  126.15 -> 33.73 / 49.10 / 77.76 / 128.17 with live decode +0.8% (MTP) / -1.5% (MTP off): no change beyond boot noise;
  outputs identical. The row count as a scalar D/U input on top: 33.81 / 50.69 / 79.12 / 130.29, 1-4% slower at 2-7
  rows (one more buffer per dispatch, a runtime clamp); dropped.

<a id="mtp-lookup"></a>
## Flash-Next: prompt lookup inside the MTP round (2f1e4bf2 + the port)

Arms of one binary per step: A = `SUSHI_MTP_LOOKUP=0`, B = lookup (c68b4cf7, ReleaseFast, sha256 848bd456…),
C = lookup with the line rule (6ea00e3f, sha256 ea4f6fd0…). M5 Max 128 GB, 2026-09-27, `tests/bench_mtp_lookup.sh`
(a file of ~600 tokens in the prompt; thinking off; greedy, and sampled 0.6 / 0.95 / 20 seed 7; 2 reps per boot),
`--ctx-size 131072 --kv-quant 8 --prefix-cache-entries 0`, `SUSHI_ROUND_COST_PERSIST=0`, MTP and exact acceptance at
their defaults, `taskpolicy -a`, GPU lock `lookup-ab` per boot, fans at max with 3 min idle from 98 °C and restored to
auto after. CONTENDED box: other workers' builds and tests ran throughout, so compare the interleaved arms only.
Sushi-3bpw, decode tok/s, median of 8 A runs (A B B A and A C C A) against 4 B and 4 C runs:

| task | greedy A / B / C | sampled A / B / C | C lookup rounds / drafted / landed |
|---|---|---|---|
| copy the file | 125.6 / 152.7 / 152.1 (+21%) | 125.2 / 140.7 / 149.1 (+19%) | 52 / 409 / 389 |
| rename in the file | 124.3 / 142.9 / 150.1 (+21%) | 123.8 / 141.2 / 145.1 (+17%) | 58 / 449 / 430 |
| fix a bug in the file | 129.4 / 138.0 / 149.5 (+16%) | 125.7 / 141.8 / 146.8 (+17%) | 39 / 301 / 279 |
| unified diff | 122.1 / 116.2 / 120.1 (-1.6%) | 122.4 / 108.4 / 119.3 (-2.5%) | 2 / 14 / 7 |
| write_file tool call | 124.0 / 137.4 / 135.4 (+9%) | 122.2 / 134.9 / 135.3 (+11%) | 44 / 337 / 303 |
| new code | 108.4 / 106.9 / 107.5 (-0.9%) | 103.9 / 103.2 / 102.4 (-1.4%) | 1 / 7 / 6 |
| prose about the file | 86.3 / 87.1 / 84.8 (-1.7%) | 82.2 / 82.2 / 82.3 (0%) | 0 / 0 / 0 |

- MTP alone already takes ~5.5 tokens per round on a copy; a lookup round lands ~7.5 of 8 drafts.
- B lost 4.8% (greedy) and 11.5% (sampled) on the diff: its lines echo the file's behind a `-`/`+`/space prefix, so a
  match agrees to the end of one line and its draft fails at the next (2-7 rounds per request landing 0-60%). The line
  rule (C) keeps 1-3 such rounds; its -1.6% / -2.5% is inside this box's noise: C's prose, with no lookup round at all,
  read -8.0% against the adjacent A boots.
- Long context (B, one pair A B then B A, median of 2, greedy): the file renamed behind ~32k tokens of repo text
  110.0 -> 136.1 (+24%), behind ~64k 108.7 -> 127.9 (+18%).
- Sushi-4bpw (B, one A B pair, median of 2): copy 112.8 -> 125.8, rename 110.7 -> 116.8, fix 102.7 -> 120.4, write_file
  104.6 -> 114.9 greedy; diff 97.0 -> 94.6 (-2.5%) and 96.9 -> 91.4 (-5.7%) before the line rule.
- llmprobe 0.6.12 `--bench-only` (`tests/bench.sh --only sushi-4bpw`, B): decode 77.7 -> 77.9, predictable 78.7 ->
  113.7 tok/s at 2.04 -> 5.33 tokens per step, novel 65.5 -> 65.6, prefill 1524 -> 1479 at 2039 tokens (lookup never
  runs in prefill; one boot per arm), 4 streams 83.2 -> 81.2 aggregate.
- Greedy output was identical to `--no-mtp` on every task, and `test_mtp_equivalence.sh` passed 18/18.

<a id="gdn-decode-recur"></a>
## Flash-Next: GDN prework and recurrence in one dispatch (caa02a4f vs the port)

A = caa02a4f (sha256 0bec2879…), B = caa02a4f + the port with a kill-switch env the landed commit drops, same kernels
(e73439e9, sha256 c45acaf6…), ReleaseFast. M5 Max 128 GB, 2026-09-27, Sushi-3bpw, `--ctx-size 65536 --kv-quant 8`,
`SUSHI_ROUND_COST_PERSIST=0`, MTP prompt lookup on in both arms, `taskpolicy -a`, GPU lock `gdn-ab` per boot, fans at
max with 10 s idle from 64 °C and restored to auto after. CONTENDED box: a system daemon at ~100% of one core
throughout, other workers' builds and tests beside the first meter and live boots.

Decode meter, the chain and the step alternated off / on / on / off per width in one process
(`SUSHI_DECODE_FWD_UBENCH=40 _S=1,3,5,7,9 _GDN_ARMS=1`, `--prefix-cache-entries 0`), three boots per length, ms per
forward, mean over boots:

| rows | 4096 keys | 32768 keys |
|---|---|---|
| 1 | 18.50 -> 18.05 (-2.4%) | 18.70 -> 18.33 (-1.9%) |
| 3 | 24.77 -> 24.35 (-1.7%) | 25.10 -> 24.68 (-1.7%) |
| 5 | 32.99 -> 32.94 (-0.2%) | 34.02 -> 33.37 (-1.9%) |
| 7 | 42.56 -> 42.23 (-0.8%) | 43.50 -> 43.08 (-1.0%) |
| 9 (control: the step declines) | 51.02 -> 51.21 (+0.4%) | 52.25 -> 52.20 (-0.1%) |

- 0.33-0.45 ms per forward at 1, 3 and 7 rows, faster in all 18 boot-width pairs: the 36 prework dispatches and their
  gaps (4668 -> 4055 graph ops per one-row forward, 6903 -> 6255 at 7 rows; 7287 both at 9).
- 5 rows at 4096 keys climbs 1.5-2.7 ms across its four passes in every boot whatever the arm (the meter's context
  grows ~430 keys per pass there), a step the pass order cannot cancel; the same width reads -1.9% in 3/3 boots at
  32768 keys.
- llmprobe 0.6.12 `--bench-only`, A B B A, `--no-mtp`: decode 64.6 / 67.0 / 67.3 / 66.1 tok/s (+2.8%, both B boots above
  both A boots); prefill, first token and the 0.5-16k rungs within noise.
- Default MTP, A B B A: decode 93.5 / 90.4 / 91.0 / 90.4 at 4.92 / 4.92 / 6.40 / 5.82 tokens per step; each boot's
  planner learns its own costs, so this cell is variance. Forced depth 3, one boot per arm, 6 prompts x 256 tokens:
  588.4 -> 588.3 tok/s summed (serial 393.0 -> 396.3 in the same boots); a ~0.4 ms saving is ~1% of a round.
- Identity: those 6 prompts serial and at forced depth 3, A == B 12/12 and MTP == serial 6/6 per arm;
  `test_mtp_equivalence.sh` 18/18 on B, lookup rounds engaged on its copy task.

<a id="qsa-pool-rope"></a>
## Flash-Next: QSA pooled-key upkeep in one kernel (ad5e6be8 vs the port)

A = ad5e6be8 (sha256 bb649a00…), B = ad5e6be8 + the port (8785d33b before the squash, sha256 15ffa4be…), ReleaseFast.
M5 Max 128 GB, 2026-09-27, Sushi-3bpw, kv8, `--mtp`, `SUSHI_ROUND_COST_PERSIST=0`, MTP prompt lookup on in both arms,
`taskpolicy -a`, GPU lock `qsa-pool-ab` per boot, fans at max per boot with 10 s idle (3 min before greedy B, which
followed a KLD run at 90.6 °C) and restored to auto after; load 1.9-3.5 with other workers' builds beside.

Decode meter on B, the composed chain and the kernel alternated A B B A per width in one process
(`SUSHI_DECODE_FWD_UBENCH=48 _S=1,3,5,1,3,5 _QSA_POOL_ARMS=1`, ctx 131072), one boot per length, two rounds per
width; the context grows ~7.3k keys over a boot. ms per forward:

| keys prefilled | rows | chain | kernel | delta | per round | graph ops |
|---|---|---|---|---|---|---|
| 16384 | 1 | 18.21 | 18.03 | -1.0% | -0.2, -1.8 | 4055 -> 4013 |
| 16384 | 3 | 26.65 | 26.72 | +0.3% | +2.1, -1.5 | 4490 -> 4364 |
| 16384 | 5 | 37.04 | 36.88 | -0.4% | +0.2, -1.0 | 4938 -> 4774 |
| 65536 | 1 | 18.88 | 18.55 | -1.7% | -1.4, -2.1 | 4055 -> 4013 |
| 65536 | 3 | 27.27 | 27.17 | -0.4% | +0.4, -1.1 | 4490 -> 4364 |
| 65536 | 5 | 37.90 | 37.25 | -1.7% | -1.0, -2.4 | 4939 -> 4774 |

- 9 of 12 rounds favour the kernel. The 65536-key 1- and 5-row cells agree in both rounds (-0.33 and -0.65 ms, all
  GPU eval); the 16384-key cells sit inside the chain arm's own block-to-block spread (0.6-1.9 ms). CPU graph build
  moves under 0.04 ms. The kernel launched 300-1224 times per fused block, 0 per chain block.
- Identity: 6 prompts of 3.1-21k tokens x 256 tokens, serial and at forced depth 3, A == B 12/12, MTP == serial 6/6
  per arm, identical per-request acceptance (prompt lookup stays off under a forced depth).
- Not attributed: forced depth 3 summed 525.6 -> 593.4 tok/s (serial 353.6 -> 355.4) and llmprobe 0.6.12
  `--bench-only` (ctx 1048576) decode 92.3 -> 95.2, prefill 1475 -> 1674, one boot each. Both A boots started hotter
  (72.8 / 73.6 °C die vs 90.6-then-3-min / 53.2 °C); the kernel is ~1% of a round and cannot reach prefill by 13%
  (the recorded v1.0.4 cell reads 1671).

## Sushi-2bpw streamed decode on an M1 Max 32 GB (`--ssd-budget-gb 20`, kv8)

Greedy 300-token decode of one prompt after a warm-up (tok/s; same box, arms run back to back):

| change | exact | `--expert-pick-tolerance 0.2` |
|---|---|---|
| v1.1.1 (per-layer id sync) | 12 | - |
| async expert hand-off + early shared expert | 14.1 | 15.3 |
| experts queued from the GPU cache map before the id read | 17.9 | 15.3 |
| deferred verification across one GDN successor | 19.1 | - |
| lossy pick on the GPU, deferral in tolerance mode | 19.3 | 22.3 |

llmprobe `--bench-only --rungs 512,2k` (ctx 32768): v1.1.1 10.3 / 9.4 tok/s decode, this branch exact 19.6 / 17.1,
tolerance 0.2 at 2k 18.9. A 3869-token prompt (QSA engaged), 200 greedy tokens: v1.1.1 8.7 / 9.1 (cold / warm), this
branch 15.2 / 16.9. Exact output is byte-identical to v1.1.1 on every single-stream check, and `kld compare` against the
exact teacher reads KLD 0 (NLL 0.4004 unchanged). Two concurrent streams can differ from a single stream by arrival
order alone (batched and solo decode are not bit-identical); v1.1.1 does the same.

Anatomy of an exact token at the end (~52 ms): GPU work ~31 ms (MoE ~13, GDN ~9, HC ~5, attention ~3); SSD fills
~0.63 ms per missed expert, ~18 misses a token (fill tuning: splitting reads or more than 4 workers is slower);
the rest is host round trips on full-attention/PLE layers and rollbacks. Tolerance 0.2 cuts misses to ~7 a token.

<a id="m2max-64gb"></a>
## Flash-Next Sushi-3bpw on an M2 Max 64 GB

A second box: M2 Max, 38-core GPU (no NAX), 64 GB, `iogpu.wired_limit_mb=58000`, `--mtp --skip-mem-preflight` (the
load check refuses with apps open), `taskpolicy -a`, the lock held per run, NOT a quiet box (desktop apps open, 6-13% memory free, swap in use).

- Before the simdgroup-matrix body (f42cd8e): prefill 28-38 tok/s at 3.1-3.9k; omp's first turn, 13,194 tokens,
  prefilled at 19.1 tok/s (11.5 min) and decoded 751 tokens at 19.7 tok/s.
- Metal System Trace over a 2.5k prefill on f42cd8e (`--instrument 'Metal GPU Counters'`): the scalar sorted EXL3
  GEMM took 90.7% of sampled GPU time (gate/up 59.5%, down 31.2%), MLX's bf16 GEMM 5.1%, everything else 4%.
- The simdgroup-matrix body against the scalar body, one process (f22a383 + the body), A B B A, paired per-block ratio,
  window table built outside the timed calls (`SUSHI_EXL3_LAYER_UBENCH=1`, MCG w15, 512 experts, top-10):

| prompt chunk | n48 2560->640 | n48 640->2560 | n64 2560->640 | n64 640->2560 |
|---|---|---|---|---|
| 17 tokens | 3.2x | 5.4x | 3.1x | 4.3x |
| 64 tokens | 3.5x | 5.0x | 3.5x | 4.8x |
| 205 tokens | 4.6x | 6.2x | 4.7x | 5.9x |
| 2048 tokens | 22.2x (241.0 -> 10.9 ms) | 19.7x (323.9 -> 16.9 ms) | 19.2x (343.8 -> 18.1 ms) | 17.8x (354.0 -> 20.3 ms) |

- End to end, f22a383 against f22a383 + the body, B A A B with a distinct 3.4-4.0k prompt per run: 123.8 / 30.9 /
  31.7 / 140.8 tok/s, 4.0x and 4.4x per pair. Disk reads rose from ~1,200 to ~5,000/s: the n-gram walk's share grew.
- llmprobe 0.6.12 `--bench-only` (`tests/bench.sh --url`), both arms `--mtp --skip-mem-preflight` with
  `QWEN4_PLE_PREFETCH_PREFILL=1`, B A A B, one boot per cell, AC power. Prefill at 2k 344.8 / 37.6 / 38.6 / 340.5 tok/s
  (9.2x, 8.8x per pair); first token at 16.3k 45.8 / 418.5 / 418.8 / 47.7 s; decode 32.7 / 34.1 / 33.8 / 32.5 tok/s
  (the change cannot reach decode: rows <= 16 take the decode chain; MTP 2.9-3.9 tokens per step across cells).
- Quality, teacher-forced against f42cd8e's own capture (16 doc paragraphs x 256 tokens, kv8): the body scores KLD
  0.0406 / top-1 91.0% / NLL 0.7830; a rounding-only control (f42cd8e with `SUSHI_FUSED_256=0`) 0.0347 / 90.8% /
  0.7831; f42cd8e itself 0.0000. On this pack any bit-different kernel reads about 0.04 against another.
- `QWEN4_PLE_PREFETCH_PREFILL=1` (pool) vs the default serial walk on f22a383 + the body, A B B A, a distinct 3.5-4.3k
  prompt per run: 165.2 / 378.7 / 353.1 / 224.9 tok/s, the pool 2.29x and 1.57x per pair. (On f42cd8e, with the
  scalar GEMM dominating, the same screen read 14-24%.)
- The n-gram residency gate pools by default on that box: its 29.8 GB table cannot stay beside 47.6 GB of weights in
  64 GB. Default flags, one boot per cell, a distinct 3.4-4.4k prompt per run, `max_tokens` 1. 8c16b2b against 8c16b2b +
  the gate, A B B A: 189.8 / 393.6 / 395.7 / 219.6 tok/s, 2.07x and 1.80x per pair. Before the body, f22a383 against
  f22a383 + the gate, A B B A twice: 33.9 / 36.6 / 31.4 / 34.8 and 30.7 / 37.0 / 37.4 / 35.1 tok/s, paired 1.08,
  0.90, 1.21, 1.07 (mean 1.06, inside the prompt-to-prompt spread).

### v1.1.0 release gate

`tests/bench.sh --tag v1.1.0 --only sushi-4bpw` (Sushi-4bpw, llmprobe 0.6.12 `--bench-only`, MTP) on the release tree
(67b794f3 plus the version bump), M5 Max, `taskpolicy -a`, lock `release-110`, fans max, no build or test running:
decode 89.4 tok/s (87.5-90.2), prefill 2139 tok/s, 5.33 tokens per step, 37/37 requests `mode=mtp`. Against v1.0.5:
decode +8%, prefill +19%.

### v1.0.5 release gate

`tests/bench.sh --tag v1.0.5` (Sushi-4bpw, llmprobe 0.6.12 `--bench-only`, MTP) on the release tree (1ea9492a plus the
version bump), M5 Max, `taskpolicy -a`, lock `release-105`, fans max, no build or test running (load1 1.74):
decode 82.6 tok/s (82.1-89.9), prefill 1796 tok/s, 5.33 tokens per step, 37/37 requests `mode=mtp`. The v1.0.4 column
has no Sushi-4bpw cell.

### v1.0.4 (27ca1c86), user-reported

A user ran the release on their own M2 Max 64 GB: llmprobe 0.6.12 against port 1234, default ladder,
warmup + median of 3, greedy, thinking on (reasoning medium), MTP engaged. Launch flags, QoS, lock and box state were not
reported, so this is a reference point, not a controlled cell.

| prompt | decode tok/s | first token | prefill tok/s | tokens per step |
|---|---:|---:|---:|---:|
| 2041 (headline) | 38.6 (38.5–38.8) | 866 ms | 399 (398.9–401.2) | 2.91 |
| ~512 | 31.4 | 2.0 s | 258 | 2.67 |
| ~4.2k | 34.2 | 10.4 s | 410 | 3.15 |
| ~8.2k | 36.9 | 20.0 s | 413 | 2.95 |
| ~16.3k | 37.8 | 39.8 s | 409 | 2.74 |

- Speculation 1.38x (predictable 46.4, novel 33.5 tok/s); prefix cache 6.8x (4.1 s cold, 607 ms warm, 1509 of 1540
  tokens cached); 4 streams 39.9 tok/s aggregate vs 26.6 alone (0.38 efficiency); sustained 38.6 -> 36 tok/s over 4 m 5 s
  (-6.7%).

<a id="m2max-decode"></a>
### M2 Max decode attribution (8c16b2b)

Decode meter (`SUSHI_DECODE_FWD_UBENCH`, 4096 keys of context, no sampling around the forward), `--mtp
--skip-mem-preflight`, `taskpolicy -a`, lock per boot. A verify row costs ~16 ms, ~47% of a 1-row forward (the M5 Max
reads ~26%), so MTP nets ~1.1-1.35x here. ms per forward at 1 / 2 / 4 / 7 rows, f22a383: 36.1 / 51.3 / 80.6 / 131.4.
"HC grouping" below is the row-grouped HC read that landed as #6 (PR head 3f97e4f), applied on the named commit.

- `QWEN4_STANDIN` sweep on 8c16b2b + HC grouping (baseline 33.4 / 127.3 then 34.0 / 129.3 ms at 1 / 7 rows),
  what each stand-in removes: whole MoE 9.4 ms at 1 row and ~8.6 ms per extra row (experts ~6.4 of it by ablating
  `moeExl3`, shared expert ~1.4); GDN 8.7 / ~3.0 (the projections, not the recurrence); attention 6.7 / ~2.0; HC 4.2 /
  ~2.5. The `gdn_proj` and `moe_router` stand-ins cost more than what they replace: unusable as ablations.
- The expert decode chain alone, 12 chained layers per eval: 149 us per layer at 1 row, 730 at 7 (+97 us per row per
  layer). 12 or 40 distinct layers' weights (11 / 38 GB) read the same as one layer reused: not TLB or working set.
- MLX affine-8 `qmv` chained in one graph: 244-259 GB/s of weights at 12288x2560, 2560x6144, 2560x2560 (~65% of peak).
  The row-identical `mtp_qmv` kernel reads within ~10% of serial `qmv` and batched `quantized_matmul` at 2-7 rows.
- HC reads grouped by row (weights read once per group, configs cached per width), bit-identical to f22a383 in 24/24
  cross-boot greedy comparisons, MTP on and off, A B B A: 1 / 2 / 4 / 7 rows 36.1 / 51.3 / 80.6 / 131.4 and 35.4 /
  49.9 / 79.1 / 129.5 -> 35.1 / 49.0 / 77.7 / 127.4 and 36.3 / 49.1 / 78.6 / 128.4 ms, 2-3% at verify widths and
  nothing at 1 row. On 8c16b2b it passes `test_mtp_equivalence.sh` 11/11 and its MTP output equals 8c16b2b's.
  End to end on 68b6f9f, greedy, 4 prompts x 256 tokens (code / list / prose / story), decode tok/s: default adaptive
  MTP, two A B B A blocks, 131.8 -> 136.0 and 132.1 -> 135.5 summed (+3.2% / +2.5%, 28/28 outputs identical);
  forced depth 3, 43.75 / 38.15 / 28.3 / 25.3 -> 44.75 / 39.15 / 28.35 / 25.6 (+1.7%, faster on every prompt).
- `SUSHI_MTP_DENSE_ROWS=1` read another 2-3% (35.6 / 49.3 / 78.0 / 129.1 and 35.6 / 49.4 / 78.3 / 127.0 -> 35.7 / 47.5
  / 76.1 / 125.0 and 35.2 / 48.2 / 75.8 / 125.5 ms). One `test_mtp_equivalence.sh` run of 8c16b2b + HC grouping +
  dense rows failed 3/11 (the story prompt left `--no-mtp` at output token 16, top-2 gap 1.125 nats; that boot decoded
  at 12 tok/s, under load). Seven reruns passed 11/11: the same commit, dense alone on 8c16b2b and 68b6f9f, router or
  gate alone, and with HC grouping on 68b6f9f. The one wrong value is unexplained, so dense rows stay off by default.
- Ruled out: a vectorized affine-8 reader (one uint2 of codes and two vec4 activations per lane, 8 rows per simdgroup),
  bit-identical to per-row `qmv`. In a chained in-graph ubench it read 10-57% faster on 6k-12k x 2560 and 2560 x 6144
  at 1-3 rows. On the decode meter (8c16b2b + HC grouping + dense rows), A B B A off / on / on / off: 33.96 / 48.59 /
  74.97 / 125.81, 34.65 / 49.87 / 78.21 / 127.24, 34.94 / 47.42 / 76.28 / 124.08, 33.85 / 46.26 / 74.55 / 124.52 ms,
  2-4% slower at 1-4 rows.
- Expert overlap between verify rows on `test_mtp_equivalence.sh` traffic (`SUSHI_EXL3_UNION_HIST`): 20 / 28 / 31% of
  routed slots repeat an expert at 2 / 3 / 4 rows, 41-47% at 6-8. Ruled out all the same: MiMo's grouped gate/up GEMV,
  byte-identical at the qwen geometry, on 68b6f9f at forced depth 3 / 5, greedy, 4 prompts x 256 tokens, A B B A:
  decode -0.8% / -1.3% (code / list / prose / story 43.4 / 37.9 / 27.1 / 24.5 -> 43.0 / 36.9 / 27.5 / 24.5 and
  46.3 / 35.3 / 22.8 / 18.7 -> 45.6 / 34.9 / 22.4 / 18.5 tok/s).
- The sampled shader profiler misattributes decode (HC read 13% of sampled time at 7 rows, ~2.5 ms of ~130 by ablation);
  attribute by stand-in or ablation, never by samples.

<a id="ngram-arm"></a>
## The n-gram gather arm is measured per load (feb9ed7d)

M5 Max 128 GB, macOS 27, Sushi-3bpw (29.8 GB table) and Sushi-4bpw (95.4 GB), `--mtp --kv-quant 8
--mtp-head-kv-quant --ctx-size 128000 --prefix-cache-disk 20GB --prefix-cache-entries 1 --prefix-cache-mem 1GB`,
llmprobe 0.6.12 `--bench-only --rungs 4k,16k --runs 1` (one sample per cell, no median), `taskpolicy -a`, GPU lock held
per arm, NOT a quiet box (no §4b fan protocol available). `SUSHI_FORCE_GPU_FAMILY_FALLBACK=1` for the non-NAX rows.

| arm | calibration read | picked | 2041 tok | ~4.2k | ~16.5k |
|---|---|---|---|---|---|
| 3bpw NAX, table warm | serial 0.67 ms, pool 4.14 ms (0.2x) | SERIAL | 1811.7 | 1890 | 1918 |
| 3bpw NAX, `SUSHI_NGRAM_WARM=0` | serial 20.94 ms, pool 1.88 ms (11.2x) | POOLED | 1535 | 1616 | 1709 |
| 3bpw non-NAX, table warm | serial 0.63 ms, pool 3.75 ms (0.2x) | SERIAL | 1130.9 | 1134 | 1131 |
| 3bpw non-NAX, pool forced | warm at the time of the probe | POOLED | 1015.9 | 990 | 1048 |
| 4bpw NAX, warm declined by the cap | serial 11.45 ms, pool 1.02 ms (11.3x) | POOLED | 1757.1 | 1891 | 1954 |
| 4bpw NAX, warm FORCED past the cap | serial 11.01 ms, pool 1.24 ms (8.9x) | POOLED | 1621.6 | 1620 | 1622 |

- **The arm flips with the state, so it cannot be a constant.** A cold table reads 7.7-11.3x for the pool; a warm one
  5-10x for serial. Forcing the pool on a warm table costs 6-13% of prefill; a serial walk on a cold one costs
  7.7-11.3x in gather time, so the 20% margin leans to the pool.
- **Forcing the 4bpw warm is 8-17% WORSE and buys nothing**: the pread finished all 95.4 GB in 7.9 s, the calibration
  read cold either way (11.45 vs 11.01 ms), because 95.4 GB of table plus ~57 GB of weights does not fit in 128 GB. The
  residency cap already declines it; that is the rule earning its keep.
- `SUSHI_NGRAM_WARM=0` on the 3bpw cost 11-15% of prefill here, but that arm ran against a partly warm page cache:
  `WARM=0` skips the pread, it does not evict. A genuinely cold 3bpw end-to-end cell is still unmeasured, and this box
  cannot produce one (49 GB of weights + 30 GB of table fits inside 128 GB, so nothing is evicted) — a 64 GB box
  would.

## Upstream comparison (decided: no rebase)

- Rebased onto upstream vs main 6755ff2 on the MCG K3 pack, interleaved: MTP
  decode 88.2/81.9 vs 82.0/83.1, MTP off 62.1 vs 60.4, prefill 1643 vs 1598: neutral. Four streams with MTP: 85
  aggregate both ways (our merged-verify decline past width one holds).
- kv8 A/B on the affine 4/8 pack: upstream's `qkvAttnMppKernel` engaged
  zero times (QSA caps keys at 2048); all differences were run-to-run and spec variance.
- Decision: main stays; upstream's kv8 attention kernel and grouped MTP are cherry-pick candidates later, each with
  its own certification.

<a id="qsa"></a>
## QSA wide verify (b89991a)

Per 12-layer forward on a packed kv8 cache, S=16: 42.2 → 3.8 ms at 8k keys, 13.4 → 4.1 at 64k, 24.0 → 4.1 at 256k.
Prefill gather over the rebuild beats the mask arm at every width (8k keys, S=1024: 227 → 81 ms). Live MCG
Flash-Next kv8: 68k prompt byte-identical, 33k diverges at a 0.125-nat near tie; prefill 1740 → 1763 tok/s (33k).
Declined: packed reads for wide long-context chunks (15-20% slower). Flash-Next attention + indexer is 6-13% of decode
and 4-8% of a depth-3 verify; a matmul2d QSA prototype (branch `qsa-mpp-proto`) cuts the attention part 30-45%
(~2% of a token) and is parked.

<a id="mimo-decode"></a>
## MiMo

Its n36 experts reached the fast decode arms only with the rate-generic readers:
[exl3-rate-generic](#exl3-rate-generic) (live decode 35 -> 61 tok/s with MTP).

<a id="mimo-longctx"></a>
### MiMo Sushi-2.25bpw long-context ladder, MTP (3d11b0f7)

One boot, one request per rung (the source-tree prompt ladder, "explain the code"), 256 tokens, T=0,
`--ctx-size 1048576 --kv-quant 8 --mtp`, info log, `taskpolicy -a`, GPU lock, fans max, 2026-09-28. Decode / prefill in
tok/s; tok/step = 1 + accepted drafts per round; stalls = rounds over twice the median at their width.

| rung | decode | prefill | TTFT | tok/step | stalls |
|---|---|---|---|---|---|
| 4k | 55.7 | 839 | 4.9 s | 2.44 | 5 (214 ms) |
| 8k | 58.9 | 1119 | 7.4 s | 2.61 | 1 |
| 16k | 56.4 | 1086 | 15.2 s | 2.42 | 0 |
| 32k | 57.0 | 949 | 34.6 s | 2.39 | 2 |
| 64k | 44.4 | 816 | 80.4 s | 2.17 | 1 |
| 128k | 43.2 | 661 | 198.5 s | 1.92 | 0 |
| 256k | 31.8 | 449 | 584.6 s | 2.17 | 0 |

- Baseline, main 1ea9492a on the same prompts: 57.2 / 55.5 / 54.8 / 57.4 decode at 4k-32k; its 64k and 256k
  cells were contended (18.0 and 22.6). The slope past 32k is the global layers' attention per verify row.
- The stalls came with other agents' builds and fan changes on the box: a quiet box read 0 on every rung, both
  before the pool fix (typical-sampling boots below) and after it (64d9c341: 63.8 / 60.8 / 55.8 / 48.9 / 39.9 at
  4k / 8k / 32k / 64k / 128k, 0 stalls, 07:38).
- Typical 0.2 at T=1.0, top_p 0.95, seed 7, two boots back to back after 3 min idle: `--mtp-greedy-tail` (90f8bf7f)
  vs without (3d11b0f7) decodes +15.1 / +12.6 / +13.2% at 16k / 32k / 64k (tok/step 3.51 / 3.51 / 3.12 vs
  2.78 / 2.72 / 2.69), with GPU clocks within 1-5% and prefill within 1-8%; at 4k / 8k the clocks differed 14-19%.

<a id="mimo-ladder"></a>
### MiMo-V2.6-Flash-Sushi-2.3bpw, 4k-128k context ladder (1a7f92d1)

Pack `MiMo-V2.6-Flash-Sushi-2.3bpw` (MCG K2.25 w12, last layer K4), binary built from `1a7f92d1` with MLX `d73eb752`
(SHA-256 `a4dd6fe7`), `--ctx-size 1048576 --kv-quant 8 --mtp`, llmprobe 0.6.12 `--bench-only --rungs
4k,8k,16k,32k,64k,128k --timeout 3600`, `taskpolicy -a`, lock `bench-mimo23-ladder`, quiet box, fans at max from a
49 °C start, 2026-09-30. Headline cells: decode 70.0 tok/s, prefill 1132 tok/s at 2k, first token 327 ms, MTP 3.69
tokens per step (predictable 80.5, novel 50.1). llmprobe saw a 12.1% sustained-load slide (70.0 -> 61.5) over the 12 min
run. Each rung is one run.

| context | decode tok/s | prefill tok/s | first token | tokens per step |
|---|---|---|---|---|
| 4k | 65.2 | 1091 | 3.8 s | 2.78 |
| 8k | 60.1 | 1134 | 7.3 s | 2.09 |
| 16k | 60.7 | 1100 | 14.8 s | 2.78 |
| 33k | 61.8 | 1025 | 31.9 s | 2.56 |
| 66k | 57.3 | 894 | 73.4 s | 2.46 |
| 131k | 52.0 | 715 | 183.3 s | 3.05 |

At `--ctx-size 1048576` the pack loads (preflight 96.7 of 102.9 GB) but admission then has 12.9 GB left, under the
16.76 GiB bill of a 1M session at kv8. That boot ran at the default GPU limit; at `iogpu.wired_limit_mb=120000` the
admission bill of a full 1M prompt (weights, 512-rung bill, 1 GiB hot cache) is 101.4 GiB and fits, 94.3 GiB at kv4
(probe on `83dc9b6c`, the method of [engine-memory-admission](engine-memory-admission.md)).

<a id="mimo-verify-2p3"></a>
### MiMo Sushi-2.3bpw decode forward: attribution and verify-row levers (2f15cc97)

Forward meter (`SUSHI_DECODE_FWD_UBENCH`), kv8, `taskpolicy -a`, GPU lock, 2026-09-30, box busy with other builds
(absolute numbers read high; compare arms only). Base 2f15cc97: 20.80 ms at 1 row, verify rows 2 / 3 / 4 = 28.54 /
34.85 / 41.47 ms at 1024 keys.

Where a 1-row forward goes (in-process stand-ins: each family replaced by a view of its input, same boot, 1024 keys):
routed experts with their router ~8.3 ms, FP8 QKV ~5.3, affine-8 o_proj ~3.3, lm_head ~1.2-1.5, attention ~1.2, the
rest (norms, layer-0 MLP, embed) ~1.5. The routed experts are ALU-bound (2.7 GB in ~8 ms) and grow ~6.7 ms per extra
verify row; the trunk GEMVs are flat in rows in isolation (FP8 direct GEMV ~128 us per sliding layer at 1-4 rows;
the shipped NR 1 / SGS 2 was the best of NR 1-4 x SGS 1-8) and run at ~470-480 GB/s.

Three row levers, each row still its decode tick's bytes: a sliding layer's verify rows in ONE dispatch
(`mimoSlidingRowsAttn`, a port of MLX's `sdpa_vector` with one threadgroup per head and row), the router's rows in
one f32 gemv (`mtp_qmv.f32GemvRows`, MLX's M=1 gemv per row), and the affine-8 row kernel at two output rows per
simdgroup on the unrolled path. Per-forward alternating A/B in one boot (arms interleaved forward by forward, n=60
per arm, 160 keys = the llmprobe decode cell's context), median ms:

| rows | no lever | all three | sliding rows | router rows | affine-8 two rows |
|---|---|---|---|---|---|
| 2 | 29.29 | 28.47 | -0.38 | -0.11 | -0.45 |
| 3 | 38.86 | 37.35 | -0.72 | -0.23 | -0.61 |
| 4 | 49.17 | 47.09 | -0.91 | -0.06 | -0.50 |

Per-lever columns: paired mean of all three on minus that lever off (negative = the lever saves). Primitives per
4-row forward 5704 -> 4978.

The pair GEMV on a once-prepared input (`pairGemvPrepared`, lane-ordered half4 reads), same meter and settings, arms
prepared / self-preparing: 2 rows 27.72 / 28.14, 3 rows 33.66 / 34.55, 4 rows 40.70 / 41.85 ms (paired -0.43 /
-0.86 / -1.21). One row keeps the fused prepare: there the extra dispatch lost 0.08-0.10 ms. In the chained kernel
microbench (47 layers, E=256, ~19 of 32 slots unique at 4 rows) the pair step went 315 -> 296 us at 4 rows without the
lane order; decode ablations at 1 row: the MCG decode stand-in -12%, no weight loads -16%, constant inputs (no prepare)
-11%, decode and loads both removed -43%; groups of three or four members spill (1.7-2x slower) and unroll_count(2)
on the pair GEMV's k loop is 5-7% slower.
The affine-8 rows kernel at four rows per simdgroup and four simdgroups beat two rows / two simdgroups by 18% on
lm_head in an isolated 12-copy microbench but lost 0.5-1.1 ms per forward; judge it by the meter.
Greedy chat, 4 prompts x 320 tokens, kv8: base, the three levers and the prepared pair byte-identical, serial and MTP,
and MTP == serial on each.

Measured and not taken (same meters): a dependent dispatch costs ~1.7 us in the live graph (`SUSHI_DISPATCH_PROBE`
0 8 8 0: +376 dispatches, +0.6-0.7 ms at 1 and 4 rows; a forward has ~1150), so a one-dispatch fusion is worth
~k x 48 x 1.7 us; MLX command-buffer commits cost nothing (`MLX_MAX_MB_PER_BUFFER=1000000`, ops 400: within boot
noise); a global layer's verify rows in one dispatch below 1024 keys (paired -0.05 / -0.15 / +0.01 ms at 2 / 3 / 4
rows); the FP8 direct GEMV loading two chunks ahead (+0.2 ms at 4 rows); the pair at one output tile per threadgroup
(faster only with heavy row sharing, 1% slower at 24 of 32 unique slots); pair K splits 1 / 4 / 8 (2 is best);
half2 FMA on the decoded weights (lossy; +5% at 1 row, -4% at 4 rows).

Rope + kv8 quantize in one dispatch (`mimoDecodeQkvPrep`, on top of the levers above, same meter, 30 forwards per pass,
`_QKV_PREP_ARMS` off/on/on/off twice in one boot): 1 row 21.23 -> 20.95 and 21.51 -> 21.26 ms (-0.27 ms, -1.3%);
4 rows 39.57 -> 39.82 and 42.21 -> 41.35 ms under a 5 ms upward drift across the passes (-0.3 ms mean, not
resolved). Primitives per forward 3420 -> 3040 at 1 row, 4766 -> 4386 at 4. Two more boots at 16 passes each
(four off/on/on/off sets): 2 rows -0.45 / -0.20 / -0.38 / -0.40 ms per set (-0.36 ms, -1.3%); 4 rows +0.30 /
-0.34 / -0.72 / -0.65 ms, the first set inside a 3 ms warm-up rise (-0.35 ms mean, -0.57 without it).

<a id="mimo-prefill-nax-body"></a>
### MiMo 2.3bpw prefill: where a 2k chunk goes, and the branch-free NAX GEMM body (2f15cc97)

One cold 2025-row chunk (the llmprobe 2k cell minus its cached prefix and final row), in-process prefill meter
(`SUSHI_PREFILL_UBENCH`), `--kv-quant 8 --mtp --ctx-size 1048576`, `taskpolicy -a`, GPU lock, busy box (other
workers building), 2026-09-30. Before this change: 1590-1624 ms (1247-1263 tok/s). A Metal System Trace put the GPU
busy 98.6% of the forward, so host graph build and syncs cost nothing. A per-stage synced pass (+18% sync inflation)
splits it:

| stage | ms per forward | share |
|---|---|---|
| EXL3 GEMMs (gate 409, up 408, down 405) | 1222 | 65% |
| FP8 QKV (dequant + MLX bf16 GEMMs) | 264 | 14% |
| affine-8 o_proj (NAX qmm; the dq+GEMM route starts at 2048 rows) | 159 | 8% |
| router + top-k + argsort, token prepare, SwiGLU mid, sorted finish | 110 | 6% |
| rope, transposes, kv8 write | 46 | 2% |
| attention (sliding band 21, global 20) | 41 | 2% |

Real text routes unevenly: per layer the busiest expert takes 583-1400 of 2025 rows and 9-60 experts none, so a
layer runs ~640 live 32-row windows (25 rows each) and 1136 16-row MMA blocks (12% padding).

The NAX body with clamped x rows, the unswitched k loop and unroll 2 (bytes equal the branch-guarded body's), kernel
ubench `SUSHI_EXL3_GEMM_ARMS` (E256, n36 MCG w12, 16200 slots on five real layers' routing counts, arms interleaved,
median of 12), us per GEMM:

| projection | branch-guarded body | this body |
|---|---|---|
| gate/up 4096->2048 | 7539-7872 | 5566-5850 (x0.73-0.75) |
| down 2048->4096 | 7457-8102 | 5542-5983 (x0.73-0.75) |

In-process meter, arms alternated in one boot (0 = branch-guarded body), ms per 2025-row chunk: 2081 / 1726,
1989 / 1777, 2225 / 2070 (x0.83 / x0.89 / x0.93; the box drifted slower through the run). A 4096-row chunk:
3512 / 3016, 3561 / 3043 (x0.86 / x0.85). A quieter moment read 1292 ms per 2025-row chunk on this body (1567
tok/s, the `SUSHI_PREFILL_UBENCH` meter on 2f15cc97 + this change). On the real pack and real text the two bodies'
final hidden states of a 2025-row chunk are byte-identical (0 of 8,294,400 bf16 differ, four alternated arms).

- On this body a GEMM is MMA-bound (ablations, 5.6 ms: no weight decode 5.04, no x loads 5.39, no MMA 2.70), and 12%
  of its MMA rows are 16-row padding.
- The synced profile overstated the trunk: unsynced microbenches put a sliding layer's FP8 QKV at 4.40 ms (MLX's bf16
  GEMM alone 4.04 ms, 60 TFLOPS) and the affine-8 o_proj at 2.74 ms (MLX `qmm_t_nax`, ~50 TFLOPS).

<a id="mimo-batched-decode"></a>
### MiMo 2.3bpw: concurrent streams, batched plain rows against interleaved MTP (d7a20bf9, the landed change's code)

One boot, `--kv-quant 8 --max-concurrent 4`, greedy, thinking off, 256 tokens per stream, short distinct prompts
(a 300-word story or a Python module with tests), N requests fired together; MTP on (default) against
`enable_mtp:false` per request, which now decodes as rows of one forward (`forwardMimoBatchedDecode`).
`taskpolicy -a`, GPU lock, fans max, die 78 C at start, 2026-10-01. Aggregate tok/s (tokens over wall):

| streams | prose MTP | prose batched | code MTP | code batched |
|---|---|---|---|---|
| 1 | 57.5 | 50.4 | 62.9 | 47.9 |
| 2 | 53.8 | 64.2 | 65.9 | 62.7 |
| 3 | 55.2 | 74.3 | 65.7 | 71.9 |
| 4 | 54.9 | 79.9 | 65.3 | 76.1 |

Interleaved MTP streams share the GPU round by round, so their aggregate stays where one stream is (~55 prose, ~66
code). Batched rows read most of a forward's weights once for the group: past 2 streams on prose and 3 on code
they beat it.

<a id="mimo-crowded-mtp"></a>
**Crowded MTP (7bc8e280, 2026-10-01).** Two consecutive boots of the same ReleaseFast binary, with
`SUSHI_MTP_BATCHED=0` then `1`, `SUSHI_ROUND_COST_PERSIST=0`, `--mtp --no-pld --kv-quant 8 --prefill-chunk 2048
--ctx-size 8192 --prefix-cache-entries 0 --max-concurrent 4 --metrics`. Greedy, thinking off, 256 output tokens,
identical prompts and warmup, `taskpolicy -a`, exclusive GPU lock per arm, fans max. The arm-boundary maximum sensor
reading was 75.6 C. Warm serial rates were 46.8 and 46.7 tok/s. All 14 response pairs were byte-identical; metrics
reported batch width zero with the policy off and widths three/four with it on.

| workload | streams | interleaved MTP | crowded batching |
|---|---|---|---|
| prose | 3 | 48.1 | 65.0 |
| prose | 4 | 48.7 | 70.9 |
| code | 3 | 57.4 | 64.3 |
| code | 4 | 59.0 | 69.3 |

Aggregate output tokens / request-group wall time, tok/s. Separate boots include run variation. The earlier
cache-enabled calibration had a different priming sequence (nine restored tokens versus three), so its byte strings
were not used as this comparison's oracle. The strict live gate also matched solo, crowded and streamed output.

<a id="mimo-mtp-vs-serial"></a>
### MiMo 2.3bpw: MTP against serial per KV bucket (63476cd1; the 256k boot on 0f5e7a95)

`--kv-quant 8`, MTP on (auto depth) against `enable_mtp:false` per request, greedy, thinking off, 256 tokens; the
context is the repo's Zig source in the system message, the task a 300-word story (prose) or a Python LRU cache
with tests (code). Per bucket and kind two pairs, each MTP request the first with its nonce (it restores at the
user-message mark, so its heads see a full window) and its serial twin after it; MTP == serial bytes on all 24
pairs. One boot for 4k-128k and one for 256k, `taskpolicy -a`, GPU lock, fans max, busy box, 2026-10-01. Decode
tok/s, MTP / serial (accepted drafts per round):

| context (tokens) | prose MTP | prose serial | prose ratio | code MTP | code serial | code ratio |
|---|---|---|---|---|---|---|
| 4,540 | 45.7 / 44.6 (0.71) | 43.1 / 42.5 | 1.05 | 64.1 / 66.6 (2.31) | 43.6 / 43.3 | 1.51 |
| 15,874 | 42.4 / 44.3 (0.78) | 41.1 / 40.2 | 1.07 | 59.6 / 58.0 (1.95) | 41.4 / 39.9 | 1.45 |
| 31,742 | 40.8 / 43.9 (0.76) | 38.6 / 39.5 | 1.08 | 55.2 / 54.4 (1.55) | 37.9 / 38.4 | 1.44 |
| 57,586 | 38.9 / 39.2 (0.66) | 37.2 / 36.5 | 1.06 | 50.4 / 50.6 (1.72) | 38.9 / 39.4 | 1.29 |
| 109,468 | 33.0 / 34.6 (0.68) | 35.6 / 36.2 | 0.94 | 48.8 / 46.1 (1.53) | 34.0 / 36.4 | 1.35 |
| 211,087 | 26.7 / 26.8 (0.89) | 32.7 / 32.4 | 0.82 | 39.7 / 37.1 (1.90) | 30.5 / 30.8 | 1.25 |

Prose stops paying past ~100k keys (each verify row reads every global key); code pays at every context. The
adaptive serial switch takes MiMo from 64k ([engine-mtp](engine-mtp.md#adaptive-serial)).

<a id="mimo-adaptive-serial"></a>
**Adaptive serial at 211k keys (0cfb70f2, 2026-10-01).** The frozen 256k-bucket requests above were replayed with
`SUSHI_MTP_ADAPTIVE_SERIAL=1 SUSHI_ROUND_COST_PERSIST=0`, `--mtp --kv-quant 8 --prefill-chunk 4096 --ctx-size 581632
--prefix-cache-entries 32 --prefix-cache-mem 5840MB --max-concurrent 1`; PLD on, greedy, thinking off, 256 output
tokens. Actual admission width was 4096. One boot, `taskpolicy -a`, exclusive GPU lock, fans max. All eight responses
matched the recorded baseline's bytes, prompt lengths, restored-prefix lengths and output-token counts.

| workload | recorded MTP, switch unavailable | adaptive MTP request | serial in this boot | switches |
|---|---|---|---|---|
| prose, 211,087 tokens | 26.7 / 26.8 | 32.1 / 31.4 | 30.9 / 30.8 | one per request |
| code, 211,088 tokens | 39.7 / 37.1 | 45.7 / 41.1 | 29.5 / 30.4 | none |

Decode tok/s, two requests per cell. Prose reached serial speed; code retained speculation. The historical arm is
the recorded 0f5e7a95 boot above, so its speed differences include intervening engine changes and run variation.

<a id="mimo-mtp-round"></a>
### MiMo-V2.6-Flash-Sushi-2.3bpw: where an MTP round goes (2f15cc97)

Setup: `--kv-quant 8 --mtp --ctx-size 1048576`, llmprobe's decode-cell prompt, greedy, `SUSHI_MTP_TRACE=1`, the
forward meter at 4k keys and a Metal System Trace, `taskpolicy -a`, lock held per boot, contended box, 2026-09-30.
- The round is GPU work. The GPU is busy 96.3% of a 5 s decode window. Rounds run 43-46 ms at ~3.0 tokens (1.9-2.0
  accepts of 2.5 drafts). The headline's 3.69 tok/step is llmprobe's predictable cell, not the decode cell.
- The verify trunk takes 29.2 / 36.6 / 44.1 ms per forward at 2 / 3 / 4 rows, lm_head included (1.0 ms). Its graph
  builds in 1.3-1.6 ms of CPU, hidden behind the draft chain.
- The draft chain takes ~1.45 ms of GPU per step: a head forward of 0.94-0.99 ms at 1-4 catch-up rows, plus the
  coarse readout. The head's GEMVs sum to ~0.61 ms at 550-700 GB/s; the rest is ~35 small dependent dispatches.
  Fusing two add+norms, the SwiGLU and the value scale saved nothing measurable.
- Host gaps per single-chunk round were ~1 ms: 0.27 + 0.25 ms around the verify's capture sync and ~0.4 ms after the
  argmax readback. Keeping the verify captures lazy removes the first two: the GPU goes from 96.3% to 98.3% busy, with
  one ~0.45 ms gap per round (the lazy capture and the 2-bit readout on 2f15cc97, same flags).
- Forced depth 3 vs the auto planner on the decode cell: 3.25 vs 2.74-3.10 tokens per round, within ~0-4% on tok/s.
  Forced 3 loses 11% on prose. The planner is not a lever here.
- Ruled out: dropping routed experts under 2% of a row's weight removes 2.8% of slots for +0.0029 KLD (16x512 to EOS,
  kv8); 4% removes 7.4% for about +0.007, over the gate.
- First token on llmprobe's 2k prefill cell, outside the 2025-row chunk: the final 1-token forward 24-25 ms, the ring
  checkpoint's copies 2.8 ms of host encode (prefix cache on), request plumbing ~3 ms, and round 1 (30-45 ms), which
  produces the first visible token when thinking is on (t1 is `<think>`). The final forward stays separate: it keeps
  a cold request's t1 decode-shaped, as its warm full-prefix replay's is.
- Not taken: a serial first step instead of round 1 when t1 is invisible. Round 1 runs at depth 1 there, a 2-row
  verify of ~29 ms against ~21-22 ms serial, so it saves ~7-8 ms of first token (~0.45%) and costs llmprobe's decode
  window ~0.3-0.5% for the token round 1 no longer commits. The ~0.45 ms gap after each round's argmax is readback
  wake-up, the commit, a 0.08 ms chain build and the first command buffer's encode; only a pre-dispatched next chain
  would remove it, at ~4 ms of wasted GPU per partial accept.

<a id="mimo-2p3-quiet-ab"></a>
### MiMo-V2.6-Flash-Sushi-2.3bpw: the verify-row, prefill-GEMM and round changes together (2f15cc97 base)

llmprobe 0.6.12 `--bench-only --rungs 4k`, `--kv-quant 8 --mtp --ctx-size 1048576`, `taskpolicy -a`, GPU lock per boot,
quiet box (no other job), fans at max from 44 °C, 2026-10-01, one boot per arm in the order A B C C B A. A = 2f15cc97;
B = the three sections above plus the one-dispatch rope + kv8 quantize (this change); C = B without the lazy verify
capture.

| arm | decode tok/s | prefill tok/s (2037-2038 tokens) | tokens per step |
|---|---|---|---|
| A | 64.0 / 64.2 | 1136.7 / 1119.2 | 2.82 / 3.69 |
| B | 72.5 / 71.1 (+12.0%) | 1308.3 / 1290.1 (+15.2%) | 3.62 / 3.10 |
| C | 71.1 / 68.6 | 1298.3 / 1297.3 | 3.15 / 3.62 |

- The 192-token decode requests in the server logs read 62-73 tok/s on A and 67-77 on B across both boots, so the
  gain is round time, not acceptance. B over C (+2.8%) is inside boot noise; the lazy capture's effect is the removed
  idle gaps a Metal trace shows ([#mimo-mtp-round](#mimo-mtp-round)).
- 16x512 KLD to first EOS on B, kv8: 0.086034761, the v1.1.0 value to the digit. B passes `test_mtp_equivalence.sh`
  on this pack (19/19) and the full suite.
- The decode cell's thinking is on, so the prefill handover does not move either cell here.

<a id="mimo-lmhead-shortlist"></a>
### MiMo 2.3bpw: greedy lm_head through the coarse top-32 (on d1408a57, 2026-10-01)

Decode meter (`SUSHI_DECODE_FWD_UBENCH=40`, 4096 keys, `_LMHEAD_ARMS` alternating the full head and the shortlist in
one boot, `--kv-quant 8 --mtp`, `taskpolicy -a`, GPU lock), ms per forward: 1 row 20.89 / 20.10 / 20.07 / 21.27
(full / shortlist / shortlist / full, about -1.0 ms); 4 verify rows 43.65 / 42.83 / 42.97 / 48.28 (at least -0.8 ms).
The full affine-8 head reads 0.64 GB; the 2-bit copy ~0.19 GB plus the 32-row re-score.

Greedy identity, 9 prompts x up to 512 tokens (story, code, prose, JSON, math, a tool call, code and prose with
thinking off, an explanation with thinking on), MTP and serial each, full-head boot vs shortlist boot: every answer
byte-identical (tool-call ids carry a timestamp), and MTP == serial in both boots. A `--no-mtp` boot on the trunk's
own copy returned the same bytes. The audit (`SUSHI_LMHEAD_SHORTLIST_AUDIT=1`) also covered the 16 KLD wikitext
prompts, raw, at 512 tokens. Over ~32,500 audited rows (serial ticks and verify rows) the full argmax was never
outside the coarse top-32, and the served argmax never differed. In the final boot (17,664 rows), every shortlist logit
was also bit-equal to the full head's. `tests/test_mtp_equivalence.sh` on this pack: 19 passed, 0 failed, with the
`--no-mtp` base on the shortlist.

llmprobe 0.6.12 `--bench-only --rungs 4k`, `--kv-quant 8 --mtp --ctx-size 1048576`, quiet box, fans at max, lock per
boot, A B B A (A = d1408a57, B = this change): decode 69.8 / 70.0 -> 73.8 / 70.5 tok/s (per-request medians of the
192-token decodes 69.9 -> 72.6); prefill 1307 / 1287 -> 1289 / 1305 tok/s (unchanged). 16x512 KLD to first EOS on B:
0.086034761, unchanged (the KLD tool reads the full head).

<a id="mimo-verify-8"></a>
### MiMo-V2.6-Flash-Sushi-2.3bpw: verify to 8 rows and prompt lookup in MTP rounds (this change vs main 27e81cfc)

FP8 trunk GEMV, `SUSHI_FP8_UBENCH=1` (`_SWEEP=direct` for the geometry), bf16 x, six weight copies, median of 30-60
laps, `taskpolicy -a`, GPU lock, busy box, 2026-10-01; us per call at 8 rows (5-7 rows rank the same):

| shape | staged (NR 4, SGS 8) | direct, one-row geometry (NR 1, SGS 2) | direct, NR 2, SGS 8 (shipped past 4 rows) |
|---|---|---|---|
| qkv global | 208 | 331 | 155 |
| qkv sliding | 220 | 368 | 169 |
| L0 gate/up | 256 | 439 | 163 |
| L0 down | 288 | 465 | 201 |

Decode forward meter on this change (`SUSHI_DECODE_FWD_UBENCH=40`, `_S=1..8`, 1024 keys, kv8, quiet box, lock): 21.6 /
30.1 / 38.0 / 46.4 / 54.6 / 62.1 / 72.0 / 80.0 ms at 1-8 rows, ~8.2 ms per extra row; a fully accepted 8-row round is
10.0 ms per token against 11.6 at 4 rows.

`tests/bench_mtp_lookup.sh`, greedy, thinking off, `--kv-quant 8 --prefix-cache-entries 0`, 2 reps per boot, quiet box
(other workers frozen), fans at max, 3 min idle first, lock per boot, A B B A (A = main 27e81cfc, B = this change), mean
tok/s over the four runs per arm:

| task | main | lookup + 8-row verify | ratio | lookup rounds/drafted/landed (rep0, rep1) |
|---|---|---|---|---|
| copy_verbatim | 85.0 | 104.8 | 1.23 | 66/462/439, 33/231/216 |
| rename | 84.2 | 99.9 | 1.19 | 54/378/361, 35/245/233 |
| prose | 65.5 | 67.7 | 1.03 (no lookup round; noise) | 0/0/0 |

- Greedy bytes identical across all eight runs of each task.
- Each boot's second rep runs fewer lookups (33 vs 66) at +10-18%: the rep0 prose request trains the model's round
  table to narrow MTP widths, and the gate prices the MTP chain at the plan's base width with the request's
  MTP-round acceptance.
- Lookup alone at three drafts (ae92c897, `SUSHI_MTP_LOOKUP=0|1`, A B B A, busy box) was neutral: the three heads
  already land ~3.9 tokens per round on a verbatim copy, and a three-draft lookup round (47-51 ms) costs what an MTP
  round does.

<a id="mimo-ttft-idle"></a>
### MiMo-V2.6-Flash-Sushi-2.3bpw: where the time to first token goes, and the GPU wake after idle (e2d5be76 base)

llmprobe's prefill-cell prompt (2038-2041 tokens, streamed, thinking on, `max_tokens` 8), `--kv-quant 8 --mtp
--ctx-size 1048576`, prefix cache on, `taskpolicy -a`, GPU lock per boot, fans at max, 2026-10-01.

- Outside the 2k chunk forward: HTTP, template, tokenize and slot ~3 ms; hot-cache lookup 0.03 ms; the one-row
  final forward 19-25 ms; generator setup and the ring checkpoint 4-5 ms; round 1 28-33 ms (t1 is `<think>`, so the
  first visible token needs it).
- The live chunk equals the in-process meter's in the same GPU state. Meter passes alternated in one boot (ms, median
  of 4): plain 1589, MLX pool cleared before each forward 1572 / 1600, every row's hidden captured 1573 / 1586, both
  1587 / 1590. Neither the pool clear nor the MTP capture costs anything measurable.
- The GPU state moves the chunk by up to 1.8x. Back to back, the first ~4 s run at 1255-1276 ms, then 1460-1537.
  After 20 s idle a forward takes 1976-2285 ms. A one-element op right before it takes 598-999 ms itself, and the
  forward then runs at 1261. A tick every second keeps it at 1264-1268; ticks every 2, 4 or 5 s do not (2200-2270).
- `--gpu-warm-secs` (this change on e2d5be76), one boot per arm, 6 requests each after 10 s idle, TTFT ms: off 2284,
  2305, 1956, 2307, 2344, 1967 (mean 2194); on 1355, 1356, 1355, 1354, 1354, 1354 (mean 1355, -38%).
- llmprobe 0.6.12 `--bench-only --rungs 4k` A B B A against 9297b93b (quiet box, fans max): decode 71.6 / 69.6 -> 69.1
  / 69.1, prefill 1313 / 1306 -> 1299 / 1301 tok/s: unchanged, because llmprobe sends its cells back to back and the
  GPU never idles long enough to pay the wake.

<a id="mimo-attn-kernels"></a>
### MiMo attention kernels (attention only: no expert pack in these timings)

Prefill attention on the matrix units (`sushi_attn_pd_nax`, 2026-09-24, binary b33ec32 built 01:30, taskpolicy -a,
lock attnpd-nax; baselines on 7ed9795).
One global layer, H 64 / Hk 4, qL 2048, kv8, ms: kL 2048 8.04 -> 2.90, 4096 22.1 -> 7.18, 16384 113.0 -> 33.0,
65536 448 -> 159, 262144 2012 -> 601 (34-39 TFLOPS; the SIMD kernel 11-12). 39 sliding layers' band call, per call:
qL 512 1.96 -> 0.78, 2048 1.66 -> 0.95-0.97, 4096 2.98 -> 0.89. The SIMD kernel's K^T staging fix on M5: 2048x16384
106.9 -> 99.4 ms, 2048x65536 448-458 -> 415 ms. Per call on real prefills (4 chunks to 6k keys, carries, kv8 slices,
band + sinks) the two arms' error against an f32 reference agrees to 1e-4 relative RMS.

`sushi_attn_pd_nax` speed work (2026-09-24). Harness: a python replica of the dispatch chain, qL 4096, H 64 / Hk 4,
kv8 slices, arms interleaved in one process, `taskpolicy -a`, lock `lever2-attn`. One global layer, ms (this run read
~30% slower in absolute terms than the same arms an hour earlier, on a contended box; the interleaved ratios hold):

| kL | 08cec69 (before f16 P) | 3ba7272 (f16 P) | f16 P + lockstep causal, clamped loads (250M budget) | this kernel (+ 1e9 NAX budget) |
|---|---|---|---|---|
| 4096 | 11.02 | 10.38 | 8.85 | 7.71 (-30%) |
| 16384 | 74.4 | 72.0 | 61.5 | 59.4 (-20%) |
| 65536 | 457 | 421 | 364 | 357 (-22%) |
| 262144 | 2004 | 1930 | 1658 | 1581 (-21%) |

- At qL 2048 (the same harness), the lockstep kernel with f16 P is -20% to -23% at every kL. The 1e9 budget adds
  nothing there beyond 4k keys.
- f16 P alone buys little: -3% to -8%. The gain comes when the causal simdgroups also walk in lockstep and loads are
  branch-free. Without f16 P, those two changes gave only -2% to -8%.
- Sliding band call, per call, ms (39 layers, window 128, sinks; band keeps per-simdgroup walks): qL 512
  0.316 -> 0.299, 2048 0.576 -> 0.532, 4096 0.947 -> 0.852.
- Ruled out in the same harness:
  - a strict float P (1.4x slower);
  - an int8 correction term (costs what a bf16 one does);
  - 16x32x32 tiles (<= 2%);
  - `max_total_threads_per_threadgroup` (0);
  - fast exp2 (0);
  - 8 simdgroups (slower);
  - a larger budget on the non-lockstep kernel: slower at long kL (1e9: +3% at 64k, +13% at 256k), because K/V fall
    out of cache.

Long-context decode, global-layer attention (2026-09-24, kv8, `taskpolicy -a`, lock `lever3-kv`): `sushi_qkv_mpp` on 4
simdgroups with packed words prefetched in registers (e4dc88e, landed as a338ca2, bit-identical) against the
8-simdgroup kernel of 79a4cb4.

Attention-only µbench (9 dependent layers, us per layer, arms interleaved in one process, two runs): 16k
133-135 -> 136-139, 64k 402 -> 321-323, 256k 1697-1782 -> 1185-1252, 512k 3591-3979 -> 2398-2670. Earlier
same-session runs had main's kernel at 1468-1481 us at 256k (~243 GB/s of a ~540 GB/s read peak).
Split-K on M5 at the same shape: 155 / 528 / 1923 / 3767 us at 16k / 64k / 256k / 512k, so it never beats matmul2d
at 8k keys or more.
Ablations at 256k show where the old kernel's time went:
- dropping both matmuls left the barrier and softmax loop at 545 us with 8 simdgroups, 217 us with 4;
- the rest was the two 16-row matmul2d calls plus loads that the per-page barriers exposed.
What did not help: 64-key pages, separate K/V tiles with two barriers, vector tile stores, transposed QK, V one page
ahead, 256 splits (-3% at 256k, worse at 16k), and one merge kernel (-10 us/layer at 2-4k only, not bit-identical).
From 4k to 16k keys the new kernel costs 3-10 us more per layer, under 0.1 ms per token.
Byte identity, no PLD: greedy serial new == main on 3 prompts (4.9k / 9.8k / 18.6k tokens, 256 generated each).
Forced-depth-3 MTP == serial on the same 3 prompts, on the new kernel rebased onto 36ae6d0.

<a id="mimo-longctx-prefill-attn"></a>
### MiMo long-context prefill: what the global layers' attention costs (kernels of 819b4751)

In-process microbench `SUSHI_ATTN_PD_UBENCH=1` (test filter "MiMo prefill attention per chunk"): one chunk over a kv8
cache built by `KVCache.update`, the served `fusedSdpaPrefillKv` chain, arms interleaved, median of 5. Built on
5834210c, whose attention code is unchanged in 819b4751. `taskpolicy -a`, locks `attn-ub1` / `attn-ub2`, fans at max,
2026-10-01. The table gives ms per global layer per chunk (Hq 64 / Hk 4, qk 192 / v 128, causal); where two runs
differ, both are shown:

| keys | qL 1024 | qL 2048 | qL 4096 |
|---|---|---|---|
| = qL | 0.76 | 1.90 | |
| 16k | 15.7 | 26.8 | |
| 32k | 34.1 | 53.4 | |
| 64k | 69.4 | 108.4-108.7 | 263.4 |
| 128k | 139.8 | 239.6-261.2 | 510.0 |
| 256k | 282.8 | 540.9-554.9 | 1004.6 |

- Throughput counts only the useful causal FLOPs. It is 39-45 TFLOPS at long context, and up to 50 at 32k-64k with
  qL 2048. qL 2048 is the fastest width per row at 128k keys or fewer.
- The dequant and the fp32 carries cost little:
  - The per-dispatch dequant alone takes 0.4 / 1.2 / 2.1 / 3.9-8.2 ms at 16k / 64k / 128k / 256k keys.
  - The same dispatches over pre-dequantized bf16 K/V run 0-10% faster than the served chain.
  - One carry-free dispatch is 6-13% slower than the chained ones from 64k keys up, because K/V fall out of cache.
  - The dispatch budget is not a lever: 5e8, 1e9 and 2e9 land within ±3% of each other.
- One sliding layer's band call, ring dequant included, takes 0.42 / 0.57 / 0.90 ms at qL 1024 / 2048 / 4096. Over 39
  layers that is 16 / 22 / 35 ms per chunk.
- Kernel ablations at 128k keys, qL 2048, on the dense chain (timing-only source edits, `AttnPdNaxAblation`), ms:

  | variant | ms |
  |---|---|
  | served kernel | 240.2 |
  | K/V rows pinned to one block (L1 hits) | 238.1 |
  | K/V fragments built in registers | 200.1 |
  | Q fragments built in registers | 223.2 |
  | no loads at all (55.6 TFLOPS) | 196.3 |
  | no loads and no matmuls | 29.9 |

  So about 69% of the time is matmul issue, about 18% is load instructions (Q is reloaded every key block, and every
  simdgroup loads its own K/V fragments), and about 12% is the softmax, masks and rescale. K/V memory traffic is about
  1%.
- The load diet, measured the same way with lock `attn-fin1`, 9 samples per arm. Build: ba87fd2b's tree before a
  comment-only edit to the header, test binary SHA-256 `f6264dab`. Each fragment row is now one 8-byte
  vector load (`SushiNax::load2`) instead of four element reads. Old kernel vs new, ms per
  global layer: 26.27 -> 25.14 at 16k keys, 108.96 -> 103.40 at 64k, 244.87 -> 226.48 at 128k, 543.56 -> 520.06 at
  256k (-4% to -8%). The output is byte-identical to the old kernel on the kv8 chain at all four lengths and on the
  band + sinks call. Two earlier runs with the loads inlined read -3% to -6%.
- Load-diet attempts that were byte-identical but slower: Q held in registers +39%, which needs the d loop fully
  unrolled, and that unroll alone costs +43%. K/V staged once per threadgroup in threadgroup memory: +107%. Two key
  blocks per Q load: +10%. Unroll 2 or 6 instead of 4: +3-4%. Dropping the mid-PV barrier: +20%. The vector loads
  with unroll 3 read the same as with unroll 4.
- Prefill meter (`SUSHI_PREFILL_UBENCH=6`, rows 2048, real text) on 819b4751, lock `attn-meter2048`: median 1669 ms
  per chunk, minimum 1342. Chunks run back to back slow down ([mimo-ttft-idle](#mimo-ttft-idle)). Attention at
  2048 keys is 39 ms of that time.
- Predicted TTFT, summed over full 2048-row chunks: each chunk costs the rest of the chunk + 9 x global + 39 x band.
  The rest is 1468 ms, fit to the ladder's 16k rung, so that rung matches by construction. The ladder's measured TTFT
  is 12.9 / 28.0 / 67.0 / 172.6 s at 16.3k / 32.7k / 65.6k / 131.0k tokens.

| prompt | rest s | global s | band s | TTFT s | attention share | predicted tok/s | ladder on 819b4751 |
|---|---|---|---|---|---|---|---|
| 16k | 11.7 | 1.0 | 0.18 | 13.0 | 9% | 1265 | 1268 |
| 32k | 23.5 | 4.0 | 0.36 | 27.9 | 16% | 1175 | 1167 |
| 64k | 47.0 | 15.9 | 0.71 | 63.6 | 26% | 1030 | 980 |
| 128k | 94.0 | 69.8 | 1.43 | 165.2 | 43% | 793 | 759 |
| 256k | 187.9 | 306.2 | 2.85 | 497.0 | 62% | 527 | |

- The ladder column ran 4096-row chunks: MiMo picks its width per request, and before 2048 became its default the
  64k-256k bills admitted 4096. The 2048 on the load line was only the load-time fallback.
- 4096 vs 2048 in one boot. Build: a scratch build of 27e81cfc that caps each long request's width in turn. Lock
  `lp-abba1`, 2026-10-01, the same 63,946-token code prompt each time, prefix cache off, thinking off, kv8, MTP on,
  `taskpolicy -a`, fans at max. Prefill: 64.1 s at 4096, 70.4 and 72.6 s at 2048, then 75.5 s at 4096. Each request
  ran slower than the one before (the sustained-load clock drop), and the ABBA means (69.8 vs 71.5 s) are within
  that drift.
  - A per-chunk fit of the live trace puts global attention at 25-27% of the prefill at 2048 and 26-32% at 4096.
    Attention costs ~8% more per row-key at qL 4096, and the rest of the chunk is cheaper per row.
  - The two widths are not byte-identical: the first-token logprob is -1.0685 at 4096 and -1.0807 at 2048. Each
    width repeated its own bytes exactly.
  - 2048 is now the default ([engine-memory-admission](engine-memory-admission.md#context-and-chunk)), so the
    table's 2048 model is the served width.

<a id="mimo-verify-global-rows"></a>
### MiMo verify rows on the global layers: one page walk per row group (A6)

Arms: each global layer's verify rows one `sushi_qkv_mpp` decode dispatch at a time, against `sushi_qkv_mpp_rows`,
which runs each group's rows on one page walk (pairs, a last three in one pass). Both arms are byte-identical to the
decode ticks: unit test at 4k / 64k / 256k keys, 16/1 and 64/4 heads, page and split edges, widths 2-8 and 15, kv8 and kv4.

Attention microbench (`SUSHI_MIMO_ROWS_UBENCH=1`, 9 dependent layers, 64/4 heads, kv8, median of 5, arms interleaved,
`taskpolicy -a`, lock `attn-rows7`, branch perf/mimo-a6-verify-rows at 829187c5), us per layer, per row -> grouped.
829187c5 runs the landed commit's kernel and its groups for 2-4 rows; the landed commit only adds the groups for
wider verifies, the warmup order and test cases. Neither run pinned the fans; the arms are interleaved in one process
or boot.

| keys | 2 rows | 3 rows | 4 rows |
|---|---|---|---|
| 64k | 585 -> 482 (-17.6%) | 854 -> 744 (-12.9%) | 1112 -> 886 (-20.3%) |
| 128k | 1024 -> 857 (-16.3%) | 1519 -> 1304 (-14.1%) | 2021 -> 1683 (-16.7%) |

- All rows in one pass through `sushi_qkv_mpp` at TQ = rows is also byte-identical, but it is 17-31% slower at 3-4
  rows: its matmuls grow to 16 x rows, and each simdgroup's softmax loop walks 4 x rows rows in turn.
- The rows kernel with all four rows in one pass saves only 3-4%: four running outputs cost registers. Two pairs save
  17-20%.

Decode-forward meter (`SUSHI_DECODE_FWD_UBENCH=12`, `_S=2,3,4`, `_KV=131072`, `_GLOBAL_ROWS_ARMS=1` off / on / on /
off), MiMo-V2.6-Flash-Sushi-2.3bpw, `--kv-quant 8 --mtp --ctx-size 1048576`, binary from 829187c5 (SHA-256
`e44e8894`), `taskpolicy -a`, lock `attn-meter128`, 2026-10-01. Results are ms per verify forward at 131k keys:

| rows | off | on | change |
|---|---|---|---|
| 2 | 37.52 / 38.70 | 34.34 / 36.39 | -7.2% |
| 3 | 51.21 / 50.74 | 46.31 / 48.29 | -7.2% |
| 4 | 63.65 / 63.25 | 60.57 / 59.89 | -5.1% |

<a id="exl3-decode-layout"></a>
## EXL3 decode GEMV layout (two tiles per threadgroup)

The lane-funnel decode GEMVs (n40 MiMo, n48 Flash-Next) take two output tiles per threadgroup, two k-tiles per
iteration with both loads issued first, and pointer bumps; outputs bit-identical (see
[engine-exl3-experts](engine-exl3-experts.md#kernels)). Kernel microbench: 47 chained dispatches per round, arms
interleaved, median net of a null chain, `taskpolicy -a`, lock `exl3-decode-layout`; base = the served kernels,
recorded in the research run, new arm with sources read verbatim from the commit.

| geometry, kernel | rows 1 | rows 2 | rows 4 | rows 8 |
|---|---|---|---|---|
| Flash-Next pair (E=512, in-process old arm) | 43.9 → 36.6 | | 133.0 → 98.7 | |
| Flash-Next fused-mid down (in-process old arm) | 59.2 → 44.2 | | 95.4 → 65.9 | |

One tile per threadgroup with the unroll and pointer bumps reads the same as two on the Flash-Next pair at one row
(35.7 us) but loses at four rows (107.2) and on the fused-mid down (54.1 / 86.0), whose SwiGLU prepare two tiles
share: one policy, two tiles.

Live, llmprobe `--bench-only`, no MTP, one boot per arm, `taskpolicy -a`, lock `exl3-decode-layout`, greedy
200-token chat completion byte-identical between the arms of each pair:

| pack, flags | base | new | decode | prefill 2k |
|---|---|---|---|---|
| Flash-Next MCG K3, kv off, ctx 65536 | 61.8 recorded (28d7fab, `--full`) | this change on c4f3f7a (aff4f85) | 61.8 → 66.2 | 1763 → 1845 |

<a id="exl3-rate-generic"></a>
## EXL3 readers for every rate (Sushi-2.6bpw n42, MiMo Sushi-2.25bpw n36)

Before this change, only n40 and n48 read through the lane funnel. Every other rate decoded through the generic window
reader at one tile per threadgroup, including Sushi-2.6bpw (n42) and the shipped MiMo pack (n36). MiMo's prepared mid,
grouped verify rows and GPU window metadata were also gated on n40. Every rate below K4 now takes the funnel
([engine-exl3-experts](engine-exl3-experts.md#format-as-the-engine-sees-it)), and outputs are bit-identical.

Setup: M5 Max 128 GB, 2026-09-27. Base 7ad2f407; new = this change (branch commit 49aad597); both ReleaseFast.
`taskpolicy -a`, GPU lock `exl3-n42`, fans at max, box otherwise idle.

Kernel microbench, us per step:
- 47 chained steps; a copy kernel makes each step wait on the last, and every step draws fresh routing.
- Arms interleaved in one process, median of 11, net of the copy-only chain.

| geometry, kernel | rows 1 | rows 2 | rows 4 | rows 8 |
|---|---|---|---|---|
| Flash-Next n42 pair GEMV (E=512) | 55.8 → 34.6 | 103.5 → 58.3 | 198.9 → 110.7 | 389.1 → 213.1 |
| Flash-Next n42 fused-mid down | 32.0 → 20.3 | 59.2 → 36.9 | 112.1 → 68.9 | 218.7 → 133.1 |
| Flash-Next n42 MoE layer | 94.1 → 59.5 | 172.3 → 100.4 | 329.2 → 188.3 | 628.0 → 354.8 |
| Flash-Next n48 pair GEMV, generic → funnel (reference) | 56.4 → 30.1 | 104.6 → 49.0 | 200.5 → 92.6 | 390.8 → 175.8 |
| MiMo n36 MoE layer (E=256): base → funnel → + prepared mid, grouped | 342 → 157 → 146 | 662 → 295 → 276 | 1271 → 562 → 498 | 2437 → 1104 → 912 |

- n42's lane reads a third word, so its funnel pair GEMV costs ~15% more per step than n48's, and its down ~12% more.
- Prefill GEMM on NAX, per projection, one 2048-token chunk:
  - Flash-Next n42 (20480 slots): 3336 → 2323 us.
  - MiMo n36 (16384 slots): 10350 → 7611 us.
  - The simdgroup-matrix body (NAX forced off) at n42: 7302 → 6858 us.

Live runs:
- Sushi-2.6bpw forward meter: `tests/fwd_ubench.sh`, A B B A, no MTP.
- Sushi-2.6bpw llmprobe: one boot of the new binary against today's recorded 7ad2f407 cells.
- MiMo: A B B A, forward meter at load, then a greedy 1024-token chat twice per boot.
- All runs kv8.

| pack, run | meter | 7ad2f407 | this change |
|---|---|---|---|
| Sushi-2.6bpw, no MTP | ms/forward, 1 row | 19.74 / 19.87 | 17.87 / 17.88 |
| Sushi-2.6bpw, no MTP | ms/forward, verify 4 rows | 33.87 / 33.89 | 27.41 / 27.59 |
| Sushi-2.6bpw, `--mtp`, ctx 131072, llmprobe `--bench-only --rungs 4k,16k,64k` | decode / prefill 2k, tok/s | 79.5 / 1492 | 93.9 / 1680 |
| same | decode at 4k / 16k / 64k | 76.7 / 77.9 / 66.5 | 102.1 / 88.2 / 72.3 |
| same, the 192-token decodes in the server log | round ms at tokens per round | 35.9 at 2.83 | 28.8 at 2.74 |
| MiMo Sushi-2.25bpw, `--mtp`, ctx 131072 | ms/forward, 1 row / verify 4 rows | 28.93 / 74.33, 29.08 / 74.36 | 19.56 / 39.06, 19.59 / 39.13 |
| same, greedy 1024-token chat | decode tok/s | 36.6 / 34.9, 35.1 / 34.4 | 62.8 / 60.7, 62.4 / 60.6 |
| same | round ms at tokens per round | 49-83 at 2.05-2.55 | 39.2-39.8 at 2.30-2.48 |

- Sushi-3bpw at the same llmprobe settings read 94.1 / 100.3 decode and 1660 / 1674 prefill today, so Sushi-2.6bpw
  now matches it.
- Residual n42 cost, same session, new binary, forward meter: 2.6bpw vs 3bpw read 17.93 vs 17.80 ms at 1 row, and
  28.72 vs 27.04 ms at 4 verify rows.
- Greedy 1024-token outputs are byte-identical across arms: MiMo 8/8, Sushi-2.6bpw 4/4.


<a id="gdn-verify-fold"></a>
## Flash-Next: GDN verify epilogues in the recurrence

`92fd9b71`, ReleaseFast binary built 2026-09-28 10:43:30 (SHA-256
`e79b345002454001b49cf3b1bde86fcc407fb7b3cd7087385de4eaad86926638`), M5 Max 128 GB,
Sushi-3bpw, `--ctx-size 65536 --kv-quant 8 --prefix-cache-entries 0 --no-mtp`.
The forward meter prefills 32768 tokens, captures verify state, and runs 40 forwards
per arm with `SUSHI_DECODE_FWD_UBENCH_GDN_FOLD_ARMS=1`, widths `1,2,3,5,7,9` and
`SUSHI_ROUND_COST_PERSIST=0`. A B B A in one process: A is the existing fused
recurrence plus norm-gate and convolution concat; B folds both epilogues into it.
There was no recorded fold-only comparison on this base, so both arms were measured.

`taskpolicy -a`; GPU lock `codex-gdn-fold-bench`; fans max, 10 seconds idle from
67.4 °C, restored to auto afterward. No other model, build or unit-test job ran
beside the measurement; GUI/background processes remained active. These are
forward timings, not end-to-end generation throughput.

| Rows | A passes, ms/forward | B passes, ms/forward | Change in mean |
|---|---|---|---|
| 1 (control, no fold) | 18.289 / 18.389 | 18.411 / 18.180 | -0.24% |
| 2 | 22.641 / 22.430 | 22.206 / 21.850 | -2.25% |
| 3 | 24.942 / 25.473 | 24.942 / 25.598 | +0.25% |
| 5 | 32.265 / 32.988 | 33.919 / 33.333 | +3.06% |
| 7 | 41.969 / 42.903 | 41.894 / 42.160 | -0.96% |
| 9 (control, no fold) | 51.353 / 51.388 | 51.040 / 51.127 | -0.56% |

At widths 2–7 every B pass records 1548 folded launches (36 layers × 43 warm/timed
forwards), every A pass zero. The folded path removes 72 graph ops per forward.
Two-row B passes beat both A passes; five-row B passes lose to both A passes.
The default therefore serves two rows only. Widths 3–8 remain available to the
parity tests and timing override, not the shipping dispatch. The sub-1% cells are
not evidence of a speedup. Widths 4, 6 and 8 have parity coverage but no timing here.

Parity covers sigmoid and Swish, widths 2–8, both small and real head geometry,
all outputs and rollback states, the pipeline thread-limit fallback, and keeping
real lazy inputs unevaluated during the probe. The production-path test also
checks rollback at every acceptance position against serial decode.


<a id="qwen4-decode-ladder"></a>
## Flash-Next: PLE-safe batched decode ladder

`4f329a4a`, ReleaseFast, M5 Max 128 GB, Sushi-3bpw, llmprobe 0.6.12 on
2026-09-28. Flags: `--ctx-size 65536 --kv-quant 8 --no-mtp --no-pld --no-drafter
--max-concurrent 2 --prefix-cache-entries 0`. Off sets `SUSHI_DECODE_ASYNC_LADDER=0`;
on leaves it unset (batched stride 4, serial off). Probe flags: `--bench-only
--rungs 4k,32k --runs 3 --concurrency 2 --reasoning off --no-save`.

Boot order was off/on/on/off/off/on. Each boot takes its own GPU lock, restores
QoS with `taskpolicy -a`, sets fans to max and cools before starting; cleanup
restores automatic fan control. No builds or unit tests ran alongside timing.
The harness runs three serial samples per rung but only ONE concurrent burst per
rung per boot. These are independent boot samples, not nine bursts per arm.

The final boot was stopped at the user's request to shorten the run, after its
4K burst completed and before its 32K burst. Its completed 4K values come from
llmprobe's progress log; the other five boots have complete JSON reports. No
missing 32K value was imputed.

| Context | Off per-stream decode, tok/s (three boots) | On per-stream decode, tok/s | Median change |
|---|---|---|---|
| 4K | 35.3 / 35.7 / 35.4 | 39.9 / 39.8 / 39.1 (three boots) | 35.4 → 39.8, +12.4% |
| 32K | 25.7 / 25.9 / 25.6 | 28.7 / 28.5 (two boots) | 25.7 → 28.6, +11.3% |

At 4K, aggregate burst throughput including prefill moves from a median 42.6 to
46.1 tok/s (+8.2%). At 32K it is effectively flat (9.5 vs 9.55 tok/s): cold prefill
dominates the request wall time. No claim is made for MTP or single-stream speed.
All on samples exceed all off samples for per-stream decode at both contexts.
The five complete reports mark sustained-load drift steady; the third off boot's
short serial samples varied from 62.4 to 66.6 tok/s and were retained.

Every on boot records the N=2 stride-4 ladder engagement; no off boot does.
Correctness: synthetic eager/lazy PLE and N=2 logits/history parity, full suite
2735 passed / 93 skipped, plus the live batched-equivalence suite (short and long
serial/batched checks, concurrent streams, logprob isolation and kv8 crash guard).

<a id="exl3-gpu-routing-meta"></a>
## Flash-Next: prefill routing metadata on the GPU and the inverse-indexed finish

Main's path (arm 0: host-built window table, then an f16 copy that un-sorts the down plane) against the served path
(arm 1: the window table and inverse built in one GPU threadgroup, the finish reduce reading through the inverse),
alternated inside one boot per row by a local switch in the prefill meter (`SUSHI_PREFILL_UBENCH`): cold prefills from
an empty cache on real text. This change on 90fdedae (4096 rows, 32k) and on 27e81cfc (8192 rows), ReleaseFast, M5 Max
128 GB, Sushi-3bpw, `--ctx-size 65536 --prefix-cache-entries 0`, kv8, `taskpolicy -a`, GPU lock per boot, fans max and
10 s (die 72-83 °C), 2026-10-01; other workers' builds were not frozen. Output bytes are equal (unit test at
Flash-Next geometry).

| chunk | arm order | arm 0, ms per chunk | arm 1, ms per chunk | change of means |
|---|---|---|---|---|
| 4096 rows, median of 3 | 0 1 1 0 0 1 1 0 | 2190.0 / 2216.5 / 2233.9 / 2267.1 | 2141.2 / 2068.9 / 2128.2 / 2145.5 | -4.8% (A B B A sets -4.5%, -5.0%) |
| 8192 rows, median of 5 | 0 1 1 0 0 1 | 3738.3 / 3965.7 / 3999.5 | 3841.2 / 3690.0 / 3844.8 | -2.8% (sets -2.2%, -3.9%) |
| 4 x 8192 rows (a 32k prompt), mean of 2 | 0 1 1 0 0 1 | 18590 / 18325 / 19596 | 18964 / 19047 / 18816 | +0.6%, inside the spread |

Each chunk saves ~0.1 s at both widths (106 ms at 4096 rows, 109 ms at 8192). The saving does not grow with the
chunk, which points at the per-layer host round trip of the host-built table rather than the copy. A 32k prompt would
save ~0.4 s of ~18.8 s (~2%), below the spread of the two-sample 32k run (one arm's samples ranged 17.9 to 20.1 s). The served path also drops the un-sort buffer
(`[rows x 10, 2560]` f16: 210 MB at 4096 rows, 420 MB at 8192).

<a id="mimo-stream-pick"></a>
## Streamed MiMo: the sigmoid-probability lossy pick

MiMo-V2.6-Flash-MOPD (MXFP4 experts) streamed, `--ssd-budget-gb 60 --no-mtp --kv-quant 8 --ctx-size 65536`, M5 Max,
llmprobe 0.6.12 `--bench-only --rungs 4k`, one boot per arm on the same binary (built at cf23043d, the landed change's
pick code), `taskpolicy -a`, GPU lock per boot, fans max + 10 s, 2026-10-01:

| `--expert-pick-tolerance` | decode tok/s (min-max) | prefill tok/s @2k | ids swapped | speculated layers kept | mean fill / wall per forward |
|---|---|---|---|---|---|
| 0 (exact) | 5.8 (5.5-5.8) | 228.6 | - | 37% | 1.10 GB / 188 ms |
| 0.2 | 10.5 (10.3-11.2) | 228.3 | ~9% | 56% | 0.67 GB / 114 ms |

The exact arm matches the recorded 819b4751 streamed cell (5.5, 5.3-5.9). Per token the exact arm spends ~85 ms waiting
on the router ids and ~100 ms filling misses at ~11 GB/s; the pick turns ~9% of routed ids into cached substitutes and
cuts the fill by 40% (means over the logged decode forwards). KLD: [quality-kld](quality-kld.md#lossy-expert-pick-mimo).
