// The Gorge's River Pulse.
//
// Every gauged river in the Columbia Gorge drawn as a wall of water on a
// tilted table. Wall height is that day's mean discharge, wall thickness is
// the river's long-term mean, and colour is the day's water temperature.
// Plain canvas, no libraries: a rotated orthographic projection and a
// painter's sort of wall segments from far to near.

(() => {
  "use strict";

  // ── Data ────────────────────────────────────────────────────────────────
  const URLS = {
    rivers: "/pulse/rivers.json",
    flows: "/pulse/flows.json",
    husum: ["/husumGaugeHistory.json", "https://gorgegauge.com/husumGaugeHistory.json"],
  };

  // Wall proportions, in kilometres of table. Height uses a square root so
  // the Columbia (150,000+ cfs) does not flatten the tributaries (500–5,000).
  const HEIGHT_K = 0.085;          // km per sqrt(cfs), "flow" mode
  const NORMAL_KM = 5;             // wall height at exactly the long-term mean, "vs normal" mode
  const NORMAL_POW = 0.7;          // floods rise, but less than linearly, so a 10× day is 5× tall
  const WIDTH_K = 0.16;            // km per sqrt(mean cfs / 1000)
  const MAX_WIDTH_KM = 2.0;
  const MIN_WIDTH_PX = 2.2;
  const CFS_TO_CMS = 0.0283168;
  const DAYS_PER_SECOND = 8;       // a year in about 45 seconds at 1×
  const SPEEDS = [0.5, 1, 2, 4];             // ½× is the default: a year in about 90 seconds
  const SPEED_LABELS = ["½×", "1×", "2×", "4×"];

  const PEAKS = [
    { name: "Mt Adams", lon: -121.4909, lat: 46.2024 },
    { name: "Mt Hood", lon: -121.6959, lat: 45.3735 },
    { name: "Mt St Helens", lon: -122.1956, lat: 46.1914 },
  ];
  const TOWNS = [
    { name: "Portland", lon: -122.6784, lat: 45.5152, big: true },
    { name: "Hood River", lon: -121.5215, lat: 45.7054, big: true },
    { name: "The Dalles", lon: -121.1787, lat: 45.5946 },
    { name: "Boardman", lon: -119.7006, lat: 45.8398 },
    { name: "Stevenson", lon: -121.8834, lat: 45.6957 },
    { name: "Goldendale", lon: -120.8217, lat: 45.8207 },
  ];
  const HUSUM = { name: "Husum Falls", lon: -121.4876, lat: 45.7998 };

  // Temperature palette, °C → colour. Cold teal-blue through sand to a hot red.
  const RAMP = [
    [29, 78, 92], [39, 95, 106], [49, 113, 120], [58, 130, 134], [77, 146, 146], [100, 161, 157],
    [124, 176, 169], [147, 185, 173], [166, 175, 149], [186, 165, 124], [206, 156, 100], [203, 137, 84],
    [196, 117, 69], [188, 97, 54], [175, 78, 43], [153, 63, 36], [131, 48, 28], [109, 33, 21],
  ];
  const TEMP_LO = 0, TEMP_HI = 22;
  const TEMP_STOPS = RAMP.map((c, i) => [TEMP_LO + (TEMP_HI - TEMP_LO) * i / (RAMP.length - 1), c]);
  const NO_TEMP = [128, 134, 132];

  // Rivers with no temperature gauge borrow a neighbour's daily reading and
  // are drawn desaturated with "est." in the tooltip. Chosen by regime:
  // glacier-fed mainstems off a volcano follow the Hood, Cascade snowmelt
  // rivers the Wind, low rain-fed rivers the Sandy. A guess, and shown as one.
  const TEMP_PROXY = {
    "14101500": "14120000",   // White River (Mt Hood glaciers) ← Hood
    "14231000": "14120000",   // Upper Cowlitz (Rainier glaciers) ← Hood
    "14242580": "14120000",   // Toutle (St Helens) ← Hood
    "14216000": "14128500",   // Upper Lewis ← Wind
    "14220500": "14128500",   // Lewis at Ariel ← Wind
    "14222500": "14142500",   // East Fork Lewis ← Sandy
    "14243000": "14142500",   // Cowlitz at Castle Rock ← Sandy
  };

  // ── State ───────────────────────────────────────────────────────────────
  const page = document.getElementById("page");
  const canvas = document.getElementById("map");
  let ctx = canvas.getContext("2d");
  const rainCanvas = document.getElementById("rain");
  const rctx = rainCanvas.getContext("2d");
  const spark = document.getElementById("spark");
  const sctx = spark.getContext("2d");
  const tip = document.getElementById("tip");

  let rivers = [];       // enriched river records
  let flows = null;      // flows.json
  let husum = {};        // iso date → { level, low, high, confidence }
  let dayCount = 0;
  let day = 0;           // selected day index
  let combined = [];     // tributaries combined, per day (cfs)
  let columbia = [];     // Columbia at The Dalles, per day
  let peakDay = 0, floorDay = 0;
  let unit = stored("pulse:units", "cfs");
  let heightMode = stored("pulse:height", "relative");   // "relative" (vs normal) | "flow"
  let playing = false, loop = true, speedIx = 0, lastTick = 0, carry = 0;
  let loopFrom = 0, autoplay = false;   // the loop restarts a year back, not at the first day
  const terrain = window.PulseTerrain || null;           // optional relief module (pulse-terrain.js)
  let hovered = null, focused = null, focusUntil = 0;
  let hits = [];         // polygons drawn this frame, for hover
  let staticLayer = null, staticKey = "";   // table + relief + network + landmarks, per camera
  let rain = null;         // flows.rain, when present
  let drops = [];          // the pool of candidate raindrops over the frame
  let rainLoop = false, rainLast = 0;
  let rainOn = stored("pulse:rain", "off");   // off until someone turns it on; remembered
  let theme = readTheme();
  let W = 0, H = 0, DPR = 1;

  function stored(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
  }
  function store(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  // Ground height under a point, in km of table, when the relief module is
  // present; the table is flat otherwise.
  const groundZ = (x, y) => (terrain && terrain.ready ? terrain.zAt(x, y) : 0);

  // Ground coordinates: kilometres east and north of the frame's centre.
  const proj = { lon0: 0, lat0: 0, kx: 1, ky: 110.574 };
  const toKm = (lon, lat) => [(lon - proj.lon0) * proj.kx, (lat - proj.lat0) * proj.ky];

  const cam = { yaw: 0, pitch: 58 * Math.PI / 180, zoom: 4, cx: 0, cy: 0, lift: 0 };
  const home = {};
  let bboxKm = null;

  // ── Loading ─────────────────────────────────────────────────────────────
  async function fetchJson(url) {
    const r = await fetch(url, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  async function fetchFirst(urls) {
    for (const u of urls) {
      try { return await fetchJson(u); } catch (e) { /* try the next */ }
    }
    return null;
  }

  async function load() {
    try {
      const [rv, fl] = await Promise.all([fetchJson(URLS.rivers), fetchJson(URLS.flows)]);
      prepare(rv, fl);
      page.dataset.state = "ready";
      fetchFirst(URLS.husum).then((h) => { if (h) { prepareHusum(h); update(); } });
    } catch (e) {
      console.error(e);
      page.dataset.state = "error";
      document.querySelector("#loading span").textContent = "Could not load river data.";
    }
  }

  function prepare(rv, fl) {
    flows = fl;
    dayCount = fl.days;

    const [w, s, e, n] = rv.bbox;
    proj.lon0 = (w + e) / 2;
    proj.lat0 = (s + n) / 2;
    proj.kx = 111.32 * Math.cos(proj.lat0 * Math.PI / 180);
    const [x0, y0] = toKm(w, s);
    const [x1, y1] = toKm(e, n);
    bboxKm = { x0, y0, x1, y1 };

    rivers = rv.rivers.map((r, ix) => {
      const series = fl.sites[r.site] || { cfs: [], temp: [] };
      const cfs = forwardFill(series.cfs, dayCount);
      const temp = forwardFill(series.temp, dayCount, 3);
      const tempEst = new Array(dayCount).fill("");     // "" gauge, "norm", or the proxy's name
      const mean = r.mean_cfs || 500;
      const walls = r.walls.map((line) => buildWall(line));
      const ground = r.ground.map((line) => line.map(([lo, la]) => toKm(lo, la)));
      return {
        ix, site: r.site, name: r.name, short: r.short, kind: r.kind,
        counted: r.counted != null ? r.counted : r.kind !== "spine",
        gauge: toKm(r.gauge[0], r.gauge[1]),
        mean, normal: r.normal || [],
        widthKm: Math.min(WIDTH_K * Math.sqrt(mean / 1000), MAX_WIDTH_KM),
        labelPt: r.kind === "spine" ? toKm(r.gauge[0], r.gauge[1]) : labelAnchor(walls),
        cfs, temp, tempEst, walls, ground,
      };
    });

    // Missing temperatures. A river with a record fills its gaps from its
    // own seasonal norm (the mean of its readings within a week of that date
    // in any year); a river with no gauge follows its proxy. Both are marked
    // so the wall is drawn hedged and the tooltip says what it is showing.
    const bySite = Object.fromEntries(rivers.map((r) => [r.site, r]));
    for (const r of rivers) {
      const gauge = r.temp.slice();
      const clim = climatology(gauge);
      for (let i = 0; i < dayCount; i++) {
        if (gauge[i] != null) continue;
        const c = clim[dayOfYearLeap(i)];
        if (c != null) { r.temp[i] = c; r.tempEst[i] = "norm"; }
      }
    }
    for (const r of rivers) {
      const proxy = bySite[TEMP_PROXY[r.site]];
      if (!proxy) continue;
      for (let i = 0; i < dayCount; i++) {
        if (r.temp[i] != null) continue;
        if (proxy.temp[i] != null) { r.temp[i] = proxy.temp[i]; r.tempEst[i] = proxy.short; }
      }
    }

    // Combined tributary flow and the Columbia, per day.
    combined = new Array(dayCount).fill(null);
    columbia = new Array(dayCount).fill(null);
    for (let i = 0; i < dayCount; i++) {
      let sum = 0, any = false;
      for (const r of rivers) {
        const v = r.cfs[i];
        if (r.kind === "spine") { columbia[i] = v; continue; }
        if (!r.counted) continue;
        if (v != null) { sum += v; any = true; }
      }
      combined[i] = any ? sum : null;
    }
    peakDay = argBest(combined, (a, b) => a > b);
    floorDay = argBest(combined, (a, b) => a < b);

    // Open a year back and play forward to today; a #d= in the URL wins.
    const last = lastDayWithData();
    loopFrom = Math.max(0, last - 365);
    const fromHash = dayFromHash();
    day = fromHash != null ? fromHash : loopFrom;
    autoplay = fromHash == null;

    document.getElementById("namesCount").textContent = `${rivers.length} gauged rivers`;
    const counted = rivers.filter((r) => r.counted);
    document.getElementById("tribNote").textContent =
      `${wordNumber(counted.length)} rivers`;
    document.getElementById("dataAge").textContent = `data through ${fmtDateShort(dayCount - 1)}`;
    const since = new Date(fl.start + "T00:00:00Z").toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
    document.querySelector(".rp-kicker").textContent = `EVERY GAUGED RIVER, EVERY DAY SINCE ${since.toUpperCase()}`;

    if (terrain && terrain.init) terrain.init({ proj: { ...proj }, bboxKm: { ...bboxKm }, toKm });
    prepareRain(fl.rain);
    document.getElementById("legendBar").style.background =
      `linear-gradient(90deg, ${RAMP.map((c, i) => `rgb(${c.join(",")}) ${(i / (RAMP.length - 1) * 100).toFixed(1)}%`).join(", ")})`;
    spark.setAttribute("aria-valuemax", String(dayCount - 1));
    buildMonths();
    fitCamera();
    Object.assign(home, { yaw: cam.yaw, pitch: cam.pitch, zoom: cam.zoom, cx: cam.cx, cy: cam.cy });
    buildNames();
    resize();
    update();
    if (autoplay) { autoplay = false; togglePlay(); }
  }

  function prepareHusum(list) {
    husum = {};
    for (const e of list) {
      if (!e.read_at_iso || e.level == null) continue;
      // Local date in the Pacific zone, which is where the camera lives.
      const d = new Date(e.read_at_iso).toLocaleDateString("en-CA", { timeZone: "America/Los_Angeles" });
      husum[d] = { level: e.level, low: e.low, high: e.high, confidence: e.confidence };
    }
  }

  // Missing days inherit the last known value, up to `maxGap` days, so a
  // one-day gap in a record does not make a river vanish.
  function forwardFill(arr, n, maxGap = 7) {
    const out = new Array(n).fill(null);
    let last = null, age = 0;
    for (let i = 0; i < n; i++) {
      const v = arr && arr[i] != null ? arr[i] : null;
      if (v != null) { last = v; age = 0; out[i] = v; }
      else if (last != null && ++age <= maxGap) out[i] = last;
    }
    return out;
  }

  // Mean reading within ±7 days of each day of the year, across all years.
  // null where the record never covers that time of year.
  function climatology(temp) {
    const sum = new Array(366).fill(0), n = new Array(366).fill(0);
    for (let i = 0; i < temp.length; i++) {
      if (temp[i] == null) continue;
      const d = dayOfYearLeap(i);
      for (let k = -7; k <= 7; k++) { const j = (d + k + 366) % 366; sum[j] += temp[i]; n[j]++; }
    }
    return sum.map((v, j) => (n[j] ? Math.round((v / n[j]) * 10) / 10 : null));
  }

  function argBest(arr, better) {
    let best = -1;
    for (let i = 0; i < arr.length; i++) {
      if (arr[i] == null) continue;
      if (best < 0 || better(arr[i], arr[best])) best = i;
    }
    return Math.max(best, 0);
  }

  function lastDayWithData() {
    for (let i = dayCount - 1; i >= 0; i--) if (combined[i] != null) return i;
    return 0;
  }

  // Labels sit mid-way along the river's longest reach, which keeps them
  // apart from each other and from the gauge cluster at the Columbia.
  function labelAnchor(walls) {
    let best = null, bestLen = -1;
    for (const w of walls) {
      let len = 0;
      for (let i = 1; i < w.pts.length; i++) len += Math.hypot(w.pts[i][0] - w.pts[i - 1][0], w.pts[i][1] - w.pts[i - 1][1]);
      if (len > bestLen) { bestLen = len; best = w; }
    }
    return best ? best.pts[Math.floor(best.pts.length / 2)] : [0, 0];
  }

  // A wall is a polyline in km with a unit normal at each vertex, averaged
  // across the two segments that meet there so adjacent quads share corners.
  function buildWall(line) {
    const pts = line.map(([lo, la]) => toKm(lo, la));
    const segN = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const dx = pts[i + 1][0] - pts[i][0], dy = pts[i + 1][1] - pts[i][1];
      const len = Math.hypot(dx, dy) || 1;
      segN.push([-dy / len, dx / len]);
    }
    const normals = pts.map((_, i) => {
      const a = segN[Math.max(i - 1, 0)], b = segN[Math.min(i, segN.length - 1)];
      let nx = a[0] + b[0], ny = a[1] + b[1];
      const len = Math.hypot(nx, ny);
      if (len < 0.3) return a;              // hairpin: fall back to one side
      nx /= len; ny /= len;
      const dot = Math.max(nx * a[0] + ny * a[1], 0.5);
      return [nx / dot, ny / dot];          // miter, clamped
    });
    return { pts, normals };
  }

  // ── Camera ──────────────────────────────────────────────────────────────
  const isPhone = () => W < 720 || H < 520;

  // The band of screen between the header and the footer, measured live so
  // the map sits in the space that is actually free of overlays.
  function freeBand() {
    const head = document.querySelector(".rp-head"), foot = document.querySelector(".rp-foot");
    const headH = head ? head.offsetHeight : 0, footH = foot ? foot.offsetHeight : 0;
    document.documentElement.style.setProperty("--head-h", `${headH}px`);
    document.documentElement.style.setProperty("--foot-h", `${footH}px`);
    return { top: headH, bottom: H - footH };
  }

  function fitCamera() {
    if (!bboxKm) return;
    const bw = bboxKm.x1 - bboxKm.x0, bh = bboxKm.y1 - bboxKm.y0;
    const phone = isPhone();
    const band = freeBand();
    // On desktop the header only occupies the left, so the map may run
    // under it; on phones the band is all there is.
    const top = phone ? band.top : Math.min(band.top, 70);
    const bottom = phone ? band.bottom : band.bottom + (H - band.bottom) * 0.3;
    const usableW = W - (phone ? 24 : 60);
    const usableH = Math.max(120, bottom - top - 16);
    cam.yaw = 0;
    cam.pitch = (phone ? 58 : 44) * Math.PI / 180;
    const widthFit = usableW / bw;
    const heightFit = usableH / (bh * Math.sin(cam.pitch) + 12);
    // Phones are taller than the Gorge is: let the frame overflow sideways
    // a little rather than shrink to a strip (pinch and drag are there).
    cam.zoom = Math.max(0.5, 0.96 * (phone ? Math.min(heightFit, widthFit * 1.6) : Math.min(widthFit, heightFit)));
    cam.cx = (bboxKm.x0 + bboxKm.x1) / 2;
    cam.cy = (bboxKm.y0 + bboxKm.y1) / 2;
    cam.lift = (top + bottom) / 2 - H / 2 + (phone ? 0 : 10);
  }

  function resetCamera() {
    fitCamera();
    update();
  }

  // Rotate about the frame centre, then tilt. Returns screen x, y and depth
  // (rotated y): larger depth is farther from the viewer.
  function project(x, y, z) {
    const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw);
    const dx = x - cam.cx, dy = y - cam.cy;
    const xr = dx * cy - dy * sy;
    const yr = dx * sy + dy * cy;
    return [
      W / 2 + xr * cam.zoom,
      H / 2 + cam.lift - yr * cam.zoom * Math.sin(cam.pitch) - z * cam.zoom * Math.cos(cam.pitch),
      yr,
    ];
  }

  // Screen delta → ground delta, for panning.
  function unprojectDelta(dxPx, dyPx) {
    const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw);
    const xr = dxPx / cam.zoom;
    const yr = -dyPx / (cam.zoom * Math.sin(cam.pitch));
    return [xr * cy + yr * sy, -xr * sy + yr * cy];
  }

  // ── Colour ──────────────────────────────────────────────────────────────
  function tempRgb(t) {
    if (t == null || !isFinite(t)) return NO_TEMP;
    if (t <= TEMP_STOPS[0][0]) return TEMP_STOPS[0][1];
    for (let i = 1; i < TEMP_STOPS.length; i++) {
      const [t1, c1] = TEMP_STOPS[i];
      if (t <= t1) {
        const [t0, c0] = TEMP_STOPS[i - 1];
        const f = (t - t0) / (t1 - t0);
        return [0, 1, 2].map((k) => c0[k] + (c1[k] - c0[k]) * f);
      }
    }
    return TEMP_STOPS[TEMP_STOPS.length - 1][1];
  }

  const rgb = (c, k = 1) => `rgb(${(c[0] * k) | 0},${(c[1] * k) | 0},${(c[2] * k) | 0})`;
  const lighten = (c, f) => c.map((v) => v + (255 - v) * f);
  const desaturate = (c, f) => { const g = 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]; return c.map((v) => v + (g - v) * f); };

  function readTheme() {
    const s = getComputedStyle(document.documentElement);
    const v = (n) => s.getPropertyValue(n).trim();
    return {
      bg: v("--bg"), table: v("--table"), ink: v("--ink"), muted: v("--muted"),
      faint: v("--faint"), spark: v("--spark"), sparkFill: v("--spark-fill"), accent: v("--accent"),
    };
  }

  // ── Drawing ─────────────────────────────────────────────────────────────
  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * DPR; canvas.height = H * DPR;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    rainCanvas.width = W * DPR; rainCanvas.height = H * DPR;
    rctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    spark.width = spark.clientWidth * DPR; spark.height = spark.clientHeight * DPR;
    sctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }

  // Wall height. "vs normal" scales each river against its own long-term
  // mean, so a wall of NORMAL_KM means "about normal" on every river and the
  // Columbia no longer flattens the tributaries; "flow" is the day's
  // discharge on a square-root scale.
  function heightKm(cfs, r) {
    if (cfs == null) return 0;
    if (heightMode === "relative" && r && r.mean) return NORMAL_KM * Math.pow(Math.max(cfs, 0) / r.mean, NORMAL_POW);
    return HEIGHT_K * Math.sqrt(Math.max(cfs, 0));
  }

  function draw() {
    if (!rivers.length) return;
    prof.start = performance.now();
    hits = [];
    const cosP = Math.cos(cam.pitch);
    ctx.clearRect(0, 0, W, H);

    const api = { project, theme, bboxKm, cam, W, H, groundZ, dpr: DPR, doy: dayOfYearLeap(day) };
    const relief = terrain && terrain.ready;

    // Everything that only changes with the camera (table, relief, the
    // tributary network, towns and peaks) is drawn once into a layer and
    // blitted every frame; playback then only pays for the walls.
    const key = [cam.yaw, cam.pitch, cam.zoom, cam.cx, cam.cy, cam.lift, W, H, DPR, theme.bg, relief ? terrain.version : -1].join("|");
    if (key !== staticKey || !staticLayer) {
      if (!staticLayer) staticLayer = document.createElement("canvas");
      staticLayer.width = W * DPR; staticLayer.height = H * DPR;
      const g = staticLayer.getContext("2d");
      g.setTransform(DPR, 0, 0, DPR, 0, 0);
      const live = ctx;
      ctx = g;                                   // route the helpers into the layer
      ctx.fillStyle = theme.table;
      ctx.beginPath();
      for (const [x, y] of [[bboxKm.x0, bboxKm.y0], [bboxKm.x1, bboxKm.y0], [bboxKm.x1, bboxKm.y1], [bboxKm.x0, bboxKm.y1]]) {
        const [sx, sy] = project(x, y, 0);
        ctx.lineTo(sx, sy);
      }
      ctx.closePath();
      ctx.fill();
      if (relief) terrain.drawBase(ctx, api);
      drawNetwork();
      drawLandmarks();
      ctx = live;
      staticKey = [cam.yaw, cam.pitch, cam.zoom, cam.cx, cam.cy, cam.lift, W, H, DPR, theme.bg, relief ? terrain.version : -1].join("|");
    }
    ctx.drawImage(staticLayer, 0, 0, W, H);
    prof.static = performance.now();
    if (relief) terrain.drawSnow(ctx, api);
    prof.snow = performance.now();

    // Walls: every segment of every river, sorted far to near.
    const segs = [];
    const frac = playing && day < dayCount - 1 ? carry : 0;   // interpolate between days while playing
    for (const r of rivers) {
      let cfs = r.cfs[day];
      if (frac > 0 && cfs != null && r.cfs[day + 1] != null) cfs = cfs + (r.cfs[day + 1] - cfs) * frac;
      const h = heightKm(cfs, r);
      let tCol = r.temp[day];
      if (frac > 0 && tCol != null && r.temp[day + 1] != null) tCol = tCol + (r.temp[day + 1] - tCol) * frac;
      const col = r.tempEst[day] ? desaturate(tempRgb(tCol), 0.45) : tempRgb(tCol);
      const wPx = Math.max(r.widthKm * cam.zoom, MIN_WIDTH_PX);
      const wKm = wPx / cam.zoom / 2;
      for (const wall of r.walls) {
        const { pts, normals } = wall;
        for (let i = 0; i < pts.length - 1; i++) {
          const depth = (pts[i][1] + pts[i + 1][1]) / 2 * Math.cos(cam.yaw) + (pts[i][0] + pts[i + 1][0]) / 2 * Math.sin(cam.yaw);
          segs.push({ r, i, wall, h, col, wKm, depth, cfs });
        }
      }
    }
    segs.sort((a, b) => b.depth - a.depth);

    const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw);
    const lightX = -0.55, lightY = -0.83;   // light from the viewer's upper left

    // A soft cast shadow on the ground, thrown down and to the right, so the
    // walls sit on the table instead of floating.
    // Filled in short batches: one path of thousands of overlapping quads
    // is far slower to rasterise than many small ones.
    ctx.fillStyle = "rgba(0,0,0,0.16)";
    let inBatch = 0;
    ctx.beginPath();
    for (const s of segs) {
      if (s.cfs == null) continue;
      const { pts } = s.wall;
      const p0 = pts[s.i], p1 = pts[s.i + 1];
      const a = project(p0[0], p0[1], groundZ(p0[0], p0[1])), b = project(p1[0], p1[1], groundZ(p1[0], p1[1]));
      const off = s.h * cam.zoom * cosP;
      const dx = off * 0.45, dy = off * 0.3;
      ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.lineTo(b[0] + dx, b[1] + dy); ctx.lineTo(a[0] + dx, a[1] + dy); ctx.closePath();
      if (++inBatch >= 64) { ctx.fill(); ctx.beginPath(); inBatch = 0; }
    }
    if (inBatch) ctx.fill();

    prof.shadow = performance.now();
    // Shade at a vertex: its averaged normal against the light, in rotated space.
    const vertexShade = (n, flip) => {
      const nrx = n[0] * cy - n[1] * sy, nry = n[0] * sy + n[1] * cy;
      const fx = flip ? -nrx : nrx, fy = flip ? -nry : nry;
      return 0.78 + 0.22 * Math.max(0, Math.min(1, (fx * lightX + fy * lightY + 1) / 2));
    };

    for (const s of segs) {
      const { pts, normals } = s.wall;
      const p0 = pts[s.i], p1 = pts[s.i + 1], n0 = normals[s.i], n1 = normals[s.i + 1];
      const w = s.wKm;
      const L0 = [p0[0] + n0[0] * w, p0[1] + n0[1] * w], R0 = [p0[0] - n0[0] * w, p0[1] - n0[1] * w];
      const L1 = [p1[0] + n1[0] * w, p1[1] + n1[1] * w], R1 = [p1[0] - n1[0] * w, p1[1] - n1[1] * w];

      const z0 = groundZ(p0[0], p0[1]), z1 = groundZ(p1[0], p1[1]);
      if (s.cfs == null) {
        // No reading that day: a dashed footprint on the ground.
        const a = project(p0[0], p0[1], z0), b = project(p1[0], p1[1], z1);
        ctx.strokeStyle = rgb(NO_TEMP);
        ctx.lineWidth = 1.2;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
        ctx.setLineDash([]);
        continue;
      }

      // Which side faces the viewer? Rotate the segment normal.
      const nx = (n0[0] + n1[0]) / 2, ny = (n0[1] + n1[1]) / 2;
      const nrx = nx * cy - ny * sy, nry = nx * sy + ny * cy;
      const leftNear = nry < 0;                      // +n side is nearer
      const near = leftNear ? [L0, L1] : [R0, R1];
      const far = leftNear ? [R0, R1] : [L0, L1];
      const shade0 = vertexShade(n0, !leftNear), shade1 = vertexShade(n1, !leftNear);
      const highlight = s.r === hovered || (s.r === focused && performance.now() < focusUntil);
      const base = highlight ? lighten(s.col, 0.25) : s.col;

      // a is the p0 end, b the p1 end; each end stands on its own ground height.
      const quad = (a, b, za, zb) => [
        project(a[0], a[1], z0), project(b[0], b[1], z1), project(b[0], b[1], zb), project(a[0], a[1], za),
      ];
      const fill = (poly, style, edge) => {
        ctx.fillStyle = style;
        ctx.beginPath();
        ctx.moveTo(poly[0][0], poly[0][1]);
        for (let k = 1; k < poly.length; k++) ctx.lineTo(poly[k][0], poly[k][1]);
        ctx.closePath(); ctx.fill();
        if (edge) { ctx.strokeStyle = edge; ctx.lineWidth = 0.5; ctx.stroke(); }
      };
      // A face's colour runs from one end's shade to the other's, so the
      // wall reads as one surface rather than a row of tiles.
      const faceStyle = (poly, k0, k1) => {
        if (Math.abs(k0 - k1) < 0.04) return rgb(base, (k0 + k1) / 2);
        const g = ctx.createLinearGradient(poly[0][0], poly[0][1], poly[1][0], poly[1][1]);
        g.addColorStop(0, rgb(base, k0)); g.addColorStop(1, rgb(base, k1));
        return g;
      };

      const farFace = quad(far[0], far[1], z0 + s.h, z1 + s.h);
      fill(farFace, faceStyle(farFace, shade0 * 0.9, shade1 * 0.9));
      const nearFace = quad(near[0], near[1], z0 + s.h, z1 + s.h);
      fill(nearFace, faceStyle(nearFace, shade0, shade1));
      const capCol = lighten(base, 0.22);
      const cap = [project(L0[0], L0[1], z0 + s.h), project(L1[0], L1[1], z1 + s.h), project(R1[0], R1[1], z1 + s.h), project(R0[0], R0[1], z0 + s.h)];
      fill(cap, rgb(capCol), rgb(capCol));

      if (cosP > 0.05) hits.push({ poly: nearFace, r: s.r });
      hits.push({ poly: cap, r: s.r });
    }

    prof.walls = performance.now();
    drawRiverLabels();
    drawHusum();
    prof.end = performance.now();
  }
  const prof = {};

  // The faint tributary network, on the ground.
  function drawNetwork() {
    ctx.strokeStyle = theme.faint;
    ctx.lineWidth = 0.7;
    ctx.beginPath();
    for (const r of rivers) {
      for (const line of r.ground) {
        for (let i = 0; i < line.length; i++) {
          const [sx, sy] = project(line[i][0], line[i][1], groundZ(line[i][0], line[i][1]));
          if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
        }
      }
    }
    ctx.stroke();
  }

  function haloText(text, x, y, opts = {}) {
    ctx.font = opts.font || `500 11px ${cssVar("--mono")}`;
    ctx.textAlign = opts.align || "left";
    ctx.textBaseline = opts.baseline || "middle";
    ctx.lineWidth = 3.5;
    ctx.lineJoin = "round";
    ctx.strokeStyle = theme.bg;
    ctx.strokeText(text, x, y);
    ctx.fillStyle = opts.color || theme.ink;
    ctx.fillText(text, x, y);
  }

  let monoCache = null;
  function cssVar(name) {
    if (name === "--mono") {
      if (!monoCache) monoCache = getComputedStyle(document.documentElement).getPropertyValue("--mono").trim();
      return monoCache;
    }
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function drawLandmarks() {
    const small = isPhone();
    const relief = terrain && terrain.ready;
    for (const t of TOWNS) {
      const [x, y] = toKm(t.lon, t.lat);
      if (!inside(x, y)) continue;
      const [sx, sy] = project(x, y, groundZ(x, y));
      ctx.fillStyle = theme.muted;
      ctx.beginPath(); ctx.arc(sx, sy, 2, 0, Math.PI * 2); ctx.fill();
      if (t.big && !small) haloText(t.name, sx + 6, sy, { color: theme.muted, font: `400 10px ${cssVar("--mono")}` });
    }
    for (const p of PEAKS) {
      const [x, y] = toKm(p.lon, p.lat);
      if (!inside(x, y)) continue;
      const gz = groundZ(x, y);
      const [sx, sy] = project(x, y, gz);
      if (relief) {
        // The mountain itself is the marker; just name it, in the serif.
        haloText(p.name, sx, sy - 8, { align: "center", baseline: "bottom", color: theme.ink, font: `italic 500 12.5px ${cssVar("--serif")}` });
        continue;
      }
      const [, ty] = project(x, y, gz + 3.2);
      ctx.fillStyle = theme.muted;
      ctx.beginPath();
      ctx.moveTo(sx - 5, sy); ctx.lineTo(sx + 5, sy); ctx.lineTo(sx, ty); ctx.closePath(); ctx.fill();
      haloText(p.name, sx, ty - 4, { align: "center", baseline: "bottom", color: theme.muted, font: `400 10px ${cssVar("--mono")}` });
    }
  }

  function inside(x, y) {
    return x >= bboxKm.x0 && x <= bboxKm.x1 && y >= bboxKm.y0 && y <= bboxKm.y1;
  }

  // Rivers are named only when pointed at; the map speaks for itself.
  function drawRiverLabels() {
    for (const r of rivers) {
      const lit = r === hovered || (r === focused && performance.now() < focusUntil);
      if (!lit) continue;
      const h = heightKm(r.cfs[day], r) + groundZ(r.labelPt[0], r.labelPt[1]);
      const [sx, sy] = project(r.labelPt[0], r.labelPt[1], h);
      const v = r.cfs[day];
      const text = r === hovered || r === focused ? `${r.short} · ${fmtFlow(v)}` : r.short;
      haloText(text, sx, sy - 9, { align: "center", baseline: "bottom" });
    }
  }

  function drawHusum() {
    const [x, y] = toKm(HUSUM.lon, HUSUM.lat);
    const [sx, sy] = project(x, y, groundZ(x, y));
    const reading = husum[dayIso(day)];
    ctx.strokeStyle = theme.ink;
    ctx.fillStyle = theme.bg;
    ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.arc(sx, sy, 4, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    if (reading) haloText(`Husum ${reading.level.toFixed(1)} ft`, sx + 8, sy + 9, { font: `400 10.5px ${cssVar("--mono")}` });
  }

  // ── Timeline ────────────────────────────────────────────────────────────
  function buildMonths() {
    const el = document.getElementById("months");
    el.innerHTML = "";
    const start = new Date(flows.start + "T00:00:00Z");
    // Under about two years every month gets a label; beyond that, years at
    // January with unlabelled ticks at the quarters.
    const monthly = dayCount < 800;
    for (let i = 0; i < dayCount; i++) {
      const d = new Date(start.getTime() + i * 864e5);
      if (d.getUTCDate() !== 1) continue;
      const m = d.getUTCMonth();
      if (!monthly && m % 3 !== 0) continue;
      const span = document.createElement("span");
      const name = d.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" });
      if (monthly) span.textContent = m === 0 || i === 0 ? `${name} ${d.getUTCFullYear()}` : name;
      else if (m === 0) span.textContent = String(d.getUTCFullYear());
      else { span.textContent = "·"; span.className = "tick"; }
      span.style.left = `${(i / (dayCount - 1)) * 100}%`;
      el.appendChild(span);
    }
  }

  function drawSpark() {
    const w = spark.clientWidth, h = spark.clientHeight;
    sctx.clearRect(0, 0, w, h);
    const max = Math.max(...combined.filter((v) => v != null), 1);
    const xAt = (i) => (i / (dayCount - 1)) * w;
    const yAt = (v) => h - 2 - (v / max) * (h - 8);

    // The year that plays is lit; the rest of the record sits back.
    sctx.fillStyle = theme.sparkFill;
    sctx.globalAlpha = 0.5;
    sctx.fillRect(xAt(loopFrom), 0, xAt(dayCount - 1) - xAt(loopFrom), h);
    sctx.globalAlpha = 1;

    sctx.beginPath();
    let started = false;
    for (let i = 0; i < dayCount; i++) {
      const v = combined[i];
      if (v == null) continue;
      if (!started) { sctx.moveTo(xAt(i), h); sctx.lineTo(xAt(i), yAt(v)); started = true; }
      else sctx.lineTo(xAt(i), yAt(v));
    }
    sctx.lineTo(xAt(lastDayWithData()), h);
    sctx.closePath();
    sctx.fillStyle = theme.sparkFill;
    sctx.fill();

    sctx.beginPath();
    started = false;
    for (let i = 0; i < dayCount; i++) {
      const v = combined[i];
      if (v == null) { started = false; continue; }
      if (!started) { sctx.moveTo(xAt(i), yAt(v)); started = true; } else sctx.lineTo(xAt(i), yAt(v));
    }
    sctx.strokeStyle = theme.spark;
    sctx.lineWidth = 1.2;
    sctx.stroke();

    // Dim everything before the loop window.
    sctx.fillStyle = theme.bg;
    sctx.globalAlpha = 0.55;
    sctx.fillRect(0, 0, xAt(loopFrom), h);
    sctx.globalAlpha = 1;

    // The peak day, dashed.
    sctx.strokeStyle = "rgba(200,90,70,0.7)";
    sctx.lineWidth = 1;
    sctx.setLineDash([2, 3]);
    sctx.beginPath(); sctx.moveTo(xAt(peakDay), 4); sctx.lineTo(xAt(peakDay), h); sctx.stroke();
    sctx.setLineDash([]);

    // Needle.
    const x = xAt(day);
    sctx.strokeStyle = theme.ink;
    sctx.lineWidth = 1.5;
    sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, h); sctx.stroke();

    const label = document.getElementById("sparkLabel");
    const v = combined[day];
    label.innerHTML = v == null
      ? `<span class="m">no data · ${fmtDateShort(day)}</span>`
      : `<b>${fmtFlow(v, false)}</b> <span class="m">${unitLabel()} · ${fmtDateShort(day)}</span>`;
    const lo = (loopFrom / (dayCount - 1)) * 100;
    const pct = Math.max(Math.min(lo + 12, 80), Math.min(91, (day / (dayCount - 1)) * 100));
    label.style.left = `${pct}%`;
  }

  // ── Rain ────────────────────────────────────────────────────────────────
  // Daily rain and snow at a few places across the frame. Each candidate
  // drop has a fixed spot on the ground and inverse-distance weights to the
  // sample points, so the day's amounts turn into a density of streaks that
  // is heavy over Portland and the Cascades and sparse past The Dalles.
  const RAIN_DROPS = 1400, RAIN_FULL_MM = 30, RAIN_AIR_KM = 14;

  function prepareRain(data) {
    rain = data && data.points && data.precip ? data : null;
    drops = [];
    if (!rain) return;
    const pts = rain.points.map((p) => toKm(p.lon, p.lat));
    let seed = 7;
    const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };
    const count = isPhone() ? RAIN_DROPS / 2 : RAIN_DROPS;
    for (let k = 0; k < count; k++) {
      const x = bboxKm.x0 + rnd() * (bboxKm.x1 - bboxKm.x0);
      const y = bboxKm.y0 + rnd() * (bboxKm.y1 - bboxKm.y0);
      const w = pts.map(([px, py]) => 1 / (Math.pow(Math.hypot(px - x, py - y), 2) + 4));
      const sum = w.reduce((a, b) => a + b, 0);
      drops.push({ x, y, w: w.map((v) => v / sum), gate: rnd(), phase: rnd(), sway: rnd() * Math.PI * 2 });
    }
  }

  // Rain (mm) and snow (cm) at a drop's spot for the day.
  function rainAt(d, i) {
    const row = rain.precip[i], srow = rain.snow[i];
    let mm = 0, cm = 0;
    for (let k = 0; k < d.w.length; k++) {
      if (row && row[k] != null) mm += d.w[k] * row[k];
      if (srow && srow[k] != null) cm += d.w[k] * srow[k];
    }
    return [mm, cm];
  }

  // The day's rain at the nearest sample point to a river's gauge.
  function rainNearest(r) {
    if (!rain) return null;
    let best = 0, bd = Infinity;
    rain.points.forEach((p, k) => {
      const [px, py] = toKm(p.lon, p.lat);
      const d = Math.hypot(px - r.gauge[0], py - r.gauge[1]);
      if (d < bd) { bd = d; best = k; }
    });
    const row = rain.precip[day], srow = rain.snow[day];
    return { name: rain.points[best].name, mm: row ? row[best] : null, cm: srow ? srow[best] : null };
  }

  function fmtRain(mm) { return unit === "cfs" ? `${(mm / 25.4).toFixed(2)} in` : `${mm.toFixed(1)} mm`; }
  function fmtSnow(cm) { return unit === "cfs" ? `${(cm / 2.54).toFixed(1)} in` : `${cm.toFixed(0)} cm`; }

  function rainNote(r) {
    const n = rainNearest(r);
    if (!n || n.mm == null) return "";
    if (n.cm != null && n.cm >= 0.5) return ` · ${fmtSnow(n.cm)} snow`;
    return n.mm >= 0.3 ? ` · ${fmtRain(n.mm)} rain` : " · dry";
  }

  function rainEnabled() { return rainOn === "on"; }

  function updateRainStat() {
    const el = document.getElementById("rainStat");
    if (!rain) { el.hidden = true; document.getElementById("rainBtn").parentElement.hidden = true; return; }
    document.getElementById("rainBtn").classList.toggle("on", rainEnabled());
    document.getElementById("rainBtn").setAttribute("aria-pressed", String(rainEnabled()));
    if (!rainEnabled()) { rainLoop = false; rctx.clearRect(0, 0, W, H); }
    const k = rain.points.findIndex((p) => p.name === "Hood River");
    const row = rain.precip[day], srow = rain.snow[day];
    const mm = row ? row[k] : null, cm = srow ? srow[k] : null;
    el.hidden = mm == null;
    if (mm == null) return;
    const snowing = cm != null && cm >= 0.5;
    document.getElementById("rainLabel").textContent = snowing ? "SNOW AT HOOD RIVER ·" : "RAIN AT HOOD RIVER ·";
    document.getElementById("rainValue").textContent = snowing ? fmtSnow(cm) : mm >= 0.3 ? fmtRain(mm) : "dry";
    if (rainEnabled() && !rainLoop) { rainLoop = true; rainLast = performance.now(); requestAnimationFrame(rainFrame); }
  }

  document.getElementById("rainBtn").addEventListener("click", () => {
    rainOn = rainEnabled() ? "off" : "on";
    store("pulse:rain", rainOn);
    update();
  });

  // Streaks fall from RAIN_AIR_KM above the ground, slanting east with the
  // weather; flakes drift down slowly and sway. Runs on its own frame loop
  // and stops itself when the day is dry everywhere.
  function rainFrame(now) {
    if (!rainLoop) return;
    const dt = Math.min((now - rainLast) / 1000, 0.1);
    rainLast = now;
    rctx.clearRect(0, 0, W, H);
    const dark = theme.bg && luminanceOf(theme.bg) < 0.45;
    let any = false;
    const rainStyle = dark ? "rgba(190,214,236," : "rgba(40,62,92,";
    const snowStyle = dark ? "rgba(236,242,248," : "rgba(255,255,255,";
    const cosP = Math.cos(cam.pitch);
    rctx.lineWidth = 1;
    for (const d of drops) {
      const [mm, cm] = rainAt(d, day);
      const snowing = cm >= 0.5;
      const amount = snowing ? cm * 6 : mm;
      const density = Math.pow(Math.min(amount / RAIN_FULL_MM, 1), 0.7);
      if (d.gate >= density) continue;
      any = true;
      d.phase += dt * (snowing ? 0.22 : 0.9);
      if (d.phase >= 1) d.phase -= 1;
      const p = d.phase;
      const z0 = groundZ(d.x, d.y);
      if (snowing) {
        const sway = Math.sin(now / 900 + d.sway) * 0.35;
        const [sx, sy] = project(d.x + sway, d.y, z0 + (1 - p) * RAIN_AIR_KM);
        rctx.fillStyle = snowStyle + (0.75 * Math.min(1, (1 - p) * 4 + 0.2)) + ")";
        rctx.beginPath(); rctx.arc(sx, sy, 1.3, 0, Math.PI * 2); rctx.fill();
      } else {
        const wind = p * 1.6;                                   // km of eastward slant over the fall
        const [sx, sy] = project(d.x + wind, d.y, z0 + (1 - p) * RAIN_AIR_KM);
        const len = 6 + 10 * density;
        rctx.strokeStyle = rainStyle + (0.32 * Math.min(1, (1 - p) * 3 + 0.15)) + ")";
        rctx.beginPath(); rctx.moveTo(sx, sy); rctx.lineTo(sx - len * 0.12, sy - len * cosP - len * 0.4); rctx.stroke();
      }
    }
    if (any) requestAnimationFrame(rainFrame); else { rainLoop = false; }
  }

  let lumProbe = null;
  function luminanceOf(color) {
    if (!lumProbe) lumProbe = document.createElement("canvas").getContext("2d");
    lumProbe.fillStyle = color;
    const v = lumProbe.fillStyle;
    const c = v[0] === "#" ? [parseInt(v.slice(1, 3), 16), parseInt(v.slice(3, 5), 16), parseInt(v.slice(5, 7), 16)] : (v.match(/[\d.]+/g) || [128, 128, 128]).map(Number);
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
  }

  // ── Formatting ──────────────────────────────────────────────────────────
  function unitLabel() { return unit === "cfs" ? "ft³/s" : "m³/s"; }
  function tempNote(r, i) {
    const e = r.tempEst[i];
    if (!e) return "";
    return e === "norm" ? " (typical for the date)" : ` (no gauge; follows the ${e})`;
  }
  function fmtTemp(t) { return unit === "cfs" ? `${(t * 1.8 + 32).toFixed(0)} °F` : `${t.toFixed(1)} °C`; }

  function fmtFlow(cfs, withUnit = true) {
    if (cfs == null) return "no data";
    let v = unit === "cfs" ? cfs : cfs * CFS_TO_CMS;
    let text;
    if (v >= 10000) text = (Math.round(v / 100) * 100).toLocaleString("en-US");
    else if (v >= 1000) text = (Math.round(v / 10) * 10).toLocaleString("en-US");
    else if (v >= 10) text = Math.round(v).toLocaleString("en-US");
    else text = v.toFixed(1);
    return withUnit ? `${text} ${unitLabel()}` : text;
  }

  function dayIso(i) {
    return new Date(new Date(flows.start + "T00:00:00Z").getTime() + i * 864e5).toISOString().slice(0, 10);
  }
  function fmtDateLong(i) {
    return new Date(dayIso(i) + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  }
  function fmtDateShort(i) {
    return new Date(dayIso(i) + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  }
  function dayOfYearLeap(i) {
    const d = new Date(dayIso(i) + "T00:00:00Z");
    return Math.round((Date.UTC(2024, d.getUTCMonth(), d.getUTCDate()) - Date.UTC(2024, 0, 1)) / 864e5);
  }
  function wordNumber(n) {
    return ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"][n] || String(n);
  }

  function dayFromHash() {
    const m = location.hash.match(/d=(\d{4}-\d{2}-\d{2})/);
    if (!m || !flows) return null;
    const i = Math.round((new Date(m[1] + "T00:00:00Z") - new Date(flows.start + "T00:00:00Z")) / 864e5);
    return i >= 0 && i < dayCount ? i : null;
  }

  // ── Update ──────────────────────────────────────────────────────────────
  function update() {
    if (!flows) return;
    page.dataset.day = day;
    document.getElementById("dateLabel").textContent = fmtDateLong(day);

    const v = combined[day];
    const max = Math.max(...combined.filter((x) => x != null), 1);
    document.getElementById("tribValue").textContent = fmtFlow(v, false);
    document.getElementById("tribUnit").textContent = unitLabel();
    document.querySelector("#tribBar i").style.width = v == null ? "0%" : `${(v / max) * 100}%`;

    const c = columbia[day];
    const cmax = Math.max(...columbia.filter((x) => x != null), 1);
    document.getElementById("colValue").textContent = fmtFlow(c, false);
    document.getElementById("colUnit").textContent = unitLabel();
    document.querySelector("#colBar i").style.width = c == null ? "0%" : `${(c / cmax) * 100}%`;

    document.getElementById("legendLo").textContent = fmtTemp(TEMP_LO);
    document.getElementById("legendHi").textContent = fmtTemp(TEMP_HI);
    const temps = rivers.filter((r) => r.counted && r.temp[day] != null).map((r) => r.temp[day]).sort((a, b) => a - b);
    if (temps.length) document.querySelector("#tribBar i").style.background = rgb(tempRgb(temps[Math.floor(temps.length / 2)]));
    const ct = rivers.find((r) => r.kind === "spine");
    if (ct && ct.temp[day] != null) document.querySelector("#colBar i").style.background = rgb(tempRgb(ct.temp[day]));
    document.getElementById("peakBtn").hidden = day === peakDay;
    spark.setAttribute("aria-valuenow", String(day));
    spark.setAttribute("aria-valuetext", fmtDateShort(day));

    const peak = combined[peakDay], floor = combined[floorDay];
    document.getElementById("ratioValue").textContent = peak && floor ? `${(peak / floor).toFixed(1)}×` : "—";
    document.getElementById("ratioNote").textContent =
      `peak day to floor day, ${fmtDateShort(peakDay)} vs ${fmtDateShort(floorDay)}`;
    document.getElementById("peakBtn").textContent = `peak day: ${fmtDateShort(peakDay)} →`;

    const hs = document.getElementById("husumStat");
    const reading = husum[dayIso(day)];
    if (Object.keys(husum).length) {
      hs.hidden = false;
      document.getElementById("husumValue").textContent = reading ? `${reading.level.toFixed(1)} ft` : "—";
      document.getElementById("husumNote").textContent = reading
        ? (reading.low != null && reading.high != null && reading.high - reading.low >= 0.15
          ? `trail cam · ${reading.low.toFixed(1)}–${reading.high.toFixed(1)} ft across photos`
          : "trail cam")
        : "no reading that day";
    }

    updateRainStat();
    document.getElementById("playBtn").textContent = playing ? "❚❚" : "▶";
    document.getElementById("playBtn").setAttribute("aria-label", playing ? "Pause" : "Play");
    page.dataset.playing = playing;

    updateNames();
    freeBand();
    drawSpark();
    draw();
  }

  function setDay(i, fromPlayback = false) {
    day = Math.max(0, Math.min(dayCount - 1, Math.round(i)));
    if (!fromPlayback) history.replaceState(null, "", `#d=${dayIso(day)}`);
    update();
  }

  // ── Playback ────────────────────────────────────────────────────────────
  function tick(now) {
    if (!playing) return;
    const dt = Math.min((now - lastTick) / 1000, 0.25);
    lastTick = now;
    carry += dt * DAYS_PER_SECOND * SPEEDS[speedIx];
    const steps = Math.floor(carry);
    if (steps > 0) {
      carry -= steps;
      let next = day + steps;
      if (next > dayCount - 1) {
        if (loop) next = loopFrom; else { next = dayCount - 1; playing = false; }
      }
      setDay(next, true);
    } else {
      draw();     // sub-day frame: walls interpolate toward tomorrow
    }
    if (playing) requestAnimationFrame(tick);
  }

  function togglePlay() {
    playing = !playing;
    if (playing) {
      if (day >= dayCount - 1) day = loopFrom;
      lastTick = performance.now(); carry = 0;
      requestAnimationFrame(tick);
    } else {
      history.replaceState(null, "", `#d=${dayIso(day)}`);
    }
    update();
  }

  // ── Names panel ─────────────────────────────────────────────────────────
  function buildNames() {
    const panel = document.getElementById("namesPanel");
    panel.innerHTML = "";
    const sorted = [...rivers].sort((a, b) => (b.mean || 0) - (a.mean || 0));
    for (const r of sorted) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.ix = r.ix;
      b.innerHTML = `<i></i><span>${r.name}</span><span class="v"></span>`;
      b.addEventListener("click", () => focusRiver(r));
      b.addEventListener("mouseenter", () => { hovered = r; draw(); });
      b.addEventListener("mouseleave", () => { hovered = null; draw(); });
      panel.appendChild(b);
    }
  }

  function updateNames() {
    const panel = document.getElementById("namesPanel");
    if (panel.hidden) return;
    for (const b of panel.children) {
      const r = rivers[+b.dataset.ix];
      const est = r.tempEst[day];
      b.querySelector("i").style.background = r.temp[day] == null ? rgb(NO_TEMP) : rgb(est ? desaturate(tempRgb(r.temp[day]), 0.45) : tempRgb(r.temp[day]));
      b.querySelector("i").style.opacity = est ? 0.6 : 1;
      b.querySelector(".v").textContent = fmtFlow(r.cfs[day], false);
      b.classList.toggle("on", r === focused && performance.now() < focusUntil);
    }
  }

  // Clicking a name lights that river until it is clicked again (or Escape).
  function focusRiver(r) {
    if (focused === r && focusUntil === Infinity) { focused = null; focusUntil = 0; update(); return; }
    focused = r;
    focusUntil = Infinity;
    cam.cx = r.gauge[0]; cam.cy = r.gauge[1];
    update();
  }

  // ── Hover ───────────────────────────────────────────────────────────────
  function pointInPoly(x, y, poly) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [xi, yi] = poly[i], [xj, yj] = poly[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  function riverAt(x, y) {
    for (let i = hits.length - 1; i >= 0; i--) {
      const p = hits[i].poly;
      let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity;
      for (const [px, py] of p) { if (px < minx) minx = px; if (px > maxx) maxx = px; if (py < miny) miny = py; if (py > maxy) maxy = py; }
      if (x < minx - 2 || x > maxx + 2 || y < miny - 2 || y > maxy + 2) continue;
      if (pointInPoly(x, y, p)) return hits[i].r;
    }
    return null;
  }

  function showTip(r, x, y) {
    if (!r) { tip.hidden = true; return; }
    const v = r.cfs[day], t = r.temp[day];
    const ratio = v != null && r.mean ? v / r.mean : null;
    const vsMean = ratio == null ? "" : ratio >= 1 ? `${ratio.toFixed(1)}× its mean` : `${Math.round(ratio * 100)}% of its mean`;
    tip.innerHTML =
      `<b>${r.name}</b>` +
      `<span class="n">${fmtFlow(v, false)}</span> <span class="m">${v == null ? "" : unitLabel()}</span><br>` +
      `<span class="m">${vsMean}${t != null ? ` · ${r.tempEst[day] ? "~" : ""}${fmtTemp(t)}${tempNote(r, day)}` : " · no temperature gauge"}</span><br>` +
      `<span class="m">mean ${fmtFlow(r.mean)}${rainNote(r)}</span>`;
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = `${Math.min(x + 14, W - tw - 8)}px`;
    tip.style.top = `${Math.max(8, Math.min(y + 14, H - th - 8))}px`;
  }

  // ── Input ───────────────────────────────────────────────────────────────
  const pointers = new Map();
  let drag = null, pinch = null;

  canvas.addEventListener("pointerdown", (e) => {
    try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 1) {
      drag = { x: e.clientX, y: e.clientY, moved: false, pan: e.shiftKey || e.button === 1 || e.button === 2 };
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y), mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, zoom: cam.zoom };
      drag = null;
    }
    tip.hidden = true;
  });

  canvas.addEventListener("pointermove", (e) => {
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      cam.zoom = Math.max(0.5, Math.min(60, pinch.zoom * (dist / pinch.dist)));
      const [dx, dy] = unprojectDelta(mx - pinch.mx, my - pinch.my);
      cam.cx -= dx; cam.cy -= dy;
      pinch.mx = mx; pinch.my = my;
      draw();
      return;
    }

    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
      if (drag.pan) {
        const [gx, gy] = unprojectDelta(dx, dy);
        cam.cx -= gx; cam.cy -= gy;
      } else {
        cam.yaw -= dx * 0.005;
        cam.pitch = Math.max(18 * Math.PI / 180, Math.min(89 * Math.PI / 180, cam.pitch + dy * 0.004));
      }
      drag.x = e.clientX; drag.y = e.clientY;
      page.dataset.dragging = "true";
      draw();
      return;
    }

    if (e.pointerType === "mouse") {
      const rect = canvas.getBoundingClientRect();
      const r = riverAt(e.clientX - rect.left, e.clientY - rect.top);
      if (r !== hovered) { hovered = r; draw(); }
      showTip(r, e.clientX - rect.left, e.clientY - rect.top);
    }
  });

  function endPointer(e) {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 0) {
      if (drag && !drag.moved && e.pointerType !== "mouse") {
        const rect = canvas.getBoundingClientRect();
        const r = riverAt(e.clientX - rect.left, e.clientY - rect.top);
        hovered = r; draw();
        showTip(r, e.clientX - rect.left, e.clientY - rect.top);
      }
      drag = null;
      page.dataset.dragging = "false";
    }
  }
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);
  canvas.addEventListener("pointerleave", () => { if (!drag) { hovered = null; tip.hidden = true; draw(); } });
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("dblclick", resetCamera);

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const f = Math.exp(-e.deltaY * 0.0015);
    zoomAt(f, e.clientX, e.clientY);
  }, { passive: false });

  function zoomAt(f, px, py) {
    const rect = canvas.getBoundingClientRect();
    const sx = px == null ? W / 2 : px - rect.left, sy = py == null ? H / 2 + cam.lift : py - rect.top;
    // Keep the ground point under the cursor fixed.
    const [gx0, gy0] = unprojectDelta(sx - W / 2, sy - (H / 2 + cam.lift));
    cam.zoom = Math.max(0.5, Math.min(60, cam.zoom * f));
    const [gx1, gy1] = unprojectDelta(sx - W / 2, sy - (H / 2 + cam.lift));
    cam.cx += gx0 - gx1; cam.cy += gy0 - gy1;
    draw();
  }

  document.querySelectorAll("[data-cam]").forEach((b) => b.addEventListener("click", () => {
    const step = 60 / cam.zoom;
    switch (b.dataset.cam) {
      case "zoomIn": zoomAt(1.35); break;
      case "zoomOut": zoomAt(1 / 1.35); break;
      case "turnLeft": cam.yaw += Math.PI / 12; break;
      case "turnRight": cam.yaw -= Math.PI / 12; break;
      case "tiltUp": cam.pitch = Math.min(89 * Math.PI / 180, cam.pitch + 0.12); break;
      case "tiltDown": cam.pitch = Math.max(18 * Math.PI / 180, cam.pitch - 0.12); break;
      case "panUp": { const [x, y] = unprojectDelta(0, -step); cam.cx += x; cam.cy += y; break; }
      case "panDown": { const [x, y] = unprojectDelta(0, step); cam.cx += x; cam.cy += y; break; }
      case "panLeft": { const [x, y] = unprojectDelta(-step, 0); cam.cx += x; cam.cy += y; break; }
      case "panRight": { const [x, y] = unprojectDelta(step, 0); cam.cx += x; cam.cy += y; break; }
      case "reset": resetCamera(); return;
    }
    draw();
  }));

  document.getElementById("playBtn").addEventListener("click", togglePlay);
  document.getElementById("loopBtn").addEventListener("click", (e) => {
    loop = !loop;
    e.currentTarget.setAttribute("aria-pressed", String(loop));
  });
  document.getElementById("speedBtn").addEventListener("click", (e) => {
    speedIx = (speedIx + 1) % SPEEDS.length;
    e.currentTarget.textContent = SPEED_LABELS[speedIx];
    e.currentTarget.title = `Playback speed: ${DAYS_PER_SECOND * SPEEDS[speedIx]} days per second`;
  });
  document.getElementById("peakBtn").addEventListener("click", () => { playing = false; setDay(peakDay); });

  document.querySelectorAll("[data-unit]").forEach((b) => {
    b.classList.toggle("on", b.dataset.unit === unit);
    b.addEventListener("click", () => {
      unit = b.dataset.unit;
      store("pulse:units", unit);
      document.querySelectorAll("[data-unit]").forEach((x) => x.classList.toggle("on", x === b));
      update();
    });
  });

  document.querySelectorAll("[data-height]").forEach((b) => {
    b.classList.toggle("on", b.dataset.height === heightMode);
    b.addEventListener("click", () => {
      heightMode = b.dataset.height;
      store("pulse:height", heightMode);
      document.querySelectorAll("[data-height]").forEach((x) => x.classList.toggle("on", x === b));
      applyHeightCopy();
      update();
    });
  });

  function applyHeightCopy() {
    const lede = document.querySelector(".rp-lede");
    if (!lede) return;
    lede.textContent = heightMode === "relative"
      ? "Height: the day's flow against the river's norm. Width: long-term mean. Colour: water temperature."
      : "Height: the day's flow. Width: long-term mean. Colour: water temperature.";
  }
  applyHeightCopy();

  document.getElementById("namesBtn").addEventListener("click", (e) => {
    const panel = document.getElementById("namesPanel");
    panel.hidden = !panel.hidden;
    e.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
    updateNames();
  });

  // Scrub the timeline.
  let scrubbing = false;
  function scrubTo(clientX) {
    const rect = spark.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    setDay(f * (dayCount - 1));
  }
  spark.addEventListener("pointerdown", (e) => {
    scrubbing = true; playing = false;
    try { spark.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
    scrubTo(e.clientX);
  });
  spark.addEventListener("pointermove", (e) => {
    if (scrubbing) { scrubTo(e.clientX); return; }
    // Hovering previews that day in the read-out without moving the needle.
    const rect = spark.getBoundingClientRect();
    const i = Math.round(Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * (dayCount - 1));
    const v = combined[i];
    document.getElementById("sparkLabel").innerHTML = v == null
      ? `<span class="m">no data · ${fmtDateShort(i)}</span>`
      : `<b>${fmtFlow(v, false)}</b> <span class="m">${unitLabel()} · ${fmtDateShort(i)}</span>`;
  });
  spark.addEventListener("pointerleave", () => { if (!scrubbing) drawSpark(); });
  spark.addEventListener("pointerup", () => { scrubbing = false; });
  spark.addEventListener("pointercancel", () => { scrubbing = false; });

  document.addEventListener("keydown", (e) => {
    if (!(e.target instanceof Element) || e.target.matches("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;
    switch (e.key) {
      case " ": e.preventDefault(); togglePlay(); break;
      case "ArrowRight": e.preventDefault(); playing = false; setDay(day + (e.shiftKey ? 7 : 1)); break;
      case "ArrowLeft": e.preventDefault(); playing = false; setDay(day - (e.shiftKey ? 7 : 1)); break;
      case "Home": e.preventDefault(); playing = false; setDay(0); break;
      case "End": e.preventDefault(); playing = false; setDay(dayCount - 1); break;
      case "+": case "=": zoomAt(1.35); break;
      case "-": case "_": zoomAt(1 / 1.35); break;
      case "r": case "R": resetCamera(); break;
      case "l": case "L": loop = !loop; document.getElementById("loopBtn").setAttribute("aria-pressed", String(loop)); break;
      case "Escape": {
        focused = null; focusUntil = 0;
        const panel = document.getElementById("namesPanel");
        panel.hidden = true; document.getElementById("namesBtn").setAttribute("aria-expanded", "false");
        update(); break;
      }
    }
  });

  // The chrome recedes when the pointer rests, so the map carries the page.
  let idleTimer = 0;
  function wake() {
    page.dataset.idle = "false";
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (!document.querySelector(".rp-head:hover, .rp-cam:hover, .rp-foot:hover")) page.dataset.idle = "true"; }, 4000);
  }
  ["pointermove", "pointerdown", "keydown", "wheel", "touchstart"].forEach((ev) => window.addEventListener(ev, wake, { passive: true }));
  wake();

  window.addEventListener("resize", () => { resize(); if (rivers.length) { fitCamera(); update(); } });
  // The relief module loads its grid on its own; redraw once it is there.
  window.addEventListener("pulse-terrain-ready", () => { if (rivers.length) update(); });
  window.addEventListener("hashchange", () => { const d = dayFromHash(); if (d != null && d !== day) setDay(d); });
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", () => { theme = readTheme(); update(); });

  // For checks from the console: what each river shows on the current day.
  window.pulseDebug = () => rivers.map((r) => ({
    river: r.short, cfs: r.cfs[day], temp: r.temp[day], source: r.tempEst[day] || "gauge",
  }));
  window.pulseProfile = () => ({ static: prof.static - prof.start, snow: prof.snow - prof.static, shadow: prof.shadow - prof.snow, walls: prof.walls - prof.shadow, labels: prof.end - prof.walls, segs: hits.length / 2 });
  window.pulseDrawMs = (n = 10) => {
    const t0 = performance.now();
    for (let i = 0; i < n; i++) draw();
    return (performance.now() - t0) / n;
  };

  resize();
  load();
})();
