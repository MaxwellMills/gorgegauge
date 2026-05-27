const DATA_URL =
  (typeof window.GORGEGAUGE_LATEST_JSON === "string" &&
   window.GORGEGAUGE_LATEST_JSON.trim())
    ? window.GORGEGAUGE_LATEST_JSON.trim()
    : "/latestHusum.json";

const LOCATION_LABEL =
  (typeof window.GORGEGAUGE_LOCATION_LABEL === "string" &&
   window.GORGEGAUGE_LOCATION_LABEL.trim())
    ? window.GORGEGAUGE_LOCATION_LABEL.trim()
    : "Husum";

const statusEl = document.getElementById("status");
const galleryEl = document.getElementById("gallery");

const lightboxEl = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightbox-image");
const lightboxCaption = document.getElementById("lightbox-caption");
const lightboxCloseBtn = document.querySelector(".lightbox-close");
const lightboxBackdrop = document.querySelector(".lightbox-backdrop");

function setStatus(text) {
  if (statusEl) statusEl.textContent = text;
}

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

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeLightbox();
});

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

  card.addEventListener("click", () => {
    openLightbox(item.url, item.fileName, item.lastModified);
  });

  return card;
}

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

loadImages();