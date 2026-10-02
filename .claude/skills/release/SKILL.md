---
name: release
description: sushi pre-release validation checklist, SemVer versioning, release steps, and CHANGELOG style. Use when preparing or cutting a release, running pre-release validation, or writing CHANGELOG entries.
---

## Pre-release validation

On the Apple M5 Max 128 GB, on the FINAL release tree, with a fresh `zig build -Doptimize=ReleaseFast`. Run the steps
directly, one at a time: no driver script, no pause/resume bookkeeping. A failed step goes to the owner before anything
else. Model loads take the GPU lock; step 2 waits for a quiet box (no builds, no AirDrop or other transfers).

| # | Step | Command | Pass |
|---|---|---|---|
| 1 | Suite + binary | `zig build test -Doptimize=ReleaseFast`; `zig build -Doptimize=ReleaseFast`; `sushi --version` | 0 fail; names `build.zig.zon`'s version |
| 2 | **Perf gate** | `./tests/bench.sh --tag v<ver> --only sushi-4bpw` | within noise of the previous column in `benchmarks.md`, mode suffix present; append this release's column |
| 3 | **KLD gate** | `sushi kld compare` 16x512 to first EOS on one Sushi pack and MiMo | within ~1% of its row in `docs/quality-kld.md` |
| 4 | Live | `test_format_matrix.sh`, `llmprobe --quick`, `test_smoke_matrix.sh`, plus the live test of each area the release changed | all pass |
| 5 | CI | `gh workflow run ci.yml --ref main` on the release commit | green (the macOS 26.2 build gate) |
| 6 | Packs | each HF pack repo holds the shards, `ngram_table.bin` and its model card | card numbers match `docs/quality-kld.md` |
| 7 | Cross-engine (only before a public claim) | start each engine yourself, `./tests/bench.sh --url <host:port> -m <id> --full` | recorded in `~/.sushi/runs/bench-<tag>/`, engine named beside every win |

**Rules:**
- **Steps 2 and 7 are different questions.** 2 = "did our code regress", sushi only, every release. 7 = the public
  comparison; re-run it only when another engine's version bumps.
- **The perf gate is Sushi-4bpw alone.** A cell that lost its mode suffix means MTP stopped engaging: chase it before
  shipping. A low cell gets one rerun on a quiet box before anyone bisects.
- **`--full`** takes median-of-3 per rung and climbs to 32k/64k; the default is one run per rung to 16k.
- **Never quote a win without naming the engine it is over.**
- **`benchmarks.md` gets one new column per release**, from the rows step 2 prints. Obey its header rules: tables
  only, M5 Max only.

## Release artifacts

The release record is `benchmarks.md` plus the llmprobe reports (JSON and HTML) under `~/.sushi/runs/bench-<tag>/`;
no CSVs or charts land in `docs/`. The working baselines agents inherit between releases live in
`docs/perf-baselines.md` (the release column is also added there as a cited row). A number taken mid-cycle is stale the
moment another perf round lands: run the gate on the final tree.

## Versioning & Releases

SemVer `MAJOR.MINOR.PATCH`, tagged `v1.0.0`. MAJOR breaks a public contract (HTTP API, flags, the pack format); MINOR
adds a model, a feature or a flag; PATCH fixes without adding.

**The version source is `build.zig.zon`'s `.version`**: a plain `zig build` stamps it into `sushi --version`, and
`-Dversion` (CI) must be SemVer or the build stops. `release.sh` dispatches only when the FIRST `## ` heading of
`CHANGELOG.md` names that same version and no GitHub release or tag carries it yet; the workflow's "Extract version"
step sources `release.sh` and applies the same checks (a pushed tag must be `v<zon>` or `v<zon>-pre-release.<n>`).
Nothing is computed from the date.

**Signing**: there is no Apple Developer ID, so the release binary ships ad-hoc signed and not notarized. The
workflow signs with a Developer ID and notarizes only when the `APPLE_*` repo secrets exist
(`tests/test_release_workflow_gates.sh`).

**Release**:
1. Set `build.zig.zon`'s `.version` to the next version and rename the top `## Unreleased` entry to
   `## v<version> — Headline` (check `gh release list --limit 1` first — never reuse an existing tag)
2. Dont commit or push
3. After the owner (or `./release.sh`) cuts it, the Release workflow leaves a DRAFT. Publishing it fires
   `.github/workflows/homebrew.yml`, which runs the tap's bump and fails unless `Formula/sushi.rb` names the new
   tag (needs the `HOMEBREW_TAP_TOKEN` secret). Confirm with `brew update && brew info beamivalice/tap/sushi`.

### CHANGELOG style

**One entry per shipped release. No new entries for unshipped work — fold it into the next pending entry.** Always run
`gh release list --limit 1` first; if the topmost CHANGELOG entry is newer than the latest GitHub release, that entry is
unshipped and any new bullets get merged into it. Unshipped work lives under `## Unreleased`; the version heading is
written in the release step. A model that is not public yet (MiMo-V2.6-Flash until v1.1) stays out of the entry.

**Contributor credit (every release):** list outside contributors' PRs since the last tag (`gh pr list --state merged
--search "merged:>=<last tag date>"`, plus PRs landed as a squash or cherry-pick whose commit subject carries `(#N)`).
The entry thanks each by @handle, naming what they shipped in user terms, in the bullet for that change or in one
closing "Thanks" bullet. A credit a shipped release missed goes into the next entry, marked as belated.

Tone: high-level executive bullets, marketing-style. The audience is users/integrators, not contributors reading the diff.

- Lead each bullet with **what changed for the user** (capability, speed, model support), not the implementation.
- Quantify where impressive — concrete tok/s percentages, model names, the workload it applies to.
- Avoid: file paths, function names, internal symbol renames, line-count diffs, "we discovered that…", PR/issue numbers.
- 4–7 bullets per release. If you need more, the release is too big and should ship sooner.

Template:

```markdown

## vMAJOR.MINOR.PATCH — Two-to-five-word headline

- **<User-visible thing>**: one or two sentences on the impact. Numbers if you have them.
- **<New model / API / behavior>**: what unlocks, when it kicks in, what stays the same.
- **<Speed or reliability win>**: workload + measured gain.
- **<Removed / deprecated thing, if any>**: why, and what users should do instead.

---
```

When in doubt, look at the existing entries — keep the same density and tone.
