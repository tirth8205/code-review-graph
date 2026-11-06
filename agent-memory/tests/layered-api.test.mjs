import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startMemoryServer } from '../backend/server.mjs';

const TOKEN = 'synthetic-layered-api-credential';
const op = (kind, fields) => ({ op: kind, client_id: null, node_id: null, type: null, content: null, source_node_id: null, target_node_id: null, label: null, evidence: null, evidence_origin: null, ...fields });
const ready = () => ({ status: 'ready', repo_id: 'configured-repo', graph: { nodes: [{ id: 'code:refund-validator', type: 'function', name: 'validateRefund' }], edges: [] }, text: 'validateRefund checks the configured refund window.' });

async function fixture(t, connector = null) {
  const dir = await mkdtemp(join(tmpdir(), 'memory-lens-layered-'));
  const responses = [];
  const events = [];
  const codeGraph = connector ?? { configured: false, repo_id: null, retrieve: async () => { events.push('code'); return { status: 'not_configured', repo_id: null, graph: { nodes: [], edges: [] }, text: '', reason: 'Configure the existing code graph connector.' }; }, close: async () => {} };
  const app = await startMemoryServer({ token: TOKEN, apiKey: 'synthetic-provider', dbPath: join(dir, 'memory.sqlite'), codeGraph, fetchImpl: async (_url, init) => {
    events.push('model');
    const request = JSON.parse(init.body);
    assert.equal(request.model, 'gpt-6-astra');
    return new Response(JSON.stringify({ status: 'completed', output: [{ type: 'message', content: [{ type: 'output_text', text: JSON.stringify({ operations: responses.shift() ?? [], message: 'Synthetic response.' }) }] }] }), { status: 200 });
  } });
  t.after(async () => { await app.close(); await rm(dir, { recursive: true, force: true }); });
  const api = async (body, auth = true) => {
    const response = await fetch(app.url, { method: 'POST', headers: { 'content-type': 'application/json', ...(auth ? { authorization: `Bearer ${TOKEN}` } : {}) }, body: JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  };
  const session = async external => (await api({ action: 'ensure_session', source: 'synthetic-tool', external_session_id: external })).body.session_id;
  return { api, session, responses, events, app };
}

test('memory is read before code retrieval and the latest correction is handed to the separate graph', async t => {
  const inputs = [];
  const connector = { configured: true, repo_id: 'configured-repo', retrieve: async input => { inputs.push(input); return ready(); }, close: async () => {} };
  const f = await fixture(t, connector);
  const id = await f.session('billing-chat');
  f.responses.push([op('add', { client_id: 'refund', type: 'constraint', content: 'Refund window: 7 days', evidence: 'Refunds last seven days', evidence_origin: 'user' })]);
  await f.api({ action: 'ingest_turn', session_id: id, turn_id: 'first', user_message: 'Refunds last seven days' });
  const graph = (await f.api({ action: 'graph', session_id: id })).body;
  f.responses.push([op('update', { node_id: graph.nodes[0].id, content: 'Refund window: 14 days', evidence: 'Change refunds to fourteen days', evidence_origin: 'command' })]);
  await f.api({ action: 'command', session_id: id, request_id: 'correction', command: 'Change refunds to fourteen days' });
  const retrieval = await f.api({ action: 'layered_context', session_id: id, query: 'Continue' });
  assert.equal(retrieval.status, 200);
  assert.equal(inputs.length, 1);
  assert.equal(inputs[0].sessionId, id);
  assert.equal(inputs[0].memoryRevision, 2);
  assert.match(inputs[0].memoryText, /14 days/);
  assert.doesNotMatch(inputs[0].memoryText, /7 days/);
  assert.equal(retrieval.body.memory.text, inputs[0].memoryText);
  assert.equal(retrieval.body.code_graph.status, 'ready');
  assert.deepEqual(retrieval.body.stages.map(stage => stage.stage), ['memory', 'code_graph', 'agent_context']);
  assert.deepEqual(retrieval.body.stages.map(stage => stage.order), [1, 2, 3]);
  assert.match(retrieval.body.text, /14 days/);
  assert.match(retrieval.body.text, /validateRefund/);
  const afterward = (await f.api({ action: 'graph', session_id: id })).body;
  assert.equal(afterward.nodes.length, 1);
  assert.equal(afterward.revision, 2);
  assert.ok(!afterward.nodes.some(node => node.id.startsWith('code:')));
  assert.equal(afterward.latest_retrieval.code_graph.graph.nodes[0].id, 'code:refund-validator');
});

test('missing or failed second-layer connectors preserve memory and honestly report partial readiness', async t => {
  const f = await fixture(t);
  const id = await f.session('empty');
  const missing = await f.api({ action: 'layered_context', session_id: id });
  assert.equal(missing.status, 200);
  assert.equal(missing.body.code_graph.status, 'not_configured');
  assert.equal(missing.body.stages.at(-1).status, 'partial');
  assert.deepEqual(missing.body.code_graph.graph.nodes, []);
  const calls = [];
  const down = await fixture(t, { configured: true, repo_id: 'configured-repo', retrieve: async input => { calls.push(input); throw new Error('private /home/example checkout and provider stderr'); }, close: async () => {} });
  const sid = await down.session('down');
  const result = await down.api({ action: 'layered_context', session_id: sid });
  assert.equal(result.body.code_graph.status, 'unavailable');
  assert.ok(!JSON.stringify(result.body).includes('/home/example'));
  assert.equal(calls.length, 1);
});

test('auth, missing conversations, input caps and repository overrides stop before code retrieval', async t => {
  let calls = 0;
  const f = await fixture(t, { configured: true, repo_id: 'bound-repo', retrieve: async () => { calls++; return ready(); }, close: async () => {} });
  const id = await f.session('scoped');
  assert.equal((await f.api({ action: 'layered_context', session_id: id }, false)).status, 401);
  assert.equal((await f.api({ action: 'layered_context', session_id: 'unknown' })).status, 404);
  assert.equal((await f.api({ action: 'layered_context', session_id: id, query: 'q'.repeat(501) })).status, 400);
  assert.equal((await f.api({ action: 'layered_context', session_id: id, limit: 21 })).status, 400);
  assert.equal((await f.api({ action: 'layered_context', session_id: id, repo: '/attacker/path' })).status, 400);
  assert.equal((await f.api({ action: 'layered_context', session_id: id, command_json: ['attacker'] })).status, 400);
  assert.equal(calls, 0);
  const health = (await f.api({ action: 'health' }, false)).body;
  assert.ok(health.capabilities.includes('layered_context'));
  assert.equal(health.existing_code_graph.configured, true);
  assert.ok(!JSON.stringify(health).includes('bound-repo'));
});

test('native chats share one retrieval trace and shared memories invalidate it across tools', async t => {
  const inputs = [];
  const f = await fixture(t, { configured: true, repo_id: 'configured-repo', retrieve: async input => { inputs.push(input); return ready(); }, close: async () => {} });
  const first = await f.session('first');
  const second = await f.session('second');
  await f.api({ action: 'layered_context', session_id: first });
  assert.ok((await f.api({ action: 'graph', session_id: first })).body.latest_retrieval);
  assert.equal(second, first);
  assert.ok((await f.api({ action: 'graph', session_id: second })).body.latest_retrieval);
  f.responses.push([op('add', { client_id: 'fact', type: 'fact', content: 'Maya owns billing', evidence: 'Maya owns billing', evidence_origin: 'command' })]);
  await f.api({ action: 'command', session_id: first, request_id: 'new-memory', command: 'Remember Maya owns billing' });
  assert.equal((await f.api({ action: 'graph', session_id: first })).body.latest_retrieval, undefined);
  await f.api({ action: 'layered_context', session_id: second });
  assert.match(inputs[1].memoryText, /Maya/);
  assert.equal(inputs[1].sessionId, second);
});

test('a correction during code retrieval prevents stale context and stale traces from being returned', async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { configured: true, repo_id: 'configured-repo', retrieve: async () => { await gate; return ready(); }, close: async () => {} });
  const id = await f.session('changing');
  const pending = f.api({ action: 'layered_context', session_id: id });
  await new Promise(resolve => setTimeout(resolve, 20));
  f.responses.push([op('add', { client_id: 'fact', type: 'fact', content: 'Use Stripe', evidence: 'Use Stripe', evidence_origin: 'command' })]);
  await f.api({ action: 'command', session_id: id, request_id: 'mutation', command: 'Use Stripe' });
  release();
  assert.equal((await pending).status, 409);
  assert.equal((await f.api({ action: 'graph', session_id: id })).body.latest_retrieval, undefined);
});

test('combined untrusted text is bounded and closing the API closes the configured connector', async t => {
  let closed = 0;
  const f = await fixture(t, { configured: true, repo_id: 'configured-repo', retrieve: async () => ({ ...ready(), text: '"\n'.repeat(12000) }), close: async () => { closed++; } });
  const id = await f.session('bounds');
  const result = (await f.api({ action: 'layered_context', session_id: id })).body;
  assert.ok(result.text.length <= 24000);
  assert.ok(result.code_graph.text.length <= 12000);
  assert.match(result.text, /untrusted reference/);
  await f.app.close();
  assert.equal(closed, 1);
});

test('an older failed retrieval cannot erase a newer valid trace for the same conversation', async t => {
  let release;
  let markStarted;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { markStarted = resolve; });
  let count = 0;
  const f = await fixture(t, { configured: true, repo_id: 'configured-repo', retrieve: async () => {
    count++;
    if (count === 1) { markStarted(); await gate; return ready(); }
    return { ...ready(), graph: { nodes: [{ id: 'code:fresh', name: 'updatedPolicy' }], edges: [] }, text: 'Fresh code context.' };
  }, close: async () => {} });
  const id = await f.session('trace-race');
  f.responses.push([op('add', { client_id: 'language', type: 'constraint', content: 'Use British English', evidence: 'Use British English', evidence_origin: 'user' })]);
  await f.api({ action: 'ingest_turn', session_id: id, turn_id: 'initial', user_message: 'Use British English' });
  const language = (await f.api({ action: 'graph', session_id: id })).body.nodes[0];
  const old = f.api({ action: 'layered_context', session_id: id, query: 'Continue' });
  await started;
  f.responses.push([op('update', { node_id: language.id, content: 'Use American English', evidence: 'Use American English instead', evidence_origin: 'command' })]);
  await f.api({ action: 'command', session_id: id, request_id: 'language-correction', command: 'Use American English instead' });
  const fresh = await f.api({ action: 'layered_context', session_id: id, query: 'Continue' });
  assert.equal(fresh.body.revision, 2);
  assert.equal((await f.api({ action: 'graph', session_id: id })).body.latest_retrieval.revision, 2);
  release();
  assert.equal((await old).status, 409);
  const after = (await f.api({ action: 'graph', session_id: id })).body;
  assert.equal(after.latest_retrieval.revision, 2);
  assert.equal(after.latest_retrieval.code_graph.graph.nodes[0].id, 'code:fresh');
  assert.match(after.latest_retrieval.memory.text, /American English/);
});
