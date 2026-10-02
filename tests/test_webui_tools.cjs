// Exercise the actual page functions without a browser or model.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('src/webui/index.html', 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
const context = vm.createContext({
  $: () => ({ value: '' }),
});
const start = script.indexOf('function wireMessages(');
const end = script.indexOf('function createStreamingView(', start);
vm.runInContext(script.slice(start, end), context);
const tools = [{ type: 'function', function: { name: 'web_search' } }];
const history = [
  { role: 'user', content: 'Search' },
  { role: 'assistant', content: '', reasoning: 'Look it up', tool_calls: [{ id: 'c1', type: 'function', function: { name: 'web_search', arguments: '{"query":"sushi"}' } }] },
  { role: 'tool', tool_call_id: 'c1', content: 'Found a page' },
];
assert.equal(context.buildRequest('test', history).tools, undefined);
assert.deepEqual(context.buildRequest('test', history, tools).tools, tools);
const wire = context.wireMessages(history);
assert.equal(wire.length, 3);
assert.equal(wire[1].tool_calls[0].id, 'c1');
assert.equal(wire[1].reasoning_content, 'Look it up');
assert.equal(wire[2].tool_call_id, 'c1');
console.log('Web UI tools: passed');
const loopStart = script.indexOf('async function runResearchTurn(');
vm.runInContext(script.slice(loopStart, script.indexOf('async function runTurn(', loopStart)), context);
context.renderTranscript = () => {};
const call = (id) => ({ id, type: 'function', function: { name: 'web_search', arguments: '{}' } });
(async () => {
  let count = 0;
  let executed = 0;
  context.streamReply = async (model, messages, signal, defs) => {
    count++;
    if (count <= 8) {
      assert.equal(defs, tools);
      return { text: '', tool_calls: [call(`c${count}`)] };
    }
    assert.equal(defs, null);
    return { text: 'Done', tool_calls: [] };
  };
  context.callResearchTools = async () => { executed++; return { text: 'Result' }; };
  const messages = [];
  await context.runResearchTurn('test', messages, { aborted: false }, tools, false);
  assert.equal(count, 9);
  assert.equal(executed, 8);
  assert.equal(messages.at(-1).content, 'Done');
  assert.equal(messages.filter((m) => m.role === 'tool').length, 8);

  const signal = { aborted: false };
  context.streamReply = async () => ({ text: '', tool_calls: [call('a'), call('b')] });
  context.callResearchTools = async () => { signal.aborted = true; throw new Error('abort'); };
  const cancelled = [];
  await context.runResearchTurn('test', cancelled, signal, tools, false);
  assert.equal(cancelled.length, 3);
  assert.equal(cancelled[1].tool_call_id, 'a');
  assert.equal(cancelled[2].tool_call_id, 'b');
  assert.equal(cancelled[2].content, 'Tool call cancelled');

  context.streamReply = async (model, messages, signal, defs) => {
    assert.equal(defs, undefined);
    return { text: 'No tools', tool_calls: [] };
  };
  context.callResearchTools = async () => assert.fail('Disabled tools executed');
  await context.runResearchTurn('test', [], { aborted: false }, undefined, false);
  console.log('Web UI tool loop: round limit, cancellation, disabled pack passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
const streaming = vm.createContext({
  $: () => ({ value: '' }), TextDecoder, Uint8Array, performance,
  perfMon: { begin() {}, finish() {}, delta() {} },
  createStreamingView: () => ({ text: '', reasoning: '', schedule() {}, finalize() {} }),
  replyMeta: () => null,
});
vm.runInContext(script.slice(start, end), streaming);
const sseStart = script.indexOf('function readSsePayloads(');
vm.runInContext(script.slice(sseStart, script.indexOf('\n/*', sseStart)), streaming);
const streamStart = script.indexOf('async function streamReply(');
vm.runInContext(script.slice(streamStart, script.indexOf('function setChatBusy(', streamStart)), streaming);
(async () => {
  for (const finish of ['tool_calls', 'length']) {
    const events = [
      { choices: [{ delta: { tool_calls: [{ index: 0, id: 'c1', function: { name: 'web_search', arguments: '{"query":' } }] } }] },
      { choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: '"sushi"}' } }] } }] },
      { choices: [{ delta: {}, finish_reason: finish }] },
    ];
    const bytes = Buffer.from(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('') + 'data: [DONE]\n\n');
    let offset = 0;
    streaming.api = async () => ({ ok: true, body: { getReader: () => ({
      read: async () => offset >= bytes.length ? { done: true } : { done: false, value: bytes.subarray(offset, offset = Math.min(offset + 7, bytes.length)) },
    }) } });
    const reply = await streaming.streamReply('test', [], { aborted: false }, tools);
    assert.equal(reply.tool_calls.length, finish === 'tool_calls' ? 1 : 0);
    if (finish === 'tool_calls') assert.equal(reply.tool_calls[0].function.arguments, '{"query":"sushi"}');
  }
  console.log('Web UI streaming: fragmented calls and token-limit truncation passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
