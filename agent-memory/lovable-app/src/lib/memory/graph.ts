/**
 * ONE shared memory graph per user/local workspace. It grows across every tool and native
 * chat. Native chat and tool IDs are SOURCE PROVENANCE on captured turns and memories, never
 * graph partitions. `workspaceId` scopes one owner's graph so separate users never merge.
 */
export type WorkspaceId = string;
export const DEMO_WORKSPACE: WorkspaceId = "workspace";
export type MemoryType = "entity" | "fact" | "decision" | "constraint" | "task";
export type HistoryEntry = {
  content: string;
  time: string;
  status: "superseded" | "forgotten";
  sourceTurn?: string | undefined;
  sourceLabel?: string | undefined;
};
export type Memory = {
  id: string;
  workspaceId: WorkspaceId;
  type: MemoryType;
  content: string;
  status: "active" | "forgotten";
  source: "Automatic capture" | "User-added";
  sourceTurn: string;
  /** Provenance, for example "Codex CLI · billing chat". */
  sourceLabel?: string | undefined;
  time: string;
  pinned: boolean;
  position: { x: number; y: number };
  history: HistoryEntry[];
};
export type Relation = {
  id: string;
  source: string;
  target: string;
  label: string;
  workspaceId: WorkspaceId;
};
export type CapturedTurn = {
  id: string;
  /** Tool that captured the prompt (CLI, editor, chat app). Provenance only. */
  tool: string;
  /** Native chat/session inside that tool. Provenance only. */
  nativeChat: string;
  user: string;
  agent: string;
  affected: string[];
  time: string;
};
export type Activity = { id: string; text: string; time: string; affected: string[] };
export type GraphState = {
  workspaceId: WorkspaceId;
  memories: Memory[];
  edges: Relation[];
  turns: CapturedTurn[];
  activity: Activity[];
  replayIndex: number;
  selectedId: string | null;
  changedIds: string[];
};
export type CommandResult = { state: GraphState; message: string; supported: boolean };
export interface MemoryGraphAdapter {
  mode: "Demo replay";
  getGraph(state: GraphState): { memories: Memory[]; edges: Relation[] };
  getContext(state: GraphState): string;
  memoryCommand(state: GraphState, input: string): CommandResult;
  ingestTurn(state: GraphState): GraphState;
}
export const demoCommands = [
  "Remember that Maya owns billing",
  "Connect the refund policy to billing",
  "Change the refund window to 14 days",
  "Forget the deployment workaround",
  "That was only for this task, forget the em-dash instruction",
];
export const sourceLabel = (turn: { tool: string; nativeChat: string }) =>
  `${turn.tool} · ${turn.nativeChat}`;
const COMMAND_LABEL = "You · memory command";
const time = () => new Date().toISOString();
const idFor = (workspaceId: WorkspaceId, key: string) => `${workspaceId}:${key}`;
const normalizeContent = (content: string) => content.toLowerCase().replace(/\s+/g, " ").trim();
const belongs = (memory: Memory, workspaceId: WorkspaceId) =>
  memory.workspaceId === workspaceId && memory.id.startsWith(`${workspaceId}:`);
function record(state: GraphState, text: string, affected: string[]): GraphState {
  const stamp = time();
  return {
    ...state,
    changedIds: affected,
    activity: [
      { id: `${stamp}:${state.activity.length}`, text, affected, time: stamp },
      ...state.activity,
    ].slice(0, 100),
  };
}
function upsert(
  state: GraphState,
  key: string,
  content: string,
  type: MemoryType,
  source: Memory["source"],
  sourceTurn: string,
  position?: Memory["position"],
  label = COMMAND_LABEL,
): GraphState {
  const id = idFor(state.workspaceId, key);
  const existing = state.memories.find((m) => m.id === id);
  if (
    existing &&
    normalizeContent(existing.content) === normalizeContent(content) &&
    existing.status === "active"
  )
    return state;
  const stamp = time();
  const n = state.memories.length;
  const memory: Memory = existing
    ? {
        ...existing,
        content,
        type,
        status: "active",
        source,
        sourceTurn,
        sourceLabel: label,
        time: stamp,
        history: [
          ...existing.history,
          {
            content: existing.content,
            time: existing.time,
            status: existing.status === "forgotten" ? "forgotten" : "superseded",
            sourceTurn: existing.sourceTurn,
            sourceLabel: existing.sourceLabel,
          },
        ],
      }
    : {
        id,
        workspaceId: state.workspaceId,
        type,
        content,
        status: "active",
        source,
        sourceTurn,
        sourceLabel: label,
        time: stamp,
        pinned: false,
        position: position ?? {
          x: 1010 + (n % 2) * 290,
          y: 60 + Math.floor(Math.max(0, n - 8) / 2) * 150,
        },
        history: [],
      };
  return {
    ...state,
    memories: existing
      ? state.memories.map((m) => (m.id === id ? memory : m))
      : [...state.memories, memory],
  };
}
export function connect(
  state: GraphState,
  source: string,
  target: string,
  label: string,
): GraphState {
  if (
    source === target ||
    ![source, target].every((id) =>
      state.memories.some(
        (m) => m.id === id && belongs(m, state.workspaceId) && m.status === "active",
      ),
    )
  )
    return state;
  const id = `${source}/${label}/${target}`;
  if (state.edges.some((e) => e.id === id)) return state;
  return record(
    {
      ...state,
      edges: [...state.edges, { id, workspaceId: state.workspaceId, source, target, label }],
    },
    `Connected memories · ${label}`,
    [source, target],
  );
}
export function editMemory(state: GraphState, id: string, content: string): GraphState {
  const memory = state.memories.find((m) => m.id === id);
  if (!memory || !content.trim() || memory.status !== "active") return state;
  const updated = upsert(
    state,
    id.split(":").slice(1).join(":"),
    content.trim(),
    memory.type,
    "User-added",
    "Memory command",
    memory.position,
  );
  return record(
    updated,
    updated === state
      ? "Memory already up to date"
      : "Corrected memory · previous version retained",
    [id],
  );
}
export function forgetMemory(state: GraphState, id: string): GraphState {
  if (!state.memories.some((m) => m.id === id && m.status === "active")) return state;
  return record(
    {
      ...state,
      memories: state.memories.map((m) => (m.id === id ? { ...m, status: "forgotten" } : m)),
    },
    "Forgot memory · excluded from next-turn context",
    [id],
  );
}
type SeedTurn = {
  key: string;
  tool: string;
  nativeChat: string;
  user: string;
  agent: string;
  memories: [string, string, MemoryType, number, number][];
};
const seedTurns: SeedTurn[] = [
  {
    key: "seed-cli",
    tool: "Codex CLI",
    nativeChat: "billing chat",
    user: "Build billing with Stripe. Refunds are allowed for 7 days.",
    agent: "I’ll use Stripe for billing with a 7-day refund policy.",
    memories: [
      ["billing", "Billing", "entity", 80, 200],
      ["stripe", "Stripe", "entity", 680, 40],
      ["stripe-decision", "Use Stripe for billing", "decision", 380, 40],
      ["refund", "Refund window: 7 days", "constraint", 380, 330],
    ],
  },
  {
    key: "seed-editor",
    tool: "Editor",
    nativeChat: "deploy config chat",
    user: "Deploy to Cloudflare Workers. Pin Node to v22 as a workaround and verify production.",
    agent: "I’ll target Workers, pin Node to v22, and verify the deployment.",
    memories: [
      ["deployment", "Deployment", "entity", 80, 560],
      ["workaround", "Deployment workaround: pin Node to v22", "constraint", 380, 620],
      ["platform", "Cloudflare Workers", "entity", 680, 520],
      ["deploy-task", "Verify production deployment", "task", 680, 330],
    ],
  },
];
export function initialState(workspaceId: WorkspaceId = DEMO_WORKSPACE, empty = false): GraphState {
  let state: GraphState = {
    workspaceId,
    memories: [],
    edges: [],
    turns: [],
    activity: [],
    replayIndex: 0,
    selectedId: null,
    changedIds: [],
  };
  if (empty) return state;
  const stamp = time();
  for (const turn of seedTurns) {
    const turnId = idFor(workspaceId, turn.key);
    for (const [key, content, type, x, y] of turn.memories)
      state = upsert(
        state,
        key,
        content,
        type,
        "Automatic capture",
        turnId,
        { x, y },
        sourceLabel(turn),
      );
    state.turns = [
      ...state.turns,
      {
        id: turnId,
        tool: turn.tool,
        nativeChat: turn.nativeChat,
        user: turn.user,
        agent: turn.agent,
        affected: turn.memories.map(([key]) => idFor(workspaceId, key)),
        time: stamp,
      },
    ];
  }
  const relationships: [string, string, string][] = [
    ["stripe-decision", "billing", "implements"],
    ["stripe-decision", "stripe", "uses"],
    ["billing", "refund", "constrained by"],
    ["deployment", "workaround", "constrained by"],
    ["deployment", "platform", "runs on"],
    ["deploy-task", "deployment", "verifies"],
  ];
  for (const [a, b, label] of relationships)
    state = connect(state, idFor(workspaceId, a), idFor(workspaceId, b), label);
  return {
    ...state,
    selectedId: idFor(workspaceId, "stripe-decision"),
    changedIds: [],
    activity: [
      {
        id: "seed",
        text: "Captured earlier prompts from two tools into one memory graph",
        time: stamp,
        affected: state.memories.map((m) => m.id),
      },
    ],
  };
}
export type ReplayTurn = { tool: string; nativeChat: string; user: string; agent: string };
/**
 * Pre-authored replay of captured prompts from DIFFERENT tools and native chats, all feeding
 * the same graph. Only `user` (captured on UserPromptSubmit) may produce memories; `agent` is
 * the assistant's work output and is displayed as read-only evidence but never interpreted.
 */
export const replayTurns: ReplayTurn[] = [
  {
    tool: "Codex CLI",
    nativeChat: "billing chat",
    user: "Thanks, that looks good!",
    agent: "You’re welcome.",
  },
  {
    tool: "Codex CLI",
    nativeChat: "billing chat",
    user: "When replying to me, use British English and em dashes. Now add a refund endpoint.",
    agent: "Added POST /refunds. It validates the window before calling Stripe.",
  },
  {
    tool: "Editor",
    nativeChat: "refunds.ts chat",
    user: "Remember that Maya owns billing.",
    agent: "Noted. Maya remains the billing owner.",
  },
  {
    tool: "Chat app",
    nativeChat: "planning chat",
    user: "Change the refund window to 14 days.",
    agent: "Updated the refund check and its tests.",
  },
  {
    tool: "Codex CLI",
    nativeChat: "webhooks chat",
    user: "Remember that billing needs webhook verification.",
    agent: "Added a webhook signature check to the billing handler.",
  },
];
const styleRules: { test: RegExp; key: string; content: string }[] = [
  { test: /british english/i, key: "style:british-english", content: "Reply in British English" },
  { test: /em[\s-]?dash/i, key: "style:em-dashes", content: "Use em dashes in replies" },
];
/**
 * Deterministic DEMO extraction from a captured USER prompt only. Assistant text is not
 * a parameter, so it cannot create or change memories.
 */
export function captureUserPrompt(state: GraphState, userPrompt: string): GraphState {
  const sentences = userPrompt
    .split(/(?<=[.!?])\s+/)
    .map((x) => x.trim())
    .filter(Boolean);
  let updated: GraphState = { ...state, changedIds: [] };
  const affected: string[] = [];
  for (const sentence of sentences) {
    if (/^when replying to me\b/i.test(sentence)) {
      for (const rule of styleRules) {
        if (!rule.test.test(sentence)) continue;
        updated = upsert(updated, rule.key, rule.content, "constraint", "Automatic capture", "");
        affected.push(idFor(state.workspaceId, rule.key));
      }
      continue;
    }
    if (/^(remember that |change the refund window to |forget the |connect the )/i.test(sentence)) {
      const result = demoAdapter.memoryCommand(updated, sentence);
      if (result.supported) {
        updated = result.state;
        affected.push(...result.state.changedIds);
      }
    }
  }
  return { ...updated, changedIds: [...new Set(affected)] };
}
const ROOTS = ["billing", "deployment"];
export const demoAdapter: MemoryGraphAdapter = {
  mode: "Demo replay",
  getGraph(state) {
    const memories = state.memories.filter(
      (m) => belongs(m, state.workspaceId) && m.status === "active",
    );
    const ids = new Set(memories.map((m) => m.id));
    return {
      memories,
      edges: state.edges.filter(
        (e) => e.workspaceId === state.workspaceId && ids.has(e.source) && ids.has(e.target),
      ),
    };
  },
  getContext(state) {
    return this.getGraph(state)
      .memories.sort((a, b) => Number(b.pinned) - Number(a.pinned))
      .map((m) => `- [${m.type}] ${m.content}${m.pinned ? " (pinned)" : ""}`)
      .join("\n");
  },
  memoryCommand(state, input) {
    const ws = state.workspaceId;
    const command = input
      .trim()
      .replace(/[.!]+$/, "")
      .toLowerCase();
    const source = "User-added";
    let updated = state;
    let affected: string[] = [];
    const active = (key: string) =>
      state.memories.some((m) => m.id === idFor(ws, key) && m.status === "active");
    if (command.startsWith("remember that ") && command.length > 14) {
      const content = input
        .trim()
        .replace(/^remember that /i, "")
        .replace(/[.!]+$/, "");
      const lower = normalizeContent(content);
      // Exact normalized content only: reuse canonical IDs and forgotten history.
      const existing = state.memories.find(
        (m) => belongs(m, ws) && normalizeContent(m.content) === lower,
      );
      const key = existing
        ? existing.id.slice(`${ws}:`.length)
        : lower === "maya owns billing"
          ? "owner"
          : `content:${encodeURIComponent(lower)}`;
      updated = upsert(
        state,
        key,
        content.charAt(0).toUpperCase() + content.slice(1),
        existing?.type ?? (lower.includes("needs ") ? "task" : "fact"),
        source,
        "Memory command",
        existing?.position ?? (key === "owner" ? { x: 80, y: 360 } : undefined),
      );
      affected = [idFor(ws, key)];
      for (const root of ROOTS)
        if (lower.includes(root))
          updated = connect(
            updated,
            idFor(ws, root),
            idFor(ws, key),
            lower.includes("owns") ? "owned by" : "requires",
          );
    } else if (command === "connect the refund policy to billing") {
      if (!active("refund") || !active("billing"))
        return {
          state,
          supported: false,
          message: "No active refund policy in your memory graph. Remember a policy first.",
        };
      updated = connect(state, idFor(ws, "billing"), idFor(ws, "refund"), "constrained by");
      affected = [idFor(ws, "billing"), idFor(ws, "refund")];
    } else if (/^change the refund window to \d+ days$/.test(command)) {
      const days = command.match(/\d+/)?.[0];
      if (!days || Number(days) < 1 || Number(days) > 365)
        return { state, supported: false, message: "Use a refund window between 1 and 365 days." };
      updated = upsert(
        state,
        "refund",
        `Refund window: ${days} days`,
        "constraint",
        source,
        "Memory command",
        { x: 380, y: 330 },
      );
      affected = [idFor(ws, "refund")];
      updated = connect(updated, idFor(ws, "billing"), idFor(ws, "refund"), "constrained by");
    } else if (/forget (the )?em[\s-]?dash instruction$/.test(command)) {
      const id = idFor(ws, "style:em-dashes");
      if (!active("style:em-dashes"))
        return {
          state,
          supported: false,
          message: "No active em-dash instruction in your memory graph.",
        };
      updated = forgetMemory(state, id);
      affected = [id];
    } else if (command === "forget the deployment workaround") {
      const id = idFor(ws, "workaround");
      if (!state.memories.some((m) => m.id === id))
        return {
          state,
          supported: false,
          message: "No deployment workaround in your memory graph.",
        };
      updated = forgetMemory(state, id);
      affected = [id];
    } else
      return {
        state,
        supported: false,
        message:
          "Demo supports: “Forget the em-dash instruction”, “Remember that …”, “Connect the refund policy to billing”, “Change the refund window to N days”, and “Forget the deployment workaround”. Other requests need the live memory service.",
      };
    const unchanged =
      JSON.stringify(updated.memories) === JSON.stringify(state.memories) &&
      updated.edges.length === state.edges.length;
    const message = unchanged
      ? "Already remembered · reused the existing memory and relationship."
      : /\bforget\b/.test(command)
        ? "Forgotten · removed from active graph and context."
        : command.startsWith("change")
          ? "Updated · previous version retained in history."
          : command.startsWith("connect")
            ? "Connected · refund policy constrains billing."
            : "Remembered · added to your memory graph’s next-turn context.";
    return { state: record(updated, message, affected), message, supported: true };
  },
  ingestTurn(state) {
    const turn = replayTurns[state.replayIndex];
    if (!turn) return state;
    return ingestCapturedTurn(state, turn, `${state.workspaceId}:replay:${state.replayIndex}`);
  },
};
/** Record a captured turn from any tool/native chat; memory comes only from `turn.user`. */
export function ingestCapturedTurn(
  state: GraphState,
  turn: { user: string; agent?: string; tool?: string; nativeChat?: string },
  id: string,
): GraphState {
  const stamp = time();
  const provenance = { tool: turn.tool ?? "CLI", nativeChat: turn.nativeChat ?? "chat" };
  let updated = captureUserPrompt(state, turn.user);
  const affected = updated.changedIds;
  updated = {
    ...updated,
    memories: updated.memories.map((m) =>
      affected.includes(m.id) && m !== state.memories.find((old) => old.id === m.id)
        ? {
            ...m,
            source: "Automatic capture",
            sourceTurn: id,
            sourceLabel: sourceLabel(provenance),
          }
        : m,
    ),
    replayIndex: state.replayIndex + 1,
    turns: [
      ...state.turns,
      { id, ...provenance, user: turn.user, agent: turn.agent ?? "", time: stamp, affected },
    ],
  };
  return record(
    updated,
    affected.length
      ? `Captured prompt from ${sourceLabel(provenance)}`
      : `Captured prompt from ${sourceLabel(provenance)} · no memory`,
    affected,
  );
}
/** Older per-chat demo storage; removed on load so no separate graphs linger. */
export const legacyStorageKeys = ["memory-lens:v1:billing", "memory-lens:v1:deployment"];
export const storageKey = (id: WorkspaceId = DEMO_WORKSPACE) => `code-review-graph:memory:v2:${id}`;
export function restoreState(id: WorkspaceId, raw: string | null): GraphState {
  if (!raw) return initialState(id);
  try {
    const s = JSON.parse(raw);
    if (
      s.workspaceId !== id ||
      !Array.isArray(s.memories) ||
      !Array.isArray(s.edges) ||
      !Array.isArray(s.turns) ||
      !Array.isArray(s.activity) ||
      typeof s.replayIndex !== "number"
    )
      return initialState(id);
    const memories = s.memories.filter(
      (m: Memory) =>
        m.workspaceId === id && m.id.startsWith(`${id}:`) && m.position && Array.isArray(m.history),
    );
    const ids = new Set(memories.map((m: Memory) => m.id));
    return {
      ...s,
      memories,
      changedIds: [],
      edges: s.edges.filter(
        (e: Relation) => e.workspaceId === id && ids.has(e.source) && ids.has(e.target),
      ),
    };
  } catch {
    return initialState(id);
  }
}

