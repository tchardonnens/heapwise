"""Self-contained HTML report: key numbers, memory timeline, flame graph and call tree.

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
    rss: int | None = None,
) -> dict:
    """Everything the page needs, as plain JSON-able data."""
    exclude = list(exclude or [])
    summary = capture.summary(title, exclude, cwd, 5).splitlines()
    header = summary[0] if summary else title
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
    # The biggest site in each view, by innermost frame outside ``exclude``,
    # for the key-number tiles at the top of the page.
    top = {}
    for view in _VIEWS:
        rows = [] if values[view] is None else capture.top(view, exclude, 1)
        if rows:
            func, file, *rest = rows[0]
            path, kind, pkg = classify(file, cwd)
            top[view] = [func, path, *rest, kind, pkg]  # rest: line, bytes, count, native
        else:
            top[view] = None
    prefix = f"heapwise · {title} · "
    return {
        "title": title,
        "header": header.removeprefix(prefix),
        "notes": list(notes or []),
        "duration": capture.duration_seconds,
        "peak": capture.peak_bytes,
        "peak_native": capture.peak_native_bytes,
        "peak_time": capture.peak_seconds,
        "leaked": capture.leaked_bytes,
        "sampled": capture.sample_rate != 0,
        "allocations": capture.allocation_count,
        "allocated": capture.allocated_bytes,
        "top": top,
        "rss": rss,
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
    rss: int | None = None,
) -> str:
    """Write the report for ``capture`` to ``path`` and return the path."""
    page = render(report_data(capture, title, exclude, cwd, notes, rss))
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
/* Color carries one meaning only: who owns the memory. Everything else is ink
   and hairlines. Owner hues are the first three categorical slots of the
   reference palette (validated all-pairs for color-vision deficiency). */
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11, 11, 11, 0.10); --hover: rgba(11, 11, 11, 0.04);
  --mine: #2a78d6; --mine-ink: #ffffff;
  --lib: #eb6834; --lib-ink: #0b0b0b;
  --native: #1baf7a; --native-ink: #0b0b0b;
  --std: #c3c2b7; --std-ink: #0b0b0b;
  --other: #e1e0d9; --other-ink: #52514e;
  --root: #f0efec; --root-ink: #0b0b0b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255, 255, 255, 0.10); --hover: rgba(255, 255, 255, 0.05);
    --mine: #3987e5; --mine-ink: #0b0b0b;
    --lib: #d95926; --lib-ink: #0b0b0b;
    --native: #199e70; --native-ink: #0b0b0b;
    --std: #52514e; --std-ink: #f0efec;
    --other: #2c2c2a; --other-ink: #c3c2b7;
    --root: #383835; --root-ink: #f0efec;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255, 255, 255, 0.10); --hover: rgba(255, 255, 255, 0.05);
  --mine: #3987e5; --mine-ink: #0b0b0b;
  --lib: #d95926; --lib-ink: #0b0b0b;
  --native: #199e70; --native-ink: #0b0b0b;
  --std: #52514e; --std-ink: #f0efec;
  --other: #2c2c2a; --other-ink: #c3c2b7;
  --root: #383835; --root-ink: #f0efec;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 0 0 2px; font-weight: 600; }
.sub { color: var(--ink-2); font-size: 13px; }
.panel { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 16px; margin-top: 16px; }

/* Key numbers: one hero tile (the peak), four supporting tiles. Each tile is
   label / value / one line of context. */
.bento { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 14px 16px; min-width: 0;
  display: flex; flex-direction: column; gap: 2px; text-align: left; color: inherit; font: inherit; }
button.tile { cursor: pointer; transition: box-shadow 160ms ease, transform 160ms ease; }
button.tile:hover { box-shadow: 0 0 0 1px var(--axis); }
button.tile:active { transform: translateY(1px); }
.tile .label { color: var(--ink-2); font-size: 13px; }
.tile .value { font-size: 26px; font-weight: 600; line-height: 1.2; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.tile .value.quiet { font-size: 20px; color: var(--ink-2); font-weight: 500; }
.tile .ctx { color: var(--ink-2); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tile .ctx .sw { margin-right: 6px; vertical-align: -1px; }
.tile .loc3 { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tile.hero { grid-column: span 2; grid-row: span 2; padding: 18px 20px; }
.tile.hero .value { font-size: 52px; line-height: 1.1; margin: 4px 0 2px; letter-spacing: -0.02em; }
.hero .split { margin: 14px 0 8px; }
.hero ul { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px 16px; font-size: 13px; }
.hero li { display: flex; align-items: center; gap: 7px; min-width: 0; }
.hero li .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hero li .v { margin-left: auto; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.hero .foot { margin-top: auto; padding-top: 10px; color: var(--muted); font-size: 12px; }
@media (max-width: 760px) {
  .bento { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .tile.hero { grid-row: auto; }
  .tile.hero .value { font-size: 44px; }
  .tile .value { font-size: 22px; }
  .tile .ctx { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .tile .loc3 { display: none; }
}
@media (max-width: 420px) { .hero ul { grid-template-columns: 1fr; } }
h2 { font-size: 15px; font-weight: 600; margin: 0 0 4px; }
.hint { color: var(--ink-2); font-size: 13px; margin: 0 0 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }

#timeline { width: 100%; height: 190px; display: block; touch-action: pan-y; cursor: crosshair; }
#timeline text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
#timeline .peak-label { fill: var(--ink-2); font-weight: 600; }
#timeline .hover { pointer-events: none; }

.controls { display: flex; flex-wrap: wrap; gap: 10px 20px; align-items: center; margin-bottom: 14px; }
.tabs { display: inline-flex; background: var(--page); border: 1px solid var(--ring); border-radius: 8px; padding: 2px; }
.tabs button { border: 0; background: transparent; color: var(--ink-2); padding: 5px 14px; font: inherit; border-radius: 6px; cursor: pointer; }
.tabs button[aria-selected="true"] { background: var(--surface); color: var(--ink); box-shadow: 0 0 0 1px var(--ring); font-weight: 600; }
.tabs button:disabled { color: var(--muted); cursor: not-allowed; }
label { color: var(--ink-2); font-size: 13px; cursor: pointer; user-select: none; display: inline-flex; gap: 6px; align-items: center; }

/* Owner legend + split bar */
.split { display: flex; height: 8px; gap: 2px; margin: 4px 0 10px; }
.split span { border-radius: 4px; min-width: 2px; transition: opacity 160ms ease; }
.owners { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 18px; }
.chip { border: 1px solid var(--ring); background: var(--surface); color: var(--ink); border-radius: 999px; padding: 3px 12px 3px 8px;
  font: 13px/18px system-ui, -apple-system, "Segoe UI", sans-serif; cursor: pointer; display: inline-flex; align-items: center; gap: 7px; }
.chip .v { color: var(--ink-2); font-variant-numeric: tabular-nums; }
.chip .what { color: var(--muted); font-size: 12px; }
.chip[aria-pressed="true"] { box-shadow: 0 0 0 2px var(--ink); border-color: transparent; }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 3px; flex: none; }
.chip, .chip .v { white-space: nowrap; }
@media (max-width: 640px) { .chip .what { display: none; } .tabs button { padding: 5px 10px; } }

/* Cascade: one row per call depth; every box is exactly as wide as the
   boxes under it put together. */
.crumbs { font-size: 13px; color: var(--ink-2); margin-bottom: 8px; min-height: 20px; }
.crumbs a { color: var(--ink); cursor: pointer; text-decoration: underline; text-decoration-color: var(--axis); text-underline-offset: 3px; }
#flame { position: relative; width: 100%; overflow: hidden; }
.box { position: absolute; height: 24px; padding: 0 6px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  font: 12px/24px system-ui, -apple-system, "Segoe UI", sans-serif; border-radius: 4px; cursor: pointer; }
.box .val { opacity: 0.8; margin-left: 6px; font-variant-numeric: tabular-nums; }
.box:hover { box-shadow: inset 0 0 0 2px var(--ink); }
.box.mine { --c: var(--mine); background: var(--mine); color: var(--mine-ink); }
.box.lib { --c: var(--lib); background: var(--lib); color: var(--lib-ink); }
.box.native { --c: var(--native); background: var(--native); color: var(--native-ink); }
.box.std { --c: var(--std); background: var(--std); color: var(--std-ink); }
.box.other { --c: var(--other); background: var(--other); color: var(--other-ink); }
.box.root { background: var(--root); color: var(--root-ink); font-weight: 600; }
/* Memory allocated directly in a function (not in something it called):
   a lighter tint of the owner's color, so it reads as "the rest of the
   box above" rather than as another function. */
.box.here { background: color-mix(in srgb, var(--c) 42%, var(--surface)); color: var(--ink); cursor: default;
  box-shadow: inset 0 2px 0 var(--c); }
.box.here:hover { box-shadow: inset 0 0 0 2px var(--ink); }
.box.more { cursor: default; }
.box.thin { padding: 0; }
.box.off { opacity: 0.2; }
.box.anim { transition: left 260ms ease, width 260ms ease, top 260ms ease, opacity 200ms ease; }
.box.fade { opacity: 0; }

#tip { position: fixed; pointer-events: none; background: var(--ink); color: var(--surface); padding: 8px 10px; border-radius: 8px;
  font-size: 12px; line-height: 1.5; max-width: 440px; display: none; z-index: 10; }
#tip .loc { opacity: 0.7; }
#tip .sw { vertical-align: -1px; margin-right: 5px; }

/* Call tree */
table { width: 100%; border-collapse: collapse; }
th { text-align: right; font-weight: 500; color: var(--muted); font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--grid); }
th:last-child, td:last-child { text-align: left; width: 100%; }
td { text-align: right; padding: 0 8px; white-space: nowrap; font-variant-numeric: tabular-nums; }
td > .c { padding: 3px 0; overflow: hidden; max-height: 28px; }
tr.row { cursor: pointer; }
tr.row:hover, tr.here:hover { background: var(--hover); }
.tw { display: inline-block; width: 14px; text-align: center; color: var(--muted); transition: transform 180ms ease; }
.tw.open { transform: rotate(90deg); }
.fname .sw { margin-right: 7px; vertical-align: -1px; }
.loc2 { color: var(--muted); margin-left: 8px; }
.pkg { color: var(--ink-2); font-size: 12px; margin-right: 6px; }
.dim { color: var(--ink-2); }
.pct { display: inline-flex; align-items: center; gap: 8px; justify-content: flex-end; }
.pct i { display: inline-block; height: 6px; border-radius: 3px; background: var(--axis); }
.empty { color: var(--muted); padding: 12px 0; }
.scroll { overflow-x: auto; }
tr.enter td > .c { animation: row-in 180ms ease-out both; animation-delay: var(--delay, 0ms); }
tr.leave td > .c { animation: row-out 150ms ease-in both; }
@keyframes row-in { from { max-height: 0; padding: 0; opacity: 0; transform: translateY(-4px); } }
@keyframes row-out { to { max-height: 0; padding: 0; opacity: 0; } }
.fade-in { animation: fade-in 200ms ease-out both; }
@keyframes fade-in { from { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) {
  tr.enter td > .c, tr.leave td > .c, .fade-in { animation: none; }
  .box.anim, .tw, .split span { transition: none; }
}
@media (forced-colors: active) { .box { border: 1px solid CanvasText; } }
</style>
</head>
<body>
<main>
  <h1 id="title"></h1>
  <div class="sub" id="header"></div>
  <div class="bento" id="bento"></div>

  <div class="panel" id="p-time">
    <h2>Memory over time</h2>
    <p class="hint" id="tl-hint"></p>
    <svg id="timeline" role="img" aria-label="Live heap memory over time"></svg>
  </div>

  <div class="panel" id="p-cascade">
    <div class="controls">
      <div class="tabs" role="tablist" id="tabs"></div>
      <label><input type="checkbox" id="group" checked> Group library internals</label>
    </div>
    <h2 id="view-title"></h2>
    <p class="hint" id="view-hint"></p>
    <div class="split" id="split" aria-hidden="true"></div>
    <div class="owners" id="owners"></div>
    <div class="crumbs" id="crumbs"></div>
    <div id="flame"></div>
  </div>

  <div class="panel" id="p-tree">
    <h2>Call tree</h2>
    <p class="hint">The same numbers as the cascade. Each row's total is the sum of the rows under it; click a row to open it.</p>
    <div class="scroll"><table id="tree"></table></div>
  </div>
</main>
<div id="tip"></div>
<script type="application/json" id="data">{{data}}</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const VIEWS = {
  peak: { label: "Peak", title: "What was using memory at the peak", root: "All memory at the peak", unit: "bytes" },
  leaks: { label: "Leaks", title: "What was still allocated when the program ended", root: "All memory left at exit", unit: "bytes" },
  temporaries: { label: "Temporaries", title: "Where short-lived allocations come from", root: "All temporary allocations", unit: "count" },
};
const NO_STACK = -1, STARTUP = -2, OUTSIDE = -3, DEEP = -4;
const SYNTH = {
  [NO_STACK]: "C code with no Python caller",
  [STARTUP]: "Python loading your script",
  [OUTSIDE]: "Outside your code",
  [DEEP]: "Deep call stack (outer frames cut)",
};
const OWNERS = {
  mine: { label: "Your code", what: "functions in your files" },
  lib: { label: "Libraries", what: "" },
  std: { label: "Python standard library", what: "json, re, collections…" },
  native: { label: "C code", what: "malloc/mmap from extensions" },
  other: { label: "Python internals", what: "startup, imports" },
};
const OWNER_ORDER = ["mine", "lib", "std", "native", "other"];

function fmtBytes(n) {
  const u = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${n} B` : `${v.toFixed(1)} ${u[i]}`;
}
const fmtCount = n => n.toLocaleString("en-US");
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const bytesView = () => VIEWS[state.view].unit === "bytes";
const fmt = v => bytesView() ? fmtBytes(v) : `${fmtCount(v)}×`;
const pctOf = (v, whole) => whole ? 100 * v / whole : 0;
const fmtPct = p => p >= 10 ? `${p.toFixed(0)}%` : p >= 0.1 ? `${p.toFixed(1)}%` : p > 0 ? "<0.1%" : "0%";
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

let state = { view: "peak", group: true, zoom: [], highlight: null };

// ---- frames and who owns them ----
function fileOf(id) { return D.files[D.frames[id][1]]; }  // [path, kind, package]
function frameInfo(id) {
  if (id < 0) return { func: SYNTH[id], loc: "", pkg: "" };
  const [raw, , line] = D.frames[id];
  const [file, , pkg] = fileOf(id);
  // "<module>" is a file's top-level code; say which file.
  const base = file.split(/[\\/]/).pop().replace(/\.py$/, "");
  const func = raw === "<module>" && base ? `${base} (top level)` : raw;
  return { func, loc: file ? `${file}:${line}` : "", pkg };
}
function ownerOf(id) {
  if (id === NO_STACK) return "native";
  if (id < 0) return "other";
  const kind = fileOf(id)[1];
  return kind === "tool" ? "other" : kind;
}
function ownerText(id) {
  const o = ownerOf(id), pkg = frameInfo(id).pkg;
  if (o === "mine") return "Your code";
  if (o === "lib") return `Library: ${pkg}`;
  if (o === "std") return pkg ? `Python standard library (${pkg})` : "Python standard library";
  return OWNERS[o].label;
}
function frameLabel(id) {
  const f = frameInfo(id), o = ownerOf(id);
  return (o === "lib" || o === "std") && f.pkg && !f.func.startsWith(f.pkg) && !f.func.endsWith("(top level)")
    ? `${f.pkg}.${f.func}` : f.func;
}

// One box per function, not per line: frames that differ only by line
// share the first such frame's id, and nodes remember which lines they cover.
const funcOf = (() => {
  const first = new Map();
  return D.frames.map(([func, fi], id) => {
    const k = `${fi}\u0000${func}`;
    if (!first.has(k)) first.set(k, id);
    return first.get(k);
  });
})();
function locOf(node) {
  if (node.id < 0 || !node.lines) return "";
  const file = fileOf(node.id)[0];
  const lines = [...node.lines].sort((a, b) => a - b);
  return file ? `${file}:${lines.slice(0, 4).join(", ")}${lines.length > 4 ? "…" : ""}` : "";
}

// Frames of a trie node, outermost first.
function pathOf(node) {
  const out = [];
  while (node !== 0) { out.push(D.nodes[2 * node + 1]); node = D.nodes[2 * node]; }
  return out.reverse();
}

// Grouping: keep your frames; of each run of library frames keep only the
// first (the call you made into the library); drop the launcher at the root.
function group(path) {
  if (!path.length) return [NO_STACK];
  const mine = path.map(id => D.frames[id][3] === 1);
  if (!mine.some(Boolean)) {
    const startup = path.some(id => /^(<frozen runpy>|runpy\.py)$/.test(fileOf(id)[0]));
    // The walk keeps the innermost max_depth frames, so a deep library call
    // (nested imports, say) can lose the frames of yours at its root.
    return [startup ? STARTUP : path.length >= D.max_depth ? DEEP : OUTSIDE, path[path.length - 1]];
  }
  const out = [];
  for (let i = mine.indexOf(true); i < path.length; i++) {
    if (mine[i] || mine[i - 1]) out.push(path[i]);
  }
  return out;
}

// ---- the tree: node.total = sum of children + what was allocated in it ----
function buildTree(view = state.view, grouped = state.group) {
  const flat = D.views[view] || [];
  const bytes = VIEWS[view].unit === "bytes";
  const root = { id: null, total: 0, self: 0, native: 0, kids: new Map() };
  for (let i = 0; i < flat.length; i += 4) {
    const v = bytes ? flat[i + 1] : flat[i + 2];
    if (!v) continue;
    const native = bytes ? flat[i + 3] : 0;
    const raw = pathOf(flat[i]);
    const path = grouped ? group(raw) : (raw.length ? raw : [NO_STACK]);
    let node = root;
    node.total += v; node.native += native;
    for (const raw of path) {
      const id = raw < 0 ? raw : funcOf[raw];
      let k = node.kids.get(id);
      if (!k) { k = { id, total: 0, self: 0, native: 0, kids: new Map(), lines: raw < 0 ? null : new Set() }; node.kids.set(id, k); }
      k.total += v; k.native += native;
      if (k.lines) k.lines.add(D.frames[raw][2]);
      node = k;
    }
    node.self += v;
  }
  return root;
}

// What a box splits into: the functions it called, plus what it allocated
// itself (Python objects, and C allocations made while it ran). These add
// up to its total exactly.
function parts(node) {
  const out = [...node.kids.values()].map(k => ({ node: k, total: k.total }));
  if (node.id === NO_STACK) return out;
  let kidsNative = 0;
  for (const k of node.kids.values()) kidsNative += k.native;
  const selfNative = Math.max(0, Math.min(node.self, node.native - kidsNative));
  const selfPy = node.self - selfNative;
  if (out.length || selfNative) {
    if (selfPy > 0) out.push({ here: "py", total: selfPy, owner: ownerOf(node.id) });
    if (selfNative > 0) out.push({ here: "c", total: selfNative, owner: "native" });
  }
  return out.sort((a, b) => b.total - a.total);
}
const partKey = (parentKey, p) => `${parentKey}/${p.node ? p.node.id : p.here}`;
function hereLabel(p, parent) {
  const who = parent.id === null ? "" : ` in ${frameLabel(parent.id)}`;
  return p.here === "c" ? `C code called${who}` : `allocated directly${who}`;
}

function nodeAt(root, ids) {
  let n = root;
  for (const id of ids) { const k = n.kids.get(id); if (!k) return null; n = k; }
  return n;
}

// ---- header ----
document.getElementById("title").textContent = `heapwise · ${D.title}`;
document.getElementById("header").textContent = D.header;

// ---- key numbers ----
const fmtSecs = t => t >= 100 ? `${t.toFixed(0)} s` : t >= 10 ? `${t.toFixed(1)} s` : `${t.toFixed(2)} s`;
// A row of capture.top(): [func, path, line, bytes, count, native, kind, pkg].
function siteName(row) {
  const [func, file, , , , , kind, pkg] = row;
  if (func === "<script startup>") return SYNTH[STARTUP];
  if (func === "<no Python stack>") return SYNTH[NO_STACK];
  const base = file.split(/[\\/]/).pop().replace(/\.py$/, "");
  const name = func === "<module>" && base ? `${base} (top level)` : func;
  return (kind === "lib" || kind === "std") && pkg && !name.startsWith(pkg) && func !== "<module>" ? `${pkg}.${name}` : name;
}
function siteOwner(row) {
  if (row[0] === "<no Python stack>") return "native";
  return row[6] === "mine" || row[6] === "lib" || row[6] === "std" ? row[6] : "other";
}
const siteLoc = row => row[1] ? `${row[1]}:${row[2]}` : "";
function showView(view) {
  if (D.views[view] === null) return;
  if (state.view !== view) { state.view = view; state.zoom = []; state.highlight = null; render(); fadeIn(); }
  document.getElementById("p-cascade").scrollIntoView({ behavior: reduceMotion() ? "auto" : "smooth", block: "start" });
}
function drawBento() {
  const tile = (o) => `<${o.view ? `button type="button" data-view="${o.view}"` : "div"} class="tile${o.cls ? " " + o.cls : ""}"` +
    `${o.view ? ` title="Show in the cascade"` : ""}>` +
    `<span class="label">${esc(o.label)}</span><span class="value${o.quiet ? " quiet" : ""}">${esc(o.value)}</span>` +
    (o.ctx ? `<span class="ctx">${o.ctx}</span>` : "") + (o.loc ? `<span class="loc3 mono">${esc(o.loc)}</span>` : "") +
    (o.extra || "") + `</${o.view ? "button" : "div"}>`;
  const sw = o => `<span class="sw" style="background:var(--${o})"></span>`;

  // Hero: the peak, and who held it.
  const { by, libs } = ownerTotals(buildTree("peak", true));
  const present = OWNER_ORDER.filter(o => by[o] > 0);
  const whole = present.reduce((a, o) => a + by[o], 0) || 1;
  const libNames = [...libs.entries()].sort((a, b) => b[1] - a[1]).map(([n]) => n);
  const ownerName = o => o === "lib" && libNames.length === 1 ? `Library: ${libNames[0]}` : o === "std" ? "Standard library" : OWNERS[o].label;
  const foot = [
    D.rss ? `The whole process peaked at ${fmtBytes(D.rss)} RSS, which also counts the interpreter itself.` : "",
    ...D.notes,
  ].filter(Boolean).map(esc).join("<br>");
  const heroExtra = D.peak
    ? `<div class="split" aria-hidden="true">${present.map(o => `<span style="flex:${by[o]} 1 0;background:var(--${o})"></span>`).join("")}</div>` +
      `<ul>${present.map(o => `<li>${sw(o)}<span class="n">${esc(ownerName(o))}</span><span class="v">${fmtPct(pctOf(by[o], whole))}</span></li>`).join("")}</ul>` +
      (foot ? `<div class="foot">${foot}</div>` : "")
    : (foot ? `<div class="foot">${foot}</div>` : "");
  const tiles = [{
    cls: "hero", view: D.peak ? "peak" : null, label: "Peak memory", value: fmtBytes(D.peak),
    ctx: D.peak ? `at ${fmtSecs(D.peak_time)} of a ${fmtSecs(D.duration)} run${D.sampled ? ", estimated from samples" : ""}` : "No allocations were recorded.",
    extra: heroExtra,
  }];

  const big = D.top.peak;
  tiles.push(big ? {
    view: "peak", label: "Biggest at the peak", value: fmtBytes(big[3]),
    ctx: `${sw(siteOwner(big))}${esc(siteName(big))} · ${fmtPct(pctOf(big[3], D.peak))} of the peak`, loc: siteLoc(big),
  } : { label: "Biggest at the peak", value: "None", quiet: true });

  const leak = D.top.leaks;
  tiles.push(D.leaked && leak ? {
    view: "leaks", label: "Still allocated at exit", value: fmtBytes(D.leaked),
    ctx: `mostly ${sw(siteOwner(leak))}${esc(siteName(leak))}`, loc: siteLoc(leak),
  } : { label: "Still allocated at exit", value: "Nothing", quiet: true, ctx: "Everything allocated was freed." });

  if (D.views.temporaries === null) {
    tiles.push({ label: "Temporary allocations", value: "Not counted", quiet: true, ctx: "Run with <span class=\"mono\">--exact</span> to count them." });
  } else {
    let n = 0;
    const flat = D.views.temporaries;
    for (let i = 2; i < flat.length; i += 4) n += flat[i];
    const t = D.top.temporaries;
    tiles.push(t && n ? {
      view: "temporaries", label: "Temporary allocations", value: fmtCount(n),
      ctx: `most from ${sw(siteOwner(t))}${esc(siteName(t))}`, loc: siteLoc(t),
    } : { label: "Temporary allocations", value: "0", quiet: true, ctx: "No short-lived allocations." });
  }

  tiles.push({
    label: "Allocated in total", value: fmtBytes(D.allocated),
    ctx: `${fmtCount(D.allocations)} allocations${D.sampled ? " (sampled)" : ""}`,
    loc: D.peak ? `${(D.allocated / D.peak).toFixed(1)}× the peak, freed and reused as it ran` : "",
  });

  const el = document.getElementById("bento");
  el.innerHTML = tiles.map(tile).join("");
  el.querySelectorAll("button[data-view]").forEach(b => { b.onclick = () => showView(b.dataset.view); });
}
drawBento();

// ---- timeline: total only, in ink, so color keeps meaning "owner" ----
// Round steps: 1, 2, 2.5 or 5 times a power of ten (bytes use powers of 1024
// for the unit, so the labels read 0, 5 MiB, 10 MiB...).
function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw)), f = raw / p;
  return p * (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10);
}
function byteTicks(max, want) {
  let unit = 1;
  while (max / unit >= 1024 && unit < 1024 ** 4) unit *= 1024;
  let step = niceStep(max / unit / want) * unit;
  if (unit > 1 && step < unit / 8) step = niceStep(max / want);  // tiny values: plain bytes
  if (unit === 1) step = Math.max(1, Math.round(step));
  const top = Math.max(step, Math.ceil(max / step - 1e-9) * step);
  const ticks = [];
  for (let v = 0; v <= top + step / 2; v += step) ticks.push(v);
  return { top, ticks };
}
function fmtTick(n) {
  if (!n) return "0";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${+v.toFixed(2)} ${u[i]}`;
}
function timeTicks(dur, want) {
  if (!(dur > 0)) return { step: 1, ticks: [0] };
  const step = niceStep(dur / want), ticks = [];
  for (let t = 0; t <= dur + step * 1e-6; t += step) ticks.push(+t.toFixed(9));
  return { step, ticks };
}
function fmtTime(t, step) {
  if (D.duration < 1) {
    const ms = t * 1000;
    let dp = 0;
    while (dp < 3 && Math.abs(step * 1000 * 10 ** dp - Math.round(step * 1000 * 10 ** dp)) > 1e-6) dp++;
    return `${+ms.toFixed(dp)} ms`;
  }
  let dp = 0;  // enough decimals to show the step exactly (0.25 s needs two)
  while (dp < 6 && Math.abs(step * 10 ** dp - Math.round(step * 10 ** dp)) > 1e-6) dp++;
  return `${+t.toFixed(dp)} s`;
}
function drawTimeline() {
  const svg = document.getElementById("timeline");
  const W = svg.clientWidth || 800, H = 190, L = 64, R = 12, B = 24, T = 18;
  const vals = D.timeline, n = vals.length, dur = D.duration || 0;
  const pw = W - L - R;
  const { top, ticks: yt } = byteTicks(Math.max(1, ...vals, D.peak), H < 160 ? 3 : 4);
  const { step, ticks: xt } = timeTicks(dur, Math.max(3, Math.min(10, Math.floor(pw / 80))));
  const xT = t => L + pw * (dur ? Math.min(1, t / dur) : 0);
  const y = v => T + (H - T - B) * (1 - v / top);
  // Value i is the highest live total in the i-th slice of the run; draw it
  // at the slice's middle, and stretch the ends to the edges.
  const mid = i => dur * (i + 0.5) / n;
  let d = `M${L},${y(vals[0] || 0)}`;
  vals.forEach((v, i) => { d += `L${xT(mid(i)).toFixed(1)},${y(v).toFixed(1)}`; });
  d += `L${L + pw},${y(vals[n - 1] || 0)}`;
  const area = d + `L${L + pw},${y(0)}L${L},${y(0)}Z`;
  const grid = yt.map(v => `<line x1="${L}" x2="${L + pw}" y1="${y(v)}" y2="${y(v)}" stroke="${v ? "var(--grid)" : "var(--axis)"}"/>` +
    `<text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${fmtTick(v)}</text>`).join("");
  const xticks = xt.map((t, i) => {
    const xx = xT(t);
    const anchor = i === 0 ? "start" : xx > L + pw - 24 ? "end" : "middle";
    return `<line x1="${xx}" x2="${xx}" y1="${y(0)}" y2="${y(0) + 4}" stroke="var(--axis)"/>` +
      `<text x="${xx}" y="${H - 6}" text-anchor="${anchor}">${fmtTime(t, step)}</text>`;
  }).join("");
  const px = xT(D.peak_time), py = y(D.peak);
  const peakLabel = `Peak ${fmtBytes(D.peak)}`;
  const labelRight = px < L + pw - 110;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = grid + xticks +
    `<path d="${area}" fill="var(--ink)" fill-opacity="0.07"/><path d="${d}" fill="none" stroke="var(--ink-2)" stroke-width="2" stroke-linejoin="round"/>` +
    (D.peak ? `<line x1="${px}" x2="${px}" y1="${py}" y2="${y(0)}" stroke="var(--ink)" stroke-dasharray="2 3"/>` +
      `<circle cx="${px}" cy="${py}" r="4" fill="var(--ink)" stroke="var(--surface)" stroke-width="2"/>` +
      `<text class="peak-label" x="${px + (labelRight ? 8 : -8)}" y="${Math.max(12, py - 6)}" text-anchor="${labelRight ? "start" : "end"}">${esc(peakLabel)}</text>` : "") +
    `<g class="hover" style="display:none"><line y1="${T - 6}" y2="${y(0)}" stroke="var(--ink)" stroke-opacity="0.45"/>` +
    `<circle r="4" fill="var(--surface)" stroke="var(--ink)" stroke-width="2"/></g>`;
  document.getElementById("tl-hint").textContent = D.peak
    ? "Live memory as the program ran. Hover the chart for the value at any moment."
    : "";

  // Hover: snap to the nearest slice and show its value.
  const hover = svg.querySelector(".hover"), hl = hover.querySelector("line"), hc = hover.querySelector("circle");
  const off = () => { hover.style.display = "none"; hideTip(); };
  svg.onpointermove = e => {
    if (!n || !dur) return;
    const r = svg.getBoundingClientRect();
    const sx = (e.clientX - r.left) * W / r.width;
    if (sx < L - 4 || sx > L + pw + 4) { off(); return; }
    const i = Math.max(0, Math.min(n - 1, Math.floor((sx - L) / pw * n)));
    const v = vals[i], cx = xT(mid(i));
    hover.style.display = "";
    hl.setAttribute("x1", cx); hl.setAttribute("x2", cx);
    hc.setAttribute("cx", cx); hc.setAttribute("cy", y(v));
    const t0 = dur * i / n, t1 = dur * (i + 1) / n;
    const hasPeak = D.peak && D.peak_time >= t0 && D.peak_time <= t1;
    const at = t => dur < 1 ? `${(t * 1000).toFixed(1)} ms` : fmtSecs(t);
    showTip(e, `<b>${fmtBytes(v)}</b> live<br><span class="loc">at ${at((t0 + t1) / 2)} of ${at(dur)}` +
      ` · ${fmtPct(pctOf(v, D.peak))} of the peak</span>` + (hasPeak ? `<br>The peak happened here.` : ""));
  };
  svg.onpointerleave = off;
  svg.onpointercancel = off;
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
    b.onclick = () => { state.view = k; state.zoom = []; state.highlight = null; render(); fadeIn(); };
    tabs.appendChild(b);
  }
}

// ---- owners: who allocated the memory in this view ----
function ownerTotals(root) {
  const by = Object.fromEntries(OWNER_ORDER.map(o => [o, 0]));
  const libs = new Map();
  // Walk the parts of every node, counting only what was allocated in it.
  const walk = (n) => {
    for (const p of parts(n)) {
      if (p.node) {
        if (!p.node.kids.size && !(p.node.native > 0) && p.node.id !== NO_STACK) addOwn(p.node.id, p.total);
        else if (p.node.id === NO_STACK) by.native += p.total;
        else walk(p.node);
      } else if (p.here === "c") by.native += p.total;
      else addOwn(n.id, p.total);
    }
  };
  const addOwn = (id, v) => {
    const o = ownerOf(id);
    by[o] += v;
    if (o === "lib") libs.set(frameInfo(id).pkg, (libs.get(frameInfo(id).pkg) || 0) + v);
  };
  walk(root);
  return { by, libs };
}
function drawOwners(root) {
  const { by, libs } = ownerTotals(root);
  const total = root.total || 1;
  const present = OWNER_ORDER.filter(o => by[o] > 0);
  if (state.highlight && !by[state.highlight]) state.highlight = null;
  document.getElementById("split").innerHTML = present.map(o =>
    `<span style="flex:${by[o]} 1 0;background:var(--${o});opacity:${!state.highlight || state.highlight === o ? 1 : 0.2}"></span>`).join("");
  const libNames = [...libs.entries()].sort((a, b) => b[1] - a[1]).map(([n]) => n);
  const owners = document.getElementById("owners");
  owners.innerHTML = present.map(o => {
    const what = o === "lib" ? libNames.slice(0, 3).join(", ") + (libNames.length > 3 ? "…" : "") : OWNERS[o].what;
    return `<button class="chip" data-o="${o}" aria-pressed="${state.highlight === o}" title="Click to highlight in the cascade">` +
      `<span class="sw" style="background:var(--${o})"></span>${OWNERS[o].label}` +
      `<span class="v">${fmtPct(pctOf(by[o], total))} · ${fmt(by[o])}</span>` +
      (what ? `<span class="what">${esc(what)}</span>` : "") + `</button>`;
  }).join("");
  owners.querySelectorAll(".chip").forEach(b => {
    b.onclick = () => { state.highlight = state.highlight === b.dataset.o ? null : b.dataset.o; render(); };
  });
}

// ---- tooltip ----
const tip = document.getElementById("tip");
function showTip(e, html) {
  tip.innerHTML = html;
  tip.style.display = "block";
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(e.clientX + 14, innerWidth - r.width - 8) + "px";
  tip.style.top = (e.clientY + 16 + r.height > innerHeight ? e.clientY - r.height - 12 : e.clientY + 16) + "px";
}
const hideTip = () => { tip.style.display = "none"; };
document.addEventListener("scroll", hideTip, true);
function tipFor(p, parent, rootTotal) {
  const share = `${fmt(p.total)} · ${fmtPct(pctOf(p.total, rootTotal))} of all` +
    (parent && parent.total ? ` · ${fmtPct(pctOf(p.total, parent.total))} of its caller` : "");
  if (p.more) return `<b>${fmtCount(p.more)} smaller items</b>, each too narrow to draw<br>${share}<br><span class="loc">All listed in the call tree below</span>`;
  if (p.here) {
    const what = p.here === "c" ? "Allocated by C code (an extension's malloc/mmap) while this function was running."
      : "Python objects created directly in this function, not in anything it called.";
    return `<b>${esc(hereLabel(p, parent))}</b><br><span class="sw" style="background:var(--${p.owner})"></span>${what}<br>${share}`;
  }
  return `<b>${esc(frameLabel(p.node.id))}</b> <span class="loc">${esc(locOf(p.node))}</span><br>` +
    `<span class="sw" style="background:var(--${ownerOf(p.node.id)})"></span>${esc(ownerText(p.node.id))}<br>${share}`;
}

// ---- cascade ----
let animateZoom = false;
function drawFlame(root) {
  const el = document.getElementById("flame");
  const crumbs = document.getElementById("crumbs");
  const before = new Map();
  if (animateZoom && !reduceMotion()) {
    for (const b of el.querySelectorAll(".box[data-k]")) before.set(b.dataset.k, [b.style.left, b.style.width, b.style.top]);
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
  const zoomTo = z => { state.zoom = z; animateZoom = true; hideTip(); render(); };
  crumbs.innerHTML = state.zoom.length
    ? `<a data-z="0">${esc(VIEWS[state.view].root)}</a>` +
      state.zoom.map((id, i) => ` › ${i + 1 < state.zoom.length ? `<a data-z="${i + 1}">` : "<b>"}${esc(frameLabel(id))}${i + 1 < state.zoom.length ? "</a>" : "</b>"}`).join("")
    : "Click a box to zoom into it.";
  crumbs.querySelectorAll("a").forEach(a => { a.onclick = () => zoomTo(state.zoom.slice(0, +a.dataset.z)); });

  // Boxes narrower than MIN_BOX share an "N more" box; boxes narrower than
  // MIN_SPLIT (5% of the width) are not split further until zoomed into, so
  // small deep call chains (imports, say) don't trail off into a tower of
  // unreadable slivers. The call tree has every row.
  const W = el.clientWidth || 800, RH = 26, GAP = 2, MIN_BOX = 6, MIN_SPLIT = Math.max(14, W * 0.05);
  let depth = 0;
  const frag = document.createDocumentFragment();
  const box = (cls, x, w, d, key, label, value, tipHtml) => {
    depth = Math.max(depth, d + 1);
    const div = document.createElement("div");
    div.className = `box ${cls}`;
    div.dataset.k = key;
    // Positions are fractions of the width, so the cascade always fits its
    // panel, even before a resize re-render (a scrollbar appearing, say).
    const pct = v => `${(100 * v / W).toFixed(4)}%`;
    const bw = Math.max(1, w - GAP);
    div.style.left = pct(x);
    div.style.top = d * RH + "px";
    div.style.width = w > GAP + 1 ? `calc(${pct(w)} - ${GAP}px)` : pct(w);
    if (bw < 16) div.classList.add("thin");  // no padding, or it would widen the box
    if (bw < 10) div.style.borderRadius = Math.max(1, bw / 3) + "px";
    if (w > 24) div.innerHTML = `${esc(label)}${w > 90 ? `<span class="val">${esc(value)}</span>` : ""}`;
    div.onmousemove = e => showTip(e, tipHtml());
    div.onmouseleave = hideTip;
    frag.appendChild(div);
    return div;
  };
  const off = owner => state.highlight && owner !== state.highlight ? " off" : "";
  const place = (p, parent, x, w, d, key, path) => {
    if (p.node) {
      const o = ownerOf(p.node.id);
      const split = w >= MIN_SPLIT, hidden = !split && parts(p.node).length > 0;
      const b = box(o + off(o), x, w, d, key, frameLabel(p.node.id), fmt(p.total),
        () => tipFor(p, parent, root.total) + (hidden ? `<br><span class="loc">Click to see what it called</span>` : ""));
      b.onclick = () => zoomTo(path);
      if (split) placeParts(p.node, x, w, d + 1, key, path);
    } else {
      box(`${p.owner} here${off(p.owner)}`, x, w, d, key, hereLabel(p, parent), fmt(p.total), () => tipFor(p, parent, root.total));
    }
  };
  // Children fill their parent's width exactly; those too narrow to draw
  // share one "N more" box at the end, so nothing goes missing.
  const placeParts = (node, x, w, d, key, path) => {
    let cx = x, restN = 0, rest = 0;
    for (const p of parts(node)) {
      const pw = w * p.total / node.total;
      if (pw < MIN_BOX) { restN++; rest += p.total; continue; }
      place(p, node, cx, pw, d, partKey(key, p), p.node ? [...path, p.node.id] : path);
      cx += pw;
    }
    if (restN && x + w - cx >= 2) {
      const more = { more: restN, total: rest };
      box("other more", cx, x + w - cx, d, `${key}/more`, `${fmtCount(restN)} more`, fmt(rest), () => tipFor(more, node, root.total));
    }
  };
  // Row 0: the whole view (or the zoomed function), full width.
  const top = focus === root
    ? box("root", 0, W, 0, "root", VIEWS[state.view].root, fmt(root.total),
        () => `<b>${esc(VIEWS[state.view].root)}</b><br>${fmt(root.total)}`)
    : null;
  if (top) placeParts(root, 0, W, 1, "root", []);
  else place({ node: focus, total: focus.total }, null, 0, W, 0, "root/" + state.zoom.join("/"), state.zoom.slice());
  el.appendChild(frag);
  el.style.height = depth * RH + "px";

  if (before.size) {
    const boxes = [...el.querySelectorAll(".box")];
    const target = boxes.map(b => [b.style.left, b.style.width, b.style.top]);
    boxes.forEach(b => {
      const old = before.get(b.dataset.k);
      if (old) [b.style.left, b.style.width, b.style.top] = old;
      else b.classList.add("fade");
    });
    void el.offsetWidth;  // commit the starting positions before transitioning
    boxes.forEach((b, i) => { b.classList.add("anim"); b.classList.remove("fade"); [b.style.left, b.style.width, b.style.top] = target[i]; });
  }
}

// ---- call tree: same parts as the cascade, as rows ----
let open = new Set();
function autoOpen(root) {
  open = new Set(["root"]);
  const walk = (n, key, d) => {
    parts(n).forEach((p, i) => {
      if (!p.node) return;
      const k = partKey(key, p);
      if (d < 1 || (i === 0 && p.total >= root.total * 0.2)) { open.add(k); walk(p.node, k, d + 1); }
    });
  };
  walk(root, "root", 0);
}
function drawTree(root, enter) {
  const t = document.getElementById("tree");
  let html = `<tr><th>Total</th><th>Share</th><th>Function</th></tr>`;
  if (!root.total) { t.innerHTML = html; return; }
  const rows = [];
  const walk = (n, key, d) => {
    let shown = 0, rest = 0, restN = 0;
    for (const p of parts(n)) {
      if (shown >= 200 || (p.total / root.total < 0.001 && shown >= 10)) { rest += p.total; restN++; continue; }
      shown++;
      const k = partKey(key, p);
      rows.push({ p, parent: n, k, d });
      if (p.node && open.has(k)) walk(p.node, k, d + 1);
    }
    if (restN) rows.push({ p: { more: restN, total: rest }, parent: n, k: `${key}/more`, d });
  };
  rows.push({ p: { node: root, total: root.total, isRoot: true }, parent: null, k: "root", d: 0 });
  if (open.has("root")) walk(root, "root", 1);
  let entering = 0;
  const cell = (inner, attrs = "") => `<td${attrs}><div class="c">${inner}</div></td>`;
  for (const { p, parent, k, d } of rows) {
    const isNew = enter !== undefined && k.startsWith(enter + "/");
    const cls = [p.node ? "row" : p.here ? "here" : "", isNew ? "enter" : ""].filter(Boolean).join(" ");
    const style = isNew ? ` style="--delay:${Math.min(entering++, 12) * 12}ms"` : "";
    const pct = pctOf(p.total, root.total);
    let name;
    if (p.isRoot) name = `<span class="tw${open.has("root") ? " open" : ""}">▸</span><b>${esc(VIEWS[state.view].root)}</b>`;
    else if (p.more) name = `<span class="tw"></span><span class="dim">${fmtCount(p.more)} more, each smaller</span>`;
    else if (p.here) name = `<span class="tw"></span><span class="sw" style="background:var(--${p.owner})"></span><span class="dim">${esc(hereLabel(p, parent))}</span>`;
    else {
      const f = frameInfo(p.node.id), o = ownerOf(p.node.id);
      const tw = parts(p.node).length ? `<span class="tw${open.has(k) ? " open" : ""}">▸</span>` : `<span class="tw"></span>`;
      const pkg = (o === "lib" || o === "std") && f.pkg ? `<span class="pkg">${esc(f.pkg)}</span>` : "";
      name = `${tw}<span class="sw" style="background:var(--${o})"></span>${pkg}<span class="mono">${esc(f.func)}</span><span class="loc2 mono">${esc(locOf(p.node))}</span>`;
    }
    html += `<tr class="${cls}"${style} data-k="${esc(k)}">` +
      cell(fmt(p.total)) +
      cell(`<span class="pct"><i style="width:${Math.max(1, pct * 0.6)}px"></i>${fmtPct(pct)}</span>`) +
      cell(name, ` class="fname" style="padding-left:${8 + d * 18}px"`) + `</tr>`;
  }
  t.innerHTML = html;
  t.querySelectorAll("tr.row").forEach(r => {
    r.onclick = () => {
      const k = r.dataset.k;
      if (!open.has(k)) { open.add(k); drawTree(root, k); return; }
      open.delete(k);
      r.querySelector(".tw")?.classList.remove("open");
      const gone = [...t.querySelectorAll("tr[data-k]")].filter(x => x.dataset.k.startsWith(k + "/"));
      if (!gone.length || reduceMotion()) { drawTree(root); return; }
      gone.forEach(x => x.classList.add("leave"));
      setTimeout(() => drawTree(root), 150);
    };
  });
}

// ---- wiring ----
function fadeIn() {
  for (const id of ["flame", "tree"]) {
    const el = document.getElementById(id);
    el.classList.remove("fade-in"); void el.offsetWidth; el.classList.add("fade-in");
  }
}
let lastTreeKey = "";
function render() {
  drawTabs();
  const v = VIEWS[state.view];
  const root = buildTree();
  document.getElementById("view-title").textContent = v.title;
  document.getElementById("view-hint").textContent =
    `Read top to bottom: each box is a function, as wide as the memory it accounts for, and splits into what it called` +
    ` and what it allocated itself. ${D.sampled && v.unit === "bytes" ? "Sizes are estimated from samples. " : ""}` +
    `Colors show who allocated it.`;
  drawOwners(root);
  drawFlame(root);
  const tk = `${state.view}/${state.group}`;
  if (tk !== lastTreeKey) { autoOpen(root); lastTreeKey = tk; }
  drawTree(root);
}
document.getElementById("group").onchange = e => { state.group = e.target.checked; state.zoom = []; render(); fadeIn(); };
let rt;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { drawTimeline(); render(); }, 100); });
// The panel can change width without a window resize (a scrollbar showing
// up as the page grows); redraw so labels match the new widths.
let lastW = 0;
new ResizeObserver(() => {
  const w = document.getElementById("flame").clientWidth;
  if (lastW && Math.abs(w - lastW) > 1) { clearTimeout(rt); rt = setTimeout(() => { drawTimeline(); render(); }, 100); }
  lastW = w;
}).observe(document.getElementById("flame"));
drawTimeline();
render();
</script>
</body>
</html>
"""
