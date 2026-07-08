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
  expandedJobs: new Set(),
  jobLogs: {},
  folderBrowsers: {},
  fileExplorer: {
    cameraRoot: "",
    cameraPath: "",
    destinationId: null,
    destinationPath: "",
    destinationLocalActions: false,
    pendingAction: null,
  },
  cameraFiles: [],
  cameraFileSelection: new Set(),
  cameraBrowserPath: "",
  currentDcimPath: "",
  lastDeviceSignature: "",
  lastMediaSignature: "",
  lastRecentUploadSignature: "",
  lastLiveActivitySignature: "",
  jobPollStarted: false,
  jobFilter: null,
  galleryPollers: [],
  jobsPoller: null,
  jobTimer: null,
  statsPoller: null,
  settingsPoller: null,
  systemHistory: {
    cpu: [],
    rx: [],
    tx: [],
    filesystems: {},
  },
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

// Network throughput, shown in bits/sec (bps/kbps/Mbps/Gbps) like routers and
// speed tests report it — input is bytes/sec.
function fmtBitrate(bytesPerSec) {
  let bits = (Number(bytesPerSec) || 0) * 8;
  if (bits < 1000) return Math.round(bits) + " bps";
  const u = ["kbps", "Mbps", "Gbps", "Tbps"];
  let i = -1;
  while (bits >= 1000 && i < u.length - 1) {
    bits /= 1000;
    i++;
  }
  return bits.toFixed(bits < 10 ? 2 : 1) + " " + u[i];
}

function fmtDur(s) {
  if (!s) return "";
  const m = Math.floor(s / 60);
  const x = Math.round(s % 60);
  return `${m}:${String(x).padStart(2, "0")}`;
}

function fmtMonthYear(year, month) {
  const date = new Date(Number(year), Number(month) - 1, 1);
  return date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function fmtDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16).replace("T", " ");
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtElapsed(start, end = null) {
  if (!start) return "Not started";
  const startDate = new Date(start);
  const endDate = end ? new Date(end) : new Date();
  if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) return "Unknown";
  const total = Math.max(0, Math.floor((endDate.getTime() - startDate.getTime()) / 1000));
  const hours = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${String(mins).padStart(2, "0")}m`;
  if (mins) return `${mins}m ${String(secs).padStart(2, "0")}s`;
  return `${secs}s`;
}

function humanTemplate(template) {
  return (template || "{year}/{month:02d}")
    .replaceAll("{year}", "Year")
    .replaceAll("{month:02d}", "Month")
    .replaceAll("{month}", "Month")
    .replaceAll("{day:02d}", "Day")
    .replaceAll("{day}", "Day")
    .replaceAll("{hour:02d}", "Hour")
    .replaceAll("{hour}", "Hour")
    .replaceAll("/", " / ");
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escJs(value) {
  return String(value ?? "")
    .replaceAll("\\", "\\\\")
    .replaceAll("'", "\\'")
    .replaceAll('"', "&quot;")
    .replaceAll("\n", "\\n")
    .replaceAll("\r", "\\r");
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

function renderProgressBar(progress, label = "") {
  const pct = Math.max(0, Math.min(100, Math.round((progress || 0) * 100)));
  return `
    <div class="progress-stack">
      <div class="prog job-progress"><span style="width:${pct}%"></span></div>
      <div class="progress-label">${label ? `${esc(label)} · ` : ""}${pct}%</div>
    </div>
  `;
}

function renderJobState(status) {
  return `<span class="job-state ${esc(status)}"><span class="status-dot"></span>${esc(status)}</span>`;
}

function isActiveJob(job) {
  return job.status === "queued" || job.status === "running";
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
    const statusParam = appState.jobFilter ? `&status=${encodeURIComponent(appState.jobFilter)}` : "";
    const [jobs, overview] = await Promise.all([
      api.get(`/api/jobs?limit=100${statusParam}`),
      api.get("/api/jobs/overview"),
    ]);
    appState.jobs = jobs;
    appState.jobsOverview = overview;
    renderJobBadge();
    renderLiveActivity();
    renderJobsPage();
    refreshExpandedJobLogs();
  } catch (_e) {
    // Ignore transient failures.
  }
}

function setJobFilter(filter) {
  appState.jobFilter = appState.jobFilter === filter ? null : filter;
  refreshGlobalJobs();
}

function renderJobBadge() {
  const b = document.getElementById("jobBadge");
  if (!b) return;
  const o = appState.jobsOverview;
  if (!o || !o.active) {
    b.innerHTML = "";
    return;
  }
  b.innerHTML = `
    <span class="badge-text">${o.running ? "●" : "○"} ${o.active} job${o.active === 1 ? "" : "s"}</span>
    <span class="prog menu-progress"><span style="width:${o.percent}%"></span></span>
    <span class="badge-pct">${o.percent}%</span>
  `;
}

function renderLiveActivity() {
  const el = document.getElementById("liveActivity");
  if (!el) return;
  const active = appState.jobs.filter(isActiveJob);
  const uploads = active.filter(j => j.kind === "upload");
  const current = active[0];
  const rows = active.slice(0, 4).map(job => `
    <div class="live-job">
      <div>
        <strong>${esc(job.kind)}</strong>
        <span class="hint">${esc(job.description || job.detail || "Background job")}</span>
      </div>
      ${renderProgressBar(job.progress || 0, job.status)}
    </div>
  `).join("");
  el.innerHTML = `
    <div class="live-card"><div class="hint">Active jobs</div><div class="value">${active.length}</div></div>
    <div class="live-card"><div class="hint">Uploads running</div><div class="value">${uploads.length}</div></div>
    <div class="live-card"><div class="hint">Lead task</div><div class="value">${esc(current ? current.kind : "idle")}</div></div>
    <div class="live-card live-card-wide">
      <div class="hint">Current progress</div>
      ${rows || "<span class='hint'>No active upload or processing jobs.</span>"}
    </div>
  `;
}

function setLiveText(key, value) {
  const node = document.querySelector(`[data-live="${key}"]`);
  if (!node) return;
  const text = String(value ?? "");
  if (node.textContent !== text) node.textContent = text;
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
  await refreshDevices();
  appState.galleryPollers.forEach(clearInterval);
  appState.galleryPollers = [
    setInterval(refreshDevices, 5000),
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
    if (signature === appState.lastDeviceSignature) {
      if (appState.currentDcimPath && !appState.cameraBrowserPath) {
        await loadCameraFiles(appState.currentDcimPath, true);
      }
      return;
    }
    appState.lastDeviceSignature = signature;
    if (!devs.length) {
      appState.currentDcimPath = "";
      appState.cameraFiles = [];
      appState.cameraFileSelection.clear();
      el.innerHTML = "<span class='hint'>No camera connected.</span>";
      const summary = document.getElementById("cameraFileSummary");
      const list = document.getElementById("cameraFiles");
      if (summary) summary.textContent = "No camera connected.";
      if (list) list.textContent = "No camera connected.";
      return;
    }
    const primary = devs.find(d => d.dcim_path) || devs[0];
    el.innerHTML = `
      <div class="camera-connected">
        <div>
          <strong>${esc(primary.label)}</strong>
          <span class="hint">${primary.file_count} media files · ${fmtBytes(primary.free_bytes)} free of ${fmtBytes(primary.total_bytes)}</span>
        </div>
      </div>
    `;
    // Auto-import-on-connect is handled server-side by the device monitor so it
    // fires exactly once per physical connection (not on every page load/poll).
    if (primary.dcim_path && primary.dcim_path !== appState.currentDcimPath) {
      await loadCameraFiles(primary.dcim_path);
    }
  } catch (e) {
    el.textContent = "Error scanning: " + e.message;
  }
}

async function importDevice(dcim, autoUpload, quiet = false, paths = null, destinationIds = null, opts = {}) {
  try {
    const body = { dcim_path: dcim };
    if (typeof autoUpload === "boolean") body.auto_upload = autoUpload;
    if (paths?.length) body.paths = paths;
    if (destinationIds?.length) body.destination_ids = destinationIds;
    if (opts.groupUploadsByMonth) body.group_uploads_by_month = true;
    const r = await api.post("/api/import-device", body);
    if (!quiet) {
      if (r.already_queued) {
        toast(`Import of ${r.file_count} files is already queued`);
      } else {
        const uploadText = r.auto_upload
          ? (r.group_uploads_by_month ? " + monthly upload batches" : " + upload")
          : "";
        toast(`Queued import of ${r.file_count} files${uploadText}`);
      }
    }
  } catch (e) {
    toast("Import failed: " + e.message);
  }
}

async function loadCameraFiles(dcimPath, preserveSelection = false) {
  const list = document.getElementById("cameraFiles");
  const summary = document.getElementById("cameraFileSummary");
  if (!list || !summary) return;
  appState.currentDcimPath = dcimPath;
  appState.cameraBrowserPath = "";
  if (!preserveSelection) {
    appState.cameraFileSelection.clear();
    list.textContent = "Loading camera videos…";
    summary.textContent = "Scanning camera storage…";
  }
  try {
    const data = await api.get(`/api/device-files?dcim_path=${encodeURIComponent(dcimPath)}`);
    appState.cameraFiles = data.files || [];
    const available = new Set(
      cameraFilesNeedingUpload().map(file => file.full_path || file.path)
    );
    appState.cameraFileSelection.forEach(path => {
      if (!available.has(path)) appState.cameraFileSelection.delete(path);
    });
    renderCameraFiles();
  } catch (e) {
    list.textContent = "Unable to load camera videos: " + e.message;
    summary.textContent = "";
  }
}

async function browseCameraFolder(path = "") {
  const list = document.getElementById("cameraFiles");
  const summary = document.getElementById("cameraFileSummary");
  if (!list || !summary || !appState.currentDcimPath) return;
  list.textContent = "Loading camera folder…";
  try {
    const data = await api.get(
      `/api/device-entries?dcim_path=${encodeURIComponent(appState.currentDcimPath)}&path=${encodeURIComponent(path)}`
    );
    appState.cameraBrowserPath = data.path || "";
    appState.cameraFiles = data.entries || [];
    renderCameraFiles();
  } catch (e) {
    list.textContent = "Unable to load camera folder: " + e.message;
    summary.textContent = "";
  }
}

function renderCameraFiles() {
  const list = document.getElementById("cameraFiles");
  const summary = document.getElementById("cameraFileSummary");
  if (!list || !summary) return;
  const files = appState.cameraFiles;
  const visible = cameraFilesNeedingUpload(files);
  const uploadedCount = files.filter(file => file.type !== "directory" && file.uploaded).length;
  const hiddenFolders = files.filter(file => file.type === "directory").length;
  const totalBytes = visible.reduce((sum, file) => sum + (file.size_bytes || 0), 0);
  summary.textContent =
    `${visible.length} video${visible.length === 1 ? "" : "s"} awaiting upload · ` +
    `${uploadedCount} already uploaded hidden · ${appState.cameraFileSelection.size} selected · ${fmtBytes(totalBytes)}`;
  if (!visible.length) {
    const empty = hiddenFolders
      ? "No unuploaded videos in this folder. Open another camera folder to check more clips."
      : "No unuploaded videos found on this camera.";
    list.innerHTML = `<span class='hint'>${empty}</span>`;
    return;
  }
  list.innerHTML = renderCameraTable(visible);
}

function cameraFilesNeedingUpload(files = appState.cameraFiles) {
  return files.filter(file => file.type === "directory" || !file.uploaded);
}

function renderCameraTable(files) {
  const sorted = [...files].sort((a, b) => {
    const at = new Date(a.modified_at || 0).getTime() || 0;
    const bt = new Date(b.modified_at || 0).getTime() || 0;
    if (at !== bt) return bt - at;
    return String(a.filename || a.path).localeCompare(String(b.filename || b.path), undefined, { numeric: true });
  });
  return `
    <table class="camera-table">
      <thead>
        <tr>
          <th></th>
          <th></th>
          <th>Video</th>
          <th>Date on camera</th>
          <th>Size</th>
          <th>Path</th>
          <th>Upload status</th>
        </tr>
      </thead>
      <tbody>
        ${sorted.map(entry => {
          const filePath = entry.full_path || entry.path;
          if (entry.type === "directory") {
            return `
              <tr>
                <td></td>
                <td></td>
                <td><button class="folder-name" onclick="browseCameraFolder('${escJs(entry.path)}')">${esc(entry.name || entry.filename)}</button></td>
                <td>${esc(fmtDateTime(entry.modified_at) || "Unknown")}</td>
                <td></td>
                <td><span class="path-text" title="${esc(entry.path)}">${esc(entry.path)}</span></td>
                <td>${renderCameraUploadStatus(entry)}</td>
              </tr>
            `;
          }
          return `
            <tr>
              <td><input type="checkbox" value="${esc(filePath)}" ${appState.cameraFileSelection.has(filePath) ? "checked" : ""} onchange="toggleCameraFile('${escJs(filePath)}', this.checked)"></td>
              <td><img class="camera-thumb" src="/api/device-file-thumb?path=${encodeURIComponent(filePath)}" alt="" loading="lazy" onerror="this.classList.add('thumb-missing')"></td>
              <td><strong>${esc(entry.filename || entry.name)}</strong></td>
              <td>${esc(fmtDateTime(entry.modified_at) || "Unknown")}</td>
              <td>${esc(fmtBytes(entry.size_bytes))}</td>
              <td><span class="path-text" title="${esc(entry.relative_path || entry.path)}">${esc(entry.relative_path || entry.path)}</span></td>
              <td>${renderCameraUploadStatus(entry)}</td>
            </tr>
          `;
        }).join("")}
      </tbody>
    </table>
  `;
}

function renderCameraUploadStatus(entry) {
  if (entry.type === "directory") return "<span class='hint'>Folder</span>";
  const uploads = entry.uploads || [];
  if (entry.uploaded) return "<span class='pill up-done'>Uploaded</span>";
  if (!uploads.length) return "<span class='pill up-pending'>Not uploaded</span>";
  return uploads.map(u => {
    const pct = u.total_bytes ? u.bytes_uploaded / u.total_bytes : (u.progress || 0);
    return `<div class="camera-upload-status up-${esc(u.status)}" title="${esc(u.error || u.remote_path || "")}">${renderProgressBar(pct, u.status)}</div>`;
  }).join(" ");
}

function toggleCameraGroup(key, checked) {
  appState.cameraFiles
    .filter(entry => entry.type === "file")
    .forEach(file => {
      const date = new Date(file.modified_at || "");
      const fileKey = Number.isNaN(date.getTime())
        ? "Undated"
        : `${date.getFullYear()}/${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getDate()).padStart(2, "0")}`;
      if (fileKey !== key) return;
      const path = file.full_path || file.path;
      if (checked) appState.cameraFileSelection.add(path);
      else appState.cameraFileSelection.delete(path);
    });
  renderCameraFiles();
}

function renderCameraBreadcrumb() {
  const crumbs = appState.cameraBrowserPath ? appState.cameraBrowserPath.split("/").filter(Boolean) : [];
  const parent = crumbs.slice(0, -1).join("/");
  const parts = [
    `<button class="folder-crumb" onclick="browseCameraFolder('')">Camera</button>`,
    ...crumbs.map((part, idx) => {
      const crumbPath = crumbs.slice(0, idx + 1).join("/");
      return `<button class="folder-crumb" onclick="browseCameraFolder('${escJs(crumbPath)}')">${esc(part)}</button>`;
    }),
  ].join("<span class='folder-sep'>/</span>");
  return `
    <div class="folder-toolbar">
      <div class="folder-crumbs">${parts}</div>
      ${appState.cameraBrowserPath ? `<button class="ghost" onclick="browseCameraFolder('${escJs(parent)}')">Up</button>` : ""}
    </div>
  `;
}

function toggleCameraFile(path, checked) {
  if (checked) appState.cameraFileSelection.add(path);
  else appState.cameraFileSelection.delete(path);
  renderCameraFiles();
}

function toggleCameraSelection(checked) {
  if (checked) {
    cameraFilesNeedingUpload()
      .filter(file => file.type !== "directory")
      .forEach(file => appState.cameraFileSelection.add(file.full_path || file.path));
  }
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

async function uploadAllCameraFiles() {
  if (!appState.currentDcimPath) return toast("No camera connected");
  const pending = cameraFilesNeedingUpload().filter(file => file.type !== "directory");
  if (!pending.length) return toast("No unuploaded camera videos found");
  const destinationIds = await chooseDestinationIds();
  if (destinationIds === false) return;
  const paths = pending.map(file => file.full_path || file.path);
  await importDevice(
    appState.currentDcimPath,
    true,
    false,
    paths,
    destinationIds,
    { groupUploadsByMonth: true },
  );
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
      o.textContent = `${fmtMonthYear(m.year, m.month)} (${m.count})`;
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
  if (!mediaCache.length && !grid.children.length) grid.textContent = "Loading…";
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
  const nextMedia = await api.get("/api/media?" + p.toString());
  const signature = JSON.stringify(nextMedia.map(m => ({
    id: m.id,
    filename: m.filename,
    capture_time: m.capture_time,
    thumbnail: m.has_thumb,
    uploads: (m.uploads || []).map(u => [
      u.destination_id,
      u.status,
      u.bytes_uploaded,
      u.total_bytes,
      u.remote_path,
      u.uploaded_at,
    ]),
    tags: m.tags,
  })));
  mediaCache = nextMedia;
  if (signature === appState.lastMediaSignature) {
    updateSelCount();
    return;
  }
  appState.lastMediaSignature = signature;
  // Verified-uploaded clips live in their own section so the library only
  // shows clips that still need attention. "Verified" = an upload that the
  // backend reported as fully done (not merely attempted/failed).
  const library = mediaCache.filter(m => !isVerifiedUploaded(m));
  grid.innerHTML = "";
  if (!library.length) {
    grid.innerHTML = "<span class='hint'>No clips awaiting upload. Import from the camera above.</span>";
  } else {
    renderLibraryList(grid, library);
  }
  updateSelCount();
}

function isVerifiedUploaded(m) {
  return (m.uploads || []).some(u => u.status === "done");
}

function renderLibraryList(target, items) {
  const table = document.createElement("table");
  table.className = "library-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th></th>
        <th>Video</th>
        <th>Date of video</th>
        <th>Time imported</th>
        <th>Location</th>
        <th>Upload progress</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  items.forEach(m => tbody.append(renderLibraryRow(m)));
  target.append(table);
}

function renderLibraryRow(m) {
  const row = document.createElement("tr");
  row.className = selected.has(m.id) ? "selected-row" : "";
  const thumb = m.has_thumb ? `/api/media/${m.id}/thumb` : "";
  const location = renderMediaLocation(m);
  const uploadText = renderMediaUploadProgress(m);
  row.innerHTML = `
    <td><input type="checkbox" class="pick" ${selected.has(m.id) ? "checked" : ""}></td>
    <td>
      <div class="library-video-cell">
        ${thumb ? `<img class="mini-thumb" src="${thumb}" alt="" loading="lazy">` : `<button class="mini-thumb placeholder-thumb">Play</button>`}
        <div>
          <div class="fn" title="${esc(m.filename)}">${esc(m.filename)}</div>
          <div class="hint">${esc(fmtDur(m.duration_s))} · ${esc(fmtBytes(m.size_bytes))}</div>
        </div>
      </div>
    </td>
    <td>${esc(fmtDateTime(m.capture_time) || "No date")}</td>
    <td>${esc(fmtDateTime(m.created_at) || "Unknown")}</td>
    <td><span class="path-text" title="${esc(location)}">${esc(location)}</span></td>
    <td>${uploadText}</td>
  `;
  row.querySelector(".pick").onchange = e => {
    e.target.checked ? selected.add(m.id) : selected.delete(m.id);
    row.classList.toggle("selected-row", e.target.checked);
    updateSelCount();
  };
  const thumbEl = row.querySelector(".mini-thumb");
  thumbEl.onclick = () => m.kind === "video" ? playVideo(m.id) : window.open(`/api/media/${m.id}/stream`);
  return row;
}

function renderMediaLocation(m) {
  const done = (m.uploads || []).filter(u => u.status === "done" && u.remote_path);
  if (done.length) return done.map(u => u.remote_path).join(" · ");
  return m.path || "Local library";
}

function renderMediaUploadProgress(m) {
  const uploads = m.uploads || [];
  if (!uploads.length) return "<span class='hint'>Not uploaded</span>";
  return uploads.map(u => {
    const pct = u.total_bytes ? Math.round((u.bytes_uploaded / u.total_bytes) * 100) : Math.round((u.progress || 0) * 100);
    const text = u.status === "done" ? "Uploaded" : `${esc(u.status)} ${pct}%`;
    return `<span class="pill up-${esc(u.status)}" title="${esc(u.error || u.remote_path || "")}">${renderProgressRing((u.progress || pct / 100))} ${text}</span>`;
  }).join(" ");
}

async function loadRecentUploads() {
  const rows = await api.get("/api/recent-uploads?limit=24&days=7");
  const signature = JSON.stringify(rows.map(r => [
    r.id,
    r.status,
    r.uploaded_at,
    r.remote_path,
    r.destination_name,
    r.media?.has_thumb,
  ]));
  if (signature === appState.lastRecentUploadSignature) return;
  appState.lastRecentUploadSignature = signature;
  renderUploaded(rows);
}

function renderUploaded(items) {
  const panel = document.getElementById("uploadedPanel");
  const grid = document.getElementById("uploadedGrid");
  const count = document.getElementById("uploadedCount");
  if (!panel || !grid) return;
  panel.hidden = items.length === 0;
  if (count) count.textContent = `${items.length} recent upload${items.length === 1 ? "" : "s"}`;
  grid.innerHTML = "";
  items.forEach(row => grid.append(renderRecentUploadCard(row)));
}

function renderRecentUploadCard(row) {
  const media = row.media || {};
  const c = document.createElement("div");
  c.className = "card uploaded-card";
  const thumb = media.has_thumb && row.source_media_id ? `/api/media/${row.source_media_id}/thumb` : "";
  const thumbHtml = thumb
    ? `<img class="thumb" src="${thumb}" alt="" loading="lazy">`
    : `<div class="thumb upload-placeholder">Uploaded</div>`;
  c.innerHTML = `
    ${thumbHtml}
    <div class="meta">
      <div class="fn" title="${esc(row.filename)}">${esc(row.filename)}</div>
      <div class="sub">${esc(row.destination_name || `Destination ${row.destination_id}`)} · ${esc(fmtDateTime(row.uploaded_at))}</div>
      <div class="status-row">
        <span class="pill up-done">Uploaded</span>
        <span class="pill" title="${esc(row.remote_path || "")}">${esc(row.remote_path || "Remote path recorded")}</span>
      </div>
    </div>`;
  const thumbEl = c.querySelector(".thumb");
  if (row.source_media_id && media.kind === "video") thumbEl.onclick = () => playVideo(row.source_media_id);
  else if (row.source_media_id) thumbEl.onclick = () => window.open(`/api/media/${row.source_media_id}/stream`);
  return c;
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
    "Combine order: selected, date, or sequence",
    "date",
  );
  if (order === null) return;
  const cleanOrder = ["selected", "date", "sequence"].includes(order.trim())
    ? order.trim()
    : "selected";
  if (!confirm(`Combine ${ids.length} clips into one video using ${cleanOrder} order?`)) return;
  try {
    await api.post("/api/merge", { media_ids: ids, order: cleanOrder });
    toast("Combine queued");
  } catch (e) {
    toast("Combine failed: " + e.message);
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
  const albums = await api.get("/api/trips");
  if (!albums.length) return toast("Create a trip first (Trips page)");
  const list = albums.map(a => `${a.id}: ${a.name}`).join("\n");
  const pick = prompt(`Trip id to add ${selected.size} clips to:\n${list}`, "");
  const aid = parseInt(pick, 10);
  if (isNaN(aid)) return;
  const album = albums.find(a => a.id === aid);
  const merged = [...new Set([...(album ? album.item_ids : []), ...selIds()])];
  await api.post(`/api/trips/${aid}/items`, { media_ids: merged });
  toast("Added to trip");
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
    basePlaceholder: "/mnt/NAS",
    hint: "Use a directory already mounted on the Pi or Docker host.",
  },
  nfs: {
    port: "",
    host: false,
    user: false,
    secret: false,
    basePlaceholder: "/mnt/NAS",
    hint: "Use an NFS share that is already mounted on the Pi or Docker host. This app does not mount NFS shares itself.",
  },
  smb: {
    port: "",
    host: false,
    user: false,
    secret: false,
    basePlaceholder: "/mnt/smb/camera",
    hint: "Use an SMB/CIFS share that is already mounted on the Pi or Docker host. This app does not mount SMB shares itself.",
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
  const dlg = document.getElementById("destDialog");
  if (!dlg) return;
  if (!document.getElementById("dId").value && !document.getElementById("dBase").value.trim()) {
    document.getElementById("dBase").value = "/mnt/NAS";
  }
  updateBaseStatus();
  if (!dlg.open) dlg.showModal();
}

function hideDestForm() {
  const dlg = document.getElementById("destDialog");
  if (dlg && dlg.open) dlg.close();
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

let destCache = [];

async function loadDestinations() {
  const el = document.getElementById("destList");
  if (!el) return;
  const dests = await api.get("/api/destinations");
  destCache = dests;
  if (!dests.length) {
    el.innerHTML = "<span class='hint'>No destinations yet.</span>";
    return;
  }
  el.innerHTML = "";
  dests.forEach((d, idx) => {
    const row = document.createElement("div");
    row.className = "dest-row";

    const ranker = document.createElement("div");
    ranker.className = "dest-rank";
    const up = document.createElement("button");
    up.className = "ghost icon";
    up.textContent = "▲";
    up.title = "Higher upload priority";
    up.disabled = idx === 0;
    up.onclick = () => moveDestination(idx, -1);
    const pos = document.createElement("span");
    pos.className = "dest-rank-num";
    pos.textContent = `#${idx + 1}`;
    const down = document.createElement("button");
    down.className = "ghost icon";
    down.textContent = "▼";
    down.title = "Lower upload priority";
    down.disabled = idx === dests.length - 1;
    down.onclick = () => moveDestination(idx, 1);
    ranker.append(up, pos, down);

    const body = document.createElement("div");
    body.className = "dest-body";
    body.innerHTML = `
      <div><b>${esc(d.name)}</b> <span class="hint">[${esc(d.type)}]${d.is_default ? " · default" : ""}${d.enabled ? "" : " · disabled"}</span></div>
      <span class="hint">${esc(d.base_path)} → ${esc(humanTemplate(d.path_template))}</span>
      <span class="hint">${renderDestinationStorageText(d)}</span>
      <div class="folder-window compact" id="destFolders-${d.id}">Browse to see available folders.</div>
    `;

    const actions = document.createElement("div");
    actions.className = "row";
    const test = document.createElement("button");
    test.className = "ghost";
    test.textContent = "Test";
    test.onclick = async () => {
      test.textContent = "Testing…";
      test.disabled = true;
      try {
        const r = await api.post(`/api/destinations/${d.id}/test`);
        if (r.ok) toast("✓ Connection + upload/download OK");
        else if (r.connection) toast("Connected, but upload/download failed: " + r.error);
        else toast("Connection failed: " + r.error);
      } catch (e) {
        toast("Test failed: " + e.message);
      }
      test.textContent = "Test";
      test.disabled = false;
    };
    const browse = document.createElement("button");
    browse.className = "ghost";
    browse.textContent = "Browse";
    browse.onclick = () => browseDestinationFolders(d.id, `destFolders-${d.id}`, "", d);
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

    row.append(ranker, body, actions);
    el.append(row);
  });
}

async function moveDestination(idx, delta) {
  const target = idx + delta;
  if (target < 0 || target >= destCache.length) return;
  const ids = destCache.map(d => d.id);
  [ids[idx], ids[target]] = [ids[target], ids[idx]];
  try {
    await api.post("/api/destinations/reorder", { ordered_ids: ids });
    loadDestinations();
  } catch (e) {
    toast("Reorder failed: " + e.message);
  }
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
  document.getElementById("dBase").value = "/mnt/NAS";
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
  target.textContent = "Loading remote files…";
  const browserConfig = destinationId ? config : (config || destForm());
  const basePath = browserConfig?.base_path || document.getElementById("dBase")?.value.trim() || "";
  try {
    const r = destinationId
      ? await api.get(`/api/destinations/${destinationId}/entries?path=${encodeURIComponent(path)}`)
      : await api.post(`/api/destinations/preview/entries?path=${encodeURIComponent(path)}`, browserConfig);
    appState.folderBrowsers[targetId] = {
      destinationId,
      config: browserConfig,
      basePath,
      path: r.path || "",
      lastEntries: r.entries || [],
      selectedPath: appState.folderBrowsers[targetId]?.selectedPath,
    };
    renderFolderBrowser(targetId, r.entries || []);
  } catch (e) {
    target.textContent = "Unable to load remote files: " + e.message;
  }
}

function browseFolderTarget(targetId, path = "") {
  const state = appState.folderBrowsers[targetId];
  if (!state) return;
  browseDestinationFolders(state.destinationId, targetId, path, state.config);
}

function renderFolderBrowser(targetId, entries) {
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
  const entryRows = entries.length
    ? entries.map(entry => {
      const childPath = entry.path || appendPath(state.path, entry.name);
      const isSel = selectedPath != null && childPath === selectedPath;
      if (entry.type === "directory") {
        return `
          <div class="folder-row${isSel ? " selected" : ""}">
            <button class="folder-open" onclick="${browseCall(childPath)}">Open</button>
            <button class="folder-name" onclick="${browseCall(childPath)}">${esc(entry.name)}</button>
            <button class="folder-select" onclick="applyFolderChoice('${escJs(targetId)}','${escJs(childPath)}')">${isSel ? "✓ Selected" : "Select"}</button>
          </div>
        `;
      }
      return `
        <div class="folder-row file-row">
          <span class="folder-open">File</span>
          <span class="folder-name">${esc(entry.name)}</span>
          <span class="hint">${esc(fmtBytes(entry.size_bytes || 0))}</span>
        </div>
      `;
    }).join("")
    : "<div class='folder-empty'>No remote files or folders at this level.</div>";
  const currentSelected = selectedPath != null && (selectedPath || "") === (state.path || "");
  target.innerHTML = `
    <div class="folder-toolbar">
      <div class="folder-crumbs">${breadcrumbHtml}</div>
      <div class="row">
        ${state.path ? `<button class="ghost" onclick="${browseCall(parent)}">Up</button>` : ""}
        <button class="ghost" onclick="applyFolderChoice('${escJs(targetId)}','${escJs(state.path)}')">${currentSelected ? "✓ This folder selected" : "Use this folder"}</button>
      </div>
    </div>
    <div class="folder-list">${entryRows}</div>
  `;
}

async function applyFolderChoice(targetId, path) {
  const base = document.getElementById("dBase");
  const state = appState.folderBrowsers[targetId] || {};
  const root = state.basePath || base?.value.trim() || "/";
  const pickedPath = path ? appendPath(root, path) : root;
  state.selectedPath = path;
  if (targetId !== "folderBrowser" && state.destinationId && state.config) {
    const body = { ...state.config, base_path: pickedPath };
    await api.put(`/api/destinations/${state.destinationId}`, body);
    renderFolderBrowser(targetId, state.lastEntries || []);
    toast(`Using ${pickedPath} for ${state.config.name}`);
    loadDestinations();
    return;
  }
  if (!base) return;
  base.value = pickedPath;
  state.selectedPath = path;
  base.classList.add("just-set");
  setTimeout(() => base.classList.remove("just-set"), 1200);
  updateBaseStatus(true);
  renderFolderBrowser(targetId, state.lastEntries || []);
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

// ============================ FILE EXPLORER =================================

async function initFileExplorer() {
  ensureGlobalJobPolling();
  const [devices, destinations] = await Promise.all([
    api.get("/api/devices"),
    api.get("/api/destinations"),
  ]);
  const cameraSelect = document.getElementById("explorerCameraSelect");
  const destSelect = document.getElementById("explorerDestinationSelect");
  if (cameraSelect) {
    cameraSelect.innerHTML = devices.length
      ? devices.map(d => `<option value="${esc(d.path || d.dcim_path)}">${esc(d.label || d.path)}</option>`).join("")
      : "<option value=''>No camera attached</option>";
    appState.fileExplorer.cameraRoot = cameraSelect.value || "";
    appState.fileExplorer.cameraPath = "";
  }
  if (destSelect) {
    destSelect.innerHTML = destinations.length
      ? destinations.map(d => `<option value="${d.id}">${esc(d.name)} (${esc(d.base_path || d.type)})</option>`).join("")
      : "<option value=''>No destination configured</option>";
    appState.fileExplorer.destinationId = destSelect.value ? Number(destSelect.value) : null;
    appState.fileExplorer.destinationPath = "";
  }
  await Promise.all([loadExplorerPane("camera"), loadExplorerPane("destination")]);
}

function selectExplorerCamera() {
  const select = document.getElementById("explorerCameraSelect");
  appState.fileExplorer.cameraRoot = select?.value || "";
  appState.fileExplorer.cameraPath = "";
  loadExplorerPane("camera");
}

function selectExplorerDestination() {
  const select = document.getElementById("explorerDestinationSelect");
  appState.fileExplorer.destinationId = select?.value ? Number(select.value) : null;
  appState.fileExplorer.destinationPath = "";
  loadExplorerPane("destination");
}

async function loadExplorerPane(side, path = null) {
  const state = appState.fileExplorer;
  const pane = document.getElementById(side === "camera" ? "cameraExplorer" : "destinationExplorer");
  const meta = document.getElementById(side === "camera" ? "cameraExplorerMeta" : "destinationExplorerMeta");
  if (!pane) return;
  pane.textContent = "Loading…";
  try {
    let data;
    if (side === "camera") {
      if (!state.cameraRoot) {
        pane.innerHTML = "<span class='hint'>No camera attached.</span>";
        if (meta) meta.textContent = "";
        return;
      }
      if (path !== null) state.cameraPath = path;
      data = await api.get(`/api/file-browser/camera/entries?root_path=${encodeURIComponent(state.cameraRoot)}&path=${encodeURIComponent(state.cameraPath)}`);
      if (meta) meta.textContent = data.root || state.cameraRoot;
    } else {
      if (!state.destinationId) {
        pane.innerHTML = "<span class='hint'>No destination configured.</span>";
        if (meta) meta.textContent = "";
        return;
      }
      if (path !== null) state.destinationPath = path;
      data = await api.get(`/api/file-browser/destinations/${state.destinationId}/entries?path=${encodeURIComponent(state.destinationPath)}`);
      state.destinationLocalActions = !!data.local_actions;
      if (meta) meta.textContent = `${data.root || ""}${data.local_actions ? "" : " · browse only"}`;
    }
    renderExplorerPane(side, data);
  } catch (e) {
    pane.textContent = "Unable to load files: " + e.message;
  }
}

function renderExplorerPane(side, data) {
  const pane = document.getElementById(side === "camera" ? "cameraExplorer" : "destinationExplorer");
  if (!pane) return;
  const path = data.path || "";
  const crumbs = path ? path.split("/").filter(Boolean) : [];
  const parent = crumbs.slice(0, -1).join("/");
  const rootLabel = side === "camera" ? "Camera" : "Destination";
  const localActions = side === "camera" || data.local_actions;
  const crumbHtml = [
    `<button class="folder-crumb" onclick="loadExplorerPane('${side}', '')">${rootLabel}</button>`,
    ...crumbs.map((part, idx) => {
      const crumbPath = crumbs.slice(0, idx + 1).join("/");
      return `<button class="folder-crumb" onclick="loadExplorerPane('${side}', '${escJs(crumbPath)}')">${esc(part)}</button>`;
    }),
  ].join("<span class='folder-sep'>/</span>");
  const entries = data.entries || [];
  const rows = entries.length
    ? entries.map(entry => renderExplorerEntry(side, entry, localActions)).join("")
    : "<div class='folder-empty'>No files in this folder.</div>";
  pane.innerHTML = `
    <div class="folder-toolbar explorer-toolbar">
      <div class="folder-crumbs">${crumbHtml}</div>
      <div class="row">
        ${path ? `<button class="ghost" onclick="loadExplorerPane('${side}', '${escJs(parent)}')">Up</button>` : ""}
        <button class="ghost" onclick="loadExplorerPane('${side}')">Refresh</button>
      </div>
    </div>
    <div class="explorer-list">${rows}</div>
  `;
}

function renderExplorerEntry(side, entry, localActions) {
  const icon = entry.type === "directory" ? "Folder" : (entry.playable ? "Video" : "File");
  const path = entry.path || "";
  if (entry.type === "directory") {
    return `
      <div class="explorer-row">
        <button class="folder-open" onclick="loadExplorerPane('${side}', '${escJs(path)}')">Open</button>
        <button class="explorer-name" onclick="loadExplorerPane('${side}', '${escJs(path)}')">${esc(entry.name)}</button>
        <span class="hint">${icon}</span>
        <span class="hint">${esc(fmtDateTime(entry.modified_at) || "")}</span>
        <span class="explorer-actions"></span>
      </div>
    `;
  }
  const play = entry.playable
    ? `<button class="ghost" onclick="playExplorerFile('${side}', '${escJs(path)}')">Play</button>`
    : "";
  const edit = localActions
    ? `<button class="ghost" onclick="openExplorerRename('${side}', '${escJs(path)}', '${escJs(entry.name)}')">Rename</button>
       <button class="ghost" onclick="openExplorerDate('${side}', '${escJs(path)}', '${escJs(entry.modified_at || "")}')">Date</button>`
    : "";
  return `
    <div class="explorer-row">
      <span class="folder-open">${icon}</span>
      <span class="explorer-name" title="${esc(path)}">${esc(entry.name)}</span>
      <span class="hint">${esc(fmtBytes(entry.size_bytes || 0))}</span>
      <span class="hint">${esc(fmtDateTime(entry.modified_at) || "")}</span>
      <span class="explorer-actions">${play}${edit}</span>
    </div>
  `;
}

function explorerActionUrl(side, action, path = "") {
  if (side === "camera") {
    const root = encodeURIComponent(appState.fileExplorer.cameraRoot);
    const pathPart = path ? `&path=${encodeURIComponent(path)}` : "";
    return `/api/file-browser/camera/${action}?root_path=${root}${pathPart}`;
  }
  const id = appState.fileExplorer.destinationId;
  const pathPart = path ? `?path=${encodeURIComponent(path)}` : "";
  return `/api/file-browser/destinations/${id}/${action}${pathPart}`;
}

function playExplorerFile(side, path) {
  const dlg = document.getElementById("explorerPlayerDlg");
  const video = document.getElementById("explorerPlayer");
  if (!dlg || !video) return;
  video.src = explorerActionUrl(side, "stream", path);
  dlg.showModal();
}

function closeExplorerPlayer() {
  const dlg = document.getElementById("explorerPlayerDlg");
  const video = document.getElementById("explorerPlayer");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
  if (dlg) dlg.close();
}

function openExplorerRename(side, path, name) {
  appState.fileExplorer.pendingAction = { side, path };
  const input = document.getElementById("explorerRenameInput");
  if (input) input.value = name;
  document.getElementById("explorerRenameDlg")?.showModal();
}

async function submitExplorerRename() {
  const pending = appState.fileExplorer.pendingAction;
  const input = document.getElementById("explorerRenameInput");
  if (!pending || !input) return;
  try {
    await api.post(explorerActionUrl(pending.side, "rename"), {
      path: pending.path,
      filename: input.value.trim(),
    });
    document.getElementById("explorerRenameDlg")?.close();
    await loadExplorerPane(pending.side);
    toast("File renamed");
  } catch (e) {
    toast("Rename failed: " + e.message);
  }
}

function openExplorerDate(side, path, modifiedAt) {
  appState.fileExplorer.pendingAction = { side, path };
  const input = document.getElementById("explorerDateInput");
  if (input) input.value = toDateTimeLocal(modifiedAt);
  document.getElementById("explorerDateDlg")?.showModal();
}

function toDateTimeLocal(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 16);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function submitExplorerDate() {
  const pending = appState.fileExplorer.pendingAction;
  const input = document.getElementById("explorerDateInput");
  if (!pending || !input || !input.value) return;
  try {
    await api.post(explorerActionUrl(pending.side, "timestamp"), {
      path: pending.path,
      modified_at: input.value,
    });
    document.getElementById("explorerDateDlg")?.close();
    await loadExplorerPane(pending.side);
    toast("File date updated");
  } catch (e) {
    toast("Date update failed: " + e.message);
  }
}

// ============================ TRIPS =========================================

function initAlbums() {
  ensureGlobalJobPolling();
  loadAlbums();
}

async function createAlbum() {
  const name = document.getElementById("albumName").value.trim();
  if (!name) return toast("Name required");
  try {
    await api.post("/api/trips", { name });
    document.getElementById("albumName").value = "";
    loadAlbums();
  } catch (e) {
    toast("Failed: " + e.message);
  }
}

async function loadAlbums() {
  const el = document.getElementById("albumList");
  if (!el) return;
  const [albums, media, recentUploads] = await Promise.all([
    api.get("/api/trips"),
    api.get("/api/media"),
    api.get("/api/recent-uploads?limit=100&days=30"),
  ]);
  const byId = Object.fromEntries(media.map(m => [m.id, m]));
  el.innerHTML = "";
  el.append(renderRecentUploadsAlbum(recentUploads));
  if (!albums.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "No trips yet.";
    el.append(empty);
    return;
  }
  albums.forEach(a => {
    const box = document.createElement("div");
    box.className = "album-folder";
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
    merge.textContent = "Combine trip";
    merge.onclick = async () => {
      await api.post("/api/merge", { album_id: a.id, order: "date" });
      toast("Trip video combine queued");
    };
    const upl = document.createElement("button");
    upl.textContent = "Upload trip";
    upl.onclick = async () => {
      await api.post("/api/upload", { media_ids: a.item_ids });
      toast("Upload queued");
    };
    const del = document.createElement("button");
    del.className = "danger";
    del.textContent = "Delete trip";
    del.onclick = async () => {
      if (confirm("Delete trip?")) {
        await api.del(`/api/trips/${a.id}`);
        loadAlbums();
      }
    };
    bar.append(merge, upl, del);
    box.append(bar);
    el.append(box);
  });
}

function renderRecentUploadsAlbum(rows) {
  const box = document.createElement("div");
  box.className = "album-folder";
  const count = rows.length;
  box.innerHTML = `
    <div class="row spread">
      <div>
        <h2>Recent uploads <span class="hint">(${count} videos)</span></h2>
        <p class="hint">Recent clips uploaded to the destination.</p>
      </div>
      <button class="ghost">Open</button>
    </div>
    <div class="grid recent-album-grid" hidden></div>
  `;
  const grid = box.querySelector(".recent-album-grid");
  const button = box.querySelector("button");
  if (!rows.length) {
    grid.innerHTML = "<span class='hint'>No recent uploads recorded yet.</span>";
  } else {
    rows.forEach(row => grid.append(renderRecentUploadCard(row)));
  }
  button.onclick = () => {
    grid.hidden = !grid.hidden;
    button.textContent = grid.hidden ? "Open" : "Close";
  };
  return box;
}

async function moveAlbumItem(album, idx, dir) {
  const ids = [...album.item_ids];
  const j = idx + dir;
  if (j < 0 || j >= ids.length) return;
  [ids[idx], ids[j]] = [ids[j], ids[idx]];
  await api.post(`/api/trips/${album.id}/items`, { media_ids: ids });
  loadAlbums();
}

// ============================ JOBS ==========================================

function initJobs() {
  ensureGlobalJobPolling();
  refreshGlobalJobs();
  clearInterval(appState.jobTimer);
  appState.jobTimer = setInterval(() => {
    if (!document.hidden) renderJobsPage();
  }, 1000);
}

// The jobs page updates in place (no innerHTML churn) so the summary boxes,
// overall bar and per-row progress don't flash on every 1s/3s refresh.
const JOB_FILTERS = [
  { key: "running,queued", label: "Queued / running", count: o => (o.running || 0) + (o.queued || 0) },
  { key: "paused", label: "Paused", count: o => o.paused || 0 },
  { key: "done", label: "Completed", count: o => o.done || 0 },
  { key: "error", label: "Errors", count: o => o.error || 0 },
];

function ensureJobsSummary(summary) {
  if (summary.dataset.built) return;
  summary.dataset.built = "1";
  summary.innerHTML =
    JOB_FILTERS.map(f =>
      `<div class="live-card jobs-filter-card" data-filter="${esc(f.key)}" role="button" tabindex="0" title="Click to filter the list">
         <div class="hint">${esc(f.label)}</div><div class="value" data-count="${esc(f.key)}">0</div>
       </div>`
    ).join("") +
    `<div class="live-card"><div class="hint">Latest</div><div data-latest>—</div></div>`;
  summary.querySelectorAll(".jobs-filter-card").forEach(card => {
    const trigger = () => setJobFilter(card.dataset.filter);
    card.addEventListener("click", trigger);
    card.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); trigger(); }
    });
  });
}

function setNodeText(node, text) {
  if (node && node.textContent !== String(text)) node.textContent = text;
}

function updateJobsSummary(summary, o, jobs) {
  JOB_FILTERS.forEach(f => {
    setNodeText(summary.querySelector(`[data-count="${f.key}"]`), f.count(o));
    const card = summary.querySelector(`.jobs-filter-card[data-filter="${f.key}"]`);
    if (card) card.classList.toggle("selected", appState.jobFilter === f.key);
  });
  setNodeText(summary.querySelector("[data-latest]"), jobs[0] ? jobs[0].description : "No jobs yet");
}

function ensureJobControls(controls) {
  if (controls.dataset.built) return;
  controls.dataset.built = "1";
  controls.innerHTML = `
    <button class="ghost" data-act="pause" onclick="pauseAllJobs()">Pause all</button>
    <button class="ghost" data-act="resume" onclick="resumeAllJobs()">Resume all</button>
    <button class="danger" data-act="stop" onclick="stopAllJobs()">Stop all</button>`;
}

function updateJobControls(controls, o) {
  const anyActive = (o.active || 0) > 0;
  const anyPaused = (o.paused || 0) > 0;
  controls.querySelector('[data-act="pause"]').disabled = !anyActive;
  controls.querySelector('[data-act="resume"]').disabled = !anyPaused;
  controls.querySelector('[data-act="stop"]').disabled = !(anyActive || anyPaused);
}

function updateJobsOverall() {
  const el = document.getElementById("jobsOverall");
  if (!el) return;
  const o = appState.jobsOverview;
  if (!o || !o.active) {
    if (el.dataset.built) { el.innerHTML = ""; delete el.dataset.built; }
    return;
  }
  if (!el.dataset.built) {
    el.dataset.built = "1";
    el.innerHTML = `
      <div class="jobs-overall-head">
        <strong>Upload progress</strong>
        <span class="hint" data-sub></span>
        <span class="jobs-overall-pct" data-pct></span>
      </div>
      <div class="prog job-progress jobs-overall-bar"><span data-fill></span></div>`;
  }
  const ofN = o.total_in_run ? `${o.completed_in_run} of ${o.total_in_run} uploads done` : "No upload transfers queued";
  setNodeText(el.querySelector("[data-sub]"), `${o.running} running · ${o.active} jobs active · ${ofN}`);
  setNodeText(el.querySelector("[data-pct]"), `${o.percent}%`);
  el.querySelector("[data-fill]").style.width = `${o.percent}%`;
}

function jobRowDetailHtml(j) {
  return j.error ? `<span style="color:#ffaea2">${esc(j.error)}</span>` : esc(j.detail || "");
}

function updateJobRow(el, j) {
  const pct = Math.round(j.progress * 100);
  const fill = el.querySelector(`[data-fill="${j.id}"]`);
  if (j.kind === "upload") {
    if (fill) fill.style.width = `${pct}%`;
    setNodeText(el.querySelector(`[data-pct="${j.id}"]`), `${pct}%`);
  }
  setNodeText(el.querySelector(`[data-elapsed="${j.id}"]`), fmtElapsed(j.started_at || j.created_at, j.finished_at));
  const detailEl = el.querySelector(`[data-detail="${j.id}"]`);
  if (detailEl) {
    const html = jobRowDetailHtml(j);
    if (detailEl.innerHTML !== html) detailEl.innerHTML = html;
  }
}

function renderJobsTable(el, jobs) {
  if (!jobs.length) {
    const key = "empty:" + (appState.jobFilter || "");
    if (el.dataset.sig !== key) {
      el.dataset.sig = key;
      el.innerHTML = `<span class='hint'>${appState.jobFilter ? "No jobs match this filter." : "No jobs yet."}</span>`;
    }
    return;
  }
  // Rebuild only when the rows/statuses/expansion change; patch progress and
  // timings in place otherwise, so a live upload doesn't flash the table.
  const sig = jobs.map(j => `${j.id}:${j.status}:${appState.expandedJobs.has(j.id) ? 1 : 0}`).join(",");
  if (el.dataset.sig !== sig) {
    el.dataset.sig = sig;
    const logScroll = captureJobLogScroll();
    let html = "<table class='jobs-table'><tr><th>ID</th><th>Kind</th><th>Description</th><th>Status</th><th>Timing</th><th class='progress-col'>Progress</th><th></th></tr>";
    jobs.forEach(j => {
      const pct = Math.round(j.progress * 100);
      const progressCell = j.kind === "upload"
        ? `<div class="prog job-progress"><span data-fill="${j.id}" style="width:${pct}%"></span></div><div class="progress-label" data-pct="${j.id}">${pct}%</div>`
        : `<span class="hint">Not a file transfer</span>`;
      const elapsed = fmtElapsed(j.started_at || j.created_at, j.finished_at);
      const started = j.started_at ? fmtDateTime(j.started_at) : "Queued";
      const cancel = (isActiveJob(j) || j.status === "paused")
        ? `<button class="ghost" onclick="cancelJob(${j.id})">Cancel</button>` : "";
      const retry = (j.kind === "upload" && !isActiveJob(j) && j.status !== "paused")
        ? `<button class="ghost" onclick="retryJob(${j.id})">Retry</button>` : "";
      const dismiss = `<button class="ghost" onclick="dismissJob(${j.id})">Dismiss</button>`;
      const expanded = appState.expandedJobs.has(j.id);
      const logs = appState.jobLogs[j.id] || [];
      html += `<tr class="job-row ${expanded ? "expanded" : ""}">
        <td>${j.id}</td>
        <td>${esc(j.kind)}</td>
        <td><button class="job-title" onclick="toggleJobLogs(${j.id})">${expanded ? "Hide" : "Show"} logs</button> ${esc(j.description)}<br><span class="hint" data-detail="${j.id}">${jobRowDetailHtml(j)}</span></td>
        <td>${renderJobState(j.status)}</td>
        <td><div class="job-time"><strong data-elapsed="${j.id}">${esc(elapsed)}</strong><span class="hint">Started ${esc(started)}</span></div></td>
        <td class="progress-cell">${progressCell}</td>
        <td><div class="row job-row-actions">${cancel}${retry}${dismiss}</div></td>
      </tr>`;
      if (expanded) {
        html += `<tr class="job-log-row"><td colspan="7">${renderJobLogPanel(j.id, logs)}</td></tr>`;
      }
    });
    el.innerHTML = html + "</table>";
    restoreJobLogScroll(logScroll);
  }
  jobs.forEach(j => updateJobRow(el, j));
}

function renderJobsPage() {
  const summary = document.getElementById("jobSummary");
  const el = document.getElementById("jobsTable");
  const controls = document.getElementById("jobBulkActions");
  if (!summary || !el) return;
  const jobs = appState.jobs || [];
  const o = appState.jobsOverview || {};
  ensureJobsSummary(summary);
  updateJobsSummary(summary, o, jobs);
  if (controls) { ensureJobControls(controls); updateJobControls(controls, o); }
  updateJobsOverall();
  renderJobsTable(el, jobs);
}

function captureJobLogScroll() {
  const scroll = {};
  document.querySelectorAll(".job-log-window[id^='jobLog-']").forEach(node => {
    scroll[node.id] = node.scrollTop;
  });
  return scroll;
}

function restoreJobLogScroll(scroll) {
  Object.entries(scroll).forEach(([id, top]) => {
    const node = document.getElementById(id);
    if (node) node.scrollTop = top;
  });
}

function renderJobLogPanel(jobId, logs) {
  if (!logs.length) {
    return `<div class="job-log-window" id="jobLog-${jobId}"><span class="hint">Loading job logs…</span></div>`;
  }
  return `<div class="job-log-window" id="jobLog-${jobId}">${
    logs.map(row => {
      const level = String(row.level || "INFO").toLowerCase();
      const pct = row.progress == null ? "" : `<span class="hint">${Math.round(row.progress * 100)}%</span>`;
      return `<div class="log-line log-${esc(level)}"><span>${esc(row.level || "INFO")}</span><code>${esc(fmtDateTime(row.created_at))} ${pct} ${esc(row.message || "")}</code></div>`;
    }).join("")
  }</div>`;
}

async function toggleJobLogs(id) {
  if (appState.expandedJobs.has(id)) {
    appState.expandedJobs.delete(id);
    renderJobsPage();
    return;
  }
  appState.expandedJobs.add(id);
  renderJobsPage();
  await loadJobLogs(id);
  updateJobLogPanel(id);
}

async function loadJobLogs(id) {
  try {
    appState.jobLogs[id] = await api.get(`/api/jobs/${id}/logs?limit=400`);
  } catch (e) {
    appState.jobLogs[id] = [{ level: "ERROR", message: "Unable to load job logs: " + e.message }];
  }
}

function refreshExpandedJobLogs() {
  appState.expandedJobs.forEach(id => {
    loadJobLogs(id).then(() => updateJobLogPanel(id));
  });
}

function updateJobLogPanel(id) {
  const panel = document.getElementById(`jobLog-${id}`);
  if (!panel) {
    renderJobsPage();
    return;
  }
  const top = panel.scrollTop;
  const replacement = document.createElement("div");
  replacement.innerHTML = renderJobLogPanel(id, appState.jobLogs[id] || []);
  const nextPanel = replacement.firstElementChild;
  panel.replaceWith(nextPanel);
  nextPanel.scrollTop = top;
}

async function cancelJob(id) {
  await api.post(`/api/jobs/${id}/cancel`);
  refreshGlobalJobs();
}

async function pauseAllJobs() {
  await api.post("/api/jobs/pause_all");
  refreshGlobalJobs();
}

async function resumeAllJobs() {
  await api.post("/api/jobs/resume_all");
  refreshGlobalJobs();
}

async function stopAllJobs() {
  await api.post("/api/jobs/stop_all");
  refreshGlobalJobs();
}

async function retryJob(id) {
  await api.post(`/api/jobs/${id}/retry`);
  toast("Upload job queued");
  refreshGlobalJobs();
}

async function dismissJob(id) {
  await api.post(`/api/jobs/${id}/dismiss`);
  refreshGlobalJobs();
}

// ============================ STATS =========================================

function initStats() {
  ensureGlobalJobPolling();
  loadStats();
  clearInterval(appState.statsPoller);
  appState.statsPoller = setInterval(() => {
    if (!document.hidden) loadStats(false);
  }, 5000);
}

async function loadStats(showLoading = true) {
  const overview = document.getElementById("statsOverview");
  const destinations = document.getElementById("statsDestinations");
  const system = document.getElementById("systemStats");
  if (!overview || !destinations) return;
  if (showLoading) {
    overview.textContent = "Loading…";
    destinations.textContent = "Loading…";
    if (system) system.textContent = "Loading…";
  }
  try {
    const hours = getTimelineMinutes() / 60;
    const stats = await api.get(`/api/stats?timeline_hours=${encodeURIComponent(hours)}`);
    const data = stats.overview || {};
    overview.innerHTML = `
      <div class="live-card"><div class="hint">Uploaded clips</div><div class="value">${data.uploaded_clip_count || 0}</div></div>
      <div class="live-card"><div class="hint">Upload errors</div><div class="value">${data.error_clip_count || 0}</div></div>
      <div class="live-card"><div class="hint">Pending / active</div><div class="value">${(data.pending_clip_count || 0) + (data.uploading_clip_count || 0)}</div></div>
      <div class="live-card"><div class="hint">Uploaded size</div><div class="value">${fmtBytes(data.uploaded_bytes || 0)}</div></div>
      <div class="live-card"><div class="hint">Average upload time</div><div class="value">${fmtDurationText(data.average_upload_duration_s)}</div></div>
      <div class="live-card"><div class="hint">Average throughput</div><div class="value">${fmtBytes(data.average_throughput_bps || 0)}/s</div></div>
    `;
    renderSystemStats(stats.system || {});
    renderStatsDestinations(stats.destinations || []);
  } catch (e) {
    overview.textContent = "Unable to load stats: " + e.message;
    destinations.textContent = "";
    if (system) system.textContent = "";
  }
}

function getTimelineMinutes() {
  const input = document.getElementById("timelineMinutes");
  const value = Number(input?.value || 30);
  if (!Number.isFinite(value)) return 30;
  return Math.max(5, Math.min(1440, value));
}

function fmtDurationText(seconds) {
  if (!seconds) return "n/a";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

function pushNamedHistory(group, name, value, max = 60) {
  const store = appState.systemHistory[group] || {};
  appState.systemHistory[group] = store;
  if (!store[name]) store[name] = [];
  store[name].push(Number(value || 0));
  while (store[name].length > max) store[name].shift();
  return store[name];
}

function renderTimeSeriesChart(series, opts = {}) {
  // The plot SVG is stretched to fill its box (preserveAspectRatio="none") so
  // the line spans the full width. That stretch would warp any text drawn
  // inside it, so ALL labels (axis values, time range) are rendered as HTML
  // overlays at real CSS font sizes and aligned to the gridlines instead.
  const width = opts.width || 360;
  const height = opts.height || 140;
  const padV = 8; // vertical inset (user units) so peaks/troughs don't clip
  const rows = series.filter(row => row.values?.length);
  if (!rows.length) return "<span class='hint'>No time-series data yet.</span>";
  const points = Math.max(...rows.map(row => row.values.length));
  const values = rows.flatMap(row => row.values.map(Number)).filter(Number.isFinite);
  const minValue = opts.min ?? Math.min(0, ...values);
  const maxValue = opts.max ?? Math.max(1, ...values);
  const span = Math.max(1, maxValue - minValue);
  const innerHeight = height - padV * 2;
  const xAt = idx => (points <= 1 ? 0 : (idx / (points - 1)) * width);
  const yAt = value => padV + innerHeight - ((Number(value || 0) - minValue) / span) * innerHeight;
  const fmt = value => (opts.format ? opts.format(value) : Number(value || 0).toFixed(0));

  // Min / mid / max gridlines, full-width in the SVG; their value labels are
  // emitted as HTML positioned at the same vertical percentage.
  const ticks = [1, 0.5, 0].map(t => {
    const y = padV + innerHeight - t * innerHeight;
    return { pct: (y / height) * 100, yUnits: y, label: fmt(minValue + t * span) };
  });
  const grid = ticks
    .map(tick => `<line class="chart-grid-line" x1="0" y1="${tick.yUnits.toFixed(1)}" x2="${width}" y2="${tick.yUnits.toFixed(1)}"></line>`)
    .join("");

  const paths = rows.map((row, idx) => {
    const coords = row.values.map((value, pointIdx) => `${xAt(pointIdx).toFixed(1)},${yAt(value).toFixed(1)}`);
    const line = coords.length ? `M ${coords.join(" L ")}` : "";
    const area = coords.length
      ? `M ${coords.join(" L ")} L ${xAt(row.values.length - 1).toFixed(1)},${(padV + innerHeight).toFixed(1)} L 0,${(padV + innerHeight).toFixed(1)} Z`
      : "";
    const latest = row.values[row.values.length - 1] || 0;
    return `<g class="chart-series ${esc(row.cls || `series-${idx + 1}`)}">
      <title>${esc(row.label || "Series")}: ${esc(fmt(latest))}</title>
      ${opts.area === false ? "" : `<path class="chart-area" d="${area}"></path>`}
      <path class="chart-line" d="${line}"></path>
    </g>`;
  }).join("");

  const yLabels = ticks
    .map(tick => `<span style="top:${tick.pct.toFixed(2)}%">${esc(tick.label)}</span>`)
    .join("");

  const xLabels = (opts.xLabels && opts.xLabels.length ? opts.xLabels : ["older", "now"]).filter(Boolean);
  const xAxis = xLabels.length
    ? `<div class="time-chart-xaxis">${xLabels.map(label => `<span>${esc(label)}</span>`).join("")}</div>`
    : "";

  const legend = rows.map(row => {
    const latest = row.values[row.values.length - 1] || 0;
    return `<span class="${esc(row.cls || "")}"><i></i>${esc(row.label || "Series")} <strong>${esc(fmt(latest))}</strong></span>`;
  }).join("");

  return `<div class="time-chart-wrap">
    ${opts.title ? `<div class="time-chart-head"><strong>${esc(opts.title)}</strong>${opts.yUnit ? `<span class="hint">${esc(opts.yUnit)}</span>` : ""}</div>` : ""}
    <div class="time-chart-plot">
      <div class="time-chart-yaxis">${yLabels}</div>
      <svg class="time-chart ${esc(opts.cls || "")}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${esc(opts.title || "Time-series chart")}">
        ${grid}
        ${paths}
      </svg>
    </div>
    ${xAxis}
    <div class="time-chart-legend">${legend}</div>
  </div>`;
}

function renderSparkline(values, cls = "", opts = {}) {
  return renderTimeSeriesChart(
    [{ label: opts.label || "Current", values, cls }],
    {
      cls,
      title: opts.title,
      height: opts.height || 116,
      max: opts.max,
      min: opts.min,
      format: opts.format,
      yUnit: opts.yUnit,
      xLabels: opts.xLabels || ["older", "now"],
    },
  );
}

function renderUploadTimeline(timeline) {
  const points = timeline?.points || [];
  if (!points.length) return "<span class='hint'>No upload timeline data.</span>";
  const first = points[0];
  const last = points[points.length - 1];
  const chart = renderTimeSeriesChart(
    [
      { label: "Completed", values: points.map(point => point.uploaded_bytes || 0), cls: "uploaded-line" },
      { label: "Active", values: points.map(point => point.active_bytes || 0), cls: "active-line" },
      { label: "Errored", values: points.map(point => point.error_bytes || 0), cls: "error-line" },
    ],
    {
      title: "Upload throughput by bucket",
      cls: "upload-series",
      height: 170,
      format: fmtBytes,
      yUnit: "bytes / bucket",
      xLabels: [fmtDateTime(first?.start) || "", fmtDateTime(last?.end) || ""],
    },
  );
  return `
    <div class="upload-timeline">
      <div class="row spread">
        <div>
          <strong>Upload timeline</strong>
          <div class="hint">Last ${Math.round((timeline.hours || getTimelineMinutes() / 60) * 60)} minutes · ${timeline.bucket_minutes || "?"} minute buckets</div>
        </div>
        <div class="timeline-totals">
          <span><i class="legend-app"></i>${fmtBytes(timeline.total_uploaded_bytes || 0)} completed</span>
          <span><i class="legend-active"></i>${fmtBytes(timeline.total_active_bytes || 0)} active</span>
          <span><i class="legend-error"></i>${fmtBytes(timeline.total_error_bytes || 0)} errored</span>
        </div>
      </div>
      ${chart}
    </div>
  `;
}

function renderGauge(percent, label) {
  const pct = percent == null ? 0 : Math.max(0, Math.min(100, Number(percent)));
  const text = percent == null ? "n/a" : `${pct.toFixed(1)}%`;
  return `<div class="system-gauge" style="--pct:${pct}">
    <span>${esc(text)}</span>
    <small>${esc(label)}</small>
  </div>`;
}

function renderSystemStats(system) {
  const el = document.getElementById("systemStats");
  if (!el) return;
  const cpu = system.cpu || {};
  const network = system.network || {};
  const timeline = network.upload_timeline || system.upload_timeline || {};
  const filesystems = system.filesystems || [];
  // History is stored server-side, so the graphs show the whole window the
  // moment the page loads instead of filling in live in the browser.
  const cpuHistory = (cpu.history || []).map(point => point.v);
  const rxHistory = (network.rx_history || []).map(point => point.v);
  const txHistory = (network.tx_history || []).map(point => point.v);
  const windowLabel = `${getTimelineMinutes()}m ago`;
  el.innerHTML = `
    <div class="system-grid">
      <div class="system-card">
        <div class="system-head">
          <h3>CPU</h3>
          <span class="hint">${esc(cpu.cpu_count || "n/a")} cores</span>
        </div>
        <div class="system-visual">
          ${renderGauge(cpu.percent, "current")}
          ${renderSparkline(cpuHistory, "cpu-line", {
            title: "CPU trend",
            max: 100,
            format: value => `${Math.round(value)}%`,
            yUnit: "% busy",
            xLabels: [windowLabel, "now"],
          })}
        </div>
        <div class="metric-row">
          <span>Load 1m <strong>${cpu.load_1m ?? "n/a"}</strong></span>
          <span>5m <strong>${cpu.load_5m ?? "n/a"}</strong></span>
          <span>15m <strong>${cpu.load_15m ?? "n/a"}</strong></span>
        </div>
      </div>
      <div class="system-card">
        <div class="system-head">
          <h3>Network</h3>
          <span class="hint">all non-loopback interfaces</span>
        </div>
        <div class="network-graphs">
          <div>
            <div class="metric-row"><span>Down <strong>${fmtBitrate(network.rx_bytes_per_s || 0)}</strong></span></div>
            ${renderSparkline(rxHistory, "rx-line", {
              title: "Download trend",
              format: value => fmtBitrate(value),
              yUnit: "bitrate",
              xLabels: [windowLabel, "now"],
            })}
          </div>
          <div>
            <div class="metric-row"><span>Up <strong>${fmtBitrate(network.tx_bytes_per_s || 0)}</strong></span></div>
            ${renderSparkline(txHistory, "tx-line", {
              title: "Upload trend",
              format: value => fmtBitrate(value),
              yUnit: "bitrate",
              xLabels: [windowLabel, "now"],
            })}
          </div>
        </div>
        <div class="metric-row">
          <span>Total down <strong>${fmtBytes(network.rx_bytes_total || 0)}</strong></span>
          <span>Total up <strong>${fmtBytes(network.tx_bytes_total || 0)}</strong></span>
        </div>
        ${renderUploadTimeline(timeline)}
      </div>
    </div>
    <div class="filesystem-grid">
      ${filesystems.map(renderFilesystemBar).join("") || "<span class='hint'>No filesystem data available.</span>"}
    </div>
  `;
}

function renderFilesystemBar(fs) {
  const pct = fs.used_percent == null ? 0 : Math.max(0, Math.min(100, Number(fs.used_percent)));
  const historyKey = fs.path || fs.label || "filesystem";
  const history = pushNamedHistory("filesystems", historyKey, pct);
  return `<div class="fs-card">
    <div class="row spread">
      <div>
        <strong>${esc(fs.label || fs.path || "Filesystem")}</strong>
        <div class="hint" title="${esc(fs.path || "")}">${esc(fs.path || "")}</div>
      </div>
      <strong>${fs.used_percent == null ? "n/a" : `${pct.toFixed(1)}%`}</strong>
    </div>
    <div class="fs-bar"><span style="width:${pct}%"></span></div>
    <div class="metric-row">
      <span>Used <strong>${fmtBytes(fs.used_bytes || 0)}</strong></span>
      <span>Free <strong>${fmtBytes(fs.free_bytes || 0)}</strong></span>
      <span>Total <strong>${fmtBytes(fs.total_bytes || 0)}</strong></span>
    </div>
    ${renderSparkline(history, "fs-line", {
      title: "Used space trend",
      min: 0,
      max: 100,
      format: value => `${Math.round(value)}%`,
      height: 98,
    })}
    ${fs.error ? `<div class="hint">Usage unavailable: ${esc(fs.error)}</div>` : ""}
  </div>`;
}

function renderStatsDestinations(rows) {
  const el = document.getElementById("statsDestinations");
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = "<span class='hint'>No destinations configured.</span>";
    return;
  }
  el.innerHTML = rows.map(row => {
    const storage = row.storage || {};
    return `<div class="usage-card">
      <div class="usage-head">
        <div>
          <h3>${esc(row.name)}</h3>
          <div class="hint">${esc(row.type)} · ${esc(row.base_path || "")}</div>
        </div>
        <div class="hint">${row.uploaded_clip_count || 0} uploaded · ${row.error_clip_count || 0} errors · ${(row.pending_clip_count || 0) + (row.uploading_clip_count || 0)} pending/active</div>
      </div>
      <div class="usage-body">
        ${renderStoragePie(storage)}
        <div class="usage-facts">
          <div><span class="hint">App footage</span><strong>${fmtBytes(storage.bytes_uploaded_by_app || row.uploaded_bytes || 0)}</strong></div>
          <div><span class="hint">NAS used</span><strong>${storage.used_bytes == null ? "Unknown" : fmtBytes(storage.used_bytes)}</strong></div>
          <div><span class="hint">NAS free</span><strong>${storage.free_bytes == null ? "Unknown" : fmtBytes(storage.free_bytes)}</strong></div>
          <div><span class="hint">NAS total</span><strong>${storage.total_bytes == null ? "Unknown" : fmtBytes(storage.total_bytes)}</strong></div>
          <div><span class="hint">Average</span><strong>${fmtDurationText(row.average_upload_duration_s)} · ${fmtBytes(row.average_throughput_bps || 0)}/s</strong></div>
        </div>
      </div>
      ${renderDestinationTimeline(row.upload_timeline)}
      ${storage.error ? `<div class="hint">Storage check failed: ${esc(storage.error)}</div>` : ""}
    </div>`;
  }).join("");
}

function renderDestinationTimeline(timeline) {
  const points = timeline?.points || [];
  if (!points.length) return "<span class='hint'>No destination upload timeline data.</span>";
  const first = points[0];
  const last = points[points.length - 1];
  return `<div class="destination-timeline">
    ${renderTimeSeriesChart(
      [
        { label: "Completed", values: points.map(point => point.uploaded_bytes || 0), cls: "uploaded-line" },
        { label: "Active", values: points.map(point => point.active_bytes || 0), cls: "active-line" },
        { label: "Errored", values: points.map(point => point.error_bytes || 0), cls: "error-line" },
      ],
      {
        title: `Destination timeline · ${timeline.bucket_minutes || "?"} minute buckets`,
        cls: "destination-series",
        height: 150,
        format: fmtBytes,
        yUnit: "bytes / bucket",
        xLabels: [fmtDateTime(first?.start) || "", fmtDateTime(last?.end) || ""],
      },
    )}
  </div>`;
}

function renderStoragePie(storage) {
  const total = Number(storage.total_bytes || 0);
  const used = Math.max(0, Number(storage.used_bytes || 0));
  const appBytes = Math.max(0, Number(storage.bytes_uploaded_by_app || 0));
  if (!total) {
    return `<div class="usage-pie unknown"><span>?</span></div>`;
  }
  const appPct = Math.min(100, (appBytes / total) * 100);
  const usedPct = Math.min(100, Math.max(appPct, (used / total) * 100));
  const appLabel = appPct < 0.1 && appBytes > 0 ? "<0.1" : appPct.toFixed(1);
  return `
    <div class="usage-pie-wrap">
      <div class="usage-pie" style="background:conic-gradient(var(--acc) 0 ${appPct}%, var(--gold) ${appPct}% ${usedPct}%, var(--surface-2) ${usedPct}% 100%)">
        <span>${appLabel}%</span>
      </div>
      <div class="pie-legend">
        <span><i class="legend-app"></i>Uploaded footage</span>
        <span><i class="legend-other"></i>Other used</span>
        <span><i class="legend-free"></i>Free</span>
      </div>
    </div>`;
}

// ============================ SETTINGS ======================================

function initSettings() {
  ensureGlobalJobPolling();
  loadSettingsPage();
  clearInterval(appState.settingsPoller);
  appState.settingsPoller = setInterval(() => {
    loadUploadLedger();
    loadAppLogs(false);
  }, 4000);
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
  loadAppLogs();
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

async function loadAppLogs(showLoading = true) {
  const el = document.getElementById("appLogs");
  if (!el) return;
  const level = document.getElementById("logLevel")?.value || "INFO";
  if (showLoading) el.textContent = "Loading logs…";
  try {
    const data = await api.get(`/api/logs?limit=700&min_level=${encodeURIComponent(level)}`);
    const rows = data.lines || [];
    if (!rows.length) {
      el.innerHTML = "<span class='hint'>No log lines at this level.</span>";
      return;
    }
    el.innerHTML = rows.map(row => {
      const levelClass = `log-${String(row.level || "INFO").toLowerCase()}`;
      return `<div class="log-line ${levelClass}"><span>${esc(row.level || "INFO")}</span><code>${esc(row.message || "")}</code></div>`;
    }).join("");
    el.scrollTop = el.scrollHeight;
  } catch (e) {
    el.textContent = "Unable to load logs: " + e.message;
  }
}
