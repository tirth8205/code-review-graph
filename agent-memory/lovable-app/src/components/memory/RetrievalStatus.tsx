import { Check, CircleDashed, CircleSlash } from "lucide-react";
import { SheetDescription, SheetTitle } from "@/components/ui/sheet";
import type { LayeredContext } from "@/lib/memory/live";

const codeLabels: Record<string, string> = {
  ready: "Code graph ready",
  not_ready: "Code graph not ready",
  unavailable: "Code graph unavailable",
  not_configured: "Existing graph not connected",
};

/** Compact stage indicator. Never renders code graph nodes; never claims delivery. */
export function RetrievalStatus({
  retrieval,
  revision,
  codeGraph,
}: {
  retrieval: LayeredContext | null;
  revision: number | null;
  codeGraph?: { configured: boolean; readiness: string } | undefined;
}) {
  if (!retrieval) {
    return (
      <div className="retrieval-status" role="status" data-testid="retrieval-status">
        <CircleDashed size={13} />
        <span>
          No retrieval for the current memory
          {revision !== null ? ` (revision ${revision})` : ""}.
        </span>
        <span className="retrieval-muted">
          Existing code graph:{" "}
          {codeGraph
            ? codeGraph.configured
              ? codeGraph.readiness
              : "not connected"
            : "not reported by service"}
        </span>
      </div>
    );
  }
  const code = retrieval.code_graph.status;
  const agent = retrieval.stages.find((s) => s.stage === "agent_context")?.status ?? "partial";
  return (
    <div className="retrieval-status" role="status" data-testid="retrieval-status">
      <span className="rs-stage rs-ok">
        <Check size={12} />1 Memory ready · r{retrieval.memory.revision} ·{" "}
        {retrieval.memory.node_ids.length} nodes
      </span>
      <span className={`rs-stage ${code === "ready" ? "rs-ok" : "rs-off"}`}>
        {code === "ready" ? <Check size={12} /> : <CircleSlash size={12} />}2 {codeLabels[code]}
      </span>
      <span className={`rs-stage ${agent === "ready" ? "rs-ok" : "rs-partial"}`}>
        3 Context prepared{agent === "ready" ? "" : " (partial)"}
      </span>
    </div>
  );
}

export function LayeredContextView({
  title,
  value,
  error,
  stale = false,
  currentRevision = null,
}: {
  title: string;
  value: LayeredContext | null;
  error: string | null;
  stale?: boolean;
  currentRevision?: number | null;
}) {
  return (
    <>
      <div className="drawer-eyebrow">Next-turn context preview · {title}</div>
      <SheetTitle className="drawer-title">Memory first, then existing code graph</SheetTitle>
      <SheetDescription className="drawer-description">
        What the service would prepare for this conversation’s next prompt. Memory and code
        references stay separate. This preview does not confirm a tool received it.
      </SheetDescription>
      {error && <p className="live-error">{error}</p>}
      {stale && !error && (
        <p className="live-error" data-testid="layered-stale">
          This preview is out of date: memory changed
          {currentRevision !== null ? ` (now revision ${currentRevision})` : ""}. It is no longer
          the next-turn context. Press “Preview next-turn context” again to refresh it.
        </p>
      )}
      {!value && !error && !stale && <p className="drawer-description">Loading…</p>}
      {value && (
        <>
          <div className="section-caption">1 · Conversation memory</div>
          <div className="live-meta">
            Revision {value.memory.revision} · {value.memory.node_ids.length} nodes
          </div>
          <pre className="live-context" data-testid="layered-memory">
            {value.memory.text || "(no active memories)"}
          </pre>
          <div className="section-caption">2 · Existing code graph reference</div>
          <div className="live-meta">
            {codeLabels[value.code_graph.status]}
            {value.code_graph.repo_id ? ` · ${value.code_graph.repo_id}` : ""}
            {value.code_graph.reason ? ` · ${value.code_graph.reason}` : ""}
          </div>
          {value.code_graph.status === "ready" ? (
            <pre className="live-context" data-testid="layered-code">
              {value.code_graph.text || "(no matching code references)"}
            </pre>
          ) : (
            <p className="drawer-description">No code references were retrieved.</p>
          )}
          <div className="section-caption">3 · Agent context</div>
          <div className="live-meta">
            {value.stages.find((s) => s.stage === "agent_context")?.status === "ready"
              ? "Prepared"
              : "Partial: memory only"}{" "}
            · retrieved {value.retrieved_at}
          </div>
        </>
      )}
    </>
  );
}

