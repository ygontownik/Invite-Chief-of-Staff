# Track G — cos-dashboard-tile.html.delta.md

Patches for `~/dashboards/app/templates/cos-dashboard.template.html`
(NOT the live `cos-dashboard.html`). Adds a "Costs" tile fed by
`DATA.costs` (the key Patch 2 in `fetch.delta.md` populates).

Design constraint per global instructions: light backgrounds only, cream/paper
tokens. No dark surfaces.

---

## Patch 1: tile container — insert into the Status-top section

**Insertion anchor** in `cos-dashboard.template.html` (around line 1262):

```javascript
    ${showStatusTop ? `<div id="section-status-top">${buildStatusTopRow()}</div>` : ''}
```

**INSERT IMMEDIATELY AFTER**:

```javascript
    ${showStatusTop ? `<div id="section-costs">${renderCostsTile(window.DATA && window.DATA.costs)}</div>` : ''}
```

(If `buildStatusTopRow()` is the more natural composition point, instead append
inside its returned template — but for an isolated initial landing, sibling
placement is safer and self-contained.)

## Patch 2: renderer function

**Insertion anchor**: just above the existing `function buildTopicsPanel()` at
line 1271 (`// ── Topics watchlist ───`). Anchor string:

```javascript
// ── Topics watchlist (persistent to data/user-state/topics.json) ─────────
```

**INSERT BEFORE** that comment:

```javascript
// ── Costs / Quota tile (Track G) ─────────────────────────────────────────
// Source: DATA.costs (built by costs_aggregator.format_for_tile).
// Shape: { summary, totalUsd, lookbackDays, topModels:[[name,usd],...],
//          topPasses, topRoutines, dailyChart:[{date,usd}], filesSeen, linesRead }
function renderCostsTile(costs) {
  if (!costs || typeof costs !== 'object') {
    return ''; // tile is opt-in; render nothing if data absent
  }
  const total   = (costs.totalUsd || 0).toFixed(2);
  const lookback= costs.lookbackDays || 30;
  const lines   = costs.linesRead   || 0;
  const files   = costs.filesSeen   || 0;

  const fmtRow = (pair) => {
    const name = esc(String(pair[0]));
    const usd  = Number(pair[1] || 0).toFixed(2);
    return `<tr><td style="padding:2px 8px 2px 0;color:#334155">${name}</td>
            <td style="padding:2px 0;text-align:right;font-variant-numeric:tabular-nums;color:#0f1e38">$${usd}</td></tr>`;
  };
  const tbl = (rows) => rows.length
    ? `<table style="width:100%;border-collapse:collapse;font-size:11px">${rows.map(fmtRow).join('')}</table>`
    : `<div style="color:#94a3b8;font-size:11px">no data</div>`;

  // Sparkline: inline SVG, no JS deps. Light cream background.
  const daily = Array.isArray(costs.dailyChart) ? costs.dailyChart : [];
  const max   = daily.reduce((m,d) => Math.max(m, d.usd||0), 0) || 1;
  const w = 120, h = 24;
  const pts = daily.map((d,i) => {
    const x = daily.length > 1 ? (i / (daily.length - 1)) * w : 0;
    const y = h - ((d.usd || 0) / max) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const spark = daily.length
    ? `<svg width="${w}" height="${h}" style="display:block">
         <polyline fill="none" stroke="#0f1e38" stroke-width="1.2" points="${pts}"/>
       </svg>`
    : '';

  return `
  <div class="tc-card tile" id="costs-tile" style="margin:0 0 18px;padding:14px 16px;background:var(--paper, #fdfaf3)">
    <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
      <div style="font-size:10px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#64748b;font-family:var(--font-data, monospace)">Anthropic Spend — Last ${lookback}d</div>
      <div style="font-size:18px;font-weight:600;color:#0f1e38;font-variant-numeric:tabular-nums">$${total}</div>
    </div>
    <div style="display:flex;gap:14px;align-items:flex-start">
      <div style="flex:1;min-width:0">
        <div style="font-size:10px;color:#64748b;margin-bottom:3px;text-transform:uppercase;letter-spacing:0.08em">By model</div>
        ${tbl(costs.topModels || [])}
      </div>
      <div style="flex:1;min-width:0">
        <div style="font-size:10px;color:#64748b;margin-bottom:3px;text-transform:uppercase;letter-spacing:0.08em">By routine</div>
        ${tbl(costs.topRoutines || [])}
      </div>
      <div style="flex:0 0 auto">
        <div style="font-size:10px;color:#64748b;margin-bottom:3px;text-transform:uppercase;letter-spacing:0.08em">Daily</div>
        ${spark}
      </div>
    </div>
    <div style="margin-top:6px;font-size:10px;color:#94a3b8">${lines} call${lines===1?'':'s'} across ${files} day file${files===1?'':'s'}</div>
  </div>`;
}
```

## Patch 3 (optional, defer-able): refresh wiring

If the template re-renders on `/refresh` via `window.DATA = ...`, no extra wiring
is needed — the next render picks up the new `costs` block automatically.

If a more granular update is wanted, add to the existing post-fetch reflow:

```javascript
const costsHost = document.getElementById('section-costs');
if (costsHost) costsHost.innerHTML = renderCostsTile(window.DATA && window.DATA.costs);
```

## Notes

- Renderer is a no-op when `DATA.costs` is absent — safe to ship before the fetch
  delta lands.
- All colors pulled from existing palette (`#0f1e38`, `#64748b`, `#94a3b8`,
  `--paper`). No dark surfaces (per global UI rule).
- SVG sparkline avoids any chart-library dependency.
- `esc()` is the existing global escape helper used elsewhere in the template.
