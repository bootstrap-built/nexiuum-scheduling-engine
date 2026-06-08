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

from engine.config import get_settings
from engine.core.labels import compose_lane_label, is_n_number
from engine.io.monday import MondayClient, gray_space_client
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
        "n_number": s.n_number,
        "flavor": s.flavor,
        "dependent_on_ids": list(s.dependent_on_ids),
        "planned_start": _iso(s.planned_start),
        "planned_end": _iso(s.planned_end),
        "actual_start": _iso(s.actual_start),
        "actual_end": _iso(s.actual_end),
        "status": s.status.value,
        "manually_placed": s.manually_placed,
        "priority": s.priority.value,
        "drift_last_detected_at": _iso(s.drift_last_detected_at),
    }


def _snapshot_to_dict(snap: Snapshot, enrich: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Serialize the snapshot, merging per-job enrichment into each slot.

    `enrich` is keyed by job_reference_id and contains display fields
    (job_label / job_client / job_active) the engine doesn't otherwise track.
    """
    slot_dicts = []
    for s in snap.slots:
        d = _slot_to_dict(s)
        meta = enrich.get(s.job_reference_id or "", {})
        d["job_label"] = meta.get("job_label")
        d["job_name"] = meta.get("job_name")
        d["job_client"] = meta.get("job_client")
        d["job_active"] = meta.get("job_active")
        d["ps_item_id"] = meta.get("ps_item_id")
        # Lane label via the labels module (single source of truth). The
        # Slot's own N# wins; for the legacy Gray Space flow (n_number=None)
        # we fall back to the Blend Records PO Number enrichment (job_label),
        # which is that flow's N#, before the `#<last-6>` fallback. Guard the
        # enrichment with is_n_number so a non-N# PO-Number value doesn't get
        # presented as a slot identity.
        job_label = meta.get("job_label")
        effective_n = s.n_number or (job_label if is_n_number(job_label) else None)
        d["lane_label"] = compose_lane_label(effective_n, s.flavor, s.id)
        slot_dicts.append(d)
    return {
        "read_at": _iso(snap.read_at),
        "machines": [_machine_to_dict(m) for m in snap.machines],
        "slots": slot_dicts,
    }


async def _fetch_blend_enrichment(
    job_ids: set[str],
    client: MondayClient | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch display-only Blend Records columns for the given job ids.

    Returns a dict keyed by Monday item id with `job_label` (PO Number /
    "N#"), `job_name` (the item's display name), `job_client`, `job_active`,
    and `ps_item_id` (the originating Production Schedule item id the
    blend-intake workflow stamped in the source-item column, for the pop-out
    PS link — None for legacy Gray-Space-origin records that carry no link).
    Missing or unset columns become None. Empty input → empty dict (no
    Monday call).

    One batched GraphQL query per /schedule.json hit. Could be cached later
    if poll volume grows, but at 30s renderer cadence this is cheap.
    """
    job_ids = {jid for jid in job_ids if jid}
    if not job_ids:
        return {}
    s = get_settings()
    col_ids = [
        s.col_blend_po_number,
        s.col_blend_client,
        s.col_blend_active_ingredient,
        s.col_blend_source_item,
    ]
    query = """
    query ($ids: [ID!], $cols: [String!]) {
      items(ids: $ids) {
        id
        name
        column_values(ids: $cols) { id text }
      }
    }
    """
    variables = {"ids": list(job_ids), "cols": col_ids}

    async def _run(c: MondayClient) -> dict[str, dict[str, Any]]:
        data = await c.query(query, variables=variables)
        out: dict[str, dict[str, Any]] = {}
        for item in data.get("items") or []:
            cols = {cv["id"]: (cv.get("text") or None) for cv in item.get("column_values") or []}
            source_item = (cols.get(s.col_blend_source_item) or "").strip() or None
            out[str(item["id"])] = {
                "job_label": cols.get(s.col_blend_po_number),
                "job_name": item.get("name"),
                "job_client": cols.get(s.col_blend_client),
                "job_active": cols.get(s.col_blend_active_ingredient),
                "ps_item_id": source_item,
            }
        return out

    if client is not None:
        return await _run(client)
    async with gray_space_client() as c:
        return await _run(c)


@router.get("/schedule.json")
async def schedule_json() -> dict[str, Any]:
    """Fresh Snapshot + per-job display metadata, serialized for the view."""
    snap = await read_snapshot()
    job_ids = {s.job_reference_id for s in snap.slots if s.job_reference_id}
    enrich = await _fetch_blend_enrichment(job_ids)
    return _snapshot_to_dict(snap, enrich)


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
    --lane-h:34px; --hdr-h:28px; --axis-h:28px; --gutter:200px;
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
  /* Frozen-pane grid: a fixed corner + horizontally-scrolling time axis on
     top; a vertically-scrolling labels gutter + a both-axis-scrolling lane
     pane below. The lane pane (.scroll) is the master scroller; JS mirrors
     its scrollLeft to the axis and its scrollTop to the labels gutter so the
     axis stays pinned vertically and the labels stay pinned horizontally. */
  .body{flex:1 1 auto;display:flex;flex-direction:column;min-height:0;position:relative}
  .axisrow{flex:0 0 var(--axis-h);display:flex;min-height:0}
  .corner{flex:0 0 var(--gutter);background:var(--panel);
    border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
  .axisscroll{flex:1 1 auto;overflow:hidden;background:var(--panel);
    border-bottom:1px solid var(--line)}
  .lanesrow{flex:1 1 auto;display:flex;min-height:0;position:relative}
  .labelscroll{flex:0 0 var(--gutter);background:var(--panel);
    border-right:1px solid var(--line);overflow:hidden}
  .labels{}
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
  .scroll{flex:1 1 auto;overflow:auto;background:var(--bg)}
  svg{display:block}
  text{font-family:var(--font)}
  .ax{font-family:var(--mono);font-size:11px;fill:var(--txt-faint)}
  .axd{font-size:11px;fill:var(--txt-dim);font-weight:600;letter-spacing:.04em}
  .seg-bar{cursor:pointer;transition:opacity .18s,filter .18s}
  .seg-bar:hover{filter:brightness(1.2)}
  .dep-line{fill:none;stroke-width:1.5;opacity:.28;pointer-events:none}
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
    padding:11px 13px;min-width:230px;max-width:340px;box-shadow:0 12px 34px rgba(0,0,0,.55)}
  #tip.pinned{pointer-events:auto;border-color:var(--press);
    box-shadow:0 12px 34px rgba(0,0,0,.7),0 0 0 1px var(--press) inset}
  #tip h4{font-size:13px;font-weight:600;margin-bottom:7px;display:flex;align-items:center;gap:8px}
  #tip h4 .sw{width:9px;height:9px;border-radius:2px;flex:0 0 auto}
  #tip h4 .ttl{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #tip .close{position:absolute;top:6px;right:8px;width:22px;height:22px;border:0;
    background:transparent;color:var(--txt-faint);cursor:pointer;font-size:18px;
    line-height:1;border-radius:4px;display:none}
  #tip.pinned .close{display:block}
  #tip .close:hover{color:var(--txt);background:var(--rail)}
  #tip .sub{font-size:11.5px;color:var(--txt-dim);margin:-2px 0 8px;
    font-family:var(--mono);letter-spacing:.02em}
  #tip .r{display:flex;justify-content:space-between;gap:22px;font-size:11.5px;
    padding:2px 0;color:var(--txt-dim)}
  #tip .r b{color:var(--txt);font-weight:500;font-family:var(--mono);text-align:right}
  #tip .sep{height:1px;background:var(--line);margin:7px -3px}
  #tip .links{display:flex;flex-direction:column;gap:5px;margin-top:8px}
  #tip .lnk{display:flex;align-items:center;gap:7px;font-size:11.5px;
    font-family:var(--mono);color:var(--press);text-decoration:none;padding:3px 0}
  #tip .lnk:hover{text-decoration:underline}
  #tip .lnk svg{width:12px;height:12px;flex:0 0 auto;stroke:currentColor;
    stroke-width:1.6;fill:none}
  #tip .lnk .lbl{color:var(--txt-dim)}
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
    <div class="axisrow">
      <div class="corner"></div>
      <div class="axisscroll" id="axisscroll"><svg id="axis"></svg></div>
    </div>
    <div class="lanesrow">
      <div class="labelscroll" id="labelscroll"><div class="labels" id="labels"></div></div>
      <div class="scroll" id="scroll"><svg id="plot"></svg></div>
      <div class="empty" id="empty" style="display:none">No slots scheduled · Schedule board is empty</div>
    </div>
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

// Monday deep-links for the slot pop-out (#31). The slot's job_reference_id is
// the Blend Record item id (Gray Space account); ps_item_id is the originating
// Production Schedule item id (Nexiuum account), present only when the blend
// was created from a spec-form order — legacy Gray-Space-origin blends have
// none, so we surface the Blend Record link always and the PS link when known.
// The Deal/PO link is deferred (needs the PS↔deal relation — see #23/#31).
const MONDAY_BLEND_RECORDS_BOARD = 18404836849;
const MONDAY_PRODUCTION_SCHEDULE_BOARD = 8196668916;
const blendRecordUrl = id => `https://gray-space-force.monday.com/boards/${MONDAY_BLEND_RECORDS_BOARD}/pulses/${encodeURIComponent(id)}`;
const psItemUrl = id => `https://nexiuum.monday.com/boards/${MONDAY_PRODUCTION_SCHEDULE_BOARD}/pulses/${encodeURIComponent(id)}`;
const LINK_ICON = '<svg viewBox="0 0 24 24"><path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>';

let state = {
  snap: null,
  zoomHours: 24,
  pinnedSlotId: null,  // if set, the popout stays open across re-renders
  didInitScroll: false,  // open scrolled to "now" once, then leave the user's pan alone (#27)
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
      row.innerHTML = `<span>${esc(m.name)}</span><span class="cap">${esc(cap)}${esc(downTag)}</span>`;
      labels.appendChild(row);
      laneIndex.set(m.id, idx);
      idx++;
    }
  }

  // Time domain.
  // zoomHours sets the horizontal *density* — one zoom-window of time fills the
  // viewport's plot width. The domain then extends rightward to cover the
  // furthest scheduled slot, so the operator can pan (scroll left↔right) to
  // reach slots beyond the initial window instead of the whole timeline being
  // squeezed to fit on screen. See #11.
  const now = new Date(snap.read_at || Date.now());
  const windowSpan = state.zoomHours * 3600 * 1000;

  // Left edge of the domain. Open with a little history, but extend further
  // left to reach slots already in the past so the operator can scroll back —
  // bounded to a 7-day look-back so the domain (and the SVG width) can't run
  // away on very old data. The view opens scrolled to "now" (see initial
  // scrollLeft below); the past sits to the left, the future to the right. #27.
  const LOOKBACK_MS = 7 * 24 * 3600 * 1000;
  const lookbackFloor = now.getTime() - LOOKBACK_MS;
  let earliest = now.getTime() - windowSpan * 0.15;
  for (const s of snap.slots) {
    const st = s.actual_start ? parseISO(s.actual_start) : parseISO(s.planned_start);
    if (st && st.getTime() < earliest) earliest = st.getTime();
  }
  const t0 = Math.max(lookbackFloor, earliest);

  // Extend the domain end to the latest slot end if it runs past the default
  // window, with a little future padding so the last bar isn't flush to the
  // edge. Floor the default end at now + a sliver so "now" is always in domain
  // even when every slot is in the past.
  let dataEnd = Math.max(t0 + windowSpan, now.getTime() + windowSpan * 0.15);
  let extendsBeyondWindow = false;
  for (const s of snap.slots) {
    const e = s.actual_end ? parseISO(s.actual_end) : parseISO(s.planned_end);
    if (e && e.getTime() > dataEnd) { dataEnd = e.getTime(); extendsBeyondWindow = true; }
  }
  const t1 = extendsBeyondWindow ? dataEnd + windowSpan * 0.1 : dataEnd;

  // Geometry — these constants mirror the gutter CSS so the SVG lanes line up
  // with the HTML labels row-for-row:
  //   .grp-l   height: 28px  (= hdrH  — "PRESS" / "CAPSULE" / "PACKAGING" band)
  //   .lane-l  height: 34px  (= laneH — one machine row)
  //   .axisrow height: 28px  (= axisH — the time-axis strip, its own frozen row)
  // If you change any of these, change the matching CSS var (--hdr-h / --lane-h
  // / --axis-h) or the labels will drift out of alignment with the SVG lanes.
  // The lane SVG starts at y=0; the time axis lives in a separate frozen SVG
  // above it, so lane y-coords no longer reserve a top strip. #27.
  const laneH = 34, hdrH = 28, axisH = 28;
  const rowsByGroup = GROUP_ORDER.map(g => byGroup[g].length).filter(n => n > 0);
  const totalRows = rowsByGroup.reduce((a,b) => a + b, 0);
  const groupCount = rowsByGroup.length;
  const innerH = totalRows * laneH + groupCount * hdrH;
  const scroll = document.getElementById('scroll');
  const padL = 16, padR = 16;

  // Fixed horizontal density: one zoom-window spans exactly the viewport's plot
  // area (floored at 1200px as before). A longer domain → a wider-than-viewport
  // SVG → the .scroll container's overflow-x:auto provides the horizontal pan.
  // The labels gutter is a separate fixed element, so lane labels stay pinned.
  const viewportPlotW = Math.max(scroll.clientWidth, 1200) - padL - padR;
  const pxPerMs = viewportPlotW / windowSpan;
  const plotW = (t1 - t0) * pxPerMs;
  const innerW = plotW + padL + padR;

  const x = t => padL + (t - t0) * pxPerMs;

  // Lane y center, accounting for the top-axis strip + group headers.
  // Mirrors the labels gutter exactly: padding-top → group header → rows.
  function laneY(machineId) {
    const li = laneIndex.get(machineId);
    if (li == null) return null;
    let y = 0, seenRows = 0;
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

  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.getElementById('plot');
  svg.setAttribute('width', innerW);
  svg.setAttribute('height', innerH);
  svg.innerHTML = '';

  // The time axis is a separate frozen SVG (same width and x-domain as the
  // plot) so it stays pinned when the lane pane scrolls vertically. Its panel
  // background comes from the .axisscroll container. #27.
  const axisSvg = document.getElementById('axis');
  axisSvg.setAttribute('width', innerW);
  axisSvg.setAttribute('height', axisH);
  axisSvg.innerHTML = '';

  // Group separator bands (one per process group)
  let y = 0;
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
    // Grid lines span the full lane height in the plot SVG; the tick label
    // goes in the frozen axis SVG above so it stays pinned on vertical scroll.
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', xx); line.setAttribute('x2', xx);
    line.setAttribute('y1', 0); line.setAttribute('y2', innerH);
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
    axisSvg.appendChild(lbl);
    tt += tickStep;
  }

  // Now line — spans the full lane height, plus a matching tick in the axis.
  const nowX = x(Date.now());
  if (nowX >= padL && nowX <= innerW - padR) {
    const nl = document.createElementNS(ns, 'line');
    nl.setAttribute('x1', nowX); nl.setAttribute('x2', nowX);
    nl.setAttribute('y1', 0); nl.setAttribute('y2', innerH);
    nl.setAttribute('stroke', 'var(--now)');
    nl.setAttribute('stroke-width', 2);
    svg.appendChild(nl);
    const nlA = document.createElementNS(ns, 'line');
    nlA.setAttribute('x1', nowX); nlA.setAttribute('x2', nowX);
    nlA.setAttribute('y1', 0); nlA.setAttribute('y2', axisH);
    nlA.setAttribute('stroke', 'var(--now)');
    nlA.setAttribute('stroke-width', 2);
    axisSvg.appendChild(nlA);
  }

  // Dependency-line layer — inserted before the bars so the lines paint
  // beneath them; populated after the bar loop computes slot geometry. #29.
  const depLayer = document.createElementNS(ns, 'g');
  svg.appendChild(depLayer);
  const slotGeom = new Map();  // slot id -> {x0, x1, y} for drawn slots

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
    slotGeom.set(s.id, {x0: bx0, x1: bx1, y: ly});  // for dependency lines (#29)
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

    // Bar label: the engine-composed lane_label (N# identity, via
    // engine.core.labels). Falls back to the legacy job_label/last-6 chain
    // only if an older snapshot lacks lane_label.
    if (w > 60) {
      const t = document.createElementNS(ns, 'text');
      t.setAttribute('x', bx0 + 8); t.setAttribute('y', ly + 4);
      t.setAttribute('fill', '#0a0c10'); t.setAttribute('font-size', 11);
      t.setAttribute('font-weight', 600);
      t.setAttribute('pointer-events', 'none');
      const id = s.lane_label || s.job_label || (s.job_reference_id ? '#' + s.job_reference_id.slice(-6) : s.name.slice(0, 18));
      t.textContent = id + ' · ' + (s.quantity ? s.quantity.toLocaleString() : '');
      g.appendChild(t);
    }

    g.addEventListener('mouseenter', e => { if (!state.pinnedSlotId) showTip(s, e, color, false); });
    g.addEventListener('mousemove', e => { if (!state.pinnedSlotId) moveTip(e); });
    g.addEventListener('mouseleave', () => { if (!state.pinnedSlotId) hideTip(); });
    g.addEventListener('click', e => { e.stopPropagation(); pinTip(s, e, color); });
    svg.appendChild(g);
  }

  // Dependency lines — for each slot, draw a curve from every predecessor's
  // right edge to this (dependent) slot's left edge. dependent_on_ids holds the
  // slot's upstream predecessors; only slots actually drawn this frame have
  // geometry, so edges to clipped/off-screen slots are silently skipped.
  // Colored by the downstream job so a job's lines match its bars. #29.
  for (const s of snap.slots) {
    const cur = slotGeom.get(s.id);
    if (!cur || !s.dependent_on_ids) continue;
    const color = hashColor(s.job_reference_id || s.id);
    for (const depId of s.dependent_on_ids) {
      const pred = slotGeom.get(depId);
      if (!pred) continue;
      const x0 = pred.x1, y0 = pred.y, x1 = cur.x0, y1 = cur.y;
      const cx = (x0 + x1) / 2;
      const path = document.createElementNS(ns, 'path');
      path.setAttribute('d', `M ${x0} ${y0} C ${cx} ${y0} ${cx} ${y1} ${x1} ${y1}`);
      path.setAttribute('class', 'dep-line');
      path.setAttribute('stroke', color);
      depLayer.appendChild(path);
    }
  }

  // If a popout was pinned before this re-render (e.g., 30s poll fired),
  // re-attach it to the same slot in the new snapshot so it doesn't vanish.
  if (state.pinnedSlotId) {
    const slot = snap.slots.find(s => s.id === state.pinnedSlotId);
    if (slot) {
      const color = hashColor(slot.job_reference_id || slot.id);
      renderTip(slot, color, true);  // keep current position
    } else {
      unpinTip();
    }
  }

  // Empty-state indicator
  document.getElementById('empty').style.display = drewSlot ? 'none' : 'flex';

  // Meta line
  const m = document.getElementById('meta');
  const sc = snap.slots.length, mc = snap.machines.length;
  m.textContent = `${mc} machines · ${sc} slot${sc === 1 ? '' : 's'} · snapshot ${snap.read_at ? new Date(snap.read_at).toLocaleString('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit'}) : '—'}`;

  // Open scrolled to "now" on first paint (and after a zoom change, which
  // resets the flag) so present+future are in view and the past sits to the
  // left. After that, leave the operator's pan position alone across the 30s
  // polls. Then mirror the lane pane's scroll onto the frozen axis/labels. #27
  if (!state.didInitScroll) {
    scroll.scrollLeft = Math.max(0, x(now.getTime()) - scroll.clientWidth * 0.15);
    state.didInitScroll = true;
  }
  syncPanes();
}

// Mirror the master (lane) pane's scroll onto the frozen panes: axis tracks
// horizontal, labels gutter tracks vertical. Keeps the time axis pinned on
// vertical scroll and the machine labels pinned on horizontal scroll. #27
function syncPanes() {
  const scroll = document.getElementById('scroll');
  const ax = document.getElementById('axisscroll');
  const lab = document.getElementById('labelscroll');
  if (ax) ax.scrollLeft = scroll.scrollLeft;
  if (lab) lab.scrollTop = scroll.scrollTop;
}

// Escape user-data before it goes into innerHTML. Flavor (and the N#
// identity that folds into lane_label) originate from the spec-sheet form —
// free text an operator types. Without escaping, a flavor like
// `<img src=x onerror=...>` would execute when an operator opens the popover
// (stored XSS on this internal dashboard). The SVG bar label uses textContent
// and is already safe; this guards the innerHTML popover + lane-label paths.
function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]
  ));
}

// Render the popout content for `s`. `keepPosition` skips repositioning
// (used during background refresh when the popout is already pinned).
function renderTip(s, color, keepPosition) {
  const tip = document.getElementById('tip');
  const ps = s.actual_start || s.planned_start;
  const pe = s.actual_end || s.planned_end;
  const status = (s.status || '').toLowerCase();
  const driftTag = s.drift_last_detected_at ? '<span class="tag drift">DRIFT</span>' : '';
  // Title prefers the engine-composed lane_label (N# identity); name +
  // client read as the subline.
  const title = s.lane_label || s.job_label || (s.job_reference_id ? '#' + s.job_reference_id.slice(-6) : s.name || 'Slot ' + s.id);
  const subParts = [];
  if (s.job_name) subParts.push(s.job_name);
  if (s.job_client && (!s.job_name || !s.job_name.includes(s.job_client))) subParts.push(s.job_client);
  const subline = subParts.length ? `<div class="sub">${subParts.map(esc).join(' · ')}</div>` : '';
  // Source-record links (#31). Blend Record link is always available (the slot
  // is keyed by its id); the PS-item link only when the blend carries a
  // source-item correlation. Links are clickable only when the pop-out is
  // pinned (hover state is pointer-events:none).
  const linkRows = [];
  if (s.ps_item_id) {
    linkRows.push(`<a class="lnk" href="${psItemUrl(s.ps_item_id)}" target="_blank" rel="noopener">${LINK_ICON}<span class="lbl">Production Schedule item</span></a>`);
  }
  if (s.job_reference_id) {
    linkRows.push(`<a class="lnk" href="${blendRecordUrl(s.job_reference_id)}" target="_blank" rel="noopener">${LINK_ICON}<span class="lbl">Blend Record</span></a>`);
  }
  const linksHtml = linkRows.length ? `<div class="sep"></div><div class="links">${linkRows.join('')}</div>` : '';
  tip.innerHTML = `
    <button class="close" aria-label="Close" title="Close (esc)">&times;</button>
    <h4><span class="sw" style="background:${color}"></span><span class="ttl">${esc(title)}</span>
      <span class="tag ${status}">${esc((s.status||'').toUpperCase())}</span>${driftTag}</h4>
    ${subline}
    ${s.job_active ? `<div class="r"><span>active</span><b>${esc(s.job_active)}</b></div>` : ''}
    ${s.flavor ? `<div class="r"><span>flavor</span><b>${esc(s.flavor)}</b></div>` : ''}
    <div class="r"><span>recipe</span><b>${esc(s.recipe_key) || '—'}${s.recipe_version ? ' v' + esc(s.recipe_version) : ''}</b></div>
    <div class="r"><span>quantity</span><b>${s.quantity ? s.quantity.toLocaleString() : '—'}</b></div>
    <div class="sep"></div>
    <div class="r"><span>planned start</span><b>${ps ? fmtTime(new Date(ps)) : '—'}</b></div>
    <div class="r"><span>planned end</span><b>${pe ? fmtTime(new Date(pe)) : '—'}</b></div>
    ${s.actual_start ? `<div class="r"><span>actual start</span><b>${fmtTime(new Date(s.actual_start))}</b></div>` : ''}
    ${s.actual_end ? `<div class="r"><span>actual end</span><b>${fmtTime(new Date(s.actual_end))}</b></div>` : ''}
    ${s.drift_last_detected_at ? `<div class="r"><span>drift detected</span><b>${fmtTime(new Date(s.drift_last_detected_at))}</b></div>` : ''}
    <div class="sep"></div>
    <div class="r"><span>pulse id</span><b>${s.job_reference_id || '—'}</b></div>
    <div class="r"><span>slot id</span><b>${s.id}</b></div>
    ${linksHtml}
  `;
  tip.style.opacity = 1;
  // Wire up the close button each render — innerHTML rewrote the node.
  const close = tip.querySelector('.close');
  if (close) close.addEventListener('click', unpinTip);
  if (!keepPosition) {
    // Centered on screen as a fallback; pinTip() repositions to the click.
    tip.style.left = ((window.innerWidth - tip.offsetWidth) / 2) + 'px';
    tip.style.top = '120px';
  }
}

function showTip(s, e, color, pinned) {
  renderTip(s, color, false);
  const tip = document.getElementById('tip');
  if (pinned) tip.classList.add('pinned');
  else tip.classList.remove('pinned');
  if (e) moveTip(e);
}

function moveTip(e) {
  const tip = document.getElementById('tip');
  const x = Math.min(window.innerWidth - tip.offsetWidth - 14, e.clientX + 14);
  const y = Math.min(window.innerHeight - tip.offsetHeight - 14, e.clientY + 14);
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}

function hideTip() {
  if (state.pinnedSlotId) return;
  document.getElementById('tip').style.opacity = 0;
}

function pinTip(s, e, color) {
  state.pinnedSlotId = s.id;
  showTip(s, e, color, true);
}

function unpinTip() {
  state.pinnedSlotId = null;
  const tip = document.getElementById('tip');
  tip.classList.remove('pinned');
  tip.style.opacity = 0;
}

// Click anywhere outside the chart / popout dismisses the pin.
document.addEventListener('click', e => {
  if (!state.pinnedSlotId) return;
  const tip = document.getElementById('tip');
  if (!tip.contains(e.target)) unpinTip();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') unpinTip(); });

// Zoom controls
document.querySelectorAll('#zoom button').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('#zoom button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    state.zoomHours = parseInt(b.dataset.z, 10);
    state.didInitScroll = false;  // re-center on "now" at the new density (#27)
    render();
  });
});

// Mirror lane-pane scroll onto the frozen axis + labels gutter (#27).
document.getElementById('scroll').addEventListener('scroll', syncPanes);

window.addEventListener('resize', render);

fetchSnap();
setInterval(fetchSnap, 30000);
</script>
</body>
</html>
"""
