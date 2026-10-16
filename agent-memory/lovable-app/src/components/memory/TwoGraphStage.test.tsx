import { act, fireEvent, render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TwoGraphStage } from "./TwoGraphStage";
import { MemoryWorkspace } from "./MemoryWorkspace";
import { createUnconnectedExistingGraphAdapter } from "@/lib/codegraph/existing";
import { initialState } from "@/lib/memory/graph";
import { advance, startTrace } from "@/lib/codegraph/handoff";
import { createSampleExistingGraphAdapter, SAMPLE_REPOSITORY } from "@/lib/codegraph/existing";

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});
beforeEach(() => localStorage.clear());

const sample = createSampleExistingGraphAdapter();
const scope = {
  ownerId: "o",
  workspaceId: "w",
  repositoryId: SAMPLE_REPOSITORY,
  conversationId: "billing",
};
function searchTrace() {
  let t = startTrace("billing", "Where should refund validation change?");
  t = advance(t, initialState("billing"), sample, scope);
  return advance(t, initialState("billing"), sample, scope);
}

describe("TwoGraphStage", () => {
  it("renders a static capsule under reduced motion and animates otherwise", () => {
    const props = {
      memoryCanvas: <div />,
      memoryScope: "Billing",
      trace: searchTrace(),
      codeGraph: sample.load(scope),
      codeStatus: sample.status(),
    };
    const { rerender } = render(<TwoGraphStage {...props} reducedMotion />);
    expect(screen.getByTestId("handoff-capsule").className).toContain("is-static");
    rerender(<TwoGraphStage {...props} reducedMotion={false} />);
    expect(screen.getByTestId("handoff-capsule").className).not.toContain("is-static");
    expect(screen.getByTestId("handoff-capsule").className).toContain("is-crossing");
  });

  it("shows the unconnected live connector without code nodes", () => {
    const live = createUnconnectedExistingGraphAdapter();
    render(
      <TwoGraphStage
        memoryCanvas={<div />}
        memoryScope="x"
        trace={null}
        codeGraph={live.load(scope)}
        codeStatus={live.status()}
        reducedMotion
      />,
    );
    expect(screen.getAllByText("Existing graph not connected").length).toBeGreaterThan(0);
    expect(document.querySelector(".code-node")).toBeNull();
  });
});

describe("demo replay controls", () => {
  it("steps the ordered handoff, applies the correction, and keeps memory-only view", async () => {
    render(
      <ReactFlowProvider>
        <MemoryWorkspace />
      </ReactFlowProvider>,
    );
    const step = () => fireEvent.click(screen.getByRole("button", { name: "Step" }));
    await act(async () => {});
    expect(screen.queryByLabelText("Existing code graph")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "How it works (demo)" }));
    step();
    expect(screen.getByText(/Captured prompt queued/)).toBeTruthy();
    step();
    expect(screen.getByRole("listitem", { current: "step" }).textContent).toContain(
      "Recall memory",
    );
    step();
    expect(screen.getByRole("listitem", { current: "step" }).textContent).toContain(
      "Search existing graph",
    );
    step();
    expect(screen.getByText(/Context prepared · sample next-turn context/)).toBeTruthy();
    expect(screen.getByText(/validateRefundWindow\(\): src/)).toBeTruthy();
    step();
    step(); // correction
    expect(screen.getByText("Refund window: 14 days")).toBeTruthy();
    for (let i = 0; i < 5; i++) step();
    expect(screen.getAllByText(/^Memory revision m-/)[0]).toBeTruthy();
    const pre = document.querySelector(".context-reveal pre")!.textContent!;
    expect(pre).toContain("14 days");
    expect(pre).not.toContain("7 days");
    expect((screen.getByRole("button", { name: "Step" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Replay" }));
    expect(document.querySelector(".context-reveal")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Memory" }));
    expect(screen.queryByLabelText("Existing code graph")).toBeNull();
    expect(screen.getByLabelText("Memory graph")).toBeTruthy();
  });

  it("play advances on a timer and pause stops it; switching chats clears nothing in the other chat", async () => {
    vi.useFakeTimers();
    try {
      render(
        <ReactFlowProvider>
          <MemoryWorkspace />
        </ReactFlowProvider>,
      );
      await act(async () => {});
      fireEvent.click(screen.getByRole("radio", { name: "How it works (demo)" }));
      fireEvent.click(screen.getByRole("button", { name: "Play the 60-second demo" }));
      await act(async () => vi.advanceTimersByTime(400));
      expect(screen.getByText(/1\/11/)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Pause" }));
      await act(async () => vi.advanceTimersByTime(20000));
      expect(screen.getByText(/1\/11/)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: /Deployment fix/ }));
      expect(screen.queryByText(/1\/11/)).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: /Billing feature/ }));
      expect(screen.getByText(/1\/11/)).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});

