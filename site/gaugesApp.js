// ── Config ────────────────────────────────────────────────────────────────────
const COLUMBIA_ID = '14128870';

const TRIBUTARIES = [
  { id: '14123500', name: 'White Salmon River', sub: 'near Underwood, WA',    run: 'Husum Falls' },
  { id: '14128500', name: 'Wind River',         sub: 'near Carson, WA',       run: 'Wind River' },
  { id: '14120000', name: 'Hood River',         sub: 'at Tucker Bridge, OR',  run: 'Hood River' },
  { id: '14113000', name: 'Klickitat River',    sub: 'near Pitt, WA',         run: 'Klickitat' },
  { id: '14103000', name: 'Deschutes River',    sub: 'at Moody, OR',          run: 'Lower Deschutes' },
  { id: '14137000', name: 'Sandy River',        sub: 'near Marmot, OR',       run: 'Sandy River' },
];

const USGS_IV   = 'https://waterservices.usgs.gov/nwis/iv/';
const USGS_PAGE = 'https://waterdata.usgs.gov/monitoring-location/';

// ── USGS helpers ──────────────────────────────────────────────────────────────
async function fetchCurrent(ids) {
  const url = `${USGS_IV}?sites=${ids.join(',')}&format=json&parameterCd=00060`;
  const res = await fetch(url);
  if (!res.ok) throw new Error('USGS fetch failed');
  const data = await res.json();

  const out = {};
  for (const ts of data.value.timeSeries) {
    const id  = ts.sourceInfo.siteCode[0].value;
    const vals = ts.values[0].value;
    if (vals.length) {
      const last = vals[vals.length - 1];
      out[id] = { cfs: parseFloat(last.value), dateTime: last.dateTime };
    }
  }
  return out;
}

async function fetchHistory(id) {
  const url = `${USGS_IV}?sites=${id}&format=json&parameterCd=00060&period=P7D`;
  const res = await fetch(url);
  if (!res.ok) return [];
  const data = await res.json();
  const ts = data.value.timeSeries[0];
  if (!ts) return [];
  return ts.values[0].value
    .map(v => ({ cfs: parseFloat(v.value), dt: new Date(v.dateTime) }))
    .filter(v => !isNaN(v.cfs));
}

// ── Formatters ────────────────────────────────────────────────────────────────
function fmtCFS(cfs) {
  if (cfs == null || isNaN(cfs)) return '—';
  return cfs.toLocaleString(undefined, { maximumFractionDigits: 0 }) + ' cfs';
}

function timeAgo(dtStr) {
  const mins = Math.floor((Date.now() - new Date(dtStr)) / 60000);
  if (mins < 60)  return `${mins}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}

// ── Sparkline ─────────────────────────────────────────────────────────────────
function buildSparkline(data, w, h) {
  if (data.length < 2) return '';
  const vals  = data.map(d => d.cfs);
  const min   = Math.min(...vals);
  const max   = Math.max(...vals);
  const range = max - min || 1;

  const pts = data.map((d, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((d.cfs - min) / range) * (h - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');

  // Gradient fill
  const fillPts = `0,${h} ` + pts + ` ${w},${h}`;
  return `
    <defs>
      <linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#3b82f6" stop-opacity="0.25"/>
        <stop offset="100%" stop-color="#3b82f6" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <polygon points="${fillPts}" fill="url(#sg)"/>
    <polyline points="${pts}" fill="none" stroke="#3b82f6" stroke-width="1.8"
              stroke-linejoin="round" stroke-linecap="round"/>`;
}

// ── Render ────────────────────────────────────────────────────────────────────
async function load() {
  const allIds = [COLUMBIA_ID, ...TRIBUTARIES.map(t => t.id)];

  // Kick off both fetches in parallel
  const [current, history] = await Promise.all([
    fetchCurrent(allIds),
    fetchHistory(COLUMBIA_ID),
  ]);

  // ── Columbia featured card ──
  const colData = current[COLUMBIA_ID];
  const readingEl = document.getElementById('columbiaReading');
  const timeEl    = document.getElementById('columbiaTime');

  if (readingEl) readingEl.textContent = colData ? fmtCFS(colData.cfs) : '—';
  if (timeEl && colData) timeEl.textContent = 'Updated ' + timeAgo(colData.dateTime);

  // Sparkline
  const chartEl = document.getElementById('columbiaChart');
  if (chartEl && history.length > 1) {
    const w = chartEl.clientWidth || 600;
    const h = 64;
    chartEl.innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg"
           style="width:100%;height:${h}px;display:block">
        ${buildSparkline(history, w, h)}
      </svg>`;

    // Min/max labels
    const vals  = history.map(d => d.cfs);
    const label = document.getElementById('columbiaRange');
    if (label) {
      const lo = Math.min(...vals).toLocaleString(undefined, { maximumFractionDigits: 0 });
      const hi = Math.max(...vals).toLocaleString(undefined, { maximumFractionDigits: 0 });
      label.textContent = `7-day range: ${lo} – ${hi} cfs`;
    }
  }

  // ── Tributary cards ──
  const grid = document.getElementById('gaugeGrid');
  if (!grid) return;

  for (const g of TRIBUTARIES) {
    const d    = current[g.id];
    const card = document.createElement('a');
    card.className = 'gauge-card';
    card.href      = USGS_PAGE + g.id + '/';
    card.target    = '_blank';
    card.rel       = 'noopener noreferrer';

    card.innerHTML = `
      <div class="gc-top">
        <div class="gc-name">${g.name}</div>
        <div class="gc-sub">${g.sub}</div>
      </div>
      <div class="gc-reading">${d ? fmtCFS(d.cfs) : '—'}</div>
      <div class="gc-footer">
        <span class="gc-run badge">${g.run}</span>
        <span class="gc-time">${d ? timeAgo(d.dateTime) : 'no data'}</span>
      </div>`;

    grid.appendChild(card);
  }
}

load().catch(err => {
  console.error('Gauge load failed:', err);
  const grid = document.getElementById('gaugeGrid');
  if (grid) grid.innerHTML = '<p style="color:#6b7280;font-size:.85rem">Could not load USGS data.</p>';
});
