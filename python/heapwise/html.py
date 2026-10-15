"""Self-contained HTML report: findings, memory timeline, flame graph and call tree.

The page has no external dependencies, so it opens offline and can be attached
to a bug report as a single file.
"""

from __future__ import annotations

import html
import json
import os

from ._native import Capture

_VIEWS = ("peak", "leaks", "temporaries")


def _short(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/\\") + os.sep):
        return path[len(cwd.rstrip("/\\")) + 1 :]
    return path


def _is_mine(file: str, exclude: list[str]) -> bool:
    return not file.startswith("<") and not any(p and file.startswith(p) for p in exclude)


def report_data(
    capture: Capture,
    title: str,
    exclude: list[str] | None = None,
    cwd: str | None = None,
    notes: list[str] | None = None,
) -> dict:
    """Everything the page needs, as plain JSON-able data."""
    exclude = list(exclude or [])
    summary = capture.summary(title, exclude, cwd, 5).splitlines()
    header = summary[0] if summary else title
    findings = []
    for line in summary[2:]:
        if not line.strip() or line.startswith("Memory over time"):
            break
        findings.append(line)
    # The capture keys frames by bytecode offset; the page shows lines, so
    # frames on the same line of the same function become one. File names
    # are listed once and referred to by index.
    frames: list[list] = []
    files: list[str] = []
    file_ids: dict[str, int] = {}
    canon: dict[tuple[str, str, int], int] = {}
    remap = []
    for func, file, line in capture.frames():
        k = (func, file, line)
        if k not in canon:
            canon[k] = len(frames)
            if file not in file_ids:
                file_ids[file] = len(files)
                files.append(_short(file, cwd))
            frames.append([func, file_ids[file], line, 1 if _is_mine(file, exclude) else 0])
        remap.append(canon[k])

    # Stacks go out as the trie the capture keeps (parent, frame per node),
    # restricted to nodes some view needs, so the page stays small however
    # many stacks share prefixes.
    trie = capture.stack_nodes()
    values = {}
    for view in _VIEWS:
        if view == "temporaries" and capture.sample_rate != 0:
            values[view] = None  # only counted with --exact
        else:
            values[view] = capture.stack_values(view)
    needed = bytearray(len(trie))
    needed[0] = 1
    for rows in values.values():
        for node, *_ in rows or ():
            while not needed[node]:
                needed[node] = 1
                node = trie[node][0]
    new_id = [0] * len(trie)
    nodes: list[int] = []  # flat: parent, frame, parent, frame, ...
    count = 0
    for i, (parent, frame) in enumerate(trie):
        if needed[i]:
            new_id[i] = count
            count += 1
            nodes += (new_id[parent], remap[frame]) if i else (0, -1)
    views = {
        view: None if rows is None else [x for node, b, c, n in rows for x in (new_id[node], b, c, n)]
        for view, rows in values.items()
    }
    prefix = f"heapwise · {title} · "
    return {
        "title": title,
        "header": header.removeprefix(prefix),
        "findings": findings,
        "notes": list(notes or []),
        "duration": capture.duration_seconds,
        "peak": capture.peak_bytes,
        "peak_native": capture.peak_native_bytes,
        "peak_time": capture.peak_seconds,
        "leaked": capture.leaked_bytes,
        "sampled": capture.sample_rate != 0,
        "timeline": capture.timeline(400),
        "files": files,
        "frames": frames,
        "nodes": nodes,
        "views": views,
    }


def render(data: dict) -> str:
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return (
        _TEMPLATE.replace("{{title}}", html.escape(f"heapwise · {data['title']}"))
        .replace("{{data}}", payload)
    )


def write_html(
    capture: Capture,
    path: str | os.PathLike[str],
    title: str = "heapwise",
    exclude: list[str] | None = None,
    cwd: str | None = None,
    notes: list[str] | None = None,
) -> str:
    """Write the report for ``capture`` to ``path`` and return the path."""
    page = render(report_data(capture, title, exclude, cwd, notes))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return os.fspath(path)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{title}}</title>
<style>
:root {
  --bg: #fbfaf8; --panel: #ffffff; --text: #1d1b19; --muted: #6b665f; --line: #e6e2dc;
  --accent: #c2410c; --mine: #f59e0b; --mine-2: #ea580c; --lib: #94a3b8; --lib-2: #64748b;
  --synthetic: #d6d3d1; --native: #7c3aed; --area: rgba(194, 65, 12, 0.16); --hover: rgba(0,0,0,0.04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161514; --panel: #1f1e1c; --text: #ece9e4; --muted: #a39e96; --line: #34322f;
    --accent: #fb923c; --mine: #f59e0b; --mine-2: #ea580c; --lib: #94a3b8; --lib-2: #7c8ba1;
    --synthetic: #44403c; --native: #a78bfa; --area: rgba(251, 146, 60, 0.18); --hover: rgba(255,255,255,0.05);
  }
}
:root[data-theme="dark"] {
  --bg: #161514; --panel: #1f1e1c; --text: #ece9e4; --muted: #a39e96; --line: #34322f;
  --accent: #fb923c; --mine: #f59e0b; --mine-2: #ea580c; --lib: #94a3b8; --lib-2: #7c8ba1;
  --synthetic: #44403c; --native: #a78bfa; --area: rgba(251, 146, 60, 0.18); --hover: rgba(255,255,255,0.05);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 0 0 2px; }
.sub { color: var(--muted); font-size: 13px; }
.findings { margin: 16px 0 20px; padding: 14px 16px; background: var(--panel); border: 1px solid var(--line);
  border-left: 3px solid var(--accent); border-radius: 8px; }
.findings p { margin: 4px 0; }
.findings .note { color: var(--muted); font-size: 13px; }
section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; margin-bottom: 16px; }
h2 { font-size: 15px; margin: 0 0 10px; display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
h2 .hint { font-weight: 400; color: var(--muted); font-size: 12px; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
#timeline { width: 100%; height: 140px; display: block; }
#timeline text { fill: var(--muted); font-size: 11px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin-bottom: 12px; }
.tabs { display: inline-flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.tabs button { border: 0; background: transparent; color: var(--text); padding: 6px 12px; font: inherit; cursor: pointer; }
.tabs button + button { border-left: 1px solid var(--line); }
.tabs button[aria-selected="true"] { background: var(--accent); color: #fff; }
.tabs button:disabled { color: var(--muted); cursor: not-allowed; }
label { color: var(--muted); font-size: 13px; cursor: pointer; user-select: none; }
.crumbs { font-size: 12px; color: var(--muted); margin-bottom: 6px; min-height: 18px; }
.crumbs a { color: var(--accent); cursor: pointer; text-decoration: none; }
#flame { position: relative; width: 100%; overflow: hidden; }
.fn { position: absolute; height: 19px; padding: 0 4px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  font: 11px/19px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: #1d1b19; cursor: pointer;
  border-right: 1px solid var(--panel); border-bottom: 1px solid var(--panel); }
.fn:hover { filter: brightness(1.12); }
.fn.dim { opacity: 0.35; }
#tip { position: fixed; pointer-events: none; background: var(--text); color: var(--bg); padding: 6px 8px; border-radius: 6px;
  font-size: 12px; max-width: 420px; display: none; z-index: 10; }
#tip .loc { opacity: 0.75; }
.legend { display: flex; gap: 12px; font-size: 12px; color: var(--muted); flex-wrap: wrap; }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: right; font-weight: 500; color: var(--muted); font-size: 12px; padding: 4px 8px; border-bottom: 1px solid var(--line); }
th:last-child, td:last-child { text-align: left; width: 100%; }
td { text-align: right; padding: 2px 8px; white-space: nowrap; font-variant-numeric: tabular-nums; }
tr.row:hover { background: var(--hover); }
.tw { cursor: pointer; display: inline-block; width: 14px; color: var(--muted); }
.lib { color: var(--muted); }
.loc2 { color: var(--muted); margin-left: 8px; }
.bar { display: inline-block; height: 8px; background: var(--accent); border-radius: 2px; vertical-align: 0; margin-right: 6px; opacity: 0.7; }
.empty { color: var(--muted); padding: 12px 0; }
.scroll { overflow-x: auto; }
</style>
</head>
<body>
<main>
  <h1 id="title"></h1>
  <div class="sub" id="header"></div>
  <div class="findings" id="findings"></div>

  <section>
    <h2>Memory over time <span class="hint" id="tl-hint"></span></h2>
    <svg id="timeline" role="img" aria-label="Live heap memory over time"></svg>
  </section>

  <section>
    <div class="controls">
      <div class="tabs" role="tablist" id="tabs"></div>
      <label><input type="checkbox" id="mine" checked> Collapse code that isn't yours</label>
      <div class="legend">
        <span><span class="sw" style="background:var(--mine)"></span>your code</span>
        <span><span class="sw" style="background:var(--lib)"></span>libraries and stdlib</span>
      </div>
    </div>
    <h2 id="view-title"></h2>
    <div class="crumbs" id="crumbs"></div>
    <div id="flame"></div>
  </section>

  <section>
    <h2>Call tree <span class="hint">click a row to expand, or a function in the flame graph to zoom</span></h2>
    <div class="scroll"><table id="tree"></table></div>
  </section>
</main>
<div id="tip"></div>
<script type="application/json" id="data">{{data}}</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const VIEWS = {
  peak: { label: "Peak", title: "What was live at the high-water mark", unit: "bytes" },
  leaks: { label: "Leaks", title: "Still allocated when the program exited", unit: "bytes" },
  temporaries: { label: "Temporaries", title: "Allocations freed right after being made", unit: "count" },
};
const NO_STACK = -1, STARTUP = -2, OUTSIDE = -3;
const SYNTH = { [NO_STACK]: "<no Python stack>", [STARTUP]: "<script startup>", [OUTSIDE]: "<outside your code>" };

function fmtBytes(n) {
  const u = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${n} B` : `${v.toFixed(1)} ${u[i]}`;
}
const fmtCount = n => n.toLocaleString("en-US");
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let state = { view: "peak", mine: true, zoom: [] };

function frameInfo(id) {
  if (id < 0) return { func: SYNTH[id], loc: "", mine: false, synth: true };
  const [func, fi, line, mine] = D.frames[id];
  const file = D.files[fi];
  return { func, loc: file ? `${file}:${line}` : "", mine: !!mine, synth: false };
}

// Frames of a trie node, outermost first.
function pathOf(node) {
  const out = [];
  while (node !== 0) { out.push(D.nodes[2 * node + 1]); node = D.nodes[2 * node]; }
  return out.reverse();
}

// Keep your frames; of each run of library frames keep only its outermost
// one (the call you made into the library); drop the launcher at the root.
function collapse(path) {
  if (!path.length) return [NO_STACK];
  const mine = path.map(id => D.frames[id][3] === 1);
  if (!mine.some(Boolean)) {
    const startup = path.some(id => /(^<frozen runpy>$|[\\/]runpy\.py$)/.test(D.files[D.frames[id][1]]));
    return [startup ? STARTUP : OUTSIDE, path[path.length - 1]];
  }
  const out = [];
  let i = mine.indexOf(true);
  for (; i < path.length; i++) {
    if (mine[i] || !(i > 0 && !mine[i - 1])) out.push(path[i]);
  }
  return out;
}

function buildTree() {
  const flat = D.views[state.view] || [];
  const unitBytes = VIEWS[state.view].unit === "bytes";
  const root = { id: null, total: 0, self: 0, native: 0, kids: new Map() };
  for (let i = 0; i < flat.length; i += 4) {
    const bytes = flat[i + 1], count = flat[i + 2], native = flat[i + 3];
    const v = unitBytes ? bytes : count;
    if (!v) continue;
    const path0 = pathOf(flat[i]);
    const path = state.mine ? collapse(path0) : (path0.length ? path0 : [NO_STACK]);
    let node = root;
    node.total += v; node.native += native;
    for (const id of path) {
      let k = node.kids.get(id);
      if (!k) { k = { id, total: 0, self: 0, native: 0, kids: new Map(), parent: node }; node.kids.set(id, k); }
      k.total += v; k.native += native;
      node = k;
    }
    node.self += v;
  }
  return root;
}

function fmt(v) { return VIEWS[state.view].unit === "bytes" ? fmtBytes(v) : `${fmtCount(v)}×`; }
function sortedKids(n) { return [...n.kids.values()].sort((a, b) => b.total - a.total); }

// ---- header ----
document.getElementById("title").textContent = `heapwise · ${D.title}`;
document.getElementById("header").textContent = D.header;
document.getElementById("findings").innerHTML =
  (D.findings.length ? D.findings : ["No allocations were recorded."]).map(l => `<p>${esc(l)}</p>`).join("") +
  D.notes.map(l => `<p class="note">${esc(l)}</p>`).join("");

// ---- timeline ----
function drawTimeline() {
  const svg = document.getElementById("timeline");
  const W = svg.clientWidth || 800, H = 140, L = 64, B = 20, T = 8;
  const vals = D.timeline, max = Math.max(1, ...vals);
  const x = i => L + (W - L - 8) * (i / Math.max(1, vals.length - 1));
  const y = v => T + (H - T - B) * (1 - v / max);
  let d = `M${x(0)},${y(0)}`;
  vals.forEach((v, i) => { d += `L${x(i)},${y(v)}`; });
  const area = d + `L${x(vals.length - 1)},${y(0)}L${x(0)},${y(0)}Z`;
  const px = L + (W - L - 8) * (D.duration ? D.peak_time / D.duration : 0);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => {
    const t = D.duration * f, xx = L + (W - L - 8) * f;
    return `<text x="${xx}" y="${H - 4}" text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}">${t.toFixed(t < 10 ? 2 : 1)} s</text>`;
  }).join("");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML =
    `<line x1="${L}" x2="${W - 8}" y1="${y(0)}" y2="${y(0)}" stroke="var(--line)"/>` +
    `<line x1="${L}" x2="${W - 8}" y1="${y(max)}" y2="${y(max)}" stroke="var(--line)" stroke-dasharray="3 3"/>` +
    `<text x="${L - 6}" y="${y(max) + 4}" text-anchor="end">${fmtBytes(max)}</text>` +
    `<text x="${L - 6}" y="${y(0) + 4}" text-anchor="end">0</text>` +
    `<path d="${area}" fill="var(--area)"/><path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.5"/>` +
    (D.peak ? `<line x1="${px}" x2="${px}" y1="${T}" y2="${y(0)}" stroke="var(--accent)" stroke-dasharray="2 3"/>` : "") +
    ticks;
  document.getElementById("tl-hint").textContent = D.peak
    ? `peak ${fmtBytes(D.peak)} at ${D.peak_time.toFixed(2)} s, dashed line`
    : "";
}

// ---- tabs ----
function drawTabs() {
  const tabs = document.getElementById("tabs");
  tabs.innerHTML = "";
  for (const [k, v] of Object.entries(VIEWS)) {
    const b = document.createElement("button");
    b.textContent = v.label;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(k === state.view));
    if (D.views[k] === null) { b.disabled = true; b.title = "Run with --exact to count temporaries"; }
    b.onclick = () => { state.view = k; state.zoom = []; render(); };
    tabs.appendChild(b);
  }
}

// ---- flame graph (icicle: callers on top) ----
const tip = document.getElementById("tip");
function showTip(e, node, rootTotal) {
  const f = frameInfo(node.id);
  const pct = rootTotal ? (100 * node.total / rootTotal).toFixed(1) : "0";
  const native = VIEWS[state.view].unit === "bytes" && node.native ? ` · ${fmtBytes(node.native)} native` : "";
  tip.innerHTML = `<b>${esc(f.func)}</b> <span class="loc">${esc(f.loc)}</span><br>` +
    `${fmt(node.total)} (${pct}%)${native}` + (node.self ? ` · ${fmt(node.self)} allocated here` : "");
  tip.style.display = "block";
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(e.clientX + 12, innerWidth - r.width - 8) + "px";
  tip.style.top = (e.clientY + 14 + r.height > innerHeight ? e.clientY - r.height - 10 : e.clientY + 14) + "px";
}
document.addEventListener("scroll", () => { tip.style.display = "none"; }, true);

function nodeAt(root, ids) {
  let n = root;
  for (const id of ids) { const k = n.kids.get(id); if (!k) return null; n = k; }
  return n;
}

function drawFlame(root) {
  const el = document.getElementById("flame");
  const crumbs = document.getElementById("crumbs");
  el.innerHTML = "";
  if (!root.total) {
    el.innerHTML = `<div class="empty">Nothing in this view.</div>`;
    el.style.height = "auto"; crumbs.innerHTML = "";
    return;
  }
  let focus = nodeAt(root, state.zoom);
  if (!focus) { state.zoom = []; focus = root; }
  crumbs.innerHTML = state.zoom.length
    ? `<a data-z="0">all</a>` + state.zoom.map((id, i) => ` › <a data-z="${i + 1}">${esc(frameInfo(id).func)}</a>`).join("")
    : "Click a box to zoom in.";
  crumbs.querySelectorAll("a").forEach(a => { a.onclick = () => { state.zoom = state.zoom.slice(0, +a.dataset.z); render(); }; });

  const W = el.clientWidth || 800, RH = 20;
  let depth = 0;
  const frag = document.createDocumentFragment();
  const place = (node, x, w, d, path, dim) => {
    if (w < 0.5) return;
    depth = Math.max(depth, d + 1);
    const f = frameInfo(node.id);
    const div = document.createElement("div");
    div.className = "fn" + (dim ? " dim" : "");
    div.style.left = x + "px"; div.style.top = d * RH + "px"; div.style.width = Math.max(w, 1) + "px";
    const hue = f.synth ? "var(--synthetic)" : f.mine ? (d % 2 ? "var(--mine)" : "var(--mine-2)") : (d % 2 ? "var(--lib)" : "var(--lib-2)");
    div.style.background = hue;
    if (w > 30) div.textContent = f.func;
    div.onmousemove = e => showTip(e, node, root.total);
    div.onmouseleave = () => { tip.style.display = "none"; };
    div.onclick = () => { state.zoom = path; tip.style.display = "none"; render(); };
    frag.appendChild(div);
    placeKids(node, x, w, d + 1, path);
  };
  // Children too narrow to see are drawn as one "N more" box, so a node
  // with thousands of small callees still shows where its memory went.
  const placeKids = (node, x, w, d, path) => {
    let cx = x, rest = 0, restN = 0;
    for (const k of sortedKids(node)) {
      const kw = w * k.total / node.total;
      if (kw < 3) { rest += k.total; restN++; continue; }
      place(k, cx, kw, d, [...path, k.id], false);
      cx += kw;
    }
    if (restN && rest) {
      const rw = w * rest / node.total;
      if (rw < 0.5) return;
      depth = Math.max(depth, d + 1);
      const div = document.createElement("div");
      div.className = "fn";
      div.style.left = cx + "px"; div.style.top = d * RH + "px"; div.style.width = Math.max(rw, 1) + "px";
      div.style.background = "var(--synthetic)"; div.style.cursor = "default";
      const label = `${fmtCount(restN)} more`;
      if (rw > 30) div.textContent = label;
      div.onmousemove = e => {
        tip.innerHTML = `<b>${label}</b>, each too small to draw<br>${fmt(rest)} (${(100 * rest / root.total).toFixed(1)}%) in total` +
          `<br><span class="loc">listed in the call tree below</span>`;
        tip.style.display = "block";
        tip.style.left = Math.min(e.clientX + 12, innerWidth - 300) + "px"; tip.style.top = e.clientY + 14 + "px";
      };
      div.onmouseleave = () => { tip.style.display = "none"; };
      frag.appendChild(div);
    }
  };
  // Ancestors of the zoomed node fill the width, dimmed.
  let d = 0;
  for (let i = 0; i < state.zoom.length; i++) {
    const anc = nodeAt(root, state.zoom.slice(0, i));
    if (i > 0) { place({ ...anc, kids: new Map() }, 0, W, d++, state.zoom.slice(0, i), true); }
  }
  if (focus === root) {
    placeKids(root, 0, W, d, []);
  } else {
    place(focus, 0, W, d, state.zoom.slice(), false);
  }
  el.appendChild(frag);
  el.style.height = depth * RH + "px";
}

// ---- call tree ----
let open = new Set();
function key(path) { return path.join("/"); }
function autoOpen(root) {
  open = new Set();
  const walk = (n, path, d) => {
    const kids = sortedKids(n);
    kids.forEach((k, i) => {
      const p = [...path, k.id];
      if (d < 1 || (i === 0 && k.total >= root.total * 0.2)) { open.add(key(p)); walk(k, p, d + 1); }
    });
  };
  walk(root, [], 0);
}
function drawTree(root) {
  const t = document.getElementById("tree");
  const bytes = VIEWS[state.view].unit === "bytes";
  let html = `<tr><th>Total</th><th>%</th><th>Here</th>${bytes ? "<th>Native</th>" : ""}<th>Function</th></tr>`;
  if (!root.total) { t.innerHTML = html; return; }
  const rows = [];
  const walk = (n, path, d) => {
    let shown = 0, rest = 0, restN = 0;
    for (const k of sortedKids(n)) {
      const p = [...path, k.id];
      if (shown >= 200 || (k.total / root.total < 0.001 && shown >= 10)) { rest += k.total; restN++; continue; }
      shown++;
      rows.push([k, p, d]);
      if (open.has(key(p))) walk(k, p, d + 1);
    }
    if (restN) rows.push([{ more: restN, total: rest }, path, d]);
  };
  walk(root, [], 0);
  for (const [k, p, d] of rows) {
    if (k.more) {
      html += `<tr><td>${fmt(k.total)}</td><td>${(100 * k.total / root.total).toFixed(1)}%</td><td></td>${bytes ? "<td></td>" : ""}` +
        `<td class="lib" style="padding-left:${22 + d * 16}px">${fmtCount(k.more)} more, each smaller</td></tr>`;
      continue;
    }
    const f = frameInfo(k.id);
    const pct = 100 * k.total / root.total;
    const tw = k.kids.size ? (open.has(key(p)) ? "▾" : "▸") : "";
    html += `<tr class="row" data-k="${esc(key(p))}"><td>${fmt(k.total)}</td>` +
      `<td><span class="bar" style="width:${Math.max(1, pct * 0.4)}px"></span>${pct.toFixed(1)}%</td>` +
      `<td>${k.self ? fmt(k.self) : ""}</td>` +
      (bytes ? `<td>${k.native ? fmtBytes(k.native) : ""}</td>` : "") +
      `<td class="mono" style="padding-left:${8 + d * 16}px"><span class="tw">${tw}</span>` +
      `<span class="${f.mine ? "" : "lib"}">${esc(f.func)}</span><span class="loc2">${esc(f.loc)}</span></td></tr>`;
  }
  t.innerHTML = html;
  t.querySelectorAll("tr.row").forEach(r => {
    r.onclick = () => { const k = r.dataset.k; open.has(k) ? open.delete(k) : open.add(k); drawTree(root); };
  });
}

let lastTreeKey = "";
function render() {
  drawTabs();
  const v = VIEWS[state.view];
  const root = buildTree();
  const whole = state.view === "peak" ? `${fmtBytes(D.peak)} at peak` : state.view === "leaks" ? `${fmtBytes(D.leaked)} at exit` : `${fmtCount(root.total)} allocations`;
  document.getElementById("view-title").innerHTML = `${esc(v.title)} <span class="hint">${esc(whole)}${D.sampled && v.unit === "bytes" ? ", estimated from samples" : ""}</span>`;
  drawFlame(root);
  const tk = `${state.view}/${state.mine}`;
  if (tk !== lastTreeKey) { autoOpen(root); lastTreeKey = tk; }
  drawTree(root);
}

document.getElementById("mine").onchange = e => { state.mine = e.target.checked; state.zoom = []; render(); };
let rt;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { drawTimeline(); render(); }, 100); });
drawTimeline();
render();
</script>
</body>
</html>
"""
