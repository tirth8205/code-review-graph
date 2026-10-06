/**
 * Layer 2: the user's EXISTING codebase knowledge graph.
 *
 * This module never builds, indexes or rewrites a code graph. It only defines the
 * boundary through which Memory Lens reads a graph someone else already generated,
 * plus a clearly labeled deterministic SAMPLE adapter for demo replay. Code graph data
 * is never stored alongside conversation memories and no permanent cross-graph edge
 * exists; memory reaches this layer only as a bounded retrieval context.
 */

export type GraphScope = {
  /** Local installation is single-owner; a real multi-user identity is not configured. */
  ownerId: string;
  workspaceId: string;
  repositoryId: string;
  conversationId: string;
};
export type CodeNodeKind = "module" | "function" | "type" | "test" | "config";
export type CodeNode = {
  id: string;
  kind: CodeNodeKind;
  label: string;
  path: string;
  summary: string;
  position: { x: number; y: number };
};
export type CodeEdge = { id: string; source: string; target: string; label: string };
export type ExistingGraph = {
  repositoryId: string;
  nodes: CodeNode[];
  edges: CodeEdge[];
};
/** Bounded retrieval context handed from Layer 1 (memory) to Layer 2 (code graph). */
export type MemoryCapsule = {
  conversationId: string;
  revision: string;
  nodeIds: string[];
  text: string;
};
export type RetrievalRequest = { prompt: string; capsule: MemoryCapsule; scope: GraphScope };
export type RetrievalHit = { nodeId: string; reason: string; evidence: string };
export type RetrievalResult = {
  repositoryId: string;
  hits: RetrievalHit[];
  pathEdgeIds: string[];
  /** Revision of the memory capsule this result was computed against. */
  capsuleRevision: string;
};
export type AdapterStatus =
  { state: "sample"; label: string } | { state: "not_connected"; label: string };

export interface ExistingGraphAdapter {
  readonly kind: "sample" | "live";
  status(): AdapterStatus;
  load(scope: GraphScope): ExistingGraph | null;
  retrieve(request: RetrievalRequest): RetrievalResult | null;
}

export const MAX_CAPSULE_NODES = 20;
export const MAX_CAPSULE_CHARS = 12_000;

export const SAMPLE_REPOSITORY = "sample/billing-service";

const sampleNodes: CodeNode[] = [
  {
    id: "code:refunds-module",
    kind: "module",
    label: "billing/refunds.ts",
    path: "src/billing/refunds.ts",
    summary: "Refund request handling for Stripe charges.",
    position: { x: 40, y: 40 },
  },
  {
    id: "code:validate-refund",
    kind: "function",
    label: "validateRefundWindow()",
    path: "src/billing/refunds.ts#validateRefundWindow",
    summary: "Rejects refunds older than REFUND_WINDOW_DAYS.",
    position: { x: 300, y: 150 },
  },
  {
    id: "code:policy-config",
    kind: "config",
    label: "REFUND_WINDOW_DAYS",
    path: "src/billing/policy.ts#REFUND_WINDOW_DAYS",
    summary: "Refund policy constant read by validation.",
    position: { x: 560, y: 40 },
  },
  {
    id: "code:stripe-client",
    kind: "module",
    label: "billing/stripe.ts",
    path: "src/billing/stripe.ts",
    summary: "Stripe SDK wrapper used to issue refunds.",
    position: { x: 560, y: 260 },
  },
  {
    id: "code:refund-test",
    kind: "test",
    label: "refunds.test.ts",
    path: "src/billing/refunds.test.ts",
    summary: "Window boundary tests for validateRefundWindow.",
    position: { x: 40, y: 270 },
  },
  {
    id: "code:invoice-type",
    kind: "type",
    label: "Invoice",
    path: "src/billing/types.ts#Invoice",
    summary: "Invoice shape with paidAt timestamp.",
    position: { x: 300, y: 380 },
  },
  {
    id: "code:deploy-config",
    kind: "config",
    label: "wrangler.toml",
    path: "wrangler.toml",
    summary: "Workers deployment configuration and compatibility date.",
    position: { x: 40, y: 470 },
  },
  {
    id: "code:health-check",
    kind: "function",
    label: "healthCheck()",
    path: "src/deploy/health.ts#healthCheck",
    summary: "Production readiness probe called after deploy.",
    position: { x: 300, y: 560 },
  },
];
const sampleEdges: CodeEdge[] = [
  { id: "ce:1", source: "code:refunds-module", target: "code:validate-refund", label: "defines" },
  { id: "ce:2", source: "code:validate-refund", target: "code:policy-config", label: "reads" },
  { id: "ce:3", source: "code:validate-refund", target: "code:invoice-type", label: "uses type" },
  { id: "ce:4", source: "code:refunds-module", target: "code:stripe-client", label: "calls" },
  { id: "ce:5", source: "code:refund-test", target: "code:validate-refund", label: "tests" },
  { id: "ce:6", source: "code:health-check", target: "code:deploy-config", label: "reads" },
];

/** Deterministic keyword routes; this is a SAMPLE, not semantic retrieval. */
const sampleRoutes: { terms: string[]; nodes: string[]; edges: string[]; reason: string }[] = [
  {
    terms: ["refund"],
    nodes: ["code:validate-refund", "code:policy-config", "code:invoice-type", "code:refund-test"],
    edges: ["ce:2", "ce:3", "ce:5"],
    reason: "Refund memory matched refund validation and its dependencies",
  },
  {
    terms: ["deploy", "workers", "node"],
    nodes: ["code:health-check", "code:deploy-config"],
    edges: ["ce:6"],
    reason: "Deployment memory matched the deploy health check",
  },
];

export function boundCapsule(capsule: MemoryCapsule): MemoryCapsule {
  return {
    ...capsule,
    nodeIds: capsule.nodeIds.slice(0, MAX_CAPSULE_NODES),
    text: capsule.text.slice(0, MAX_CAPSULE_CHARS),
  };
}

export function createSampleExistingGraphAdapter(): ExistingGraphAdapter {
  const graph: ExistingGraph = {
    repositoryId: SAMPLE_REPOSITORY,
    nodes: sampleNodes,
    edges: sampleEdges,
  };
  return {
    kind: "sample",
    status: () => ({ state: "sample", label: "Sample existing graph · demo replay" }),
    load: (scope) => (scope.repositoryId === SAMPLE_REPOSITORY ? graph : null),
    retrieve({ prompt, capsule, scope }) {
      if (scope.repositoryId !== SAMPLE_REPOSITORY) return null;
      const bounded = boundCapsule(capsule);
      const haystack = `${prompt}\n${bounded.text}`.toLowerCase();
      const hits: RetrievalHit[] = [];
      const pathEdgeIds: string[] = [];
      for (const route of sampleRoutes) {
        if (!route.terms.some((t) => haystack.includes(t))) continue;
        for (const id of route.nodes) {
          const node = sampleNodes.find((n) => n.id === id)!;
          if (!hits.some((h) => h.nodeId === id))
            hits.push({
              nodeId: id,
              reason: route.reason,
              evidence: `${node.path}: ${node.summary}`,
            });
        }
        pathEdgeIds.push(...route.edges);
      }
      return {
        repositoryId: SAMPLE_REPOSITORY,
        hits,
        pathEdgeIds,
        capsuleRevision: bounded.revision,
      };
    },
  };
}

/**
 * Live connector placeholder. The real existing-graph source has not been supplied,
 * so this never loads data and never returns retrieval results.
 */
export function createUnconnectedExistingGraphAdapter(): ExistingGraphAdapter {
  return {
    kind: "live",
    status: () => ({ state: "not_connected", label: "Existing graph not connected" }),
    load: () => null,
    retrieve: () => null,
  };
}

