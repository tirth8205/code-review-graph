import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { startMemoryServer } from '../backend/server.mjs';

const TOKEN = 'synthetic-shared-workspace-token';
const op = (kind, fields) => ({ op: kind, client_id: null, node_id: null, type: null, content: null, source_node_id: null, target_node_id: null, label: null, evidence: null, evidence_origin: null, ...fields });
const fact = (text, extra = {}) => op('add', { client_id: 'new', type: 'constraint', content: text, evidence: text, evidence_origin: 'user', ...extra });

function seedLegacy(path, rows) {
  const db = new DatabaseSync(path);
  db.exec(`CREATE TABLE sessions(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,source TEXT NOT NULL,external_session_id TEXT NOT NULL,title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,last_manual_revision INTEGER NOT NULL DEFAULT 0,graph_json TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(owner_id,source,external_session_id));
    CREATE TABLE events(owner_id TEXT NOT NULL,session_id TEXT NOT NULL REFERENCES sessions(id),kind TEXT NOT NULL,event_id TEXT NOT NULL,content_hash TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,reservation TEXT NOT NULL,base_revision INTEGER NOT NULL,lease_until INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(owner_id,session_id,kind,event_id));`);
  const snapshots = new Map();
  for (const row of rows) {
    const snapshot = JSON.stringify({ nodes: row.nodes, edges: row.edges ?? [] });
    snapshots.set(row.id, snapshot);
    db.prepare('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)').run(row.id, 'local', row.id, row.id, row.id, 1, 0, snapshot, row.updated_at);
    for (const node of row.nodes) db.prepare('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)').run('local', row.id, node.source_kind === 'command' ? 'command' : 'turn', node.source_turn_id, 'legacy-hash', JSON.stringify({ user_message: node.content, assistant_message: '' }), 'applied', JSON.stringify({ session_id: row.id, revision: 1, status: 'applied', changed_ids: [node.id] }), 'reservation', 0, 0, row.event_time ?? row.updated_at);
  }
  db.close();
  return snapshots;
}

const legacyNode = (id, content, fields = {}) => ({ id, type: 'constraint', content, status: 'active', source_kind: 'user', source_turn_id: id, evidence: content, pinned: false, history: [], ...fields });

async function workspace(t, seed) {
  const dir = await mkdtemp(join(tmpdir(), 'memory-lens-shared-'));
  const dbPath = join(dir, 'memory.sqlite');
  if (seed) seed(dbPath);
  let calls = 0;
  const responses = [];
  const app = await startMemoryServer({ token: TOKEN, dbPath, apiKey: 'synthetic-model', fetchImpl: async () => {
    calls++;
    return new Response(JSON.stringify({ status: 'completed', output: [{ type: 'message', content: [{ type: 'output_text', text: JSON.stringify({ operations: responses.shift() ?? [], message: 'Synthetic result.' }) }] }] }));
  } });
  t.after(async () => { await app.close(); await rm(dir, { recursive: true, force: true }); });
  const api = async body => { const res = await fetch(app.url, { method: 'POST', headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' }, body: JSON.stringify(body) }); return { status: res.status, body: await res.json() }; };
  const ensure = async (source, external) => (await api({ action: 'ensure_session', source, external_session_id: external })).body.session_id;
  return { api, ensure, responses, calls: () => calls, dbPath };
}

test('all origins grow one graph while same native turn IDs deduplicate only within their source', async t => {
  const f = await workspace(t);
  const id = await f.ensure('codex-cli', 'chat-one');
  assert.equal(await f.ensure('ide', 'chat-two'), id);
  f.responses.push([fact('Use British English')]);
  const first = { action: 'ingest_turn', session_id: id, turn_id: 'native-one', source: 'codex-cli', external_session_id: 'chat-one', native_turn_id: 'native-one', user_message: 'Use British English' };
  assert.equal((await f.api(first)).body.status, 'applied');
  f.responses.push([fact('Use em dashes')]);
  const second = { ...first, source: 'ide', external_session_id: 'chat-two', user_message: 'Use em dashes' };
  assert.equal((await f.api(second)).body.status, 'applied');
  assert.equal((await f.api(first)).body.status, 'duplicate');
  assert.equal((await f.api(second)).body.status, 'duplicate');
  assert.equal(f.calls(), 2);
  assert.equal((await f.api({ ...first, user_message: 'Use American English' })).status, 409);
  const graph = (await f.api({ action: 'graph', session_id: id })).body;
  assert.equal(graph.nodes.length, 2);
  assert.equal(graph.turns.length, 2);
  assert.deepEqual(graph.nodes.map(node => node.source).sort(), ['codex-cli', 'ide']);
  assert.deepEqual(graph.nodes.map(node => node.external_session_id).sort(), ['chat-one', 'chat-two']);
  assert.ok(graph.nodes.every(node => node.native_turn_id === 'native-one'));
  assert.deepEqual(graph.turns.map(turn => turn.source).sort(), ['codex-cli', 'ide']);
  assert.equal((await f.api({ action: 'list_sessions' })).body.sessions.length, 1);
  const context = (await f.api({ action: 'context', session_id: await f.ensure('another-tool', 'chat-three') })).body;
  assert.match(context.text, /British English/);
  assert.match(context.text, /em dashes/);
});

test('forget applies across every tool and source history survives a shared correction', async t => {
  const f = await workspace(t);
  const id = await f.ensure('codex', 'first');
  f.responses.push([fact('Use British English'), fact('Use em dashes', { client_id: 'punctuation' })]);
  await f.api({ action: 'ingest_turn', session_id: id, turn_id: 'capture-one', native_turn_id: 'original-one', source: 'codex', external_session_id: 'first', user_message: 'Use British English. Use em dashes.' });
  const language = (await f.api({ action: 'graph', session_id: id })).body.nodes.find(node => node.content === 'Use British English');
  f.responses.push([op('update', { node_id: language.id, content: 'Use American English', evidence: 'Use American English instead', evidence_origin: 'user' })]);
  await f.api({ action: 'ingest_turn', session_id: await f.ensure('ide', 'second'), turn_id: 'capture-one', source: 'ide', external_session_id: 'second', native_turn_id: 'original-one', user_message: 'Use American English instead' });
  const updated = (await f.api({ action: 'graph', session_id: id })).body.nodes.find(node => node.id === language.id);
  assert.equal(updated.source, 'ide');
  assert.equal(updated.history[0].source, 'codex');
  assert.equal(updated.history[0].external_session_id, 'first');
  assert.equal(updated.history[0].native_turn_id, 'original-one');
  f.responses.push([op('forget', { node_id: language.id, evidence: 'Forget the language preference', evidence_origin: 'command' })]);
  await f.api({ action: 'command', session_id: id, request_id: 'forget', command: 'Forget the language preference' });
  const context = (await f.api({ action: 'context', session_id: await f.ensure('third-tool', 'third') })).body;
  assert.doesNotMatch(context.text, /English/);
  assert.match(context.text, /em dashes/);
});

test('separate local installations have independent graphs and reject foreign workspace IDs', async t => {
  const first = await workspace(t), second = await workspace(t);
  const a = await first.ensure('same-tool', 'same-chat'), b = await second.ensure('same-tool', 'same-chat');
  assert.notEqual(a, b);
  first.responses.push([fact('Use British English')]);
  await first.api({ action: 'ingest_turn', session_id: a, turn_id: 'one', user_message: 'Use British English' });
  assert.equal((await second.api({ action: 'graph', session_id: b })).body.nodes.length, 0);
  assert.equal((await second.api({ action: 'context', session_id: a })).status, 404);
  assert.equal((await second.api({ action: 'ingest_turn', session_id: a, turn_id: 'one', source: 'same-tool', external_session_id: 'same-chat', user_message: 'Use British English' })).status, 404);
  assert.equal(second.calls(), 0);
});

test('legacy per-chat graphs consolidate additively while their original data remains intact', async t => {
  const originals = new Map();
  const f = await workspace(t, path => {
    const db = new DatabaseSync(path);
    db.exec(`CREATE TABLE sessions(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,source TEXT NOT NULL,external_session_id TEXT NOT NULL,title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,last_manual_revision INTEGER NOT NULL DEFAULT 0,graph_json TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(owner_id,source,external_session_id));
      CREATE TABLE events(owner_id TEXT NOT NULL,session_id TEXT NOT NULL REFERENCES sessions(id),kind TEXT NOT NULL,event_id TEXT NOT NULL,content_hash TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,reservation TEXT NOT NULL,base_revision INTEGER NOT NULL,lease_until INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(owner_id,session_id,kind,event_id));`);
    for (const [id, source, external, content] of [['legacy-one', 'cli', 'first', 'Use British English'], ['legacy-two', 'ide', 'second', 'Use em dashes']]) {
      const nodeId = id + ':old-node';
      const snapshot = JSON.stringify({ nodes: [{ id: nodeId, type: 'constraint', content, status: 'active', source_kind: 'user', source_turn_id: 'same-turn', evidence: content, pinned: false, history: [] }], edges: [] });
      originals.set(id, snapshot);
      db.prepare('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)').run(id, 'local', source, external, external, 1, 0, snapshot, '2026-10-06T17:00:00.000Z');
      db.prepare('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)').run('local', id, 'turn', 'same-turn', 'legacy-hash', JSON.stringify({ user_message: content, assistant_message: '' }), 'applied', JSON.stringify({ session_id: id, revision: 1, status: 'applied', changed_ids: [nodeId] }), 'reservation', 0, 0, '2026-10-06T17:00:00.000Z');
    }
    db.close();
  });
  const id = await f.ensure('cli', 'first');
  assert.equal(await f.ensure('ide', 'second'), id);
  const graph = (await f.api({ action: 'graph', session_id: id })).body;
  assert.equal(graph.nodes.length, 2);
  assert.equal(graph.turns.length, 2);
  assert.ok(graph.nodes.every(node => node.id.startsWith(id + ':')));
  assert.deepEqual(graph.turns.map(turn => turn.source).sort(), ['cli', 'ide']);
  assert.equal((await f.api({ action: 'graph', session_id: 'legacy-one' })).status, 404);
  const db = new DatabaseSync(f.dbPath, { readOnly: true });
  for (const [legacyId, original] of originals) assert.equal(db.prepare('SELECT graph_json FROM sessions WHERE id=?').get(legacyId).graph_json, original);
  assert.equal(db.prepare('SELECT count(*) AS n FROM events WHERE session_id IN (?,?)').get('legacy-one', 'legacy-two').n, 2);
  db.close();
});

test('legacy correction replaces its historical predecessor even when an older chat was later touched by a noop', async t => {
  let originals;
  const f = await workspace(t, path => { originals = seedLegacy(path, [
    { id: 'old', updated_at: '2026-10-06T20:00:00.000Z', event_time: '2026-10-06T17:00:00.000Z', nodes: [legacyNode('old:language', 'Use British English'), legacyNode('old:person', 'User preferences')], edges: [{ id: 'old:edge', source: 'old:person', target: 'old:language', label: 'has preference' }] },
    { id: 'corrected', updated_at: '2026-10-06T18:00:00.000Z', nodes: [legacyNode('corrected:language', 'Use American English', { source_kind: 'command', history: [{ content: 'Use British English', status: 'superseded', source_turn_id: 'before-correction', source_kind: 'user', evidence: 'Use British English', changed_at: '2026-10-06T18:00:00.000Z' }] })] }
  ]); });
  const id = await f.ensure('new-tool', 'new-chat');
  const graph = (await f.api({ action: 'graph', session_id: id })).body;
  assert.deepEqual(graph.nodes.filter(node => node.status === 'active').map(node => node.content).sort(), ['Use American English', 'User preferences']);
  const language = graph.nodes.find(node => node.content === 'Use American English');
  assert.ok(language.history.some(entry => entry.content === 'Use British English' && entry.source === 'old'));
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.edges[0].target, language.id);
  const context = (await f.api({ action: 'context', session_id: id })).body.text;
  assert.match(context, /American English/);
  assert.doesNotMatch(context, /British English/);
  const db = new DatabaseSync(f.dbPath, { readOnly: true });
  for (const [legacyId, original] of originals) assert.equal(db.prepare('SELECT graph_json FROM sessions WHERE id=?').get(legacyId).graph_json, original);
  db.close();
});

test('latest explicit legacy restoration survives another chat being touched later', async t => {
  const f = await workspace(t, path => seedLegacy(path, [
    { id: 'corrected', updated_at: '2026-10-06T21:00:00.000Z', event_time: '2026-10-06T18:00:00.000Z', nodes: [legacyNode('corrected:language', 'Use American English', { source_kind: 'command', history: [{ content: 'Use British English', status: 'superseded', changed_at: '2026-10-06T18:00:00.000Z' }] })] },
    { id: 'restored', updated_at: '2026-10-06T19:00:00.000Z', nodes: [legacyNode('restored:language', 'Use British English', { source_kind: 'command', history: [{ content: 'Use American English', status: 'superseded', changed_at: '2026-10-06T19:00:00.000Z' }] })] }
  ]));
  const id = await f.ensure('any', 'chat');
  const graph = (await f.api({ action: 'graph', session_id: id })).body;
  assert.deepEqual(graph.nodes.filter(node => node.status === 'active').map(node => node.content), ['Use British English']);
  assert.ok(graph.nodes[0].history.some(entry => entry.content === 'Use American English'));
});

test('an oversized legacy graph remains editable without allowing additional growth', async t => {
  const f = await workspace(t, path => seedLegacy(path, ['one', 'two'].map(id => ({ id, updated_at: '2026-10-06T17:00:00.000Z', nodes: Array.from({ length: 501 }, (_, index) => legacyNode(id + ':' + index, 'Preference ' + id + ' ' + index)) }))));
  const id = await f.ensure('any', 'chat');
  const graph = (await f.api({ action: 'graph', session_id: id })).body;
  assert.equal(graph.nodes.length, 1002);
  f.responses.push([op('forget', { node_id: graph.nodes[0].id, evidence: 'Forget the first preference', evidence_origin: 'command' })]);
  assert.equal((await f.api({ action: 'command', session_id: id, request_id: 'forget-large', command: 'Forget the first preference' })).body.status, 'applied');
  f.responses.push([fact('A new preference', { evidence_origin: 'command' })]);
  assert.equal((await f.api({ action: 'command', session_id: id, request_id: 'grow-large', command: 'A new preference' })).status, 503);
});
