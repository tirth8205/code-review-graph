/* eslint-disable @typescript-eslint/no-explicit-any -- untyped JSON fixtures in tests */
import { describe, expect, it, vi } from "vitest";
import {
  createCommandRequestIds,
  createLiveClient,
  createRequestGate,
  isAutomaticSource,
  isUncertain,
  MemoryApiError,
  setupHint,
} from "./live";

const ok = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const health = { ok: true, api_version: 1, model: "gpt-6-astra", openai_configured: false };
const node = (id: string) => ({
  id,
  type: "fact",
  content: id,
  status: "active",
  source_turn_id: "t1",
  source_kind: "turn",
  evidence: "e",
  pinned: false,
  history: [],
});

describe("live memory client", () => {
  it("sends health without credentials and other actions as bearer POST bodies", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(ok(health))
      .mockResolvedValueOnce(ok({ sessions: [] }));
    const client = createLiveClient({
      baseUrl: "http://127.0.0.1:8787",
      token: "mlb_synthetic",
      fetch,
    });
    await client.health();
    await client.listSessions();
    const [, healthInit] = fetch.mock.calls[0]!;
    const [url, listInit] = fetch.mock.calls[1]!;
    expect(url).toBe("http://127.0.0.1:8787/");
    expect(healthInit.headers.Authorization).toBeUndefined();
    expect(listInit.method).toBe("POST");
    expect(listInit.headers.Authorization).toBe("Bearer mlb_synthetic");
    expect(listInit.headers.apikey).toBeUndefined();
    expect(JSON.parse(listInit.body)).toEqual({ action: "list_sessions" });
  });

  it("adds a fresh request_id to commands", async () => {
    const fetch = vi.fn(async () =>
      ok({ session_id: "s", revision: 2, status: "applied", changed_ids: [], message: "ok" }),
    );
    const client = createLiveClient({ baseUrl: "https://h", token: "t", fetch });
    await client.command({ session_id: "s", command: "a" });
    await client.command({ session_id: "s", command: "a" });
    const ids = fetch.mock.calls.map((c: any) => JSON.parse(c[1].body).request_id);
    expect(ids[0]).toBeTruthy();
    expect(ids[0]).not.toBe(ids[1]);
  });

  it("rejects malformed responses", async () => {
    const client = createLiveClient({
      baseUrl: "https://h",
      token: "t",
      fetch: async () => ok({ sessions: [{ id: 1 }] }),
    });
    await expect(client.listSessions()).rejects.toMatchObject({ code: "malformed_response" });
    const notJson = createLiveClient({
      baseUrl: "https://h",
      token: "t",
      fetch: async () => new Response("<html>"),
    });
    await expect(notJson.health()).rejects.toMatchObject({ code: "malformed_response" });
  });

  it("maps auth errors without exposing the token", async () => {
    const client = createLiveClient({
      baseUrl: "https://h",
      token: "mlb_secret",
      fetch: async () => ok({ error: { code: "unauthorized", message: "expired" } }, 401),
    });
    const error = await client.listSessions().catch((e) => e);
    expect(error).toBeInstanceOf(MemoryApiError);
    expect(error.code).toBe("unauthorized");
    expect(setupHint(error) + error.message).not.toContain("mlb_secret");
    await expect(
      createLiveClient({ baseUrl: "https://h", token: "", fetch: vi.fn() }).listSessions(),
    ).rejects.toMatchObject({ code: "unauthorized" });
  });

  it("maps network/CORS failures and no-key errors to setup guidance", async () => {
    const client = createLiveClient({
      baseUrl: "https://h",
      token: "t",
      fetch: async () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const error = await client.health().catch((e) => e);
    expect(error.code).toBe("network");
    expect(setupHint(error)).toMatch(/CORS/);
    const noKey = createLiveClient({
      baseUrl: "https://h",
      token: "t",
      fetch: async () => ok({ error: { code: "openai_not_configured", message: "x" } }, 503),
    });
    expect(
      setupHint(await noKey.command({ session_id: "s", command: "c" }).catch((e) => e)),
    ).toMatch(/no OpenAI key/);
  });

  it("validates graph session and drops edges to foreign nodes", async () => {
    const graph = {
      session_id: "a",
      revision: 3,
      nodes: [node("n1"), node("n2")],
      edges: [
        { id: "e1", source: "n1", target: "n2", label: "x" },
        { id: "e2", source: "n1", target: "foreign", label: "x" },
      ],
      turns: [],
    };
    const client = createLiveClient({
      baseUrl: "https://h",
      token: "t",
      fetch: async () => ok(graph),
    });
    expect((await client.graph("a")).edges.map((e) => e.id)).toEqual(["e1"]);
    await expect(client.graph("b")).rejects.toMatchObject({ code: "malformed_response" });
  });

  it("rejects invalid URLs and URLs with embedded credentials", () => {
    expect(() => createLiveClient({ baseUrl: "not a url", token: "t" })).toThrow(MemoryApiError);
    expect(() => createLiveClient({ baseUrl: "ftp://h", token: "t" })).toThrow(MemoryApiError);
    expect(() => createLiveClient({ baseUrl: "http://u:p@h", token: "t" })).toThrow(MemoryApiError);
  });

  it("request gate only accepts the latest ticket", () => {
    const gate = createRequestGate();
    const a = gate.next();
    const b = gate.next();
    expect(gate.isCurrent(a)).toBe(false);
    expect(gate.isCurrent(b)).toBe(true);
    gate.invalidate();
    expect(gate.isCurrent(b)).toBe(false);
  });
});

describe("ingest_turn base_revision", () => {
  it("sends base_revision when provided and omits it otherwise", async () => {
    const fetch = vi.fn(async () =>
      ok({ session_id: "s", revision: 1, status: "noop", changed_ids: [] }),
    );
    const client = createLiveClient({ baseUrl: "https://h", token: "t", fetch });
    await client.ingestTurn({ session_id: "s", turn_id: "t", user_message: "u", base_revision: 0 });
    await client.ingestTurn({ session_id: "s", turn_id: "t2", user_message: "u" });
    const bodies = fetch.mock.calls.map((c: any) => JSON.parse(c[1].body));
    expect(bodies[0].base_revision).toBe(0);
    expect("base_revision" in bodies[1]).toBe(false);
  });
  it("rejects negative or fractional base_revision before any request", async () => {
    const fetch = vi.fn();
    const client = createLiveClient({ baseUrl: "https://h", token: "t", fetch });
    for (const base_revision of [-1, 1.5, Number.NaN])
      await expect(
        client.ingestTurn({ session_id: "s", turn_id: "t", user_message: "u", base_revision }),
      ).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("review fixes at the client boundary", () => {
  const cmd = { session_id: "s", revision: 2, status: "applied", changed_ids: [], message: "ok" };
  it("sends tokens only over HTTPS or loopback HTTP, with redirect: error", async () => {
    for (const baseUrl of ["http://example.com", "http://10.0.0.5:8787", "http://127.evil.com"])
      expect(() => createLiveClient({ baseUrl, token: "t" })).toThrow(
        expect.objectContaining({ code: "insecure_transport" }),
      );
    for (const baseUrl of [
      "http://127.0.0.1:8787",
      "http://localhost:8787",
      "http://[::1]:8787",
      "https://api.example",
    ]) {
      const fetch = vi.fn(async () => ok({ sessions: [] }));
      await createLiveClient({ baseUrl, token: "t", fetch }).listSessions();
      expect((fetch.mock.calls[0] as any)[1].redirect).toBe("error");
    }
  });

  it("rejects context/command/ingest responses for another session", async () => {
    const client = (body: unknown) =>
      createLiveClient({ baseUrl: "https://h", token: "t", fetch: async () => ok(body) });
    await expect(
      client({ ...cmd, session_id: "other" }).command({ session_id: "s", command: "c" }),
    ).rejects.toMatchObject({ code: "malformed_response" });
    await expect(
      client({ session_id: "other", revision: 1, text: "", node_ids: [] }).context("s"),
    ).rejects.toMatchObject({ code: "malformed_response" });
    await expect(
      client({ session_id: "other", revision: 1, status: "applied", changed_ids: [] }).ingestTurn({
        session_id: "s",
        turn_id: "t",
        user_message: "u",
      }),
    ).rejects.toMatchObject({ code: "malformed_response" });
  });

  it("drops foreign-prefixed or foreign-tagged nodes before deriving edges", async () => {
    const graph = {
      session_id: "a",
      revision: 1,
      nodes: [node("a:1"), node("b:2"), { ...node("x3"), session_id: "b" }],
      edges: [
        { id: "e1", source: "a:1", target: "b:2", label: "x" },
        { id: "e2", source: "a:1", target: "x3", label: "x" },
      ],
      turns: [],
    };
    const g = await createLiveClient({
      baseUrl: "https://h",
      token: "t",
      fetch: async () => ok(graph),
    }).graph("a", undefined, ["a", "b"]);
    expect(g.nodes.map((n) => n.id)).toEqual(["a:1"]);
    expect(g.edges).toEqual([]);
  });

  it("reuses a request_id across uncertain retries of the same command until settled", () => {
    let n = 0;
    const ids = createCommandRequestIds(() => `r${++n}`);
    expect(ids.idFor("s", "forget x")).toBe("r1");
    expect(ids.idFor("s", "forget x")).toBe("r1"); // retry after lost response
    expect(ids.idFor("s", "forget y")).toBe("r2"); // edited command
    expect(ids.idFor("s2", "forget y")).toBe("r3"); // other session
    ids.settle("s2", "forget y");
    expect(ids.idFor("s2", "forget y")).toBe("r4");
    expect(isUncertain(new MemoryApiError("network", "x"))).toBe(true);
    expect(isUncertain(new MemoryApiError("conflicting_request", "x"))).toBe(false);
  });

  it("treats user and turn source kinds as automatic capture", () => {
    expect(isAutomaticSource("user")).toBe(true);
    expect(isAutomaticSource("turn")).toBe(true);
    expect(isAutomaticSource("command")).toBe(false);
  });
});

import { createLiveClient as mk, currentRetrieval as cur } from "./live";
describe("layered_context client", () => {
  const dto = (sid: string, rev: number) => ({
    session_id: sid,
    revision: rev,
    node_ids: [],
    text: "",
    memory: { status: "ready", session_id: sid, revision: rev, node_ids: [], text: "" },
    code_graph: { status: "not_configured" },
    stages: [{ stage: "memory", order: 1, status: "ready", revision: rev }],
    retrieved_at: "t",
  });
  it("posts layered_context with bounded limit and rejects long queries / foreign sessions", async () => {
    const calls: unknown[] = [];
    const fetch = async (_: string, init?: RequestInit) => {
      calls.push(JSON.parse(String(init!.body)));
      return new Response(JSON.stringify(dto("s1", 3)), { status: 200 });
    };
    const c = mk({ baseUrl: "http://127.0.0.1:8787", token: "t", fetch });
    await c.layeredContext({ session_id: "s1", limit: 99 });
    expect(calls[0]).toMatchObject({ action: "layered_context", session_id: "s1", limit: 20 });
    await expect(
      c.layeredContext({ session_id: "s1", query: "x".repeat(501) }),
    ).rejects.toMatchObject({ code: "invalid_input" });
    await expect(c.layeredContext({ session_id: "s2" })).rejects.toMatchObject({
      code: "malformed_response",
    });
  });
  it("currentRetrieval requires matching session and revision", () => {
    const d = dto("s1", 3) as never;
    expect(cur(d, "s1", 3)).toBe(d);
    expect(cur(d, "s1", 4)).toBeNull();
    expect(cur(d, "s2", 3)).toBeNull();
    expect(cur(null, "s1", 3)).toBeNull();
  });
});

