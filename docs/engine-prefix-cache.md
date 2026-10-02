# Engine: prefix cache and SSD-first

How prompt-prefix KV reuse works: the hot RAM cache, hybrid (GDN/QSA) restore points, the SSD tier and SSD-first
mode, checkouts and donations, and spec state riding the cache. Read this before touching `src/prefix_cache.zig`,
`src/kv_disk_cache.zig` or `src/kv_disk_writer.zig`.

Index: [CLAUDE.md](../CLAUDE.md#docs-index). Related: [engine-kv-cache](engine-kv-cache.md),
[engine-memory-admission](engine-memory-admission.md), [engine-mtp](engine-mtp.md),
[arch-mimo-v2](arch-mimo-v2.md#sliding-layers-the-ring).

## Code map

| File | Role |
|---|---|
| `src/prefix_cache.zig` | Hot prefix cache (`--prefix-cache-entries`, `--prefix-cache-mem`) |
| `src/kv_disk_cache.zig` | SSD tier (`--prefix-cache-disk`) |
| `src/kv_disk_writer.zig` | SSD-first mode's background writer thread |
| `src/restore_dump.zig` | Prefix-cache restore diagnostics (`tests/diff_restore_dump.py`) |

## Basics

- KV reuse via prompt-prefix matching; invalidated after tool calls + pad-only gens (`commitDeclinesPadOnly`: only an
  ALL-pad generation declines); hot cache spills to SSD; RAM invalidation propagates to disk.
- Restore ALWAYS clamps (`truncate(final_len)`); a failed restore hands back an EMPTY cache; every eviction loop has
  a no-progress exit (checked-out entries are unevictable). A restore leaves a token to forward: a one-token prompt
  prefills cold (its full hit restored everything and the empty prefill crashed upstream).
- **Media keys are a CHAIN** (`MediaSpan`: per block, a hash of its pixels, its position and every block before it;
  the entry key is the last). An entry keyed by a request's block k restores up to block k+1, so a turn that appends
  a screenshot reuses everything before it; any other key mismatch shares only the text before the first media row
  (`crossKeyBoundary`). The splice resumes at the placeholder count inside the restored prefix.
- **A restore is not bit-identical on a HYBRID** (≤ 0.047 nats; the chunking class ~0.3 nats top-5 for QSA state) ⇒
  byte-stable greedy needs `--prefix-cache-entries 0`. A hybrid cache hit moves the top logprob ~0.2 nats, so any
  scorer boots with the cache off.
- The always-on SSM snapshot sits 30 tokens BEFORE prompt end; a restored tail inside that window forwards as ONE
  span (`ssmSnapshotBackoff`). Guard: `tests/test_hybrid_reuse_equivalence.sh`.
- **A ringed (sliding-window) entry restores at its end or at one of its ring checkpoints**
  (`KVCache.ringCheckpoint`, `Entry.ring_cps`, up to `RING_CHECKPOINT_MAX` = 8): each ringed layer's
  window + 30 rows at a position (down to the window when the ring holds no more, as one restored off a checkpoint
  does); the slot's own are its restore point, its prompt end and its message marks (`SlotRingCps`). A reply longer than the
  ring's slack compacts it past where the next turn diverges (the previous reply re-renders); the checkpoint's rows
  go under the ringed layers (`restoreRing`) and the usual clamp follows.
  A checkpoint restore of fewer than `RING_RESTORE_MIN_TOKENS` (64) cold-prefills: below it a restore cost more than
  the cold prefill it replaced.
  Below both, `SlidingRingRewindPastWindow` → cold prefill, and the declined entry keeps its recency: promoted, a
  header-only match made the entry a later turn needed the next count-cap victim.
- **A ringed prefill marks the message starts it forwards** (`ringMarkPositions`: `<|im_start|>` past the restored
  prefix and short of the prompt end's reach, at most `RING_MARKS_MAX` = 4, thinned keeping the first and the last):
  each ringed KV write fills a mark it reaches before its compaction drops the rows (`KVCache.ring_marks`), so no
  chunk is split and the forward is unchanged. A new session sharing only another's system prompt and tools
  diverges inside its first user message, below every fork and prompt end, and restores at that mark.
  Measured (MiMo 2.3bpw, kv8, MTP and prefix cache at their defaults, a 12,042-token tools + system prefix, a
  ~300-token first task, 12,346-token prompts, `taskpolicy -a`, lock per boot, busy box, 2026-10-01). One boot of
  63476cd1: the first session cold-prefills in 9,876 ms; the second and third restore 12,037 / 12,042 tokens and
  prefill in 434 / 420 ms. One boot of main 819b4751: the first session cold in 15,682 ms, and the second and third
  cold again (`cached_n` 0) in 14,970 / 14,582 ms. Splitting a chunk at the boundary instead would cost ~0.4 s per
  split on MiMo (the fixed per-chunk cost the 2025- and 4096-row prefill meter rows imply).
- **Ring checkpoints thin span-preserving** (`thinRingCps`, on merge, inheritance and shed): the lowest (a shared
  preamble's mark) and the newest stay longest; kept highest-first, a conversation's later turns pushed the preamble's
  mark out after two turns.
- **The SSD tier restores a ringed entry only at a ring file** (`bestRingMatch`, `restoreIntoRinged`): chunks hold the
  global layers, `r{pos}.safetensors` each restore point's ringed rows (the RAM entry's checkpoints plus its end,
  `RING_DISK_MAX_PER_ENTRY` = 8 kept, thinned as in RAM, salvaged per file at scan; manifest v9, which an older reader
  drops) ([arch-mimo-v2](arch-mimo-v2.md#sliding-layers-the-ring)).
- **A disk restore fills its buffers chunk by chunk** (`restoreKvInto`): each chunk is evaluated into buffers
  allocated at the restored length before the next file opens. A lazy `mlx_load_safetensors` holds its file open until
  eval (one eval at the end failed past the soft limit of 256 files), and a concatenation at the end held every chunk
  beside the result, twice the restored KV before any bill saw it. Each chunk's eval is drained before the next chunk
  writes: undrained, a write that beat the command buffer's release copied the whole buffers instead of donating, up
  to three copies of the restored KV at once on CI's M1 VM. The restore entry points drop the MLX latch they
  raised, or the cold fallback's prefill fails on it. Measured on a 150k-token Sushi-3bpw entry (147 chunks; b9dbbf53
  plus this change, `--ctx-size 262144 --prefix-cache-disk 20GB --prefix-cache-entries 1`, arms O P M M P O, 4
  restores per boot, `taskpolicy -a`, fans max, a lock per boot, 2026-09-27): a warm restore takes 170-177 ms with the
  fill, 185-188 ms with a per-chunk eval and the final concatenation, 183-187 ms on b9dbbf53; the first restore of a
  process takes 611-618 ms with the fill, 642-917 ms without it, 307-326 ms on b9dbbf53 (one eval per chunk).
- **A commit that forked off another entry inherits that entry's ring checkpoints below the fork** (`bestRingDonor`,
  refcount-shared and billed per entry like SSM checkpoints): a request appending to the conversation (a client's
  side request: the chat + a reminder) otherwise holds only its own prompt end, and once the count cap evicts the
  main entry the next main turn, diverging where the reminder was appended, cold-prefilled every turn. The slot's
  own checkpoint at its restore covers a donor that another slot's commit evicts before this one commits.

## Candidate ranking and trimming

- **Hybrid candidates rank by RESTORABLE checkpoint position, not raw match** (`findBestRestorableMatch` RAM,
  `bestHybridMatch` disk). Ringed candidates rank by `ringRestore`; an un-restorable one stays eligible at 0, so a
  lookup with nothing better still declines by name.
- **A lookup that restores 0 rows is not a use**, SSD-first or not (a hybrid with no usable checkpoint, the QSA
  history decline): the entry keeps its recency and its admission protection drops, else the count cap's next
  victim is an entry that can serve.
- Checkpoint retention thins the INTERIOR with a dense newest quarter (`spanPreservingDropIndex`, `ThinPolicy`).
- An oversized candidate is TRIMMED to the longest restorable prefix that fits (`trimLenForBudget`,
  `KVCacheSnapshot.trimmedCopy` is a REAL copy); a QSA trim bills the bank on the final retained checkpoint.
- **A ringed candidate trims only where it restores** (`ringTrimLen`): its end, dropping the reservation's spare
  capacity, or a ring checkpoint whose rows become its ring (`trimmedCopy`'s `ring_cp`). Its ringed layers are a
  constant, not a per-token price: priced per token, every MiMo target fell below the ring and every entry declined.
- A decline carries its `TrimDecline` reason; a RAM-budget decline spills to SSD (`spillDeclinedToDisk`).

## Budget

- An in-place SSD commit keeps the bill for an owned QSA history file when no new QSA checkpoint arrives; sidecar-only commits bill the change in all retained non-chunk files, including rings.

- A commit declines and frees its incoming snapshot when checked-out residents prevent satisfying either the entry-count or byte cap; the request continues and one `[hot-cache]` line names the limiting cap.
  The byte cap is judged AFTER the new entry sheds checkpoints (`retainNewEntry`): a qwen4_exp trim is priced against
  its shed survivors, and judging it unshed declined every session past the budget, so each turn cold-prefilled.

- **The hot-cache budget is CLAMPED at load** to what the weights leave under the GPU ceiling and is a HARD cap; it
  FOLLOWS residency (`reviseHotCacheBudgets` after every load/unload, repeated for 10 s because the OS returns pages
  lazily).
- **An unnamed `--prefix-cache-mem` holds one session at the working context** (`oneSessionFor`, >= 2 GB, both arms)
  where the ceiling holds it beside the weights, the n-gram page cache (`page_cache_claim`) and a cold full-context
  prompt's bill (MiMo refuses rather than evicts); else that room, at most half the bill, so an outgrown session's
  trim copy fits beside it. A flag stands, `2GB` too; context sizing and the chunk pin still read the raw ask.
- A replacement over the budget sheds ring checkpoints (thinned as above) before the entry goes (`shedRingCheckpoints`).
- Measured (b9dbbf53 plus this change, Sushi-3bpw, auto context 1M, kv8, MTP on; a ~200k-token three-turn session; `taskpolicy -a`,
  fans max, GPU lock per boot; 2026-09-27): unset, the budget is 11516 MB and turns 2-3 prefill in 0.35 s (199.7k
  reused); `--prefix-cache-mem 2GB` keeps a 139k-147k prefix and prefills in 36.0 / 31.3 s (turn 1: 114-115 s cold). The
  n-gram table stayed 100% resident (mincore) in both arms.
- Eviction is WORKLOAD-fair (`cache_key`: `prompt_cache_key` > `metadata.user_id` > system-prompt hash;
  `lruIndexExcluding`).

## SSD-only storage

`--no-prefix-cache-ram --prefix-cache-disk 10GB` keeps reusable text prefixes on SSD without retaining idle
KV snapshots in the RAM cache. The live request still needs KV memory, and queued disk writes can hold
buffers temporarily. The entry count must remain positive: `--prefix-cache-entries 0` disables both tiers.
With RAM and disk disabled, SSM checkpoint capture is disabled too. `/props` reports
`settings.prefix_cache.ram_enabled=false` and `mem_bytes=0` when RAM retention is off.

Qwen prefill chunks write through continuously in SSD-only mode. Hybrid SSM checkpoints and MiMo ring
restore points survive restart. Image-bearing entries remain ineligible for disk persistence. RAM+SSD defaults
are unchanged. `SUSHI_PREFIX_CACHE_DIR` can select an absolute cache directory; unset, the root stays
`~/.sushi/kv-cache`. Live tests use a separate root without changing home settings.

Ported from [mlx-serve #680](https://github.com/ddalcu/mlx-serve/pull/680), with Sushi's ring checkpoint handling.

## SSD-first

- Disk fingerprints include the model path, config size/mtime and overrides, plus sorted indexed weight-shard (or unindexed safetensors) names and size/mtime and `ngram_table.bin` size/mtime; payloads are statted through symlinks, never content-hashed.

- `prefix_cache.ssdFirstActive` = a disk tier AND (capable arch OR RAM retention disabled), mirrored onto `HotPrefixCache.ssd_first` +
  `DiskTier.ssd_first`: with RAM enabled it floors at ONE session, `--prefix-cache-mem` = the IDLE allowance.
  SSD-only storage retains no idle RAM entry.
- Spill and EVICT are two decisions (`PersistOutcome`: only `.persisted` + an agreeing index + landed files license
  discarding RAM); writes ride `kv_disk_writer.zig` (FIFO, `meta.json` last, epoch fence at the ONE removal site);
  per-chunk write-through; a diverging turn hard-links the donor's LANDED chunks; a full-prefix hit CHECKS the entry
  OUT so the first append donates.
- **A checkout is a PROMISE until the append DONATES** (`donateCheckout` right before `Generator.initWithOptions`,
  below every refusal; `releaseCheckout` hands an undonated entry back intact).
- **Off SSD-first, a warm share that does not fit is taken over, not refused** (`checkoutRestored`, qwen4_exp's
  admission pass): a full-entry hit is checked out on demand and billed as donated. The tradeoff: a request that
  fails after donating loses the entry. Disk checkpoints come off the TOP of
  the flush budget; the disk tier serves the pre-media text prefix only.
- **"Free disk" is what the OS will GRANT** (`sushi_volume_free_for_use`, statfs fallback): purgeable space is released
  on demand. The `volumeSpace` test must not race the OS's purgeable answer.

<a id="spec-state"></a>
## Spec state rides the cache

`Entry.mtp` + `restoreSpecSnap`, adopt only on `base + step == matched`; MTP trims to `mtpCommittedLen`; survives the
SSD tier (`spec.safetensors`). An adopted spec cache has ONE owner at a time (`runPrefill` clears its locals BEFORE
`initWithOptions`).

## Guards

`tests/test_prefix_cache_*.sh` (budget revisit, disk, hot, mem, workloads), `tests/test_hybrid_reuse_equivalence.sh`,
`tests/test_mimo_ring_reuse.sh`, `tests/test_mimo_ring_fork_ssd.sh`, `tests/test_qwen4_mtp_head_persist.sh`. Grep the log for `[cache]`, `[hot-cache]`, `[disk-cache]`.
