const DATA_URL =
  (typeof window.GORGEGAUGE_LATEST_JSON === "string" &&
   window.GORGEGAUGE_LATEST_JSON.trim())
    ? window.GORGEGAUGE_LATEST_JSON.trim()
    : "/latest.json";

const LOCATION_LABEL =
  (typeof window.GORGEGAUGE_LOCATION_LABEL === "string" &&
   window.GORGEGAUGE_LOCATION_LABEL.trim())
    ? window.GORGEGAUGE_LOCATION_LABEL.trim()
    : "Husum";

/******************************************************************
 * DOM ELEMENTS
 ******************************************************************/
const statusEl = document.getElementById("status");
const galleryEl = document.getElementById("gallery");

const lightboxEl = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightbox-image");
const lightboxCaption = document.getElementById("lightbox-caption");
const lightboxCloseBtn = document.querySelector(".lightbox-close");

/******************************************************************
 * LIGHTBOX CONTROLS
 ******************************************************************/
function openLightbox(url, caption) {
  if (!lightboxEl || !lightboxImg || !lightboxCaption) return;

  lightboxImg.src = url;
  lightboxImg.alt = caption || "";
  lightboxCaption.textContent = caption || "";

  lightboxImg.style.width = "auto";
  lightboxImg.style.height = "auto";
  lightboxImg.style.maxWidth = "100vw";
  lightboxImg.style.maxHeight = "90vh";
  lightboxImg.style.objectFit = "contain";
  lightboxImg.style.display = "block";
  lightboxImg.style.margin = "0 auto";

  lightboxEl.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeLightbox() {
  if (!lightboxEl || !lightboxImg) return;
  lightboxEl.classList.add("hidden");
  lightboxImg.src = "";
  document.body.style.overflow = "";
}

if (lightboxCloseBtn) lightboxCloseBtn.addEventListener("click", closeLightbox);

if (lightboxEl) {
  lightboxEl.addEventListener("click", (e) => {
    if (e.target === lightboxEl || e.target.classList.contains("lightbox-backdrop")) {
      closeLightbox();
    }
  });
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeLightbox();
});

/******************************************************************
 * MAIN: FETCH JSON + RENDER
 ******************************************************************/
async function loadImages() {
  try {
    if (statusEl) statusEl.textContent = "Loading…";

    const bust = DATA_URL.includes("?") ? "&" : "?";
    const res = await fetch(`${DATA_URL}${bust}v=${Date.now()}`, { cache: "no-store" });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const items = await res.json();

    if (!Array.isArray(items) || items.length === 0) {
      if (statusEl) statusEl.textContent = "No images found.";
      if (galleryEl) galleryEl.innerHTML = "";
      return;
    }

    if (statusEl) statusEl.textContent = `Showing ${items.length} latest images.`;

    const fragment = document.createDocumentFragment();

    items.forEach((item, index) => {
      const card = document.createElement("article");
      card.className = "card";
      card.style.cursor = "pointer";

      const img = document.createElement("img");
      img.src = item.url;
      img.loading = "lazy";
      img.alt = item.fileName || `Image ${index + 1}`;

      const footer = document.createElement("div");
      footer.className = "card-footer";

      const left = document.createElement("div");
      left.style.display = "flex";
      left.style.flexDirection = "column";

      const ts = document.createElement("span");
      ts.className = "timestamp";

      const date = item.lastModified ? new Date(item.lastModified) : null;
      ts.textContent = date
        ? date.toLocaleString(undefined, {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit"
          })
        : "";

      const name = document.createElement("span");
      name.style.fontSize = "0.7rem";
      name.style.color = "#6b7280";
      name.textContent = item.fileName || "";

      left.appendChild(ts);
      left.appendChild(name);

      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = index === 0 ? `Latest (${LOCATION_LABEL})` : "Recent";

      footer.appendChild(left);
      footer.appendChild(badge);

      card.appendChild(img);
      card.appendChild(footer);

      card.addEventListener("click", () => {
        openLightbox(item.url, item.fileName);
      });

      fragment.appendChild(card);
    });

    if (galleryEl) {
      galleryEl.innerHTML = "";
      galleryEl.appendChild(fragment);
    }
  } catch (err) {
    console.error("Error loading images:", err);
    if (statusEl) statusEl.textContent = "Error loading images.";
  }
}

/******************************************************************
 * RUN
 ******************************************************************/
loadImages();