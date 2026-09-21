// Front-page card for the River Pulse: every gauged river as a bar for the
// most recent day, coloured by water temperature, linking to /pulse.html.
// Reads the same two files the full page uses.

(() => {
  "use strict";

  const strip = document.getElementById("pulseStrip");
  if (!strip) return;

  const TEMP_STOPS = [
    [0, [43, 95, 136]],
    [6, [58, 154, 144]],
    [11, [189, 184, 139]],
    [16, [217, 139, 78]],
    [22, [168, 58, 44]],
  ];
  const NO_TEMP = [128, 134, 132];

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
  const rgb = (c) => `rgb(${c[0] | 0},${c[1] | 0},${c[2] | 0})`;

  function fmt(v) {
    if (v == null) return "—";
    if (v >= 1000) {
      const mag = Math.pow(10, Math.floor(Math.log10(v)) - 2);
      return (Math.round(v / mag) * mag).toLocaleString("en-US");
    }
    return Math.round(v).toLocaleString("en-US");
  }

  // Last value on or before day i, looking back at most a week.
  function recent(arr, i) {
    for (let k = i; k >= Math.max(0, i - 7); k--) if (arr[k] != null) return arr[k];
    return null;
  }

  async function load() {
    const [rv, fl] = await Promise.all([
      fetch("/pulse/rivers.json", { cache: "no-cache" }).then((r) => r.json()),
      fetch("/pulse/flows.json", { cache: "no-cache" }).then((r) => r.json()),
    ]);

    // The most recent day on which most rivers reported.
    let day = fl.days - 1;
    for (; day > 0; day--) {
      const n = rv.rivers.filter((r) => (fl.sites[r.site]?.cfs || [])[day] != null).length;
      if (n >= rv.rivers.length / 2) break;
    }
    const iso = new Date(new Date(fl.start + "T00:00:00Z").getTime() + day * 864e5).toISOString().slice(0, 10);
    const dateText = new Date(iso + "T00:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

    const rows = rv.rivers.map((r) => {
      const s = fl.sites[r.site] || { cfs: [], temp: [] };
      return { name: r.short, cfs: recent(s.cfs, day), temp: recent(s.temp, day), mean: r.mean_cfs, spine: r.kind === "spine", kind: r.kind };
    }).filter((r) => r.kind !== "beyond" || window.innerWidth >= 900);
    const maxH = Math.max(...rows.map((r) => Math.sqrt(r.cfs || 0)), 1);

    const bars = document.getElementById("pulseBars");
    bars.innerHTML = "";
    for (const r of rows) {
      const col = document.createElement("a");
      col.className = "pulse-col" + (r.spine ? " pulse-col-spine" : "");
      col.href = `/pulse.html#d=${iso}`;
      const pct = r.cfs == null ? 0 : Math.max(4, (Math.sqrt(r.cfs) / maxH) * 100);
      const vsNormal = r.cfs != null && r.mean ? Math.round((r.cfs / r.mean) * 100) : null;
      col.title = `${r.name}: ${fmt(r.cfs)} cfs` + (r.temp != null ? `, ${r.temp.toFixed(1)} °C` : "") +
        (vsNormal != null ? `, ${vsNormal}% of long-term mean` : "");
      col.innerHTML =
        `<span class="pulse-val">${fmt(r.cfs)}</span>` +
        `<span class="pulse-bar-wrap"><i style="height:${pct}%;background:${rgb(tempRgb(r.temp))}"></i></span>` +
        `<span class="pulse-name">${r.name}</span>`;
      bars.appendChild(col);
    }

    document.getElementById("pulseDate").textContent = dateText;
    const link = document.getElementById("pulseLink");
    if (link) {
      link.href = `/pulse.html#d=${iso}`;
      const since = new Date(fl.start + "T00:00:00Z").toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
      link.textContent = `Every gauged river, every day since ${since} →`;
    }
    strip.style.display = "block";
  }

  load().catch(() => { /* card stays hidden if the data is unavailable */ });
})();
