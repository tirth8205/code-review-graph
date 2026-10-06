import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { LiveWorkspace } from "./LiveWorkspace";
import type { LiveClient, LiveGraph } from "@/lib/memory/live";

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

const graph = (session_id: string, content: string): LiveGraph => ({
  session_id,
  revision: 1,
  nodes: [
    {
      id: `${session_id}-n`,
      type: "fact",
      content,
      status: "active",
      source_turn_id: "t1",
      source_kind: "turn",
      evidence: content,
      pinned: false,
      history: [],
    },
  ],
  edges: [],
  turns: [],
});

function fakeClient(overrides: Partial<LiveClient> = {}) {
  const client = {
    health: vi.fn(async () => ({
      ok: true as const,
      api_version: 1 as const,
      model: "gpt-6-astra",
      openai_configured: false,
    })),
    listSessions: vi.fn(async () => ({
      sessions: [
        {
          id: "a",
          title: "Alpha chat",
          source: "codex",
          external_session_id: "x1",
          revision: 1,
          updated_at: "",
        },
        {
          id: "b",
          title: "Beta chat",
          source: "claude",
          external_session_id: "x2",
          revision: 1,
          updated_at: "",
        },
      ],
    })),
    ensureSession: vi.fn(),
    graph: vi.fn(),
    ingestTurn: vi.fn(),
    command: vi.fn(),
    context: vi.fn(),
    ...overrides,
  } as unknown as LiveClient;
  return client;
}
const renderLive = (client: LiveClient, factory = vi.fn(() => client)) => {
  render(
    <ReactFlowProvider>
      <LiveWorkspace clientFactory={factory} />
    </ReactFlowProvider>,
  );
  return factory;
};
async function connect() {
  fireEvent.click(screen.getAllByRole("button", { name: "Connect source" })[0]!);
  fireEvent.change(await screen.findByLabelText("Owner bridge token"), {
    target: { value: "mlb_synthetic" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Test and connect" }));
}

describe("LiveWorkspace", () => {
  it("never calls the service before the user connects, then checks health before list_sessions", async () => {
    const client = fakeClient();
    const factory = renderLive(client);
    expect(factory).not.toHaveBeenCalled();
    expect(screen.getByText("Live service disconnected")).toBeTruthy();
    await connect();
    await screen.findByText("Alpha chat");
    expect(client.health).toHaveBeenCalledBefore(client.listSessions as never);
    expect(screen.getByTestId("live-status").textContent).toContain("no model key");
    expect(screen.getByText(/reports no OpenAI key/)).toBeTruthy();
  });

  it("ignores a stale graph response after switching sessions", async () => {
    let resolveA: (g: LiveGraph) => void = () => {};
    const client = fakeClient({
      graph: vi.fn((id: string) =>
        id === "a"
          ? new Promise<LiveGraph>((r) => (resolveA = r))
          : Promise.resolve(graph("b", "Beta memory")),
      ) as never,
    });
    renderLive(client);
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    fireEvent.click(screen.getByText("Beta chat"));
    await screen.findByText("Beta memory");
    await act(async () => resolveA(graph("a", "Alpha memory")));
    expect(screen.queryByText("Alpha memory")).toBeNull();
    expect(screen.getByText("Beta memory")).toBeTruthy();
  });

  it("shows auth errors without demo fallback and disconnect clears live data", async () => {
    const client = fakeClient({
      listSessions: vi.fn(async () => {
        const { MemoryApiError } = await import("@/lib/memory/live");
        throw new MemoryApiError("unauthorized", "x", 401);
      }) as never,
    });
    renderLive(client);
    await connect();
    await screen.findByText(/bridge token was rejected/);
    expect(screen.queryByText("Billing feature")).toBeNull();
    expect(screen.queryByText("Use Stripe for billing")).toBeNull();
  });

  it("disconnect forgets sessions and token", async () => {
    const client = fakeClient({ graph: vi.fn(async () => graph("a", "Alpha memory")) as never });
    renderLive(client);
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    await screen.findByText("Alpha memory");
    fireEvent.click(screen.getAllByRole("button", { name: "Connect source" })[0]!);
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect and forget token" }));
    await waitFor(() => expect(screen.queryByText("Alpha chat")).toBeNull());
    expect(screen.queryByText("Alpha memory")).toBeNull();
    expect(sessionStorage.getItem("memory-lens:live:token")).toBeNull();
  });
});

describe("LiveWorkspace review fixes", () => {
  const userNode = (pinnedCap: boolean) =>
    fakeClient({
      health: vi.fn(async () => ({
        ok: true as const,
        api_version: 1 as const,
        model: "gpt-6-astra",
        openai_configured: true,
        ...(pinnedCap ? { capabilities: ["pin"] } : {}),
      })),
      graph: vi.fn(async () => ({
        ...graph("a", "Local memory"),
        nodes: [{ ...graph("a", "Local memory").nodes[0]!, source_kind: "user" }],
      })) as never,
    });

  it("shows local 'user' captures as automatic and hides Pin without a capability", async () => {
    renderLive(userNode(false));
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    expect(await screen.findByText("Captured from source")).toBeTruthy();
    fireEvent.click(screen.getByText("Local memory"));
    expect(await screen.findByText(/Automatic capture \(/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pin" })).toBeNull();
    expect(screen.getByRole("button", { name: "Forget" })).toBeTruthy();
  });

  it("shows Pin only when the service advertises it", async () => {
    renderLive(userNode(true));
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    fireEvent.click(await screen.findByText("Local memory"));
    expect(await screen.findByRole("button", { name: "Pin" })).toBeTruthy();
  });

  it("retries the same command with the same request_id after a network error", async () => {
    const { MemoryApiError } = await import("@/lib/memory/live");
    const command = vi
      .fn()
      .mockRejectedValueOnce(new MemoryApiError("network", "lost"))
      .mockResolvedValueOnce({
        session_id: "a",
        revision: 2,
        status: "applied",
        changed_ids: [],
        message: "Done",
      })
      .mockResolvedValueOnce({
        session_id: "a",
        revision: 3,
        status: "applied",
        changed_ids: [],
        message: "Done",
      });
    renderLive(Object.assign(userNode(false), { command }));
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    const input = await screen.findByLabelText("Tell memory what to remember");
    const send = () => fireEvent.click(screen.getByRole("button", { name: "Send memory command" }));
    fireEvent.change(input, { target: { value: "Forget the old plan" } });
    send();
    await screen.findByText(/Service unreachable/);
    send();
    await screen.findByText(/Done · revision 2/);
    fireEvent.change(input, { target: { value: "Forget the old plan" } });
    send();
    await screen.findByText(/Done · revision 3/);
    const ids = command.mock.calls.map((c) => c[0].request_id);
    expect(ids[0]).toBe(ids[1]);
    expect(ids[2]).not.toBe(ids[1]);
  });

  const layered = (sid: string, revision: number, code = "not_configured") => ({
    session_id: sid,
    revision,
    node_ids: [`${sid}-n`],
    text: "memory text",
    memory: {
      status: "ready",
      session_id: sid,
      revision,
      node_ids: [`${sid}-n`],
      text: "Reply in British English",
    },
    code_graph:
      code === "ready"
        ? {
            status: "ready",
            repo_id: "repo",
            graph: { nodes: [{ id: "x" }], edges: [] },
            text: "refunds.py:validate",
          }
        : { status: code, reason: "connector missing" },
    stages: [
      { stage: "memory", order: 1, status: "ready", revision },
      { stage: "code_graph", order: 2, status: code },
      { stage: "agent_context", order: 3, status: code === "ready" ? "ready" : "partial" },
    ],
    retrieved_at: "2026-10-06T18:40:00Z",
  });
  const withCaps = {
    health: vi.fn(async () => ({
      ok: true as const,
      api_version: 1 as const,
      model: "gpt-6-astra",
      openai_configured: true,
      capabilities: ["layered_context"],
      existing_code_graph: { configured: false, readiness: "not_configured" },
    })),
  };

  it("shows only the memory graph; latest_retrieval is a compact status, validated by revision", async () => {
    const client = fakeClient({
      ...withCaps,
      graph: vi.fn(async (id: string) => ({
        ...graph(id, `${id} memory`),
        latest_retrieval: layered(id, 1),
      })) as never,
    });
    renderLive(client);
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    await screen.findByText("a memory");
    const status = screen.getByTestId("retrieval-status").textContent!;
    expect(status).toContain("Memory ready · r1");
    expect(status).toContain("Existing graph not connected");
    expect(status).toContain("Context prepared (partial)");
    expect(status).toContain("not confirmed");
    expect(screen.queryByLabelText("Existing code graph")).toBeNull();
    expect(document.querySelector(".code-node")).toBeNull();
    expect(document.querySelector(".memory-node.is-recalled")).toBeTruthy();
  });

  it("ignores a latest_retrieval for an older revision or another session", async () => {
    const client = fakeClient({
      ...withCaps,
      graph: vi.fn(async (id: string) => ({
        ...graph(id, `${id} memory`),
        revision: 2,
        latest_retrieval: layered(id, 1),
      })) as never,
    });
    renderLive(client);
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    await screen.findByText("a memory");
    expect(screen.getByTestId("retrieval-status").textContent).toContain(
      "No retrieval for the current memory (revision 2)",
    );
  });

  it("Preview next-turn context calls layered_context only when advertised and keeps layers separate", async () => {
    const client = fakeClient({
      ...withCaps,
      graph: vi.fn(async (id: string) => graph(id, `${id} memory`)) as never,
      layeredContext: vi.fn(async () => layered("a", 1, "ready")) as never,
    });
    renderLive(client);
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    await screen.findByText("a memory");
    fireEvent.click(screen.getByRole("button", { name: "Preview next-turn context" }));
    expect((await screen.findByTestId("layered-memory")).textContent).toContain("British English");
    expect(screen.getByTestId("layered-code").textContent).toContain("refunds.py");
    expect(screen.getByTestId("layered-memory").textContent).not.toContain("refunds.py");
    expect(client.context).not.toHaveBeenCalled();
  });

  it("older services without the capability use plain context", async () => {
    const client = fakeClient({
      graph: vi.fn(async (id: string) => graph(id, `${id} memory`)) as never,
      context: vi.fn(async (id: string) => ({
        session_id: id,
        revision: 1,
        text: "ctx",
        node_ids: [],
      })) as never,
      layeredContext: vi.fn() as never,
    });
    renderLive(client);
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    await screen.findByText("a memory");
    fireEvent.click(screen.getByRole("button", { name: "Preview next-turn context" }));
    await screen.findByText("ctx");
    expect(client.layeredContext).not.toHaveBeenCalled();
  });

  it("drops a stale layered preview after switching sessions", async () => {
    let resolveA: (v: unknown) => void = () => {};
    const client = fakeClient({
      ...withCaps,
      graph: vi.fn(async (id: string) => graph(id, `${id} memory`)) as never,
      layeredContext: vi.fn(() => new Promise((r) => (resolveA = r))) as never,
    });
    renderLive(client);
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    await screen.findByText("a memory");
    fireEvent.click(screen.getByRole("button", { name: "Preview next-turn context" }));
    fireEvent.click(screen.getByText("Beta chat"));
    await screen.findByText("b memory");
    await act(async () => resolveA(layered("a", 1)));
    expect(screen.getByTestId("retrieval-status").textContent).toContain("No retrieval");
  });

  it("withholds an r1 preview once polling advances the graph to r2 after a Forget", async () => {
    let rev = 1;
    const client = fakeClient({
      ...withCaps,
      graph: vi.fn(async (id: string) =>
        rev === 1
          ? graph(id, `${id} memory`)
          : {
              ...graph(id, `${id} memory`),
              revision: 2,
              nodes: [{ ...graph(id, "x").nodes[0]!, content: "Reply in British English", status: "forgotten" }],
            },
      ) as never,
      layeredContext: vi.fn(async () => layered("a", 1)) as never,
    });
    renderLive(client);
    await connect();
    fireEvent.click(await screen.findByText("Alpha chat"));
    await screen.findByText("a memory");
    fireEvent.click(screen.getByRole("button", { name: "Preview next-turn context" }));
    expect((await screen.findByTestId("layered-memory")).textContent).toContain("British English");
    rev = 2;
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await screen.findByTestId("layered-stale");
    expect(screen.queryByTestId("layered-memory")).toBeNull();
    expect(screen.getByTestId("layered-stale").textContent).toContain("revision 2");
  });
  it("test capture form sends only the user prompt (no assistant field)", async () => {
    const client = fakeClient({
      ensureSession: vi.fn(async () => ({ session_id: "a", revision: 4 })) as never,
      ingestTurn: vi.fn(async () => ({
        session_id: "a",
        revision: 5,
        status: "applied",
        changed_ids: [],
      })) as never,
      graph: vi.fn(async (id: string) => graph(id, `${id} memory`)) as never,
    });
    renderLive(client);
    await connect();
    await screen.findByText("Alpha chat");
    fireEvent.click(screen.getAllByRole("button", { name: /Send test prompt/ })[0]!);
    expect(screen.queryByLabelText(/Assistant/)).toBeNull();
    fireEvent.change(screen.getByLabelText("External session ID"), { target: { value: "x1" } });
    fireEvent.change(screen.getByLabelText("User prompt"), {
      target: { value: "When replying to me, use British English and em dashes." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send prompt" }));
    await screen.findByText(/Turn applied/);
    const sent = (client.ingestTurn as ReturnType<typeof vi.fn>).mock.calls[0]![0];
    expect(sent).not.toHaveProperty("assistant_message");
    expect(sent).toMatchObject({
      user_message: expect.stringContaining("British"),
      base_revision: 4,
    });
  });
});

