import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, readFile, readdir, stat, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { referenceContext } from '../bridge/hooks.mjs';

const cli = fileURLToPath(new URL('../bin/memory-lens.mjs', import.meta.url));
async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'memory-lens-bridge-test-'));
  const calls = [], mappings = new Map(), captured = new Map();
  const control = { failIngest: false, failAll: false, context: 'Refund window: 14 days', delay: 0, revision: 3 };
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    calls.push({ body, authorization: req.headers.authorization });
    if (control.delay) await new Promise(resolve => setTimeout(resolve, control.delay));
    res.setHeader('Content-Type', 'application/json');
    if (body.action === 'context' && body.query?.length > 500) { res.writeHead(400); res.end(JSON.stringify({ error: { code: 'invalid_input', message: 'query exceeds 500' } })); return; }
    if (control.failAll || (body.action === 'ingest_turn' && (control.failIngest || (control.failTurn && body.native_turn_id === control.failTurn)))) {
      res.writeHead(503); res.end(JSON.stringify({ error: { code: 'upstream_unavailable', message: 'Provider failed with synthetic-secret-should-not-log' } })); return;
    }
    let result;
    if (body.action === 'ensure_session') {
      const key = body.source + ':' + body.external_session_id;
      if (!mappings.has(key)) mappings.set(key, 'shared-memory');
      result = { session_id: mappings.get(key), revision: control.revision };
    } else if (body.action === 'ingest_turn') {
      if (control.idempotency) {
        const payload = JSON.stringify(body), prior = captured.get(body.turn_id);
        if (prior && prior !== payload) { res.writeHead(409); res.end(JSON.stringify({ error: { code: 'conflicting_turn' } })); return; }
        if (!prior) { captured.set(body.turn_id, payload); control.revision++; }
        result = { session_id: body.session_id, revision: control.revision, status: prior ? 'duplicate' : 'applied', changed_ids: [] };
      } else result = { session_id: body.session_id, revision: 1, status: control.nonDurable ? 'pending' : 'applied', changed_ids: ['fact-1'] };
    }
    else if (body.action === 'context') result = { session_id: body.session_id, revision: control.revision, text: control.context, node_ids: ['fact-1'] };
    else if (body.action === 'graph') result = { session_id: body.session_id, revision: 3, nodes: [{ id: 'fact-1', content: 'Refund window: 14 days' }], edges: [], turns: [] };
    else if (body.action === 'command') result = { session_id: body.session_id, revision: 4, status: 'applied', changed_ids: ['fact-1'], message: 'Updated memory' };
    else if (body.action === 'list_sessions') result = { sessions: [] };
    else result = { ok: true, api_version: 1, model: 'gpt-6-astra', openai_configured: false };
    res.end(JSON.stringify(result));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const env = { ...process.env, MEMORY_LENS_API_URL: 'http://127.0.0.1:' + server.address().port, MEMORY_LENS_TOKEN: 'synthetic-test-token', MEMORY_LENS_STATE_DIR: join(root, 'state'), MEMORY_LENS_TIMEOUT_MS: '500', MEMORY_LENS_LAYERED_CONTEXT: '0' };
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await rm(root, { recursive: true, force: true }); });
  return { root, calls, mappings, control, env };
}
function run(args, input, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli, ...args], { env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = ''; child.stdout.on('data', chunk => stdout += chunk); child.stderr.on('data', chunk => stderr += chunk);
    child.on('error', reject); child.on('close', code => resolve({ code, stdout, stderr }));
    child.stdin.end(typeof input === 'string' ? input : input ? JSON.stringify(input) : '');
  });
}
async function files(root) {
  let entries; try { entries = await readdir(root, { withFileTypes: true }); } catch { return []; }
  const nested = await Promise.all(entries.map(entry => entry.isDirectory() ? files(join(root, entry.name)) : [join(root, entry.name)]));
  return nested.flat();
}

// Catches dropping explicit source/session mapping or sending the token in a JSON body.
test('manual CLI ingest uses stable native session and exact version 1 action fields', async t => {
  const f = await fixture(t);
  const event = { source: 'editor', external_session_id: 'native-a', turn_id: 'turn-1', user_message: 'Use Stripe', assistant_message: 'Agreed' };
  const first = await run(['ingest'], event, f.env);
  assert.equal(first.code, 0, first.stderr);
  assert.equal(JSON.parse(first.stdout).status, 'applied');
  assert.deepEqual(f.calls[0].body, { action: 'ensure_session', source: 'editor', external_session_id: 'native-a' });
  const { turn_id, ...sent } = f.calls[1].body;
  assert.match(turn_id, /^memory-turn:[a-f0-9]{64}$/);
  assert.deepEqual(sent, { action: 'ingest_turn', session_id: 'shared-memory', source: 'editor', external_session_id: 'native-a', native_turn_id: 'turn-1', user_message: 'Use Stripe', assistant_message: 'Agreed' });
  assert.ok(f.calls.every(call => call.authorization === 'Bearer synthetic-test-token'));
  const second = await run(['ingest'], { ...event, turn_id: 'turn-2' }, f.env);
  assert.equal(second.code, 0);
  assert.equal(f.calls.at(-1).body.session_id, 'shared-memory');
});

// Catches creating one graph per chat or colliding identical native turn IDs across origins.
test('CLI shares one memory graph while preserving distinct chat/tool provenance and event identity', async t => {
  const f = await fixture(t);
  for (const [source, external_session_id] of [['cli', 'a'], ['cli', 'b'], ['editor', 'a']]) {
    const result = await run(['ingest'], { source, external_session_id, turn_id: 'same-turn', user_message: 'Synthetic fact' }, f.env);
    assert.equal(result.code, 0, result.stderr);
  }
  const ingests = f.calls.filter(call => call.body.action === 'ingest_turn');
  assert.deepEqual(ingests.map(call => call.body.session_id), ['shared-memory', 'shared-memory', 'shared-memory']);
  assert.equal(new Set(ingests.map(call => call.body.turn_id)).size, 3);
  assert.deepEqual(ingests.map(call => [call.body.source, call.body.external_session_id, call.body.native_turn_id]), [['cli', 'a', 'same-turn'], ['cli', 'b', 'same-turn'], ['editor', 'a', 'same-turn']]);
});

// Catches defaulting a graph/command to an arbitrary session, or changing action names.
test('graph context and natural-language command target only the explicit API session', async t => {
  const f = await fixture(t);
  const graph = await run(['graph', '--session-id', 'selected-chat'], null, f.env);
  assert.equal(graph.code, 0, graph.stderr);
  assert.equal(JSON.parse(graph.stdout).nodes[0].content, 'Refund window: 14 days');
  const context = await run(['context', '--session-id', 'selected-chat', '--query', 'refunds', '--limit', '2'], null, f.env);
  assert.equal(context.code, 0);
  const command = await run(['command', '--session-id', 'selected-chat', '--request-id', 'request-1'], 'Change refund window to 14 days', f.env);
  assert.equal(command.code, 0);
  assert.deepEqual(f.calls.map(call => call.body), [
    { action: 'graph', session_id: 'selected-chat' },
    { action: 'context', session_id: 'selected-chat', query: 'refunds', limit: 2 },
    { action: 'command', session_id: 'selected-chat', command: 'Change refund window to 14 days', request_id: 'request-1' }
  ]);
});

// Catches losing failed turns, insecure local modes, or retrying with a new turn ID.
test('failed ingress is private durable outbox and flush retries the same turn', async t => {
  const f = await fixture(t); f.control.failIngest = true;
  const event = { source: 'cli', external_session_id: 'native-a', turn_id: 'turn-1', user_message: 'Use Stripe', assistant_message: 'Agreed' };
  const result = await run(['ingest'], event, f.env);
  assert.equal(result.code, 1);
  assert.equal(JSON.parse(result.stdout).status, 'queued');
  assert.doesNotMatch(result.stdout + result.stderr, /synthetic-secret-should-not-log|synthetic-test-token/);
  const pending = (await files(f.env.MEMORY_LENS_STATE_DIR)).filter(path => path.includes('/outbox/'));
  assert.equal(pending.length, 1);
  assert.equal((await stat(pending[0])).mode & 0o777, 0o600);
  assert.equal((await stat(f.env.MEMORY_LENS_STATE_DIR)).mode & 0o777, 0o700);
  const queuedId = JSON.parse(await readFile(pending[0], 'utf8')).event.turn_id;
  assert.match(queuedId, /^memory-turn:[a-f0-9]{64}$/);
  f.control.failIngest = false;
  const flushed = await run(['flush'], null, f.env);
  assert.equal(flushed.code, 0, flushed.stderr);
  assert.equal(JSON.parse(flushed.stdout).sent, 1);
  assert.equal((await files(f.env.MEMORY_LENS_STATE_DIR)).filter(path => path.includes('/outbox/')).length, 0);
  assert.equal(f.calls.at(-1).body.turn_id, queuedId);
  assert.equal(f.calls.at(-1).body.native_turn_id, 'turn-1');
});

// Catches delaying capture until assistant completion or combining native prompt identities.
test('Codex captures each user prompt immediately by native session and turn then injects bounded context', async t => {
  const f = await fixture(t);
  for (const [session, turn, prompt] of [['native-a', 't1', 'First prompt'], ['native-a', 't2', 'Second prompt'], ['native-b', 't1', 'Other chat']]) {
    const result = await run(['hook', 'UserPromptSubmit'], { hook_event_name: 'UserPromptSubmit', session_id: session, turn_id: turn, prompt, transcript_path: '/never/read/this' }, f.env);
    assert.equal(result.code, 0, result.stderr);
    const output = JSON.parse(result.stdout);
    assert.equal(output.hookSpecificOutput.hookEventName, 'UserPromptSubmit');
    assert.match(output.hookSpecificOutput.additionalContext, /Refund window: 14 days/);
    assert.match(output.hookSpecificOutput.additionalContext, /reference data/i);
  }
  assert.equal(f.calls.filter(call => call.body.action === 'ingest_turn').length, 3, 'capture happens before any Stop');
  const beforeStops = f.calls.length;
  for (const [session, turn, answer] of [['native-a', 't1', 'First answer'], ['native-b', 't1', 'Other answer'], ['native-a', 't2', 'Second answer']]) {
    const result = await run(['hook', 'Stop'], { hook_event_name: 'Stop', session_id: session, turn_id: turn, last_assistant_message: answer, stop_hook_active: false }, f.env);
    assert.equal(result.code, 0);
    assert.deepEqual(JSON.parse(result.stdout), {});
  }
  assert.equal(f.calls.length, beforeStops, 'Stop never sends assistant text or performs network calls');
  const ingested = f.calls.filter(call => call.body.action === 'ingest_turn').map(call => call.body);
  assert.deepEqual(ingested.map(({ turn_id, ...body }) => body), [
    { action: 'ingest_turn', session_id: 'shared-memory', source: 'codex', external_session_id: 'native-a', native_turn_id: 't1', base_revision: 3, user_message: 'First prompt' },
    { action: 'ingest_turn', session_id: 'shared-memory', source: 'codex', external_session_id: 'native-a', native_turn_id: 't2', base_revision: 3, user_message: 'Second prompt' },
    { action: 'ingest_turn', session_id: 'shared-memory', source: 'codex', external_session_id: 'native-b', native_turn_id: 't1', base_revision: 3, user_message: 'Other chat' }
  ]);
  assert.match(ingested[0].turn_id, /^memory-turn:[a-f0-9]{64}$/);
  assert.notEqual(ingested[0].turn_id, ingested[1].turn_id);
  const saved = (await files(f.env.MEMORY_LENS_STATE_DIR)).filter(path => path.includes('/prompts/'));
  assert.equal(saved.length, 3);
  assert.ok((await Promise.all(saved.map(path => stat(path)))).every(info => (info.mode & 0o777) === 0o600));
});

// Catches losing an offline prompt before agent work begins or contaminating it with assistant text.
test('offline UserPromptSubmit queues only the user prompt and later prompts flush in order', async t => {
  const f = await fixture(t); f.control.failAll = true;
  const prompt = await run(['hook', 'UserPromptSubmit'], { session_id: 'native-a', turn_id: 't1', prompt: 'Visible synthetic prompt' }, f.env);
  assert.equal(prompt.code, 0); assert.deepEqual(JSON.parse(prompt.stdout), {});
  const saved = await files(f.env.MEMORY_LENS_STATE_DIR);
  assert.equal(saved.length, 2); assert.ok((await Promise.all(saved.map(path => stat(path)))).every(info => (info.mode & 0o777) === 0o600));
  const beforeStop = f.calls.length;
  const stop = await run(['hook', 'Stop'], { session_id: 'native-a', turn_id: 't1', last_assistant_message: 'assistant-only-secret-claim' }, f.env);
  assert.equal(stop.code, 0); assert.deepEqual(JSON.parse(stop.stdout), {});
  assert.doesNotMatch(stop.stderr, /synthetic-secret-should-not-log|synthetic-test-token/);
  assert.equal(f.calls.length, beforeStop);
  assert.doesNotMatch((await Promise.all(saved.map(path => readFile(path, 'utf8')))).join(''), /assistant-only-secret-claim|assistant_message/);
  f.control.failAll = false;
  const next = await run(['hook', 'UserPromptSubmit'], { session_id: 'native-a', turn_id: 't2', prompt: 'Next prompt' }, f.env);
  assert.equal(next.code, 0);
  assert.deepEqual(f.calls.filter(call => call.body.action === 'ingest_turn').map(call => call.body.user_message), ['Visible synthetic prompt', 'Next prompt']);
  assert.ok(f.calls.filter(call => call.body.action === 'ingest_turn').every(call => !Object.hasOwn(call.body, 'assistant_message')));
  assert.equal((await files(f.env.MEMORY_LENS_STATE_DIR)).filter(path => path.includes('/prompts/')).length, 2);
});

// Catches ingesting an unknown Stop or mixing a missing turn into another prompt.
test('Codex missing IDs or unmatched Stop never ingest another conversation', async t => {
  const f = await fixture(t);
  for (const [event, payload] of [['UserPromptSubmit', { session_id: 'a', prompt: 'No turn' }], ['Stop', { session_id: 'b', turn_id: 'unknown', last_assistant_message: 'Answer' }]]) {
    const result = await run(['hook', event], payload, f.env);
    assert.equal(result.code, 0); assert.deepEqual(JSON.parse(result.stdout), {});
  }
  assert.equal(f.calls.length, 0);
  const malformed = await run(['hook', 'Stop'], '{broken', f.env);
  assert.equal(malformed.code, 0); assert.deepEqual(JSON.parse(malformed.stdout), {});
});

// Catches not rehydrating memory after compaction, or returning unbounded context.
test('SessionStart compact restores current chat context and caps text', async t => {
  const f = await fixture(t); f.control.context = 'x'.repeat(20000);
  const result = await run(['hook', 'SessionStart'], { session_id: 'native-a', source: 'compact' }, f.env);
  assert.equal(result.code, 0);
  const output = JSON.parse(result.stdout).hookSpecificOutput;
  assert.equal(output.hookEventName, 'SessionStart');
  assert.ok(output.additionalContext.length < 12500);
  assert.equal(f.calls.at(-1).body.session_id, 'shared-memory');
});

// Catches installer replacement, shell quoting bugs in paths, and duplicate registration.
test('hook installer preserves definitions and installed commands execute from other directories', async t => {
  const f = await fixture(t); const project = join(f.root, "project with ' quote");
  await mkdir(join(project, '.codex'), { recursive: true });
  const path = join(project, '.codex', 'hooks.json');
  const original = { description: 'Existing project hooks', hooks: { Stop: [{ hooks: [{ type: 'command', command: 'echo existing' }] }], PreToolUse: [{ matcher: 'Bash', hooks: [] }] } };
  await writeFile(path, JSON.stringify(original));
  const first = await run(['install-codex-hooks', '--project', project], null, f.env);
  assert.equal(first.code, 0, first.stderr);
  const second = await run(['install-codex-hooks', '--project', project], null, f.env);
  assert.equal(second.code, 0, second.stderr);
  const installed = JSON.parse(await readFile(path, 'utf8'));
  assert.deepEqual(installed.hooks.Stop[0], original.hooks.Stop[0]);
  assert.deepEqual(installed.hooks.PreToolUse, original.hooks.PreToolUse);
  assert.equal(installed.hooks.Stop.length, 2);
  const command = installed.hooks.UserPromptSubmit[0].hooks[0].command;
  const result = await new Promise((resolve, reject) => {
    const child = spawn('/bin/sh', ['-c', command], { cwd: f.root, env: f.env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = ''; child.stdout.on('data', chunk => stdout += chunk); child.stderr.on('data', chunk => stderr += chunk);
    child.on('error', reject); child.on('close', code => resolve({ code, stdout, stderr }));
    child.stdin.end(JSON.stringify({ session_id: 'a', turn_id: 't1', prompt: 'Test installed command' }));
  });
  assert.equal(result.code, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).hookSpecificOutput.hookEventName, 'UserPromptSubmit');
});

// Catches JSON-RPC output contamination or tools that silently use an arbitrary chat.
test('MCP stdio lists explicit tools and routes an ingestion to the same API protocol', async t => {
  const f = await fixture(t);
  const messages = [
    { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'fixture', version: '1' } } },
    { jsonrpc: '2.0', method: 'notifications/initialized' },
    { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} },
    { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'memory_ingest_turn', arguments: { turn_id: 't1', user_message: 'Use Stripe' } } },
    { jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'memory_context', arguments: { query: 'refunds' } } },
    { jsonrpc: '2.0', id: 5, method: 'tools/call', params: { name: 'memory_graph', arguments: { session_id: 'foreign-session' } } }
  ];
  const result = await run(['mcp'], messages.map(message => JSON.stringify(message)).join('\n') + '\n', { ...f.env, MEMORY_LENS_SOURCE: 'editor', MEMORY_LENS_EXTERNAL_SESSION_ID: 'native-a' });
  assert.equal(result.code, 0, result.stderr);
  const responses = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(responses.length, 5);
  assert.equal(responses[0].result.serverInfo.name, 'memory-lens');
  const tools = responses.find(response => response.id === 2).result.tools;
  assert.ok(tools.find(tool => tool.name === 'memory_ingest_turn'));
  assert.match(tools.find(tool => tool.name === 'memory_ingest_turn').description, /does not automatically capture/i);
  assert.equal(responses.find(response => response.id === 3).result.structuredContent.status, 'applied');
  assert.match(responses.find(response => response.id === 4).result.structuredContent.text, /14 days/);
  assert.equal(responses.find(response => response.id === 5).result.isError, true);
  assert.ok(f.calls.every(call => call.body.session_id !== 'foreign-session'));
});

// Catches rebilling duplicate prompt hooks or admitting assistant claims during continuations.
test('duplicate user prompt hooks are idempotent and changed Stop answers never become memory', async t => {
  const f = await fixture(t);
  await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt: 'Use Stripe' }, f.env);
  await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt: 'Use Stripe' }, f.env);
  const beforeStop = f.calls.length;
  for (const last_assistant_message of ['assistant-secret-first', 'assistant-secret-first', 'assistant-secret-final']) {
    const result = await run(['hook', 'Stop'], { session_id: 'a', turn_id: 't1', last_assistant_message, stop_hook_active: true }, f.env);
    assert.equal(result.code, 0); assert.deepEqual(JSON.parse(result.stdout), {});
  }
  const ingests = f.calls.filter(call => call.body.action === 'ingest_turn');
  assert.equal(ingests.length, 1);
  assert.equal(f.calls.length, beforeStop);
  assert.equal(ingests[0].body.user_message, 'Use Stripe');
  assert.ok(!Object.hasOwn(ingests[0].body, 'assistant_message'));
  assert.doesNotMatch(JSON.stringify(f.calls), /assistant-secret/);
  const stateText = (await Promise.all((await files(f.env.MEMORY_LENS_STATE_DIR)).map(path => readFile(path, 'utf8')))).join('');
  assert.doesNotMatch(stateText, /assistant-secret|assistant_message/);
  const conflicting = await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt: 'Use a different gateway' }, f.env);
  assert.deepEqual(JSON.parse(conflicting.stdout), {});
  assert.match(conflicting.stderr, /conflicting_turn/);
  assert.equal(f.calls.length, beforeStop);
});

// Catches credential rotations causing old private outbox content to go to a different owner.
test('outbox is scoped to endpoint and credential and another token cannot flush it', async t => {
  const f = await fixture(t); f.control.failAll = true;
  await run(['ingest'], { source: 'cli', external_session_id: 'a', turn_id: 't1', user_message: 'Private synthetic fact' }, f.env);
  f.control.failAll = false; const before = f.calls.length;
  const other = await run(['flush'], null, { ...f.env, MEMORY_LENS_TOKEN: 'other-synthetic-owner' });
  assert.equal(other.code, 0); assert.equal(JSON.parse(other.stdout).sent, 0);
  assert.equal(f.calls.length, before);
  assert.equal((await files(f.env.MEMORY_LENS_STATE_DIR)).length, 1);
});

// Catches updating an old capture's base revision during retry and undoing a manual correction.
test('hook outbox preserves original context revision across outages and retries', async t => {
  const f = await fixture(t);
  f.control.failIngest = true;
  await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt: 'Use Stripe' }, f.env);
  const outbox = (await files(f.env.MEMORY_LENS_STATE_DIR)).filter(path => path.includes('/outbox/'));
  assert.equal(outbox.length, 1, 'failed user prompt must be durably queued');
  assert.equal(JSON.parse(await readFile(outbox[0], 'utf8')).event.base_revision, 3);
  f.control.revision = 10;
  await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt: 'Use Stripe' }, f.env);
  assert.equal(JSON.parse(await readFile(outbox[0], 'utf8')).event.base_revision, 3, 'retry cannot rebase the captured prompt');
  f.control.failIngest = false;
  await run(['flush'], null, f.env);
  assert.equal(f.calls.filter(call => call.body.action === 'ingest_turn').at(-1).body.base_revision, 3);
  const records = (await files(f.env.MEMORY_LENS_STATE_DIR)).filter(path => path.includes('/prompts/'));
  const savedPrompt = JSON.parse(await readFile(records[0], 'utf8'));
  assert.ok(savedPrompt.completed_at > 0);
  assert.equal(savedPrompt.completed_ids.length, 1);
});

// Catches silently discarding a pending exchange on a 200 response that isn't durable.
test('non-durable ingestion response remains queued', async t => {
  const f = await fixture(t);
  f.control.nonDurable = true;
  const result = await run(['ingest'], { source: 'cli', external_session_id: 'a', turn_id: 't1', user_message: 'Fact', base_revision: 5 }, f.env);
  assert.equal(result.code, 1); assert.equal(JSON.parse(result.stdout).status, 'queued');
  assert.equal((await files(f.env.MEMORY_LENS_STATE_DIR)).filter(path => path.includes('/outbox/')).length, 1);
});

// Catches normal long prompts making retrieval fail even when the full capture is valid.
test('long Codex prompt uses a bounded retrieval query while retaining full visible evidence', async t => {
  const f = await fixture(t), prompt = 'Billing details '.repeat(60);
  const submitted = await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt }, f.env);
  assert.match(JSON.parse(submitted.stdout).hookSpecificOutput?.additionalContext || '', /14 days/);
  await run(['hook', 'Stop'], { session_id: 'a', turn_id: 't1', last_assistant_message: 'Understood' }, f.env);
  assert.equal(f.calls.find(call => call.body.action === 'ingest_turn').body.user_message, prompt);
  assert.equal(f.calls.find(call => call.body.action === 'context').body.query.length, 500);
});

// Catches permanent backend-invalid payloads being admitted to an endlessly retried outbox.
test('oversized visible exchange is rejected before network or durable queue admission', async t => {
  const f = await fixture(t);
  for (const user_message of ['x'.repeat(20001), 'Invalid\u0000capture']) {
    const result = await run(['ingest'], { source: 'cli', external_session_id: 'a', turn_id: 't1', user_message }, f.env);
    assert.equal(result.code, 1); assert.equal(JSON.parse(result.stdout).error.code, 'invalid_input');
  }
  assert.equal(f.calls.length, 0); assert.equal((await files(f.env.MEMORY_LENS_STATE_DIR)).length, 0);
});

// Catches a valid long native turn ID becoming invalid after adding the capture digest.
test('long native turn identifiers produce bounded deterministic capture IDs', async t => {
  const f = await fixture(t), turn_id = 'native-'.repeat(35);
  await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id, prompt: 'Use Stripe' }, f.env);
  await run(['hook', 'Stop'], { session_id: 'a', turn_id, last_assistant_message: 'Understood' }, f.env);
  const ingestion = f.calls.find(call => call.body.action === 'ingest_turn');
  assert.ok(ingestion); assert.ok(ingestion.body.turn_id.length <= 255);
});

// Catches hash filename sorting replaying a newer correction before the older queued fact.
test('outbox replays queued exchanges in their capture order within one native chat', async t => {
  const f = await fixture(t); f.control.failAll = true;
  for (const [turn_id, user_message] of [['older0', 'Refund window is 7 days'], ['newer0', 'Refund window is 14 days']]) {
    await run(['ingest'], { source: 'cli', external_session_id: 'a', turn_id, user_message, base_revision: 0 }, f.env);
  }
  f.control.failAll = false; const before = f.calls.length;
  const result = await run(['flush'], null, f.env);
  assert.equal(result.code, 0);
  assert.deepEqual(f.calls.slice(before).filter(call => call.body.action === 'ingest_turn').map(call => call.body.native_turn_id), ['older0', 'newer0']);
});

// Catches a newer ingestion bypassing an unresolved earlier turn in the same chat.
test('a failed older capture blocks newer same-chat captures while another chat can proceed', async t => {
  const f = await fixture(t); f.control.failTurn = 'older';
  await run(['ingest'], { source: 'cli', external_session_id: 'a', turn_id: 'older', user_message: '7 days' }, f.env);
  const newer = await run(['ingest'], { source: 'cli', external_session_id: 'a', turn_id: 'newer', user_message: '14 days' }, f.env);
  assert.equal(newer.code, 1); assert.equal(JSON.parse(newer.stdout).status, 'queued');
  assert.ok(!f.calls.some(call => call.body.action === 'ingest_turn' && call.body.native_turn_id === 'newer'));
  const other = await run(['ingest'], { source: 'cli', external_session_id: 'b', turn_id: 'independent', user_message: 'Other chat' }, f.env);
  assert.equal(other.code, 0);
  assert.equal(f.calls.at(-1).body.native_turn_id, 'independent');
});

// Catches retention deleting native identity/base data or pending prompt evidence.
test('pending prompts retain evidence and completed prompts compact to immutable idempotency tombstones', async t => {
  const f = await fixture(t);
  f.control.failIngest = true;
  await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt: 'Use Stripe' }, f.env);
  const path = (await files(f.env.MEMORY_LENS_STATE_DIR)).find(path => path.includes('/prompts/'));
  const old = JSON.parse(await readFile(path, 'utf8'));
  await writeFile(path, JSON.stringify({ ...old, completed_at: Date.now() - 172800000 }));
  await run(['hook', 'UserPromptSubmit'], { session_id: 'b', turn_id: 't2', prompt: 'Other chat' }, f.env);
  assert.ok((await files(f.env.MEMORY_LENS_STATE_DIR)).includes(path), 'pending prompt evidence must remain available');
  assert.equal(JSON.parse(await readFile(path, 'utf8')).prompt, 'Use Stripe');
  f.control.failIngest = false; await run(['flush'], null, f.env);
  const completed = JSON.parse(await readFile(path, 'utf8'));
  assert.equal(completed.completed_ids.length, 1);
  assert.equal(completed.base_revision, 3);
  await writeFile(path, JSON.stringify({ ...completed, completed_at: Date.now() - 172800000 }));
  await run(['hook', 'SessionStart'], { session_id: 'a', source: 'compact' }, f.env);
  const tombstone = JSON.parse(await readFile(path, 'utf8'));
  assert.ok(!Object.hasOwn(tombstone, 'prompt'));
  assert.equal(tombstone.base_revision, 3);
  assert.match(tombstone.prompt_hash, /^[a-f0-9]{64}$/);
  const beforeDuplicate = f.calls.filter(call => call.body.action === 'ingest_turn').length;
  f.control.revision = 10;
  await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt: 'Use Stripe' }, f.env);
  assert.equal(f.calls.filter(call => call.body.action === 'ingest_turn').length, beforeDuplicate);
  assert.equal(JSON.parse(await readFile(path, 'utf8')).base_revision, 3);
});

test('the entire layered hook envelope including headers and pending diagnostics fits 24000 characters', () => {
  const context = referenceContext({ session_id: 's', revision: 7, pending_ingestion: 999, memory: { node_ids: ['s:fact'], text: 'x'.repeat(12000) }, code_graph: { status: 'ready', repo_id: 'r', graph: { nodes: Array.from({ length: 20 }, (_, i) => ({ id: String(i) + 'q'.repeat(1020) })), edges: [] }, text: 'y'.repeat(12000) } });
  assert.ok(context.length <= 24000);
  const encoded = context.slice(context.indexOf('\n') + 1).split('\n')[0];
  const payload = JSON.parse(encoded);
  assert.ok(payload.truncated);
  assert.ok(payload.code_graph.node_ids.every(id => id.length >= 1021), 'canonical IDs cannot be clipped');
  assert.match(context, /999.*pending/);
});

test('reference data clipping is explicit and escaped legacy text stays inside the complete envelope bound', () => {
  for (const text of ['x'.repeat(20000), 'Fact " quoted\n'.repeat(1500)]) {
    const context = referenceContext({ session_id: 's', revision: 7, pending_ingestion: 2, node_ids: ['s:fact'], text });
    assert.ok(context.length <= 12500);
    const payload = JSON.parse(context.slice(context.indexOf('\n') + 1).split('\n')[0]);
    assert.equal(payload.truncated, true);
    assert.match(context, /2.*pending/);
  }
});

test('oversized encoded user prompts never enter automatic private state or HTTP admission', async t => {
  const f = await fixture(t);
  for (const prompt of ['x'.repeat(20001), '\u0001'.repeat(20000), 'Invalid\u0000prompt']) {
    const result = await run(['hook', 'UserPromptSubmit'], { session_id: 'a', turn_id: 't1', prompt }, f.env);
    assert.equal(result.code, 0);
    assert.deepEqual(JSON.parse(result.stdout), {});
    assert.match(result.stderr, /invalid_input/);
  }
  assert.equal(f.calls.length, 0);
  assert.equal((await files(f.env.MEMORY_LENS_STATE_DIR)).length, 0);
});

test('shared context drains older prompts from another native chat before retrieval', async t => {
  const f = await fixture(t); f.control.failAll = true;
  await run(['hook', 'UserPromptSubmit'], { session_id: 'chat-a', turn_id: 'first', prompt: 'Use British English' }, f.env);
  f.control.failAll = false;
  const before = f.calls.length;
  const resumed = await run(['hook', 'SessionStart'], { session_id: 'chat-b', source: 'startup' }, f.env);
  assert.ok(JSON.parse(resumed.stdout).hookSpecificOutput);
  const requests = f.calls.slice(before).map(call => call.body);
  assert.ok(requests.find(body => body.action === 'ingest_turn' && body.external_session_id === 'chat-a'));
  assert.ok(requests.findIndex(body => body.action === 'ingest_turn') < requests.findIndex(body => body.action === 'context'));
  assert.equal((await files(f.env.MEMORY_LENS_STATE_DIR)).filter(path => path.includes('/outbox/')).length, 0);
});

test('a new offline chat inherits the owner memory revision after a global manual edit', async t => {
  const f = await fixture(t);
  await run(['hook', 'UserPromptSubmit'], { session_id: 'chat-a', turn_id: 'first', prompt: 'Use British English' }, f.env);
  await run(['command', '--session-id', 'shared-memory', '--request-id', 'edit-1'], 'Forget the English preference', f.env);
  f.control.failAll = true;
  await run(['hook', 'UserPromptSubmit'], { session_id: 'chat-b', turn_id: 'first', prompt: 'Continue' }, f.env);
  const path = (await files(f.env.MEMORY_LENS_STATE_DIR)).find(path => path.includes('/outbox/'));
  assert.equal(JSON.parse(await readFile(path, 'utf8')).event.base_revision, 4);
});

test('MCP retries without a supplied causal revision do not replace the original event payload', async t => {
  const f = await fixture(t); f.control.idempotency = true;
  const requests = [
    { jsonrpc: '2.0', id: 1, method: 'initialize', params: {} },
    { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'memory_ingest_turn', arguments: { turn_id: 'same-native-turn', user_message: 'Use British English' } } },
    { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'memory_ingest_turn', arguments: { turn_id: 'same-native-turn', user_message: 'Use British English' } } }
  ];
  const result = await run(['mcp'], requests.map(request => JSON.stringify(request)).join('\n') + '\n', { ...f.env, MEMORY_LENS_SOURCE: 'editor', MEMORY_LENS_EXTERNAL_SESSION_ID: 'editor-chat' });
  assert.equal(result.code, 0);
  const replies = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(replies[1].result.structuredContent.status, 'applied');
  assert.equal(replies[2].result.structuredContent.status, 'duplicate');
  assert.equal(f.control.revision, 4, 'exact retry cannot trigger another extraction/revision');
});
