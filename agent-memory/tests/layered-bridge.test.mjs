import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { configuration } from '../bridge/api.mjs';
import { MemoryBridge } from '../bridge/client.mjs';
import { codexHook } from '../bridge/hooks.mjs';

const sid = 'synthetic-chat';
const memory = { status: 'ready', session_id: sid, revision: 2, node_ids: [sid + ':refund'], text: '"Refund window: 14 days"' };
const code = { status: 'ready', repo_id: 'synthetic-repo', graph: { nodes: [{ id: 'billing.refund', name: 'refund' }], edges: [] }, text: '"billing.refund in billing.py"' };
function layered(codeGraph = code) {
  return { session_id: sid, revision: 2, node_ids: memory.node_ids, text: memory.text + '\n' + codeGraph.text, memory, code_graph: codeGraph, stages: [{ stage: 'memory', order: 1, status: 'ready', revision: 2 }, { stage: 'code_graph', order: 2, status: codeGraph.status }, { stage: 'agent_context', order: 3, status: codeGraph.status === 'ready' ? 'ready' : 'partial' }] };
}
async function fixture(t, reply = layered(), enabled = true, advertised = false) {
  const requests = [], dir = await mkdtemp(join(tmpdir(), 'memory-lens-layered-'));
  const server = createServer(async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); requests.push({ ...body, authenticated: !!req.headers.authorization });
    const result = body.action === 'health' ? { api_version: 1, capabilities: advertised ? ['layered_context'] : [] } : body.action === 'ensure_session' ? { session_id: sid, revision: 2 } : body.action === 'ingest_turn' ? { session_id: sid, revision: 2, status: 'noop', changed_ids: [] } : body.action === 'layered_context' ? reply : { session_id: sid, revision: 2, node_ids: memory.node_ids, text: memory.text };
    res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(result));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise(resolve => server.close(resolve)); await rm(dir, { recursive: true, force: true }); });
  const env = { MEMORY_LENS_API_URL: `http://127.0.0.1:${server.address().port}/memory-api`, MEMORY_LENS_TOKEN: 'synthetic-test-token-only', MEMORY_LENS_STATE_DIR: dir, ...(enabled ? { MEMORY_LENS_LAYERED_CONTEXT: '1' } : {}) };
  return { bridge: new MemoryBridge(configuration(env)), requests, env };
}

test('opted-in Codex prompt retrieves memory then code and injects separate quoted envelopes', async t => {
  const { bridge, requests } = await fixture(t);
  const result = await codexHook(bridge, 'UserPromptSubmit', { session_id: 'native-chat', turn_id: 'native-turn', prompt: 'Continue' });
  assert.equal(requests.at(-1).action, 'layered_context');
  const context = result.hookSpecificOutput.additionalContext;
  assert.match(context, /14 days/); assert.match(context, /billing\.refund/);
  assert.match(context, /"memory":/); assert.match(context, /"code_graph":/);
  const record = await bridge.state.read('prompts', JSON.stringify(['native-chat', 'native-turn']));
  assert.equal(record.base_revision, 2);
});

test('missing existing connector injects honest memory-only partial context', async t => {
  const { bridge } = await fixture(t, layered({ status: 'not_configured', repo_id: null, graph: { nodes: [], edges: [] }, text: '' }));
  const result = await codexHook(bridge, 'SessionStart', { session_id: 'native-chat' });
  const context = result.hookSpecificOutput.additionalContext;
  assert.match(context, /14 days/); assert.match(context, /not_configured/); assert.doesNotMatch(context, /billing\.refund/);
});

test('layered responses with foreign memory session or mismatched revision are rejected', async t => {
  for (const mismatch of [{ session_id: 'other-chat' }, { revision: 99 }]) {
    const reply = layered(); reply.memory = { ...memory, ...mismatch };
    const { bridge } = await fixture(t, reply);
    await assert.rejects(bridge.context({ source: 'codex', external_session_id: 'native-chat' }, 'Continue', 10), /invalid|different|mismatch/i);
  }
});

test('memory-only bridge remains compatible and layered opt-in is explicitly validated', async t => {
  const { bridge, requests, env } = await fixture(t, layered(), false);
  const result = await codexHook(bridge, 'SessionStart', { session_id: 'native-chat' });
  assert.equal(requests.at(-1).action, 'context');
  assert.match(result.hookSpecificOutput.additionalContext, /14 days/);
  assert.doesNotMatch(result.hookSpecificOutput.additionalContext, /"code_graph":/);
  assert.throws(() => configuration({ ...env, MEMORY_LENS_LAYERED_CONTEXT: 'sometimes' }), /LAYERED_CONTEXT/);
});

test('advertised two-layer context is automatic and health is cached without transmitting a bearer token', async t => {
  const { bridge, requests } = await fixture(t, layered(), false, true);
  const selection = { source: 'codex', external_session_id: 'native-chat' };
  await bridge.context(selection, 'Continue', 10); await bridge.context(selection, 'Continue', 10);
  const health = requests.filter(request => request.action === 'health');
  assert.equal(health.length, 1); assert.equal(health[0].authenticated, false);
  assert.equal(requests.filter(request => request.action === 'layered_context').length, 2);
  assert.equal(requests.filter(request => request.action === 'context').length, 0);
});

test('a failed advertised code/memory pipeline never silently falls back to the old context endpoint', async t => {
  const { bridge, requests } = await fixture(t, { error: { code: 'upstream_unavailable' } }, false, true);
  await assert.rejects(bridge.context({ source: 'codex', external_session_id: 'native-chat' }, 'Continue', 10), /upstream_unavailable/);
  assert.equal(requests.filter(request => request.action === 'context').length, 0);
});

test('valid long canonical code identities remain intact in the agent envelope', async t => {
  const codeId = 'qualified.' + 'x'.repeat(600);
  const { bridge } = await fixture(t, layered({ ...code, graph: { nodes: [{ id: codeId }], edges: [] } }));
  const result = await codexHook(bridge, 'SessionStart', { session_id: 'native-chat' });
  assert.ok(result.hookSpecificOutput.additionalContext.includes(codeId));
});
