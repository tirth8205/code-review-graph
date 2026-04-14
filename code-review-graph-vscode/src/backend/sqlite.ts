/**
 * Read-only SQLite reader for the code-review-graph database.
 *
 * Opens the database created by the Python backend and provides typed
 * query methods.  All writes are performed by the Python side; this
 * module never mutates the database.
 *
 * Uses `better-sqlite3` with prepared statements for performance.
 */

import type BetterSqlite3 from 'better-sqlite3';

type DatabaseType = BetterSqlite3.Database;

// Load better-sqlite3 with graceful error handling for ABI mismatches.
// On WSL or mismatched Node.js versions, the native module may fail to load.
// better-sqlite3 uses `export =` so we import the value via require() and
// type it as the DatabaseConstructor.
let Database: typeof import('better-sqlite3');
try {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  Database = require('better-sqlite3');
} catch (err: unknown) {
  const msg = err instanceof Error ? err.message : String(err);
  const isAbiMismatch = msg.includes('NODE_MODULE_VERSION')
    || msg.includes('was compiled against')
    || msg.includes('not a valid Win32');
  if (isAbiMismatch) {
    console.error(
      '[code-review-graph] better-sqlite3 ABI mismatch. '
      + 'Your VS Code uses a different Node.js version than the one '
      + 'this extension was built for. '
      + 'Try: cd ~/.vscode/extensions/code-review-graph-* && npm rebuild better-sqlite3'
    );
  }
  throw err;
}

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

export type NodeKind = 'File' | 'Class' | 'Function' | 'Type' | 'Test';

export type EdgeKind =
  | 'CALLS'
  | 'IMPORTS_FROM'
  | 'INHERITS'
  | 'IMPLEMENTS'
  | 'CONTAINS'
  | 'TESTED_BY'
  | 'DEPENDS_ON';

export interface GraphNode {
  id: number;
  kind: NodeKind;
  name: string;
  qualifiedName: string;
  filePath: string;
  lineStart: number | null;
  lineEnd: number | null;
  language: string | null;
  parentName: string | null;
  params: string | null;
  returnType: string | null;
  modifiers: string | null;
  isTest: boolean;
  fileHash: string | null;
}

export interface GraphEdge {
  id: number;
  kind: EdgeKind;
  sourceQualified: string;
  targetQualified: string;
  filePath: string;
  line: number;
}

export interface GraphStats {
  totalNodes: number;
  totalEdges: number;
  nodesByKind: Record<string, number>;
  edgesByKind: Record<string, number>;
  languages: string[];
  filesCount: number;
  lastUpdated: string | null;
  embeddingsCount: number;
}

export interface ImpactRadius {
  changedNodes: GraphNode[];
  impactedNodes: GraphNode[];
  impactedFiles: string[];
  edges: GraphEdge[];
}

// ---------------------------------------------------------------------------
// Raw row types returned by better-sqlite3
// ---------------------------------------------------------------------------

interface NodeRow {
  id: number;
  kind: string;
  name: string;
  qualified_name: string;
  file_path: string;
  line_start: number | null;
  line_end: number | null;
  language: string | null;
  parent_name: string | null;
  params: string | null;
  return_type: string | null;
  modifiers: string | null;
  is_test: number;
  file_hash: string | null;
  extra: string;
  updated_at: number;
}

interface EdgeRow {
  id: number;
  kind: string;
  source_qualified: string;
  target_qualified: string;
  file_path: string;
  line: number;
  extra: string;
  updated_at: number;
}

interface CountRow {
  cnt: number;
}

interface KindCountRow {
  kind: string;
  cnt: number;
}

interface LanguageRow {
  language: string;
}

interface FilePathRow {
  file_path: string;
}

interface MetadataRow {
  value: string;
}

// ---------------------------------------------------------------------------
// SqliteReader
// ---------------------------------------------------------------------------

const MAX_OPEN_RETRIES = 3;
const RETRY_BACKOFF_MS = 100;

export class SqliteReader {
  private db: DatabaseType | null = null;

  /**
   * Create a SqliteReader with retry logic that does not block the event loop.
   * Prefer this over the constructor when calling from async code.
   */
  static async create(dbPath: string): Promise<SqliteReader> {
    let lastError: unknown;
    for (let attempt = 0; attempt < MAX_OPEN_RETRIES; attempt++) {
      try {
        return new SqliteReader(dbPath);
      } catch (err) {
        lastError = err;
        if (attempt < MAX_OPEN_RETRIES - 1) {
          await new Promise(resolve =>
            setTimeout(resolve, RETRY_BACKOFF_MS * (attempt + 1))
          );
        }
      }
    }
    throw lastError;
  }

  constructor(dbPath: string) {
    this.db = new Database(dbPath, { readonly: true });
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('busy_timeout = 5000');
  }

  /**
   * Check if the database schema is compatible with this extension version.
   * Returns a warning message if incompatible, or undefined if OK.
   */
  checkSchemaCompatibility(): string | undefined {
    if (!this.db) { return 'Database is not open'; }
    try {
      // Check that required tables exist
      const tables = this.db
        .prepare("SELECT name FROM sqlite_master WHERE type='table'")
        .all() as Array<{ name: string }>;
      const tableNames = new Set(tables.map((t) => t.name));

      if (!tableNames.has('nodes') || !tableNames.has('edges')) {
        return 'Database is missing required tables (nodes/edges). Rebuild required.';
      }

      // Check for schema_version in metadata if it exists
      if (tableNames.has('metadata')) {
        const row = this.db
          .prepare("SELECT value FROM metadata WHERE key = 'schema_version'")
          .get() as { value: string } | undefined;
        if (row) {
          const version = parseInt(row.value, 10);
          // Must match LATEST_VERSION in code_review_graph/migrations.py
          const SUPPORTED_SCHEMA_VERSION = 9;
          if (!isNaN(version) && version > SUPPORTED_SCHEMA_VERSION) {
            return `Database was created with a newer version (schema v${version}). Update the extension.`;
          }
        }
      }

      return undefined;
    } catch {
      return 'Could not verify database schema.';
    }
  }

  /** Close the database connection. Safe to call multiple times. */
  close(): void {
    if (this.db) {
      this.db.close();
      this.db = null;
    }
  }

  /** Returns true if the database is open and contains the nodes table. */
  isValid(): boolean {
    if (!this.db) { return false; }
    try {
      const row = this.db
        .prepare(
          "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
        )
        .get() as { name: string } | undefined;
      return row !== undefined;
    } catch {
      return false;
    }
  }

  // -----------------------------------------------------------------------
  // Node queries
  // -----------------------------------------------------------------------

  /** All file paths (kind='File'), ordered by file_path. */
  getAllFiles(): string[] {
    const rows = this._db()
      .prepare(
        "SELECT DISTINCT file_path FROM nodes WHERE kind = 'File' ORDER BY file_path"
      )
      .all() as FilePathRow[];
    return rows.map((r) => r.file_path);
  }

  /** All nodes in a file, ordered by line_start. */
  getNodesByFile(filePath: string): GraphNode[] {
    const rows = this._db()
      .prepare(
        'SELECT * FROM nodes WHERE file_path = ? ORDER BY line_start'
      )
      .all(filePath) as NodeRow[];
    return rows.map((r) => this._rowToNode(r));
  }

  /** Single node lookup by qualified_name. */
  getNode(qualifiedName: string): GraphNode | undefined {
    const row = this._db()
      .prepare('SELECT * FROM nodes WHERE qualified_name = ?')
      .get(qualifiedName) as NodeRow | undefined;
    return row ? this._rowToNode(row) : undefined;
  }

  /**
   * Innermost node at a cursor position.
   *
   * Returns the node whose line range contains `line` with the smallest
   * span (i.e. the most specific / innermost enclosing node).
   */
  getNodeAtCursor(filePath: string, line: number): GraphNode | undefined {
    const row = this._db()
      .prepare(
        `SELECT * FROM nodes
         WHERE file_path = ? AND line_start <= ? AND line_end >= ?
         ORDER BY (line_end - line_start) ASC
         LIMIT 1`
      )
      .get(filePath, line, line) as NodeRow | undefined;
    return row ? this._rowToNode(row) : undefined;
  }

  /** LIKE search on name and qualified_name. */
  searchNodes(query: string, limit: number = 20): GraphNode[] {
    const pattern = `%${query}%`;
    const rows = this._db()
      .prepare(
        'SELECT * FROM nodes WHERE name LIKE ? OR qualified_name LIKE ? LIMIT ?'
      )
      .all(pattern, pattern, limit) as NodeRow[];
    return rows.map((r) => this._rowToNode(r));
  }

  // -----------------------------------------------------------------------
  // Edge queries
  // -----------------------------------------------------------------------

  /** Outgoing edges from a node. */
  getEdgesBySource(qualifiedName: string): GraphEdge[] {
    const rows = this._db()
      .prepare('SELECT * FROM edges WHERE source_qualified = ?')
      .all(qualifiedName) as EdgeRow[];
    return rows.map((r) => this._rowToEdge(r));
  }

  /** Incoming edges to a node. */
  getEdgesByTarget(qualifiedName: string): GraphEdge[] {
    const rows = this._db()
      .prepare('SELECT * FROM edges WHERE target_qualified = ?')
      .all(qualifiedName) as EdgeRow[];
    return rows.map((r) => this._rowToEdge(r));
  }

  /**
   * Edges where both source and target are in the given set.
   *
   * Uses a parameterised IN clause -- safe for arbitrary set sizes
   * (better-sqlite3 handles large parameter lists efficiently).
   */
  getEdgesAmong(qualifiedNames: Set<string>): GraphEdge[] {
    if (qualifiedNames.size === 0) { return []; }
    const qns = [...qualifiedNames];
    const placeholders = qns.map(() => '?').join(',');
    const rows = this._db()
      .prepare(
        `SELECT * FROM edges
         WHERE source_qualified IN (${placeholders})
           AND target_qualified IN (${placeholders})`
      )
      .all(...qns, ...qns) as EdgeRow[];
    return rows.map((r) => this._rowToEdge(r));
  }

  // -----------------------------------------------------------------------
  // Statistics & metadata
  // -----------------------------------------------------------------------

  /** Aggregate counts, languages, last_updated, and embeddings count. */
  getStats(): GraphStats {
    const db = this._db();

    const totalNodes = (
      db.prepare('SELECT COUNT(*) AS cnt FROM nodes').get() as CountRow
    ).cnt;

    const totalEdges = (
      db.prepare('SELECT COUNT(*) AS cnt FROM edges').get() as CountRow
    ).cnt;

    const nodesByKind: Record<string, number> = {};
    const nkRows = db
      .prepare('SELECT kind, COUNT(*) AS cnt FROM nodes GROUP BY kind')
      .all() as KindCountRow[];
    for (const r of nkRows) { nodesByKind[r.kind] = r.cnt; }

    const edgesByKind: Record<string, number> = {};
    const ekRows = db
      .prepare('SELECT kind, COUNT(*) AS cnt FROM edges GROUP BY kind')
      .all() as KindCountRow[];
    for (const r of ekRows) { edgesByKind[r.kind] = r.cnt; }

    const languages = (
      db
        .prepare(
          "SELECT DISTINCT language FROM nodes WHERE language IS NOT NULL AND language != ''"
        )
        .all() as LanguageRow[]
    ).map((r) => r.language);

    const filesCount = (
      db
        .prepare("SELECT COUNT(*) AS cnt FROM nodes WHERE kind = 'File'")
        .get() as CountRow
    ).cnt;

    const lastUpdated = this.getMetadata('last_updated') ?? null;

    // Embeddings count -- table may not exist
    let embeddingsCount = 0;
    try {
      embeddingsCount = (
        db.prepare('SELECT COUNT(*) AS cnt FROM embeddings').get() as CountRow
      ).cnt;
    } catch {
      // embeddings table does not exist -- that is fine
    }

    return {
      totalNodes,
      totalEdges,
      nodesByKind,
      edgesByKind,
      languages,
      filesCount,
      lastUpdated,
      embeddingsCount,
    };
  }

  /** Read a single key from the metadata table. */
  getMetadata(key: string): string | undefined {
    const row = this._db()
      .prepare('SELECT value FROM metadata WHERE key = ?')
      .get(key) as MetadataRow | undefined;
    return row?.value;
  }

  // -----------------------------------------------------------------------
  // Impact radius (BFS traversal)
  // -----------------------------------------------------------------------

  /**
   * BFS from changed files to find all impacted nodes within `maxDepth` hops.
   *
   * Matches the Python `GraphStore.get_impact_radius` logic exactly:
   *  1. Collect all nodes in `changedFiles` as seed set.
   *  2. BFS forward (outgoing) AND backward (incoming) edges up to `maxDepth`.
   *  3. Return impacted nodes (excluding seeds), impacted files, and edges
   *     among all involved nodes.
   */
  getImpactRadius(
    changedFiles: string[],
    maxDepth: number = 2,
  ): ImpactRadius {
    // 1. Seed: all qualified names in changed files
    const seeds = new Set<string>();
    for (const f of changedFiles) {
      for (const node of this.getNodesByFile(f)) {
        seeds.add(node.qualifiedName);
      }
    }

    // 2. BFS outward through all edge types (forward + backward)
    const visited = new Set<string>();
    let frontier = new Set(seeds);
    const impacted = new Set<string>();
    let depth = 0;

    while (frontier.size > 0 && depth < maxDepth) {
      const nextFrontier = new Set<string>();
      for (const qn of frontier) {
        visited.add(qn);

        // Forward edges (things this node affects)
        for (const e of this.getEdgesBySource(qn)) {
          if (!visited.has(e.targetQualified)) {
            nextFrontier.add(e.targetQualified);
            impacted.add(e.targetQualified);
          }
        }

        // Reverse edges (things that depend on this node)
        for (const e of this.getEdgesByTarget(qn)) {
          if (!visited.has(e.sourceQualified)) {
            nextFrontier.add(e.sourceQualified);
            impacted.add(e.sourceQualified);
          }
        }
      }
      frontier = nextFrontier;
      depth++;
    }

    // 3. Resolve to full node info
    const changedNodes: GraphNode[] = [];
    for (const qn of seeds) {
      const node = this.getNode(qn);
      if (node) { changedNodes.push(node); }
    }

    const impactedNodes: GraphNode[] = [];
    for (const qn of impacted) {
      if (seeds.has(qn)) { continue; }
      const node = this.getNode(qn);
      if (node) { impactedNodes.push(node); }
    }

    const impactedFiles = [
      ...new Set(impactedNodes.map((n) => n.filePath)),
    ];

    // Collect relevant edges among all involved nodes
    const allQns = new Set([...seeds, ...impacted]);
    const edges = allQns.size > 0 ? this.getEdgesAmong(allQns) : [];

    return { changedNodes, impactedNodes, impactedFiles, edges };
  }

  // -----------------------------------------------------------------------
  // Size-based queries
  // -----------------------------------------------------------------------

  /**
   * Find nodes exceeding a line-count threshold.
   *
   * Mirrors the Python `GraphStore.get_nodes_by_size()` method.
   */
  getNodesBySize(
    minLines: number = 50,
    kind?: string,
    filePathPattern?: string,
    limit: number = 50,
  ): Array<GraphNode & { lineCount: number }> {
    const conditions = ['(line_end - line_start + 1) >= ?'];
    const params: Array<string | number> = [minLines];

    if (kind) {
      conditions.push('kind = ?');
      params.push(kind);
    }
    if (filePathPattern) {
      conditions.push('file_path LIKE ?');
      params.push(`%${filePathPattern}%`);
    }

    params.push(limit);
    const where = conditions.join(' AND ');
    const rows = this._db()
      .prepare(
        `SELECT * FROM nodes WHERE ${where} ` + // nosec
        'ORDER BY (line_end - line_start + 1) DESC LIMIT ?',
      )
      .all(...params) as NodeRow[];

    return rows.map((r) => ({
      ...this._rowToNode(r),
      lineCount:
        r.line_start != null && r.line_end != null
          ? r.line_end - r.line_start + 1
          : 0,
    }));
  }

  // -----------------------------------------------------------------------
  // Private helpers
  // -----------------------------------------------------------------------

  /** Return the open database handle or throw. */
  private _db(): DatabaseType {
    if (!this.db) {
      throw new Error('SqliteReader: database is closed');
    }
    return this.db;
  }

  /** Convert a raw node row (snake_case) to a typed GraphNode (camelCase). */
  private _rowToNode(row: NodeRow): GraphNode {
    return {
      id: row.id,
      kind: row.kind as NodeKind,
      name: row.name,
      qualifiedName: row.qualified_name,
      filePath: row.file_path,
      lineStart: row.line_start,
      lineEnd: row.line_end,
      language: row.language ?? null,
      parentName: row.parent_name ?? null,
      params: row.params ?? null,
      returnType: row.return_type ?? null,
      modifiers: row.modifiers ?? null,
      isTest: row.is_test === 1,
      fileHash: row.file_hash ?? null,
    };
  }

  /** Convert a raw edge row (snake_case) to a typed GraphEdge (camelCase). */
  private _rowToEdge(row: EdgeRow): GraphEdge {
    return {
      id: row.id,
      kind: row.kind as EdgeKind,
      sourceQualified: row.source_qualified,
      targetQualified: row.target_qualified,
      filePath: row.file_path,
      line: row.line ?? 0,
    };
  }
}
