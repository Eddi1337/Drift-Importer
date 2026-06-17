// Drift-Import front-end. Vanilla JS, no build step.

const api = {
  async get(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
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

const appState = {
  settings: null,
  jobs: [],
  seenDevices: new Set(),
  autoImportTriggered: new Set(),
  folderBrowsers: {},
  cameraFiles: [],
  cameraFileSelection: new Set(),
  currentDcimPath: "",
  lastDeviceSignature: "",
  jobPollStarted: false,
  galleryPollers: [],
  jobsPoller: null,
  settingsPoller: null,
};

const selected = new Set();
let mediaCache = [];

function toast(msg, ms = 2800) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return n.toFixed(1) + " " + u[i];
}

function fmtDur(s) {
  if (!s) return "";
  const m = Math.floor(s / 60);
  const x = Math.round(s % 60);
  return `${m}:${String(x).padStart(2, "0")}`;
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escJs(value) {
  return String(value ?? "").replaceAll("\\", "\\\\").replaceAll("'", "\\'");
}

function appendPath(base, child) {
  if (!base) return child;
  if (base.includes("://")) {
    return base.replace(/\/$/, "") + "/" + child.replace(/^\//, "");
  }
  const left = base.endsWith("/") ? base.slice(0, -1) : base;
  const right = child.startsWith("/") ? child.slice(1) : child;
  return `${left}/${right}`;
}

function renderProgressRing(progress) {
  const pct = Math.max(0, Math.min(100, Math.round((progress || 0) * 100)));
  return `<span class="progress-chip"><span class="ring" style="--pct:${pct}"></span><span>${pct}%</span></span>`;
}

function renderJobState(status) {
  return `<span class="job-state ${esc(status)}"><span class="status-dot"></span>${esc(status)}</span>`;
}

async function loadSettings() {
  try {
    appState.settings = await api.get("/api/settings");
  } catch (e) {
    appState.settings = null;
  }
  renderDeviceDefaults();
  return appState.settings;
}

function ensureGlobalJobPolling() {
  if (appState.jobPollStarted) return;
  appState.jobPollStarted = true;
  refreshGlobalJobs();
  setInterval(refreshGlobalJobs, 3000);
}

async function refreshGlobalJobs() {
  try {
    appState.jobs = await api.get("/api/jobs?limit=20");
    renderJobBadge();
    renderLiveActivity();
    renderJobsPage();
  } catch (_e) {
    // Ignore transient failures.
  }
}

function renderJobBadge() {
  const b = document.getElementById("jobBadge");
  if (!b) return;
  const active = appState.jobs.filter(j => j.status === "queued" || j.status === "running");
  if (!active.length) {
    b.textContent = "";
    return;
  }
  const running = active.filter(j => j.status === "running").length;
  b.textContent = `${running ? "●" : "○"} ${active.length} active job${active.length === 1 ? "" : "s"}`;
}

function renderLiveActivity() {
  const el = document.getElementById("liveActivity");
  if (!el) return;
  const active = appState.jobs.filter(j => j.status === "queued" || j.status === "running");
  const uploads = active.filter(j => j.kind === "upload");
  const current = active[0];
  el.innerHTML = `
    <div class="live-card"><div class="hint">Active jobs</div><div class="value">${active.length}</div></div>
    <div class="live-card"><div class="hint">Uploads moving</div><div class="value">${uploads.length}</div></div>
    <div class="live-card"><div class="hint">Lead task</div><div class="value">${esc(current ? current.kind : "idle")}</div></div>
    <div class="live-card"><div class="hint">Latest detail</div><div>${esc(current ? (current.detail || current.description) : "Waiting for camera activity")}</div></div>
  `;
}

function renderDeviceDefaults() {
  const el = document.getElementById("deviceDefaults");
  if (!el || !appState.settings) return;
  const names = appState.settings.default_destination_ids?.length
    ? `destinations: ${appState.settings.default_destination_ids.join(", ")}`
    : "destinations: defaults unset";
  el.textContent =
    `Defaults: auto-import ${appState.settings.auto_import_on_connect ? "on" : "off"}, ` +
    `auto-upload ${appState.settings.auto_upload_on_import ? "on" : "off"}, ${names}.`;
}

// ============================ GALLERY =======================================

async function initGallery() {
  ensureGlobalJobPolling();
  await loadSettings();
  await Promise.all([refreshDevices(), loadFilters(), loadMedia()]);
  appState.galleryPollers.forEach(clearInterval);
  appState.galleryPollers = [
    setInterval(refreshDevices, 5000),
    setInterval(() => {
      if (!document.hidden) loadMedia();
    }, 7000),
  ];
}

async function refreshDevices() {
  const el = document.getElementById("devices");
  if (!el) return;
  if (!el.children.length) el.textContent = "Scanning…";
  try {
    const devs = await api.get("/api/devices");
    const signature = JSON.stringify(devs.map(d => ({
      path: d.path,
      dcim_path: d.dcim_path,
      file_count: d.file_count,
      free_bytes: d.free_bytes,
      total_bytes: d.total_bytes,
    })));
    if (signature === appState.lastDeviceSignature) return;
    appState.lastDeviceSignature = signature;
    if (!devs.length) {
      el.innerHTML = "<span class='hint'>No camera detected. Plug in the Drift camera.</span>";
      return;
    }
    el.innerHTML = "";
    devs.forEach(d => {
      const isNew = !appState.seenDevices.has(d.path);
      appState.seenDevices.add(d.path);
      const row = document.createElement("div");
      row.className = "device";
      row.innerHTML = `
        <div class="device-main">
          <b>${esc(d.label)}</b>
          <span class="hint">${d.file_count} files · ${fmtBytes(d.free_bytes)} free of ${fmtBytes(d.total_bytes)}</span>
        </div>
      `;
      if (d.dcim_path) {
        const actions = document.createElement("div");
        actions.className = "row";

        const defaults = document.createElement("button");
        defaults.textContent = "Use Defaults";
        defaults.onclick = () => importDevice(d.dcim_path);

        const view = document.createElement("button");
        view.className = "ghost";
        view.textContent = "View videos";
        view.onclick = () => loadCameraFiles(d.dcim_path);

        const imp = document.createElement("button");
        imp.className = "ghost";
        imp.textContent = "Import";
        imp.onclick = () => importDevice(d.dcim_path, false);

        const all = document.createElement("button");
        all.textContent = "Upload Everything";
        all.onclick = () => importDevice(d.dcim_path, true);

        actions.append(view, defaults, imp, all);
        row.append(actions);
      }
      el.append(row);

      if (
        isNew &&
        d.dcim_path &&
        appState.settings?.auto_import_on_connect &&
        !appState.autoImportTriggered.has(d.path)
      ) {
        appState.autoImportTriggered.add(d.path);
        importDevice(d.dcim_path, undefined, true);
      }
    });
  } catch (e) {
    el.textContent = "Error scanning: " + e.message;
  }
}

async function importDevice(dcim, autoUpload, quiet = false, paths = null, destinationIds = null) {
  try {
    const body = { dcim_path: dcim };
    if (typeof autoUpload === "boolean") body.auto_upload = autoUpload;
    if (paths?.length) body.paths = paths;
    if (destinationIds?.length) body.destination_ids = destinationIds;
    const r = await api.post("/api/import-device", body);
    if (!quiet) {
      toast(`Queued import of ${r.file_count} files${r.auto_upload ? " + upload" : ""}`);
    }
  } catch (e) {
    toast("Import failed: " + e.message);
  }
}

async function loadCameraFiles(dcimPath) {
  const list = document.getElementById("cameraFiles");
  const summary = document.getElementById("cameraFileSummary");
  if (!list || !summary) return;
  appState.currentDcimPath = dcimPath;
  appState.cameraFileSelection.clear();
  list.textContent = "Loading camera videos…";
  summary.textContent = "Scanning camera storage…";
  try {
    const data = await api.get(`/api/device-files?dcim_path=${encodeURIComponent(dcimPath)}`);
    appState.cameraFiles = data.files || [];
    renderCameraFiles();
  } catch (e) {
    list.textContent = "Unable to load camera videos: " + e.message;
    summary.textContent = "";
  }
}

function renderCameraFiles() {
  const list = document.getElementById("cameraFiles");
  const summary = document.getElementById("cameraFileSummary");
  if (!list || !summary) return;
  const totalBytes = appState.cameraFiles.reduce((sum, file) => sum + (file.size_bytes || 0), 0);
  summary.textContent = `${appState.cameraFiles.length} video file${appState.cameraFiles.length === 1 ? "" : "s"} on camera · ${appState.cameraFileSelection.size} selected · ${fmtBytes(totalBytes)}`;
  if (!appState.cameraFiles.length) {
    list.innerHTML = "<span class='hint'>No video files found on this camera.</span>";
    return;
  }
  list.innerHTML = appState.cameraFiles.map(file => `
    <label class="camera-file-row">
      <input type="checkbox" value="${esc(file.path)}" ${appState.cameraFileSelection.has(file.path) ? "checked" : ""} onchange="toggleCameraFile('${escJs(file.path)}', this.checked)">
      <span class="camera-file-name">${esc(file.relative_path || file.filename)}</span>
      <span class="hint">${esc(fmtBytes(file.size_bytes))}</span>
      <span class="hint">${esc((file.modified_at || "").slice(0, 16).replace("T", " "))}</span>
    </label>
  `).join("");
}

function toggleCameraFile(path, checked) {
  if (checked) appState.cameraFileSelection.add(path);
  else appState.cameraFileSelection.delete(path);
  renderCameraFiles();
}

function toggleCameraSelection(checked) {
  if (checked) appState.cameraFiles.forEach(file => appState.cameraFileSelection.add(file.path));
  else appState.cameraFileSelection.clear();
  renderCameraFiles();
}

async function importSelectedCameraFiles(autoUpload) {
  if (!appState.currentDcimPath) return toast("Load a camera video list first");
  const paths = [...appState.cameraFileSelection];
  if (!paths.length) return toast("Select camera videos first");
  let destinationIds = null;
  if (autoUpload) {
    destinationIds = await chooseDestinationIds();
    if (destinationIds === false) return;
  }
  await importDevice(appState.currentDcimPath, autoUpload, false, paths, destinationIds);
}

async function loadFilters() {
  const [months, tags] = await Promise.all([
    api.get("/api/media/months"),
    api.get("/api/tags"),
  ]);
  const mf = document.getElementById("monthFilter");
  const tf = document.getElementById("tagFilter");
  if (mf) {
    mf.innerHTML = "<option value=''>All dates</option>";
    months.forEach(m => {
      const o = document.createElement("option");
      o.value = `${m.year}-${m.month}`;
      o.textContent = `${m.year}-${String(m.month).padStart(2, "0")} (${m.count})`;
      mf.append(o);
    });
  }
  if (tf) {
    tf.innerHTML = "<option value=''>All tags</option>";
    tags.forEach(t => {
      const o = document.createElement("option");
      o.value = t.name;
      o.textContent = t.name;
      tf.append(o);
    });
  }
}

async function loadMedia() {
  const grid = document.getElementById("grid");
  if (!grid) return;
  grid.textContent = "Loading…";
  const p = new URLSearchParams();
  const mv = document.getElementById("monthFilter")?.value;
  if (mv) {
    const [y, m] = mv.split("-");
    p.set("year", y);
    p.set("month", m);
  }
  const tag = document.getElementById("tagFilter")?.value;
  const st = document.getElementById("statusFilter")?.value;
  if (tag) p.set("tag", tag);
  if (st) p.set("status", st);
  mediaCache = await api.get("/api/media?" + p.toString());
  // Verified-uploaded clips live in their own section so the library only
  // shows clips that still need attention. "Verified" = an upload that the
  // backend reported as fully done (not merely attempted/failed).
  const uploaded = mediaCache.filter(isVerifiedUploaded);
  const library = mediaCache.filter(m => !isVerifiedUploaded(m));
  grid.innerHTML = "";
  if (!library.length) {
    grid.innerHTML = "<span class='hint'>No clips awaiting upload. Import from the camera above.</span>";
  } else {
    library.forEach(m => grid.append(renderCard(m)));
  }
  renderUploaded(uploaded);
  updateSelCount();
}

function isVerifiedUploaded(m) {
  return (m.uploads || []).some(u => u.status === "done");
}

function renderUploaded(items) {
  const panel = document.getElementById("uploadedPanel");
  const grid = document.getElementById("uploadedGrid");
  const count = document.getElementById("uploadedCount");
  if (!panel || !grid) return;
  panel.hidden = items.length === 0;
  if (count) count.textContent = `${items.length} clip${items.length === 1 ? "" : "s"}`;
  grid.innerHTML = "";
  items.forEach(m => grid.append(renderCard(m, { uploaded: true })));
}

function renderCard(m, opts = {}) {
  const c = document.createElement("div");
  c.className = "card" + (selected.has(m.id) ? " sel" : "");
  const thumb = m.has_thumb ? `/api/media/${m.id}/thumb` : "";
  // In the Uploaded section only show the confirmed destinations; in the
  // library only show in-flight/failed attempts (confirmed ones moved out).
  const ups = m.uploads
    .filter(u => (opts.uploaded ? u.status === "done" : u.status !== "done"))
    .map(u => {
      const pct = u.total_bytes ? `${Math.round((u.bytes_uploaded / u.total_bytes) * 100)}%` : u.status;
      return `<span class="pill up-${esc(u.status)}" title="${esc(u.error || "")}">${renderProgressRing(u.progress || 0)} ${esc(pct)}</span>`;
    }).join("");
  c.innerHTML = `
    <input type="checkbox" class="pick" ${selected.has(m.id) ? "checked" : ""}>
    <img class="thumb" src="${thumb}" alt="" loading="lazy">
    <div class="meta">
      <div class="fn" title="${esc(m.filename)}">${esc(m.filename)}</div>
      <div class="sub">${esc((m.capture_time || "").slice(0, 16).replace("T", " "))} · ${esc(fmtDur(m.duration_s))} · ${esc(fmtBytes(m.size_bytes))}</div>
      <div class="status-row">${m.tags.map(t => `<span class="pill">${esc(t)}</span>`).join("")}${ups}</div>
    </div>`;
  c.querySelector(".pick").onchange = e => {
    e.target.checked ? selected.add(m.id) : selected.delete(m.id);
    c.classList.toggle("sel", e.target.checked);
    updateSelCount();
  };
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

function updateSelCount() {
  const e = document.getElementById("selCount");
  if (e) e.textContent = `${selected.size} selected`;
}

function selIds() { return [...selected]; }

async function uploadSelected() {
  if (!selected.size) return toast("Select clips first");
  const ids = await chooseDestinationIds();
  if (ids === false) return;
  try {
    await api.post("/api/upload", { media_ids: selIds(), destination_ids: ids });
    toast("Upload queued");
  } catch (e) {
    toast("Upload failed: " + e.message);
  }
}

async function chooseDestinationIds() {
  const dests = await api.get("/api/destinations");
  if (!dests.length) {
    toast("Add a destination first");
    return false;
  }
  const names = dests.map(d => `${d.id}: ${d.name}${d.is_default ? " (default)" : ""}`).join("\n");
  const pick = prompt(`Destination IDs (comma-separated), blank = defaults:\n${names}`, "");
  if (pick === null) return false;
  if (!pick.trim()) return null;
  return pick.split(",").map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
}

function openTimestamp() {
  if (!selected.size) return toast("Select clips first");
  document.getElementById("tsDlg").showModal();
}

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
  try {
    await api.post("/api/timestamp", body);
    document.getElementById("tsDlg").close();
    toast("Timestamp job queued");
  } catch (e) {
    toast("Failed: " + e.message);
  }
}

async function mergeSelected() {
  const ids = selIds();
  if (ids.length < 2) return toast("Select 2+ clips (in capture order)");
  const order = prompt(
    "Merge order: selected, date, or sequence",
    "date",
  );
  if (order === null) return;
  const cleanOrder = ["selected", "date", "sequence"].includes(order.trim())
    ? order.trim()
    : "selected";
  if (!confirm(`Merge ${ids.length} clips using ${cleanOrder} order?`)) return;
  try {
    await api.post("/api/merge", { media_ids: ids, order: cleanOrder });
    toast("Merge queued");
  } catch (e) {
    toast("Merge failed: " + e.message);
  }
}

async function tagSelected() {
  if (!selected.size) return toast("Select clips first");
  const t = prompt("Tags (comma-separated):", "");
  if (!t) return;
  const tags = t.split(",").map(s => s.trim()).filter(Boolean);
  await api.post("/api/tags/assign", { media_ids: selIds(), tags });
  toast("Tagged");
  loadFilters();
  loadMedia();
}

async function addToAlbum() {
  if (!selected.size) return toast("Select clips first");
  const albums = await api.get("/api/albums");
  if (!albums.length) return toast("Create an album first (Albums page)");
  const list = albums.map(a => `${a.id}: ${a.name}`).join("\n");
  const pick = prompt(`Album id to add ${selected.size} clips to:\n${list}`, "");
  const aid = parseInt(pick, 10);
  if (isNaN(aid)) return;
  const album = albums.find(a => a.id === aid);
  const merged = [...new Set([...(album ? album.item_ids : []), ...selIds()])];
  await api.post(`/api/albums/${aid}/items`, { media_ids: merged });
  toast("Added to album");
}

async function deleteSelected() {
  if (!selected.size) return toast("Select clips first");
  const ids = selIds();
  const items = ids.map(id => mediaCache.find(m => m.id === id)).filter(Boolean);
  const hasCameraFiles = items.some(item => item.source === "device");
  const delFile = hasCameraFiles
    ? false
    : confirm(`Delete ${selected.size} item(s) from the library.\n\nOK = also delete the underlying file.\nCancel = remove from library only.`);
  if (hasCameraFiles) {
    toast("Camera files are protected; removing selected items from the library only.");
  }
  for (const id of ids) await api.del(`/api/media/${id}?delete_file=${delFile}`);
  selected.clear();
  toast("Deleted");
  loadMedia();
}

// ============================ DESTINATIONS ==================================

const destinationTypeMap = {
  local: {
    port: "",
    host: false,
    user: false,
    secret: false,
    basePlaceholder: "/mnt/nas/camera",
    hint: "Use a directory already mounted on the Pi or Docker host.",
  },
  nfs: {
    port: "2049",
    host: true,
    user: false,
    secret: false,
    basePlaceholder: "/mnt/nfs/camera or /export/camera",
    hint: "NFS uses server/export settings and does not need a password. The share must be mounted on the Pi/container host for uploads.",
  },
  smb: {
    port: "445",
    host: true,
    user: true,
    secret: true,
    basePlaceholder: "/mnt/smb/camera",
    hint: "SMB/CIFS uses server, username, password, and a mounted base path on the Pi/container host.",
  },
  nextcloud: {
    port: "",
    host: false,
    user: true,
    secret: true,
    basePlaceholder: "https://cloud/remote.php/dav/files/USER",
    hint: "Use your WebDAV URL and an app password. Base path should be the full DAV user root.",
  },
  sftp: {
    port: "22",
    host: true,
    user: true,
    secret: true,
    basePlaceholder: "/remote/camera",
    hint: "Connect to an SSH/SFTP server with hostname, username, password, and a remote base folder.",
  },
  rsync: {
    port: "22",
    host: true,
    user: true,
    secret: false,
    basePlaceholder: "/remote/camera",
    hint: "Rsync over SSH uses host, port, username, and an SSH-accessible remote path. It should use SSH keys rather than a password.",
  },
};

function initDestinations() {
  ensureGlobalJobPolling();
  loadDestinations();
  onTypeChange();
  clearFolderBrowser("folderBrowser");
  hideDestForm();
}

function showDestForm() {
  const panel = document.getElementById("destFormPanel");
  if (!panel) return;
  panel.hidden = false;
  updateBaseStatus();
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function hideDestForm() {
  const panel = document.getElementById("destFormPanel");
  if (panel) panel.hidden = true;
}

function onTypeChange(applyDefaultPort = false) {
  const t = document.getElementById("dType").value;
  const cfg = destinationTypeMap[t];
  if (!cfg) return;
  if (applyDefaultPort) document.getElementById("dPort").value = cfg.port;
  else if (cfg.port) document.getElementById("dPort").value = document.getElementById("dPort").value || cfg.port;
  document.getElementById("dMethodHint").textContent = cfg.hint;
  document.getElementById("dBase").placeholder = cfg.basePlaceholder;
  document.querySelector(".field-host").classList.toggle("show", cfg.host);
  document.querySelector(".field-port").classList.toggle("show", cfg.host);
  document.querySelector(".field-user").classList.toggle("show", cfg.user);
  document.querySelector(".field-secret").classList.toggle("show", cfg.secret);
}

async function loadDestinations() {
  const el = document.getElementById("destList");
  if (!el) return;
  const dests = await api.get("/api/destinations");
  if (!dests.length) {
    el.innerHTML = "<span class='hint'>No destinations yet.</span>";
    return;
  }
  el.innerHTML = "";
  dests.forEach(d => {
    const row = document.createElement("div");
    row.className = "dest-row";
    row.innerHTML = `
      <div class="dest-body">
        <div><b>${esc(d.name)}</b> <span class="hint">[${esc(d.type)}]${d.is_default ? " default" : ""}${d.enabled ? "" : " (disabled)"}</span></div>
        <span class="hint">${esc(d.base_path)} → ${esc(d.path_template)}</span>
        <span class="hint">${renderDestinationStorageText(d)}</span>
        <div class="folder-window compact" id="destFolders-${d.id}">Browse to see available folders.</div>
      </div>
    `;
    const actions = document.createElement("div");
    actions.className = "row";
    const test = document.createElement("button");
    test.className = "ghost";
    test.textContent = "Test";
    test.onclick = async () => {
      test.textContent = "…";
      const r = await api.post(`/api/destinations/${d.id}/test`);
      toast(r.ok ? "Connection OK" : "Failed: " + r.error);
      test.textContent = "Test";
    };
    const browse = document.createElement("button");
    browse.className = "ghost";
    browse.textContent = "Browse";
    browse.onclick = () => browseDestinationFolders(d.id, `destFolders-${d.id}`, "");
    const edit = document.createElement("button");
    edit.className = "ghost";
    edit.textContent = "Edit";
    edit.onclick = () => editDestination(d);
    const del = document.createElement("button");
    del.className = "danger";
    del.textContent = "Delete";
    del.onclick = async () => {
      if (confirm("Delete destination?")) {
        await api.del(`/api/destinations/${d.id}`);
        loadDestinations();
      }
    };
    actions.append(test, browse, edit, del);
    row.append(actions);
    el.append(row);
  });
}

function renderDestinationStorageText(d) {
  const storage = d.storage || {};
  const uploaded = storage.bytes_uploaded_by_app ?? 0;
  const free = storage.free_bytes;
  const total = storage.total_bytes;
  const used = storage.used_bytes;
  const parts = [`App uploaded ${fmtBytes(uploaded)}`];
  if (free !== null && free !== undefined) parts.push(`${fmtBytes(free)} free`);
  if (used !== null && used !== undefined) parts.push(`${fmtBytes(used)} used`);
  if (total !== null && total !== undefined) parts.push(`${fmtBytes(total)} total`);
  if (storage.error) parts.push(`storage check failed`);
  return parts.join(" · ");
}

function destForm() {
  return {
    name: document.getElementById("dName").value.trim(),
    type: document.getElementById("dType").value,
    host: document.getElementById("dHost").value.trim() || null,
    port: parseInt(document.getElementById("dPort").value, 10) || null,
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
    toast("Saved");
    resetDestForm();
    hideDestForm();
    loadDestinations();
  } catch (e) {
    toast("Save failed: " + e.message);
  }
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
  clearFolderBrowser("folderBrowser");
  showDestForm();
}

function resetDestForm() {
  ["dId", "dName", "dHost", "dPort", "dUser", "dSecret", "dBase"].forEach(i => { document.getElementById(i).value = ""; });
  document.getElementById("dTemplate").value = "{year}/{month:02d}";
  document.getElementById("dType").value = "local";
  document.getElementById("dDefault").checked = false;
  document.getElementById("dEnabled").checked = true;
  document.getElementById("formTitle").textContent = "Add destination";
  onTypeChange();
  clearFolderBrowser("folderBrowser");
  updateBaseStatus();
}

async function testAndBrowseDestination() {
  const existingId = document.getElementById("dId").value;
  const config = destForm();
  if (!config.base_path) return toast("Base path required before browsing");
  const test = existingId
    ? await api.post(`/api/destinations/${existingId}/test`)
    : await api.post("/api/destinations/preview/test", config);
  if (!test.ok) {
    toast("Connection failed: " + test.error);
    return;
  }
  toast("Connection OK");
  browseDestinationFolders(existingId || null, "folderBrowser", "", config);
}

async function browseDestinationFolders(destinationId, targetId, path = "", config = null) {
  const target = document.getElementById(targetId);
  if (!target) return;
  target.textContent = "Loading folders…";
  const browserConfig = destinationId ? null : (config || destForm());
  const basePath = browserConfig?.base_path || document.getElementById("dBase")?.value.trim() || "";
  try {
    const r = destinationId
      ? await api.get(`/api/destinations/${destinationId}/folders?path=${encodeURIComponent(path)}`)
      : await api.post(`/api/destinations/preview/folders?path=${encodeURIComponent(path)}`, browserConfig);
    appState.folderBrowsers[targetId] = {
      destinationId,
      config: browserConfig,
      basePath,
      path: r.path || "",
      lastFolders: r.folders || [],
      selectedPath: appState.folderBrowsers[targetId]?.selectedPath,
    };
    renderFolderBrowser(targetId, r.folders);
  } catch (e) {
    target.textContent = "Unable to load folders: " + e.message;
  }
}

function browseFolderTarget(targetId, path = "") {
  const state = appState.folderBrowsers[targetId];
  if (!state) return;
  browseDestinationFolders(state.destinationId, targetId, path, state.config);
}

function renderFolderBrowser(targetId, folders) {
  const target = document.getElementById(targetId);
  if (!target) return;
  const state = appState.folderBrowsers[targetId] || { destinationId: null, path: "" };
  const crumbs = state.path ? state.path.split("/").filter(Boolean) : [];
  const parent = crumbs.slice(0, -1).join("/");
  const browseCall = nextPath => `browseFolderTarget('${escJs(targetId)}','${escJs(nextPath)}')`;
  const breadcrumbHtml = [
    `<button class="folder-crumb" onclick="${browseCall("")}">Root</button>`,
    ...crumbs.map((part, idx) => {
      const crumbPath = crumbs.slice(0, idx + 1).join("/");
      return `<button class="folder-crumb" onclick="${browseCall(crumbPath)}">${esc(part)}</button>`;
    }),
  ].join("<span class='folder-sep'>/</span>");
  const selectedPath = state.selectedPath;
  const folderRows = folders.length
    ? folders.map(name => {
      const childPath = appendPath(state.path, name);
      const isSel = selectedPath != null && childPath === selectedPath;
      return `
        <div class="folder-row${isSel ? " selected" : ""}">
          <button class="folder-open" onclick="${browseCall(childPath)}">Open</button>
          <button class="folder-name" onclick="${browseCall(childPath)}">${esc(name)}</button>
          <button class="folder-select" onclick="applyFolderChoice('${escJs(targetId)}','${escJs(childPath)}')">${isSel ? "✓ Selected" : "Select"}</button>
        </div>
      `;
    }).join("")
    : "<div class='folder-empty'>No child folders at this level.</div>";
  const currentSelected = selectedPath != null && (selectedPath || "") === (state.path || "");
  target.innerHTML = `
    <div class="folder-toolbar">
      <div class="folder-crumbs">${breadcrumbHtml}</div>
      <div class="row">
        ${state.path ? `<button class="ghost" onclick="${browseCall(parent)}">Up</button>` : ""}
        <button class="ghost" onclick="applyFolderChoice('${escJs(targetId)}','${escJs(state.path)}')">${currentSelected ? "✓ This folder selected" : "Use this folder"}</button>
      </div>
    </div>
    <div class="folder-list">${folderRows}</div>
  `;
}

function applyFolderChoice(targetId, path) {
  if (targetId !== "folderBrowser") return;
  const base = document.getElementById("dBase");
  const state = appState.folderBrowsers[targetId] || {};
  const root = state.basePath || base.value.trim();
  base.value = path ? appendPath(root, path) : root;
  state.selectedPath = path;
  base.classList.add("just-set");
  setTimeout(() => base.classList.remove("just-set"), 1200);
  updateBaseStatus(true);
  renderFolderBrowser(targetId, state.lastFolders || []);
  toast(`Base path set to ${base.value} — click Save to store it`);
}

function updateBaseStatus(picked = false) {
  const el = document.getElementById("dBaseStatus");
  const base = document.getElementById("dBase");
  if (!el || !base) return;
  const value = base.value.trim();
  if (!value) {
    el.textContent = "No base path set yet.";
    el.classList.remove("ok");
    return;
  }
  el.textContent = picked
    ? `✓ Selected: ${value} — not saved yet. Click Save to store this destination.`
    : `Current base path: ${value} — click Save to store changes.`;
  el.classList.toggle("ok", picked);
}

function clearFolderBrowser(targetId) {
  const el = document.getElementById(targetId);
  if (el) el.textContent = "No folder listing loaded yet.";
  delete appState.folderBrowsers[targetId];
}

// ============================ ALBUMS ========================================

function initAlbums() {
  ensureGlobalJobPolling();
  loadAlbums();
}

async function createAlbum() {
  const name = document.getElementById("albumName").value.trim();
  if (!name) return toast("Name required");
  try {
    await api.post("/api/albums", { name });
    document.getElementById("albumName").value = "";
    loadAlbums();
  } catch (e) {
    toast("Failed: " + e.message);
  }
}

async function loadAlbums() {
  const el = document.getElementById("albumList");
  if (!el) return;
  const [albums, media] = await Promise.all([api.get("/api/albums"), api.get("/api/media")]);
  const byId = Object.fromEntries(media.map(m => [m.id, m]));
  if (!albums.length) {
    el.innerHTML = "<span class='hint'>No albums yet.</span>";
    return;
  }
  el.innerHTML = "";
  albums.forEach(a => {
    const box = document.createElement("div");
    box.className = "panel";
    box.innerHTML = `<h2>${esc(a.name)} <span class="hint">(${a.item_ids.length} clips)</span></h2>`;
    const list = document.createElement("div");
    a.item_ids.forEach((mid, idx) => {
      const m = byId[mid];
      if (!m) return;
      const item = document.createElement("div");
      item.className = "order-item";
      item.innerHTML = `<span>${idx + 1}.</span><span class="fn">${esc(m.filename)}</span><span class="hint">${esc((m.capture_time || "").slice(0, 16).replace("T", " "))}</span>`;
      const up = document.createElement("button");
      up.className = "ghost";
      up.textContent = "↑";
      up.onclick = () => moveAlbumItem(a, idx, -1);
      const dn = document.createElement("button");
      dn.className = "ghost";
      dn.textContent = "↓";
      dn.onclick = () => moveAlbumItem(a, idx, 1);
      item.append(up, dn);
      list.append(item);
    });
    box.append(list);
    const bar = document.createElement("div");
    bar.className = "row";
    const merge = document.createElement("button");
    merge.textContent = "Merge album";
    merge.onclick = async () => {
      await api.post("/api/merge", { album_id: a.id, order: "date" });
      toast("Merge queued");
    };
    const upl = document.createElement("button");
    upl.textContent = "Upload album";
    upl.onclick = async () => {
      await api.post("/api/upload", { media_ids: a.item_ids });
      toast("Upload queued");
    };
    const del = document.createElement("button");
    del.className = "danger";
    del.textContent = "Delete album";
    del.onclick = async () => {
      if (confirm("Delete album?")) {
        await api.del(`/api/albums/${a.id}`);
        loadAlbums();
      }
    };
    bar.append(merge, upl, del);
    box.append(bar);
    el.append(box);
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

function initJobs() {
  ensureGlobalJobPolling();
  refreshGlobalJobs();
}

function renderJobsPage() {
  const summary = document.getElementById("jobSummary");
  const el = document.getElementById("jobsTable");
  if (!summary || !el) return;
  const jobs = appState.jobs;
  const active = jobs.filter(j => j.status === "queued" || j.status === "running");
  const failed = jobs.filter(j => j.status === "error");
  const completed = jobs.filter(j => j.status === "done");
  summary.innerHTML = `
    <div class="live-card"><div class="hint">Queued / running</div><div class="value">${active.length}</div></div>
    <div class="live-card"><div class="hint">Completed</div><div class="value">${completed.length}</div></div>
    <div class="live-card"><div class="hint">Errors</div><div class="value">${failed.length}</div></div>
    <div class="live-card"><div class="hint">Latest</div><div>${esc(jobs[0] ? jobs[0].description : "No jobs yet")}</div></div>
  `;
  if (!jobs.length) {
    el.innerHTML = "<span class='hint'>No jobs yet.</span>";
    return;
  }
  let html = "<table><tr><th>ID</th><th>Kind</th><th>Description</th><th>Status</th><th>Progress</th><th></th></tr>";
  jobs.forEach(j => {
    const pct = Math.round(j.progress * 100);
    const detail = j.error ? `<span style="color:#ffaea2">${esc(j.error)}</span>` : esc(j.detail || "");
    const cancel = (j.status === "queued" || j.status === "running")
      ? `<button class="ghost" onclick="cancelJob(${j.id})">Cancel</button>` : "";
    html += `<tr><td>${j.id}</td><td>${esc(j.kind)}</td><td>${esc(j.description)}<br><span class="hint">${detail}</span></td><td>${renderJobState(j.status)}</td><td><div class="prog"><span style="width:${pct}%"></span></div>${pct}%</td><td>${cancel}</td></tr>`;
  });
  el.innerHTML = html + "</table>";
}

async function cancelJob(id) {
  await api.post(`/api/jobs/${id}/cancel`);
  refreshGlobalJobs();
}

// ============================ STATS =========================================

function initStats() {
  ensureGlobalJobPolling();
  loadStats();
}

async function loadStats() {
  const overview = document.getElementById("statsOverview");
  const destinations = document.getElementById("statsDestinations");
  if (!overview || !destinations) return;
  overview.textContent = "Loading…";
  destinations.textContent = "Loading…";
  try {
    const stats = await api.get("/api/stats");
    const data = stats.overview || {};
    overview.innerHTML = `
      <div class="live-card"><div class="hint">Uploaded clips</div><div class="value">${data.uploaded_clip_count || 0}</div></div>
      <div class="live-card"><div class="hint">Uploaded size</div><div class="value">${fmtBytes(data.uploaded_bytes || 0)}</div></div>
      <div class="live-card"><div class="hint">Average upload time</div><div class="value">${fmtDurationText(data.average_upload_duration_s)}</div></div>
      <div class="live-card"><div class="hint">Average throughput</div><div class="value">${fmtBytes(data.average_throughput_bps || 0)}/s</div></div>
    `;
    renderStatsDestinations(stats.destinations || []);
  } catch (e) {
    overview.textContent = "Unable to load stats: " + e.message;
    destinations.textContent = "";
  }
}

function fmtDurationText(seconds) {
  if (!seconds) return "n/a";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

function renderStatsDestinations(rows) {
  const el = document.getElementById("statsDestinations");
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = "<span class='hint'>No destinations configured.</span>";
    return;
  }
  let html = "<table><tr><th>Destination</th><th>Uploads</th><th>App storage</th><th>Destination storage</th><th>Average</th></tr>";
  rows.forEach(row => {
    const storage = row.storage || {};
    html += `<tr>
      <td><b>${esc(row.name)}</b><br><span class="hint">${esc(row.type)}</span></td>
      <td>${row.uploaded_clip_count || 0}</td>
      <td>${fmtBytes(row.uploaded_bytes || 0)}</td>
      <td>${renderDestinationStorageText({ storage })}</td>
      <td>${fmtDurationText(row.average_upload_duration_s)} · ${fmtBytes(row.average_throughput_bps || 0)}/s</td>
    </tr>`;
  });
  el.innerHTML = html + "</table>";
}

// ============================ SETTINGS ======================================

function initSettings() {
  ensureGlobalJobPolling();
  loadSettingsPage();
  clearInterval(appState.settingsPoller);
  appState.settingsPoller = setInterval(loadUploadLedger, 4000);
}

async function loadSettingsPage() {
  const [settings, dests] = await Promise.all([loadSettings(), api.get("/api/destinations")]);
  document.getElementById("sAutoImport").checked = !!settings.auto_import_on_connect;
  document.getElementById("sAutoUpload").checked = !!settings.auto_upload_on_import;
  document.getElementById("sHaPrefix").value = settings.ha_entity_prefix || "drift_import";
  document.getElementById("sHaUrl").value = settings.ha_base_url || "";
  document.getElementById("sHaToken").value = settings.ha_token || "";
  renderSettingsDestinations(dests, settings.default_destination_ids || []);
  loadUploadLedger();
}

function renderSettingsDestinations(dests, selectedIds) {
  const el = document.getElementById("settingsDestinations");
  if (!el) return;
  if (!dests.length) {
    el.innerHTML = "<span class='hint'>No destinations configured yet.</span>";
    return;
  }
  const set = new Set(selectedIds);
  el.innerHTML = `<div class="check-list">${
    dests.map(d => `
      <label class="check-row choice-card">
        <input type="checkbox" value="${d.id}" ${set.has(d.id) ? "checked" : ""}>
        <span><b>${esc(d.name)}</b><small>[${esc(d.type)}]${d.enabled ? "" : " disabled"}</small></span>
      </label>
    `).join("")
  }</div>`;
}

async function saveSettings() {
  const selectedDestinations = [...document.querySelectorAll("#settingsDestinations input:checked")]
    .map(el => parseInt(el.value, 10))
    .filter(n => !isNaN(n));
  const body = {
    auto_import_on_connect: document.getElementById("sAutoImport").checked,
    auto_upload_on_import: document.getElementById("sAutoUpload").checked,
    default_destination_ids: selectedDestinations,
    ha_base_url: document.getElementById("sHaUrl").value.trim(),
    ha_token: document.getElementById("sHaToken").value.trim(),
    ha_entity_prefix: document.getElementById("sHaPrefix").value.trim() || "drift_import",
  };
  await api.put("/api/settings", body);
  await loadSettings();
  toast("Settings saved");
  loadUploadLedger();
}

async function loadUploadLedger() {
  const el = document.getElementById("uploadLedger");
  if (!el) return;
  const rows = await api.get("/api/uploaded-clips?limit=200");
  if (!rows.length) {
    el.innerHTML = "<span class='hint'>No uploaded clips tracked yet.</span>";
    return;
  }
  let html = "<table><tr><th>Status</th><th>Filename</th><th>Hash</th><th>Progress</th><th>Remote path</th></tr>";
  rows.forEach(r => {
    const progress = r.size_bytes ? r.bytes_uploaded / r.size_bytes : 0;
    html += `<tr>
      <td>${renderJobState(r.status)}</td>
      <td>${esc(r.filename)}</td>
      <td><code>${esc(r.checksum.slice(0, 12))}</code></td>
      <td>${renderProgressRing(progress)} ${esc(fmtBytes(r.bytes_uploaded))} / ${esc(fmtBytes(r.size_bytes))}</td>
      <td><span class="hint">${esc(r.remote_path || r.temp_remote_path || "")}</span></td>
    </tr>`;
  });
  el.innerHTML = html + "</table>";
}
