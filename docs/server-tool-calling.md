# Server: chat templates, thinking and tool calling

How prompts are rendered and how model output is split into reasoning, content and tool calls: the Jinja render and
its silent fallback, the tool-call parse chain and its one chokepoint, think-tag handling, loop stops, and the
replay-pinned invariants. Read this before touching `src/chat.zig`, the tool paths in `src/server.zig`, or
`src/format_corpus_test.zig`.

Index: [CLAUDE.md](../CLAUDE.md#docs-index). Related: [server-http-apis](server-http-apis.md),
[tests/CLAUDE.md](../tests/CLAUDE.md).

## The pipeline

- With `tools`, tokens buffer for detection; thinking buffers separately. Parse chain strict → tolerant repairs →
  truncation salvage, then the ONE chokepoint `server.parseToolCallsForRequest` = parse → inferred-name filter →
  parallel clamp → buried-param hoist → schema coercion (last two gated by `--no-tool-autocorrect`; emitted
  `arguments` ALWAYS valid JSON).
- Serialization `chat.serializeMessagesJson`: role "tool" native, args as JSON STRINGS, every string via
  `appendJsonString`. Streaming: full args in ONE SSE delta, thinking → `reasoning_content`.
- Buffered calls may exceed a client's inter-event progress deadline even while SSE keepalives arrive. The omp launcher
  disables that deadline only for its Sushi provider; argument truncation and loop-stop rules remain unchanged.
- **Hard invariants (replay-pinned)**: emitted args ALWAYS valid JSON; every converter escapes + dedups; coercion
  never worsens conformance; a parsed NAME never contains `<|`; no tag leaks. Harness:
  `src/tool_traffic_replay_test.zig` over `src/fixtures/tool_traffic.jsonl`.
- A live failure revealing a CLASS ships the instance test plus a corpus entry or invariant in
  `src/format_corpus_test.zig` plus a rule here. Capture traffic: `SUSHI_RAW_DUMP_FILE=<abs>` →
  `tests/harvest_tool_traffic.py`. Reproduce tool bugs `stream:false` first.

## Templates

- One-shot CLI prompts carry the same thinking fields through the chat request policy; no CLI render may hardcode thinking off.

- **Control bytes**: ONE raw byte <0x20 in history kills the strict render → SILENT `fallbackFormatChat` (model loses
  its stop token). Everything through `appendJsonString`; wrong-family tags out ⇒ suspect silent fallback first. A
  NUL byte truncated the rendered prompt (`jinja_render_chat` returns its LENGTH; tell: the same `prompt=` count on
  consecutive turns).
- **A `chat_template` value can be a POINTER** (`{% include 'chat_template.jinja' %}`): `chat.isIncludeStub` reads it
  as "no inline template" so the sidecar loads. Grep the log for `jinja` first.
- A template can raise on OUR extra-context values: `serializeExtraContext` sniffs the family; tool-call `arguments`
  stay OBJECTS; history tool_calls carry `"id"`; only a refusing template gets `noThinkTailSuffix`.
- **A system turn past index 0 renders where the template allows it**: a template that raises on it (Qwen3.8) or
  drops it gets it folded into the leading system (`templateProbeRendersLateSystem`, every surface); MiMo's role
  loop keeps it in place, byte for byte.
- **A surface never folds before the render**: a pre-fold moved Codex's mid-input `developer` turn and Claude Code's
  hook output to the front, so on MiMo turn N's prompt stopped being a prefix of turn N+1's. Responses' fresh
  `instructions` replace only the stored history's LEADING system turn.
- **A generic ChatML role header preserves tool roles**: absence of a literal `'tool'` branch does not license
  rewriting tool results as user text (`templateReferencesToolRole`).
- **`preserve_thinking` reaches only a template that reads it** (`serializeExtraContext`), resolved per request:
  `chat_template_kwargs` > `--preserve-thinking` > model-settings.json > undefined (Qwen3.8 keeps every turn's
  thinking). Each value renders its own prompt, so the tokenize-cache key carries it.
- **A continuation is the template's own rendering of the partial turn**, cut after its text (transformers'
  `continue_final_message` rule): `<think>\n\n</think>\n\n` on Qwen3.8, `<think></think>` on MiMo, thinking on or off.
  The thinking-on prompt plus a bare closer gave `<think>\n</think>` and no block at all; it is only the fallback now.
- **Assistant-history reasoning round-trips** (`Message.reasoning_content`, OMITTED when absent). A contract COMMENT
  is read as a spec — pin it with a test.

## Parsing tool calls

- Nameless calls are discarded; XML function names reject empty placeholders and whitespace or tag fragments from prose.

- **A `<tool_call>` body carrying `<function=` is the XML dialect and is read FIRST** (qwen 3.5+ template mandates
  it); a parameter VALUE never decides the call. A `<parameter>` VALUE may spell the dialect's own close tags
  (`hermesValueEnd` = LAST `</parameter>` before the next opener). A Hermes value keeps its own whitespace
  (`stripHermesValueFraming`).
- **Framing is removed only where the family writes it**: a newline-framed `<function=>` body (Qwen3.8) loses one
  newline per value side, an unframed one (MiMo) keeps the value verbatim, so a written file keeps its last newline.
- **A JSON call cut INSIDE the object still names its tool** (`truncatedJsonCallName`): recover NAME + `{}`, NEVER ship
  partial values, never ship raw markup as content. Model-mangled arg JSON → `looseRepairToolCallJson`, never drop
  the whole call. A tag parser never bails on ONE missing delimiter.
- **A `</think>` inside a tool ARGUMENT is payload** (`thinkCloseIsToolCallPayload`): decline a close whose nearest
  preceding tool opener is still OPEN AND whose block closes afterwards.
- **Types come from the SCHEMA, never the value's spelling** (`coerceToolArgsToSchema`; undecidable → untouched).
  Buried required params hoist only on all-schema-read unanimity. A container string with a key repeated at the SAME
  value still coerces (`parseContainerAllowingRepeats`). Heuristic raw-JSON inference must name a DECLARED tool
  (`filterInferredBySchema`).

## tool_choice

- **One parser, every wire shape** (`chat.parseToolChoice`): chat nests the name under `function`, Responses and
  Anthropic carry it flat, Anthropic spells required `any`; anything else is `auto`, which is unchanged.
- **`none` is prompt-level plus the parser**: tools are withheld from the prompt and the reply is never parsed for
  calls. No decode mask.
- **`required`/`any` and a named function are enforced at decode** (`generate.CallForce`, armed by
  `server.armCallForce`): the markup the template itself writes after a closed thought (`chat.forcedCallText`; Qwen3.8
  `\n\n<tool_call>\n<function=`, MiMo `<tool_call><function=`) is committed as tokens, through the name and its
  delimiter when one is named. For `required` the name is then one of the DECLARED names (each with its delimiter, as
  ids: a lone continuation is committed, a branch is a sample among its ids), since the model wrote `getTime` and
  `call: get_time` there. The arguments stay the model's; the parse chain and its valid-JSON invariant run unchanged.
- **They are also prompt-level**: the instruction closes the last user turn (or opens one after tool results), so every
  byte before the last message stays and the prefix cache still serves it; the generic fallback inlines it with the
  tools. A continuation keeps the client's prefill: no instruction, no force.
- **A forced call does not skip the thought**: it runs free (under any reasoning budget) and the call takes the
  position after the closer; a closed-thought prompt takes it at token 0. Where the template leaves the opener to the
  model (MiMo), the opener is committed at token 0, as a prompt-opened block would be. A thought that ends the turn
  (EOS, `length`, loop stop) ends it without a call.
- While the call is pending each successor is decided after its predecessor is known (no pipelined fast path), draft
  paths stay off for the request (`draftsRefused`), and the batched tick leaves the slot serial (`forced_call`).
- A named choice for a function the request does not declare is a 400 on every surface. Guard:
  `tests/test_tool_choice_forced.sh`.

## Thinking

- Strip pos-0 unclosed openers; `trimTrailingThinkClosers`; unparsed tool markup never rides out as reasoning OR
  content (`trimLeakedToolMarkup`, ONE cut).
- Whether a prompt ends inside a think block is a property of the RENDERED BYTES (`promptOpensThink`), never ANDed
  with `enable_thinking`; `in_think_block` seeds from `prompt_opened_think` ALONE at every stream site; a model can
  open its OWN block (`modelThinkOpener`).
- **Streaming + tools + thinking**: buffer until pattern resolution; reasoning streams INCREMENTALLY on the tools
  path (`.hold_thinking` + `unstreamedReasoning`, never a resend); the think gate scans with a CURSOR (`ThinkScan`).
- Thinking-off is enforced in the PROMPT; generated reasoning is ALWAYS delivered (every site splits via
  `splitThinkBlock(text, true, …)`).
- **A thought is decided at its first byte when no opener can start it** (`chat.thinkOpenerPossible`: every opener
  starts with `<` or is a Muse `assistant` / `to=` header); otherwise the three stream sites wait for 7 bytes. On the
  surface budget path (`armThinkBound` declined) the budget then counts from that earlier consumption.
- **An open thought streams only what its closed split delivers** (`trim(thought, "\n ")`): a trailing `"\n "` run and
  a close tag still arriving wait (`chat.settledReasoning` on the tools path, `chat.openThoughtFlush` /
  `closedThoughtDelta` on raw flushes). Streaming the newline before `</think>` made stream and non-stream differ.
- **A thought the length limit cuts ends on what its split delivers** (`chat.cutThoughtDelta`): a lone opener is
  structure, and a thinking block or reasoning item opens at its first delta, so an empty thought streams none.

## Loop stops

A short exact cycle convicts on SPAN (`degenerate_loop_min_span` 128; a 24-wide map row is legit), near-repeat needs
THREE low ratios incl. PROGRESS (1024-token window, `near_repeat_min_span` 4096), long-period tier 9..64 at 10 reps.
Cuts are intentional stops: `finish_reason "stop"`, `finish_details:{"type":"repetition_loop"}`, `[loop-stop]`
logged, non-streaming trimmed to the span start. Guard: `tests/test_loop_stop_signal.sh`.
