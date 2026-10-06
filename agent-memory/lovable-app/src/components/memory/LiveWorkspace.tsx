import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Background, Controls, MiniMap, ReactFlow, MarkerType } from "@xyflow/react";
import { RetrievalStatus, LayeredContextView } from "./RetrievalStatus";
import {
  ArrowUp,
  BookOpen,
  CircleHelp,
  Command,
  FileCode2,
  FlaskConical,
  LoaderCircle,
  Network,
  PlugZap,
  RefreshCw,
  Terminal,
  Unplug,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import {
  createCommandRequestIds,
  createLiveClient,
  createRequestGate,
  isUncertain,
  layoutNodes,
  liveStorage,
  MemoryApiError,
  newRequestId,
  setupHint,
  isAutomaticSource,
  type LiveClient,
  type LayeredContext,
  currentRetrieval,
  type LiveContext,
  type LiveGraph,
  type LiveHealth,
  type LiveNode,
  type LiveSession,
  type LiveClientOptions,
} from "@/lib/memory/live";
import { MemoryNode, type MemoryFlowNode } from "./MemoryNode";

const nodeTypes = { memory: MemoryNode };
export const POLL_MS = 4000;
type Connection =
  | { status: "disconnected" }
  | { status: "connecting" }
  | { status: "connected"; health: LiveHealth }
  | { status: "error"; message: string };
type Drawer = "connect" | "context" | "layered" | "test" | null;

function readStorage(store: "local" | "session", key: string) {
  try {
    return (store === "local" ? localStorage : sessionStorage).getItem(key) ?? "";
  } catch {
    return "";
  }
}
function writeStorage(store: "local" | "session", key: string, value: string | null) {
  try {
    const s = store === "local" ? localStorage : sessionStorage;
    if (value) s.setItem(key, value);
    else s.removeItem(key);
  } catch {
    /* storage unavailable: keep in memory only */
  }
}
const sessionLabel = (s: LiveSession) => s.title?.trim() || s.external_session_id;

export function LiveWorkspace({
  modeSwitch,
  clientFactory = createLiveClient,
}: {
  modeSwitch?: ReactNode;
  clientFactory?: (options: LiveClientOptions) => LiveClient;
}) {
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const urlState = url;
  const tokenState = token;
  const [connection, setConnection] = useState<Connection>({ status: "disconnected" });
  const [sessions, setSessions] = useState<LiveSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [graph, setGraph] = useState<LiveGraph | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<Drawer>(null);
  const [context, setContext] = useState<LiveContext | null>(null);
  const [contextError, setContextError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ message: string; error: boolean } | null>(null);
  const [showTurns, setShowTurns] = useState(false);
  const [layered, setLayered] = useState<LayeredContext | null>(null);
  const [layeredError, setLayeredError] = useState<string | null>(null);
  const client = useRef<LiveClient | null>(null);
  const graphGate = useRef(createRequestGate());
  const connectGate = useRef(createRequestGate());
  const graphAbort = useRef<AbortController | null>(null);
  const commandIds = useRef(createCommandRequestIds());
  const sessionsRef = useRef<LiveSession[]>([]);
  sessionsRef.current = sessions;

  // Prefill; auto-reconnect once only when THIS tab already holds a saved URL and
  // a tab-session credential from an earlier explicit pairing. Otherwise show setup.
  const autoTried = useRef(false);
  useEffect(() => {
    if (autoTried.current) return;
    autoTried.current = true;
    const savedUrl = readStorage("local", liveStorage.urlKey);
    const savedToken = readStorage("session", liveStorage.tokenKey);
    setUrl(savedUrl || "http://127.0.0.1:8787");
    setToken(savedToken);
    if (savedUrl && savedToken) void connect(undefined, { url: savedUrl, token: savedToken });
    else setDrawer("connect");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const connected = connection.status === "connected";
  const health = connected ? connection.health : null;

  const refreshSessions = useCallback(async () => {
    const c = client.current;
    if (!c) return;
    const ticket = connectGate.current.next();
    try {
      const list = await c.listSessions();
      if (connectGate.current.isCurrent(ticket) && client.current === c) setSessions(list.sessions);
    } catch (error) {
      if (connectGate.current.isCurrent(ticket)) setGraphError(setupHint(error));
    }
  }, []);

  async function connect(event?: React.FormEvent, saved?: { url: string; token: string }) {
    event?.preventDefault();
    const url = saved?.url ?? urlState;
    const token = saved?.token ?? tokenState;
    disconnect(false);
    let c: LiveClient;
    try {
      c = clientFactory({ baseUrl: url, token: token.trim() });
    } catch (error) {
      setConnection({ status: "error", message: setupHint(error) });
      return;
    }
    const ticket = connectGate.current.next();
    setConnection({ status: "connecting" });
    writeStorage("local", liveStorage.urlKey, url.trim());
    writeStorage("session", liveStorage.tokenKey, token.trim() || null);
    try {
      const h = await c.health();
      const list = await c.listSessions();
      if (!connectGate.current.isCurrent(ticket)) return;
      client.current = c;
      setSessions(list.sessions);
      setConnection({ status: "connected", health: h });
      setDrawer(null);
    } catch (error) {
      if (!connectGate.current.isCurrent(ticket)) return;
      setConnection({ status: "error", message: setupHint(error) });
    }
  }

  function disconnect(clearToken: boolean) {
    connectGate.current.invalidate();
    graphGate.current.invalidate();
    graphAbort.current?.abort();
    client.current = null;
    setConnection({ status: "disconnected" });
    setSessions([]);
    setSessionId(null);
    setGraph(null);
    setContext(null);
    setLayered(null);
    setLayeredError(null);
    setSelectedNode(null);
    setGraphError(null);
    setFeedback(null);
    if (clearToken) {
      setToken("");
      writeStorage("session", liveStorage.tokenKey, null);
    }
  }

  const loadGraph = useCallback(async (id: string) => {
    const c = client.current;
    if (!c) return;
    graphAbort.current?.abort();
    const abort = new AbortController();
    graphAbort.current = abort;
    const ticket = graphGate.current.next();
    try {
      const g = await c.graph(
        id,
        abort.signal,
        sessionsRef.current.map((s) => s.id),
      );
      if (!graphGate.current.isCurrent(ticket) || client.current !== c) return;
      setGraph(g);
      setGraphError(null);
    } catch (error) {
      if (!graphGate.current.isCurrent(ticket)) return;
      if (error instanceof MemoryApiError && error.code === "aborted") return;
      // Keep last good live graph visible; never fall back to demo data.
      setGraphError(setupHint(error));
    }
  }, []);

  // Poll only the selected session while connected and visible.
  useEffect(() => {
    if (!connected || !sessionId) return;
    const id = sessionId;
    void loadGraph(id);
    const tick = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      void loadGraph(id);
    };
    const timer = setInterval(tick, POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void loadGraph(id);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      graphGate.current.invalidate();
      graphAbort.current?.abort();
    };
  }, [connected, sessionId, loadGraph]);

  // One shared memory graph per local workspace: auto-open the graph the service returns.
  useEffect(() => {
    if (connected && !sessionId && sessions[0]) selectSession(sessions[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, sessions, sessionId]);

  function selectSession(id: string) {
    if (id === sessionId) return;
    graphGate.current.invalidate();
    graphAbort.current?.abort();
    setGraph(null);
    setContext(null);
    setLayered(null);
    setLayeredError(null);
    setSelectedNode(null);
    setFeedback(null);
    setGraphError(null);
    setSessionId(id);
  }

  async function openContext() {
    setDrawer("context");
    const c = client.current;
    const id = sessionId;
    if (!c || !id) return;
    setContext(null);
    setContextError(null);
    try {
      const ctx = await c.context(id);
      if (client.current === c && id === sessionIdRef.current) setContext(ctx);
    } catch (error) {
      setContextError(setupHint(error));
    }
  }
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;

  /** User-requested preview: layered_context when advertised, else plain context. */
  async function previewNextTurn() {
    const c = client.current;
    const id = sessionId;
    if (!c || !id) return;
    if (!health?.capabilities?.includes("layered_context")) {
      void openContext();
      return;
    }
    setDrawer("layered");
    setLayered(null);
    setLayeredError(null);
    try {
      const result = await c.layeredContext({ session_id: id, limit: 20 });
      if (client.current === c && sessionIdRef.current === id) setLayered(result);
    } catch (error) {
      if (client.current === c && sessionIdRef.current === id) setLayeredError(setupHint(error));
    }
  }

  async function runCommand(text: string) {
    const c = client.current;
    const id = sessionId;
    if (!c || !id || !text.trim() || busy) return;
    setBusy(true);
    setFeedback({ message: "Sending to memory service…", error: false });
    const command = text.trim();
    try {
      const result = await c.command({
        session_id: id,
        command,
        request_id: commandIds.current.idFor(id, command),
      });
      commandIds.current.settle(id, command);
      if (sessionIdRef.current !== id) return;
      setInput("");
      setFeedback({ message: `${result.message} · revision ${result.revision}`, error: false });
      await loadGraph(id);
      if (drawer === "context") void openContext();
    } catch (error) {
      // Uncertain failures keep the request_id so a retry of the same command is idempotent.
      if (!isUncertain(error)) commandIds.current.settle(id, command);
      if (sessionIdRef.current === id) setFeedback({ message: setupHint(error), error: true });
    } finally {
      setBusy(false);
    }
  }

  // A retrieval is shown only for this session and the graph's current memory revision.
  const retrieval =
    graph && sessionId
      ? (currentRetrieval(layered, sessionId, graph.revision) ??
        currentRetrieval(graph.latest_retrieval, sessionId, graph.revision))
      : null;
  // The preview drawer uses the same session + current-revision guard; a preview
  // from an older revision is withheld and marked stale, never shown as next-turn context.
  const layeredPreview =
    graph && sessionId ? currentRetrieval(layered, sessionId, graph.revision) : null;
  const layeredStale = !!layered && !layeredPreview;
  const laid = useMemo(
    () => layoutNodes(graph?.nodes.filter((n) => n.status === "active") ?? []),
    [graph],
  );
  const nodes: MemoryFlowNode[] = useMemo(
    () =>
      laid.map((n) => ({
        id: n.id,
        type: "memory",
        position: n.position,
        data: {
          memory: {
            id: n.id,
            type: n.type,
            content: n.content,
            pinned: n.pinned,
            source: isAutomaticSource(n.source_kind) ? "Automatic capture" : "User-added",
          },
          changed: false,
          recalled: !!retrieval?.memory.node_ids.includes(n.id),
        },
        selected: selectedNode === n.id,
        ariaLabel: `${n.type}: ${n.content}`,
      })),
    [laid, selectedNode, retrieval],
  );
  const activeIds = new Set(laid.map((n) => n.id));
  const edges = (graph?.edges ?? [])
    .filter((e) => activeIds.has(e.source) && activeIds.has(e.target))
    .map((e) => ({
      ...e,
      type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed },
      labelBgPadding: [7, 4] as [number, number],
      labelBgBorderRadius: 3,
    }));
  const session = sessions.find((s) => s.id === sessionId);
  const node = graph?.nodes.find((n) => n.id === selectedNode);
  const astraReady = !!health?.openai_configured;
  const liveStage = (
    <div className="graph-stage">
      <ReactFlow<MemoryFlowNode>
        key={sessionId ?? "none"}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_, n) => setSelectedNode(n.id)}
        nodesConnectable={false}
        fitView
        fitViewOptions={{ padding: 0.23, maxZoom: 1 }}
        minZoom={0.3}
        maxZoom={1.6}
      >
        <Background gap={23} size={1} />
        <Controls showInteractive={false} position="bottom-right" orientation="horizontal" />
        <MiniMap pannable zoomable ariaLabel="Live memory graph minimap" />
      </ReactFlow>
      {!laid.length && (
        <div className="empty-graph">
          <Network size={35} strokeWidth={1.2} />
          <h2>
            {connected
              ? sessionId
                ? "No active memories"
                : "Opening your memory graph"
              : "Live service disconnected"}
          </h2>
          <p>
            {connected
              ? "Memories appear here after any connected tool sends a prompt or you apply a command."
              : "Open Connect source to enter your local service URL and access token. Nothing is called until you connect."}
          </p>
        </div>
      )}
    </div>
  );

  return (
    <div className="workspace">
      <aside className="session-rail" aria-label="Live memory graph">
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
          {connected && (
            <Button
              variant="ghost"
              size="icon"
              className="h-5 w-5"
              aria-label="Refresh memory graph"
              onClick={() => void refreshSessions()}
            >
              <RefreshCw />
            </Button>
          )}
        </div>
        <nav className="session-list">
          {!connected && <p className="live-hint">Local service not connected.</p>}
          {connected && !sessions.length && (
            <p className="live-hint">No memory graph yet. Send a test prompt or connect a tool.</p>
          )}
          {sessions.map((s) => (
            <Button
              key={s.id}
              variant="ghost"
              className={`session-button ${sessionId === s.id ? "active" : ""}`}
              aria-pressed={sessionId === s.id}
              onClick={() => selectSession(s.id)}
            >
              <FileCode2 size={16} />
              <div className="min-w-0">
                <div className="session-title">{sessionLabel(s)}</div>
                <div className="session-sub">Shared across tools · rev {s.revision}</div>
              </div>
              {sessionId === s.id && <span className="session-marker" />}
            </Button>
          ))}
        </nav>
        <div className="rail-rule" />
        <nav className="rail-menu">
          <Button
            variant="ghost"
            className="rail-action"
            disabled={!connected}
            onClick={() => setDrawer("test")}
          >
            <FlaskConical />
            Send test prompt
          </Button>
          <Button
            variant="ghost"
            className="rail-action"
            disabled={!sessionId}
            onClick={() => void openContext()}
          >
            <BookOpen />
            Memory context
          </Button>
          <Button
            variant="ghost"
            className="rail-action"
            disabled={!graph}
            onClick={() => setShowTurns(!showTurns)}
          >
            <Terminal />
            Source turns<span>{graph?.turns.length ?? 0}</span>
          </Button>
        </nav>
        <div className="rail-bottom">
          <div className="connection-box">
            <div className="connection-title">
              {connected ? <PlugZap size={12} /> : <Unplug size={12} />}
              {connected ? "Service connected" : "Service not connected"}
            </div>
            <Button
              variant="outline"
              className="connect-button"
              onClick={() => setDrawer("connect")}
            >
              <PlugZap />
              Connect source
            </Button>
          </div>
        </div>
      </aside>
      <main className="main-workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <span>Live</span>
            <span>·</span>
            <strong className="truncate">{session ? sessionLabel(session) : "Agent memory"}</strong>
          </div>
          <div className="topbar-actions" data-testid="live-status">
            <span className="demo-badge">
              <span className="status-dot" />
              Live service
            </span>
            {connected ? (
              <span className="offline-label">
                API v1 · {health?.model} · {astraReady ? "model key configured" : "no model key"}
              </span>
            ) : (
              <span className="offline-label">
                <Unplug size={11} />
                {connection.status === "connecting" ? "Connecting…" : "Disconnected"}
              </span>
            )}
            <Button
              variant="ghost"
              size="icon"
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
              {graph
                ? `${laid.length} active memories · ${edges.length} relationships · revision ${graph.revision}`
                : connected
                  ? "Opening your memory graph"
                  : "Not connected"}
            </p>
          </div>
          <div className="heading-actions">
            <Button
              variant="outline"
              onClick={() => void previewNextTurn()}
              disabled={!connected || !sessionId}
            >
              <BookOpen />
              Preview next-turn context
            </Button>
          </div>
        </section>
        {connected && !astraReady && (
          <div className="live-banner" role="status">
            The service reports no OpenAI key. Graphs are readable, but extraction and memory
            commands will fail until a key is configured on the service.
          </div>
        )}
        {graphError && (
          <div className="live-banner live-error" role="alert">
            {graphError}
          </div>
        )}
        {connected && (
          <RetrievalStatus
            retrieval={retrieval}
            revision={graph?.revision ?? null}
            codeGraph={health?.existing_code_graph}
          />
        )}
        <section className="graph-area" aria-label="Live memory graph">
          {liveStage}
          {node && (
            <LiveInspector
              key={node.id}
              node={node}
              disabled={busy || !astraReady}
              canPin={!!health?.capabilities?.includes("pin")}
              onClose={() => setSelectedNode(null)}
              onCommand={(text) => void runCommand(text)}
            />
          )}
        </section>
        {showTurns && graph && (
          <section className="activity-panel" aria-label="Source turns">
            <div className="activity-toolbar">
              <span className="activity-title">
                Source turns · read-only evidence from the service
              </span>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Close turns"
                onClick={() => setShowTurns(false)}
              >
                <X />
              </Button>
            </div>
            <div className="live-turns">
              {graph.turns.length ? (
                graph.turns.map((t) => (
                  <div key={t.turn_id} className="live-turn">
                    <div className="live-meta">
                      {t.source ? `${t.source} · ${t.external_session_id ?? "chat"} · ` : ""}
                      {t.turn_id} · {t.status}
                    </div>
                    <pre>User: {t.user_message}</pre>
                  </div>
                ))
              ) : (
                <p className="live-hint">No turns ingested.</p>
              )}
            </div>
          </section>
        )}
        <section className="command-section">
          <form
            className="live-command"
            onSubmit={(e) => {
              e.preventDefault();
              void runCommand(input);
            }}
          >
            <Command size={14} />
            <input
              aria-label="Tell memory what to remember"
              placeholder={
                sessionId ? "Tell memory what to remember…" : "Connect to open your memory graph"
              }
              value={input}
              disabled={!sessionId || busy}
              onChange={(e) => setInput(e.target.value)}
            />
            <Button
              type="submit"
              size="icon"
              aria-label="Send memory command"
              disabled={!sessionId || busy || !input.trim()}
            >
              {busy ? <LoaderCircle className="animate-spin" /> : <ArrowUp />}
            </Button>
          </form>
          <div className={`live-feedback ${feedback?.error ? "live-error" : ""}`} role="status">
            {feedback?.message ??
              "Live interpretation by the memory service model. No local parser is used."}
          </div>
        </section>
      </main>
      <Sheet open={drawer !== null} onOpenChange={(o) => !o && setDrawer(null)}>
        <SheetContent className="drawer-content sm:max-w-[460px] overflow-y-auto">
          {drawer === "connect" && (
            <ConnectPanel
              url={url}
              token={token}
              connection={connection}
              onUrl={setUrl}
              onToken={setToken}
              onConnect={connect}
              onDisconnect={() => disconnect(true)}
            />
          )}
          {drawer === "layered" && (
            <LayeredContextView
              title={session ? sessionLabel(session) : ""}
              value={layeredPreview}
              stale={layeredStale}
              currentRevision={graph?.revision ?? null}
              error={layeredError}
            />
          )}
          {drawer === "context" && (
            <>
              <div className="drawer-eyebrow">
                Live context · {session ? sessionLabel(session) : ""}
              </div>
              <SheetTitle className="drawer-title">Available to the next tool turn</SheetTitle>
              <SheetDescription className="drawer-description">
                Exactly what the service returns from your memory graph for the next prompt in any
                tool. Delivery depends on the tool bridge; this view does not confirm receipt.
              </SheetDescription>
              {contextError && <p className="live-error">{contextError}</p>}
              {!context && !contextError && <p className="drawer-description">Loading…</p>}
              {context && (
                <>
                  <div className="live-meta">
                    Revision {context.revision} · {context.node_ids.length} nodes
                  </div>
                  <div className="live-meta">{context.node_ids.join(", ") || "No node IDs"}</div>
                  <pre className="live-context">{context.text || "(empty)"}</pre>
                </>
              )}
            </>
          )}
          {drawer === "test" && (
            <TestExchange
              defaultSession={session}
              onSubmit={async (form) => {
                const c = client.current;
                if (!c) throw new MemoryApiError("unauthorized", "Not connected.");
                const ensured = await c.ensureSession({
                  source: form.source,
                  external_session_id: form.external_session_id,
                  ...(form.title ? { title: form.title } : {}),
                });
                const result = await c.ingestTurn({
                  session_id: ensured.session_id,
                  turn_id: form.turn_id,
                  user_message: form.user_message,
                  base_revision: ensured.revision,
                });
                await refreshSessions();
                selectSession(ensured.session_id);
                if (ensured.session_id === sessionIdRef.current) void loadGraph(ensured.session_id);
                if (result.status === "noop" && result.revision > ensured.revision)
                  return `Turn noop · a newer manual edit (revision ${result.revision}) took precedence`;
                return `Turn ${result.status} · revision ${result.revision} · ${result.changed_ids.length} changed`;
              }}
            />
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function ConnectPanel(props: {
  url: string;
  token: string;
  connection: Connection;
  onUrl: (v: string) => void;
  onToken: (v: string) => void;
  onConnect: (e: React.FormEvent) => void;
  onDisconnect: () => void;
}) {
  const { connection } = props;
  return (
    <>
      <div className="drawer-eyebrow">Live service</div>
      <SheetTitle className="drawer-title">Connect your tools to one memory graph</SheetTitle>
      <SheetDescription className="drawer-description">
        Enter the memory-api URL and your owner bridge token. The token stays in this tab’s session
        storage only. No model key is entered here.
      </SheetDescription>
      <form className="live-form" onSubmit={props.onConnect}>
        <label>
          Memory-api URL
          <input
            value={props.url}
            onChange={(e) => props.onUrl(e.target.value)}
            placeholder="http://127.0.0.1:8787"
            autoComplete="url"
          />
        </label>
        <label>
          Owner bridge token
          <input
            type="password"
            value={props.token}
            onChange={(e) => props.onToken(e.target.value)}
            autoComplete="off"
          />
        </label>
        <div className="flex gap-2">
          <Button type="submit" disabled={connection.status === "connecting" || !props.url.trim()}>
            {connection.status === "connecting" ? "Testing…" : "Test and connect"}
          </Button>
          {connection.status === "connected" && (
            <Button type="button" variant="outline" onClick={props.onDisconnect}>
              Disconnect and forget token
            </Button>
          )}
        </div>
        <div
          className={`live-meta ${connection.status === "error" ? "live-error" : ""}`}
          role="status"
        >
          {connection.status === "connected"
            ? `Connected · API v${connection.health.api_version} · ${connection.health.model} · ${connection.health.openai_configured ? "model key configured" : "no model key configured"}`
            : connection.status === "error"
              ? connection.message
              : connection.status === "connecting"
                ? "Checking health, then opening your memory graph…"
                : "Not connected."}
        </div>
      </form>
      <div className="section-caption">Pair a tool with the portable bridge</div>
      <p className="drawer-description">
        Set these in the environment of the bridge process. Your CLI’s hooks or MCP config then run
        the bridge. Showing this text does not connect any tool.
      </p>
      <pre className="live-context">{`MEMORY_LENS_API_URL=${props.url || "<memory-api URL>"}
MEMORY_LENS_TOKEN=<owner bridge token>`}</pre>
      <p className="drawer-description">
        Every tool and chat feeds the same memory graph for your workspace. Each prompt keeps its
        tool and native chat ID as provenance. Hook installation is reviewed in your tool;
        code-review-graph does not install hooks.
      </p>
    </>
  );
}

type TestForm = {
  source: string;
  external_session_id: string;
  title: string;
  user_message: string;
  turn_id: string;
};
function TestExchange({
  defaultSession,
  onSubmit,
}: {
  defaultSession: LiveSession | undefined;
  onSubmit: (form: TestForm) => Promise<string>;
}) {
  const [form, setForm] = useState<TestForm>({
    source: defaultSession?.source ?? "manual",
    external_session_id: defaultSession?.external_session_id ?? "",
    title: defaultSession?.title ?? "",
    user_message: "",
    turn_id: newRequestId(),
  });
  const [status, setStatus] = useState<{ text: string; error: boolean } | null>(null);
  const [sending, setSending] = useState(false);
  const field = (k: keyof TestForm, label: string, area = false) => (
    <label>
      {label}
      {area ? (
        <textarea
          value={form[k]}
          onChange={(e) => setForm({ ...form, [k]: e.target.value })}
          rows={3}
        />
      ) : (
        <input value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
      )}
    </label>
  );
  return (
    <>
      <div className="drawer-eyebrow">Live service</div>
      <SheetTitle className="drawer-title">Send test prompt</SheetTitle>
      <SheetDescription className="drawer-description">
        Sends only the user prompt you enter to the memory service (ensure_session, then
        ingest_turn). Assistant responses are never sent or used for memory. The service may forward
        the prompt to its model for extraction.
      </SheetDescription>
      <form
        className="live-form"
        onSubmit={async (e) => {
          e.preventDefault();
          if (
            !form.source.trim() ||
            !form.external_session_id.trim() ||
            !form.user_message.trim()
          ) {
            setStatus({
              text: "Source, external session ID and user prompt are required.",
              error: true,
            });
            return;
          }
          setSending(true);
          try {
            setStatus({ text: await onSubmit(form), error: false });
            setForm((f) => ({
              ...f,
              user_message: "",
              turn_id: newRequestId(),
            }));
          } catch (error) {
            setStatus({ text: setupHint(error), error: true });
          } finally {
            setSending(false);
          }
        }}
      >
        {field("source", "Source")}
        {field("external_session_id", "Native chat ID (provenance)")}
        {field("title", "Title (optional)")}
        {field("turn_id", "Turn ID")}
        {field("user_message", "User prompt", true)}
        <Button type="submit" disabled={sending}>
          {sending ? "Sending…" : "Send prompt"}
        </Button>
        {status && (
          <div className={`live-meta ${status.error ? "live-error" : ""}`} role="status">
            {status.text}
          </div>
        )}
      </form>
    </>
  );
}

function LiveInspector({
  node,
  disabled,
  canPin,
  onClose,
  onCommand,
}: {
  node: LiveNode;
  disabled: boolean;
  canPin: boolean;
  onClose: () => void;
  onCommand: (text: string) => void;
}) {
  const [draft, setDraft] = useState(node.content);
  return (
    <aside className="inspector" aria-label="Memory details">
      <div className="inspector-head">
        <div className={`type-label type-${node.type}`}>
          <span>{node.type}</span>
        </div>
        <Button variant="ghost" size="icon" aria-label="Close inspector" onClick={onClose}>
          <X />
        </Button>
      </div>
      <div className="inspector-body">
        <p className="live-node-content">{node.content}</p>
        <dl className="live-dl">
          <dt>Status</dt>
          <dd>
            {node.status}
            {node.pinned ? " · pinned" : ""}
          </dd>
          <dt>Source</dt>
          <dd>
            {isAutomaticSource(node.source_kind) ? "Automatic capture" : "User edit"} (
            {node.source_kind})
          </dd>
          <dt>Source turn</dt>
          <dd className="font-mono">{node.source_turn_id ?? "-"}</dd>
          <dt>Node ID</dt>
          <dd className="font-mono">{node.id}</dd>
        </dl>
        <div className="section-caption">Evidence</div>
        <pre className="live-context">{node.evidence ?? "No evidence recorded."}</pre>
        <div className="section-caption">History</div>
        {node.history.length ? (
          node.history.map((h, i) => (
            <div className="live-meta" key={i}>
              {h.status ?? "superseded"} · {h.content}
              {h.source_turn_id ? ` · ${h.source_turn_id}` : ""}
            </div>
          ))
        ) : (
          <p className="live-hint">No previous versions.</p>
        )}
        <div className="section-caption">Edit through memory command</div>
        <textarea
          className="live-textarea"
          value={draft}
          rows={3}
          onChange={(e) => setDraft(e.target.value)}
        />
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={disabled || !draft.trim() || draft.trim() === node.content}
            onClick={() => onCommand(`Correct memory ${node.id} to: ${draft.trim()}`)}
          >
            Correct
          </Button>
          {canPin && (
            <Button
              size="sm"
              variant="outline"
              disabled={disabled}
              onClick={() => onCommand(`${node.pinned ? "Unpin" : "Pin"} memory ${node.id}`)}
            >
              {node.pinned ? "Unpin" : "Pin"}
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            disabled={disabled}
            onClick={() => onCommand(`Forget memory ${node.id}`)}
          >
            Forget
          </Button>
        </div>
        <p className="live-hint">
          These send a natural-language memory command referencing the node ID; the service model
          interprets and validates it. Nothing changes locally.
        </p>
      </div>
    </aside>
  );
}

