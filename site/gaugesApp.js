// ── River definitions ─────────────────────────────────────────────────────────
const RIVERS = [
  {
    id:      '14128870',
    name:    'Columbia River',
    sub:     'Below Bonneville Dam, OR',
    context: 'Main stem reference gauge for the Gorge. High spring flows (Mar–Jun) drive the best wind and kite conditions.',
    ranges:  [
      { label: 'Low',    max: 100000, color: '#22c55e' },
      { label: 'Normal', max: 300000, color: '#3b82f6' },
      { label: 'High',   max: Infinity, color: '#f97316' },
    ],
  },
  {
    id:      '14123500',
    name:    'White Salmon River',
    sub:     'Near Underwood, WA',
    context: 'Downstream gauge — use GorgeGauge trail cam for Husum Falls staff reading. 400–900 cfs is the classic Husum range.',
    ranges:  [
      { label: 'Low (<300)',     max: 300,  color: '#22c55e' },
      { label: 'Prime (300–900)', max: 900,  color: '#3b82f6' },
      { label: 'High (>900)',    max: Infinity, color: '#f97316' },
    ],
  },
  {
    id:      '14128500',
    name:    'Wind River',
    sub:     'Near Carson, WA',
    context: 'Tight canyon run above Carson. Best between 150–500 cfs. Washes out above 700 cfs.',
    ranges:  [
      { label: 'Low (<150)',     max: 150,  color: '#22c55e' },
      { label: 'Prime (150–500)', max: 500, color: '#3b82f6' },
      { label: 'High (>500)',    max: Infinity, color: '#f97316' },
    ],
  },
  {
    id:      '14120000',
    name:    'Hood River',
    sub:     'At Tucker Bridge, OR',
    context: 'Snowmelt-driven. Main Fork runs well 200–700 cfs. West Fork (Dee) adds volume in spring.',
    ranges:  [
      { label: 'Low (<200)',     max: 200,  color: '#22c55e' },
      { label: 'Prime (200–700)', max: 700, color: '#3b82f6' },
      { label: 'High (>700)',    max: Infinity, color: '#f97316' },
    ],
  },
  {
    id:      '14113000',
    name:    'Klickitat River',
    sub:     'Near Pitt, WA',
    context: 'Long multi-day run from Glenwood to Lyle. Sweet spot 500–2,000 cfs. Classic at ~800 cfs.',
    ranges:  [
      { label: 'Low (<500)',       max: 500,  color: '#22c55e' },
      { label: 'Prime (500–2000)', max: 2000, color: '#3b82f6' },
      { label: 'High (>2000)',     max: Infinity, color: '#f97316' },
    ],
  },
  {
    id:      '14103000',
    name:    'Deschutes River',
    sub:     'At Moody, OR',
    context: 'Lower Deschutes — dam-regulated and consistent. 1,500–4,000 cfs is the year-round paddling range.',
    ranges:  [
      { label: 'Low (<1500)',       max: 1500, color: '#22c55e' },
      { label: 'Prime (1500–4000)', max: 4000, color: '#3b82f6' },
      { label: 'High (>4000)',      max: Infinity, color: '#f97316' },
    ],
  },
  {
    id:      '14137000',
    name:    'Sandy River',
    sub:     'Near Marmot, OR',
    context: 'Closest Gorge-area run to Portland. Marmot to Revenue Bridge is best 200–700 cfs. Very bony below 150.',
    ranges:  [
      { label: 'Low (<200)',     max: 200,  color: '#22c55e' },
      { label: 'Prime (200–700)', max: 700, color: '#3b82f6' },
      { label: 'High (>700)',    max: Infinity, color: '#f97316' },
    ],
  },
];

const USGS_IV   = 'https://waterservices.usgs.gov/nwis/iv/';
const USGS_PAGE = 'https://waterdata.usgs.gov/monitoring-location/';

// ── USGS fetch ────────────────────────────────────────────────────────────────
async function fetchAll() {
  const ids = RIVERS.map(r => r.id).join(',');
  const url = `${USGS_IV}?sites=${ids}&format=json&parameterCd=00060&period=P7D`;
  const res = await fetch(url);
  if (!res.ok) throw new Error('USGS fetch failed: ' + res.status);
  return res.json();
}

function parseTimeSeries(data) {
  const out = {};
  for (const ts of data.value.timeSeries) {
    const id   = ts.sourceInfo.siteCode[0].value;
    const vals = ts.values[0].value
      .map(v => ({ cfs: parseFloat(v.value), dt: new Date(v.dateTime) }))
      .filter(v => !isNaN(v.cfs));
    if (vals.length) out[id] = vals;
  }
  return out;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtCFS(cfs) {
  return cfs.toLocaleString(undefined, { maximumFractionDigits: 0 }) + ' cfs';
}

function timeAgo(dt) {
  const mins = Math.floor((Date.now() - dt) / 60000);
  if (mins < 60)   return `${mins}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}

function levelColor(river, cfs) {
  for (const r of river.ranges) {
    if (cfs < r.max) return r.color;
  }
  return '#ef4444';
}

function levelLabel(river, cfs) {
  for (const r of river.ranges) {
    if (cfs < r.max) return r.label;
  }
  return river.ranges[river.ranges.length - 1].label;
}

// ── Sparkline SVG ─────────────────────────────────────────────────────────────
function sparkline(vals, w, h, color) {
  if (vals.length < 2) return '';
  const cfsList = vals.map(v => v.cfs);
  const lo  = Math.min(...cfsList);
  const hi  = Math.max(...cfsList);
  const rng = hi - lo || 1;
  const pad = 4;

  const pts = vals.map((v, i) => {
    const x = (i / (vals.length - 1)) * w;
    const y = h - pad - ((v.cfs - lo) / rng) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  const linePts = pts.join(' ');
  const fillPts = `0,${h} ` + linePts + ` ${w},${h}`;

  return `<svg viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg"
    preserveAspectRatio="none" style="width:100%;height:${h}px;display:block">
    <defs>
      <linearGradient id="g${w}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.2"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <polygon points="${fillPts}" fill="url(#g${w})"/>
    <polyline points="${linePts}" fill="none" stroke="${color}"
      stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

// ── Tick labels ───────────────────────────────────────────────────────────────
function chartTicks(vals) {
  if (!vals || vals.length < 2) return '';
  const lo = Math.min(...vals.map(v => v.cfs));
  const hi = Math.max(...vals.map(v => v.cfs));
  const oldest = vals[0].dt;
  const newest = vals[vals.length - 1].dt;
  const fmtDate = d => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  const fmtNum  = n => n >= 1000
    ? (n / 1000).toFixed(n % 1000 === 0 ? 0 : 1) + 'k'
    : Math.round(n).toString();

  return `<div class="rg-ticks">
    <span>${fmtDate(oldest)}</span>
    <span class="rg-tick-range">${fmtNum(lo)} – ${fmtNum(hi)} cfs</span>
    <span>${fmtDate(newest)}</span>
  </div>`;
}

// ── Build river section ───────────────────────────────────────────────────────
function buildSection(river, vals) {
  const latest = vals ? vals[vals.length - 1] : null;
  const cfs    = latest ? latest.cfs : null;
  const color  = cfs != null ? levelColor(river, cfs) : '#475569';
  const label  = cfs != null ? levelLabel(river, cfs) : '—';

  const section = document.createElement('article');
  section.className = 'rg-section';

  const W = 800, H = 80;
  const chart = vals ? sparkline(vals, W, H, color) : '';
  const ticks  = chartTicks(vals);

  section.innerHTML = `
    <div class="rg-header">
      <div class="rg-header-left">
        <h2 class="rg-name">${river.name}</h2>
        <div class="rg-sub">${river.sub}</div>
      </div>
      <div class="rg-header-right">
        <div class="rg-cfs" style="color:${color}">${cfs != null ? fmtCFS(cfs) : '—'}</div>
        <div class="rg-meta">
          <span class="rg-badge" style="color:${color};border-color:${color}44;background:${color}18">${label}</span>
          ${latest ? `<span class="rg-ago">${timeAgo(latest.dt)}</span>` : ''}
        </div>
      </div>
    </div>

    ${chart ? `<div class="rg-chart">${chart}</div>${ticks}` : '<div class="rg-no-chart">No chart data</div>'}

    <div class="rg-footer">
      <p class="rg-context">${river.context}</p>
      <a class="rg-usgs-link" href="${USGS_PAGE}${river.id}/"
         target="_blank" rel="noopener noreferrer">USGS ${river.id} ↗</a>
    </div>`;

  return section;
}

// ── Boot ──────────────────────────────────────────────────────────────────────
async function load() {
  const list = document.getElementById('riverList');

  try {
    const raw    = await fetchAll();
    const series = parseTimeSeries(raw);

    list.innerHTML = '';
    for (const river of RIVERS) {
      list.appendChild(buildSection(river, series[river.id] || null));
    }
  } catch (err) {
    console.error(err);
    list.innerHTML = '<p class="rg-loading">Could not load USGS data. Try refreshing.</p>';
  }
}

load();
