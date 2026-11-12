#!/usr/bin/env python3
"""Generate 9 Excalidraw diagrams for code-review-graph Medium article.

All statistics match repo benchmarks exactly. No invented features or numbers.
"""

import json
import math
import os
import random

random.seed(2024)
OUT = os.path.dirname(os.path.abspath(__file__))

_n = 0
def _id():
    global _n; _n += 1; return f"e{_n:04d}"
def _s(): return random.randint(100000, 9999999)
def _tw(t, fs):
    lines = t.split('\n')
    return max(len(l) for l in lines) * fs * 0.6
def _th(t, fs): return (t.count('\n') + 1) * fs * 1.25

# ── Color palette ──
RED = "#e03131"; RED_BG = "#ffc9c9"
GRN = "#2f9e44"; GRN_BG = "#b2f2bb"
ORG = "#e8590c"; ORG_BG = "#ffd8a8"
YLW = "#e67700"; YLW_BG = "#fff3bf"
BLU = "#1971c2"; BLU_BG = "#a5d8ff"
PRP = "#6741d9"; PRP_BG = "#d0bfff"
GRY = "#868e96"; GRY_BG = "#dee2e6"
CYN = "#0c8599"; CYN_BG = "#99e9f2"
DRK = "#1e1e1e"

# ── Element factories ──

def _base(typ, x, y, w, h, **k):
    return {
        "type": typ, "version": 1, "versionNonce": _s(),
        "isDeleted": False, "id": _id(),
        "fillStyle": k.get("fs", "hachure"), "strokeWidth": k.get("sw", 2),
        "strokeStyle": k.get("ss", "solid"), "roughness": k.get("rough", 1),
        "opacity": k.get("op", 100), "angle": 0, "x": x, "y": y,
        "strokeColor": k.get("sc", DRK), "backgroundColor": k.get("bg", "transparent"),
        "width": w, "height": h, "seed": _s(),
        "groupIds": k.get("gids", []), "frameId": None,
        "roundness": k.get("rnd", None), "boundElements": k.get("be", []),
        "updated": 1710000000000, "link": None, "locked": False,
    }

def R(x, y, w, h, **k):
    k.setdefault("rnd", {"type": 3})
    return _base("rectangle", x, y, w, h, **k)

def E(x, y, w, h, **k):
    k.setdefault("rnd", {"type": 2})
    return _base("ellipse", x, y, w, h, **k)

def D(x, y, w, h, **k):
    k.setdefault("rnd", {"type": 2})
    return _base("diamond", x, y, w, h, **k)

def T(x, y, s, fs=20, **k):
    w = _tw(s, fs); h = _th(s, fs)
    e = _base("text", x, y, w, h, **k)
    e.update({"fontSize": fs, "fontFamily": k.get("ff", 1),
              "text": s, "textAlign": k.get("ta", "left"),
              "verticalAlign": k.get("va", "top"),
              "containerId": None, "originalText": s,
              "lineHeight": 1.25, "autoResize": True})
    e.pop("roundness", None)
    return e

def A(x, y, pts, **k):
    w = max(abs(p[0]) for p in pts) if pts else 0
    h = max(abs(p[1]) for p in pts) if pts else 0
    e = _base("arrow", x, y, w, h, **k)
    e.update({"points": pts, "lastCommittedPoint": None,
              "startBinding": None, "endBinding": None,
              "startArrowhead": None, "endArrowhead": k.get("head", "arrow")})
    return e

def LN(x, y, pts, **k):
    w = max(abs(p[0]) for p in pts) if pts else 0
    h = max(abs(p[1]) for p in pts) if pts else 0
    e = _base("line", x, y, w, h, **k)
    e.update({"points": pts, "lastCommittedPoint": None,
              "startBinding": None, "endBinding": None,
              "startArrowhead": None, "endArrowhead": None})
    return e

def TC(cx, y, s, fs=20, **k):
    """Text centered horizontally at cx."""
    w = _tw(s, fs)
    return T(cx - w/2, y, s, fs, **k)

def TM(cx, y, s, fs=20, **k):
    """Text centred on cx by Excalidraw itself (textAlign "center").

    TC centres the estimated width, which is wider than the real font, so its
    text sits left of centre. With "center" the text is drawn around x + width/2
    whatever its real width.
    """
    k.setdefault("ta", "center")
    return TC(cx, y, s, fs, **k)

def save(name, els):
    with open(name, 'w') as f:
        json.dump({"type": "excalidraw", "version": 2,
                   "source": "https://excalidraw.com", "elements": els,
                   "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None},
                   "files": {}}, f, indent=2)
    print(f"  {name}: {len(els)} elements")


# ════════════════════════════════════════════
# DIAGRAM 1 — Before vs After
# ════════════════════════════════════════════
def d1():
    # Layout follows the hand-tuned .excalidraw: panels drawn closer together,
    # labels above their arrows. Keep the element count and order: the seeds of
    # every later diagram depend on them.
    # Numbers: flask row of the README token table (naive_corpus_tokens /
    # avg graph_tokens = 143,594 / 2,196 = 65.4x).
    els = []
    LC = 673   # left panel center-x
    RC = 1123  # right panel center-x
    # Title and banner center-x: the middle of the content, from the left
    # grid's edge (x 448) to the right dashed box's edge (x 1289).
    MID = 868.5

    # Title
    els.append(TM(MID, -15, "The Token Problem", 40, sc=DRK))

    # Dashed divider
    els.append(LN(918, 77, [[0,0],[0,680]], ss="dashed", sc=GRY, sw=1, op=40))

    # ── LEFT: Without Graph ──
    els.append(TM(LC, 77, "Without Graph", 28, sc=RED))

    # Claude Code box
    els.append(R(LC-125, 132, 250, 48, bg=GRY_BG, fs="solid"))
    els.append(TM(LC, 142, "Claude Code", 20))

    # Arrow + label. No arrow gets width or height 0: the Excalidraw VS Code
    # extension rejects a file that has one.
    els.append(A(LC-12, 213, [[0,0],[3,53]], sc=RED))
    els.append(TM(LC-17, 191, "reads entire codebase", 14, sc=RED))

    # File grid container
    els.append(R(LC-225, 267, 450, 240, sc=GRY, bg="#f8f9fa", fs="solid", op=80))

    # 20 small file rects (4×5 grid)
    for row in range(4):
        for col in range(5):
            fx = LC - 202 + col * 85
            fy = 284 + row * 52
            shade = random.choice(["#dee2e6", "#e9ecef", "#ced4da"])
            els.append(R(fx, fy, 62, 32, bg=shade, fs="solid", sc=GRY, sw=1, rough=0))

    els.append(TM(LC, 520, "Entire Codebase", 16, sc=GRY))

    # Red badge
    els.append(R(LC-125, 557, 250, 48, bg=RED_BG, fs="solid", sc=RED))
    els.append(TM(LC, 567, "143,594 tokens", 22, sc=RED))

    # Impact detection
    els.append(TM(LC, 622, "Impact detection: unknown", 16, sc=GRY))

    # ── RIGHT: With Graph ──
    els.append(TM(RC, 77, "With Graph", 28, sc=GRN))

    # Claude Code box
    els.append(R(RC-125, 132, 250, 48, bg=GRY_BG, fs="solid"))
    els.append(TM(RC, 142, "Claude Code", 20))

    # Arrow + label
    els.append(A(RC, 208, [[0,0],[1,42]], sc=GRN))
    els.append(TM(RC-3, 189, "queries graph", 14, sc=GRN))

    # Diamond: Graph
    els.append(D(RC-42, 248, 84, 58, bg=PRP_BG, fs="solid", sc=PRP))
    els.append(TM(RC, 264, "Graph", 16, sc=PRP))

    # Arrow + label
    els.append(A(RC+3, 335, [[0,0],[-1,38]], sc=GRN))
    els.append(TM(RC+5, 311, "search hits + edges", 14, sc=GRN))

    # Ghost rect (faded full codebase): 16 px clear of the green box on every
    # side, so it stays inside the image.
    els.append(R(RC-166, 370, 332, 157, sc=GRY, bg="#f8f9fa", fs="solid", op=20, ss="dashed"))

    # Relevant files rect
    els.append(R(RC-150, 386, 300, 125, sc=GRN, bg="#ebfbee", fs="solid"))

    # 5 green file squares
    for i in range(5):
        fx = RC - 130 + i * 55
        els.append(R(fx, 408, 40, 35, bg=GRN_BG, fs="solid", sc=GRN, sw=1, rough=0))

    els.append(TM(RC, 458, "Graph answer", 16, sc=GRN))

    # Green badge
    els.append(R(RC-125, 557, 250, 48, bg=GRN_BG, fs="solid", sc=GRN))
    els.append(TM(RC, 567, "2,196 tokens", 22, sc=GRN))

    # Impact detection
    els.append(TM(RC, 622, "Impact recall 1.0 (upper bound)", 16, sc=GRN))

    # ── BOTTOM BANNER ──
    els.append(R(MID-350, 700, 700, 52, bg=BLU_BG, fs="solid", sc=BLU))
    els.append(TM(MID, 712, "65.4× fewer tokens · recall 1.0 upper bound (flask)", 22, sc=BLU))

    return els


# ════════════════════════════════════════════
# DIAGRAM 2 — Architecture Pipeline
# ════════════════════════════════════════════
def d2():
    els = []
    els.append(TC(920, 25, "How It Works", 40))

    boxes = [
        ("Repository",       "your code",              BLU_BG, BLU, 60),
        ("Tree-sitter Parser","30+ languages + notebooks", ORG_BG, ORG, 380),
        ("SQLite Graph",     "nodes + edges\nflows + communities",  PRP_BG, PRP, 700),
        ("Blast Radius",     "BFS traversal",           YLW_BG, YLW, 1020),
        ("Minimal Review Set","only what matters",        GRN_BG, GRN, 1380),
    ]
    bw, bh, by = 260, 90, 160

    for name, sub, bg, sc, bx in boxes:
        els.append(R(bx, by, bw, bh, bg=bg, fs="solid", sc=sc))
        els.append(TC(bx + bw/2, by + 18, name, 20, sc=sc))
        els.append(TC(bx + bw/2, by + 50, sub, 14, sc=GRY))

    arrow_labels = ["parse ASTs", "store", "query impacted files", "return set"]
    for i, label in enumerate(arrow_labels):
        ax = boxes[i][4] + bw + 8
        ay = by + bh/2
        gap = boxes[i+1][4] - ax - 8
        els.append(A(ax, ay, [[0,0],[gap,0]]))
        els.append(TC(ax + gap/2, ay - 24, label, 13, sc=GRY))

    # Bottom bracket
    lx, rx = boxes[0][4], boxes[-1][4] + bw
    bky = by + bh + 55
    els.append(LN(lx, bky, [[0,0],[rx-lx,0]], sc=GRY, sw=1))
    els.append(LN(lx, bky-8, [[0,0],[0,8]], sc=GRY, sw=1))
    els.append(LN(rx, bky-8, [[0,0],[0,8]], sc=GRY, sw=1))
    els.append(TC((lx+rx)/2, bky+12, "Persistent \u00b7 Incremental \u00b7 Local", 18, sc=GRY))

    return els


# ════════════════════════════════════════════
# DIAGRAM 3 — Blast Radius
# ════════════════════════════════════════════
def d3():
    els = []
    cx, cy = 480, 400

    els.append(TC(cx, 20, "Blast Radius of a Change", 36))

    # Center node (CHANGED)
    els.append(E(cx-80, cy-40, 160, 80, bg=RED_BG, fs="solid", sc=RED))
    els.append(TC(cx, cy-25, "auth.py::", 13, sc=RED))
    els.append(TC(cx, cy-5, "login()", 20, sc=RED))
    els.append(TC(cx, cy+22, "CHANGED", 12, sc=RED))

    # Ring 1 (depth 1, orange) — 3 nodes
    r1 = 200
    ring1_spec = [
        ("validate_token()", "CALLS",     90),
        ("User",             "DEPENDS_ON", 215),
        ("test_login()",     "TESTED_BY", 325),
    ]
    ring1_pos = []
    for name, edge_label, angle_deg in ring1_spec:
        a = math.radians(angle_deg)
        nx = cx + r1 * math.cos(a)
        ny = cy - r1 * math.sin(a)
        ring1_pos.append((nx, ny))
        nw, nh = 175, 50
        els.append(E(nx-nw/2, ny-nh/2, nw, nh, bg=ORG_BG, fs="solid", sc=ORG))
        els.append(TC(nx, ny-8, name, 14, sc=ORG))
        # Arrow from center
        dx, dy = nx-cx, ny-cy
        dist = math.sqrt(dx*dx+dy*dy)
        sf, ef = 50/dist, (dist-40)/dist
        sx, sy = cx+dx*sf, cy+dy*sf
        els.append(A(sx, sy, [[0,0],[dx*(ef-sf), dy*(ef-sf)]], sc=ORG, sw=1))
        # Edge label at midpoint
        mx, my = cx+dx*0.55, cy+dy*0.55
        els.append(T(mx+8, my-14, edge_label, 11, sc=ORG, op=70))

    # Ring 2 (depth 2, yellow) — 3 nodes
    r2 = 370
    ring2_spec = [
        ("protected_route()", "CALLS",     65,  "r1", 0),
        ("AuthMiddleware",    "CALLS",     118, "r1", 0),
        ("test_protected()",  "TESTED_BY", 340, "r2", 0),  # from protected_route
    ]
    ring2_pos = []
    for name, edge_label, angle_deg, _, _ in ring2_spec:
        a = math.radians(angle_deg)
        nx = cx + r2 * math.cos(a)
        ny = cy - r2 * math.sin(a)
        ring2_pos.append((nx, ny))

    for i, (name, edge_label, angle_deg, parent_ring, pidx) in enumerate(ring2_spec):
        nx, ny = ring2_pos[i]
        nw, nh = 180, 50
        els.append(E(nx-nw/2, ny-nh/2, nw, nh, bg=YLW_BG, fs="solid", sc=YLW))
        els.append(TC(nx, ny-8, name, 14, sc=YLW))
        # Parent position
        if parent_ring == "r1":
            px, py = ring1_pos[pidx]
        else:
            px, py = ring2_pos[pidx]
        dx, dy = nx-px, ny-py
        dist = math.sqrt(dx*dx+dy*dy)
        if dist > 0:
            sf, ef = 40/dist, (dist-45)/dist
            sx, sy = px+dx*sf, py+dy*sf
            els.append(A(sx, sy, [[0,0],[dx*(ef-sf), dy*(ef-sf)]], sc=YLW, sw=1))
            mx, my = px+dx*0.5, py+dy*0.5
            els.append(T(mx+8, my-14, edge_label, 11, sc=YLW, op=70))

    # Outer gray nodes (NOT IMPACTED)
    outer = [("utils.py", 920, 180), ("config.py", 920, 320),
             ("database.py", 920, 460), ("static/...", 920, 600)]
    for name, ox, oy in outer:
        els.append(E(ox-60, oy-22, 120, 44, sc=GRY, bg=GRY_BG, fs="solid", op=35, ss="dashed"))
        els.append(TC(ox, oy-8, name, 12, sc=GRY, op=40))
    els.append(TC(920, 400, "Unrelated files", 14, sc=GRY, op=50))

    # Legend
    ly = 720
    for i, (bg, sc, label) in enumerate([
        (RED_BG, RED, "Changed"), (ORG_BG, ORG, "Direct dependents"),
        (YLW_BG, YLW, "Indirect dependents"), (GRY_BG, GRY, "Unrelated files"),
    ]):
        lx = 100 + i * 200
        els.append(R(lx, ly, 22, 22, bg=bg, fs="solid", sc=sc, sw=1))
        els.append(T(lx+30, ly+3, label, 14, sc=sc))

    return els


# ════════════════════════════════════════════
# DIAGRAM 4 — Incremental Update Flow
# ════════════════════════════════════════════
def d4():
    els = []
    els.append(TC(450, 20, "Incremental Updates in < 2 Seconds", 34))

    sx = 180  # step box x
    sw, sh = 370, 55
    step_cx = sx + sw/2

    # Step 1: Trigger
    y1 = 95
    els.append(R(sx, y1, sw, sh, bg=BLU_BG, fs="solid", sc=BLU))
    els.append(TC(step_cx, y1+12, "git commit / file save", 20, sc=BLU))
    els.append(T(sx+sw+18, y1+18, "hook triggered", 13, sc=GRY))

    els.append(A(step_cx, y1+sh+5, [[0,0],[0,30]]))

    # Step 2: Detect
    y2 = 190
    els.append(R(sx, y2, sw, sh, bg=ORG_BG, fs="solid", sc=ORG))
    els.append(TC(step_cx, y2+12, "git diff", 20, sc=ORG))
    # Chips
    for i, f in enumerate(["auth.py", "routes.py"]):
        cx_chip = sx + sw + 18 + i * 115
        els.append(R(cx_chip, y2+5, 100, 30, bg="#fff4e6", fs="solid", sc=ORG, sw=1))
        els.append(T(cx_chip+8, y2+11, f, 13, sc=ORG))
    els.append(T(sx+sw+18, y2+42, "2 changed files", 13, sc=ORG))

    els.append(A(step_cx, y2+sh+5, [[0,0],[0,30]]))

    # Step 3: Cascade
    y3 = 285
    els.append(R(sx, y3, sw, sh, bg=YLW_BG, fs="solid", sc=YLW))
    els.append(TC(step_cx, y3+12, "Find dependent files", 20, sc=YLW))
    for i, f in enumerate(["test_auth.py", "test_routes.py", "middleware.py"]):
        cx_chip = sx + sw + 18 + i * 122
        els.append(R(cx_chip, y3+5, 112, 30, bg="#fff9db", fs="solid", sc=YLW, sw=1))
        els.append(T(cx_chip+6, y3+11, f, 12, sc=YLW))
    els.append(T(sx+sw+18, y3+42, "3 dependent files", 13, sc=YLW))
    els.append(T(sx-160, y3+15, "SHA-256\nhash check", 13, sc=GRY, ta="right"))

    els.append(A(step_cx, y3+sh+5, [[0,0],[0,30]]))

    # Step 4: Re-parse
    y4 = 380
    els.append(R(sx, y4, sw, sh+10, bg=GRN_BG, fs="solid", sc=GRN))
    els.append(TC(step_cx, y4+8, "Re-parse 5 files", 20, sc=GRN))
    els.append(TC(step_cx, y4+35, "Graph updated \u2713", 14, sc=GRN))
    # Badge
    els.append(R(sx+sw+18, y4+8, 140, 42, bg=GRN_BG, fs="solid", sc=GRN))
    els.append(TC(sx+sw+88, y4+17, "< 2 seconds", 17, sc=GRN))

    # Right panel: skipped files
    skip_x, skip_y = 810, 120
    els.append(R(skip_x, skip_y, 160, 300, sc=GRY, bg=GRY_BG, fs="solid", op=25, ss="dashed"))
    for i in range(9):
        fy = skip_y + 15 + i * 30
        els.append(R(skip_x+15, fy, 130, 18, bg="#e9ecef", fs="solid", sc=GRY, sw=1, op=30, rough=0))
    els.append(TC(skip_x+80, skip_y+300, "2,910 files", 15, sc=GRY))
    els.append(TC(skip_x+80, skip_y+320, "skipped", 15, sc=GRY))

    return els


# ════════════════════════════════════════════
# DIAGRAM 5 — Benchmark Metric Board
# ════════════════════════════════════════════
def d5():
    # Same measure as the README token table: naive_corpus_tokens / avg
    # graph_tokens. The benchmark's own average_reduction_ratio (mean of the
    # per-question ratios) reads higher; docs/REPRODUCING.md lists both.
    els = []
    els.append(TM(800, 15, "Benchmarks Across Real Repos", 36))

    # Header: median left, quality badge right
    els.append(TM(500, 75, "~63× median", 64, sc=BLU))
    els.append(TM(500, 160, "fewer tokens · 6 repos · range 35× – 358×", 20, sc=GRY))

    els.append(R(795, 85, 390, 80, bg=GRN_BG, fs="solid", sc=GRN))
    els.append(TM(990, 100, "0.69 avg F1 on 13 commits", 22, sc=GRN))
    els.append(TM(990, 132, "recall 1.0: graph-derived upper bound", 14, sc=GRN))

    # 3 repo cards — naive_corpus_tokens → avg graph_tokens across 5 questions
    # Pinned SHAs: gin@5c00df8a, flask@a29f88ce, fastapi@22381558 (2026-08-02 capture)
    cards = [
        {"name":"gin",     "files":"Go web framework",     "red":"60.3×",
         "tok":"166,868 → 2,766 tokens",
         "c":BLU, "bg":BLU_BG},
        {"name":"flask",   "files":"Python web framework", "red":"65.4×",
         "tok":"143,594 → 2,196 tokens",
         "c":ORG, "bg":ORG_BG},
        {"name":"fastapi", "files":"Python web framework", "red":"357.6×",
         "tok":"948,793 → 2,653 tokens",
         "c":GRN, "bg":GRN_BG},
    ]
    cw, ch = 370, 200
    gap = 50
    total = 3*cw + 2*gap
    x0 = (1600 - total) / 2
    cy = 230

    for i, cd in enumerate(cards):
        cx = x0 + i*(cw+gap)
        ccx = cx + cw/2
        els.append(R(cx, cy, cw, ch, bg=cd["bg"], fs="solid", sc=cd["c"], op=80))
        els.append(TM(ccx, cy+15,  cd["name"],  24, sc=cd["c"]))
        els.append(TM(ccx, cy+48,  cd["files"], 14, sc=GRY))
        els.append(TM(ccx, cy+75,  cd["red"],   52, sc=cd["c"]))
        els.append(TM(ccx, cy+150, cd["tok"],   14, sc=DRK))

    # Footnote — styled as a subtle callout
    fn_y = cy + ch + 20
    els.append(LN(x0+80, fn_y, [[0,0],[total-160,0]], sc=GRY, sw=1, op=30))
    els.append(TM(800, fn_y+10, "Reproducible: docs/REPRODUCING.md (pinned SHAs, Leiden seed=42)"
                  " · captured 2026-08-02", 16, sc=GRY))

    return els


# ════════════════════════════════════════════
# DIAGRAM 6 — Monorepo Funnel
# ════════════════════════════════════════════
def d6():
    # This repository at the 84bde354 snapshot (2026-08-02 capture):
    # 208,821 / 3,190 = 65.5x, the README token table's measure.
    els = []
    # Title centred on the content below: from the grid's left edge (x 40) to
    # the end of the widest right-hand label (x 974).
    els.append(TM(507, 15, "Whole Codebase or Targeted Answer?", 40))

    # ── LEFT: dense grid ──
    gx, gy = 40, 110
    cols, rows = 14, 9
    dw, dh = 20, 16
    gapx, gapy = 26, 22

    for r in range(rows):
        for c in range(cols):
            fx = gx + c*gapx
            fy = gy + r*gapy
            shade = random.choice(["#e9ecef","#dee2e6","#ced4da","#d0d0d0"])
            els.append(R(fx, fy, dw, dh, bg=shade, fs="solid", sc=GRY, sw=1, rough=0))

    gcx = gx + (cols*gapx)/2
    els.append(TM(gcx, gy-35, "code-review-graph", 22, sc=DRK))
    els.append(TM(gcx, gy+rows*gapy+8,  "1,446 nodes · snapshot 84bde354", 16, sc=GRY))
    els.append(TM(gcx, gy+rows*gapy+30, "208,821 source tokens", 14, sc=RED))

    # ── CENTER: funnel (rounded rect) ──
    fx, fy, fw, fh = 470, 120, 210, 180
    els.append(R(fx, fy, fw, fh, bg=PRP_BG, fs="solid", sc=PRP))
    fcx = fx + fw/2
    els.append(TM(fcx, fy+25,  "code-review-graph", 17, sc=PRP))
    els.append(TM(fcx, fy+65,  "parse →", 13, sc=PRP, op=70))
    els.append(TM(fcx, fy+85,  "graph →", 13, sc=PRP, op=70))
    els.append(TM(fcx, fy+105, "blast radius", 13, sc=PRP, op=70))

    # Arrow into funnel. Each arrow drops 1px so none has height 0, which
    # the Excalidraw VS Code extension rejects.
    els.append(A(gx+cols*gapx+5, gy+(rows*gapy)/2,
                 [[0,0],[fx-gx-cols*gapx-20, 1]]))

    # Arrow out of funnel
    rx = 740
    els.append(A(fx+fw+5, fy+fh/2, [[0,0],[rx-fx-fw-10, 1]], sc=GRN))

    # ── RIGHT: sparse green files ──
    rf_x, rf_y = 760, 130
    file_w, file_h, file_gap = 50, 38, 52

    for i in range(5):
        fy2 = rf_y + i*file_gap
        els.append(R(rf_x, fy2, file_w, file_h, bg=GRN_BG, fs="solid", sc=GRN))

    lbl_x = rf_x + file_w + 20
    els.append(T(lbl_x, rf_y,    "Graph answer",    17, sc=GRN))
    els.append(T(lbl_x, rf_y+22, "5 hits + edges",  14, sc=GRN))
    els.append(T(lbl_x, rf_y + 4*file_gap + 5,  "~3,190 tokens",  16, sc=GRN))
    els.append(T(lbl_x, rf_y + 4*file_gap + 26, "avg over 5 questions",   12, sc=GRN))

    # Big number at bottom
    els.append(TM(550, 400, "65.5×", 80, sc=BLU))
    els.append(TM(550, 490, "fewer tokens per question", 24, sc=BLU))
    els.append(TM(550, 525, "answer-shaped context, not file dumps", 18, sc=GRY))

    return els


# ════════════════════════════════════════════
# DIAGRAM 7 — MCP Integration Flow
# ════════════════════════════════════════════
def d7():
    els = []
    els.append(TC(700, 20, "How Claude Code Uses the Graph", 36))

    # ── Step boxes (vertical flow) ──
    bw, bh = 320, 65
    sx = 100
    rx = 550  # right column for annotations

    # Step 1: User asks
    y = 90
    els.append(R(sx, y, bw, bh, bg=BLU_BG, fs="solid", sc=BLU))
    els.append(TC(sx+bw/2, y+10, "User", 22, sc=BLU))
    els.append(TC(sx+bw/2, y+38, '"Review my changes"', 14, sc=GRY))

    els.append(A(sx+bw/2, y+bh+5, [[0,0],[0,30]], sc=GRY))

    # Step 2: Claude Code
    y = 200
    els.append(R(sx, y, bw, bh, bg=PRP_BG, fs="solid", sc=PRP))
    els.append(TC(sx+bw/2, y+10, "Claude Code", 22, sc=PRP))
    els.append(TC(sx+bw/2, y+38, "checks MCP tools", 14, sc=GRY))

    # Right annotation: what Claude looks for
    els.append(R(rx, y-5, 380, 75, bg="#f8f9fa", fs="solid", sc=GRY, op=60))
    els.append(T(rx+15, y+5, "Skills tell Claude:", 14, sc=GRY))
    els.append(T(rx+15, y+25, '"Use get_review_context before\n scanning files manually"', 13, sc=PRP))

    els.append(A(sx+bw/2, y+bh+5, [[0,0],[0,30]], sc=PRP))

    # Step 3: MCP call
    y = 310
    els.append(R(sx, y, bw, bh, bg=ORG_BG, fs="solid", sc=ORG))
    els.append(TC(sx+bw/2, y+10, "MCP Server", 22, sc=ORG))
    els.append(TC(sx+bw/2, y+38, "code-review-graph serve", 13, sc=GRY))

    # Right annotation: what gets called
    els.append(R(rx, y-5, 380, 75, bg="#fff4e6", fs="solid", sc=ORG, op=60))
    els.append(T(rx+15, y+5, "30 tools available:", 14, sc=ORG))
    els.append(T(rx+15, y+25, "detect_changes \u2192 get_review_context\n\u2192 get_impact_radius \u2192 query_graph", 13, sc=ORG))

    els.append(A(sx+bw/2, y+bh+5, [[0,0],[0,30]], sc=ORG))

    # Step 4: Graph query
    y = 420
    els.append(D(sx+bw/2-50, y, 100, 65, bg=GRN_BG, fs="solid", sc=GRN))
    els.append(TC(sx+bw/2, y+18, "graph.db", 16, sc=GRN))

    # Right annotation: what gets returned
    els.append(R(rx, y-5, 380, 75, bg="#ebfbee", fs="solid", sc=GRN, op=60))
    els.append(T(rx+15, y+5, "Returns:", 14, sc=GRN))
    els.append(T(rx+15, y+25, "Blast radius, affected flows,\ntest gaps, risk scores", 13, sc=GRN))

    els.append(A(sx+bw/2, y+65+5, [[0,0],[0,30]], sc=GRN))

    # Step 5: Claude responds
    y = 530
    els.append(R(sx, y, bw, bh, bg=GRN_BG, fs="solid", sc=GRN))
    els.append(TC(sx+bw/2, y+10, "Precise Review", 22, sc=GRN))
    els.append(TC(sx+bw/2, y+38, "reads only what matters", 14, sc=GRN))

    # ── Bottom banner ──
    els.append(R(200, 630, 600, 48, bg=RED_BG, fs="solid", sc=RED))
    els.append(TC(500, 640, "Without skills/hooks: Claude ignores the graph entirely", 16, sc=RED))

    return els


# ════════════════════════════════════════════
# DIAGRAM 8 — Supported Platforms
# ════════════════════════════════════════════
def d8():
    els = []
    els.append(TM(600, 20, "One Install, Every Platform", 36))
    els.append(TM(600, 70, "code-review-graph install", 20, sc=PRP, ff=3))

    # 18 platforms, in the order of the PLATFORMS dict in code_review_graph/skills.py,
    # each with the config file install writes by default ("~" is the home folder,
    # the rest sit in the repository). Zed's file depends on the OS.
    platforms = [
        ("Codex",              "~/.codex/config.toml"),
        ("Claude Code",        ".mcp.json"),
        ("Cursor",             ".cursor/mcp.json"),
        ("Windsurf",           "~/.codeium/windsurf/mcp_config.json"),
        ("Zed",                "Zed settings.json"),
        ("Continue",           "~/.continue/config.json"),
        ("OpenCode",           "opencode.jsonc"),
        ("MiMo Code",          ".mimocode/mimocode.json"),
        ("Antigravity",        "~/.gemini/antigravity/mcp_config.json"),
        ("Gemini CLI",         ".gemini/settings.json"),
        ("Qwen Code",          "~/.qwen/settings.json"),
        ("Kiro",               ".kiro/settings/mcp.json"),
        ("Qoder",              ".qoder/mcp.json"),
        ("GitHub Copilot",     ".vscode/mcp.json"),
        ("GitHub Copilot CLI", "~/.copilot/mcp-config.json"),
        ("Hermes Agent",       "~/.hermes/config.yaml"),
        ("DeepSeek Harness",   "~/.dsh/cordis.patch.yml"),
        ("CodeBuddy Code",     ".mcp.json"),
    ]
    colours = [(BLU, BLU_BG), (PRP, PRP_BG), (ORG, ORG_BG), (GRN, GRN_BG),
               (YLW, YLW_BG), (RED, RED_BG), (GRY, GRY_BG)]

    # Central "install" node
    center_x, center_y = 600, 175
    els.append(E(center_x-75, center_y-30, 150, 60, bg=PRP_BG, fs="solid", sc=PRP))
    els.append(TM(center_x, center_y-10, "auto-detect", 16, sc=PRP))

    # Three rows of 6 cards
    cols = 6
    card_w, card_h = 165, 85
    gap_x, gap_y = 20, 30
    total_w = cols * card_w + (cols - 1) * gap_x  # 6*165 + 5*20 = 1090
    x0 = center_x - total_w/2
    rows = math.ceil(len(platforms) / cols)
    row_y = [290 + r * (card_h + gap_y) for r in range(rows)]
    path_fs = 10
    # Characters that fit on one line: Cascadia's advance is about 0.59em.
    max_chars = int((card_w - 6) / (path_fs * 0.59))

    for i, (name, cfg) in enumerate(platforms):
        sc, bg = colours[i % len(colours)]
        row = i // cols
        col = i % cols
        cx = x0 + col * (card_w + gap_x) + card_w/2
        cy = row_y[row]

        # Light arrow from the center node to the first row only, to keep
        # things readable. With an even number of columns no arrow is
        # vertical: the Excalidraw VS Code extension rejects a file with an
        # arrow of width 0 or height 0.
        if row == 0:
            sx = center_x + (cx - center_x) * 0.12
            sy = center_y + 32
            els.append(A(sx, sy, [[0,0], [cx - sx, cy - 8 - sy]],
                         sc=sc, sw=1, op=45))

        # Platform card
        els.append(R(cx-card_w/2, cy, card_w, card_h, bg=bg, fs="solid", sc=sc))
        els.append(TM(cx, cy+14, name, 14, sc=sc))
        # A long path breaks after its last "/" that fits, never mid-name.
        if len(cfg) > max_chars:
            cut = cfg.rindex("/", 0, max_chars) + 1
            cfg = cfg[:cut] + "\n" + cfg[cut:]
        y_path = cy + 50 if "\n" not in cfg else cy + 43
        els.append(TM(cx, y_path, cfg, path_fs, sc=GRY, ff=3))

    # Footer
    footer_y = row_y[-1] + card_h + 30
    els.append(TM(600, footer_y, "18 platforms · Auto-detects installed ones"
                  " · Poetry / uv / uvx aware · Writes correct config", 14, sc=GRY))

    return els


# ════════════════════════════════════════════
# DIAGRAM 9 — Language Coverage
# ════════════════════════════════════════════
def d9():
    els = []
    els.append(TM(700, 15, "40+ Languages + Notebook Support", 34))

    # Group languages by ecosystem — verified against parser.py
    # EXTENSION_TO_LANGUAGE and detect_language (Blade, Ansible, Spring config,
    # dbt models, Databricks notebooks and shebang scripts are found by name,
    # path or content; custom languages come from .code-review-graph/languages.toml).
    groups = [
        ("Web",       ["TypeScript", "JavaScript", "TSX", "Vue", "Svelte", "Astro",
                       "HTML", "Blade", "Liquid"],                                    BLU, BLU_BG),
        ("Backend",   ["Python", "Go", "Rust", "Java", "Scala", "Elixir"],            GRN, GRN_BG),
        ("Systems",   ["C", "C++", "C#", "Objective-C", "Zig", "VB.NET"],             ORG, ORG_BG),
        ("Mobile",    ["Kotlin", "Swift", "Dart"],                                    PRP, PRP_BG),
        ("Scripting", ["Ruby", "PHP", "Perl", "Lua", "R", "Julia", "Luau",
                       "Perl XS"],                                                    YLW, YLW_BG),
        ("Shells",    ["Bash", "PowerShell", "Zsh / Ksh"],                            RED, RED_BG),
        ("Domain",    ["Solidity", "SQL", "Oracle PL/SQL", "dbt models", "Verilog",
                       "GDScript", "Robot Framework"],                                GRY, GRY_BG),
        ("Config",    ["JSON", "YAML", "XSLT", "Terraform", "Ansible",
                       "Spring config", "Nix"],                                       CYN, CYN_BG),
        ("Other",     ["ReScript", "Jupyter/.ipynb", "Databricks .py",
                       "custom (toml)"],                                              PRP, PRP_BG),
    ]

    gw = 130  # group width
    gap = 18
    total_w = len(groups) * gw + (len(groups)-1) * gap  # 9*130 + 8*18 = 1314
    x0 = (1400 - total_w) / 2  # center on 700
    gy = 75

    for gi, (group_name, langs, sc, bg) in enumerate(groups):
        gx = x0 + gi * (gw + gap)
        gh = 55 + len(langs) * 32

        # Group container
        els.append(R(gx, gy, gw, gh, bg=bg, fs="solid", sc=sc, op=60))
        els.append(TM(gx+gw/2, gy+10, group_name, 16, sc=sc))

        # Language pills
        for li, lang in enumerate(langs):
            ly = gy + 42 + li * 32
            pw = gw - 20
            els.append(R(gx+10, ly, pw, 24, bg="#ffffff", fs="solid",
                         sc=sc, sw=1, rough=0, op=80))
            els.append(TM(gx+gw/2, ly+4, lang, 12, sc=sc))

    # Bottom: what each language gets
    max_gh = max(55 + len(g[1]) * 32 for g in groups)
    fy = gy + max_gh + 20
    features = ["Functions", "Classes", "Imports", "Calls", "Inheritance", "Tests"]
    feat_w = total_w / len(features)
    for i, feat in enumerate(features):
        fx = x0 + i * feat_w + feat_w/2
        els.append(TM(fx, fy, "✓ " + feat, 13, sc=GRN))
    els.append(TM(700, fy+26, "where the grammar supports it — not all six apply", 12, sc=GRY))

    els.append(TM(700, fy+52, "Tree-sitter grammars + text-based fallbacks (ReScript, VB.NET, Blade,"
                  " Liquid, PL/SQL, Robot Framework) + notebook, config & shebang passes",
                  13, sc=GRY))

    return els


# ════════════════════════════════════════════
# GENERATE ALL
# ════════════════════════════════════════════
if __name__ == "__main__":
    # Pass diagram numbers (e.g. `1 5 6`) to write only those files. Every
    # diagram is still built in order, because element ids and seeds run on
    # across diagrams; skipping one would change the ones after it.
    import sys
    only = set(sys.argv[1:])
    print("Generating diagrams...")
    for num, (name, fn) in enumerate([
        ("diagram1_before_vs_after.excalidraw", d1),
        ("diagram2_architecture_pipeline.excalidraw", d2),
        ("diagram3_blast_radius.excalidraw", d3),
        ("diagram4_incremental_update.excalidraw", d4),
        ("diagram5_benchmark_board.excalidraw", d5),
        ("diagram6_monorepo_funnel.excalidraw", d6),
        ("diagram7_mcp_integration_flow.excalidraw", d7),
        ("diagram8_supported_platforms.excalidraw", d8),
        ("diagram9_language_coverage.excalidraw", d9),
    ], start=1):
        els = fn()
        if not only or str(num) in only:
            save(f"{OUT}/{name}", els)
    print("\nDone! Open the files in an Excalidraw editor.")
