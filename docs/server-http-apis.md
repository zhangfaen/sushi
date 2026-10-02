# Server: HTTP APIs, streaming, sampling and constrained output

What each HTTP surface promises its clients: the OpenAI chat/completions/Responses and Anthropic Messages contracts,
streaming and usage chunks, logprobs, seeds and sampling, reasoning budgets and constrained JSON, plus the agent
launcher. Read this before touching `src/server.zig`, `src/responses.zig`, `src/launch.zig` or the sampler.

Index: [CLAUDE.md](../CLAUDE.md#docs-index). Related: [server-tool-calling](server-tool-calling.md),
[server-lifecycle](server-lifecycle.md), [engine-mtp](engine-mtp.md).

## Surfaces

- **OpenAI chat/completions + Responses**: usage ALWAYS carries `prompt_tokens_details.cached_tokens`; thinking
  opt-ins = `reasoning_effort` OR `enable_thinking` (top-level, else vLLM's `chat_template_kwargs.enable_thinking`;
  `reasoning_budget_tokens` outranks); a request naming neither takes the arch default (`defaultEnableThinking`);
  on Responses a `reasoning` object decides alone, and without one the same rule applies; `n>1` 400s.
- **Effort vocabulary** `off low medium high xhigh max` (`none` = off; `minimal` keeps the legacy 1024 budget):
  each served arch accepts a subset (`model.effortArms`), listed as `reasoning_efforts` on its `/v1/models` row; any
  other word 400s on chat, Responses and Anthropic with the accepted list, never rounded. qwen4_exp: off, low (2048),
  medium (8192), xhigh (uncapped); the template reads the word. mimo_v2: off, low (2048), medium (8192), high, xhigh,
  max (uncapped); the template has only on/off, the budget is the whole effect. Uncapped = `--reasoning-budget`.
  A thinking request that names NO effort gets no server budget (decided): Qwen3.8 renders it as low
  (`chat.qwen38EffortFor`), whose preamble shortens the thought without truncating it. `/v1/responses`:
  `sequence_number` on every event, stateful via
  `ResponseStore`, WS via Upgrade. Continuing a partial reply: `continue_final_message` explicit on chat, INFERRED on
  `/v1/messages`.
- **Anthropic `/v1/messages`** (Claude Code): typed blocks, `input_schema`→`parameters`, stop-reason map incl.
  `stop_sequence` echo, full SSE block lifecycle; a `system`-role message past index 0 (Claude Code's hook output,
  Codex's mid-input `developer` turn on Responses) renders where it was sent, folded only for a template that cannot
  place it ([server-tool-calling](server-tool-calling.md#templates)); `developer` reads as `system` (`canonicalRole`).
- **`preserve_thinking`** (templates that read it: Qwen3.8), resolved per request as `chat_template_kwargs.preserve_thinking`
  (bool, all three surfaces) > `--preserve-thinking on|off` > `preserve_thinking` in model-settings.json > the template
  default (undefined: every turn's thinking kept). Off renders only the latest user turn's thinking.
- `tool_choice` reads every surface's wire shape; `required`/`any` and a named function are enforced at decode, a
  named function the request does not declare is a 400 ([server-tool-calling](server-tool-calling.md#tool_choice)).
- `/v1/models` rows carry `context_length` + `max_model_len` at TOP level. Context-overflow 400s name BOTH counts.
- `/v1/models` `meta.quantization` reports EXL3’s configured expert rate and dense width (e.g. `EXL3 3bpw experts, 8-bit dense`) for loaded and unloaded packs; affine labels remain `{bits}-bit`, and `/props` numeric quantization fields retain their dense-trunk meaning.
- Endpoint EXISTENCE never depends on model state and the 404 is answered BEFORE the model resolves (`ROUTE_PATHS`);
  a status route never reaches `ensureLoaded` (`handlePropsNoModel`). Removed upstream routes answer named 404s.
- **Only an unknown NAME falls back to the default model** (SDK names like `gpt-4`); a PATH (`/…`, `~/…`, never
  `org/repo`) names its own entry as `/v1/load-model` resolves it (`routeRequestModel` → `registry.peekPath`), and an
  unregistered path is a 404 `model_not_found` (Anthropic `not_found_error`), never another model's answer.
- A content array's text parts JOIN in order (`joinedTextParts`); its media parts render at the offset they sat at.
- **Media is read from EVERY message** on all three surfaces: chat `image_url`/`video_url` parts in any role
  (`tool` included), Anthropic `image` blocks beside the text or inside a `tool_result`, Responses `input_image` in
  a message or a `function_call_output` array. Only base64 data URLs decode (remote URLs are refused, not fetched);
  a failure is a 400 naming the message index and the reason, an `input_audio` part a 400 on a model without an
  audio encoder, more than 64 images a 400 with both counts, an encode failure a 500 (Anthropic: `api_error`).
  Stored Responses history keeps text only.

## Streaming

- **A stream and a non-stream answer are the SAME BYTES**; leading whitespace is the one thing a stream may withhold
  (`streamContentLead`). A spent reasoning budget WITHHOLDS the rest of the thought; a non-stream tool-call reply
  carries the pre-markup text (`visibleToolPreamble`); a non-stream disconnect reports `client_disconnect`, never
  `length`, and is noticed after every decoded token as well as during prefill, so a client's timed-out retry never
  leaves a ghost decoding to `max_tokens`; a stop sequence cuts at its INDEX (`stopSequenceCut`); request ints clamp (`parseRequestSeed`,
  `clampJsonI32`).
- **`stream_options.include_usage` chunk ships `"choices": []`** (`sendSSEUsageChunk`); the ending appears on exactly
  ONE chunk; a client cannot time our stream — use the final chunk's server `timings`.
- Liveness is a property of the SOCKET: `beatStreamKeepalive` at the bottom of every streaming loop, emit on 5 s
  byte-silence. SSE comments keep the transport alive but do not count as model progress for every client.
  `sushi launch omp` sets the Sushi provider's `compat.streamIdleTimeoutMs: 0` so buffered calls can finish;
  explicit omp timeout settings, environment overrides and per-call options still take precedence (verified with
  omp 18.3.0). Non-loopback `--url` targets also have a separate first-event deadline: omp's
  `providers.streamFirstEventTimeoutSeconds` controls it, with zero allowing unlimited initial buffering.
  `--timeout` remains a token-progress STALL timeout (`StallClock`).
- **NO string built from model bytes is guaranteed UTF-8**: sanitizing lives INSIDE the escaper (`chat.utf8Next`
  under every `jsonEscape`/`appendJsonString`); logprobs `bytes` keeps the exact bytes. Hand-written error text is
  escaped at the SINK.

### Closing a streamed response

`Conn.close` half-closes the write side of a close-delimited response, then holds the socket until the peer
hangs up, server shutdown starts, or five minutes pass. This prevents macOS's orphaned FIN_WAIT_2 timeout from
resetting a client that is still reading after the final SSE event. The request has already released its model
slot; only the connection thread waits. Responses with `Content-Length` close immediately.

Ported from [mlx-serve #673](https://github.com/ddalcu/mlx-serve/pull/673). Socket-pair tests cover peer closure,
length-framed responses and shutdown; `tests/test_responses_streaming.sh` also waits past the TCP FIN timeout
before reading a completed stream and requires clean EOF.

## Seeds, logprobs, sampling

- **A `seed` binds EVERY sampler with a fresh key PER DRAW** (`generate.seedKey`).
- Logprobs are the MODEL's distribution (pre-temperature), ids travel WITH values, entry belongs to the RETURNED
  token (one-token delay); `logprobs.content` describes `message.content` (`contentTokenRange`); streaming logprobs
  are a SIBLING of `delta` shipped EXACTLY once against a high-water mark. logprobs>0 + grammar disable spec.
- Logprobs are `logits - logsumexp` in f32 (`computeLogprobs`): `log(softmax)` in bf16 lands on bf16's grid, 0.125
  apart between -16 and -32.
- Sampling defaults for omitted fields: body > launch flags > model `generation_config.json` > hardcoded.
- Top-k and top-p are ONE pass (`filterTopKTopP`); a filter cuts by RANK, never by value (bf16 ties at the top
  constantly; `ranksDescending` ties by lowest id); the nucleus is the mass STRICTLY above each rank, cumsum in f32;
  `top_p` 0 is GREEDY (`applyTopP` floors at `floatMin(f32)`).
- A sampler never draws a RESERVED special or PADDING row (`installSuppressMask`; logprobs stay RAW).
- **`ignore_eos: true`** (vLLM's field) decodes a `/v1/completions` request past EOS to `max_tokens`
  (`requestEosSlice`); stop sequences and the loop stops still end it, and its text skips `special: true` tokens
  (`completionShowsToken`, vLLM's default `skip_special_tokens`). Chat refuses it with a named 400
  (`chatIgnoreEosRejectReason`): past its end of turn the model writes another turn, and that turn's think block is
  merged into the reasoning by the non-stream reply (`normalizeEmbeddedThinkBlocks`) but not by the live stream.
- **A repeat/presence penalty samples on the synchronous serial path** (`samplesSync`, like logprobs): no draft
  path (`draftsRefused`), no batched tick (`.penalty`), never the lazy pipeline, which never applied it.
  A penalty-mask allocation failure fails the request instead of sampling without its penalty. Chat and
  completions parse it alike (`parseRepeatPenalty`); a repeat penalty of 0 or below is off.
- **Think penalty** (`think_penalty` request > `--think-penalty` > model-settings.json > off, 0-20; arXiv 2606.00206):
  while the thought is open every single-token spelling of the paper's markers (bare or space-led, lower or capital)
  loses λ; a split spelling is skipped (its first piece starts other words). The paper penalises everywhere; we stop
  at the closer. Every sampled position, serial or verify row, is shifted by its own prefix (`thinkShifted`,
  `thinkShiftRows`), so greedy MTP keeps serial's bytes; logprobs stay RAW.


## Experimental logit bias

`--logit-bias-file <path>` loads a JSON or CSV file at model load; the launch flag overrides the per-model
`logit_bias_file` setting. The default is off. Each entry names exactly one `id`, exact vocabulary `token` string,
or `word`; words expand to single-token lower/capital spellings with and without a leading space. Split spellings
are skipped and counted in the load log.

```json
{"entries":[{"word":"Wait","delta":-1,"scope":"reasoning"},{"id":1234,"delta":2}]}
```

The equivalent CSV columns are `kind,target,delta,scope`:

```csv
kind,target,delta,scope
word,Wait,-1,reasoning
id,1234,2,all
```

`delta` is finite and between -100 and 100: negative penalizes, positive rewards. `scope` is `reasoning`, `answer`,
or `all` (default). Answer scope applies outside reasoning, including before an opener; the closer ends reasoning.
Unknown targets/scopes, out-of-vocabulary ids and malformed files fail model load with a named `LogitBias*` error.
The load line reports the file, entry count, expanded ids and skipped spellings.

`/v1/chat/completions` and `/v1/completions` accept OpenAI `logit_bias`, a map such as `{"1234":-2}`. These deltas
apply to all positions and add to file entries and the optional think-penalty preset. Invalid ids, nonnumeric or
out-of-range biases return 400. `think_penalty: 0` disables the preset only; file and request biases still apply.
Overlapping entries add. Sampling uses shifted logits; returned logprobs remain raw.

Scoped vectors are prepared once per request on the inference thread. Active biases use the full target vocabulary
head, including positive rewards. Serial, MTP, PLD and prompt-lookup rows apply the same prefix-dependent shifts;
streaming and non-streaming preserve the same generated tokens. Existing non-stream-only repetition-tail trimming
can still shorten displayed loop-stop replies; `SUSHI_LOOP_TRIM=0` disables that presentation step for strict byte
comparisons while retaining loop detection. The unchanged `--think-penalty` preset remains off by default.

## Reasoning budget

Enforced at DECODE (`server.armThinkBound` → `SamplingParams.think_bound`, `scheduler.thinkBoundTick`): at the budget
the early-stop line + the atomic closer commit as ONE multi-token forward (`commitForcedTokens`); the whole closed
thought is delivered. Every decode tick checks it with the loop stop (`loopGuardTick`), the plain batched tick too
(`batchedTickRows`): skipped there, a budget under `--max-concurrent` overran by up to ~2.5k tokens while its slot
batched plain. Guard: `tests/test_reasoning_budget_stream.sh`. Effort budgets = pi's ladder
(`model.effortArms` for served arches, `responses.effortBudget` for the rest).
Every surface arms it with one precedence: explicit budget (`reasoning_budget_tokens`, Anthropic `budget_tokens`) > the
effort word's budget > `--reasoning-budget`. `/v1/responses` parsed the word and dropped the budget.

## Constrained JSON

- Constrained generation reports each returned token against its raw logits before the grammar mask, including forced choices; those logprobs have no one-token delay.

- The payload offset is AUTHORITATIVE (`reasoning_protocol.Delivery`, all surfaces, stream + non-stream).
- The grammar mask never walks the whole vocabulary (`token_mask.buildMask`); every grammar state has a legal byte;
  no whitespace OUTSIDE the root value, the model's OWN layout inside (`MAX_FREE_WS` 16).
- Every schema-mask surface uses ONE thinking policy (`schemaMasksThinking`); tools present = no mask. Per-model
  grammar table lives on `LoadedModel`.
- Code: `src/json_schema.zig` / `src/json_grammar.zig` / `src/token_mask.zig` / `src/regex.zig` (schema IR →
  streaming grammar → per-token mask), `src/reasoning_protocol.zig`.

- `/props.settings.prefill_decode_share` reports the effective process-wide share, including zero under the interleave kill switch.

## Security and observability

- `--api-key`: loopback exempt, `/health` + OPTIONS + `GET` of the chat page open, `constTimeEql`.
- `--metrics`: zero cost off; TTFT at prefill completion; live tok/s via ONE atomic per tick; `/metrics(.json)`.
- `/metrics.json` ends with `"sessions"`, one row per live request (phases `prefill` and `decode`, cap 32; published
  by the inference thread under `queue_mu`, copied by the reader under the same lock — there is no separate
  `/requests` route): `model`, `request_id` (submit sequence; stable across polls of one request, never reused),
  `phase`, `context_tokens` (prompt + decoded; the FULL prompt on a prefill row), `context_length` (the model's
  effective limit, 0 = not ready/unknown), `cached_tokens`, `generated_tokens`, `max_tokens` (the request's own
  output cap), `elapsed_seconds` (age since arrival, refreshed every publish) and `state_bytes` (GPU bytes of the
  slot's own KV + SSM buffers; a restored share stays billed to the hot-cache entry, never to the row).
- A live row's `state_bytes` is at capacity: a ringed layer's ring, each QSA bank by its capacity buffer (never also
  its view), and the ring restore points the slot holds.
- After the live rows come `cached` rows, one per hot-cache entry of every ready model (cap 32, published under
  `digest_mu`): `context_tokens` = `cached_tokens` = the entry's tokens, `state_bytes` its `kv_bytes`. A cached row has
  no request, so `request_id`, `max_tokens`, `generated_tokens` and `elapsed_seconds` are 0.
- An entry a live row restored from is listed once, as that row: the dedupe keys on an internal entry id that
  `/metrics.json` does not emit.
- `/props` `memory.kv_cache_bytes` = the current model's hot-cache residency + every live slot's state, published
  each tick with or without `--metrics`; a donated checkout's buffers, which the entry bills until release, count once.

## Chat page (`GET /`, `GET /chat`)

- One self-contained file, `src/webui/index.html` (CSS, JS and the logo inline, no external fetch), embedded with
  `@embedFile` and served as `text/html; charset=utf-8`. Any other method on those two paths is a 405 answered BEFORE
  model resolution, so it can never cold-load a model. Guards: `tests/test_webui.sh`, the `chat page:` tests.
- It speaks only the public API: `/v1/models` for the picker (`reasoning_efforts` fills the effort select, `vision` or
  an `image` input modality shows the attach button), `/v1/chat/completions` streamed with `include_usage` (the
  readout is the final chunk's `usage` + `timings`), `reasoning_content` shown collapsed. Stop aborts the fetch; the
  server cancels on disconnect.
- Under `--api-key` the page is served without the key (it holds no data), asks for it on the first 401 and sends it as
  a Bearer token. Its fetches use `credentials: "omit"`: the 401's Basic challenge would otherwise open the browser's
  own login dialog.
- Conversations live in the browser's `localStorage` (every access guarded); attached images stay in memory only, as a
  few photos would fill the storage quota.
- `/props` gives the version and the update banner: shown while `update.available`, its button POSTs `/v1/update`,
  polls `/health` until the server has gone down and come back, reloads, and reports the new version or `update.error`.
- Startup prints `chat in your browser: <url>` once (`chatPageUrl`: a `0.0.0.0` bind shows as `127.0.0.1`); `sushi run`
  prints it under its banner, since its log is quieted to warn.

## Self-update (`POST /v1/update`, `/props.update`)

- `/props` carries `update: {current, latest, available, checked_at, url, error, command}` with or without a model
  (`update.propsJson`): `latest`/`checked_at`/`url` stay null until a daily check has answered; `error` is why the
  last update failed, cleared by the next success; `command` is `brew upgrade sushi` for a Homebrew install (the page
  shows it in place of its button), else null.
- `update.guard`, first refusal wins: a non-loopback bind 403 (update on the server with `sushi update`), a
  non-loopback peer 403, `--api-key` set and not presented 401 FROM LOOPBACK TOO, no Origin or one other than the bind's
  own `http://<host>:<port>` (127.0.0.1 and localhost interchangeable; DNS rebinding carries its own name) 403,
  `--parent-pid` 403 (the host updates its engine), a request decoding or queued or a model loading 409 `update_busy`
  (refused, never queued), an install that cannot replace itself (Homebrew, source build, app bundle, unwritable) 409
  by name, no newer release known 409.
- Accepted: 202 `{"status":"updating","from","to"}`, then the SIGTERM shutdown path and the in-place updater
  ([server-lifecycle](server-lifecycle.md#self-update)).

## Agent launcher (`sushi launch <agent>`)

- `src/launch.zig` (claude/pi/omp/opencode/codex/hermes/aider): reads `/v1/models`, writes agent configs into
  `~/.sushi/<agent>/`. Launcher env: `ANTHROPIC_BASE_URL` + dummy keys + `ANTHROPIC_DEFAULT_*_MODEL=sushi`.
- Claude Code's stream watchdogs and 10-min request timeout are raised and its non-stream fallback is off: a long
  prefill plus a long think tripped them, and each fallback re-sent the whole prompt, then timed out and retried.
- Agent budgets (`launch.budgetForContext` + `compactionReserve`): output share ctx/2, compaction reserve ctx/4
  capped at 20000, carried into pi's `settings.json` and opencode's `compaction` + `limit.output`. A launch below the
  agent's context floor WARNS (claude 64k, opencode 32k, others 16k).
- pi sends its thinking level as `reasoning_effort` through a per-model `thinkingLevelMap` built from the row's
  `reasoning_efforts` (`launch.piEffortFor`: exact, else the next accepted word up, else down; Qwen3.8 high → xhigh).
  pi's `thinkingFormat: qwen` sent only `enable_thinking`, so low/medium never reached the server.
- omp (a pi fork) has no off entry in its maps: off rides the qwen dialect (`enable_thinking: false`), `whenThinking`
  switches thinking requests to `reasoning_effort`, and a per-model `thinking` block remaps each level with the same
  rule; `requiresEffort: false` stops omp clamping off to the lowest effort.

## Web UI research tools

- The composer's **Tools on/off** button enables the same research pack as `sushi run`, off by default.
  The preference persists in this browser. It is fixed for a turn; the button is disabled while a reply runs.
- The browser sends definitions, assembles streamed tool calls, executes them through `POST /v1/tools`, and
  sends results back to the model. Eight tool rounds maximum, followed by a final request without tools.
  Results are collapsible in the transcript. Stop cancels browser requests and records cancelled results for
  remaining calls so the conversation stays valid. An already-running server tool may finish its bounded work.
- `POST /v1/tools` with `{ "vision": false }` lists definitions and the file root. With `name`, JSON-string
  `arguments`, and `vision`, it executes one call and returns `text` plus optional `image` data URL.
  Vision models get `view_image`; returned images remain in memory only.
- This bridge requires a loopback bind and peer, the chat page's Origin, and the normal API-key policy.
  It works from `localhost` or `127.0.0.1`, not a remote browser or wildcard bind. File tools are confined to
  the server's working folder, with the existing hidden/secret-file and symlink checks; network tools keep
  the REPL's public-address restrictions. No MCP configuration is added.
- Checks: `node tests/test_webui_tools.cjs`, `tests/test_webui.sh`, and the `web tools:` unit test.

## `sushi run` research tools (client-side)

- **The REPL orchestrates and runs its tools locally** (`src/repl_tools.zig`, loop `cli.runToolTurn`): it sends `tools`,
  runs the returned calls, appends `tool` messages and asks again. OFF by default: `--tool on|off`, `/tool on|off`,
  bare `/tool` shows the state and list. One dim trace line per call (`search:`, `fetch:`, `read:` …).
- Tools: `web_search` (GET html.duckduckgo.com, top 8 title/url/snippet, `uddg=` unwrapped, ads dropped),
  `fetch_url` (GET, ≤5 redirects, 10 s wall clock, 2 MB, HTML → text ≤20k chars), `read_file` (≤256 KB),
  `list_dir`, `search_files` (substring or regex, ≤100 hits), `view_image` (only when `/v1/models` lists `vision`).
- **8 tool rounds per user turn**, then a user nudge and one request WITHOUT tools for the final answer.
- **Only the latest USER turn's images are decoded** (`server.activeWireMediaIndex`): a tool image rides a synthetic
  user turn after the tool results. `/image <path>` attaches to the next message; a pasted path is never attached.
- **File tools are confined to one folder by REAL path**, the start folder until `/cd <folder>` moves it
  (`changeRoot`: absolute, `~` or relative to the current folder; must be a directory, symlinks resolved, a path with
  a secret name refused; bare `/cd` shows it). `..`, outside absolutes and escaping symlinks are refused, as are dot
  entries and secret names (`.env*`, `*.pem`, `*.key`, `id_*`, `*.p12`, `credentials*`, `*.keychain*`, `.ssh`,
  `.aws`, `.gnupg`), checked both as typed and after resolution (`confinePath`).
- An outside path's refusal tells the model the folder is fixed and the user can type `/cd <folder>`, so it asks for
  that instead of guessing other paths.
- The user's `/image <path>` (`loadUserImage`): a RELATIVE path resolves in the `/cd` folder under the same
  confinement; an ABSOLUTE or `~` path (typed or dragged in) may leave it, but a secret name anywhere on it or a hidden
  file name is refused, as typed and resolved (`userPathRefusal`). The model's `view_image` stays confined.
- Every prompt carries that folder and the tools state, dim: `~/project · tools on >>> ` (`formatPromptStatus`: `~` for
  `$HOME`, `…` and the tail past 32 characters); it is rebuilt before each input, so `/cd` and `/tool` show at once.
  The ready banner prints the same pair.
- **Web tools reach public hosts only**: http/https, no userinfo, local names refused, EVERY resolved address and the
  connected peer (`getpeername`, defeats DNS rebinding) must classify public (`classifyIp4/6`; mapped, NAT64 and 6to4
  judged by their IPv4); each redirect hop re-checked; no cookies, auth headers or POST.
- Every failure is a short tool-result string; results are data, never executed. A DuckDuckGo bot check (HTTP 202,
  `anomaly-modal`) reads as "search unavailable", never as zero results.
