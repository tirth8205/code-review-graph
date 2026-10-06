import { render, screen, waitFor } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { LiveWorkspace } from "./LiveWorkspace";
import { initialMode, MODE_KEY } from "./MemoryWorkspace";
import { liveStorage, type LiveClient } from "@/lib/memory/live";

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});
afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

const g = { session_id: "ws", revision: 1, nodes: [], edges: [], turns: [] };
function client(fail = false) {
  return {
    health: vi.fn(async () => {
      if (fail) throw new Error("down");
      return { ok: true, api_version: 1, model: "m", openai_configured: false };
    }),
    listSessions: vi.fn(async () => ({
      sessions: [
        {
          id: "ws",
          title: "Agent memory",
          source: "local",
          external_session_id: "w",
          revision: 1,
          updated_at: "",
        },
      ],
    })),
    graph: vi.fn(async () => g),
    context: vi.fn(),
    layeredContext: vi.fn(),
    ensureSession: vi.fn(),
    ingestTurn: vi.fn(),
    command: vi.fn(),
  } as unknown as LiveClient;
}
const mount = (factory: ReturnType<typeof vi.fn>) =>
  render(
    <ReactFlowProvider>
      <LiveWorkspace clientFactory={factory as never} />
    </ReactFlowProvider>,
  );

describe("live reconnect on reload", () => {
  it("auto-reconnects once with the saved URL and tab credential", async () => {
    localStorage.setItem(liveStorage.urlKey, "http://127.0.0.1:9999/");
    sessionStorage.setItem(liveStorage.tokenKey, "tok");
    const c = client();
    const factory = vi.fn(() => c);
    mount(factory);
    await waitFor(() => expect(c.listSessions).toHaveBeenCalled());
    expect(factory).toHaveBeenCalledTimes(1);
    expect(factory).toHaveBeenCalledWith({ baseUrl: "http://127.0.0.1:9999/", token: "tok" });
    expect(localStorage.getItem(liveStorage.tokenKey)).toBeNull();
  });

  it("without a credential shows setup and makes no calls", async () => {
    localStorage.setItem(liveStorage.urlKey, "http://127.0.0.1:9999/");
    const factory = vi.fn(() => client());
    mount(factory);
    expect(await screen.findByRole("button", { name: /test and connect/i })).toBeTruthy();
    expect(factory).not.toHaveBeenCalled();
  });

  it("errors stay in live mode with retry available", async () => {
    localStorage.setItem(liveStorage.urlKey, "http://127.0.0.1:9999/");
    sessionStorage.setItem(liveStorage.tokenKey, "tok");
    const c = client(true);
    mount(vi.fn(() => c));
    await waitFor(() => expect(c.health).toHaveBeenCalled());
    expect(screen.queryByText(/Sample replay/)).toBeNull();
    expect(c.listSessions).not.toHaveBeenCalled();
  });
});

describe("initial mode", () => {
  it("?mode=live selects live without touching the endpoint", () => {
    localStorage.setItem(liveStorage.urlKey, "http://127.0.0.1:9999/");
    window.history.replaceState(null, "", "/?mode=live");
    expect(initialMode()).toBe("live");
    expect(localStorage.getItem(liveStorage.urlKey)).toBe("http://127.0.0.1:9999/");
  });
  it("persisted mode and tab credential default", () => {
    expect(initialMode()).toBe("demo");
    sessionStorage.setItem(liveStorage.tokenKey, "t");
    expect(initialMode()).toBe("live");
    localStorage.setItem(MODE_KEY, "demo");
    expect(initialMode()).toBe("demo");
  });
});
