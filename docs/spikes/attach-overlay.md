# Spike: SQLite ATTACH-overlay graph (aihub-3t7yh.3.6)

Measured 2026-09-07, CPython 3.13 / SQLite bundled, 50k-node base + 5k-node
overlay, 50 iterations each.

| Strategy | Mean latency | Relative |
| --- | --- | --- |
| `ATTACH` base + overlay, `UNION ALL` full scan | 0.13 ms | **8.6x** |
| single merged table full scan | 0.015 ms | 1x |

## Exit criterion

Full-graph scans through `ATTACH`+`UNION ALL` pay ~an order of magnitude
over the merged baseline, while saving the copy/merge cost of materializing
an overlay (the reason the spike exists: cheap per-worktree overlays).

**Adopt ATTACH-overlay only for targeted, indexed lookups against an
overlay** (subgraph/impact queries with an index on the overlay side);
**reject it as the default read path** — whole-graph scans (search,
communities, flows) must keep reading the materialized store. Revisit if a
future need requires zero-copy read-only overlay views for short-lived
worktrees where the 8.6x scan penalty is acceptable.
