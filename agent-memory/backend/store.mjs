import { DatabaseSync } from 'node:sqlite';
import { randomUUID, createHash } from 'node:crypto';
import { mkdirSync, chmodSync } from 'node:fs';
import { dirname } from 'node:path';
import { ApiError } from './errors.mjs';

const LOCAL_OWNER = 'local';
const digest = value => createHash('sha256').update(value).digest('hex');
const normalize = value => value.trim().replace(/\s+/g, ' ').toLowerCase();
export const originEventId = (source, external, turnId) => 'origin-turn:' + digest(JSON.stringify([source, external, turnId]));

export class MemoryStore {
  constructor(path, { reservationLeaseMs = 120_000 } = {}) {
    this.reservationLeaseMs = reservationLeaseMs;
    if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    if (path !== ':memory:') chmodSync(path, 0o600);
    this.db.exec('PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL;');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source TEXT NOT NULL,
        external_session_id TEXT NOT NULL, title TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0, last_manual_revision INTEGER NOT NULL DEFAULT 0, graph_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(owner_id, source, external_session_id)
      );
      CREATE TABLE IF NOT EXISTS events (
        owner_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id),
        kind TEXT NOT NULL, event_id TEXT NOT NULL, content_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
        reservation TEXT NOT NULL, base_revision INTEGER NOT NULL, lease_until INTEGER NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(owner_id, session_id, kind, event_id)
      );
      CREATE INDEX IF NOT EXISTS events_session ON events(owner_id, session_id, updated_at);
      CREATE TABLE IF NOT EXISTS workspace_memory (
        owner_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id)
      );
      CREATE TABLE IF NOT EXISTS native_sources (
        owner_id TEXT NOT NULL, source TEXT NOT NULL, external_session_id TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES sessions(id), title TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY(owner_id,source,external_session_id)
      );
    `);
    if (!this.db.prepare('PRAGMA table_info(sessions)').all().some(column => column.name === 'last_manual_revision')) this.db.exec('ALTER TABLE sessions ADD COLUMN last_manual_revision INTEGER NOT NULL DEFAULT 0');
    if (!this.db.prepare('PRAGMA table_info(events)').all().some(column => column.name === 'base_revision')) this.db.exec('ALTER TABLE events ADD COLUMN base_revision INTEGER NOT NULL DEFAULT 0');
    this.initializeWorkspace();
  }

  transaction(work) {
    this.db.exec('BEGIN IMMEDIATE');
    try { const result = work(); this.db.exec('COMMIT'); return result; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }

  initializeWorkspace() {
    return this.transaction(() => {
      if (this.db.prepare('SELECT session_id FROM workspace_memory WHERE owner_id=?').get(LOCAL_OWNER)) return;
      // Additive migration: consolidate a copy; retain every legacy session/event row.
      const legacy = this.db.prepare('SELECT * FROM sessions WHERE owner_id=? ORDER BY updated_at ASC,id ASC').all(LOCAL_OWNER);
      const id = `s_${randomUUID()}`;
      const nodes = new Map();
      const edges = new Map();
      const idMaps = new Map();
      const now = new Date().toISOString();
      const revision = legacy.length ? Math.max(...legacy.map(row => row.revision)) + 1 : 0;
      const manualRevision = legacy.some(row => row.last_manual_revision > 0) ? revision : 0;
      const candidates = [];
      for (const row of legacy) {
        const graph = JSON.parse(row.graph_json);
        idMaps.set(row.id, new Map());
        const eventTimes = new Map(this.db.prepare('SELECT kind,event_id,updated_at FROM events WHERE owner_id=? AND session_id=?').all(LOCAL_OWNER, row.id).map(event => [event.kind + ':' + event.event_id, event.updated_at]));
        const provenance = { source: row.source, external_session_id: row.external_session_id };
        for (const original of graph.nodes) {
          // A noop can touch the session later than its actual memory change.
          // Prefer captured event/history chronology; row time is legacy fallback.
          const currentTime = eventTimes.get((original.source_kind === 'command' ? 'command' : 'turn') + ':' + original.source_turn_id);
          const times = [currentTime, ...(original.history || []).map(entry => entry.changed_at)].filter(value => Number.isFinite(Date.parse(value)));
          const versionAt = times.length ? new Date(Math.max(...times.map(value => Date.parse(value)))).toISOString() : row.updated_at;
          candidates.push({ row, original, provenance, versionAt });
        }
      }
      const priorVersion = (node, changedAt) => ({ content: node.content, status: node.status === 'active' ? 'superseded' : 'forgotten', source_turn_id: node.source_turn_id, source_kind: node.source_kind, source: node.source, external_session_id: node.external_session_id, native_turn_id: node.native_turn_id, evidence: node.evidence, changed_at: changedAt });
      candidates.sort((a, b) => a.versionAt.localeCompare(b.versionAt) || a.row.id.localeCompare(b.row.id) || a.original.id.localeCompare(b.original.id));
      for (const { row, original, provenance, versionAt } of candidates) {
        const key = normalize(original.content);
        const existing = nodes.get(key);
        const historical = new Set((original.history || []).filter(entry => ['superseded', 'forgotten'].includes(entry.status)).map(entry => normalize(entry.content)));
        const predecessors = [...nodes.entries()].filter(([content]) => content !== key && historical.has(content));
        const inherited = [...nodes.values()].find(node => node.history.some(entry => normalize(entry.content) === key));
        if (!existing && !predecessors.length && inherited && original.source_kind !== 'command') {
          // A later automatic capture cannot revive already superseded memory.
          idMaps.get(row.id).set(original.id, inherited.id);
          inherited.history.push(priorVersion({ ...original, ...provenance }, versionAt));
          continue;
        }
        const nextId = existing?.id ?? predecessors[0]?.[1].id ?? (original.source_kind === 'command' ? inherited?.id : undefined) ?? `${id}:n:${digest(key)}`;
        const absorbed = [...new Set([existing, ...predecessors.map(([, node]) => node), ...(inherited?.id === nextId && !existing ? [inherited] : [])].filter(Boolean))];
        for (const [content, node] of [...nodes]) if (absorbed.includes(node)) nodes.delete(content);
        for (const map of idMaps.values()) for (const [oldId, mappedId] of map) if (absorbed.some(node => node.id === mappedId)) map.set(oldId, nextId);
        idMaps.get(row.id).set(original.id, nextId);
        const history = absorbed.flatMap(node => [...node.history, priorVersion(node, versionAt)]);
        const node = { ...original, ...provenance, native_turn_id: original.native_turn_id ?? original.source_turn_id, id: nextId, history: [...history, ...(original.history || []).map(entry => ({ ...provenance, native_turn_id: entry.source_turn_id, ...entry }))] };
        nodes.set(key, node);
      }
      // Resolve edges only after all predecessor identities have been reconciled.
      for (const row of legacy) {
        const graph = JSON.parse(row.graph_json);
        const map = idMaps.get(row.id);
        for (const edge of graph.edges) {
          const source = map.get(edge.source), target = map.get(edge.target);
          if (!source || !target || source === target) continue;
          const nextId = `${id}:e:${digest(JSON.stringify([source, target, normalize(edge.label)]))}`;
          edges.set(nextId, { ...edge, id: nextId, source, target });
        }
      }
      const active = new Set([...nodes.values()].filter(node => node.status === 'active').map(node => node.id));
      const snapshot = { nodes: [...nodes.values()], edges: [...edges.values()].filter(edge => active.has(edge.source) && active.has(edge.target)) };
      this.db.prepare('INSERT INTO sessions(id,owner_id,source,external_session_id,title,revision,last_manual_revision,graph_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?)').run(id, LOCAL_OWNER, 'workspace', 'agent-memory:' + id, 'Agent memory', revision, manualRevision, JSON.stringify(snapshot), now);
      this.db.prepare('INSERT INTO workspace_memory(owner_id,session_id) VALUES (?,?)').run(LOCAL_OWNER, id);
      for (const row of legacy) {
        this.db.prepare('INSERT OR IGNORE INTO native_sources(owner_id,source,external_session_id,session_id,title,updated_at) VALUES (?,?,?,?,?,?)').run(LOCAL_OWNER, row.source, row.external_session_id, id, row.title, now);
        const events = this.db.prepare('SELECT * FROM events WHERE owner_id=? AND session_id=?').all(LOCAL_OWNER, row.id);
        for (const event of events) {
          const original = JSON.parse(event.payload_json);
          const source = original.source ?? row.source;
          const external = original.external_session_id ?? row.external_session_id;
          const payload = event.kind === 'turn' ? { user_message: original.user_message, assistant_message: original.assistant_message ?? '', source, external_session_id: external, native_turn_id: original.native_turn_id ?? event.event_id, capture_turn_id: original.capture_turn_id ?? event.event_id, ...(original.base_revision !== undefined ? { base_revision: original.base_revision } : {}) } : original;
          const nextId = event.kind === 'turn' ? originEventId(source, external, event.event_id) : 'legacy-command:' + digest(JSON.stringify([row.id, event.event_id]));
          const result = event.result_json ? { ...JSON.parse(event.result_json), session_id: id, revision, changed_ids: (JSON.parse(event.result_json).changed_ids || []).map(old => idMaps.get(row.id).get(old)).filter(Boolean) } : null;
          this.db.prepare('INSERT OR IGNORE INTO events(owner_id,session_id,kind,event_id,content_hash,payload_json,status,result_json,reservation,base_revision,lease_until,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)').run(LOCAL_OWNER, id, event.kind, nextId, digest(JSON.stringify(payload)), JSON.stringify(payload), event.status === 'pending' ? 'failed' : event.status, result ? JSON.stringify(result) : null, randomUUID(), event.base_revision, 0, event.updated_at);
        }
      }
    });
  }

  ensureSession(source, external, title = '') {
    return this.transaction(() => {
      const id = this.db.prepare('SELECT session_id FROM workspace_memory WHERE owner_id=?').get(LOCAL_OWNER).session_id;
      this.db.prepare('INSERT INTO native_sources(owner_id,source,external_session_id,session_id,title,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(owner_id,source,external_session_id) DO UPDATE SET updated_at=excluded.updated_at').run(LOCAL_OWNER, source, external, id, title || external.slice(0, 100), new Date().toISOString());
      return { session_id: id, revision: this.requireSession(id).revision };
    });
  }

  requireSession(id) {
    const row = this.db.prepare('SELECT s.* FROM sessions s JOIN workspace_memory w ON w.session_id=s.id AND w.owner_id=s.owner_id WHERE s.id=? AND s.owner_id=?').get(id, LOCAL_OWNER);
    if (!row) throw new ApiError(404, 'session_not_found', 'This memory graph does not belong to this local workspace.');
    return { ...row, graph: JSON.parse(row.graph_json) };
  }

  listSessions() {
    return this.db.prepare('SELECT s.id,s.title,s.source,s.external_session_id,s.revision,s.updated_at FROM sessions s JOIN workspace_memory w ON w.session_id=s.id AND w.owner_id=s.owner_id WHERE s.owner_id=?').all(LOCAL_OWNER);
  }

  graph(id) {
    const session = this.requireSession(id);
    const rows = this.db.prepare("SELECT event_id,payload_json,status,updated_at FROM events WHERE owner_id=? AND session_id=? AND kind='turn' ORDER BY updated_at DESC LIMIT 200").all(LOCAL_OWNER, id);
    const nodes = session.graph.nodes.filter(node => node.id.startsWith(id + ':'));
    const active = new Set(nodes.filter(node => node.status === 'active').map(node => node.id));
    const edges = session.graph.edges.filter(edge => edge.id.startsWith(id + ':') && active.has(edge.source) && active.has(edge.target));
    return { session_id: id, revision: session.revision, nodes, edges, sources: this.db.prepare('SELECT source,external_session_id,title,updated_at FROM native_sources WHERE owner_id=? AND session_id=? ORDER BY updated_at').all(LOCAL_OWNER, id), turns: rows.reverse().map(row => { const payload = JSON.parse(row.payload_json); return { ...payload, turn_id: payload.capture_turn_id ?? row.event_id, status: row.status, updated_at: row.updated_at }; }) };
  }

  reserve(id, kind, eventId, contentHash, payload) {
    return this.transaction(() => {
      const session = this.requireSession(id);
      const existing = this.db.prepare('SELECT * FROM events WHERE owner_id=? AND session_id=? AND kind=? AND event_id=?').get(LOCAL_OWNER, id, kind, eventId);
      if (existing && existing.content_hash !== contentHash) throw new ApiError(409, 'conflicting_turn', 'This request ID was already used with different content.');
      if (existing && ['applied', 'noop'].includes(existing.status)) return { duplicate: true, result: JSON.parse(existing.result_json) };
      if (existing?.status === 'pending' && existing.lease_until > Date.now()) throw new ApiError(409, 'revision_conflict', 'This request is still processing. Retry the same request after it completes.');
      const reservation = randomUUID();
      const now = new Date().toISOString();
      const baseRevision = existing?.base_revision ?? payload.base_revision ?? session.revision;
      this.db.prepare(`INSERT INTO events(owner_id,session_id,kind,event_id,content_hash,payload_json,status,reservation,base_revision,lease_until,updated_at)
        VALUES (?,?,?,?,?,?,'pending',?,?,?,?) ON CONFLICT(owner_id,session_id,kind,event_id)
        DO UPDATE SET status='pending', reservation=excluded.reservation, lease_until=excluded.lease_until, updated_at=excluded.updated_at`).run(LOCAL_OWNER, id, kind, eventId, contentHash, JSON.stringify(payload), reservation, baseRevision, Date.now() + this.reservationLeaseMs, now);
      return { session, reservation, base_revision: baseRevision };
    });
  }

  complete(id, kind, eventId, reservation, expectedRevision, graph, result) {
    return this.transaction(() => {
      const current = this.requireSession(id);
      const row = this.db.prepare('SELECT reservation,status FROM events WHERE owner_id=? AND session_id=? AND kind=? AND event_id=?').get(LOCAL_OWNER, id, kind, eventId);
      if (current.revision !== expectedRevision || row?.reservation !== reservation || row?.status !== 'pending') {
        throw new ApiError(409, 'revision_conflict', 'The conversation changed during extraction. Retry the same request to extract against its latest memory.');
      }
      const now = new Date().toISOString();
      const manualRevision = kind === 'command' && result.status === 'applied' ? result.revision : current.last_manual_revision;
      const changed = this.db.prepare('UPDATE sessions SET graph_json=?,revision=?,last_manual_revision=?,updated_at=? WHERE id=? AND owner_id=? AND revision=?').run(JSON.stringify(graph), result.revision, manualRevision, now, id, LOCAL_OWNER, expectedRevision);
      if (changed.changes !== 1) throw new ApiError(409, 'revision_conflict', 'The conversation changed. Retry the same request.');
      this.db.prepare('UPDATE events SET status=?,result_json=?,lease_until=0,updated_at=? WHERE owner_id=? AND session_id=? AND kind=? AND event_id=? AND reservation=?').run(result.status, JSON.stringify(result), now, LOCAL_OWNER, id, kind, eventId, reservation);
      return result;
    });
  }

  fail(id, kind, eventId, reservation) {
    this.db.prepare("UPDATE events SET status='failed',lease_until=0,updated_at=? WHERE owner_id=? AND session_id=? AND kind=? AND event_id=? AND reservation=? AND status='pending'").run(new Date().toISOString(), LOCAL_OWNER, id, kind, eventId, reservation);
  }

  close() { this.db.close(); }
}
