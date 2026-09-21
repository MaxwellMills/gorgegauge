// Relief for /pulse.html — the hillshaded ground the river walls stand on.
//
// Loaded before pulse.js, which talks to it through window.PulseTerrain:
//
//   ready          false until /pulse/terrain.json has loaded and init() ran
//   init(opts)     called once by pulse.js after the rivers load; converts the
//                  lon/lat grid into table kilometres
//   zAt(x, y)      ground height (km of table, exaggerated) under a point
//   draw(ctx, api) paints the relief under everything else
//
// The grid is a regular lon/lat lattice of elevations in metres (row 0 =
// north). Because pulse.js's toKm is linear in lon and lat, the lattice is
// also a regular grid in table kilometres, so lookups are a plain bilinear
// interpolation.
//
// Rendering: the visible part of the frame is meshed into cells of a size
// chosen from the zoom (about 2 km at the default desktop view), each cell is
// projected through pulse.js's own project() and filled with a shade from its
// slope against a light at the viewer's upper left — the same light the walls
// use. The result is cached in an offscreen canvas keyed on the camera, size
// and theme, so playback (which redraws every frame) only blits it. While the
// camera is being dragged a coarser mesh is used, and a fine one is rebuilt
// once the camera settles.

(function () {
  "use strict";

  const URL = "/pulse/terrain.json";

  // Metres → km of table. 3.2× puts Mt Adams (about 3,380 m as a cell mean,
  // ~2,950 m after smoothing) at ~9.5 km, a little taller than a river wall
  // at normal flow (7 km).
  const EXAGGERATION = 3.2;

  // Snow. The snowline follows the season: down near 900 m in March, up
  // around 2,600 m by early September, blending over a few hundred metres.
  // Snow is drawn as its own cached layer over the relief, keyed to the
  // snowline in 50 m steps so it moves through the year without rebuilding
  // the whole mesh every day.
  const SNOW_LOW_M = 1450, SNOW_HIGH_M = 2650, SNOW_PEAK_DOY = 245, SNOW_EDGE_M = 260;
  const SNOW_LIT = [240, 244, 248], SNOW_SHADE = [140, 158, 182];

  // Contour lines on the high ground give the peaks a drawn, topographic
  // feel: every 200 m from 1,400 m up.
  const CONTOUR_FROM_M = 1400, CONTOUR_STEP_M = 200;

  // Valley shading: ground that sits below its surroundings darkens a
  // little, so the drainages read as carved.
  const AO_RADIUS_PASSES = 6;    // 3×3 blurs, ~6 km at 0.5 km cells

  // Light from the viewer's upper left, in rotated ground space (x right,
  // y away from the viewer, z up): matches pulse.js's wall light (-0.55, -0.83)
  // with some elevation so flat ground sits between lit and shaded slopes.
  const LIGHT = normalize([-0.55, -0.83, 0.85]);
  const FLAT_DOT = LIGHT[2];

  // Mesh sizing: the on-screen size of a cell we aim for, and the budget.
  const TARGET_PX_DESKTOP = 2.4;   // ~0.5 km cells at the default desktop view (~130k quads)
  const TARGET_PX_PHONE = 3;       // the phone map is small: ~2 km cells
  const MAX_QUADS = 160000;
  const MIN_CELL_KM = 0.3;         // finer than this just interpolates the data
  const COARSE_FACTOR = 1.8;       // while the camera is moving
  const SETTLE_MS = 180;           // how long the camera must be still before refining
  const SMOOTH_PASSES = 1;         // 3×3 binomial passes over the height grid

  let raw = null;      // terrain.json as loaded
  let grid = null;     // prepared: { cols, rows, z: Float32Array, xc0, ycN, dx, dy, maxZ }
  let bbox = null;     // bboxKm from pulse.js
  let initOpts = null;

  // Offscreen cache.
  let cache = null, cacheKey = "", cacheCoarse = false;
  let mesh = null;                                   // the fine mesh behind the cache, for the snow layer
  let snowCache = null, snowKey = "";
  let lastBuild = 0, refineTimer = 0, lastApi = null;

  const T = {
    ready: false,
    init,
    zAt,
    draw,          // base relief + snow
    drawBase,      // relief only (the caller may cache it with other static layers)
    drawSnow,      // the season's snow, cached separately
    version: 0,    // bumps whenever the base mesh is rebuilt
    EXAGGERATION,
  };
  window.PulseTerrain = T;

  // ── Loading ─────────────────────────────────────────────────────────────
  // terrain.json describes the grid; the metres live in terrain.png packed as
  // R*256 + G, read back through a canvas.
  fetch(URL)
    .then((r) => { if (!r.ok) throw new Error(`${URL}: HTTP ${r.status}`); return r.json(); })
    .then((json) => new Promise((resolve, reject) => {
      if (json.elev) { resolve(json); return; }        // old inline form
      const img = new Image();
      img.onload = () => {
        const c = document.createElement("canvas");
        c.width = json.cols; c.height = json.rows;
        const g = c.getContext("2d", { willReadFrequently: true });
        g.drawImage(img, 0, 0);
        const px = g.getImageData(0, 0, json.cols, json.rows).data;
        const elev = new Int32Array(json.cols * json.rows);
        for (let i = 0; i < elev.length; i++) elev[i] = px[i * 4] * 256 + px[i * 4 + 1];
        resolve({ ...json, elev });
      };
      img.onerror = () => reject(new Error("terrain.png failed to load"));
      img.src = "/pulse/" + json.png;
    }))
    .then((data) => { raw = data; prepare(); })
    .catch((e) => console.warn("pulse-terrain: no relief —", e.message));

  function init(opts) {
    initOpts = opts;
    bbox = opts.bboxKm;
    prepare();
  }

  // Turn the lon/lat grid into a km grid once both the data and init() are in.
  function prepare() {
    if (!raw || !initOpts || T.ready) return;
    const [w, s, e, n] = raw.bbox;
    const cols = raw.cols, rows = raw.rows, elev = raw.elev;
    const dlon = (e - w) / cols, dlat = (n - s) / rows;

    // Cell centres: column 0 is half a cell in from the west edge, row 0
    // half a cell down from the north edge.
    const [xc0, ycN] = initOpts.toKm(w + dlon / 2, n - dlat / 2);
    const [xc1, ycS] = initOpts.toKm(e - dlon / 2, s + dlat / 2);
    const dx = (xc1 - xc0) / (cols - 1), dy = (ycN - ycS) / (rows - 1);

    // Base level: the grid's lowest point (the Columbia near Portland), so
    // the river-level ground sits at z ≈ 0 and the walls stand on it.
    let base = Infinity;
    for (let i = 0; i < elev.length; i++) if (elev[i] < base) base = elev[i];
    base = Math.max(base, 0);

    let z = new Float32Array(elev.length);
    for (let i = 0; i < elev.length; i++) z[i] = Math.max(elev[i] - base, 0) / 1000 * EXAGGERATION;

    // Smooth the field a little (a 3×3 binomial kernel, about 3 km): the
    // cells are ~1.4 km, and at this exaggeration every one of them is its
    // own steep facet, which shades as static. Smoothing keeps the landforms
    // — the volcanoes, the Gorge, the ridges — and loses the noise. Peaks
    // come down a few percent, which the exaggeration allows for.
    for (let pass = 0; pass < SMOOTH_PASSES; pass++) z = blur3(z, cols, rows);

    let maxZ = 0;
    for (let i = 0; i < z.length; i++) if (z[i] > maxZ) maxZ = z[i];
    let zb = z;
    for (let pass = 0; pass < AO_RADIUS_PASSES; pass++) zb = blur3(zb, cols, rows);
    grid = { cols, rows, z, zb, xc0, ycN, dx, dy, maxZ };
    T.maxZ = maxZ;
    T.ready = true;
    window.dispatchEvent(new Event("pulse-terrain-ready"));
  }

  // Separable [1 2 1] blur with clamped edges.
  function blur3(src, cols, rows) {
    const tmp = new Float32Array(src.length), out = new Float32Array(src.length);
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const k = r * cols + c;
        const l = c > 0 ? src[k - 1] : src[k], rr = c < cols - 1 ? src[k + 1] : src[k];
        tmp[k] = (l + 2 * src[k] + rr) / 4;
      }
    }
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const k = r * cols + c;
        const u = r > 0 ? tmp[k - cols] : tmp[k], d = r < rows - 1 ? tmp[k + cols] : tmp[k];
        out[k] = (u + 2 * tmp[k] + d) / 4;
      }
    }
    return out;
  }

  // ── Heights ─────────────────────────────────────────────────────────────
  // Bilinear interpolation over the cell centres; clamped to the grid inside
  // the frame, 0 outside it.
  function zAt(x, y) { return sampleGrid(grid ? grid.z : null, x, y); }
  function zBlurAt(x, y) { return sampleGrid(grid ? grid.zb : null, x, y); }
  function sampleGrid(field, x, y) {
    if (!field) return 0;
    return zAtField(field, x, y);
  }
  function zAtField(field, x, y) {
    if (!grid) return 0;
    if (x < bbox.x0 || x > bbox.x1 || y < bbox.y0 || y > bbox.y1) return 0;
    const { cols, rows, z } = grid;
    let fc = (x - grid.xc0) / grid.dx;
    let fr = (grid.ycN - y) / grid.dy;
    if (fc < 0) fc = 0; else if (fc > cols - 1) fc = cols - 1;
    if (fr < 0) fr = 0; else if (fr > rows - 1) fr = rows - 1;
    const c0 = Math.floor(fc), r0 = Math.floor(fr);
    const c1 = c0 < cols - 1 ? c0 + 1 : c0, r1 = r0 < rows - 1 ? r0 + 1 : r0;
    const tc = fc - c0, tr = fr - r0;
    const top = field[r0 * cols + c0] * (1 - tc) + field[r0 * cols + c1] * tc;
    const bot = field[r1 * cols + c0] * (1 - tc) + field[r1 * cols + c1] * tc;
    return top * (1 - tr) + bot * tr;
  }

  // ── Drawing ─────────────────────────────────────────────────────────────
  function draw(ctx, api) {
    drawBase(ctx, api);
    drawSnow(ctx, api);
  }

  function drawBase(ctx, api) {
    if (!T.ready) return;
    const { cam, W, H, dpr, theme } = api;
    const key = [cam.yaw, cam.pitch, cam.zoom, cam.cx, cam.cy, cam.lift, W, H, dpr, theme.table, theme.bg].join("|");
    lastApi = api;

    if (key !== cacheKey || !cache) {
      const now = performance.now();
      const moving = now - lastBuild < SETTLE_MS;      // camera still changing
      build(api, moving);
      cacheKey = key;
      cacheCoarse = moving;
      lastBuild = now;
      T.version++;
    }
    if (cacheCoarse) scheduleRefine();

    ctx.drawImage(cache, 0, 0, W, H);
  }

  // After the camera settles, rebuild the fine mesh and ask pulse.js to
  // redraw (it listens for this event and calls update()).
  function scheduleRefine() {
    clearTimeout(refineTimer);
    refineTimer = setTimeout(() => {
      if (!cacheCoarse || !lastApi) return;
      build(lastApi, false);
      cacheCoarse = false;
      lastBuild = 0;
      window.dispatchEvent(new Event("pulse-terrain-ready"));
    }, SETTLE_MS + 40);
  }

  function build(api, coarse) {
    const { project, cam, W, H, dpr, theme } = api;
    if (!cache) cache = document.createElement("canvas");
    const pw = Math.max(1, Math.round(W * dpr)), ph = Math.max(1, Math.round(H * dpr));
    if (cache.width !== pw || cache.height !== ph) { cache.width = pw; cache.height = ph; }
    const g = cache.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);

    const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw);
    const sinP = Math.sin(cam.pitch), cosP = Math.cos(cam.pitch);

    // Which part of the frame is on screen? Unproject the screen corners at
    // z = 0, allowing for the tallest ground pushing points up the screen.
    const xrHalf = W / 2 / cam.zoom;
    const yrTop = (H / 2 + cam.lift) / (cam.zoom * sinP);
    const yrBot = (H / 2 + cam.lift - H) / (cam.zoom * sinP) - grid.maxZ * cosP / sinP;
    let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
    for (const [xr, yr] of [[-xrHalf, yrTop], [xrHalf, yrTop], [-xrHalf, yrBot], [xrHalf, yrBot]]) {
      const x = cam.cx + xr * cy + yr * sy, y = cam.cy - xr * sy + yr * cy;
      if (x < xmin) xmin = x; if (x > xmax) xmax = x;
      if (y < ymin) ymin = y; if (y > ymax) ymax = y;
    }
    xmin = Math.max(xmin, bbox.x0); xmax = Math.min(xmax, bbox.x1);
    ymin = Math.max(ymin, bbox.y0); ymax = Math.min(ymax, bbox.y1);
    if (xmin >= xmax || ymin >= ymax) return;

    // Cell size from the zoom, then the quad budget.
    const targetPx = W < 720 ? TARGET_PX_PHONE : TARGET_PX_DESKTOP;
    let cell = Math.max(targetPx / cam.zoom, MIN_CELL_KM) * (coarse ? COARSE_FACTOR : 1);
    const quads = ((xmax - xmin) / cell) * ((ymax - ymin) / cell);
    if (quads > MAX_QUADS) cell *= Math.sqrt(quads / MAX_QUADS);

    // Mesh nodes cover the frame in steps of `cell` from its south-west
    // corner; the last column/row is clamped to the frame edge.
    const bw = bbox.x1 - bbox.x0, bh = bbox.y1 - bbox.y0;
    const NX = Math.ceil(bw / cell - 1e-6) + 1, NY = Math.ceil(bh / cell - 1e-6) + 1;
    const i0 = Math.max(0, Math.floor((xmin - bbox.x0) / cell)), i1 = Math.min(NX - 1, Math.ceil((xmax - bbox.x0) / cell));
    const j0 = Math.max(0, Math.floor((ymin - bbox.y0) / cell)), j1 = Math.min(NY - 1, Math.ceil((ymax - bbox.y0) / cell));
    const nx = i1 - i0 + 1, ny = j1 - j0 + 1;
    if (nx < 2 || ny < 2) return;

    const nodeX = (i) => Math.min(bbox.x0 + i * cell, bbox.x1);
    const nodeY = (j) => Math.min(bbox.y0 + j * cell, bbox.y1);

    // Node heights, screen positions and valley depth (how far the ground
    // sits below its blurred surroundings, in km of table).
    const gz = new Float32Array(nx * ny), sx = new Float32Array(nx * ny), syy = new Float32Array(nx * ny);
    const ao = new Float32Array(nx * ny);
    for (let j = 0; j < ny; j++) {
      const y = nodeY(j0 + j);
      for (let i = 0; i < nx; i++) {
        const x = nodeX(i0 + i), k = j * nx + i;
        const z = zAt(x, y);
        const p = project(x, y, z);
        gz[k] = z; sx[k] = p[0]; syy[k] = p[1];
        ao[k] = Math.min(1, Math.max(0, zBlurAt(x, y) - z) / (0.35 * EXAGGERATION));
      }
    }

    // Colours.
    const table = parseColor(theme.table), bg = parseColor(theme.bg);
    const dark = luminance(table) < 0.45;
    const litK = dark ? 0.17 : 0.10;      // how far a lit slope goes toward white
    const shadeK = dark ? 0.24 : 0.20;    // how far a shaded slope goes toward black
    const fogK = dark ? 0.25 : 0.18;      // far-edge fade toward the page background
    const aoK = dark ? 0.22 : 0.16;
    const contourStyle = dark ? "rgba(230,236,240,0.16)" : "rgba(35,39,42,0.2)";
    const skirt = mix(table, [0, 0, 0], dark ? 0.40 : 0.24);

    // Skirts: the block's side faces, from the ground down to the table plane
    // along each frame edge. Far ones are hidden by the mesh; near ones close
    // the gap between the lifted ground and the flat table.
    g.fillStyle = css(skirt); g.strokeStyle = css(skirt); g.lineWidth = 0.7;
    const edge = (nodes) => {
      g.beginPath();
      for (let k = 0; k < nodes.length; k++) {
        const n = nodes[k];
        if (k === 0) g.moveTo(sx[n], syy[n]); else g.lineTo(sx[n], syy[n]);
      }
      for (let k = nodes.length - 1; k >= 0; k--) {
        const n = nodes[k], i = n % nx, j = (n - i) / nx;
        const p = project(nodeX(i0 + i), nodeY(j0 + j), 0);
        g.lineTo(p[0], p[1]);
      }
      g.closePath(); g.fill(); g.stroke();
    };
    const range = (n, f) => Array.from({ length: n }, (_, k) => f(k));
    if (j0 === 0) edge(range(nx, (i) => i));                              // south
    if (j1 === NY - 1) edge(range(nx, (i) => (ny - 1) * nx + i));         // north
    if (i0 === 0) edge(range(ny, (j) => j * nx));                         // west
    if (i1 === NX - 1) edge(range(ny, (j) => j * nx + nx - 1));           // east

    // Light at every node from the slope across its neighbours, so a cell
    // takes the mean of its four corners and the shading varies smoothly
    // across the mesh instead of jumping cell to cell.
    const nodeDot = new Float32Array(nx * ny);
    for (let j = 0; j < ny; j++) {
      const jm = Math.max(j - 1, 0), jp = Math.min(j + 1, ny - 1);
      const wY = nodeY(j0 + jp) - nodeY(j0 + jm);
      for (let i = 0; i < nx; i++) {
        const im = Math.max(i - 1, 0), ip = Math.min(i + 1, nx - 1);
        const wX = nodeX(i0 + ip) - nodeX(i0 + im);
        const dzdx = (gz[j * nx + ip] - gz[j * nx + im]) / Math.max(wX, 1e-6);
        const dzdy = (gz[jp * nx + i] - gz[jm * nx + i]) / Math.max(wY, 1e-6);
        const inv = 1 / Math.sqrt(dzdx * dzdx + dzdy * dzdy + 1);
        const nX = -dzdx * inv, nY = -dzdy * inv, nZ = inv;
        const nxr = nX * cy - nY * sy, nyr = nX * sy + nY * cy;
        nodeDot[j * nx + i] = nxr * LIGHT[0] + nyr * LIGHT[1] + nZ * LIGHT[2];
      }
    }

    // Cells, far to near. Depth is the rotated y of the cell centre.
    const cx = nx - 1, cyN = ny - 1, count = cx * cyN;
    const order = new Uint32Array(count), depth = new Float32Array(count);
    let dmin = Infinity, dmax = -Infinity;
    for (let j = 0; j < cyN; j++) {
      const y = (nodeY(j0 + j) + nodeY(j0 + j + 1)) / 2 - cam.cy;
      for (let i = 0; i < cx; i++) {
        const x = (nodeX(i0 + i) + nodeX(i0 + i + 1)) / 2 - cam.cx;
        const k = j * cx + i, d = x * sy + y * cy;
        order[k] = k; depth[k] = d;
        if (d < dmin) dmin = d; if (d > dmax) dmax = d;
      }
    }
    order.sort((a, b) => depth[b] - depth[a]);
    const dspan = Math.max(dmax - dmin, 1e-6);

    // Keep the mesh for the snow layer, which is drawn over this cache.
    mesh = { nx, ny, cx, cyN, gz, sx, syy, nodeDot, order, depth, dmin, dspan, table, bg, dark, fogK, W, H, dpr };
    snowKey = "";

    const cFrom = CONTOUR_FROM_M / 1000 * EXAGGERATION, cStep = CONTOUR_STEP_M / 1000 * EXAGGERATION;
    g.lineWidth = 0.7;
    let lastStyle = "";
    for (let o = 0; o < count; o++) {
      const k = order[o], i = k % cx, j = (k - i) / cx;
      const n00 = j * nx + i, n10 = n00 + 1, n01 = n00 + nx, n11 = n01 + 1;

      // Skip cells wholly off screen.
      const x0 = sx[n00], x1 = sx[n10], x2 = sx[n11], x3 = sx[n01];
      const y0 = syy[n00], y1 = syy[n10], y2 = syy[n11], y3 = syy[n01];
      if ((x0 < 0 && x1 < 0 && x2 < 0 && x3 < 0) || (x0 > W && x1 > W && x2 > W && x3 > W)) continue;
      if ((y0 < 0 && y1 < 0 && y2 < 0 && y3 < 0) || (y0 > H && y1 > H && y2 > H && y3 > H)) continue;

      const dot = (nodeDot[n00] + nodeDot[n10] + nodeDot[n01] + nodeDot[n11]) / 4;

      let col;
      if (dot >= FLAT_DOT) col = mix(table, [255, 255, 255], (dot - FLAT_DOT) / (1 - FLAT_DOT) * litK);
      else col = mix(table, [0, 0, 0], (FLAT_DOT - dot) / FLAT_DOT * shadeK);
      const zc = (gz[n00] + gz[n10] + gz[n01] + gz[n11]) / 4;
      const aoc = (ao[n00] + ao[n10] + ao[n01] + ao[n11]) / 4;
      if (aoc > 0) col = mix(col, [0, 0, 0], aoc * aoK);
      const fog = (depth[k] - dmin) / dspan;
      col = mix(col, bg, fog * fog * fogK);

      const style = css(col);
      if (style !== lastStyle) { g.fillStyle = style; g.strokeStyle = style; lastStyle = style; }
      g.beginPath();
      g.moveTo(x0, y0); g.lineTo(x1, y1); g.lineTo(x2, y2); g.lineTo(x3, y3);
      g.closePath(); g.fill(); g.stroke();

      // Contours through this cell (marching squares on its four corners).
      const zmax = Math.max(gz[n00], gz[n10], gz[n01], gz[n11]);
      if (zmax >= cFrom) {
        const zmin = Math.min(gz[n00], gz[n10], gz[n01], gz[n11]);
        const first = Math.max(cFrom, Math.ceil(zmin / cStep) * cStep);
        if (first <= zmax) {
          g.strokeStyle = contourStyle; g.lineWidth = 0.55;
          g.beginPath();
          // corners in ring order: n00 → n10 → n11 → n01
          const cz = [gz[n00], gz[n10], gz[n11], gz[n01]];
          const cxs = [x0, x1, x2, x3], cys = [y0, y1, y2, y3];
          for (let lv = first; lv <= zmax; lv += cStep) {
            const pts = [];
            for (let e = 0; e < 4; e++) {
              const a = cz[e], b = cz[(e + 1) % 4];
              if ((a < lv) !== (b < lv)) {
                const t = (lv - a) / (b - a);
                pts.push([cxs[e] + (cxs[(e + 1) % 4] - cxs[e]) * t, cys[e] + (cys[(e + 1) % 4] - cys[e]) * t]);
              }
            }
            if (pts.length >= 2) { g.moveTo(pts[0][0], pts[0][1]); g.lineTo(pts[1][0], pts[1][1]); }
            if (pts.length === 4) { g.moveTo(pts[2][0], pts[2][1]); g.lineTo(pts[3][0], pts[3][1]); }
          }
          g.stroke();
          g.strokeStyle = lastStyle; g.lineWidth = 0.7;
        }
      }
    }
  }

  // ── Snow ────────────────────────────────────────────────────────────────
  // Snowline in metres for a day of the year (0..365).
  function snowlineM(doy) {
    const mid = (SNOW_LOW_M + SNOW_HIGH_M) / 2, amp = (SNOW_HIGH_M - SNOW_LOW_M) / 2;
    return mid + amp * Math.cos(2 * Math.PI * (doy - SNOW_PEAK_DOY) / 366);
  }

  // Snow above the season's snowline, on the cached mesh, as its own cached
  // layer. Lit faces are near-white, shaded faces a cool blue-grey.
  function drawSnow(ctx, api) {
    if (!T.ready || !mesh) return;
    const doy = api.doy == null ? 200 : api.doy;
    const lineM = Math.round(snowlineM(doy) / 50) * 50;
    const key = cacheKey + "|" + lineM;
    if (key !== snowKey || !snowCache) {
      const { nx, cx, cyN, gz, sx, syy, nodeDot, order, depth, dmin, dspan, bg, fogK, W, H, dpr } = mesh;
      if (!snowCache) snowCache = document.createElement("canvas");
      const pw = Math.max(1, Math.round(W * dpr)), ph = Math.max(1, Math.round(H * dpr));
      if (snowCache.width !== pw || snowCache.height !== ph) { snowCache.width = pw; snowCache.height = ph; }
      const g = snowCache.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, W, H);
      const snowZ = lineM / 1000 * EXAGGERATION, edge = SNOW_EDGE_M / 1000 * EXAGGERATION;
      g.lineWidth = 0.6;
      for (let o = 0; o < order.length; o++) {
        const k = order[o], i = k % cx, j = (k - i) / cx;
        const n00 = j * nx + i, n10 = n00 + 1, n01 = n00 + nx, n11 = n01 + 1;
        const zc = (gz[n00] + gz[n10] + gz[n01] + gz[n11]) / 4;
        if (zc < snowZ - edge) continue;
        let a = (zc - (snowZ - edge)) / (2 * edge);
        a = a < 0 ? 0 : a > 1 ? 1 : a;
        a = a * a * (3 - 2 * a);                              // smooth edge
        if (a <= 0.02) continue;
        const dot = (nodeDot[n00] + nodeDot[n10] + nodeDot[n01] + nodeDot[n11]) / 4;
        let t = (dot - 0.45) / (1 - 0.45);
        t = t < 0 ? 0 : t > 1 ? 1 : t;
        let col = mix(SNOW_SHADE, SNOW_LIT, t);
        const fog = (depth[k] - dmin) / dspan;
        col = mix(col, bg, fog * fog * fogK);
        g.globalAlpha = a * 0.86;
        const style = css(col);
        g.fillStyle = style; g.strokeStyle = style;
        g.beginPath();
        g.moveTo(sx[n00], syy[n00]); g.lineTo(sx[n10], syy[n10]); g.lineTo(sx[n11], syy[n11]); g.lineTo(sx[n01], syy[n01]);
        g.closePath(); g.fill(); g.stroke();
      }
      g.globalAlpha = 1;
      snowKey = key;
    }
    ctx.drawImage(snowCache, 0, 0, mesh.W, mesh.H);
  }

  // ── Colour helpers ──────────────────────────────────────────────────────
  let probe = null;
  // Any CSS colour → [r, g, b], via the canvas's own parser.
  function parseColor(str) {
    if (!probe) probe = document.createElement("canvas").getContext("2d");
    probe.fillStyle = "#000";
    probe.fillStyle = str || "#888";
    const v = probe.fillStyle;
    if (v[0] === "#") return [parseInt(v.slice(1, 3), 16), parseInt(v.slice(3, 5), 16), parseInt(v.slice(5, 7), 16)];
    const m = v.match(/[\d.]+/g);
    return m ? [+m[0], +m[1], +m[2]] : [128, 128, 128];
  }
  function mix(a, b, t) {
    t = Math.max(0, Math.min(1, t));
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  }
  function css(c) {
    return `rgb(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])})`;
  }
  function luminance(c) {
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
  }
  function normalize(v) {
    const n = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / n, v[1] / n, v[2] / n];
  }
})();
