import { randomUUID } from 'node:crypto';
import { unlink } from 'node:fs/promises';
import { api, BridgeError, required, safeError } from './api.mjs';
import { PrivateState, digest } from './state.mjs';

const DURABLE = new Set(['applied', 'noop', 'duplicate']);
export class MemoryBridge {
  constructor(config) { this.config = config; this.state = new PrivateState(config); this.layeredCapability = null; this.capabilityCheckedAt = 0; }
  async ensure(source, external_session_id, title) {
    const body = { action: 'ensure_session', source: required(source, 'source', 100), external_session_id: required(external_session_id, 'external_session_id', 255) };
    if (title) body.title = required(title, 'title', 200);
    const result = await api(this.config, body); required(result.session_id, 'API session_id', 255);
    if (!Number.isInteger(result.revision) || result.revision < 0) throw new BridgeError('upstream_unavailable', 'Session revision is invalid');
    await this.cacheRevision({ source, external_session_id }, result.revision, result.session_id);
    return result.session_id;
  }
  async cacheRevision(selection, revision, session_id) {
    await this.state.write('sessions', JSON.stringify(selection), { revision, session_id });
    const shared = await this.state.read('sessions', 'owner-memory');
    await this.state.write('sessions', 'owner-memory', { session_id, revision: shared?.session_id === session_id ? Math.max(shared.revision, revision) : revision });
  }
  async cachedRevision(selection) {
    return (await this.state.read('sessions', 'owner-memory'))?.revision ?? (await this.state.read('sessions', JSON.stringify(selection)))?.revision ?? 0;
  }
  async resolve(selection) {
    if (selection.source && selection.external_session_id) return this.ensure(selection.source, selection.external_session_id, selection.title);
    if (selection.session_id) return required(selection.session_id, 'session_id', 255);
    return this.ensure(selection.source, selection.external_session_id, selection.title);
  }
  normalizeEvent(input) {
    const capture_id = required(input.capture_id ?? input.turn_id, 'turn_id', 255);
    const session_id = input.session_id ? required(input.session_id, 'session_id', 255) : undefined;
    const source = required(input.source ?? (session_id ? 'manual-cli' : undefined), 'source', 100);
    const external_session_id = required(input.external_session_id ?? (session_id ? 'selected-memory:' + digest(session_id) : undefined), 'external_session_id', 255);
    const event = { source, external_session_id, capture_id,
      turn_id: 'memory-turn:' + digest(JSON.stringify([source, external_session_id, capture_id])),
      native_turn_id: required(input.native_turn_id ?? capture_id, 'native_turn_id', 255),
      user_message: required(input.user_message, 'user_message', 20000) };
    if (session_id) event.session_id = session_id;
    if (input.title) event.title = required(input.title, 'title', 200);
    if (input.assistant_message !== undefined && input.assistant_message !== null) {
      if (typeof input.assistant_message !== 'string' || input.assistant_message.length > 20000 || input.assistant_message.includes('\u0000')) throw new BridgeError('invalid_input', 'assistant_message must be visible text of at most 20000 characters without NUL');
      event.assistant_message = input.assistant_message;
    }
    if (input.base_revision !== undefined) {
      if (!Number.isSafeInteger(input.base_revision) || input.base_revision < 0) throw new BridgeError('invalid_input', 'base_revision must be a nonnegative integer');
      event.base_revision = input.base_revision;
    }
    const body = { action: 'ingest_turn', session_id: event.session_id || 's'.repeat(255), turn_id: event.turn_id, source, external_session_id, native_turn_id: event.native_turn_id, user_message: event.user_message, ...(event.assistant_message === undefined ? {} : { assistant_message: event.assistant_message }), ...(event.base_revision === undefined ? {} : { base_revision: event.base_revision }) };
    if (Buffer.byteLength(JSON.stringify(body)) > 100000) throw new BridgeError('invalid_input', 'Encoded visible exchange exceeds the API 100000-byte limit');
    return event;
  }
  async send(event) {
    const session_id = await this.resolve(event);
    const result = await api(this.config, { action: 'ingest_turn', session_id, turn_id: event.turn_id, source: event.source, external_session_id: event.external_session_id, native_turn_id: event.native_turn_id, user_message: event.user_message, ...(event.assistant_message === undefined ? {} : { assistant_message: event.assistant_message }), ...(event.base_revision === undefined ? {} : { base_revision: event.base_revision }) });
    if (!DURABLE.has(result.status) || !Number.isSafeInteger(result.revision) || result.revision < 0 || !Array.isArray(result.changed_ids)) throw new BridgeError('upstream_unavailable', 'Ingestion was not durably acknowledged');
    await this.cacheRevision({ source: event.source, external_session_id: event.external_session_id }, result.revision, session_id);
    return result;
  }
  async ingest(input, { promptKey } = {}) {
    const event = this.normalizeEvent(input), key = this.state.key(event);
    const prior = await this.state.read('outbox', key);
    if (prior && JSON.stringify(prior.event) !== JSON.stringify(event)) throw new BridgeError('conflicting_turn', 'A different pending payload already uses this turn ID');
    await this.state.write('outbox', key, { event, queued_at: prior?.queued_at ?? Date.now(), enqueue_order: prior?.enqueue_order ?? process.hrtime.bigint().toString(), ...(promptKey ? { prompt_key: promptKey } : prior?.prompt_key ? { prompt_key: prior.prompt_key } : {}) });
    const drained = await this.drain(event, { targetKey: key });
    return drained.targetResult ?? { status: 'queued', pending: true, turn_id: event.turn_id, error: drained.error ?? { code: 'upstream_unavailable', message: 'Earlier captures in this conversation remain pending' } };
  }
  async drain(selection = null, { budget = 5000, limit = 20, targetKey } = {}) {
    let sent = 0, pending = 0, targetResult, lastError; const started = Date.now(), blocked = new Set();
    const entries = (await this.state.entries('outbox')).sort((a, b) => (a.value.queued_at - b.value.queued_at) || compareOrder(a.value.enqueue_order, b.value.enqueue_order) || a.path.localeCompare(b.path));
    for (const { path, value } of entries) {
      const event = value.event;
      if (!event || (selection && !sameSession(event, selection))) continue;
      const sessionKey = JSON.stringify([event.source, event.external_session_id, event.session_id]);
      if (blocked.has(sessionKey) || sent + pending >= limit || Date.now() - started >= budget) { pending++; continue; }
      try {
        const result = await this.send(this.normalizeEvent(event));
        if (value.prompt_key) await this.acknowledgePrompt(value.prompt_key, event.turn_id);
        await unlink(path).catch(error => { if (error.code !== 'ENOENT') throw error; });
        sent++; if (this.state.key(event) === targetKey) targetResult = result;
      } catch (error) { pending++; blocked.add(sessionKey); lastError = safeError(error); }
    }
    return { sent, pending, targetResult, error: lastError };
  }
  async flush(selection = null, options = {}) {
    const { sent, pending } = await this.drain(selection, options);
    return { sent, pending };
  }
  async acknowledgePrompt(key, captureId) {
    const record = await this.state.read('prompts', key);
    if (record) await this.state.write('prompts', key, { ...record, completed_at: Date.now(), completed_ids: [...new Set([...record.completed_ids, captureId])].slice(-20) });
  }
  async graph(selection) { return api(this.config, { action: 'graph', session_id: await this.resolve(selection) }); }
  async useLayeredContext() {
    if (this.config.env.MEMORY_LENS_LAYERED_CONTEXT === '1') return true;
    if (this.config.env.MEMORY_LENS_LAYERED_CONTEXT === '0') return false;
    if (this.layeredCapability === null || Date.now() - this.capabilityCheckedAt > 60000) {
      const health = await api(this.config, { action: 'health' }, { unauthenticated: true });
      this.layeredCapability = Array.isArray(health.capabilities) && health.capabilities.includes('layered_context');
      this.capabilityCheckedAt = Date.now();
    }
    return this.layeredCapability;
  }
  async context(selection, query, limit) {
    const flushed = await this.flush();
    const layered = await this.useLayeredContext();
    const body = { action: layered ? 'layered_context' : 'context', session_id: await this.resolve(selection) };
    if (query !== undefined) body.query = required(query, 'query', 500);
    if (limit !== undefined) { if (!Number.isInteger(limit) || limit < 1 || limit > 20) throw new BridgeError('invalid_input', 'limit must be 1–20'); body.limit = limit; }
    const result = await api(this.config, body);
    if (typeof result.text !== 'string' || !Array.isArray(result.node_ids) || !Number.isInteger(result.revision)) throw new BridgeError('upstream_unavailable', 'Context response is invalid');
    if (layered) validateLayeredContext(result, body.session_id);
    await this.cacheRevision(selection, result.revision, body.session_id);
    return flushed.pending ? { ...result, pending_ingestion: flushed.pending } : result;
  }
  async command(selection, command, request_id = randomUUID()) {
    const session_id = await this.resolve(selection);
    const result = await api(this.config, { action: 'command', session_id, command: required(command, 'command', 4000), request_id: required(request_id, 'request_id', 255) });
    if (Number.isSafeInteger(result.revision) && result.revision >= 0) await this.cacheRevision(selection, result.revision, session_id);
    return result;
  }
}
function sameSession(a, b) {
  if (a.source && a.external_session_id && b.source && b.external_session_id) return a.source === b.source && a.external_session_id === b.external_session_id;
  return Boolean(a.session_id && b.session_id && a.session_id === b.session_id);
}
function compareOrder(a = '0', b = '0') {
  const left = BigInt(a), right = BigInt(b);
  return left < right ? -1 : left > right ? 1 : 0;
}
function validateLayeredContext(result, sessionId) {
  const memory = result.memory, code = result.code_graph;
  const invalid = () => { throw new BridgeError('upstream_unavailable', 'Layered context response is invalid or has a different session/revision'); };
  if (!memory || memory.status !== 'ready' || memory.session_id !== sessionId || memory.revision !== result.revision || typeof memory.text !== 'string' || memory.text.length > 12000 || !Array.isArray(memory.node_ids) || memory.node_ids.length > 20 || memory.node_ids.some(id => typeof id !== 'string' || id.length > 512 || !id.startsWith(sessionId + ':')) || JSON.stringify(memory.node_ids) !== JSON.stringify(result.node_ids)) invalid();
  if (!code || !['ready', 'not_ready', 'not_configured', 'unavailable'].includes(code.status) || !(code.repo_id === null || typeof code.repo_id === 'string' && code.repo_id.length <= 128) || typeof code.text !== 'string' || code.text.length > 12000 || !code.graph || !Array.isArray(code.graph.nodes) || code.graph.nodes.length > 20 || !Array.isArray(code.graph.edges) || code.graph.edges.length > 50) invalid();
  if (code.graph.nodes.some(node => !node || typeof node.id !== 'string' || node.id.length > 1024)) invalid();
  if (!Array.isArray(result.stages) || result.stages.length !== 3 || result.stages.some((stage, i) => stage.stage !== ['memory', 'code_graph', 'agent_context'][i] || stage.order !== i + 1)) invalid();
}
