import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  MarkerType,
  type NodeChange,
} from "@xyflow/react";
import {
  Activity,
  ArrowUp,
  ArrowUpRight,
  BookOpen,
  ChevronRight,
  CircleHelp,
  Command,
  FileCode2,
  FolderGit2,
  GitBranch,
  History,
  Layers3,
  LoaderCircle,
  Network,
  PanelRight,
  Play,
  Plus,
  RotateCcw,
  Search,
  Square,
  Terminal,
  Unplug,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import {
  demoAdapter,
  demoCommands,
  initialState,
  replayTurns,
  restoreState,
  storageKey,
  legacyStorageKeys,
  sourceLabel,
  DEMO_WORKSPACE,
  connect,
  type GraphState,
} from "@/lib/memory/graph";
import { MemoryNode, type MemoryFlowNode } from "./MemoryNode";
import { MemoryInspector, displayTime } from "./MemoryInspector";
import { MemoryDrawers } from "./MemoryDrawers";
import { LiveWorkspace } from "./LiveWorkspace";
import { TwoGraphStage, usePrefersReducedMotion, ViewToggle } from "./TwoGraphStage";
import {
  createSampleExistingGraphAdapter,
  SAMPLE_REPOSITORY,
  type GraphScope,
} from "@/lib/codegraph/existing";
import {
  demoScript,
  memoryRevision,
  newRun,
  stepDemo,
  type DemoRun,
} from "@/lib/codegraph/handoff";
import { Pause, SkipForward } from "lucide-react";
const sampleCodeAdapter = createSampleExistingGraphAdapter();
// One shared memory graph for the local workspace; native chats are provenance only.
const demoScope: GraphScope = {
  ownerId: "local-owner",
  workspaceId: "demo-workspace",
  repositoryId: SAMPLE_REPOSITORY,
  conversationId: DEMO_WORKSPACE,
};
const DEMO_STEP_MS = 5200;
const nodeTypes = { memory: MemoryNode };
type Feedback = { message: string; error: boolean };
export function MemoryWorkspace({ modeSwitch }: { modeSwitch?: ReactNode }) {
  const [state, setState] = useState<GraphState>(() => initialState(DEMO_WORKSPACE));
  const [ready, setReady] = useState(false);
  const [inspector, setInspector] = useState(true);
  const [feed, setFeed] = useState<"turns" | "activity" | null>(null);
  const [drawer, setDrawer] = useState<"context" | "connect" | null>(null);
  const [query, setQuery] = useState("");
  const [input, setInput] = useState("");
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [persistError, setPersistError] = useState(false);
  const [view, setView] = useState<"dual" | "memory">("memory");
  const [run, setRun] = useState<DemoRun>(() => newRun(DEMO_WORKSPACE));
  const reducedMotion = usePrefersReducedMotion();
  const codeGraph = useMemo(() => sampleCodeAdapter.load(demoScope), []);
  const [measurements, setMeasurements] = useState<
    Record<string, { width: number; height: number }>
  >({});
  const commandTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const runningRef = useRef(false);
  const replayTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [flow, setFlow] = useState<
    import("@xyflow/react").ReactFlowInstance<MemoryFlowNode> | null
  >(null);
  const recalledIds = useMemo(
    () =>
      new Set(
        view === "dual" && run.trace && run.trace.stage !== "idle"
          ? (run.trace.capsule?.nodeIds ?? [])
          : [],
      ),
    [run, view],
  );
  const graph = useMemo(() => demoAdapter.getGraph(state), [state]);
  const nodes: MemoryFlowNode[] = useMemo(
    () =>
      graph.memories.map((memory) => ({
        id: memory.id,
        type: "memory",
        position: memory.position,
        measured: measurements[memory.id] ?? {},
        data: {
          memory,
          changed: state.changedIds.includes(memory.id),
          recalled: recalledIds.has(memory.id),
        },
        selected: state.selectedId === memory.id,
        dimmed: !!query && !memory.content.toLowerCase().includes(query.toLowerCase()),
        className:
          query && !memory.content.toLowerCase().includes(query.toLowerCase()) ? "opacity-30" : "",
        ariaLabel: `${memory.type}: ${memory.content}`,
      })),
    [graph.memories, state.changedIds, state.selectedId, query, measurements, recalledIds],
  );
  const edges = useMemo(
    () =>
      graph.edges.map((edge) => ({
        ...edge,
        label: edge.label,
        type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed },
        labelBgPadding: [7, 4] as [number, number],
        labelBgBorderRadius: 3,
        className:
          state.changedIds.includes(edge.source) || state.changedIds.includes(edge.target)
            ? "edge-changed"
            : "",
      })),
    [graph.edges, state.changedIds],
  );
  const selected = state.memories.find((m) => m.id === state.selectedId);
  const matches = query
    ? graph.memories.filter((m) => m.content.toLowerCase().includes(query.toLowerCase()))
    : [];
  const sources = useMemo(
    () => [...new Set(state.turns.map((t) => sourceLabel(t)))],
    [state.turns],
  );
  const update = (apply: (s: GraphState) => GraphState) => setState(apply);
  useEffect(() => {
    try {
      for (const key of legacyStorageKeys) localStorage.removeItem(key);
      setState(restoreState(DEMO_WORKSPACE, localStorage.getItem(storageKey(DEMO_WORKSPACE))));
    } catch {
      setPersistError(true);
    }
    setReady(true);
    if (window.innerWidth < 1100) setInspector(false);
    return () => {
      if (commandTimer.current) clearTimeout(commandTimer.current);
      if (replayTimer.current) clearTimeout(replayTimer.current);
    };
  }, []);
  useEffect(() => {
    if (!ready) return;
    try {
      localStorage.setItem(storageKey(DEMO_WORKSPACE), JSON.stringify(state));
    } catch {
      setPersistError(true);
    }
  }, [state, ready]);
  useEffect(() => {
    if (!running) return;
    if (state.replayIndex >= replayTurns.length) {
      runningRef.current = false;
      setRunning(false);
      return;
    }
    replayTimer.current = setTimeout(() => {
      if (runningRef.current) update((s) => demoAdapter.ingestTurn(s));
    }, 1100);
    return () => {
      if (replayTimer.current) clearTimeout(replayTimer.current);
    };
  }, [running, state]);
  useEffect(() => {
    if (flow) {
      const timer = setTimeout(
        () =>
          flow.fitView({
            padding: 0.23,
            duration: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 300,
            maxZoom: 1,
          }),
        80,
      );
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [flow]);
  // Demo trace: one step at a time, always against the current shared memory.
  function stepRun(keepPlaying: boolean) {
    const result = stepDemo(run, state, sampleCodeAdapter, demoScope);
    const applied = demoScript[run.step];
    if (applied?.kind === "edit")
      setFeedback({
        message: `Demo replay · ${applied.command} · previous version kept in history`,
        error: false,
      });
    if (result.memory !== state) update(() => result.memory);
    setRun({ ...result.run, playing: keepPlaying && !result.done });
  }
  useEffect(() => {
    if (!run.playing) return;
    const timer = setTimeout(() => stepRun(true), run.step === 0 ? 300 : DEMO_STEP_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run]);
  function playDemo() {
    if (runningRef.current) stopReplay();
    setView("dual");
    const fresh = run.step >= demoScript.length;
    setRun((r) => ({ ...(fresh ? newRun(DEMO_WORKSPACE) : r), playing: true }));
  }
  function pauseDemo() {
    setRun((r) => ({ ...r, playing: false }));
  }
  function replayDemo() {
    setRun(newRun(DEMO_WORKSPACE));
  }
  function choose(id: string) {
    update((s) => ({ ...s, selectedId: id }));
    setInspector(true);
  }
  function stopReplay() {
    runningRef.current = false;
    setRunning(false);
    if (replayTimer.current) clearTimeout(replayTimer.current);
  }
  function play() {
    if (running) {
      stopReplay();
      return;
    }
    runningRef.current = true;
    setRunning(true);
    setFeed("turns");
  }
  function submit(text = input) {
    if (!text.trim() || busy) return;
    setBusy(true);
    setInput("");
    setFeedback({ message: "Demo interpretation · applying to your memory graph", error: false });
    commandTimer.current = setTimeout(() => {
      setState((previous) => {
        const result = demoAdapter.memoryCommand(previous, text);
        setFeedback({ message: result.message, error: !result.supported });
        return result.state;
      });
      commandTimer.current = null;
      setBusy(false);
    }, 400);
  }
  function reset(empty: boolean) {
    if (runningRef.current) stopReplay();
    if (commandTimer.current) {
      clearTimeout(commandTimer.current);
      commandTimer.current = null;
      setBusy(false);
    }
    setState(initialState(DEMO_WORKSPACE, empty));
    setRun(newRun(DEMO_WORKSPACE));
    setFeedback(null);
    setQuery("");
    setInput("");
    setConfirmReset(false);
  }
  function onNodesChange(changes: NodeChange<MemoryFlowNode>[]) {
    const dimensions = changes.filter((change) => change.type === "dimensions");
    if (dimensions.length) {
      setMeasurements((previous) => {
        const next = { ...previous };
        for (const change of dimensions) {
          if (change.dimensions) next[change.id] = change.dimensions;
        }
        return next;
      });
    }
    const moves = changes.filter((change) => change.type === "position");
    if (!moves.length) return;
    update((s) => ({
      ...s,
      memories: s.memories.map((memory) => {
        const move = moves.find((change) => change.id === memory.id);
        return move?.position ? { ...memory, position: move.position } : memory;
      }),
    }));
  }
  const completed = state.replayIndex >= replayTurns.length;
  const memoryStage = (
    <div className="graph-stage">
      <div className="canvas-top">
        <div className="search-box">
          <Search size={13} />
          <input
            aria-label="Search memories"
            value={query}
            placeholder="Search your memories…"
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <Button
              variant="ghost"
              size="icon"
              className="h-5 w-5"
              aria-label="Clear search"
              onClick={() => setQuery("")}
            >
              <X />
            </Button>
          )}
        </div>
        <div className="canvas-status">
          <span className="status-dot" />
          {persistError ? "Storage unavailable" : "Saved locally"}
          <Button
            variant="ghost"
            size="icon"
            title="Toggle inspector"
            aria-label="Toggle inspector"
            aria-pressed={inspector}
            onClick={() => setInspector(!inspector)}
          >
            <PanelRight />
          </Button>
        </div>
        {query && (
          <div className="search-results">
            {matches.length ? (
              matches.map((m) => (
                <Button
                  variant="ghost"
                  className="search-result"
                  key={m.id}
                  onClick={() => {
                    choose(m.id);
                    setQuery("");
                    flow?.setCenter(m.position.x + 116, m.position.y + 60, {
                      zoom: 1,
                      duration: 250,
                    });
                  }}
                >
                  {m.content}
                </Button>
              ))
            ) : (
              <div className="search-empty">No matching active memories in your graph.</div>
            )}
          </div>
        )}
      </div>
      <ReactFlow<MemoryFlowNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onInit={setFlow}
        onNodesChange={onNodesChange}
        onNodeClick={(_, node) => choose(node.id)}
        onConnect={(connection) => {
          if (connection.source && connection.target)
            update((s) => connect(s, connection.source, connection.target, "related to"));
        }}
        fitView
        fitViewOptions={{ padding: 0.23, maxZoom: 1 }}
        minZoom={0.3}
        maxZoom={1.6}
        defaultEdgeOptions={{ type: "smoothstep" }}
        proOptions={{ hideAttribution: false }}
      >
        <Background gap={23} size={1} />
        <Controls showInteractive={false} position="bottom-right" orientation="horizontal" />
        <MiniMap pannable zoomable ariaLabel="Memory graph minimap" />
      </ReactFlow>
      {!graph.memories.length && (
        <div className="empty-graph">
          <Network size={35} strokeWidth={1.2} />
          <h2>No memories yet</h2>
          <p>Your memory graph is empty. Add a memory below or reset to the sample graph.</p>
        </div>
      )}
      {(running || completed || state.replayIndex > 0) && (
        <div className="replay-strip">
          {running ? <LoaderCircle size={12} className="replay-pulse" /> : <History size={12} />}
          <span>
            {running
              ? "Capturing the next prompt…"
              : completed
                ? "Replay complete"
                : "Replay paused"}{" "}
            · {state.replayIndex}/{replayTurns.length}
          </span>
          {!running && !completed && (
            <Button variant="ghost" size="sm" onClick={play}>
              <Play />
              Resume
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon"
            aria-label="Reset memory graph"
            title="Reset memory graph"
            onClick={() => setConfirmReset(true)}
          >
            <RotateCcw />
          </Button>
        </div>
      )}
      <div className="canvas-bottom">
        {(["entity", "fact", "decision", "constraint", "task"] as const).map((type) => (
          <span className="legend-item" key={type}>
            <span className={`legend-dot legend-${type}`} />
            {type.charAt(0).toUpperCase() + type.slice(1)}
          </span>
        ))}
      </div>
    </div>
  );
  return (
    <div className="workspace">
      <aside className="session-rail" aria-label="Memory graph navigation">
        <div className="brand">
          <Network size={24} strokeWidth={1.5} className="lens-logo" />
          <div>
            <div className="brand-name">code-review-graph</div>
            <div className="brand-sub">Agent memory</div>
          </div>
        </div>
        {modeSwitch}
        <div className="rail-section">
          <span className="rail-label">Your memory graph</span>
          <FolderGit2 size={12} className="text-muted-foreground" />
        </div>
        <div className="session-list">
          <div className="session-button active" aria-current="true">
            <Network size={16} />
            <div className="min-w-0">
              <div className="session-title">Agent memory</div>
              <div className="session-sub">
                {graph.memories.length} memories · {sources.length} sources
              </div>
            </div>
            <span className="session-marker" />
          </div>
        </div>
        <div className="rail-section">
          <span className="rail-label">Captured from</span>
        </div>
        <ul className="source-list" aria-label="Capture sources">
          {sources.map((label) => (
            <li key={label}>
              <FileCode2 size={12} />
              <span className="truncate">{label}</span>
            </li>
          ))}
        </ul>
        <div className="rail-rule" />
        <nav className="rail-menu">
          <Button variant="ghost" className="rail-action" onClick={() => setDrawer("context")}>
            <Layers3 />
            Memory context<span>{graph.memories.length}</span>
          </Button>
          <Button
            variant="ghost"
            className="rail-action"
            onClick={() => setFeed(feed === "activity" ? null : "activity")}
          >
            <Activity />
            Activity<span>{state.activity.length}</span>
          </Button>
          <Button
            variant="ghost"
            className="rail-action"
            onClick={() => setFeed(feed === "turns" ? null : "turns")}
          >
            <Terminal />
            Captured prompts<span>{state.turns.length}</span>
          </Button>
        </nav>
        <div className="rail-bottom">
          <div className="connection-box">
            <div className="connection-title">
              <Unplug size={12} className="text-muted-foreground" />
              Local service not connected
            </div>
            <p>
              Demo replay uses sample prompts.
              <br />
              Your coding chats stay in your tools.
            </p>
            <Button
              variant="outline"
              className="connect-button"
              onClick={() => setDrawer("connect")}
            >
              <Plus />
              Connect source
              <ArrowUpRight className="ml-auto" />
            </Button>
          </div>
          <div className="rail-footer">
            <div className="avatar">ML</div>
            <div>
              <strong>Hackathon workspace</strong>
              <small>GPT-6 Astra · London</small>
            </div>
          </div>
        </div>
      </aside>
      <main className="main-workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <span>Workspace</span>
            <ChevronRight size={11} />
            <strong className="truncate">Agent memory</strong>
            <ChevronRight size={11} />
            <span className="hidden sm:inline">Memory graph</span>
            <span className="sm:hidden">Demo replay</span>
          </div>
          <div className="topbar-actions">
            <span className="demo-badge">
              <span className="status-dot" />
              Demo replay
            </span>
            <span className="offline-label">
              <Unplug size={11} />
              Local service not connected
            </span>
            <Button
              variant="ghost"
              size="icon"
              title="Connect a source"
              aria-label="Connect source"
              onClick={() => setDrawer("connect")}
            >
              <CircleHelp />
            </Button>
          </div>
        </header>
        <section className="graph-heading">
          <div className="min-w-0">
            <h1>Your memory graph</h1>
            <p>
              <Network size={12} />
              {graph.memories.length} active memories<span>·</span>
              {graph.edges.length} relationships<span>·</span>
              {sources.length} sources
            </p>
          </div>
          <div className="heading-actions">
            <ViewToggle view={view} onChange={setView} />
            <Button
              variant="outline"
              className="context-button"
              onClick={() => setDrawer("context")}
              title="Memory context"
            >
              <BookOpen />
              <span>Memory context</span>
            </Button>
            <Button
              className="replay-button"
              onClick={completed ? () => setConfirmReset(true) : play}
              disabled={!ready}
            >
              {running ? <Square /> : completed ? <RotateCcw /> : <Play />}
              {running ? "Stop replay" : completed ? "Replay again" : "Replay captured prompts"}
            </Button>
          </div>
        </section>
        <section className="graph-area" aria-label="Memory graph">
          {view === "dual" ? (
            <TwoGraphStage
              memoryCanvas={memoryStage}
              memoryScope={`Agent memory · ${memoryRevision(state)}`}
              trace={run.trace}
              codeGraph={codeGraph}
              codeStatus={sampleCodeAdapter.status()}
              reducedMotion={reducedMotion}
              staleCapsule={
                !!run.trace?.capsule && run.trace.capsule.revision !== memoryRevision(state)
              }
              controls={
                <div className="demo-controls" role="group" aria-label="Demo replay controls">
                  <span className="replay-tag">Sample replay</span>
                  {run.playing ? (
                    <Button size="sm" variant="outline" onClick={pauseDemo}>
                      <Pause />
                      Pause
                    </Button>
                  ) : (
                    <Button size="sm" onClick={playDemo} disabled={!ready}>
                      <Play />
                      {run.step === 0 || run.step >= demoScript.length
                        ? "Play the 60-second demo"
                        : "Resume demo"}
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => stepRun(false)}
                    disabled={!ready || run.playing || run.step >= demoScript.length}
                  >
                    <SkipForward />
                    Step
                  </Button>
                  <Button size="sm" variant="ghost" onClick={replayDemo} disabled={run.step === 0}>
                    <RotateCcw />
                    Replay
                  </Button>
                  <span className="demo-caption" role="status">
                    {run.step
                      ? `${run.step}/${demoScript.length} · ${demoScript[run.step - 1]?.caption}`
                      : "Coding prompts come from captured sample prompts"}
                  </span>
                </div>
              }
            />
          ) : (
            memoryStage
          )}
          {inspector && (
            <MemoryInspector
              key={state.selectedId ?? "none"}
              memory={selected}
              state={state}
              onUpdate={(apply) => update(apply)}
              onClose={() => setInspector(false)}
              onSource={() => setFeed("turns")}
            />
          )}
        </section>
        {feed && (
          <section className="activity-panel" aria-label="Captured prompt evidence">
            <div className="activity-toolbar">
              <Button
                variant="ghost"
                className={feed === "turns" ? "tab-active" : ""}
                onClick={() => setFeed("turns")}
              >
                <Terminal />
                Captured prompts
              </Button>
              <Button
                variant="ghost"
                className={feed === "activity" ? "tab-active" : ""}
                onClick={() => setFeed("activity")}
              >
                <Activity />
                Memory activity
              </Button>
              <span className="activity-title">
                {feed === "turns" ? "Read-only · demo evidence" : "Agent memory"}
              </span>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Close activity feed"
                onClick={() => setFeed(null)}
              >
                <X />
              </Button>
            </div>
            <div className="activity-scroll">
              {feed === "turns" ? (
                state.turns.length ? (
                  [...state.turns].reverse().map((turn) => (
                    <div className="turn" key={turn.id}>
                      <span className="turn-source">{sourceLabel(turn)}</span>
                      <span className="turn-role">you</span>
                      <div>{turn.user}</div>
                      {turn.agent && (
                        <>
                          <span className="turn-role agent">work</span>
                          <div className="turn-work">
                            {turn.agent}
                            <small> · assistant output, not used for memory</small>
                          </div>
                        </>
                      )}
                      <div className="turn-affected">
                        <span>
                          {displayTime(turn.time)} ·{" "}
                          {turn.affected.length ? "Affected:" : "No relevant memories"}
                        </span>
                        {turn.affected.map((id) => (
                          <Button key={id} variant="secondary" size="sm" onClick={() => choose(id)}>
                            {state.memories.find((m) => m.id === id)?.content ?? id}
                          </Button>
                        ))}
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="text-xs text-muted-foreground">No captured prompts yet.</p>
                )
              ) : state.activity.length ? (
                state.activity.map((entry) => (
                  <div className="activity-entry" key={entry.id}>
                    <time>{displayTime(entry.time)}</time>
                    <div>
                      {entry.text}
                      {entry.affected.length > 0 && (
                        <div className="turn-affected">
                          {entry.affected.map((id) => (
                            <Button key={id} variant="ghost" size="sm" onClick={() => choose(id)}>
                              {state.memories.find((m) => m.id === id)?.content ?? id}
                            </Button>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                ))
              ) : (
                <p className="text-xs text-muted-foreground">No memory activity yet.</p>
              )}
            </div>
          </section>
        )}
        <section className="command-section">
          <div className="command-label-row">
            <label className="command-label" htmlFor="memory-command">
              <Command size={13} />
              Tell memory what to remember
            </label>
            <span className="demo-interpretation">
              <span className="status-dot" />
              Demo interpretation
            </span>
          </div>
          <form
            className="command-input-wrap"
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
          >
            <input
              id="memory-command"
              value={input}
              placeholder="Add a fact, connect an idea, or correct a memory…"
              onChange={(e) => setInput(e.target.value)}
            />
            <Button
              type="submit"
              size="icon"
              disabled={!ready || busy || !input.trim()}
              aria-label="Apply memory command"
              title="Apply memory command"
            >
              {busy ? <LoaderCircle /> : <ArrowUp />}
            </Button>
          </form>
          <div className="command-chips">
            {demoCommands.map((command, i) => (
              <Button
                key={command}
                variant="ghost"
                className="command-chip"
                onClick={() => setInput(command)}
              >
                {i === 0 ? <Plus /> : i === 1 ? <GitBranch /> : i === 2 ? <RotateCcw /> : <X />}
                {command}
              </Button>
            ))}
          </div>
          {feedback && (
            <div role="status" className={`command-feedback ${feedback.error ? "is-error" : ""}`}>
              {feedback.message}
            </div>
          )}
        </section>
        <footer className="workspace-footer">
          <span>
            <Terminal size={11} />
            <Button
              variant="link"
              className="h-auto p-0 text-[9px] text-muted-foreground"
              onClick={() => setFeed(feed ? null : "turns")}
            >
              {feed ? "Hide captured prompts" : "Show captured prompts"}
            </Button>
            <span>·</span>
            <Button
              variant="link"
              className="h-auto p-0 text-[9px] text-muted-foreground"
              onClick={() => setConfirmReset(true)}
            >
              Reset memory graph
            </Button>
          </span>
          <span>
            GPT-6 Astra Hackathon London<span>·</span>Visual prototype
          </span>
        </footer>
      </main>
      <MemoryDrawers drawer={drawer} onClose={() => setDrawer(null)} state={state} />
      <Dialog
        open={confirmReset}
        onOpenChange={(open) => {
          if (!open) setConfirmReset(false);
        }}
      >
        <DialogContent className="confirm-dialog">
          <DialogTitle>Reset your memory graph?</DialogTitle>
          <DialogDescription className="confirm-description">
            Reset the demo memory graph, its positions, history and replay progress. You can restore
            the sample graph or start empty.
          </DialogDescription>
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirmReset(false)}>
              Cancel
            </Button>
            <Button variant="outline" onClick={() => reset(true)}>
              Start empty
            </Button>
            <Button onClick={() => reset(false)}>
              <RotateCcw />
              Reset demo
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
export function ModeSwitch({
  mode,
  onChange,
}: {
  mode: "demo" | "live";
  onChange: (mode: "demo" | "live") => void;
}) {
  return (
    <div className="mode-switch" role="radiogroup" aria-label="Memory source mode">
      {(["demo", "live"] as const).map((m) => (
        <button
          key={m}
          type="button"
          role="radio"
          aria-checked={mode === m}
          className={mode === m ? "active" : ""}
          onClick={() => onChange(m)}
        >
          {m === "demo" ? "Demo replay" : "Live service"}
        </button>
      ))}
    </div>
  );
}
export const MODE_KEY = "memory-lens:mode";

/** ?mode=live|demo wins; else the saved mode; else Live when this tab holds a credential. */
export function initialMode(): "demo" | "live" {
  try {
    const q = new URLSearchParams(window.location.search).get("mode");
    if (q === "live" || q === "demo") return q;
    const saved = localStorage.getItem(MODE_KEY);
    if (saved === "live" || saved === "demo") return saved;
    if (sessionStorage.getItem("memory-lens:live:token")) return "live";
  } catch {
    /* storage unavailable */
  }
  return "demo";
}

export function MemoryLens() {
  const [mode, setModeState] = useState<"demo" | "live" | null>(null);
  useEffect(() => {
    setModeState(initialMode());
  }, []);
  const setMode = (next: "demo" | "live") => {
    setModeState(next);
    try {
      localStorage.setItem(MODE_KEY, next);
    } catch {
      /* storage unavailable */
    }
  };
  if (mode === null) return <div className="min-h-screen bg-background" />;
  const modeSwitch = <ModeSwitch mode={mode} onChange={setMode} />;
  return (
    <ReactFlowProvider>
      {mode === "demo" ? (
        <MemoryWorkspace modeSwitch={modeSwitch} />
      ) : (
        <LiveWorkspace modeSwitch={modeSwitch} />
      )}
    </ReactFlowProvider>
  );
}

