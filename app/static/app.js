// Drift-Import front-end. Vanilla JS, no build step (keeps the Pi light).

const api = {
  async get(url) { const r = await fetch(url); if (!r.ok) throw new Error(await r.text()); return r.json(); },
  async send(method, url, body) {
    const r = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    return r.json();
  },
  post(url, body) { return this.send("POST", url, body); },
  put(url, body) { return this.send("PUT", url, body); },
  del(url) { return this.send("DELETE", url); },
};

function toast(msg, ms = 2500) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}
function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + " " + u[i];
}
function fmtDur(s) { if (!s) return ""; const m = Math.floor(s / 60), x = Math.round(s % 60); return `${m}:${String(x).padStart(2, "0")}`; }

// ---- job badge (runs on every page) ----------------------------------------
async function pollBadge() {
  try {
    const jobs = await api.get("/api/jobs?limit=20");
    const active = jobs.filter(j => j.status === "queued" || j.status === "running");
    const b = document.getElementById("jobBadge");
    if (b) b.textContent = active.length ? `⏳ ${active.length} job(s)` : "";
  } catch (e) { /* ignore */ }
}
setInterval(pollBadge, 3000); pollBadge();

// ============================ GALLERY =======================================
const selected = new Set();
let mediaCache = [];

function initGallery() { refreshDevices(); loadFilters(); loadMedia(); }

async function refreshDevices() {
  const el = document.getElementById("devices");
  el.textContent = "Scanning…";
  try {
    const devs = await api.get("/api/devices");
    if (!devs.length) { el.innerHTML = "<span class='hint'>No camera detected. Plug in the Drift XL.</span>"; return; }
    el.innerHTML = "";
    devs.forEach(d => {
      const row = document.createElement("div");
      row.className = "device";
      row.innerHTML = `<b>${d.label}</b>
        <span class='hint'>${d.file_count} files · ${fmtBytes(d.free_bytes)} free of ${fmtBytes(d.total_bytes)}</span>`;
      if (d.dcim_path) {
        const imp = document.createElement("button");
        imp.textContent = "Import"; imp.onclick = () => importDevice(d.dcim_path, false);
        const all = document.createElement("button");
        all.textContent = "Upload Everything"; all.onclick = () => importDevice(d.dcim_path, true);
        row.append(imp, all);
      }
      el.append(row);
    });
  } catch (e) { el.textContent = "Error scanning: " + e.message; }
}

async function importDevice(dcim, autoUpload) {
  try {
    const r = await api.post("/api/import-device", { dcim_path: dcim, auto_upload: autoUpload });
    toast(`Queued import of ${r.file_count} files${autoUpload ? " + upload" : ""}`);
  } catch (e) { toast("Import failed: " + e.message); }
}

async function loadFilters() {
  const months = await api.get("/api/media/months");
  const mf = document.getElementById("monthFilter");
  mf.innerHTML = "<option value=''>All dates</option>";
  months.forEach(m => {
    const o = document.createElement("option");
    o.value = `${m.year}-${m.month}`;
    o.textContent = `${m.year}-${String(m.month).padStart(2, "0")} (${m.count})`;
    mf.append(o);
  });
  const tags = await api.get("/api/tags");
  const tf = document.getElementById("tagFilter");
  tf.innerHTML = "<option value=''>All tags</option>";
  tags.forEach(t => { const o = document.createElement("option"); o.value = t.name; o.textContent = t.name; tf.append(o); });
}

async function loadMedia() {
  const grid = document.getElementById("grid");
  grid.textContent = "Loading…";
  const p = new URLSearchParams();
  const mv = document.getElementById("monthFilter").value;
  if (mv) { const [y, m] = mv.split("-"); p.set("year", y); p.set("month", m); }
  const tag = document.getElementById("tagFilter").value; if (tag) p.set("tag", tag);
  const st = document.getElementById("statusFilter").value; if (st) p.set("status", st);
  mediaCache = await api.get("/api/media?" + p.toString());
  grid.innerHTML = "";
  if (!mediaCache.length) { grid.innerHTML = "<span class='hint'>No media. Import from the camera above.</span>"; return; }
  mediaCache.forEach(m => grid.append(renderCard(m)));
  updateSelCount();
}

function renderCard(m) {
  const c = document.createElement("div");
  c.className = "card" + (selected.has(m.id) ? " sel" : "");
  const thumb = m.has_thumb ? `/api/media/${m.id}/thumb` : "";
  const ups = m.uploads.map(u => `<span class="pill up-${u.status}" title="${u.error || ''}">${u.status}</span>`).join("");
  c.innerHTML = `
    <input type="checkbox" class="pick" ${selected.has(m.id) ? "checked" : ""}>
    <img class="thumb" src="${thumb}" alt="" loading="lazy">
    <div class="meta">
      <div class="fn" title="${m.filename}">${m.filename}</div>
      <div class="sub">${(m.capture_time || "").slice(0, 16).replace("T", " ")} · ${fmtDur(m.duration_s)} · ${fmtBytes(m.size_bytes)}</div>
      <div>${m.tags.map(t => `<span class="pill" style="background:#2a3040">${t}</span>`).join("")}${ups}</div>
    </div>`;
  c.querySelector(".pick").onchange = e => { e.target.checked ? selected.add(m.id) : selected.delete(m.id); c.classList.toggle("sel", e.target.checked); updateSelCount(); };
  const img = c.querySelector(".thumb");
  if (m.kind === "video") img.onclick = () => playVideo(m.id);
  else img.onclick = () => window.open(`/api/media/${m.id}/stream`);
  return c;
}

function playVideo(id) {
  const dlg = document.getElementById("playerDlg");
  const v = document.getElementById("player");
  v.src = `/api/media/${id}/stream`;
  dlg.showModal();
}

function updateSelCount() { const e = document.getElementById("selCount"); if (e) e.textContent = `${selected.size} selected`; }
function selIds() { return [...selected]; }

async function uploadSelected() {
  if (!selected.size) return toast("Select clips first");
  const dests = await api.get("/api/destinations");
  if (!dests.length) return toast("Add a destination first");
  const names = dests.map(d => `${d.id}: ${d.name}${d.is_default ? " (default)" : ""}`).join("\n");
  const pick = prompt(`Destination IDs (comma-separated), blank = defaults:\n${names}`, "");
  let ids = null;
  if (pick && pick.trim()) ids = pick.split(",").map(s => parseInt(s.trim())).filter(n => !isNaN(n));
  try { await api.post("/api/upload", { media_ids: selIds(), destination_ids: ids }); toast("Upload queued"); }
  catch (e) { toast("Upload failed: " + e.message); }
}

function openTimestamp() { if (!selected.size) return toast("Select clips first"); document.getElementById("tsDlg").showModal(); }
async function submitTimestamp() {
  const mode = document.querySelector("input[name=tsmode]:checked").value;
  const body = { media_ids: selIds(), mode, write_metadata: document.getElementById("tsMeta").checked };
  if (mode === "set") {
    const v = document.getElementById("tsAbsolute").value;
    if (!v) return toast("Pick a date/time");
    body.absolute = v;
  } else {
    body.days = +document.getElementById("tsDays").value || 0;
    body.hours = +document.getElementById("tsHours").value || 0;
    body.minutes = +document.getElementById("tsMins").value || 0;
    body.seconds = +document.getElementById("tsSecs").value || 0;
  }
  try { await api.post("/api/timestamp", body); document.getElementById("tsDlg").close(); toast("Timestamp job queued"); }
  catch (e) { toast("Failed: " + e.message); }
}

async function mergeSelected() {
  const ids = selIds();
  if (ids.length < 2) return toast("Select 2+ clips (in capture order)");
  if (!confirm(`Merge ${ids.length} clips in displayed order?`)) return;
  try { await api.post("/api/merge", { media_ids: ids }); toast("Merge queued"); }
  catch (e) { toast("Merge failed: " + e.message); }
}

async function tagSelected() {
  if (!selected.size) return toast("Select clips first");
  const t = prompt("Tags (comma-separated):", "");
  if (!t) return;
  const tags = t.split(",").map(s => s.trim()).filter(Boolean);
  await api.post("/api/tags/assign", { media_ids: selIds(), tags });
  toast("Tagged"); loadFilters(); loadMedia();
}

async function addToAlbum() {
  if (!selected.size) return toast("Select clips first");
  const albums = await api.get("/api/albums");
  if (!albums.length) return toast("Create an album first (Albums page)");
  const list = albums.map(a => `${a.id}: ${a.name}`).join("\n");
  const pick = prompt(`Album id to add ${selected.size} clips to:\n${list}`, "");
  const aid = parseInt(pick);
  if (isNaN(aid)) return;
  const album = albums.find(a => a.id === aid);
  const merged = [...new Set([...(album ? album.item_ids : []), ...selIds()])];
  await api.post(`/api/albums/${aid}/items`, { media_ids: merged });
  toast("Added to album");
}

async function deleteSelected() {
  if (!selected.size) return toast("Select clips first");
  const delFile = confirm(`Delete ${selected.size} item(s) from the library.\n\nOK = also delete the underlying file.\nCancel = remove from library only.`);
  for (const id of selIds()) await api.del(`/api/media/${id}?delete_file=${delFile}`);
  selected.clear(); toast("Deleted"); loadMedia();
}

// ============================ DESTINATIONS ==================================
function initDestinations() { loadDestinations(); onTypeChange(); }
function onTypeChange() {
  const t = document.getElementById("dType").value;
  document.querySelectorAll(".net").forEach(el => el.classList.toggle("show", t !== "local"));
}
async function loadDestinations() {
  const el = document.getElementById("destList");
  const dests = await api.get("/api/destinations");
  if (!dests.length) { el.innerHTML = "<span class='hint'>No destinations yet.</span>"; return; }
  el.innerHTML = "";
  dests.forEach(d => {
    const row = document.createElement("div");
    row.className = "dest-row";
    row.innerHTML = `<div><b>${d.name}</b> <span class="hint">[${d.type}]${d.is_default ? " ★default" : ""}${d.enabled ? "" : " (disabled)"}</span>
      <br><span class="hint">${d.base_path} → ${d.path_template}</span></div>`;
    const actions = document.createElement("div"); actions.className = "row";
    const test = document.createElement("button"); test.className = "ghost"; test.textContent = "Test";
    test.onclick = async () => { test.textContent = "…"; const r = await api.post(`/api/destinations/${d.id}/test`); toast(r.ok ? "Connection OK" : "Failed: " + r.error); test.textContent = "Test"; };
    const edit = document.createElement("button"); edit.className = "ghost"; edit.textContent = "Edit"; edit.onclick = () => editDestination(d);
    const del = document.createElement("button"); del.className = "danger"; del.textContent = "Delete";
    del.onclick = async () => { if (confirm("Delete destination?")) { await api.del(`/api/destinations/${d.id}`); loadDestinations(); } };
    actions.append(test, edit, del); row.append(actions); el.append(row);
  });
}
function destForm() {
  return {
    name: document.getElementById("dName").value.trim(),
    type: document.getElementById("dType").value,
    host: document.getElementById("dHost").value.trim() || null,
    port: parseInt(document.getElementById("dPort").value) || null,
    username: document.getElementById("dUser").value.trim() || null,
    secret: document.getElementById("dSecret").value || null,
    base_path: document.getElementById("dBase").value.trim() || "/",
    path_template: document.getElementById("dTemplate").value.trim() || "{year}/{month:02d}",
    is_default: document.getElementById("dDefault").checked,
    enabled: document.getElementById("dEnabled").checked,
  };
}
async function saveDestination() {
  const body = destForm();
  if (!body.name) return toast("Name required");
  const id = document.getElementById("dId").value;
  try {
    if (id) await api.put(`/api/destinations/${id}`, body);
    else await api.post("/api/destinations", body);
    toast("Saved"); resetDestForm(); loadDestinations();
  } catch (e) { toast("Save failed: " + e.message); }
}
function editDestination(d) {
  document.getElementById("dId").value = d.id;
  document.getElementById("dName").value = d.name;
  document.getElementById("dType").value = d.type;
  document.getElementById("dHost").value = d.host || "";
  document.getElementById("dPort").value = d.port || "";
  document.getElementById("dUser").value = d.username || "";
  document.getElementById("dSecret").value = "";
  document.getElementById("dBase").value = d.base_path || "";
  document.getElementById("dTemplate").value = d.path_template || "";
  document.getElementById("dDefault").checked = d.is_default;
  document.getElementById("dEnabled").checked = d.enabled;
  document.getElementById("formTitle").textContent = "Edit destination";
  onTypeChange();
  window.scrollTo(0, document.body.scrollHeight);
}
function resetDestForm() {
  ["dId", "dName", "dHost", "dPort", "dUser", "dSecret", "dBase"].forEach(i => document.getElementById(i).value = "");
  document.getElementById("dTemplate").value = "{year}/{month:02d}";
  document.getElementById("dType").value = "local";
  document.getElementById("dDefault").checked = false;
  document.getElementById("dEnabled").checked = true;
  document.getElementById("formTitle").textContent = "Add destination";
  onTypeChange();
}

// ============================ ALBUMS ========================================
function initAlbums() { loadAlbums(); }
async function createAlbum() {
  const name = document.getElementById("albumName").value.trim();
  if (!name) return toast("Name required");
  try { await api.post("/api/albums", { name }); document.getElementById("albumName").value = ""; loadAlbums(); }
  catch (e) { toast("Failed: " + e.message); }
}
async function loadAlbums() {
  const el = document.getElementById("albumList");
  const albums = await api.get("/api/albums");
  const media = await api.get("/api/media");
  const byId = Object.fromEntries(media.map(m => [m.id, m]));
  if (!albums.length) { el.innerHTML = "<span class='hint'>No albums yet.</span>"; return; }
  el.innerHTML = "";
  albums.forEach(a => {
    const box = document.createElement("div"); box.className = "panel";
    box.innerHTML = `<h2>${a.name} <span class="hint">(${a.item_ids.length} clips)</span></h2>`;
    const list = document.createElement("div");
    a.item_ids.forEach((mid, idx) => {
      const m = byId[mid]; if (!m) return;
      const item = document.createElement("div"); item.className = "order-item";
      item.innerHTML = `<span>${idx + 1}.</span><span class="fn">${m.filename}</span>
        <span class="hint">${(m.capture_time || '').slice(0, 16).replace('T', ' ')}</span>`;
      const up = document.createElement("button"); up.className = "ghost"; up.textContent = "↑";
      up.onclick = () => moveAlbumItem(a, idx, -1);
      const dn = document.createElement("button"); dn.className = "ghost"; dn.textContent = "↓";
      dn.onclick = () => moveAlbumItem(a, idx, 1);
      item.append(up, dn); list.append(item);
    });
    box.append(list);
    const bar = document.createElement("div"); bar.className = "row";
    const merge = document.createElement("button"); merge.textContent = "Merge album";
    merge.onclick = async () => { await api.post("/api/merge", { album_id: a.id }); toast("Merge queued"); };
    const upl = document.createElement("button"); upl.textContent = "Upload album";
    upl.onclick = async () => { await api.post("/api/upload", { media_ids: a.item_ids }); toast("Upload queued"); };
    const del = document.createElement("button"); del.className = "danger"; del.textContent = "Delete album";
    del.onclick = async () => { if (confirm("Delete album?")) { await api.del(`/api/albums/${a.id}`); loadAlbums(); } };
    bar.append(merge, upl, del); box.append(bar); el.append(box);
  });
}
async function moveAlbumItem(album, idx, dir) {
  const ids = [...album.item_ids];
  const j = idx + dir;
  if (j < 0 || j >= ids.length) return;
  [ids[idx], ids[j]] = [ids[j], ids[idx]];
  await api.post(`/api/albums/${album.id}/items`, { media_ids: ids });
  loadAlbums();
}

// ============================ JOBS ==========================================
function initJobs() { loadJobs(); setInterval(loadJobs, 2000); }
async function loadJobs() {
  const el = document.getElementById("jobsTable");
  const jobs = await api.get("/api/jobs?limit=50");
  if (!jobs.length) { el.innerHTML = "<span class='hint'>No jobs yet.</span>"; return; }
  let html = "<table><tr><th>ID</th><th>Kind</th><th>Description</th><th>Status</th><th>Progress</th><th></th></tr>";
  jobs.forEach(j => {
    const pct = Math.round(j.progress * 100);
    const detail = j.error ? `<span style="color:#e05555">${j.error}</span>` : (j.detail || "");
    const cancel = (j.status === "queued" || j.status === "running")
      ? `<button class="ghost" onclick="cancelJob(${j.id})">Cancel</button>` : "";
    html += `<tr><td>${j.id}</td><td>${j.kind}</td><td>${j.description}<br><span class="hint">${detail}</span></td>
      <td>${j.status}</td><td><div class="prog"><span style="width:${pct}%"></span></div>${pct}%</td><td>${cancel}</td></tr>`;
  });
  el.innerHTML = html + "</table>";
}
async function cancelJob(id) { await api.post(`/api/jobs/${id}/cancel`); loadJobs(); }
