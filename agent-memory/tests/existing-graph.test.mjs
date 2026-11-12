import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ExistingCodeGraph } from '../bridge/existing-graph.mjs';

const node = (name, extra = {}) => ({ qualified_name: '/bound/src/billing.py::' + name, name, kind: 'Function', file_path: '/bound/src/billing.py', line_start: 10, line_end: 20, ...extra });
const firstNode = node('refund');
const callerNode = node('checkout');
const edge = { id: 17, source: callerNode.qualified_name, target: firstNode.qualified_name, kind: 'CALLS', file_path: '/bound/src/billing.py', line: 15 };

async function fixture(t, scenario = {}) {
  const root = await mkdtemp(join(tmpdir(), 'memory-lens-existing-graph-'));
  const directory = join(root, "operator repo with ' space");
  await mkdir(directory);
  const repo = await realpath(directory);
  const record = join(root, 'requests.jsonl');
  const scenarioPath = join(root, 'scenario.json');
  const executable = join(root, 'mcp-fixture.mjs');
  await writeFile(scenarioPath, JSON.stringify({ results: [firstNode], relationships: { results: [callerNode], edges: [edge] }, ...scenario }));
  await writeFile(executable, `
import { createInterface } from 'node:readline';
import { appendFileSync, readFileSync } from 'node:fs';
const scenario = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const log = process.argv[3];
const send = message => process.stdout.write(JSON.stringify(message) + '\\n');
appendFileSync(log, JSON.stringify({ startup: true, cwd: process.cwd(), pid: process.pid, inherited: { memoryToken: process.env.MEMORY_LENS_TOKEN ?? null, openaiKey: process.env.OPENAI_API_KEY ?? null, unrelatedSecret: process.env.OTHER_SERVICE_SECRET ?? null, graphHome: process.env.CRG_HOME ?? null, graphRoot: process.env.CRG_REPO_ROOT ?? null, path: process.env.PATH ?? null } }) + '\\n');
for await (const line of createInterface({ input: process.stdin })) {
  const message = JSON.parse(line);
  appendFileSync(log, JSON.stringify(message) + '\\n');
  if (!Object.hasOwn(message, 'id')) continue;
  if (scenario.stderr) process.stderr.write('synthetic-secret-DO-NOT-LOG\\n');
  if (scenario.mode === 'hang') continue;
  if (scenario.mode === 'exit') { process.exit(2); }
  if (scenario.mode === 'oversized') { process.stdout.write('x'.repeat(300000)); continue; }
  if (scenario.mode === 'malformed') { process.stdout.write('not JSON\\n'); continue; }
  if (message.method === 'initialize') {
    send({ jsonrpc: '2.0', id: message.id, result: { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'synthetic-code-graph', version: '1' } } }); continue;
  }
  if (message.method === 'tools/call') {
    if (scenario.relationshipHang && message.params.name === 'query_graph_tool') continue;
    let value;
    if (scenario.toolError || (scenario.relationshipError && message.params.name === 'query_graph_tool')) { send({ jsonrpc: '2.0', id: message.id, result: { isError: true, content: [{ type: 'text', text: 'synthetic-secret-DO-NOT-LOG' }] } }); continue; }
    if (message.params.name === 'get_minimal_context_tool') {
      if (scenario.unsupportedMinimal) { send({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: 'Unknown tool get_minimal_context_tool' } }); continue; }
      value = scenario.readiness ?? { status: 'ok', summary: 'Synthetic indexed code graph' };
    } else if (message.params.name === 'semantic_search_nodes_tool') {
      value = { status: scenario.status ?? 'ok', results: scenario.literalTaskOnly && message.params.arguments.query.startsWith('Task: ') ? [] : scenario.results, ...(scenario.confidence ? { confidence: scenario.confidence } : {}) };
      if (message.params.arguments.detail_level === 'minimal') value.results = scenario.results.slice(0, 5).map(({ name, kind, file_path, score }) => ({ name, kind, file_path, score }));
    } else if (message.params.name === 'query_graph_tool') value = { status: 'ok', ...scenario.relationships };
    else value = { status: 'error', message: 'Forbidden non-retrieval tool' };
    send({ jsonrpc: '2.0', id: message.id, result: scenario.textOnly ? { content: [{ type: 'text', text: JSON.stringify(value) }] } : { structuredContent: value, content: [{ type: 'text', text: JSON.stringify(value) }] } });
  }
}
`);
  const env = { ...process.env, MEMORY_LENS_CODE_GRAPH_REPO: repo, MEMORY_LENS_CODE_GRAPH_COMMAND_JSON: JSON.stringify([process.execPath, executable, scenarioPath, record]) };
  const client = new ExistingCodeGraph({ env, timeoutMs: 350 });
  t.after(async () => { await client.close(); await rm(root, { recursive: true, force: true }); });
  return { client, repo, env, root, record, async records() { try { return (await readFile(record, 'utf8')).trim().split('\n').filter(Boolean).map(line => JSON.parse(line)); } catch { return []; } } };
}

test('unconfigured code graph stays honest and never launches an arbitrary command', async () => {
  const client = new ExistingCodeGraph({ env: { MEMORY_LENS_CODE_GRAPH_COMMAND_JSON: '["/does/not/exist"]' } });
  const result = await client.retrieve({ query: 'refund' });
  assert.equal(client.configured, false);
  assert.deepEqual(result, { status: 'not_configured', repo_id: null, graph: { nodes: [], edges: [] }, text: '', reason: 'code_graph_not_configured' });
  await client.close();
});

test('an empty task cannot label an unverified configured connector ready', async t => {
  const f = await fixture(t);
  const client = new ExistingCodeGraph({ env: { ...f.env, MEMORY_LENS_CODE_GRAPH_COMMAND_JSON: '["/definitely-missing-synthetic-mcp"]' } });
  const result = await client.retrieve({ query: '', memoryText: '' });
  assert.equal(result.status, 'not_ready');
  assert.equal(result.reason, 'no_query');
  assert.deepEqual(result.graph, { nodes: [], edges: [] });
  await client.close();
});

test('existing MCP retrieval binds operator repo and returns canonical separate code graph after readiness lookup', async t => {
  const f = await fixture(t, { textOnly: true });
  const result = await f.client.retrieve({ query: 'Where are refunds handled?', memoryText: 'Refund window is 14 days', memoryRevision: 7, sessionId: 'chat-a', repo_root: '/model-selected-other-project' });
  assert.equal(result.status, 'ready');
  assert.equal(result.repo_id, f.client.repo_id);
  assert.deepEqual(result.graph.nodes.map(node => node.id), [firstNode.qualified_name, callerNode.qualified_name]);
  assert.deepEqual(result.graph.edges.map(edge => [edge.source, edge.target, edge.label]), [[callerNode.qualified_name, firstNode.qualified_name, 'CALLS']]);
  assert.match(result.text, /untrusted.*reference data/i);
  assert.match(result.text, /"memory_revision":7/);
  const messages = await f.records();
  assert.equal(messages[0].cwd, f.repo);
  assert.deepEqual(messages.slice(1, 3).map(message => message.method), ['initialize', 'notifications/initialized']);
  const calls = messages.filter(message => message.method === 'tools/call');
  assert.deepEqual(calls.map(call => [call.params.name, call.params.arguments.detail_level]), [['get_minimal_context_tool', undefined], ['semantic_search_nodes_tool', 'standard'], ['query_graph_tool', 'standard']]);
  assert.ok(calls.every(call => call.params.arguments.repo_root === f.repo));
  assert.match(calls[0].params.arguments.task, /Where are refunds handled/);
  assert.equal(calls[1].params.arguments.limit, 12);
  assert.equal(calls[1].params.arguments.include_source, false);
  assert.match(calls[1].params.arguments.query, /Where are refunds handled/);
  assert.match(calls[1].params.arguments.query, /Refund window is 14 days/);
  assert.equal(calls[2].params.arguments.target, firstNode.qualified_name);
  assert.equal(calls[2].params.arguments.resolution, 'direct');
  assert.equal(calls[2].params.arguments.max_results, 8);
  assert.doesNotMatch(JSON.stringify(messages), /model-selected-other-project|build_or_update_graph|embed_graph|watch_tool/);
  await f.client.retrieve({ query: 'What calls checkout?', memoryRevision: 8, sessionId: 'chat-b' });
  assert.equal((await f.records()).filter(message => message.startup).length, 1, 'reuse the initialized process');
});

test('canonical graph normalization deduplicates and caps nodes/edges without invented identities', async t => {
  const many = Array.from({ length: 32 }, (_, index) => node('function-' + index, { name: 'untrusted " text\n'.repeat(100) }));
  const f = await fixture(t, { results: [firstNode, firstNode, { name: 'no canonical ID', kind: 'Function' }, ...many], relationships: { results: many, edges: [...Array.from({ length: 100 }, (_, index) => ({ id: index, source: many[index % 16].qualified_name, target: firstNode.qualified_name, kind: 'CALLS', line: index + 1 })), { id: 'dangling', source: '/unknown', target: firstNode.qualified_name, kind: 'CALLS' }] } });
  const result = await f.client.retrieve({ query: 'refund', memoryText: 'x'.repeat(20000), memoryRevision: 3 });
  assert.equal(result.status, 'ready');
  assert.ok(result.graph.nodes.length <= 20);
  assert.equal(new Set(result.graph.nodes.map(node => node.id)).size, result.graph.nodes.length);
  assert.ok(result.graph.edges.length <= 50);
  const ids = new Set(result.graph.nodes.map(node => node.id));
  assert.ok(result.graph.edges.every(edge => ids.has(edge.source) && ids.has(edge.target)));
  assert.ok(result.text.length <= 12000);
  const quoted = JSON.parse(result.text.slice(result.text.indexOf('\n') + 1));
  assert.deepEqual(quoted.graph, result.graph);
  const query = (await f.records()).find(message => message.params?.name === 'semantic_search_nodes_tool').params.arguments.query;
  assert.ok(query.length <= 4500);
  assert.ok(result.graph.nodes.every(node => node.name.length <= 256));
});

test('keyword-only indexes retry the literal task without turning memory preferences into required code keywords', async t => {
  const f = await fixture(t, { literalTaskOnly: true });
  const result = await f.client.retrieve({ query: 'GraphStore', memoryText: 'Use British English. Maya owns billing.', memoryRevision: 9 });
  assert.equal(result.status, 'ready');
  assert.deepEqual(result.graph.nodes.map(n => n.id), [firstNode.qualified_name]);
  const calls = (await f.records()).filter(message => message.method === 'tools/call');
  assert.equal(calls.length, 3, 'keep the original three-call bound');
  assert.equal(calls[2].params.arguments.query, 'GraphStore');
  assert.ok(calls.every(call => !['build_or_update_graph', 'embed_graph'].includes(call.params.name)));
});

test('empty or stale index reports not_ready while a genuine zero match returns an empty ready graph', async t => {
  for (const [confidence, status] of [['graph is empty: nothing is indexed, so this 0 says nothing about the code; run `code-review-graph build`', 'not_ready'], ['graph is stale: built at an older commit than HEAD, so this 0 may be out of date', 'not_ready'], ["no indexed node matches 'refund'; search covers names, paths and signatures, not source text", 'ready']]) {
    const f = await fixture(t, { results: [], confidence });
    const result = await f.client.retrieve({ query: 'refund' });
    assert.equal(result.status, status, confidence);
    assert.deepEqual(result.graph, { nodes: [], edges: [] });
    assert.equal((await f.records()).filter(message => message.params?.name === 'query_graph_tool').length, 0);
  }
});

test('readiness refusal prevents search and an older MCP without readiness tool is explicit', async t => {
  const f = await fixture(t, { readiness: { status: 'not_ready', reason: 'missing_graph' } });
  const result = await f.client.retrieve({ query: 'refund' });
  assert.equal(result.status, 'not_ready');
  assert.equal(result.reason, 'missing_graph');
  assert.deepEqual((await f.records()).filter(message => message.method === 'tools/call').map(message => message.params.name), ['get_minimal_context_tool']);
  const legacy = await fixture(t, { unsupportedMinimal: true });
  const partial = await legacy.client.retrieve({ query: 'refund' });
  assert.equal(partial.status, 'unavailable');
  assert.equal(partial.reason, 'readiness_tool_unavailable');
  assert.equal((await legacy.records()).filter(message => message.params?.name === 'semantic_search_nodes_tool').length, 0);
});

test('a later not_ready search response cannot be presented as a healthy empty code graph', async t => {
  const f = await fixture(t, { results: [], status: 'not_ready' });
  const result = await f.client.retrieve({ query: 'refund' });
  assert.equal(result.status, 'not_ready');
  assert.equal(result.reason, 'code_graph_not_ready');
});

test('malformed readiness and pending engine results never claim successful code retrieval', async t => {
  for (const scenario of [{ readiness: {} }, { results: [], status: 'pending' }]) {
    const f = await fixture(t, scenario);
    assert.equal((await f.client.retrieve({ query: 'refund' })).status, 'unavailable');
  }
});

test('legacy compact-only nodes are not assigned invented qualified names or relationship targets', async t => {
  const f = await fixture(t, { results: [{ name: 'refund', kind: 'Function', file_path: 'billing.py' }] });
  const result = await f.client.retrieve({ query: 'refund' });
  assert.equal(result.status, 'not_ready');
  assert.equal(result.reason, 'canonical_nodes_unavailable');
  assert.deepEqual(result.graph, { nodes: [], edges: [] });
  assert.equal((await f.records()).filter(message => message.params?.name === 'query_graph_tool').length, 0);
});

test('hung oversized malformed and exiting MCP processes are terminated with sanitized unavailable results', async t => {
  for (const mode of ['hang', 'oversized', 'malformed', 'exit']) {
    const f = await fixture(t, { mode, stderr: true });
    const started = Date.now();
    const result = await f.client.retrieve({ query: 'refund' });
    assert.equal(result.status, 'unavailable', mode);
    assert.ok(Date.now() - started < 2000, mode + ' must be bounded');
    assert.deepEqual(result.graph, { nodes: [], edges: [] });
    assert.doesNotMatch(JSON.stringify(result), /synthetic-secret|operator repo/);
    await f.client.close();
    const pid = (await f.records())[0].pid;
    assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
  }
});

test('tool errors and invalid trusted configuration never expose raw engine diagnostics', async t => {
  const f = await fixture(t, { toolError: true, stderr: true });
  const result = await f.client.retrieve({ query: 'refund' });
  assert.equal(result.status, 'unavailable');
  assert.doesNotMatch(JSON.stringify(result), /synthetic-secret/);
  const client = new ExistingCodeGraph({ env: { MEMORY_LENS_CODE_GRAPH_REPO: f.repo, MEMORY_LENS_CODE_GRAPH_COMMAND_JSON: '{synthetic-private-command' } });
  const invalid = await client.retrieve({ query: 'refund' });
  assert.equal(invalid.status, 'unavailable');
  assert.equal(invalid.reason, 'invalid_code_graph_configuration');
  assert.doesNotMatch(JSON.stringify(invalid), /synthetic-private-command/);
  await client.close();
});

test('unavailable optional relationships retain real search nodes with an explicit limitation', async t => {
  const f = await fixture(t, { relationshipError: true });
  const result = await f.client.retrieve({ query: 'refund' });
  assert.equal(result.status, 'ready');
  assert.equal(result.reason, 'relationships_unavailable');
  assert.deepEqual(result.graph.nodes.map(node => node.id), [firstNode.qualified_name]);
  assert.deepEqual(result.graph.edges, []);
  assert.doesNotMatch(JSON.stringify(result), /synthetic-secret/);
});

test('repo identity is stable across clients and cannot be selected through retrieval arguments', async t => {
  const f = await fixture(t);
  const same = new ExistingCodeGraph({ env: f.env });
  assert.equal(same.repo_id, f.client.repo_id);
  await same.close();
  const other = await fixture(t);
  assert.notEqual(other.client.repo_id, f.client.repo_id);
  const result = await f.client.retrieve({ query: 'refund', repo_root: other.repo, repo_id: other.client.repo_id });
  assert.equal(result.repo_id, f.client.repo_id);
  assert.ok((await f.records()).filter(message => message.method === 'tools/call').every(message => message.params.arguments.repo_root === f.repo));
});

test('code graph subprocess receives runtime and graph configuration without memory backend secrets', async t => {
  const f = await fixture(t);
  const client = new ExistingCodeGraph({ env: { ...f.env, MEMORY_LENS_TOKEN: 'synthetic-memory-secret', OPENAI_API_KEY: 'synthetic-backend-provider-secret', OTHER_SERVICE_SECRET: 'synthetic-unrelated-secret', CRG_HOME: join(f.root, 'graph-home'), CRG_REPO_ROOT: '/wrong-inherited-root' } });
  t.after(() => client.close());
  assert.equal((await client.retrieve({ query: 'refund' })).status, 'ready');
  const inherited = (await f.records())[0].inherited;
  assert.equal(inherited.memoryToken, null);
  assert.equal(inherited.openaiKey, null);
  assert.equal(inherited.unrelatedSecret, null);
  assert.equal(inherited.graphHome, join(f.root, 'graph-home'));
  assert.equal(inherited.graphRoot, f.repo);
  assert.equal(inherited.path, process.env.PATH);
});

test('close cancels in-flight retrieval and never leaves its child process running', async t => {
  const f = await fixture(t, { mode: 'hang' });
  const pending = f.client.retrieve({ query: 'refund' });
  await new Promise(resolve => setTimeout(resolve, 60));
  await f.client.close();
  assert.equal((await pending).status, 'unavailable');
  const pid = (await f.records())[0].pid;
  assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
  assert.equal((await f.client.retrieve({ query: 'refund' })).reason, 'code_graph_closed');
});

test('close during optional relationship lookup cancels the complete retrieval', async t => {
  const f = await fixture(t, { relationshipHang: true });
  const pending = f.client.retrieve({ query: 'refund' });
  const deadline = Date.now() + 1000;
  while (!(await f.records()).some(message => message.params?.name === 'query_graph_tool')) {
    assert.ok(Date.now() < deadline, 'relationship lookup should start');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  await f.client.close();
  const result = await pending;
  assert.equal(result.status, 'unavailable');
  assert.equal(result.reason, 'code_graph_closed');
  const pid = (await f.records())[0].pid;
  assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
});

test('wrapper descendants cannot hold stdio open past the retrieval and close timeout', async t => {
  const f = await fixture(t);
  const marker = join(f.root, 'descendant-pid');
  const wrapper = join(f.root, 'wrapper.mjs');
  await writeFile(wrapper, `import { spawn } from 'node:child_process'; import { writeFileSync } from 'node:fs'; const descendant = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: ['ignore', 'inherit', 'inherit'] }); writeFileSync(process.argv[2], String(descendant.pid)); setInterval(() => {}, 1000);`);
  let descendantPid;
  t.after(() => { if (descendantPid) { try { process.kill(descendantPid, 'SIGKILL'); } catch {} } });
  const client = new ExistingCodeGraph({ env: { ...f.env, MEMORY_LENS_CODE_GRAPH_COMMAND_JSON: JSON.stringify([process.execPath, wrapper, marker]) }, timeoutMs: 100 });
  t.after(() => client.close());
  const started = Date.now();
  const result = await Promise.race([client.retrieve({ query: 'refund' }), new Promise(resolve => setTimeout(() => resolve({ status: 'still_pending' }), 800))]);
  descendantPid = Number(await readFile(marker, 'utf8'));
  assert.equal(result.status, 'unavailable');
  assert.ok(Date.now() - started < 800);
  await client.close();
  assert.throws(() => process.kill(descendantPid, 0), { code: 'ESRCH' });
});
