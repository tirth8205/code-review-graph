// Browser client for the Memory Lens memory-api wire contract (version 1).
// Single POST endpoint, JSON body with `action`, bearer credential. No Supabase
// publishable key is required, so a local bridge service or a future deployed
// Cloud function can be used interchangeably. Tokens are never logged.
import { z } from "zod";

export const memoryTypes = ["entity", "fact", "decision", "constraint", "task"] as const;

const historySchema = z.object({
  content: z.string(),
  source_turn_id: z.string().nullable().optional(),
  source_kind: z.string().optional(),
  evidence: z.string().nullable().optional(),
  status: z.string().optional(),
  time: z.string().optional(),
});
export const liveNodeSchema = z.object({
  id: z.string().min(1),
  type: z.enum(memoryTypes),
  content: z.string(),
  status: z.enum(["active", "forgotten"]),
  source_turn_id: z.string().nullable(),
  source_kind: z.string(),
  evidence: z.string().nullable(),
  pinned: z.boolean(),
  position: z.object({ x: z.number(), y: z.number() }).nullable().optional(),
  history: z.array(historySchema),
  session_id: z.string().optional(),
});
export const liveEdgeSchema = z.object({
  id: z.string().min(1),
  source: z.string().min(1),
  target: z.string().min(1),
  label: z.string(),
});
const turnSchema = z.object({
  turn_id: z.string(),
  user_message: z.string(),
  assistant_message: z.string().nullable().optional(),
  status: z.string(),
  created_at: z.string().optional(),
  changed_ids: z.array(z.string()).optional(),
  // Provenance only: which tool and native chat the prompt came from. Never a graph partition.
  source: z.string().nullable().optional(),
  external_session_id: z.string().nullable().optional(),
});
const sessionSchema = z.object({
  id: z.string().min(1),
  title: z.string().nullable(),
  source: z.string(),
  external_session_id: z.string(),
  revision: z.number().int().nonnegative(),
  updated_at: z.string(),
});
const stageSchema = z.object({
  stage: z.enum(["memory", "code_graph", "agent_context"]),
  order: z.number().int(),
  status: z.string(),
  revision: z.number().int().optional(),
  reason: z.string().optional(),
});
export const codeGraphStatuses = ["ready", "not_ready", "unavailable", "not_configured"] as const;
export const layeredContextSchema = z.object({
  session_id: z.string(),
  revision: z.number().int().nonnegative(),
  node_ids: z.array(z.string()),
  text: z.string(),
  memory: z.object({
    status: z.literal("ready"),
    session_id: z.string(),
    revision: z.number().int().nonnegative(),
    node_ids: z.array(z.string()),
    text: z.string(),
  }),
  code_graph: z.object({
    status: z.enum(codeGraphStatuses),
    repo_id: z.string().nullable().optional(),
    /** Code graph content stays opaque here; the memory UI never renders it as nodes. */
    graph: z
      .object({ nodes: z.array(z.unknown()), edges: z.array(z.unknown()) })
      .nullable()
      .optional(),
    text: z.string().nullable().optional(),
    reason: z.string().optional(),
  }),
  stages: z.array(stageSchema),
  retrieved_at: z.string(),
});
const schemas = {
  health: z.object({
    ok: z.literal(true),
    api_version: z.literal(1),
    model: z.string(),
    openai_configured: z.boolean(),
    /** Optional advertised features, e.g. "pin". Absent means none. */
    capabilities: z.array(z.string()).optional(),
    existing_code_graph: z.object({ configured: z.boolean(), readiness: z.string() }).optional(),
  }),
  list_sessions: z.object({ sessions: z.array(sessionSchema) }),
  ensure_session: z.object({ session_id: z.string().min(1), revision: z.number().int() }),
  graph: z.object({
    session_id: z.string(),
    revision: z.number().int(),
    nodes: z.array(liveNodeSchema),
    edges: z.array(liveEdgeSchema),
    turns: z.array(turnSchema),
    latest_retrieval: layeredContextSchema.nullable().optional(),
  }),
  ingest_turn: z.object({
    session_id: z.string(),
    revision: z.number().int(),
    status: z.enum(["applied", "noop", "duplicate"]),
    changed_ids: z.array(z.string()),
  }),
  command: z.object({
    session_id: z.string(),
    revision: z.number().int(),
    status: z.string(),
    changed_ids: z.array(z.string()),
    message: z.string(),
  }),
  context: z.object({
    session_id: z.string(),
    revision: z.number().int(),
    text: z.string(),
    node_ids: z.array(z.string()),
  }),
  layered_context: layeredContextSchema,
};
export type LiveNode = z.infer<typeof liveNodeSchema>;
export type LiveEdge = z.infer<typeof liveEdgeSchema>;
export type LiveSession = z.infer<typeof sessionSchema>;
export type LiveHealth = z.infer<typeof schemas.health>;
export type LiveGraph = z.infer<typeof schemas.graph>;
export type LiveContext = z.infer<typeof schemas.context>;
export type IngestResult = z.infer<typeof schemas.ingest_turn>;
export type CommandOutcome = z.infer<typeof schemas.command>;
export type LayeredContext = z.infer<typeof layeredContextSchema>;

/**
 * Accept a layered retrieval only when it belongs to this session AND to the current
 * memory revision (both top-level and memory layer); otherwise it is stale.
 */
export function currentRetrieval(
  value: LayeredContext | null | undefined,
  sessionId: string,
  revision: number,
): LayeredContext | null {
  if (!value) return null;
  if (value.session_id !== sessionId || value.memory.session_id !== sessionId) return null;
  if (value.revision !== revision || value.memory.revision !== revision) return null;
  return value;
}

export type ErrorCode =
  | "network"
  | "malformed_response"
  | "invalid_url"
  | "aborted"
  | "invalid_input"
  | "unauthorized"
  | "session_not_found"
  | "conflicting_turn"
  | "revision_conflict"
  | "openai_not_configured"
  | "upstream_unavailable"
  | "http_error";

export class MemoryApiError extends Error {
  constructor(
    readonly code: ErrorCode | string,
    message: string,
    readonly status: number | null = null,
  ) {
    super(message);
    this.name = "MemoryApiError";
  }
}

/** Practical, credential-free guidance for each failure class. */
export function setupHint(error: unknown): string {
  if (!(error instanceof MemoryApiError)) return "Unexpected error. Check the service and retry.";
  switch (error.code) {
    case "network":
      return "Service unreachable. Check the URL, that the service is running, and that it allows this page's origin (CORS). Browsers block http services from https pages unless the host is 127.0.0.1/localhost.";
    case "invalid_url":
      return "Enter a full http(s) URL for the memory-api service.";
    case "insecure_transport":
      return "Tokens are only sent over HTTPS, or plain HTTP to this computer (127.0.0.1, localhost, ::1). Use an https URL for remote services.";
    case "request_in_flight":
      return "The service is still processing this exact request. Retry shortly; it will not be billed twice.";
    case "conflicting_request":
      return "That request ID was already used for a different command. Edit the command to start a new request.";
    case "unauthorized":
      return "The owner bridge token was rejected, expired or revoked. Issue a new token from the memory service and reconnect.";
    case "openai_not_configured":
      return "The service has no OpenAI key configured, so extraction and memory commands are unavailable. Graphs remain readable.";
    case "upstream_unavailable":
      return "The memory model did not respond in time. Retry shortly; nothing was applied.";
    case "malformed_response":
      return "The service replied with data that does not match memory-api v1. Check the service version.";
    case "session_not_found":
      return "That conversation no longer exists for this owner. Refresh the conversation list.";
    case "conflicting_turn":
      return "That turn ID was already ingested with different content. Use a new turn ID.";
    case "revision_conflict":
      return "The graph changed concurrently. Refresh and retry.";
    default:
      return error.message;
  }
}

type FetchLike = (input: string, init: RequestInit) => Promise<Response>;
export type LiveClientOptions = { baseUrl: string; token: string; fetch?: FetchLike };

export function normalizeBaseUrl(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new MemoryApiError("invalid_url", "Invalid service URL.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:")
    throw new MemoryApiError("invalid_url", "Service URL must use http or https.");
  if (url.username || url.password)
    throw new MemoryApiError("invalid_url", "Do not put credentials in the service URL.");
  return url.toString();
}

/** Local API uses "user", earlier drafts used "turn": both mean automatic capture. */
export function isAutomaticSource(sourceKind: string) {
  return sourceKind === "user" || sourceKind === "turn";
}

export function isLoopbackHost(hostname: string): boolean {
  const h = hostname.toLowerCase();
  return h === "localhost" || h === "[::1]" || h === "::1" || /^127(\.\d{1,3}){3}$/.test(h);
}

/**
 * Keeps one request_id per (session, exact command) until a definitive result, so an
 * uncertain failure (lost response, network error) retries idempotently instead of
 * re-running and re-billing the model. Changing the command or session starts fresh.
 */
export function createCommandRequestIds(make: () => string = newRequestId) {
  let pending: { sessionId: string; command: string; requestId: string } | null = null;
  return {
    idFor(sessionId: string, command: string) {
      if (!pending || pending.sessionId !== sessionId || pending.command !== command)
        pending = { sessionId, command, requestId: make() };
      return pending.requestId;
    },
    /** Call after applied/noop/duplicate (or a definitive rejection of this command). */
    settle(sessionId: string, command: string) {
      if (pending?.sessionId === sessionId && pending.command === command) pending = null;
    },
  };
}
/** Errors after which the same request may already be applied or still running. */
export function isUncertain(error: unknown) {
  return (
    !(error instanceof MemoryApiError) ||
    [
      "network",
      "aborted",
      "http_error",
      "malformed_response",
      "request_in_flight",
      "upstream_unavailable",
      "revision_conflict",
    ].includes(error.code)
  );
}

export function newRequestId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `req-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function expectSession<T extends { session_id: string }>(value: T, sessionId: string): T {
  if (value.session_id !== sessionId)
    throw new MemoryApiError("malformed_response", "Response belongs to another session.");
  return value;
}

export function createLiveClient(options: LiveClientOptions) {
  const url = normalizeBaseUrl(options.baseUrl);
  const parsedUrl = new URL(url);
  if (options.token && parsedUrl.protocol === "http:" && !isLoopbackHost(parsedUrl.hostname))
    throw new MemoryApiError(
      "insecure_transport",
      "Refusing to send a bearer token over plain HTTP to a non-loopback host.",
    );
  const doFetch: FetchLike = options.fetch ?? ((i, init) => fetch(i, init));
  async function call<K extends keyof typeof schemas>(
    action: K,
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<z.infer<(typeof schemas)[K]>> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (action !== "health") {
      if (!options.token) throw new MemoryApiError("unauthorized", "No bridge token.", 401);
      headers["Authorization"] = `Bearer ${options.token}`;
    }
    let response: Response;
    try {
      response = await doFetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify({ ...body, action }),
        ...(signal ? { signal } : {}),
        credentials: "omit",
        redirect: "error",
        cache: "no-store",
      });
    } catch (error) {
      if (signal?.aborted || (error instanceof DOMException && error.name === "AbortError"))
        throw new MemoryApiError("aborted", "Request cancelled.");
      throw new MemoryApiError("network", "Could not reach the memory service.");
    }
    let json: unknown;
    try {
      json = await response.json();
    } catch {
      throw new MemoryApiError(
        response.ok ? "malformed_response" : "http_error",
        response.ok ? "Response was not JSON." : `Service returned HTTP ${response.status}.`,
        response.status,
      );
    }
    if (!response.ok) {
      const parsed = z
        .object({ error: z.object({ code: z.string(), message: z.string() }) })
        .safeParse(json);
      if (parsed.success)
        throw new MemoryApiError(
          parsed.data.error.code,
          parsed.data.error.message.slice(0, 300),
          response.status,
        );
      throw new MemoryApiError(
        response.status === 401 ? "unauthorized" : "http_error",
        `Service returned HTTP ${response.status}.`,
        response.status,
      );
    }
    const parsed = schemas[action].safeParse(json);
    if (!parsed.success)
      throw new MemoryApiError(
        "malformed_response",
        `Malformed ${action} response.`,
        response.status,
      );
    return parsed.data as z.infer<(typeof schemas)[K]>;
  }
  return {
    health: (signal?: AbortSignal) => call("health", {}, signal),
    listSessions: (signal?: AbortSignal) => call("list_sessions", {}, signal),
    ensureSession: (
      input: { source: string; external_session_id: string; title?: string },
      signal?: AbortSignal,
    ) => call("ensure_session", input, signal),
    graph: async (sessionId: string, signal?: AbortSignal, otherSessionIds: string[] = []) => {
      const graph = expectSession(
        await call("graph", { session_id: sessionId }, signal),
        sessionId,
      );
      // Reject nodes tagged for, or ID-prefixed with, another known session before
      // deriving edges; then drop edges whose endpoints are not accepted nodes.
      const foreign = otherSessionIds.filter((id) => id && id !== sessionId);
      const nodes = graph.nodes.filter(
        (n) =>
          (n.session_id === undefined || n.session_id === sessionId) &&
          !foreign.some((f) => n.id.startsWith(`${f}:`)),
      );
      const ids = new Set(nodes.map((n) => n.id));
      return {
        ...graph,
        nodes,
        edges: graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
      };
    },
    ingestTurn: async (
      input: {
        session_id: string;
        turn_id: string;
        /** Only the user's prompt is sent; assistant output never feeds memory. */
        user_message: string;
        /** Revision the capture was based on; stale captures become durable noops. */
        base_revision?: number;
      },
      signal?: AbortSignal,
    ) => {
      if (
        input.base_revision !== undefined &&
        !(Number.isInteger(input.base_revision) && input.base_revision >= 0)
      )
        throw new MemoryApiError("invalid_input", "base_revision must be a nonnegative integer.");
      return expectSession(await call("ingest_turn", input, signal), input.session_id);
    },
    command: async (
      input: { session_id: string; command: string; request_id?: string },
      signal?: AbortSignal,
    ) =>
      expectSession(
        await call("command", { ...input, request_id: input.request_id ?? newRequestId() }, signal),
        input.session_id,
      ),
    layeredContext: async (
      input: { session_id: string; query?: string; limit?: number },
      signal?: AbortSignal,
    ) => {
      if (input.query !== undefined && input.query.length > 500)
        throw new MemoryApiError("invalid_input", "query must be at most 500 characters.");
      const limit = Math.min(Math.max(Math.trunc(input.limit ?? 20), 1), 20);
      const result = await call("layered_context", { ...input, limit }, signal);
      if (result.session_id !== input.session_id || result.memory.session_id !== input.session_id)
        throw new MemoryApiError("malformed_response", "Response was for a different session.");
      return result;
    },
    context: async (sessionId: string, signal?: AbortSignal) =>
      expectSession(await call("context", { session_id: sessionId, limit: 20 }, signal), sessionId),
  };
}
export type LiveClient = ReturnType<typeof createLiveClient>;

/** Monotonic gate: only the most recent ticket may apply results. */
export function createRequestGate() {
  let current = 0;
  return {
    next: () => ++current,
    isCurrent: (ticket: number) => ticket === current,
    invalidate: () => {
      current++;
    },
  };
}

export const liveStorage = {
  urlKey: "memory-lens:live:url",
  tokenKey: "memory-lens:live:token", // sessionStorage only
};

/** Deterministic grid placement for nodes the service returned without a position. */
export function layoutNodes(
  nodes: LiveNode[],
): (LiveNode & { position: { x: number; y: number } })[] {
  return nodes.map((node, index) => ({
    ...node,
    position: node.position ?? { x: 80 + (index % 3) * 310, y: 80 + Math.floor(index / 3) * 170 },
  }));
}

