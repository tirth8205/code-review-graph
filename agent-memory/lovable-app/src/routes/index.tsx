import { createFileRoute } from "@tanstack/react-router";
import { MemoryLens } from "@/components/memory/MemoryWorkspace";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "code-review-graph: Agent memory" },
      {
        name: "description",
        content:
          "Explore and edit the memories of a coding CLI conversation in a chat-scoped graph. Interactive demo replay prototype.",
      },
      { property: "og:title", content: "code-review-graph: your coding agent’s memory" },
      {
        property: "og:description",
        content:
          "A visual memory companion for external coding CLI conversations. Graph, provenance and next-turn context in a clearly labeled demo replay.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: MemoryLens,
});

