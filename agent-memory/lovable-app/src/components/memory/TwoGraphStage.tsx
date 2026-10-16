import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import {
  Braces,
  Check,
  Code2,
  FileCode2,
  FlaskConical,
  Plug,
  Settings2,
  Unplug,
} from "lucide-react";
import type {
  AdapterStatus,
  CodeNode as CodeNodeData,
  ExistingGraph,
} from "@/lib/codegraph/existing";
import { stageLabels, stageOrder, type Trace } from "@/lib/codegraph/handoff";

export function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!mq) return;
    setReduced(mq.matches);
    const on = () => setReduced(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return reduced;
}

type CodeFlowNode = Node<{ node: CodeNodeData; hit: boolean }, "code">;
const kindIcon = {
  module: FileCode2,
  function: Code2,
  type: Braces,
  test: FlaskConical,
  config: Settings2,
};
function CodeNodeCard({ data }: NodeProps<CodeFlowNode>) {
  const Icon = kindIcon[data.node.kind];
  return (
    <div className={`code-node ${data.hit ? "is-hit" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div className="code-kind">
        <Icon size={12} />
        {data.node.kind}
      </div>
      <div className="code-label">{data.node.label}</div>
      <div className="code-path">{data.node.path}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
const codeNodeTypes = { code: CodeNodeCard };

function stageState(trace: Trace | null, stage: (typeof stageOrder)[number]) {
  if (!trace || trace.stage === "idle") return "pending";
  const order = ["recall", "search", "prepare", "done"];
  const at = order.indexOf(trace.stage);
  const me = order.indexOf(stage);
  if (trace.stoppedAt === stage) return "stopped";
  if (me < at || trace.stage === "done") return "complete";
  return me === at ? "current" : "pending";
}

export function StageBar({ trace }: { trace: Trace | null }) {
  return (
    <ol className="stage-bar" aria-label="Retrieval handoff stages">
      {stageOrder.map((stage, i) => {
        const s = stageState(trace, stage);
        return (
          <li
            key={stage}
            className={`stage stage-${s}`}
            aria-current={s === "current" ? "step" : undefined}
          >
            <span className="stage-num">{s === "complete" ? <Check size={11} /> : i + 1}</span>
            <span>{stageLabels[stage]}</span>
            <span className="sr-only">{s === "stopped" ? "stopped: connector missing" : s}</span>
          </li>
        );
      })}
    </ol>
  );
}

export function TwoGraphStage(props: {
  memoryCanvas: ReactNode;
  memoryScope: string;
  trace: Trace | null;
  codeGraph: ExistingGraph | null;
  codeStatus: AdapterStatus;
  controls?: ReactNode;
  reducedMotion: boolean;
  staleCapsule?: boolean | undefined;
}) {
  const { trace, codeGraph, codeStatus, reducedMotion } = props;
  const hits = useMemo(() => new Set(trace?.retrieval?.hits.map((h) => h.nodeId) ?? []), [trace]);
  const pathEdges = useMemo(() => new Set(trace?.retrieval?.pathEdgeIds ?? []), [trace]);
  const showHits =
    trace && (trace.stage === "search" || trace.stage === "prepare" || trace.stage === "done");
  const nodes: CodeFlowNode[] = useMemo(
    () =>
      (codeGraph?.nodes ?? []).map((n) => ({
        id: n.id,
        type: "code",
        position: n.position,
        data: { node: n, hit: !!showHits && hits.has(n.id) },
        ariaLabel: `${n.kind}: ${n.label}`,
        draggable: false,
      })),
    [codeGraph, hits, showHits],
  );
  const edges = useMemo(
    () =>
      (codeGraph?.edges ?? []).map((e) => ({
        ...e,
        type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed },
        labelBgPadding: [6, 3] as [number, number],
        className: showHits && pathEdges.has(e.id) ? "code-edge-hit" : "code-edge",
        animated: !reducedMotion && !!showHits && pathEdges.has(e.id),
      })),
    [codeGraph, pathEdges, showHits, reducedMotion],
  );
  const crossing = trace?.stage === "search";
  const capsuleVisible =
    trace && trace.capsule && (trace.stage === "recall" || trace.stage === "search");
  return (
    <div className="two-graph">
      <div className="two-graph-top">
        <StageBar trace={trace} />
        {props.controls}
      </div>
      <div className="two-graph-canvases">
        <section className="graph-pane pane-memory" aria-label="Conversation memory graph">
          <header className="pane-head">
            <span className="pane-layer">Layer 1</span>
            <strong>Conversation memory</strong>
            <span className="pane-scope">{props.memoryScope}</span>
          </header>
          <div className="pane-body">{props.memoryCanvas}</div>
        </section>
        <div className="handoff-gutter" aria-hidden={!capsuleVisible}>
          {capsuleVisible && (
            <div
              data-testid="handoff-capsule"
              className={`capsule ${crossing ? "is-crossing" : ""} ${reducedMotion ? "is-static" : ""} ${trace?.stoppedAt ? "is-stopped" : ""}`}
              role="status"
            >
              <span>Context capsule</span>
              <small>
                {trace!.capsule!.nodeIds.length} memories · {trace!.capsule!.revision}
              </small>
            </div>
          )}
        </div>
        <section className="graph-pane pane-code" aria-label="Existing code graph">
          <header className="pane-head">
            <span className="pane-layer">Layer 2</span>
            <strong>Existing code graph</strong>
            <span className={`pane-scope ${codeStatus.state === "not_connected" ? "is-off" : ""}`}>
              {codeStatus.state === "not_connected" ? <Unplug size={11} /> : <Plug size={11} />}
              {codeGraph ? `${codeGraph.repositoryId} · ` : ""}
              {codeStatus.label}
            </span>
          </header>
          <div className="pane-body code-canvas">
            {codeGraph ? (
              <ReactFlowProvider>
                <ReactFlow<CodeFlowNode>
                  nodes={nodes}
                  edges={edges}
                  nodeTypes={codeNodeTypes}
                  nodesConnectable={false}
                  fitView
                  fitViewOptions={{ padding: 0.18, maxZoom: 1 }}
                  minZoom={0.3}
                  maxZoom={1.4}
                >
                  <Background gap={23} size={1} />
                  <Controls
                    showInteractive={false}
                    position="bottom-right"
                    orientation="horizontal"
                  />
                </ReactFlow>
              </ReactFlowProvider>
            ) : (
              <div className="empty-graph" data-testid="code-not-connected">
                <Unplug size={30} strokeWidth={1.2} />
                <h2>Existing graph not connected</h2>
                <p>
                  Supply your existing code graph connector to search it. Nothing is generated,
                  indexed or filled with sample results here.
                </p>
              </div>
            )}
          </div>
        </section>
      </div>
      <ContextPanel trace={trace} stale={props.staleCapsule} />
    </div>
  );
}

function ContextPanel({ trace, stale }: { trace: Trace | null; stale?: boolean | undefined }) {
  if (!trace) return null;
  return (
    <section className="context-reveal" aria-label="Prepared agent context" aria-live="polite">
      <div className="context-meta">
        <span className="turn-role">prompt</span>
        <span className="mono">{trace.prompt}</span>
        {trace.capsule && <span>Memory revision {trace.capsule.revision}</span>}
        {stale && <span className="stale-note">Memory changed since this capsule</span>}
      </div>
      {trace.stoppedAt ? (
        <p className="context-stop">
          Trace stopped at Search existing graph: the existing graph connector is missing. No code
          results were produced.
        </p>
      ) : trace.preparedContext ? (
        <>
          <div className="context-label">
            Context prepared · sample next-turn context (not delivered to an agent)
          </div>
          <pre className="live-context">{trace.preparedContext}</pre>
        </>
      ) : (
        <p className="context-stop">
          {trace.stage === "idle" ? "Captured prompt queued." : "Preparing…"}
        </p>
      )}
    </section>
  );
}

export function ViewToggle({
  view,
  onChange,
}: {
  view: "dual" | "memory";
  onChange: (v: "dual" | "memory") => void;
}) {
  return (
    <div className="view-toggle" role="radiogroup" aria-label="Graph view">
      {(["memory", "dual"] as const).map((v) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={view === v}
          className={view === v ? "active" : ""}
          onClick={() => onChange(v)}
        >
          {v === "dual" ? "How it works (demo)" : "Memory"}
        </button>
      ))}
    </div>
  );
}

