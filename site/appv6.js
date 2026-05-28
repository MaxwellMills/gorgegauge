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

    setStatus("Showing " + items.length + " latest images.");
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
  return l < 2.5 ? "#22c55e" : l < 3.5 ? "#3b82f6" : l < 4.5 ? "#f97316" : "#ef4444";
}
function levelLabel(l) {
  return l < 2.5 ? "Chill" : l < 3.5 ? "A Vibe" : l < 4.5 ? "Spicy" : "Wildin'";
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

  } catch (e) {
    // Gauge data unavailable — strip stays hidden, gallery still loads
  }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
loadGauge();
loadImages();
