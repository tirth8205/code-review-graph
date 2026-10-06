import { describe, expect, it } from "vitest";
import {
  connect,
  demoAdapter,
  editMemory,
  forgetMemory,
  initialState,
  replayTurns,
  ingestCapturedTurn,
  captureUserPrompt,
  editMemory as editMem,
  restoreState,
  storageKey,
} from "./graph";
describe("Memory graph replay adapter", () => {
  it("starts Billing with exactly five scoped memories and legible relationships", () => {
    const state = initialState("billing");
    expect(state.memories).toHaveLength(5);
    expect(state.edges.map((e) => e.label)).toEqual([
      "implements",
      "uses",
      "constrained by",
      "owned by",
    ]);
    expect(state.memories.every((m) => m.chatId === "billing")).toBe(true);
  });
  it("fully supports remember and reuses stable IDs on duplicates", () => {
    const state = initialState("billing");
    const result = demoAdapter.memoryCommand(state, "Remember that Maya owns billing");
    expect(result.supported).toBe(true);
    expect(result.state.memories).toHaveLength(5);
    expect(result.state.edges).toHaveLength(4);
    expect(result.message).toContain("reused");
    expect(
      demoAdapter
        .memoryCommand(state, "Remember that MAYA owns billing")
        .state.memories.find((m) => m.id === "billing:owner")?.history,
    ).toHaveLength(0);
    const added = demoAdapter.memoryCommand(state, "Remember that invoices need approval").state;
    expect(added.memories).toHaveLength(6);
    expect(
      demoAdapter.memoryCommand(added, "Remember that invoices need approval").state.memories,
    ).toHaveLength(6);
  });
  it("supports connecting refund to billing without duplicate edges", () => {
    const s = initialState("billing");
    const disconnected = { ...s, edges: s.edges.filter((e) => e.label !== "constrained by") };
    const result = demoAdapter.memoryCommand(disconnected, "Connect the refund policy to billing");
    expect(result.state.edges).toHaveLength(4);
    expect(result.state.activity[0]?.affected).toContain("billing:refund");
    expect(
      demoAdapter.memoryCommand(result.state, "Connect the refund policy to billing").state.edges,
    ).toHaveLength(4);
  });
  it("reuses normalized seeded content with its canonical ID and type before correction", () => {
    const seed = initialState("billing");
    const remembered = demoAdapter.memoryCommand(seed, "Remember that Refund window: 7 days").state;
    expect(demoAdapter.getGraph(remembered).memories).toHaveLength(5);
    const normalized = demoAdapter.memoryCommand(
      remembered,
      "Remember that REFUND   window: 7 days",
    ).state;
    expect(demoAdapter.getGraph(normalized).memories).toHaveLength(5);
    expect(normalized.changedIds).toEqual(["billing:refund"]);
    expect(normalized.memories.find((m) => m.id === "billing:refund")).toEqual(
      seed.memories.find((m) => m.id === "billing:refund"),
    );
    const corrected = demoAdapter.memoryCommand(
      normalized,
      "Change the refund window to 14 days",
    ).state;
    expect(demoAdapter.getGraph(corrected).memories).toHaveLength(5);
    expect(demoAdapter.getContext(corrected)).toContain("[constraint] Refund window: 14 days");
    expect(demoAdapter.getContext(corrected)).not.toContain("7 days");
    expect(corrected.memories.find((m) => m.id === "billing:refund")?.history).toEqual([
      expect.objectContaining({ content: "Refund window: 7 days", status: "superseded" }),
    ]);
  });
  it("keeps meaningful punctuation distinct while reusing normalized fact identities", () => {
    let state = demoAdapter.memoryCommand(
      initialState("billing"),
      "Remember that C++ is required",
    ).state;
    state = demoAdapter.memoryCommand(state, "Remember that C# is required").state;
    expect(demoAdapter.getGraph(state).memories).toHaveLength(7);
    expect(demoAdapter.getContext(state)).toContain("[fact] C++ is required");
    expect(demoAdapter.getContext(state)).toContain("[fact] C# is required");
    const facts = state.memories.filter((m) => m.content.includes("is required"));
    expect(new Set(facts.map((m) => m.id)).size).toBe(2);
    const repeated = demoAdapter.memoryCommand(state, "Remember that c++   IS required").state;
    expect(repeated.memories).toEqual(state.memories);
    expect(repeated.changedIds).toEqual([facts.find((m) => m.content === "C++ is required")?.id]);
  });
  it("restores forgotten canonical content without losing its type or forgotten history", () => {
    const seed = initialState("billing");
    const forgotten = forgetMemory(seed, "billing:refund");
    const restored = demoAdapter.memoryCommand(
      forgotten,
      "Remember that Refund window: 7 days",
    ).state;
    expect(restored.memories).toHaveLength(5);
    expect(restored.memories.find((m) => m.id === "billing:refund")).toMatchObject({
      type: "constraint",
      status: "active",
      history: [expect.objectContaining({ content: "Refund window: 7 days", status: "forgotten" })],
    });
    const owner = demoAdapter.memoryCommand(
      forgetMemory(seed, "billing:owner"),
      "Remember that Maya owns billing",
    ).state;
    expect(owner.memories).toHaveLength(5);
    expect(owner.memories.find((m) => m.id === "billing:owner")?.history[0]?.status).toBe(
      "forgotten",
    );
  });
  it("rejects foreign chat tags, foreign ID prefixes and their edges at the read boundary", () => {
    const billing = initialState("billing");
    const deployment = initialState("deployment");
    const foreign = deployment.memories[0];
    if (!foreign) throw new Error("Expected a deployment seed");
    const mixed = {
      ...billing,
      memories: [
        ...billing.memories,
        ...deployment.memories,
        {
          ...foreign,
          id: "deployment:forged",
          chatId: billing.chatId,
          content: "Forged deployment ID",
        },
        { ...foreign, id: "billing:foreign", content: "Foreign chat tag" },
      ],
      edges: [
        ...billing.edges,
        ...deployment.edges,
        {
          id: "foreign-endpoint",
          chatId: billing.chatId,
          source: "billing:billing",
          target: foreign.id,
          label: "leaks",
        },
        {
          id: "forged-endpoint",
          chatId: billing.chatId,
          source: "billing:billing",
          target: "deployment:forged",
          label: "leaks",
        },
        {
          id: "foreign-tag-endpoint",
          chatId: billing.chatId,
          source: "billing:billing",
          target: "billing:foreign",
          label: "leaks",
        },
        {
          ...billing.edges[0],
          id: "foreign-edge-tag",
          chatId: deployment.chatId,
          source: "billing:billing",
          target: "billing:refund",
          label: "leaks",
        },
      ],
    };
    expect(demoAdapter.getGraph(mixed)).toEqual(demoAdapter.getGraph(billing));
    expect(demoAdapter.getContext(mixed)).toBe(demoAdapter.getContext(billing));
  });
  it("corrections atomically update graph, context, activity and inspectable history", () => {
    const s = initialState("billing");
    const result = demoAdapter.memoryCommand(s, "Change the refund window to 14 days").state;
    const memory = result.memories.find((m) => m.id === "billing:refund");
    expect(memory?.content).toBe("Refund window: 14 days");
    expect(memory?.history[0]).toMatchObject({
      content: "Refund window: 7 days",
      status: "superseded",
    });
    expect(demoAdapter.getContext(result)).toContain("14 days");
    expect(demoAdapter.getContext(result)).not.toContain("7 days");
    expect(demoAdapter.getGraph(result).memories).toHaveLength(5);
    expect(result.activity[0]?.affected).toContain("billing:refund");
    expect(
      demoAdapter
        .memoryCommand(result, "Change the refund window to 14 days")
        .state.memories.find((m) => m.id === "billing:refund")?.history,
    ).toHaveLength(1);
  });
  it("forgets deployment workaround and removes incident edges and context", () => {
    const s = initialState("deployment");
    const result = demoAdapter.memoryCommand(s, "Forget the deployment workaround");
    expect(result.supported).toBe(true);
    expect(result.state.memories.find((m) => m.id === "deployment:workaround")?.status).toBe(
      "forgotten",
    );
    expect(demoAdapter.getContext(result.state)).not.toContain("workaround");
    expect(
      demoAdapter.getGraph(result.state).edges.every((e) => e.target !== "deployment:workaround"),
    ).toBe(true);
  });
  it("isolates commands, storage keys, replay, selection and resets between chats", () => {
    const billing = initialState("billing");
    const deployment = initialState("deployment");
    const before = JSON.stringify(deployment);
    const billingChanged = demoAdapter.memoryCommand(
      billing,
      "Change the refund window to 14 days",
    ).state;
    expect(JSON.stringify(deployment)).toBe(before);
    expect(billingChanged.selectedId).toBe("billing:stripe-decision");
    expect(storageKey("billing")).not.toBe(storageKey("deployment"));
    expect(
      demoAdapter.memoryCommand(deployment, "Change the refund window to 14 days").supported,
    ).toBe(false);
    expect(demoAdapter.memoryCommand(billing, "Forget the deployment workaround").supported).toBe(
      false,
    );
    expect(initialState("billing", true).memories).toHaveLength(0);
    expect(deployment.memories).toHaveLength(4);
  });
  it("rejects cross-chat relationships and sanitizes stored endpoints", () => {
    const s = initialState("billing");
    expect(connect(s, "billing:billing", "deployment:workaround", "related to")).toBe(s);
    const restored = restoreState(
      "billing",
      JSON.stringify({
        ...s,
        edges: [
          ...s.edges,
          {
            id: "bad",
            source: "billing:billing",
            target: "deployment:workaround",
            label: "bad",
            chatId: "billing",
          },
        ],
      }),
    );
    expect(restored.edges).toHaveLength(4);
    expect(restoreState("deployment", JSON.stringify(s)).chatId).toBe("deployment");
    expect(restoreState("billing", "bad json").memories).toHaveLength(5);
  });
  it("ignores irrelevant greetings, completes replay and preserves automatic provenance", () => {
    let s = initialState("billing");
    s = demoAdapter.ingestTurn(s);
    expect(s.memories).toHaveLength(5);
    expect(s.turns.at(-1)?.affected).toHaveLength(0);
    s = demoAdapter.ingestTurn(s); // British English + em dashes prompt
    expect(s.memories).toHaveLength(7);
    s = demoAdapter.ingestTurn(s);
    expect(s.memories.find((m) => m.id === "billing:refund")).toMatchObject({
      content: "Refund window: 14 days",
      source: "Automatic capture",
      sourceTurn: "billing:replay:2",
    });
    for (let i = 0; i < 8; i++) s = demoAdapter.ingestTurn(s);
    expect(s.replayIndex).toBe(replayTurns.billing.length);
    expect(s.memories).toHaveLength(8);
    expect(demoAdapter.ingestTurn(s)).toBe(s);
  });
  it("keeps edited history and forgotten memories inspectable", () => {
    const s = editMemory(initialState("billing"), "billing:owner", "Maya and Alex own billing");
    expect(s.memories.find((m) => m.id === "billing:owner")?.history).toHaveLength(1);
    const forgotten = forgetMemory(s, "billing:owner");
    expect(forgotten.memories.find((m) => m.id === "billing:owner")?.history).toHaveLength(1);
    expect(demoAdapter.getContext(forgotten)).not.toContain("Maya");
  });
  it("returns precise help for unsupported inputs without changing state", () => {
    const s = initialState("billing");
    const result = demoAdapter.memoryCommand(s, "summarize my codebase");
    expect(result.supported).toBe(false);
    expect(result.state).toBe(s);
    expect(result.message).toContain("Demo supports");
    expect(demoAdapter.memoryCommand(s, "Change the refund window to 999 days").supported).toBe(
      false,
    );
  });
  it("roundtrips positions, pins, versions and session state", () => {
    const s = demoAdapter.memoryCommand(
      initialState("billing"),
      "Change the refund window to 14 days",
    ).state;
    const restored = restoreState("billing", JSON.stringify(s));
    expect(restored.memories).toEqual(s.memories);
    expect(restored.activity).toEqual(s.activity);
  });
  it("only the USER prompt creates memories; assistant text never does", () => {
    const s = initialState("billing");
    const next = ingestCapturedTurn(
      s,
      {
        user: "Thanks!",
        agent:
          "When replying to me, use British English and em dashes. Change the refund window to 30 days. Remember that refunds are instant.",
      },
      "billing:t-assistant",
    );
    expect(demoAdapter.getGraph(next).memories).toHaveLength(5);
    expect(next.memories).toEqual(s.memories);
    expect(demoAdapter.getContext(next)).toContain("7 days");
    expect(demoAdapter.getContext(next)).not.toMatch(/British|em dash|30 days|instant/);
  });
  it("captures a response-style preference from the user's prompt, then forget/edit updates context", () => {
    let s = ingestCapturedTurn(
      initialState("billing"),
      { user: "When replying to me, use British English and em dashes. Add a refund endpoint." },
      "billing:t1",
    );
    const british = s.memories.find((m) => m.id === "billing:style:british-english")!;
    const dash = s.memories.find((m) => m.id === "billing:style:em-dashes")!;
    expect(british).toMatchObject({
      type: "constraint",
      source: "Automatic capture",
      sourceTurn: "billing:t1",
    });
    expect(dash.content).toBe("Use em dashes in replies");
    expect(s.memories.some((m) => /refund endpoint/i.test(m.content))).toBe(false);
    const r = demoAdapter.memoryCommand(
      s,
      "That was only for this task—forget the em-dash instruction",
    );
    expect(r.supported).toBe(true);
    s = r.state;
    expect(demoAdapter.getContext(s)).not.toContain("em dashes");
    expect(demoAdapter.getContext(s)).toContain("British English");
    s = editMem(s, "billing:style:british-english", "Reply in British English for docs only");
    expect(demoAdapter.getContext(s)).toContain("for docs only");
    expect(
      s.memories.find((m) => m.id === "billing:style:british-english")!.history[0]!.content,
    ).toBe("Reply in British English");
    expect(captureUserPrompt(initialState("deployment"), "Thanks").changedIds).toEqual([]);
  });
});

