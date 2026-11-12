import { describe, expect, it } from "vitest";
import { demoAdapter, initialState } from "@/lib/memory/graph";
import {
  createSampleExistingGraphAdapter,
  createUnconnectedExistingGraphAdapter,
  MAX_CAPSULE_NODES,
  boundCapsule,
  SAMPLE_REPOSITORY,
  type GraphScope,
} from "./existing";
import {
  advance,
  demoScripts,
  liveTrace,
  memoryRevision,
  newRun,
  startTrace,
  stepDemo,
} from "./handoff";

const scope = (conversationId: string): GraphScope => ({
  ownerId: "local-owner",
  workspaceId: "demo",
  repositoryId: SAMPLE_REPOSITORY,
  conversationId,
});
const sample = createSampleExistingGraphAdapter();

function runAll(chat: "billing" | "deployment") {
  let run = newRun(chat);
  let memory = initialState(chat);
  const traces = [];
  for (let i = 0; i < demoScripts[chat]!.length; i++) {
    const r = stepDemo(run, memory, sample, scope(chat));
    run = r.run;
    memory = r.memory;
    traces.push(run.trace);
  }
  return { run, memory, traces };
}

describe("two independent graphs", () => {
  it("keeps code graph data out of memory state and memory out of code graph", () => {
    const code = sample.load(scope("billing"))!;
    const memory = initialState("billing");
    expect(code.nodes.every((n) => n.id.startsWith("code:"))).toBe(true);
    expect(memory.memories.some((m) => m.id.startsWith("code:"))).toBe(false);
    const memoryIds = new Set(memory.memories.map((m) => m.id));
    expect(code.edges.some((e) => memoryIds.has(e.source) || memoryIds.has(e.target))).toBe(false);
  });

  it("memory correction leaves the existing code graph unchanged", () => {
    const before = JSON.stringify(sample.load(scope("billing")));
    const { memory } = runAll("billing");
    expect(demoAdapter.getContext(memory)).toContain("14 days");
    expect(JSON.stringify(sample.load(scope("billing")))).toBe(before);
    expect(memory.memories.some((m) => m.id.startsWith("code:"))).toBe(false);
  });
});

describe("ordered handoff", () => {
  it("runs recall -> search -> prepare in order and highlights refund validation", () => {
    const memory = initialState("billing");
    let t = startTrace("billing", "Where should refund validation change?");
    const stages: string[] = [];
    for (let i = 0; i < 4; i++) {
      t = advance(t, memory, sample, scope("billing"));
      stages.push(t.stage);
    }
    expect(stages).toEqual(["recall", "search", "prepare", "done"]);
    expect(t.capsule!.nodeIds).toContain("billing:refund");
    expect(t.retrieval!.hits.map((h) => h.nodeId)).toContain("code:validate-refund");
    expect(t.preparedContext).toContain("Refund window: 7 days");
    expect(t.preparedContext).toContain("validateRefundWindow()");
  });

  it("the Continue turn carries the corrected revision and 14-day context before search", () => {
    const { traces, memory } = runAll("billing");
    const first = traces[4]!;
    const second = traces[traces.length - 1]!;
    expect(first.capsule!.text).toContain("7 days");
    expect(second.prompt).toBe("Continue");
    expect(second.capsule!.text).toContain("14 days");
    expect(second.capsule!.text).not.toContain("7 days");
    expect(second.capsule!.revision).toBe(memoryRevision(memory));
    expect(second.capsule!.revision).not.toBe(first.capsule!.revision);
    expect(second.retrieval!.capsuleRevision).toBe(second.capsule!.revision);
    expect(second.preparedContext).toContain("14 days");
    const refund = memory.memories.find((m) => m.id === "billing:refund")!;
    expect(refund.history.map((h) => h.content)).toContain("Refund window: 7 days");
  });

  it("bounds the capsule", () => {
    const big = boundCapsule({
      conversationId: "billing",
      revision: "x",
      nodeIds: Array.from({ length: 50 }, (_, i) => `billing:${i}`),
      text: "a".repeat(20000),
    });
    expect(big.nodeIds).toHaveLength(MAX_CAPSULE_NODES);
    expect(big.text.length).toBe(12000);
  });
});

describe("live missing connector", () => {
  const live = createUnconnectedExistingGraphAdapter();
  it("loads nothing and never fabricates retrieval", () => {
    expect(live.load(scope("s1"))).toBeNull();
    expect(live.status().label).toBe("Existing graph not connected");
    const t = liveTrace(
      "s1",
      { session_id: "s1", revision: 4, text: "x", node_ids: ["n1"] },
      live,
    )!;
    expect(t.retrieval).toBeNull();
    expect(t.stoppedAt).toBe("search");
    expect(t.preparedContext).toBeNull();
    expect(t.capsule!.revision).toBe("r4");
  });
  it("stops the demo-style trace at search when the adapter is unconnected", () => {
    const memory = initialState("billing");
    let t = startTrace("billing", "refund");
    for (let i = 0; i < 5; i++) t = advance(t, memory, live, scope("billing"));
    expect(t.stage).toBe("search");
    expect(t.retrieval).toBeNull();
    expect(t.preparedContext).toBeNull();
  });
  it("rejects a context response for another session", () => {
    expect(
      liveTrace("s1", { session_id: "s2", revision: 1, text: "", node_ids: [] }, live),
    ).toBeNull();
  });
});

describe("session guards", () => {
  it("ignores memory or scope from another conversation", () => {
    const t = startTrace("billing", "refund");
    expect(advance(t, initialState("deployment"), sample, scope("deployment"))).toBe(t);
    expect(advance(t, initialState("billing"), sample, scope("deployment"))).toBe(t);
  });
  it("a run cannot step against another chat's memory", () => {
    const run = newRun("billing");
    const r = stepDemo(run, initialState("deployment"), sample, scope("billing"));
    expect(r.done).toBe(true);
    expect(r.run.trace).toBeNull();
  });
  it("deployment demo retrieves only deployment code and does not touch billing", () => {
    const { traces } = runAll("deployment");
    const last = traces[traces.length - 1]!;
    expect(last.retrieval!.hits.map((h) => h.nodeId)).toContain("code:health-check");
    expect(last.capsule!.nodeIds.every((id) => id.startsWith("deployment:"))).toBe(true);
  });
});

