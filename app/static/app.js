// ---- Element refs ----
const librarySelect      = document.getElementById("library-select");
const searchBox          = document.getElementById("search-box");
const grid               = document.getElementById("grid");
const gridStatus         = document.getElementById("grid-status");
const refreshLibraryBtn  = document.getElementById("refresh-library-btn");
const statusFilterGroup  = document.getElementById("status-filter");

const modal              = document.getElementById("modal");
const modalClose         = document.getElementById("modal-close");
const modalTitle         = document.getElementById("modal-title");
const modalStatus        = document.getElementById("modal-status");
const logoGrid           = document.getElementById("logo-grid");
const currentLogoBox     = document.getElementById("current-logo");
const langFilter         = document.getElementById("lang-filter");
const refreshLogosBtn    = document.getElementById("refresh-logos-btn");

// ---- State ----
let allItems        = [];   // full cached list from server
let currentRatingKey = null;
let currentSectionId = null;
let activeStatusFilter = "all"; // "all" | "set" | "unset"

// ---- Library loading ----
async function loadLibraries() {
  if (!librarySelect) return;
  try {
    const res = await fetch("/api/libraries");
    const data = await res.json();
    if (data.error) { grid.innerHTML = `<p>${data.error}</p>`; return; }
    const enabled = data.filter(l => l.enabled);
    if (!enabled.length) {
      grid.innerHTML = `<p>No libraries enabled. Enable some in <a href="/settings">Settings</a>.</p>`;
      return;
    }
    librarySelect.innerHTML = enabled
      .map(l => `<option value="${l.key}">${l.title} (${l.type})</option>`)
      .join("");
    currentSectionId = enabled[0].key;
    await loadItems(currentSectionId);
  } catch (e) {
    grid.innerHTML = `<p>Failed to load libraries: ${e}</p>`;
  }
}

async function loadItems(sectionId, forceRefresh = false) {
  currentSectionId = sectionId;
  grid.innerHTML = "";
  gridStatus.textContent = forceRefresh
    ? "Re-indexing library and downloading thumbnails from Plex…"
    : "Loading…";

  try {
    const url = `/api/library/${sectionId}/items` + (forceRefresh ? "?refresh=true" : "");
    const res = await fetch(url);
    const data = await res.json();
    if (data.error) { grid.innerHTML = `<p>${data.error}</p>`; gridStatus.textContent = ""; return; }
    allItems = data;
    renderGrid();
  } catch (e) {
    grid.innerHTML = `<p>Failed to load items: ${e}</p>`;
    gridStatus.textContent = "";
  }
}

// ---- Grid rendering ----
function getFilteredItems() {
  const q = searchBox.value.toLowerCase();
  return allItems.filter(i => {
    const matchesText = i.title.toLowerCase().includes(q);
    const matchesStatus =
      activeStatusFilter === "all" ||
      i.logoStatus === activeStatusFilter;
    return matchesText && matchesStatus;
  });
}

function renderGrid() {
  const items = getFilteredItems();
  const total = allItems.length;
  const shown = items.length;
  const setCount  = allItems.filter(i => i.logoStatus === "set").length;
  const unsetCount = allItems.filter(i => i.logoStatus === "unset").length;

  gridStatus.innerHTML =
    `${total} items &nbsp;·&nbsp; ` +
    `<span class="status-dot set"></span> ${setCount} set &nbsp;·&nbsp; ` +
    `<span class="status-dot unset"></span> ${unsetCount} unset` +
    (shown < total ? ` &nbsp;·&nbsp; showing ${shown}` : "");

  if (!items.length) {
    grid.innerHTML = `<p style="color:var(--text-dim)">No items match the current filter.</p>`;
    return;
  }

  grid.innerHTML = items.map(item => {
    let badge;
    if (item.logoStatus === "set") {
      // Always a local file when status is "set"
      badge = `<span class="card-badge set" title="Clear logo is set (local file)">✓</span>`;
    } else if (item.logoSource === "plex_server") {
      badge = `<span class="card-badge plex-only" title="Plex is showing a clear logo, but no local file exists yet">P</span>`;
    } else {
      badge = `<span class="card-badge unset" title="No clear logo set">–</span>`;
    }
    return `
      <div class="card" data-key="${item.ratingKey}" data-title="${escHtml(item.title)}">
        <div class="card-img-wrap">
          <img src="/thumb/${item.ratingKey}" alt="${escHtml(item.title)}"
               onerror="this.style.opacity=0">
          ${badge}
        </div>
        <div class="meta">
          <div class="title">${escHtml(item.title)}</div>
          <div class="year">${item.year || ""}</div>
        </div>
      </div>`;
  }).join("");

  grid.querySelectorAll(".card").forEach(card => {
    card.addEventListener("click", () =>
      openLogoPicker(card.dataset.key, card.dataset.title));
  });
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// ---- Status filter toggle ----
statusFilterGroup && statusFilterGroup.querySelectorAll(".toggle-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    statusFilterGroup.querySelectorAll(".toggle-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activeStatusFilter = btn.dataset.value;
    renderGrid();
  });
});

// ---- Logo picker modal ----
async function openLogoPicker(ratingKey, title) {
  currentRatingKey = ratingKey;
  modalTitle.textContent = title;
  modalStatus.textContent = "Loading logos…";
  modalStatus.className = "status";
  logoGrid.innerHTML = "";
  langFilter.innerHTML = "";
  currentLogoBox.className = "current-logo-box none";
  currentLogoBox.textContent = "Checking current logo…";
  modal.classList.remove("hidden");
  await fetchLogos();
}

async function fetchLogos(forceRefresh = false, lang = "") {
  modalStatus.textContent = "Loading logos…";
  modalStatus.className = "status";
  try {
    const params = new URLSearchParams();
    if (forceRefresh) params.set("refresh", "true");
    if (lang) params.set("lang", lang);
    const res = await fetch(`/api/item/${currentRatingKey}/logos?${params}`);
    const data = await res.json();
    if (data.error) {
      modalStatus.textContent = data.error;
      modalStatus.className = "status error";
      return;
    }
    renderCurrentLogo(data.currentLogo);
    if (!lang) populateLangFilter(data.availableLangs);
    renderLogoGrid(data);

    // Update logoStatus/logoSource in allItems so the badge reflects this
    // without needing a full library re-index. "set" only counts a local file -
    // a Plex-server-detected logo is shown in the modal but doesn't flip the badge.
    const item = allItems.find(i => i.ratingKey === currentRatingKey);
    if (item && data.currentLogo) {
      const isLocallySet = data.currentLogo.exists && data.currentLogo.source === "local_file";
      item.logoStatus = isLocallySet ? "set" : "unset";
      item.logoSource = data.currentLogo.exists ? data.currentLogo.source : null;
    }
  } catch (e) {
    modalStatus.textContent = `Error: ${e}`;
    modalStatus.className = "status error";
  }
}

function renderCurrentLogo(cl) {
  if (cl && cl.exists) {
    currentLogoBox.className = "current-logo-box";
    let sourceLabel, detail;
    if (cl.source === "local_file") {
      sourceLabel = "Local file";
      detail = `<code>${cl.path}</code>`;
    } else {
      sourceLabel = "From Plex server";
      detail = "Detected via Plex (no local clearlogo.png on disk — likely set through Plex Web, an agent, or another tool). Cached locally for fast reloads.";
    }
    currentLogoBox.innerHTML = `
      <img src="${cl.image_endpoint}?ts=${Date.now()}" alt="current logo">
      <div class="label"><strong>Currently applied</strong> &middot; <span class="source-pill ${cl.source}">${sourceLabel}</span><br>${detail}</div>`;
  } else {
    currentLogoBox.className = "current-logo-box none";
    currentLogoBox.textContent = cl && cl.path
      ? `No clear logo set yet — will be saved to: ${cl.path}`
      : "No clear logo currently set.";
  }
}

function populateLangFilter(langs) {
  langFilter.innerHTML = ['<option value="">All languages</option>']
    .concat(langs.map(l => {
      const label = l === "none" ? "Textless" : l;
      return `<option value="${l}">${label}</option>`;
    })).join("");
}

function renderLogoGrid(data) {
  if (!data.logos.length) {
    modalStatus.textContent = "No logos found for the selected filter.";
    logoGrid.innerHTML = "";
    return;
  }
  modalStatus.textContent = `${data.logos.length} logo(s) — click one to apply.`;
  modalStatus.className = "status";

  logoGrid.innerHTML = data.logos.map(l => `
    <div class="logo-card" data-url="${l.url}" data-source="${l.source}">
      <img src="${l.url}" alt="logo" loading="lazy">
      <div class="tag">
        <span class="source-badge ${l.source}">${l.source}</span>
        <span>${l.lang === "none" ? "textless" : l.lang}</span>
      </div>
    </div>`).join("");

  logoGrid.querySelectorAll(".logo-card").forEach(card => {
    card.addEventListener("click", () => applyLogo(card.dataset.url, card.dataset.source));
  });
}

async function applyLogo(url, source) {
  modalStatus.textContent = "Applying logo…";
  modalStatus.className = "status";
  try {
    const res = await fetch(`/api/item/${currentRatingKey}/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, source }),
    });
    const data = await res.json();
    if (data.error) {
      modalStatus.textContent = `Error: ${data.error}`;
      modalStatus.className = "status error";
      return;
    }
    modalStatus.textContent = `Applied! Saved to ${data.path}`;
    modalStatus.className = "status ok";
    await fetchLogos(false, langFilter.value);
    // Re-render grid so badge updates in place
    renderGrid();
  } catch (e) {
    modalStatus.textContent = `Error: ${e}`;
    modalStatus.className = "status error";
  }
}

// ---- Event wiring ----
if (librarySelect) {
  librarySelect.addEventListener("change", e => loadItems(e.target.value));
  searchBox.addEventListener("input", renderGrid);
  modalClose.addEventListener("click", () => modal.classList.add("hidden"));
  modal.addEventListener("click", e => { if (e.target === modal) modal.classList.add("hidden"); });
  refreshLibraryBtn.addEventListener("click", () => { if (currentSectionId) loadItems(currentSectionId, true); });
  langFilter.addEventListener("change", () => fetchLogos(false, langFilter.value));
  refreshLogosBtn.addEventListener("click", () => fetchLogos(true, langFilter.value));
  loadLibraries();
}
