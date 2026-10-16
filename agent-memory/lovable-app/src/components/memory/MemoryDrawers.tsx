import { Copy, Pin, Unplug } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { demoAdapter, sessionNames, type GraphState } from "@/lib/memory/graph";
export function MemoryDrawers({
  drawer,
  onClose,
  state,
}: {
  drawer: "context" | "connect" | null;
  onClose: () => void;
  state: GraphState;
}) {
  const [copied, setCopied] = useState(false);
  const memories = demoAdapter.getGraph(state).memories;
  return (
    <Sheet
      open={drawer !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent className="drawer-content sm:max-w-[460px]">
        {drawer === "context" ? (
          <>
            <div className="drawer-eyebrow">Memory context · {sessionNames[state.chatId]}</div>
            <SheetTitle className="drawer-title">Preview of next-turn context</SheetTitle>
            <SheetDescription className="drawer-description">
              Active memories prepared for the selected CLI chat. This is a local preview; no coding
              agent has received it.
            </SheetDescription>
            <div className="context-summary">
              <span>{memories.length} active memories</span>
              <Button
                variant="ghost"
                size="sm"
                disabled={!memories.length}
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(demoAdapter.getContext(state));
                    setCopied(true);
                  } catch {
                    setCopied(false);
                  }
                }}
              >
                <Copy />
                {copied ? "Copied" : "Copy context"}
              </Button>
            </div>
            <div className="context-list">
              {memories.length ? (
                memories
                  .sort((a, b) => Number(b.pinned) - Number(a.pinned))
                  .map((memory) => (
                    <div className="context-memory" key={memory.id}>
                      <div className={`type-label type-${memory.type}`}>
                        <span>{memory.type}</span>
                        {memory.pinned && <Pin size={11} />}
                        <span className="ml-auto text-muted-foreground">{memory.source}</span>
                      </div>
                      <p>{memory.content}</p>
                    </div>
                  ))
              ) : (
                <p className="drawer-description">No active memories in this chat.</p>
              )}
            </div>
            <div className="drawer-note">
              Forgotten and superseded versions are excluded. A future CLI hook or MCP bridge will
              retrieve this chat’s active memories before its next prompt.
            </div>
          </>
        ) : (
          <>
            <div className="drawer-eyebrow">Demo replay · local service not connected</div>
            <SheetTitle className="drawer-title">Connect a source</SheetTitle>
            <SheetDescription className="drawer-description">
              Demo replay uses sample prompts. For live capture, start your local code-review-graph
              service, configure the coding tool bridge, then connect its URL and local access token
              in Live service.
            </SheetDescription>
            <div className="offline-label mt-5">
              <Unplug size={14} />
              Local service not connected
            </div>
            <div className="contract-flow">
              <div className="contract-step">
                <span>01</span>
                <div>
                  <strong>Start your local code-review-graph service</strong>
                  <p>It stores memory on your machine, one conversation at a time.</p>
                </div>
              </div>
              <div className="contract-step">
                <span>02</span>
                <div>
                  <strong>Configure the coding tool bridge</strong>
                  <p>
                    Set MEMORY_LENS_API_URL and MEMORY_LENS_TOKEN. The bridge sends only your
                    prompts; assistant responses never feed memory.
                  </p>
                </div>
              </div>
              <div className="contract-step">
                <span>03</span>
                <div>
                  <strong>Connect in Live service</strong>
                  <p>
                    Switch to Live service, enter the service URL and access token, then Test and
                    connect.
                  </p>
                </div>
              </div>
            </div>
            <div className="drawer-note">
              The service accepts one POST per request with an action: ensure_session, ingest_turn,
              graph, command, context and layered_context.
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

