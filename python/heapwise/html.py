"""Self-contained HTML report: findings, memory timeline, flame graph and call tree.

The page has no external dependencies, so it opens offline and can be attached
to a bug report as a single file.
"""

from __future__ import annotations

import html
import json
import os
import re
import sysconfig

from ._native import MAX_STACK_DEPTH, Capture

_VIEWS = ("peak", "leaks", "temporaries")


def _short(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/\\") + os.sep):
        return path[len(cwd.rstrip("/\\")) + 1 :]
    return path


_SITE_DIR = re.compile(r"[\\/](?:site|dist)-packages[\\/]([^\\/]+)")
_FROZEN = re.compile(r"^<frozen ([\w.]+)>$")


def _module_name(component: str) -> str:
    name = component.split(".", 1)[0] if component.endswith((".py", ".so", ".pyd")) else component
    return name.split("-", 1)[0]  # foo-1.0.dist-info and the like


class _Classifier:
    """Sorts a file into your code, a third-party package, the stdlib, or
    heapwise itself, with the package or module name to show."""

    def __init__(self, exclude: list[str]):
        paths = sysconfig.get_paths()
        self.stdlib = sorted(
            {p.rstrip("/\\") for k in ("stdlib", "platstdlib") if (p := paths.get(k))}, key=len, reverse=True
        )
        self.tool = os.path.dirname(os.path.abspath(__file__))
        self.exclude = exclude

    def __call__(self, file: str, cwd: str | None) -> tuple[str, str, str]:
        """(path to show, kind, package). Library and stdlib paths are shown
        from the package directory, e.g. numpy/core/numeric.py."""
        if not file:
            return file, "std", ""
        m = _FROZEN.match(file)
        if m:
            return file, "std", m.group(1).split(".", 1)[0].lstrip("_")
        if file.startswith("<"):
            return file, "std", ""
        if file.startswith(self.tool):
            return "heapwise" + file[len(self.tool) :], "tool", "heapwise"
        m = _SITE_DIR.search(file)
        if m:
            return file[m.start(1) :], "lib", _module_name(m.group(1))
        for root in self.stdlib:
            if file.startswith(root + os.sep):
                rest = file[len(root) + 1 :]
                parts = rest.split(os.sep)
                if parts[0] == "lib-dynload" and len(parts) > 1:
                    parts = parts[1:]
                return rest, "std", _module_name(parts[0])
        if any(p and file.startswith(p) for p in self.exclude):
            return os.path.basename(file), "tool", "heapwise"
        return _short(file, cwd), "mine", ""


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
    files: list[list] = []  # [short path, kind, package]
    classify = _Classifier(exclude)
    file_ids: dict[str, int] = {}
    canon: dict[tuple[str, str, int], int] = {}
    remap = []
    for func, file, line in capture.frames():
        k = (func, file, line)
        if k not in canon:
            canon[k] = len(frames)
            if file not in file_ids:
                file_ids[file] = len(files)
                files.append(list(classify(file, cwd)))
            frames.append([func, file_ids[file], line, 1 if files[file_ids[file]][1] == "mine" else 0])
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
        "max_depth": MAX_STACK_DEPTH,
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
#breakdown { margin: 4px 0 14px; }
.bd-title { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.bd-bar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; background: var(--line); }
.bd-bar span { height: 100%; transition: opacity 160ms ease; }
.bd-legend { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip { border: 1px solid var(--line); background: transparent; color: var(--text); border-radius: 999px; padding: 2px 10px 2px 8px;
  font: 12px/18px system-ui, -apple-system, "Segoe UI", sans-serif; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
.chip b { font-weight: 600; font-variant-numeric: tabular-nums; }
.chip[aria-pressed="true"] { border-color: var(--text); }
.chip .sw { margin: 0; }
.fn.hl-off { opacity: 0.18; }
.fn .nat { position: absolute; left: 0; bottom: 0; height: 4px; background: #7c3aed; pointer-events: none; }
.pkg { display: inline-block; font: 11px/16px system-ui, -apple-system, "Segoe UI", sans-serif; color: #1d1b19; border-radius: 4px;
  padding: 0 5px; margin-right: 6px; vertical-align: 1px; }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: right; font-weight: 500; color: var(--muted); font-size: 12px; padding: 4px 8px; border-bottom: 1px solid var(--line); }
th:last-child, td:last-child { text-align: left; width: 100%; }
td { text-align: right; padding: 0 8px; white-space: nowrap; font-variant-numeric: tabular-nums; }
td > .c { padding: 2px 0; overflow: hidden; max-height: 24px; }
tr.enter td > .c { animation: row-in 180ms ease-out both; animation-delay: var(--delay, 0ms); }
tr.leave td > .c { animation: row-out 150ms ease-in both; }
@keyframes row-in { from { max-height: 0; padding: 0; opacity: 0; transform: translateY(-4px); } }
@keyframes row-out { to { max-height: 0; padding: 0; opacity: 0; } }
.fn.anim { transition: left 260ms ease, width 260ms ease, top 260ms ease, opacity 200ms ease; }
.fn.fade { opacity: 0; }
.fade-in { animation: fade-in 200ms ease-out both; }
@keyframes fade-in { from { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) {
  tr.enter td > .c, tr.leave td > .c, .fade-in { animation: none; }
  .fn.anim, .tw { transition: none; }
}
tr.row:hover { background: var(--hover); }
.tw { cursor: pointer; display: inline-block; width: 14px; text-align: center; color: var(--muted); transition: transform 180ms ease; }
.tw.open { transform: rotate(90deg); }
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
    </div>
    <div id="breakdown">
      <div class="bd-title">Who allocated it. Click a name to highlight its boxes in the graph; a purple underline marks memory allocated by native (C) code.</div>
      <div class="bd-bar" id="bd-bar"></div>
      <div class="bd-legend" id="bd-legend"></div>
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
const NO_STACK = -1, STARTUP = -2, OUTSIDE = -3, DEEP = -4;
const SYNTH = {
  [NO_STACK]: "<no Python stack>", [STARTUP]: "<script startup>", [OUTSIDE]: "<outside your code>",
  [DEEP]: "<deep stack, outer frames cut>",
};

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
  const file = D.files[fi][0];
  return { func, loc: file ? `${file}:${line}` : "", mine: !!mine, synth: false };
}

// ---- who is who: your code, each library, the stdlib ----
const LIB_COLORS = ["#60a5fa", "#2dd4bf", "#4ade80", "#f472b6", "#22d3ee", "#a3e635", "#fbbf24", "#93c5fd"];
const CATS = {
  mine: { label: "Your code", color: "#f59e0b" },
  std: { label: "Python standard library", color: "#a8a29e" },
  tool: { label: "heapwise launcher", color: "#d6d3d1" },
  native: { label: "Native code (C extensions)", color: "#a78bfa" },
  deep: { label: "Deep stacks, outer frames cut", color: "#d6d3d1" },
  startup: { label: "Python loading your script", color: "#d6d3d1" },
  outside: { label: "Outside your code", color: "#d6d3d1" },
};
const SYNTH_CAT = { [NO_STACK]: "native", [STARTUP]: "startup", [OUTSIDE]: "outside", [DEEP]: "deep" };
function catOf(id) {
  if (id < 0) return SYNTH_CAT[id];
  const [, kind, pkg] = D.files[D.frames[id][1]];
  return kind === "lib" ? `lib:${pkg}` : kind;
}
function catInfo(c) {
  if (CATS[c]) return CATS[c];
  const pkg = c.slice(4);
  return (CATS[c] = { label: pkg, color: LIB_COLORS[libOrder.length % LIB_COLORS.length], lib: true, order: libOrder.push(c) });
}
const libOrder = [];
// Give the libraries that allocate the most the most distinct colors.
(function assignLibColors() {
  const size = new Map();
  for (const flat of Object.values(D.views)) {
    if (!flat) continue;
    for (let i = 0; i < flat.length; i += 4) {
      let n = flat[i];
      while (n !== 0) {
        const c = catOf(D.nodes[2 * n + 1]);
        if (c.startsWith("lib:")) size.set(c, (size.get(c) || 0) + flat[i + 1] + flat[i + 2]);
        n = D.nodes[2 * n];
      }
    }
  }
  [...size.entries()].sort((a, b) => b[1] - a[1]).forEach(([c]) => catInfo(c));
})();
function pkgOf(id) { return id < 0 ? "" : D.files[D.frames[id][1]][2]; }
function catDescription(id) {
  const c = catOf(id), pkg = pkgOf(id);
  if (c === "mine") return "your code";
  if (c.startsWith("lib:")) return `library: ${pkg}`;
  if (c === "std") return pkg ? `Python standard library (${pkg})` : "Python standard library";
  return catInfo(c).label;
}
function boxLabel(id, func) {
  const c = catOf(id), pkg = pkgOf(id);
  return (c.startsWith("lib:") || c === "std") && pkg && !func.startsWith(pkg) ? `${pkg} · ${func}` : func;
}
let highlight = null;

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
    const startup = path.some(id => /(^<frozen runpy>$|^runpy\.py$)/.test(D.files[D.frames[id][1]][0]));
    // The walk keeps the innermost max_depth frames, so a deep library call
    // (nested imports, say) can lose the frames of yours at its root.
    const kind = startup ? STARTUP : path.length >= D.max_depth ? DEEP : OUTSIDE;
    return [kind, path[path.length - 1]];
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
    b.onclick = () => { state.view = k; state.zoom = []; render(); fadeIn(); };
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
    `<span class="sw" style="background:${catInfo(catOf(node.id)).color}"></span>${esc(catDescription(node.id))}<br>` +
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

// Set by zoom clicks: boxes then glide from where they were to where they go.
let animateZoom = false;
function drawFlame(root) {
  const el = document.getElementById("flame");
  const crumbs = document.getElementById("crumbs");
  const before = new Map();
  if (animateZoom && !reduceMotion()) {
    for (const b of el.querySelectorAll(".fn[data-k]")) {
      before.set(b.dataset.k, [b.style.left, b.style.width, b.style.top]);
    }
  }
  animateZoom = false;
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
  crumbs.querySelectorAll("a").forEach(a => {
    a.onclick = () => { state.zoom = state.zoom.slice(0, +a.dataset.z); animateZoom = true; render(); };
  });

  const W = el.clientWidth || 800, RH = 20;
  let depth = 0;
  const frag = document.createDocumentFragment();
  const place = (node, x, w, d, path, dim) => {
    if (w < 0.5) return;
    depth = Math.max(depth, d + 1);
    const f = frameInfo(node.id);
    const div = document.createElement("div");
    div.className = "fn" + (dim ? " dim" : "");
    div.dataset.k = key(path);
    div.style.left = x + "px"; div.style.top = d * RH + "px"; div.style.width = Math.max(w, 1) + "px";
    const cat = catOf(node.id);
    div.style.background = catInfo(cat).color;
    if (d % 2) div.style.filter = "brightness(0.93)";  // tell neighbouring rows apart
    if (highlight && cat !== highlight && !dim) div.classList.add("hl-off");
    if (w > 30) div.textContent = boxLabel(node.id, f.func);
    // Purple underline: the part of this box's memory that native (C) code allocated.
    if (VIEWS[state.view].unit === "bytes" && node.native > 0 && node.total > 0) {
      const nat = document.createElement("i");
      nat.className = "nat";
      nat.style.width = Math.min(100, 100 * node.native / node.total) + "%";
      div.appendChild(nat);
    }
    div.onmousemove = e => showTip(e, node, root.total);
    div.onmouseleave = () => { tip.style.display = "none"; };
    div.onclick = () => { state.zoom = path; tip.style.display = "none"; animateZoom = true; render(); };
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
      div.dataset.k = key(path) + "/more";
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
  if (before.size) {
    const boxes = [...el.querySelectorAll(".fn")];
    const target = boxes.map(b => [b.style.left, b.style.width, b.style.top]);
    boxes.forEach(b => {
      const old = before.get(b.dataset.k);
      if (old) [b.style.left, b.style.width, b.style.top] = old;
      else b.classList.add("fade");
    });
    void el.offsetWidth; // commit the starting positions before transitioning
    boxes.forEach((b, i) => {
      b.classList.add("anim");
      b.classList.remove("fade");
      [b.style.left, b.style.width, b.style.top] = target[i];
    });
  }
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
// `enter`: key of a row just expanded; its new descendants animate in.
function drawTree(root, enter) {
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
  const cell = (inner, attrs = "") => `<td${attrs}><div class="c">${inner}</div></td>`;
  // Rows under the just-expanded one slide in, slightly staggered.
  let entering = 0;
  const enterAttrs = (p, cls) => {
    if (enter === undefined || !key(p).startsWith(enter + "/")) return cls ? ` class="${cls}"` : "";
    return ` class="${cls ? cls + " " : ""}enter" style="--delay:${Math.min(entering++, 12) * 12}ms"`;
  };
  for (const [k, p, d] of rows) {
    if (k.more) {
      html += `<tr${enterAttrs([...p, "more"], "")} data-parent="${esc(key(p))}">${cell(fmt(k.total))}${cell(`${(100 * k.total / root.total).toFixed(1)}%`)}${cell("")}${bytes ? cell("") : ""}` +
        cell(`${fmtCount(k.more)} more, each smaller`, ` class="lib" style="padding-left:${22 + d * 16}px"`) + `</tr>`;
      continue;
    }
    const f = frameInfo(k.id);
    const pct = 100 * k.total / root.total;
    const isOpen = open.has(key(p));
    const tw = k.kids.size ? `<span class="tw${isOpen ? " open" : ""}">▸</span>` : `<span class="tw"></span>`;
    html += `<tr${enterAttrs(p, "row")} data-k="${esc(key(p))}">${cell(fmt(k.total))}` +
      cell(`<span class="bar" style="width:${Math.max(1, pct * 0.4)}px"></span>${pct.toFixed(1)}%`) +
      cell(k.self ? fmt(k.self) : "") +
      (bytes ? cell(k.native ? fmtBytes(k.native) : "") : "") +
      cell(`${tw}${badge(k.id)}<span class="${f.mine ? "" : "lib"}">${esc(f.func)}</span><span class="loc2">${esc(f.loc)}</span>`,
        ` class="mono" style="padding-left:${8 + d * 16}px"`) + `</tr>`;
  }
  t.innerHTML = html;
  t.querySelectorAll("tr.row").forEach(r => {
    r.onclick = () => {
      const k = r.dataset.k;
      if (!open.has(k)) {
        open.add(k);
        drawTree(root, k);
        return;
      }
      open.delete(k);
      // Animate the subtree out, then redraw without it.
      r.querySelector(".tw")?.classList.remove("open");
      const gone = [...t.querySelectorAll("tr")].filter(x => {
        const xk = x.dataset.k ?? x.dataset.parent;  // "N more" rows carry their parent's key
        return xk !== undefined && (xk === k && x.dataset.parent !== undefined || xk.startsWith(k + "/"));
      });
      if (!gone.length || reduceMotion()) { drawTree(root); return; }
      gone.forEach(x => x.classList.add("leave"));
      setTimeout(() => drawTree(root), 150);
    };
  });
}

function badge(id) {
  const c = catOf(id);
  if (c === "mine") return "";
  const text = c.startsWith("lib:") ? pkgOf(id) : c === "std" ? (pkgOf(id) ? `stdlib · ${pkgOf(id)}` : "stdlib") : "";
  return text ? `<span class="pkg" style="background:${catInfo(c).color}">${esc(text)}</span>` : "";
}

// ---- breakdown: who allocated the memory in this view ----
function drawBreakdown() {
  const flat = D.views[state.view] || [];
  const unitBytes = VIEWS[state.view].unit === "bytes";
  const by = new Map();
  let total = 0;
  const add = (c, v) => { if (v > 0) { by.set(c, (by.get(c) || 0) + v); total += v; } };
  for (let i = 0; i < flat.length; i += 4) {
    let v = unitBytes ? flat[i + 1] : flat[i + 2];
    if (!v) continue;
    // malloc/mmap from C code: we know it was native, not which extension,
    // so it gets its own share rather than the calling Python function's.
    if (unitBytes) { add("native", flat[i + 3]); v -= flat[i + 3]; }
    const path = pathOf(flat[i]);
    let c;
    if (!path.length) c = "native";
    else {
      c = catOf(path[path.length - 1]);
      if (c === "std" || c === "tool") {
        // Stdlib called from your code counts as the stdlib; with no frame
        // of yours anywhere it is the interpreter starting your script.
        const head = collapse(path)[0];
        if (head === STARTUP) c = "startup";
        else if (head === DEEP) c = "deep";
      }
    }
    add(c, v);
  }
  const items = [...by.entries()].sort((a, b) => b[1] - a[1]);
  if (highlight && !by.has(highlight)) highlight = null;
  document.getElementById("bd-bar").innerHTML = items.map(([c, v]) =>
    `<span title="${esc(catInfo(c).label)}" style="width:${100 * v / total}%;background:${catInfo(c).color};opacity:${!highlight || highlight === c ? 1 : 0.25}"></span>`).join("");
  const legend = document.getElementById("bd-legend");
  legend.innerHTML = items.map(([c, v]) => {
    const pct = 100 * v / total;
    return `<button class="chip" data-c="${esc(c)}" aria-pressed="${highlight === c}"><span class="sw" style="background:${catInfo(c).color}"></span>` +
      `${esc(catInfo(c).label)} <b>${pct < 0.1 ? "<0.1" : pct.toFixed(1)}%</b></button>`;
  }).join("");
  legend.querySelectorAll(".chip").forEach(b => {
    b.onclick = () => { highlight = highlight === b.dataset.c ? null : b.dataset.c; drawBreakdown(); drawFlame(buildTree()); };
  });
}

const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

let lastTreeKey = "";
function render() {
  drawTabs();
  const v = VIEWS[state.view];
  const root = buildTree();
  const whole = state.view === "peak" ? `${fmtBytes(D.peak)} at peak` : state.view === "leaks" ? `${fmtBytes(D.leaked)} at exit` : `${fmtCount(root.total)} allocations`;
  document.getElementById("view-title").innerHTML = `${esc(v.title)} <span class="hint">${esc(whole)}${D.sampled && v.unit === "bytes" ? ", estimated from samples" : ""}</span>`;
  drawBreakdown();
  drawFlame(root);
  const tk = `${state.view}/${state.mine}`;
  if (tk !== lastTreeKey) { autoOpen(root); lastTreeKey = tk; }
  drawTree(root);
}

function fadeIn() {
  for (const id of ["flame", "tree"]) {
    const el = document.getElementById(id);
    el.classList.remove("fade-in");
    void el.offsetWidth;
    el.classList.add("fade-in");
  }
}
document.getElementById("mine").onchange = e => { state.mine = e.target.checked; state.zoom = []; render(); fadeIn(); };
let rt;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { drawTimeline(); render(); }, 100); });
drawTimeline();
render();
</script>
</body>
</html>
"""
