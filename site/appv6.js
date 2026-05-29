// ── Config ────────────────────────────────────────────────────────────────────
const DATA_URL =
  (typeof window.GORGEGAUGE_LATEST_JSON === "string" && window.GORGEGAUGE_LATEST_JSON.trim())
    ? window.GORGEGAUGE_LATEST_JSON.trim()
    : "/latestHusum.json";

const GAUGE_URL =
  (typeof window.GORGEGAUGE_GAUGE_JSON === "string" && window.GORGEGAUGE_GAUGE_JSON.trim())
    ? window.GORGEGAUGE_GAUGE_JSON.trim()
    : "/husumGauge.json";

const LOCATION_LABEL =
  (typeof window.GORGEGAUGE_LOCATION_LABEL === "string" && window.GORGEGAUGE_LOCATION_LABEL.trim())
    ? window.GORGEGAUGE_LOCATION_LABEL.trim()
    : "Husum";

// ── Gallery elements ──────────────────────────────────────────────────────────
const statusEl  = document.getElementById("status");
const galleryEl = document.getElementById("gallery");

const lightboxEl      = document.getElementById("lightbox");
const lightboxImg     = document.getElementById("lightbox-image");
const lightboxCaption = document.getElementById("lightbox-caption");
const lightboxCloseBtn  = document.querySelector(".lightbox-close");
const lightboxBackdrop  = document.querySelector(".lightbox-backdrop");

function setStatus(text) {
  if (statusEl) statusEl.textContent = text;
}

// ── Lightbox ──────────────────────────────────────────────────────────────────
function openLightbox(url, caption, whenRaw) {
  if (!lightboxEl || !lightboxImg || !lightboxCaption) return;
  lightboxImg.src = url;
  const whenText = whenRaw ? " " + new Date(whenRaw).toLocaleString() : "";
  lightboxCaption.textContent = (caption || "") + whenText;
  lightboxEl.classList.remove("hidden");
  document.body.classList.add("no-scroll");
}

function closeLightbox() {
  if (!lightboxEl || !lightboxImg || !lightboxCaption) return;
  lightboxEl.classList.add("hidden");
  lightboxImg.src = "";
  lightboxCaption.textContent = "";
  document.body.classList.remove("no-scroll");
}

if (lightboxCloseBtn) lightboxCloseBtn.addEventListener("click", closeLightbox);
if (lightboxBackdrop) lightboxBackdrop.addEventListener("click", closeLightbox);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeLightbox(); });

// ── Gallery card builder ──────────────────────────────────────────────────────
function buildCard(item, index) {
  const card = document.createElement("article");
  card.className = "card";

  const img = document.createElement("img");
  img.src = item.url;
  img.loading = "lazy";
  img.alt = item.fileName || "Image";

  const footer = document.createElement("div");
  footer.className = "card-footer";

  const left = document.createElement("div");
  left.style.display = "flex";
  left.style.flexDirection = "column";
  left.style.minWidth = "0";

  const ts = document.createElement("span");
  ts.className = "timestamp";
  const date = item.lastModified ? new Date(item.lastModified) : null;
  ts.textContent = date
    ? date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "";

  const name = document.createElement("span");
  name.style.fontSize = "0.7rem";
  name.style.color = "#6b7280";
  name.style.whiteSpace = "nowrap";
  name.style.overflow = "hidden";
  name.style.textOverflow = "ellipsis";
  name.textContent = item.fileName || "";

  left.appendChild(ts);
  left.appendChild(name);

  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = index === 0 ? "Latest (" + LOCATION_LABEL + ")" : "Recent";

  footer.appendChild(left);
  footer.appendChild(badge);
  card.appendChild(img);
  card.appendChild(footer);

  card.addEventListener("click", () => openLightbox(item.url, item.fileName, item.lastModified));
  return card;
}

// ── Gallery loader ────────────────────────────────────────────────────────────
async function loadImages() {
  try {
    setStatus("Loading…");
    const bust = DATA_URL.includes("?") ? "&" : "?";
    const res = await fetch(DATA_URL + bust + "v=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const items = await res.json();

    if (!Array.isArray(items) || items.length === 0) {
      setStatus("No images found.");
      if (galleryEl) galleryEl.innerHTML = "";
      return;
    }

    setStatus(items.length + " images");
    const fragment = document.createDocumentFragment();
    items.forEach((item, index) => fragment.appendChild(buildCard(item, index)));
    if (galleryEl) {
      galleryEl.innerHTML = "";
      galleryEl.appendChild(fragment);
    }
  } catch (err) {
    console.error(err);
    setStatus("Error loading images.");
  }
}

// ── Gauge strip ───────────────────────────────────────────────────────────────
function levelColor(l) {
  return l < 2.5 ? "#22c55e" : l < 4.0 ? "#3b82f6" : l < 6.0 ? "#f97316" : "#ef4444";
}
function levelLabel(l) {
  return l < 2.5 ? "Chill" : l < 4.0 ? "A Vibe" : l < 6.0 ? "Spicy" : "Wildin'";
}

async function loadGauge() {
  try {
    const bust = GAUGE_URL.includes("?") ? "&" : "?";
    const res = await fetch(GAUGE_URL + bust + "v=" + Date.now(), { cache: "no-store" });
    if (!res.ok) return; // no reading yet — strip stays hidden
    const d = await res.json();
    if (d.error || d.level == null) return;

    const color = levelColor(d.level);

    // Reveal the strip
    const strip = document.getElementById("gaugeStrip");
    if (strip) strip.style.display = "block";

    // Level number
    const numEl = document.getElementById("gaugeNumber");
    if (numEl) {
      numEl.textContent = parseFloat(d.level).toFixed(2);
      numEl.style.color = color;
    }

    // Badge
    const badgeEl = document.getElementById("gaugeBadge");
    if (badgeEl) {
      badgeEl.textContent = levelLabel(d.level);
      badgeEl.style.background = color + "22";
      badgeEl.style.color = color;
      badgeEl.style.borderColor = color + "55";
    }

    // Progress bar
    const fill = document.getElementById("gaugeBarFill");
    if (fill) {
      fill.style.background = `linear-gradient(90deg, #22c55e, #3b82f6 40%, ${color})`;
      setTimeout(() => {
        fill.style.width = Math.min((d.level / 6) * 100, 100) + "%";
      }, 50);
    }

    // Timestamp
    const readAt = document.getElementById("gaugeReadAt");
    if (readAt && d.read_at) readAt.textContent = "Last read: " + d.read_at;

    // AI notes
    const notesEl = document.getElementById("gaugeNotes");
    if (notesEl && d.notes) notesEl.textContent = d.notes;

    // Trend indicator
    const trendEl = document.getElementById("gaugeTrend");
    if (trendEl && d.previous_level != null) {
      const diff = Math.round((d.level - d.previous_level) * 100) / 100;
      const abs  = Math.abs(diff);
      if (abs < 0.1) {
        trendEl.textContent = "→ Stable";
        trendEl.style.color = "#9ca3af";
      } else if (diff > 0) {
        trendEl.textContent = `↑ +${diff.toFixed(2)} ft`;
        trendEl.style.color = "#f97316";
      } else {
        trendEl.textContent = `↓ ${diff.toFixed(2)} ft`;
        trendEl.style.color = "#22c55e";
      }
    }

  } catch (e) {
    // Gauge data unavailable — strip stays hidden, gallery still loads
  }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
loadGauge();
loadImages();
loadReports();
const reportForm = document.getElementById("reportForm");
if (reportForm) reportForm.addEventListener("submit", submitReport);

// Date picker — the input overlays the pill at opacity 0.01, so tapping
// the pill directly hits the input with no JS trigger required.
const datePicker      = document.getElementById("datePicker");
const datePickerClear = document.getElementById("datePickerClear");

if (datePicker) {
  datePicker.max = new Date().toISOString().slice(0, 10);

  function onDateChange() {
    const val = datePicker.value;
    if (!val) return;
    loadImagesByDate(val);
    if (datePickerClear) datePickerClear.classList.remove("hidden");
    const labelText = document.querySelector(".archive-btn-text");
    if (labelText) {
      const d = new Date(val + "T12:00:00");
      labelText.textContent = d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
    }
  }

  datePicker.addEventListener("change", onDateChange);
  datePicker.addEventListener("input",  onDateChange);
}

if (datePickerClear) {
  datePickerClear.addEventListener("click", function () {
    if (datePicker) datePicker.value = "";
    this.classList.add("hidden");
    const labelText = document.querySelector(".archive-btn-text");
    if (labelText) labelText.textContent = "Browse dates";
    resetToLatest();
  });
}

// ── Archive / date-picker ─────────────────────────────────────────────────────

const INDEX_URL =
  (typeof window.GORGEGAUGE_INDEX_JSON === "string" && window.GORGEGAUGE_INDEX_JSON.trim())
    ? window.GORGEGAUGE_INDEX_JSON.trim()
    : "/husumIndex.json";

let _imageIndex = null; // cached after first fetch

async function getImageIndex() {
  if (_imageIndex) return _imageIndex;
  const resp = await fetch(INDEX_URL + "?v=" + Date.now());
  if (!resp.ok) throw new Error("index fetch failed");
  const data = await resp.json();
  _imageIndex = data.images || [];
  return _imageIndex;
}

async function loadImagesByDate(dateStr) {
  setStatus("Loading…");
  if (galleryEl) galleryEl.innerHTML = "";

  try {
    const index = await getImageIndex();
    const matches = index.filter(img => img.date === dateStr);

    // Friendly date label
    const label = new Date(dateStr + "T12:00:00").toLocaleDateString("en-US",
      { weekday: "long", month: "long", day: "numeric", year: "numeric" });

    if (!matches.length) {
      setStatus("No images found for " + label + ".");
      return;
    }

    setStatus(matches.length + " image" + (matches.length !== 1 ? "s" : ""));

    // setTimeout(0) gives Safari a tick to repaint the cleared gallery before
    // we append new cards — without it Safari sometimes shows stale content
    setTimeout(function () {
      const fragment = document.createDocumentFragment();
      matches.forEach(function (item, i) { fragment.appendChild(buildCard(item, i + 1)); });
      if (galleryEl) galleryEl.appendChild(fragment);
    }, 0);
  } catch (err) {
    console.error(err);
    setStatus("Could not load archive — try again.");
  }
}

function resetToLatest() {
  loadImages();
}

// ── Paddler Reports ───────────────────────────────────────────────────────────

const REPORTS_JSON =
  (typeof window.GORGEGAUGE_REPORTS_JSON === "string" && window.GORGEGAUGE_REPORTS_JSON.trim())
    ? window.GORGEGAUGE_REPORTS_JSON.trim()
    : "/husumReports.json";

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatReportDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " at " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZoneName: "short" });
}

function renderReports(reports) {
  const list = document.getElementById("reportList");
  if (!list) return;
  if (!reports || !reports.length) {
    list.innerHTML = '<p class="report-empty">No reports yet — be the first to share conditions.</p>';
    return;
  }
  list.innerHTML = reports.map(r => `
    <div class="report-card">
      <div class="report-meta">
        <span class="report-name">${escapeHtml(r.name || "Anonymous")}</span>
        <span>·</span>
        <span>${formatReportDate(r.iso)}</span>
        ${r.level != null ? `<span>·</span><span class="report-level">${r.level} ft</span>` : ""}
      </div>
      <div class="report-text">${escapeHtml(r.text)}</div>
    </div>
  `).join("");
}

async function loadReports() {
  try {
    const resp = await fetch(REPORTS_JSON + "?v=" + Date.now());
    if (!resp.ok) return;
    const data = await resp.json();
    renderReports(data.reports || []);
  } catch (e) {
    // Reports are optional — fail silently
  }
}

async function submitReport(e) {
  e.preventDefault();
  const form = e.target;
  const btn  = form.querySelector(".report-submit");
  const msg  = document.getElementById("reportMsg");
  const text = (form.querySelector("[name=text]").value || "").trim();
  const name = (form.querySelector("[name=name]").value || "").trim();
  const honeypot = form.querySelector("[name=website]").value;

  if (!text || honeypot) return;

  const apiUrl = (typeof window.REPORTS_API === "string") ? window.REPORTS_API.trim() : "";
  if (!apiUrl) {
    if (msg) { msg.textContent = "Submissions not yet configured."; msg.style.color = "#9ca3af"; msg.style.display = "block"; }
    return;
  }

  btn.disabled = true;
  btn.textContent = "Sending…";
  if (msg) msg.style.display = "none";

  try {
    const resp = await fetch(apiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, name }),
    });
    if (!resp.ok) throw new Error("server error");

    form.querySelector("[name=text]").value = "";
    form.querySelector("[name=name]").value = "";
    if (msg) { msg.textContent = "Thanks — your report has been shared!"; msg.style.color = "#22c55e"; msg.style.display = "block"; }
    await loadReports();
  } catch (err) {
    if (msg) { msg.textContent = "Something went wrong — try again."; msg.style.color = "#ef4444"; msg.style.display = "block"; }
  } finally {
    btn.disabled = false;
    btn.textContent = "Share Beta";
  }
}
