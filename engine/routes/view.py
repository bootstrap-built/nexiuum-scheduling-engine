"""GET /schedule.json + GET /view — live Marey chart of the current Schedule.

`/schedule.json` serializes a fresh Snapshot (machines + slots) for any
consumer — embedded board view, debugging, ad-hoc tooling.

`/view` serves a standalone HTML Marey renderer that polls /schedule.json.
Lives at the engine origin so we can demo the schedule without first
building a Monday Apps Framework iframe wrapper.

Public read-only; no auth. The engine's mutating endpoints (/commit,
/webhook) are auth-gated separately.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from engine.io.snapshot import read_snapshot
from engine.models import Machine, Slot, Snapshot

router = APIRouter(tags=["view"])


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _machine_to_dict(m: Machine) -> dict[str, Any]:
    return {
        "id": m.id,
        "name": m.name,
        "process_group": m.process_group,
        "status": m.status.value,
        "capacity_per_hour": m.capacity_per_hour,
        "hours_per_day": m.hours_per_day,
        "working_window_start": m.working_window_start,
        "working_window_end": m.working_window_end,
        "dual_sided_only": m.dual_sided_only,
        "max_job_size": m.max_job_size,
        "force_route_condition": m.force_route_condition,
        "last_job_ended_at": _iso(m.last_job_ended_at),
    }


def _slot_to_dict(s: Slot) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "job_reference_id": s.job_reference_id,
        "machine_id": s.machine_id,
        "stage_id": s.stage_id,
        "recipe_key": s.recipe_key,
        "recipe_version": s.recipe_version,
        "quantity": s.quantity,
        "planned_start": _iso(s.planned_start),
        "planned_end": _iso(s.planned_end),
        "actual_start": _iso(s.actual_start),
        "actual_end": _iso(s.actual_end),
        "status": s.status.value,
        "manually_placed": s.manually_placed,
        "priority": s.priority.value,
        "drift_last_detected_at": _iso(s.drift_last_detected_at),
    }


def _snapshot_to_dict(snap: Snapshot) -> dict[str, Any]:
    return {
        "read_at": _iso(snap.read_at),
        "machines": [_machine_to_dict(m) for m in snap.machines],
        "slots": [_slot_to_dict(s) for s in snap.slots],
    }


@router.get("/schedule.json")
async def schedule_json() -> dict[str, Any]:
    """Fresh Snapshot serialized to JSON for the view to render."""
    snap = await read_snapshot()
    return _snapshot_to_dict(snap)


@router.get("/view", response_class=HTMLResponse)
async def view() -> str:
    """Standalone Marey chart of the live schedule.

    Polls /schedule.json every 30s. Renderer is intentionally self-contained
    (no external CSS/JS) so this works in any browser without a build step.
    """
    return _MAREY_HTML


# ─────────────────────────────────────────────────────────────────────────
# Inline renderer — kept here so the route serves a single self-contained
# response. Extract to engine/static/ if it grows past ~500 lines.
# ─────────────────────────────────────────────────────────────────────────


_MAREY_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Production Schedule — Live</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0d0f13; --panel:#13161c; --panel-2:#171b22; --rail:#1f2530;
    --line:#262c38; --grid:#1d222b; --grid-strong:#2c333f;
    --txt:#cdd2dc; --txt-dim:#828a99; --txt-faint:#5b6473;
    --press:#3c9be0; --capsule:#a067e0; --pkg:#2bb39a;
    --now:#f0a93c; --drift:#e0653c; --running:#39d98a; --blocked:#c2554a;
    --j1:#e0653c; --j2:#2bb39a; --j3:#8b7be8; --j4:#e0a23c;
    --j5:#3c9be0; --j6:#e058a0; --j7:#7bd0e0; --j8:#a067e0;
    --lane-h:34px; --hdr-h:28px; --gutter:200px;
    --font:'Archivo',system-ui,sans-serif; --mono:'IBM Plex Mono',ui-monospace,monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{background:var(--bg);color:var(--txt);font-family:var(--font);
    font-feature-settings:"tnum" 1;-webkit-font-smoothing:antialiased;overflow:hidden}
  .app{display:flex;flex-direction:column;height:100vh}
  header{display:flex;align-items:center;gap:20px;padding:14px 22px;
    background:var(--panel);border-bottom:1px solid var(--line);flex:0 0 auto}
  .brand h1{font-size:15px;font-weight:600;letter-spacing:.02em}
  .brand span{font-size:11px;color:var(--txt-dim);letter-spacing:.06em;text-transform:uppercase}
  .feed{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--txt-dim);
    font-family:var(--mono);letter-spacing:.02em;margin-left:24px}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--txt-faint);transition:.3s}
  .feed.live .dot{background:var(--running);box-shadow:0 0 0 4px rgba(57,217,138,.15);
    animation:pulse 1.6s ease-in-out infinite}
  .feed.stale .dot{background:var(--drift)}
  @keyframes pulse{50%{box-shadow:0 0 0 7px rgba(57,217,138,.04)}}
  .controls{margin-left:auto;display:flex;align-items:center;gap:10px}
  .seg{display:flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
  .seg button{background:transparent;color:var(--txt-dim);border:0;
    font-family:var(--font);font-size:12px;padding:7px 12px;cursor:pointer;transition:.15s}
  .seg button+button{border-left:1px solid var(--line)}
  .seg button.on{background:var(--rail);color:var(--txt)}
  .seg button:hover{color:var(--txt)}
  .body{flex:1 1 auto;display:flex;min-height:0;position:relative}
  .labels{flex:0 0 var(--gutter);background:var(--panel);border-right:1px solid var(--line);
    overflow:hidden;padding-top:var(--hdr-h)}
  .grp-l{height:var(--hdr-h);display:flex;align-items:center;padding:0 16px;
    font-size:10.5px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
    color:var(--txt-faint);border-top:1px solid var(--line)}
  .grp-l.press{color:var(--press)}
  .grp-l.capsule{color:var(--capsule)}
  .grp-l.pkg{color:var(--pkg)}
  .lane-l{height:var(--lane-h);display:flex;align-items:center;justify-content:space-between;
    padding:0 16px 0 26px;font-size:12.5px;color:var(--txt);position:relative}
  .lane-l .cap{font-family:var(--mono);font-size:10.5px;color:var(--txt-faint)}
  .lane-l.down{color:var(--txt-faint)}
  .lane-l.down .cap{color:var(--blocked)}
  .lane-l::before{content:"";position:absolute;left:12px;top:50%;width:5px;height:5px;
    border-radius:50%;transform:translateY(-50%);background:var(--rail)}
  .lane-l.press::before{background:rgba(60,155,224,.6)}
  .lane-l.capsule::before{background:rgba(160,103,224,.6)}
  .lane-l.pkg::before{background:rgba(43,179,154,.6)}
  .scroll{flex:1 1 auto;overflow-x:auto;overflow-y:hidden;background:var(--bg)}
  svg{display:block}
  text{font-family:var(--font)}
  .ax{font-family:var(--mono);font-size:11px;fill:var(--txt-faint)}
  .axd{font-size:11px;fill:var(--txt-dim);font-weight:600;letter-spacing:.04em}
  .seg-bar{cursor:pointer;transition:opacity .18s,filter .18s}
  .seg-bar:hover{filter:brightness(1.2)}
  .drift{stroke:var(--drift);stroke-width:2;stroke-dasharray:5 3}
  .empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    pointer-events:none;color:var(--txt-faint);font-size:13px;font-family:var(--mono)}
  footer{flex:0 0 auto;display:flex;align-items:center;gap:18px;flex-wrap:wrap;
    padding:11px 22px;background:var(--panel);border-top:1px solid var(--line);
    font-size:11.5px;color:var(--txt-dim);font-family:var(--mono)}
  footer .key{display:flex;gap:18px}
  footer .key span{display:flex;align-items:center;gap:6px}
  footer .key i{width:14px;height:10px;border-radius:2px;display:inline-block}
  footer .right{margin-left:auto;color:var(--txt-faint)}
  #tip{position:fixed;pointer-events:none;z-index:30;opacity:0;transition:opacity .12s;
    background:#0a0c10;border:1px solid var(--grid-strong);border-radius:9px;
    padding:11px 13px;min-width:210px;max-width:320px;box-shadow:0 12px 34px rgba(0,0,0,.55)}
  #tip h4{font-size:13px;font-weight:600;margin-bottom:7px;display:flex;align-items:center;gap:8px}
  #tip h4 .sw{width:9px;height:9px;border-radius:2px;flex:0 0 auto}
  #tip .r{display:flex;justify-content:space-between;gap:22px;font-size:11.5px;
    padding:2px 0;color:var(--txt-dim)}
  #tip .r b{color:var(--txt);font-weight:500;font-family:var(--mono)}
  #tip .tag{font-size:10px;font-family:var(--mono);padding:2px 7px;border-radius:4px;
    letter-spacing:.04em;display:inline-block}
  .tag.queued{background:var(--rail);color:var(--txt-dim)}
  .tag.running{background:rgba(57,217,138,.14);color:var(--running)}
  .tag.done{background:rgba(95,103,120,.18);color:var(--txt-dim)}
  .tag.blocked{background:rgba(194,85,74,.18);color:var(--blocked)}
  .tag.drift{background:rgba(224,101,60,.18);color:var(--drift);margin-left:6px}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">
      <h1>Production Schedule</h1>
      <span>Live · Nexiuum Engine</span>
    </div>
    <div class="feed" id="feed">
      <div class="dot"></div><span id="feedTxt">connecting…</span>
    </div>
    <div class="controls">
      <div class="seg" id="zoom">
        <button data-z="6" >6 h</button>
        <button data-z="24" class="on">24 h</button>
        <button data-z="72">72 h</button>
        <button data-z="168">7 d</button>
      </div>
    </div>
  </header>
  <div class="body">
    <div class="labels" id="labels"></div>
    <div class="scroll" id="scroll"><svg id="plot"></svg></div>
    <div class="empty" id="empty" style="display:none">No slots scheduled · Schedule board is empty</div>
  </div>
  <footer>
    <div class="key">
      <span><i style="background:var(--j1)"></i>job color = job_reference_id</span>
      <span><i style="background:var(--running);opacity:.85"></i>running</span>
      <span><i style="background:transparent;border:1.5px dashed var(--drift)"></i>drift detected</span>
      <span><i style="background:var(--now);width:2px;border-radius:0"></i>now</span>
    </div>
    <div class="right" id="meta">—</div>
  </footer>
</div>
<div id="tip"></div>

<script>
/* =========================================================================
   Marey renderer for /schedule.json
   - One row per machine
   - Each slot is a horizontal bar at planned_start..planned_end
   - Color hashed by job_reference_id
   - Status drives opacity / fill style
   - Drift overlay is a red dashed border
   ========================================================================= */

const JOB_COLORS = ['var(--j1)','var(--j2)','var(--j3)','var(--j4)','var(--j5)','var(--j6)','var(--j7)','var(--j8)'];

let state = {
  snap: null,
  zoomHours: 24,
  hoverSlotId: null,
};

// — grouping —
const PRESS_GROUPS = new Set(['Pressing']);
const CAPSULE_GROUPS = new Set(['Capsule']);
function groupOf(pg) {
  if (PRESS_GROUPS.has(pg)) return 'press';
  if (CAPSULE_GROUPS.has(pg)) return 'capsule';
  return 'pkg';
}
const GROUP_ORDER = ['press', 'capsule', 'pkg'];
const GROUP_LABEL = {press:'PRESS', capsule:'CAPSULE', pkg:'PACKAGING'};

function hashColor(s) {
  if (!s) return JOB_COLORS[0];
  let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return JOB_COLORS[h % JOB_COLORS.length];
}

function parseISO(s) { return s ? new Date(s) : null; }
function fmtTime(d) {
  return d.toLocaleString('en-US', {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
}

async function fetchSnap() {
  const feed = document.getElementById('feed');
  const feedTxt = document.getElementById('feedTxt');
  try {
    const r = await fetch('/schedule.json', {cache:'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    state.snap = await r.json();
    feed.className = 'feed live';
    feedTxt.textContent = 'LIVE · refreshed ' + new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit'});
    render();
  } catch (e) {
    feed.className = 'feed stale';
    feedTxt.textContent = 'FEED ERROR · ' + e.message;
  }
}

function render() {
  if (!state.snap) return;
  const snap = state.snap;
  // Bucket machines by group, preserve snapshot order within
  const byGroup = {press:[], capsule:[], pkg:[]};
  for (const m of snap.machines) byGroup[groupOf(m.process_group)].push(m);

  // Build labels
  const labels = document.getElementById('labels');
  labels.innerHTML = '';
  const laneIndex = new Map(); // machine id -> row index across all groups
  let idx = 0;
  for (const g of GROUP_ORDER) {
    const machines = byGroup[g];
    if (!machines.length) continue;
    const hdr = document.createElement('div');
    hdr.className = 'grp-l ' + g;
    hdr.textContent = GROUP_LABEL[g];
    labels.appendChild(hdr);
    for (const m of machines) {
      const row = document.createElement('div');
      row.className = 'lane-l ' + g + (m.status === 'Online' ? '' : ' down');
      const cap = m.capacity_per_hour ? (m.capacity_per_hour/1000).toFixed(0) + 'k/hr' : '';
      const downTag = m.status === 'Online' ? '' : ' · ' + m.status.toLowerCase();
      row.innerHTML = `<span>${m.name}</span><span class="cap">${cap}${downTag}</span>`;
      labels.appendChild(row);
      laneIndex.set(m.id, idx);
      idx++;
    }
  }

  // Time domain
  const now = new Date(snap.read_at || Date.now());
  const span = state.zoomHours * 3600 * 1000;
  const t0 = now.getTime() - span * 0.15;  // a little history on the left
  const t1 = t0 + span;

  // Geometry — keep in sync with the labels-gutter CSS:
  //   .labels  padding-top: 28px            (= topAxisH — reserved for the time-axis strip)
  //   .grp-l   height:      28px            (= hdrH    — "PRESS" / "CAPSULE" / "PACKAGING" band)
  //   .lane-l  height:      34px            (= laneH   — one machine row)
  // If you change any of these constants, change the matching CSS var (--hdr-h / --lane-h)
  // or the labels will drift out of alignment with the SVG lanes.
  const laneH = 34, hdrH = 28, topAxisH = 28;
  const rowsByGroup = GROUP_ORDER.map(g => byGroup[g].length).filter(n => n > 0);
  const totalRows = rowsByGroup.reduce((a,b) => a + b, 0);
  const groupCount = rowsByGroup.length;
  const innerH = topAxisH + totalRows * laneH + groupCount * hdrH;
  const scroll = document.getElementById('scroll');
  const innerW = Math.max(scroll.clientWidth, 1200);
  const padL = 16, padR = 16;
  const plotW = innerW - padL - padR;

  const x = t => padL + ((t - t0) / (t1 - t0)) * plotW;

  // Lane y center, accounting for the top-axis strip + group headers.
  // Mirrors the labels gutter exactly: padding-top → group header → rows.
  function laneY(machineId) {
    const li = laneIndex.get(machineId);
    if (li == null) return null;
    let y = topAxisH, seenRows = 0;
    for (const g of GROUP_ORDER) {
      const n = byGroup[g].length;
      if (n === 0) continue;
      y += hdrH;
      if (seenRows + n > li) {
        return y + (li - seenRows) * laneH + laneH / 2;
      }
      y += n * laneH;
      seenRows += n;
    }
    return null;
  }

  const svg = document.getElementById('plot');
  svg.setAttribute('width', innerW);
  svg.setAttribute('height', innerH);
  svg.innerHTML = '';
  const ns = 'http://www.w3.org/2000/svg';

  // Top-axis strip (matches the labels gutter's padding-top)
  const axisBg = document.createElementNS(ns, 'rect');
  axisBg.setAttribute('x', 0); axisBg.setAttribute('y', 0);
  axisBg.setAttribute('width', innerW); axisBg.setAttribute('height', topAxisH);
  axisBg.setAttribute('fill', 'var(--panel)');
  svg.appendChild(axisBg);

  // Group separator bands (one per process group, sits just below the time axis)
  let y = topAxisH;
  for (const g of GROUP_ORDER) {
    const n = byGroup[g].length;
    if (n === 0) continue;
    const sep = document.createElementNS(ns, 'rect');
    sep.setAttribute('x', 0); sep.setAttribute('y', y);
    sep.setAttribute('width', innerW); sep.setAttribute('height', hdrH);
    sep.setAttribute('fill', 'var(--panel)');
    svg.appendChild(sep);
    const border = document.createElementNS(ns, 'line');
    border.setAttribute('x1', 0); border.setAttribute('x2', innerW);
    border.setAttribute('y1', y); border.setAttribute('y2', y);
    border.setAttribute('stroke', 'var(--line)');
    svg.appendChild(border);
    y += hdrH + n * laneH;
  }

  // Hourly grid lines + axis ticks
  const hour = 60 * 60 * 1000;
  const tickStepHours = state.zoomHours <= 6 ? 1 : state.zoomHours <= 24 ? 2 : state.zoomHours <= 72 ? 6 : 12;
  const tickStep = tickStepHours * hour;
  let tt = Math.ceil(t0 / tickStep) * tickStep;
  while (tt < t1) {
    const xx = x(tt);
    // Grid lines start below the time-axis strip so they don't render through
    // the tick labels.
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', xx); line.setAttribute('x2', xx);
    line.setAttribute('y1', topAxisH); line.setAttribute('y2', innerH);
    line.setAttribute('stroke', 'var(--grid)');
    svg.appendChild(line);
    const d = new Date(tt);
    const lbl = document.createElementNS(ns, 'text');
    lbl.setAttribute('x', xx + 4); lbl.setAttribute('y', 18);
    lbl.setAttribute('class', 'ax');
    const isMidnight = d.getHours() === 0;
    lbl.textContent = isMidnight
      ? d.toLocaleDateString('en-US', {weekday:'short', month:'short', day:'numeric'})
      : d.toLocaleTimeString('en-US', {hour:'numeric', minute:'2-digit'});
    if (isMidnight) lbl.setAttribute('class', 'axd');
    svg.appendChild(lbl);
    tt += tickStep;
  }

  // Now line — runs through the lane area only, not the time-axis strip.
  const nowX = x(Date.now());
  if (nowX >= padL && nowX <= innerW - padR) {
    const nl = document.createElementNS(ns, 'line');
    nl.setAttribute('x1', nowX); nl.setAttribute('x2', nowX);
    nl.setAttribute('y1', topAxisH); nl.setAttribute('y2', innerH);
    nl.setAttribute('stroke', 'var(--now)');
    nl.setAttribute('stroke-width', 2);
    svg.appendChild(nl);
  }

  // Slot bars
  let drewSlot = false;
  for (const s of snap.slots) {
    const ly = laneY(s.machine_id);
    if (ly == null) continue;
    const ps = s.actual_start ? parseISO(s.actual_start) : parseISO(s.planned_start);
    const pe = s.actual_end ? parseISO(s.actual_end) : parseISO(s.planned_end);
    if (!ps || !pe) continue;
    // Skip slots fully outside view
    const tsMs = ps.getTime(), teMs = pe.getTime();
    if (teMs < t0 || tsMs > t1) continue;
    const bx0 = Math.max(padL, x(tsMs));
    const bx1 = Math.min(innerW - padR, x(teMs));
    const w = Math.max(2, bx1 - bx0);
    drewSlot = true;
    const color = hashColor(s.job_reference_id || s.id);

    const g = document.createElementNS(ns, 'g');
    g.setAttribute('class', 'seg-bar');
    g.setAttribute('data-slot', s.id);

    const r = document.createElementNS(ns, 'rect');
    r.setAttribute('x', bx0); r.setAttribute('y', ly - 9);
    r.setAttribute('width', w); r.setAttribute('height', 18);
    r.setAttribute('rx', 4); r.setAttribute('ry', 4);
    r.setAttribute('fill', color);
    if (s.status === 'Done') r.setAttribute('opacity', '0.35');
    else if (s.status === 'Queued') r.setAttribute('opacity', '0.75');
    else if (s.status === 'Blocked') { r.setAttribute('fill', 'transparent'); r.setAttribute('stroke', 'var(--blocked)'); r.setAttribute('stroke-width', 2); }
    g.appendChild(r);

    // Running gets a thin green inner bar
    if (s.status === 'Running') {
      const inner = document.createElementNS(ns, 'rect');
      inner.setAttribute('x', bx0); inner.setAttribute('y', ly + 6);
      inner.setAttribute('width', w); inner.setAttribute('height', 3);
      inner.setAttribute('rx', 1.5); inner.setAttribute('fill', 'var(--running)');
      g.appendChild(inner);
    }

    // Drift overlay
    if (s.drift_last_detected_at) {
      const drift = document.createElementNS(ns, 'rect');
      drift.setAttribute('x', bx0 - 1); drift.setAttribute('y', ly - 10);
      drift.setAttribute('width', w + 2); drift.setAttribute('height', 20);
      drift.setAttribute('rx', 5); drift.setAttribute('fill', 'transparent');
      drift.setAttribute('class', 'drift');
      g.appendChild(drift);
    }

    // Slot label if wide enough
    if (w > 60) {
      const t = document.createElementNS(ns, 'text');
      t.setAttribute('x', bx0 + 8); t.setAttribute('y', ly + 4);
      t.setAttribute('fill', '#0a0c10'); t.setAttribute('font-size', 11);
      t.setAttribute('font-weight', 600);
      t.setAttribute('pointer-events', 'none');
      const lbl = s.job_reference_id ? '#' + s.job_reference_id.slice(-6) : s.name.slice(0, 18);
      t.textContent = lbl + ' · ' + (s.quantity ? s.quantity.toLocaleString() : '');
      g.appendChild(t);
    }

    g.addEventListener('mouseenter', e => showTip(s, e, color));
    g.addEventListener('mousemove', e => moveTip(e));
    g.addEventListener('mouseleave', hideTip);
    svg.appendChild(g);
  }

  // Empty-state indicator
  document.getElementById('empty').style.display = drewSlot ? 'none' : 'flex';

  // Meta line
  const m = document.getElementById('meta');
  const sc = snap.slots.length, mc = snap.machines.length;
  m.textContent = `${mc} machines · ${sc} slot${sc === 1 ? '' : 's'} · snapshot ${snap.read_at ? new Date(snap.read_at).toLocaleString('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit'}) : '—'}`;
}

function showTip(s, e, color) {
  const tip = document.getElementById('tip');
  const ps = s.actual_start || s.planned_start;
  const pe = s.actual_end || s.planned_end;
  const status = (s.status || '').toLowerCase();
  const driftTag = s.drift_last_detected_at ? '<span class="tag drift">DRIFT</span>' : '';
  tip.innerHTML = `
    <h4><span class="sw" style="background:${color}"></span>${s.name || 'Slot ' + s.id}
      <span class="tag ${status}">${(s.status||'').toUpperCase()}</span>${driftTag}</h4>
    <div class="r"><span>job</span><b>${s.job_reference_id || '—'}</b></div>
    <div class="r"><span>recipe</span><b>${s.recipe_key || '—'}${s.recipe_version ? ' v' + s.recipe_version : ''}</b></div>
    <div class="r"><span>quantity</span><b>${s.quantity ? s.quantity.toLocaleString() : '—'}</b></div>
    <div class="r"><span>planned start</span><b>${ps ? fmtTime(new Date(ps)) : '—'}</b></div>
    <div class="r"><span>planned end</span><b>${pe ? fmtTime(new Date(pe)) : '—'}</b></div>
    ${s.actual_start ? `<div class="r"><span>actual start</span><b>${fmtTime(new Date(s.actual_start))}</b></div>` : ''}
    ${s.actual_end ? `<div class="r"><span>actual end</span><b>${fmtTime(new Date(s.actual_end))}</b></div>` : ''}
    ${s.drift_last_detected_at ? `<div class="r"><span>drift detected</span><b>${fmtTime(new Date(s.drift_last_detected_at))}</b></div>` : ''}
  `;
  tip.style.opacity = 1;
  moveTip(e);
}
function moveTip(e) {
  const tip = document.getElementById('tip');
  const x = Math.min(window.innerWidth - tip.offsetWidth - 14, e.clientX + 14);
  const y = Math.min(window.innerHeight - tip.offsetHeight - 14, e.clientY + 14);
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip() { document.getElementById('tip').style.opacity = 0; }

// Zoom controls
document.querySelectorAll('#zoom button').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('#zoom button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    state.zoomHours = parseInt(b.dataset.z, 10);
    render();
  });
});

// Sync vertical scroll between labels gutter and SVG scroll area (none needed —
// labels gutter doesn't scroll horizontally; vertical overflow not used yet).

window.addEventListener('resize', render);

fetchSnap();
setInterval(fetchSnap, 30000);
</script>
</body>
</html>
"""
