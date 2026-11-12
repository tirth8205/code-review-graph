/**
 * Tests for the SqliteReader module.
 *
 * Creates a temporary SQLite database with the exact schema used by the
 * Python backend, inserts representative test data, and validates every
 * public method of SqliteReader.
 */

import * as assert from 'assert';
import { after, before, describe, it } from 'node:test';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import Database from 'better-sqlite3';
import { SqliteReader, GraphNode, GraphEdge } from '../src/backend/sqlite';

// ---------------------------------------------------------------------------
// Schema (mirrors the Python backend exactly)
// ---------------------------------------------------------------------------

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_qualified ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file_path);
`;

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

const NOW = Date.now() / 1000;

interface TestNode {
  kind: string;
  name: string;
  qualified_name: string;
  file_path: string;
  line_start: number;
  line_end: number;
  language: string;
  parent_name: string | null;
  params: string | null;
  return_type: string | null;
  modifiers: string | null;
  is_test: number;
  file_hash: string;
  extra: string;
  updated_at: number;
}

interface TestEdge {
  kind: string;
  source_qualified: string;
  target_qualified: string;
  file_path: string;
  line: number;
  extra: string;
  updated_at: number;
}

const TEST_NODES: TestNode[] = [
  // auth.py -- File node + 2 functions
  {
    kind: 'File', name: 'auth.py', qualified_name: 'src/auth.py',
    file_path: 'src/auth.py', line_start: 1, line_end: 50,
    language: 'python', parent_name: null, params: null, return_type: null,
    modifiers: null, is_test: 0, file_hash: 'aaa', extra: '{}', updated_at: NOW,
  },
  {
    kind: 'Function', name: 'login', qualified_name: 'src/auth.py::login',
    file_path: 'src/auth.py', line_start: 5, line_end: 20,
    language: 'python', parent_name: null, params: '(username, password)',
    return_type: 'bool', modifiers: null, is_test: 0, file_hash: 'aaa',
    extra: '{}', updated_at: NOW,
  },
  {
    kind: 'Function', name: 'logout', qualified_name: 'src/auth.py::logout',
    file_path: 'src/auth.py', line_start: 22, line_end: 35,
    language: 'python', parent_name: null, params: '(session)',
    return_type: 'None', modifiers: null, is_test: 0, file_hash: 'aaa',
    extra: '{}', updated_at: NOW,
  },

  // routes.py -- File node + 1 function
  {
    kind: 'File', name: 'routes.py', qualified_name: 'src/routes.py',
    file_path: 'src/routes.py', line_start: 1, line_end: 40,
    language: 'python', parent_name: null, params: null, return_type: null,
    modifiers: null, is_test: 0, file_hash: 'bbb', extra: '{}', updated_at: NOW,
  },
  {
    kind: 'Function', name: 'handle_login', qualified_name: 'src/routes.py::handle_login',
    file_path: 'src/routes.py', line_start: 10, line_end: 30,
    language: 'python', parent_name: null, params: '(request)',
    return_type: 'Response', modifiers: null, is_test: 0, file_hash: 'bbb',
    extra: '{}', updated_at: NOW,
  },

  // test_auth.py -- File node + 1 test function
  {
    kind: 'File', name: 'test_auth.py', qualified_name: 'tests/test_auth.py',
    file_path: 'tests/test_auth.py', line_start: 1, line_end: 30,
    language: 'python', parent_name: null, params: null, return_type: null,
    modifiers: null, is_test: 0, file_hash: 'ccc', extra: '{}', updated_at: NOW,
  },
  {
    kind: 'Test', name: 'test_login', qualified_name: 'tests/test_auth.py::test_login',
    file_path: 'tests/test_auth.py', line_start: 5, line_end: 25,
    language: 'python', parent_name: null, params: '()',
    return_type: 'None', modifiers: null, is_test: 1, file_hash: 'ccc',
    extra: '{}', updated_at: NOW,
  },
];

const TEST_EDGES: TestEdge[] = [
  // routes.py::handle_login CALLS auth.py::login
  {
    kind: 'CALLS', source_qualified: 'src/routes.py::handle_login',
    target_qualified: 'src/auth.py::login', file_path: 'src/routes.py',
    line: 15, extra: '{}', updated_at: NOW,
  },
  // routes.py IMPORTS_FROM auth.py
  {
    kind: 'IMPORTS_FROM', source_qualified: 'src/routes.py',
    target_qualified: 'src/auth.py', file_path: 'src/routes.py',
    line: 1, extra: '{}', updated_at: NOW,
  },
  // auth.py CONTAINS login
  {
    kind: 'CONTAINS', source_qualified: 'src/auth.py',
    target_qualified: 'src/auth.py::login', file_path: 'src/auth.py',
    line: 5, extra: '{}', updated_at: NOW,
  },
  // auth.py CONTAINS logout
  {
    kind: 'CONTAINS', source_qualified: 'src/auth.py',
    target_qualified: 'src/auth.py::logout', file_path: 'src/auth.py',
    line: 22, extra: '{}', updated_at: NOW,
  },
  // test_auth.py::test_login TESTED_BY (reverse: login is tested by test_login)
  {
    kind: 'TESTED_BY', source_qualified: 'src/auth.py::login',
    target_qualified: 'tests/test_auth.py::test_login', file_path: 'tests/test_auth.py',
    line: 5, extra: '{}', updated_at: NOW,
  },
];

// ---------------------------------------------------------------------------
// Helper: create a populated temp database
// ---------------------------------------------------------------------------

function createTestDb(): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'crg-test-'));
  const dbPath = path.join(tmpDir, 'graph.db');

  const db = new Database(dbPath);
  db.exec(SCHEMA_SQL);

  const insertNode = db.prepare(`
    INSERT INTO nodes
      (kind, name, qualified_name, file_path, line_start, line_end,
       language, parent_name, params, return_type, modifiers, is_test,
       file_hash, extra, updated_at)
    VALUES
      (@kind, @name, @qualified_name, @file_path, @line_start, @line_end,
       @language, @parent_name, @params, @return_type, @modifiers, @is_test,
       @file_hash, @extra, @updated_at)
  `);

  const insertEdge = db.prepare(`
    INSERT INTO edges
      (kind, source_qualified, target_qualified, file_path, line, extra, updated_at)
    VALUES
      (@kind, @source_qualified, @target_qualified, @file_path, @line, @extra, @updated_at)
  `);

  const insertMeta = db.prepare(
    'INSERT INTO metadata (key, value) VALUES (?, ?)'
  );

  const insertMany = db.transaction(() => {
    for (const n of TEST_NODES) { insertNode.run(n); }
    for (const e of TEST_EDGES) { insertEdge.run(e); }
    insertMeta.run('last_updated', '2025-06-15T10:30:00Z');
  });
  insertMany();
  db.close();

  return dbPath;
}

function cleanup(dbPath: string): void {
  try {
    const dir = path.dirname(dbPath);
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    // best effort
  }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('SqliteReader', () => {
  let dbPath: string;
  let reader: SqliteReader;

  before(() => {
    dbPath = createTestDb();
    reader = new SqliteReader(dbPath);
  });

  after(() => {
    reader.close();
    cleanup(dbPath);
  });

  // -- isValid ------------------------------------------------------------

  it('isValid() returns true for a properly initialised database', () => {
    assert.strictEqual(reader.isValid(), true);
  });

  // -- getAllFiles ---------------------------------------------------------

  it('getAllFiles() returns file paths ordered alphabetically', () => {
    const files = reader.getAllFiles();
    assert.deepStrictEqual(files, [
      'src/auth.py',
      'src/routes.py',
      'tests/test_auth.py',
    ]);
  });

  // -- getNodesByFile -----------------------------------------------------

  it('getNodesByFile() returns nodes ordered by line_start', () => {
    const nodes = reader.getNodesByFile('src/auth.py');
    assert.strictEqual(nodes.length, 3); // File + login + logout

    // Verify ordering
    assert.strictEqual(nodes[0].name, 'auth.py');
    assert.strictEqual(nodes[1].name, 'login');
    assert.strictEqual(nodes[2].name, 'logout');

    // Verify camelCase conversion
    assert.strictEqual(nodes[1].qualifiedName, 'src/auth.py::login');
    assert.strictEqual(nodes[1].lineStart, 5);
    assert.strictEqual(nodes[1].lineEnd, 20);
    assert.strictEqual(nodes[1].returnType, 'bool');
    assert.strictEqual(nodes[1].isTest, false);
  });

  // -- getNode ------------------------------------------------------------

  it('getNode() returns a single node by qualified name', () => {
    const node = reader.getNode('src/auth.py::login');
    assert.ok(node);
    assert.strictEqual(node.kind, 'Function');
    assert.strictEqual(node.name, 'login');
    assert.strictEqual(node.params, '(username, password)');
  });

  it('getNode() returns undefined for non-existent qualified name', () => {
    const node = reader.getNode('src/auth.py::nonexistent');
    assert.strictEqual(node, undefined);
  });

  // -- getNodeAtCursor ----------------------------------------------------

  it('getNodeAtCursor() returns the innermost node at the cursor', () => {
    // Line 10 is inside login (5-20) and inside auth.py (1-50).
    // login has the smaller span so it should be returned.
    const node = reader.getNodeAtCursor('src/auth.py', 10);
    assert.ok(node);
    assert.strictEqual(node.name, 'login');
  });

  it('getNodeAtCursor() returns the File node when cursor is outside functions', () => {
    // Line 45 is inside auth.py (1-50) but outside both functions.
    const node = reader.getNodeAtCursor('src/auth.py', 45);
    assert.ok(node);
    assert.strictEqual(node.kind, 'File');
    assert.strictEqual(node.name, 'auth.py');
  });

  it('getNodeAtCursor() returns undefined when no node covers the line', () => {
    const node = reader.getNodeAtCursor('src/auth.py', 999);
    assert.strictEqual(node, undefined);
  });

  // -- getEdgesBySource ---------------------------------------------------

  it('getEdgesBySource() returns outgoing edges', () => {
    const edges = reader.getEdgesBySource('src/routes.py::handle_login');
    assert.strictEqual(edges.length, 1);
    assert.strictEqual(edges[0].kind, 'CALLS');
    assert.strictEqual(edges[0].targetQualified, 'src/auth.py::login');
    assert.strictEqual(edges[0].line, 15);
  });

  // -- getEdgesByTarget ---------------------------------------------------

  it('getEdgesByTarget() returns incoming edges', () => {
    const edges = reader.getEdgesByTarget('src/auth.py::login');
    // CALLS from handle_login + CONTAINS from auth.py
    assert.strictEqual(edges.length, 2);
    const kinds = edges.map((e) => e.kind).sort();
    assert.deepStrictEqual(kinds, ['CALLS', 'CONTAINS']);
  });

  // -- getEdgesAmong ------------------------------------------------------

  it('getEdgesAmong() returns only edges within the given set', () => {
    const qns = new Set([
      'src/routes.py::handle_login',
      'src/auth.py::login',
      'src/auth.py',
    ]);
    const edges = reader.getEdgesAmong(qns);
    // Should include: CALLS handle_login->login, CONTAINS auth.py->login,
    // IMPORTS_FROM routes.py->auth.py only if routes.py is in set (it's not).
    assert.ok(edges.length >= 2);
    for (const e of edges) {
      assert.ok(qns.has(e.sourceQualified), `source ${e.sourceQualified} should be in set`);
      assert.ok(qns.has(e.targetQualified), `target ${e.targetQualified} should be in set`);
    }
  });

  it('getEdgesAmong() returns empty array for empty set', () => {
    const edges = reader.getEdgesAmong(new Set());
    assert.deepStrictEqual(edges, []);
  });

  // -- searchNodes --------------------------------------------------------

  it('searchNodes() finds nodes by name substring', () => {
    const results = reader.searchNodes('login');
    assert.ok(results.length >= 2); // login, handle_login, test_login
    const names = results.map((n) => n.name);
    assert.ok(names.includes('login'));
    assert.ok(names.includes('handle_login'));
  });

  it('searchNodes() respects limit', () => {
    const results = reader.searchNodes('login', 1);
    assert.strictEqual(results.length, 1);
  });

  it('searchNodes() returns empty for no match', () => {
    const results = reader.searchNodes('zzz_no_match_zzz');
    assert.deepStrictEqual(results, []);
  });

  // -- getStats -----------------------------------------------------------

  it('getStats() returns correct aggregate counts', () => {
    const stats = reader.getStats();
    assert.strictEqual(stats.totalNodes, TEST_NODES.length);
    assert.strictEqual(stats.totalEdges, TEST_EDGES.length);
    assert.strictEqual(stats.filesCount, 3); // 3 File nodes
    assert.deepStrictEqual(stats.languages, ['python']);
    assert.strictEqual(stats.lastUpdated, '2025-06-15T10:30:00Z');
    assert.strictEqual(stats.embeddingsCount, 0); // no embeddings table data

    // Nodes by kind
    assert.strictEqual(stats.nodesByKind['File'], 3);
    assert.strictEqual(stats.nodesByKind['Function'], 3);
    assert.strictEqual(stats.nodesByKind['Test'], 1);

    // Edges by kind
    assert.strictEqual(stats.edgesByKind['CALLS'], 1);
    assert.strictEqual(stats.edgesByKind['IMPORTS_FROM'], 1);
    assert.strictEqual(stats.edgesByKind['CONTAINS'], 2);
    assert.strictEqual(stats.edgesByKind['TESTED_BY'], 1);
  });

  // -- getMetadata --------------------------------------------------------

  it('getMetadata() returns stored value', () => {
    const val = reader.getMetadata('last_updated');
    assert.strictEqual(val, '2025-06-15T10:30:00Z');
  });

  it('getMetadata() returns undefined for missing key', () => {
    const val = reader.getMetadata('nonexistent_key');
    assert.strictEqual(val, undefined);
  });

  // -- getImpactRadius ----------------------------------------------------

  it('getImpactRadius() finds changed and impacted nodes', () => {
    const result = reader.getImpactRadius(['src/auth.py'], 2);

    // Changed nodes: everything in auth.py (File + login + logout)
    assert.strictEqual(result.changedNodes.length, 3);
    const changedNames = result.changedNodes.map((n) => n.name).sort();
    assert.deepStrictEqual(changedNames, ['auth.py', 'login', 'logout']);

    // Impacted nodes: handle_login (calls login), test_login (tested_by),
    // routes.py (imports_from auth.py), test_auth.py file (contains test_login)
    assert.ok(
      result.impactedNodes.length > 0,
      'should have impacted nodes'
    );
    const impactedNames = result.impactedNodes.map((n) => n.name);
    assert.ok(
      impactedNames.includes('handle_login'),
      'handle_login should be impacted (calls login)'
    );
    assert.ok(
      impactedNames.includes('test_login'),
      'test_login should be impacted (tested_by)'
    );

    // Impacted files should include routes.py and/or tests/test_auth.py
    assert.ok(
      result.impactedFiles.length > 0,
      'should have impacted files'
    );

    // Edges among all involved nodes
    assert.ok(
      result.edges.length > 0,
      'should have connecting edges'
    );
  });

  it('getImpactRadius() with depth 0 returns only seeds, no impacted nodes', () => {
    const result = reader.getImpactRadius(['src/auth.py'], 0);
    assert.strictEqual(result.changedNodes.length, 3);
    assert.strictEqual(result.impactedNodes.length, 0);
  });

  it('getImpactRadius() for non-existent file returns empty', () => {
    const result = reader.getImpactRadius(['nonexistent.py']);
    assert.strictEqual(result.changedNodes.length, 0);
    assert.strictEqual(result.impactedNodes.length, 0);
    assert.strictEqual(result.impactedFiles.length, 0);
  });

  // -- close / isValid after close ----------------------------------------

  it('isValid() returns false after close()', () => {
    const tmpPath = createTestDb();
    const tmpReader = new SqliteReader(tmpPath);
    assert.strictEqual(tmpReader.isValid(), true);
    tmpReader.close();
    assert.strictEqual(tmpReader.isValid(), false);
    cleanup(tmpPath);
  });

  // -- constructor retry on bad path --------------------------------------

  it('constructor throws after retries for a non-existent path', () => {
    assert.throws(() => {
      new SqliteReader('/nonexistent/path/to/database.db');
    });
  });
});
