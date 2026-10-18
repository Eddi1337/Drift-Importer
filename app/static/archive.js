// NAS inventory and deliberate duplicate review. Reading a report never scans files.
(() => {
  const state = {
    mode: null, summary: null, report: null, loading: false, refreshAgain: false,
    poller: null, activeJob: null, pendingJobs: new Map(), busy: false,
    fileOffset: 0, fileTotal: 0, filesLoading: false, groupOffset: 0,
    choices: new Map(), confirmation: null,
  };
  const PAGE_SIZE = 25;
  const node = id => document.getElementById(id);
  const number = value => (Number(value) || 0).toLocaleString();
  const active = job => job && ["queued", "running"].includes(job.status);
  const groupFiles = group => Array.isArray(group.files) ? group.files : [];
  const pathOf = file => String(file.path || file.relative_path || file.filename || "Unknown path");
  const fileId = file => String(file.id);
  const statusOf = group => group.status === "already_linked" ? "linked" : group.status;

  async function request(method, url, body) {
    const response = await fetch(url, {
      method, headers: body ? {"Content-Type": "application/json"} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail || data.message;
      throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status}).`);
    }
    return data;
  }

  function error(message, dialog = false) {
    const target = node(dialog ? "hardlinkError" : "archiveError");
    if (target) { target.textContent = message || ""; target.hidden = !message; }
  }

  function metrics(target, cards) {
    if (!target) return;
    renderIfChanged(target, cards.map(([label, value, detail, warn]) =>
      `<div class="live-card"><div class="hint">${esc(label)}</div><div class="value${warn ? " needs-attention" : ""}">${esc(value)}</div><small>${esc(detail)}</small></div>`).join(""));
  }

  function jobOf(data) {
    const value = data?.active_job;
    if (value && typeof value === "object") return value;
    return value ? {id: value, status: "running"} : null;
  }

  function renderActivity() {
    const job = state.activeJob || [...state.pendingJobs.values()].find(active);
    const status = node("archiveScanStatus");
    if (status) {
      if (job) {
        const percent = Number.isFinite(Number(job.progress)) ? ` · ${Math.round(Number(job.progress) * 100)}%` : "";
        const description = job.description || "Archive check";
        status.innerHTML = `<span>${esc(description)} · ${esc(job.status || "queued")}${esc(percent)}</span> <a href="/jobs">View job ${esc(job.id || "")} →</a>`;
      } else {
        const scan = state.summary?.last_scan;
        const date = typeof scan === "string" ? scan : scan?.completed_at || scan?.finished_at || scan?.created_at;
        const scanError = typeof scan === "object" && scan?.error;
        status.textContent = date ? `Last scan: ${fmtDateTime(date)}${scanError ? " · check needs attention" : ""}` : "No completed NAS scan yet. Run a scan to discover older videos.";
      }
    }
    const isWorking = state.busy || active(job);
    if (node("archiveScanButton")) node("archiveScanButton").disabled = isWorking;
    if (node("archiveRefreshButton")) node("archiveRefreshButton").disabled = state.loading;
    document.querySelectorAll(".archive-group-action").forEach(button => { button.disabled = isWorking || button.dataset.unavailable === "true"; });
    if (active(job) || [...state.pendingJobs.values()].some(active)) schedulePoll();
    else { clearTimeout(state.poller); state.poller = null; }
  }

  function schedulePoll() {
    if (state.poller) return;
    state.poller = setTimeout(() => {
      state.poller = null;
      if (!document.hidden) refresh(false);
      else schedulePoll();
    }, 8000);
  }

  function renderSummary(data) {
    if (!data) return;
    state.summary = data;
    state.activeJob = jobOf(data);
    metrics(node("archiveMetrics"), [
      ["Saved files", number(data.total_files), `${fmtBytes(data.total_bytes || 0)} in the archive`],
      ["Present on NAS", number(data.present_files), "Present at the last scan or upload"],
      ["Missing uploads", number(data.missing_files), "Expected saved files not found by the last scan", Number(data.missing_files) > 0],
      ["Not checked yet", number(data.unchecked_files), "Known uploads awaiting a storage check"],
    ]);
    renderActivity();
  }

  async function refresh(manual = true) {
    if (state.loading) { state.refreshAgain = true; return; }
    state.loading = true;
    if (manual) error("");
    renderActivity();
    try {
      const [summary, report] = await Promise.all([
        request("GET", "/api/archive/summary"),
        state.mode === "duplicates" ? request("GET", "/api/archive/duplicates") : Promise.resolve(null),
      ]);
      renderSummary(summary);
      if (report) { state.report = report; state.activeJob = jobOf(report) || state.activeJob; renderDuplicates(); }
      await Promise.all([...state.pendingJobs.keys()].map(async id => {
        const result = await request("GET", `/api/jobs/${encodeURIComponent(id)}`);
        const job = result.job || result;
        if (active(job)) state.pendingJobs.set(id, job);
        else {
          state.pendingJobs.delete(id);
          if (job.status === "error" || job.status === "cancelled") error(job.error || `Archive job ${id} ${job.status}. Check Jobs for details.`);
          // The report read may have preceded the job's final database commit.
          if (job.status === "done") state.refreshAgain = true;
        }
      }));
      if (state.mode === "stats" && node("archiveFilesDetails")?.open) await loadFiles();
    } catch (e) {
      error(`Unable to refresh the archive: ${e.message}`);
      // Do not leave a failed initial load looking as though it is still loading.
      ["archiveMetrics", "duplicateMetrics", "duplicateGroups"].forEach(id => {
        const target = node(id);
        if (target?.querySelector(".loading-placeholder")) target.innerHTML = '<p class="hint">Report unavailable. Try Refresh report.</p>';
      });
    } finally {
      state.loading = false;
      renderActivity();
      if (state.refreshAgain) { state.refreshAgain = false; refresh(false); }
    }
  }

  async function queue(url, body, label) {
    if (state.busy || active(state.activeJob)) return;
    state.busy = true; error(""); renderActivity();
    try {
      const result = await request("POST", url, body);
      if (result.job_id !== null && result.job_id !== undefined) {
        state.pendingJobs.set(String(result.job_id), {id: result.job_id, status: "queued", description: label});
      }
      toast(`${label} queued. Progress is available in Jobs.`, 5000);
      await refresh(false);
    } catch (e) { error(e.message); }
    finally { state.busy = false; renderActivity(); }
  }

  async function loadFiles() {
    if (state.filesLoading || !node("archiveFilesList")) return;
    state.filesLoading = true;
    const filter = node("archiveFileStatus")?.value || "all";
    node("archiveFilesPrevious").disabled = true; node("archiveFilesNext").disabled = true;
    try {
      const result = await request("GET", `/api/archive/files?limit=${PAGE_SIZE}&offset=${state.fileOffset}&status=${encodeURIComponent(filter)}`);
      const files = result.files || result.items || [];
      state.fileTotal = Number(result.total || 0);
      node("archiveFileCount").textContent = `${number(state.fileTotal)} files`;
      if (!files.length) node("archiveFilesList").innerHTML = '<p class="empty-state">No files match this view. Scan the NAS to discover saved videos.</p>';
      else node("archiveFilesList").innerHTML = `<div class="table-scroll"><table class="archive-file-table"><thead><tr><th scope="col">Saved file</th><th scope="col">Size</th><th scope="col">Storage check</th></tr></thead><tbody>${files.map(file => {
        const status = file.status || file.presence || "unchecked";
        const label = {present: "Present", missing: "Missing", unchecked: "Not checked"}[status] || status;
        return `<tr><td><span class="archive-path">${esc(pathOf(file))}</span>${file.destination_name ? `<small class="hint">${esc(file.destination_name)}</small>` : ""}</td><td>${esc(fmtBytes(file.size_bytes || file.size || 0))}</td><td><span class="status-pill${status === "missing" ? " failed" : status === "unchecked" ? " unsaved" : ""}">${esc(label)}</span></td></tr>`;
      }).join("")}</tbody></table></div>`;
      node("archiveFilesPage").textContent = files.length ? `${number(state.fileOffset + 1)}–${number(state.fileOffset + files.length)} of ${number(state.fileTotal)}` : "";
    } catch (e) { error(`Unable to load saved files: ${e.message}`); }
    finally {
      state.filesLoading = false;
      node("archiveFilesPrevious").disabled = state.fileOffset === 0;
      node("archiveFilesNext").disabled = state.fileOffset + PAGE_SIZE >= state.fileTotal;
    }
  }

  function sameInode(left, right) {
    return left && right && left.inode != null && left.device != null && String(left.inode) === String(right.inode) && String(left.device) === String(right.device);
  }

  function choiceFor(group) {
    const files = groupFiles(group);
    let choice = state.choices.get(String(group.id));
    if (!choice || !files.some(file => fileId(file) === choice.keeper)) {
      choice = {keeper: fileId(files[0] || {}), targets: new Set(files.slice(1).map(fileId))};
      state.choices.set(String(group.id), choice);
    }
    return choice;
  }

  function renderDuplicates() {
    const report = state.report || {};
    const groups = Array.isArray(report.groups) ? report.groups : [];
    const summary = report.summary || {};
    const candidateCount = groups.filter(group => statusOf(group) === "candidate").length;
    const verifiedCount = groups.filter(group => statusOf(group) === "verified").length;
    const linkedCount = groups.filter(group => statusOf(group) === "linked").length;
    const reclaimable = groups.filter(group => statusOf(group) === "verified").reduce((sum, group) => sum + Number(group.reclaimable_bytes || 0), 0);
    metrics(node("duplicateMetrics"), [
      ["Needs verification", number(summary.candidate_groups ?? candidateCount), "Size and sample matches only"],
      ["Verified identical", number(summary.verified_groups ?? verifiedCount), "Full content hashes match"],
      ["Potential saving", fmtBytes(summary.reclaimable_bytes ?? reclaimable), "Verified copies; storage may use compression or snapshots"],
      ["Already linked", number(summary.already_linked_groups ?? linkedCount), "Already sharing storage; no extra saving"],
    ]);
    const filter = node("duplicateFilter")?.value || "all";
    const filtered = groups.filter(group => filter === "all" || statusOf(group) === filter);
    if (state.groupOffset >= filtered.length) state.groupOffset = Math.max(0, Math.floor((filtered.length - 1) / PAGE_SIZE) * PAGE_SIZE);
    const visible = filtered.slice(state.groupOffset, state.groupOffset + PAGE_SIZE);
    node("duplicateCount").textContent = `${number(filtered.length)} ${filtered.length === 1 ? "group" : "groups"}${filter === "all" ? "" : " in this view"}. Only verified copies can be linked.`;
    const root = node("duplicateGroups");
    if (!visible.length) root.innerHTML = `<div class="empty-state"><strong>${groups.length ? "No groups match this view." : "No duplicate groups in this report."}</strong><p>${groups.length ? "Choose another filter to see the other matches." : "Run a fast scan to inspect the saved videos. An empty report before scanning does not confirm that the archive is unique."}</p></div>`;
    else {
      const signature = JSON.stringify([visible, state.groupOffset, filter]);
      if (root._archiveSignature !== signature) {
        root._archiveSignature = signature;
        root.replaceChildren(...visible.map((group, index) => buildGroup(group, index + state.groupOffset)));
      }
    }
    node("duplicatePrevious").disabled = state.groupOffset === 0;
    node("duplicateNext").disabled = state.groupOffset + PAGE_SIZE >= filtered.length;
    node("duplicatePage").textContent = visible.length ? `${number(state.groupOffset + 1)}–${number(state.groupOffset + visible.length)} of ${number(filtered.length)}` : "";
    renderActivity();
  }

  function buildGroup(group, index) {
    const files = groupFiles(group), status = statusOf(group), verified = status === "verified";
    const choice = choiceFor(group);
    const section = document.createElement("article"); section.className = "archive-duplicate-group";
    section.innerHTML = `<div class="archive-group-heading"><div><h3>Match ${index + 1} <span class="status-pill${verified ? "" : status === "linked" ? "" : " unsaved"}">${esc({candidate: "Sample match", verified: "Verified identical", linked: "Already linked"}[status] || status)}</span></h3><p class="hint">${esc(number(files.length))} files · ${esc(fmtBytes(group.size_bytes || files[0]?.size_bytes || 0))} each${verified && group.reclaimable_bytes ? ` · up to ${esc(fmtBytes(group.reclaimable_bytes))} recoverable` : ""}</p></div><div class="row archive-group-buttons"></div></div><div class="archive-copy-list"></div><p class="archive-group-message hint"></p>`;
    const list = section.querySelector(".archive-copy-list");
    const message = section.querySelector(".archive-group-message");
    const controls = section.querySelector(".archive-group-buttons");
    let linkButton = null;
    function updateChoices() {
      const keeper = files.find(file => fileId(file) === choice.keeper);
      list.querySelectorAll(".archive-copy").forEach(row => {
        const file = files.find(item => fileId(item) === row.dataset.fileId);
        const isKeeper = row.dataset.fileId === choice.keeper;
        const isLinked = sameInode(file, keeper);
        const checkbox = row.querySelector('input[type="checkbox"]');
        const radio = row.querySelector('input[type="radio"]');
        if (radio) radio.checked = isKeeper;
        if (checkbox) {
          checkbox.disabled = isKeeper || isLinked;
          checkbox.checked = !isKeeper && !isLinked && choice.targets.has(row.dataset.fileId);
        }
        const badge = row.querySelector(".archive-copy-label");
        if (badge) badge.textContent = isKeeper ? "Keep this file" : isLinked ? "Already linked to kept file" : "Link this copy";
      });
      if (linkButton) {
        const selected = files.filter(file => fileId(file) !== choice.keeper && !sameInode(file, keeper) && choice.targets.has(fileId(file)));
        linkButton.dataset.unavailable = String(!selected.length);
        linkButton.disabled = !selected.length || state.busy || active(state.activeJob);
        linkButton.textContent = selected.length ? `Review ${selected.length} hardlink${selected.length === 1 ? "" : "s"}` : "Select copies to link";
      }
    }
    files.forEach(file => {
      const row = document.createElement("div"); row.className = "archive-copy"; row.dataset.fileId = fileId(file);
      const date = file.modified_at || file.mtime;
      const modified = typeof date === "number" ? new Date(date * (date < 1e12 ? 1000 : 1)).toLocaleString() : date ? fmtDateTime(date) : "Not recorded";
      row.innerHTML = `<div class="archive-copy-path"><span class="archive-path">${esc(pathOf(file))}</span><small class="hint">${esc(file.root_path || "")} · Modified: ${esc(modified)}${file.mode ? ` · permissions ${esc(Number(file.mode).toString(8))}` : ""}</small></div>`;
      if (verified) {
        const keepLabel = document.createElement("label"); keepLabel.className = "archive-keep-choice";
        const radio = document.createElement("input"); radio.type = "radio"; radio.name = `archive-keeper-${index}`; radio.value = fileId(file);
        radio.setAttribute("aria-label", `Keep ${pathOf(file)}`);
        radio.addEventListener("change", () => { choice.keeper = fileId(file); updateChoices(); });
        keepLabel.append(radio, document.createTextNode("Keep"));
        const targetLabel = document.createElement("label"); targetLabel.className = "archive-target-choice";
        const checkbox = document.createElement("input"); checkbox.type = "checkbox";
        checkbox.setAttribute("aria-label", `Replace duplicate ${pathOf(file)} with a hardlink`);
        checkbox.addEventListener("change", () => { checkbox.checked ? choice.targets.add(fileId(file)) : choice.targets.delete(fileId(file)); updateChoices(); });
        const copyLabel = document.createElement("span"); copyLabel.className = "archive-copy-label";
        targetLabel.append(checkbox, copyLabel);
        row.append(keepLabel, targetLabel);
      } else {
        const badge = document.createElement("span"); badge.className = "hint"; badge.textContent = status === "linked" ? "Shares stored content" : "Full hash needed"; row.append(badge);
      }
      list.append(row);
    });
    if (status === "candidate") {
      message.textContent = "Matching samples can miss differences elsewhere in a video. Full verification reads these files once and caches the result while they stay unchanged.";
      const button = document.createElement("button"); button.type = "button"; button.className = "archive-group-action"; button.textContent = "Verify full content";
      button.addEventListener("click", () => queue(`/api/archive/duplicates/${encodeURIComponent(group.id)}/verify`, {}, "Full duplicate verification")); controls.append(button);
    } else if (verified) {
      message.textContent = "Choose a kept file and the copies to link. The job rechecks full content and compatible metadata before changing a file.";
      linkButton = document.createElement("button"); linkButton.type = "button"; linkButton.className = "archive-group-action";
      linkButton.addEventListener("click", () => showConfirmation(group, choice)); controls.append(linkButton); updateChoices();
    } else message.textContent = "These filenames already share the same stored file. There is no additional duplicate storage to reclaim here.";
    if (group.error || group.message) message.textContent += ` ${group.error || group.message}`;
    return section;
  }

  function showConfirmation(group, choice) {
    const files = groupFiles(group);
    const keeper = files.find(file => fileId(file) === choice.keeper);
    const targets = files.filter(file => fileId(file) !== choice.keeper && !sameInode(file, keeper) && choice.targets.has(fileId(file)));
    if (!keeper || !targets.length || state.busy) return;
    state.confirmation = {group, keeper, targets};
    node("hardlinkKeeper").textContent = pathOf(keeper);
    node("hardlinkTargets").replaceChildren(...targets.map(file => { const li = document.createElement("li"); li.textContent = pathOf(file); return li; }));
    node("hardlinkSavings").textContent = `Up to ${fmtBytes(Number(group.size_bytes || keeper.size_bytes || 0) * targets.length)} of duplicate file data may be released. NAS snapshots and compression can affect the actual saving.`;
    node("hardlinkConsent").checked = false; node("hardlinkConfirm").disabled = true; error("", true);
    node("hardlinkDialog").showModal();
  }

  async function submitHardlinks(event) {
    event.preventDefault();
    const selection = state.confirmation;
    if (!selection || state.busy || !node("hardlinkConsent").checked) return;
    state.busy = true; node("hardlinkConfirm").disabled = true; node("hardlinkCancel").disabled = true; error("", true); renderActivity();
    try {
      const result = await request("POST", `/api/archive/duplicates/${encodeURIComponent(selection.group.id)}/hardlink`, {
        keeper_id: selection.keeper.id, file_ids: selection.targets.map(file => file.id), confirm: true,
      });
      if (result.job_id != null) state.pendingJobs.set(String(result.job_id), {id: result.job_id, status: "queued", description: "Verified hardlink operation"});
      node("hardlinkDialog").close(); state.confirmation = null;
      toast("Hardlink job queued. Files will be rechecked before any changes.", 6000);
      await refresh(false);
    } catch (e) { error(e.message, true); }
    finally { state.busy = false; node("hardlinkCancel").disabled = false; node("hardlinkConfirm").disabled = !node("hardlinkConsent").checked; renderActivity(); }
  }

  function common(mode) {
    state.mode = mode;
    node("archiveRefreshButton").addEventListener("click", () => refresh());
    node("archiveScanButton").addEventListener("click", () => queue("/api/archive/scan", {}, "Fast NAS scan"));
    document.addEventListener("visibilitychange", () => { if (!document.hidden && (state.activeJob || state.pendingJobs.size)) refresh(false); });
    window.addEventListener("pagehide", () => clearTimeout(state.poller));
    refresh(false);
  }

  window.renderArchiveStats = data => { if (state.mode === "stats" && data) renderSummary(data); };
  window.initArchiveStats = () => {
    common("stats");
    node("archiveFilesDetails").addEventListener("toggle", event => { if (event.currentTarget.open) loadFiles(); });
    node("archiveFileStatus").addEventListener("change", () => { state.fileOffset = 0; loadFiles(); });
    node("archiveFilesPrevious").addEventListener("click", () => { state.fileOffset = Math.max(0, state.fileOffset - PAGE_SIZE); loadFiles(); });
    node("archiveFilesNext").addEventListener("click", () => { state.fileOffset += PAGE_SIZE; loadFiles(); });
  };
  window.initDuplicates = () => {
    common("duplicates");
    node("duplicateFilter").addEventListener("change", () => { state.groupOffset = 0; renderDuplicates(); });
    node("duplicatePrevious").addEventListener("click", () => { state.groupOffset = Math.max(0, state.groupOffset - PAGE_SIZE); renderDuplicates(); });
    node("duplicateNext").addEventListener("click", () => { state.groupOffset += PAGE_SIZE; renderDuplicates(); });
    node("hardlinkConsent").addEventListener("change", event => { node("hardlinkConfirm").disabled = !event.target.checked || state.busy; });
    node("hardlinkCancel").addEventListener("click", () => { node("hardlinkDialog").close(); state.confirmation = null; });
    node("hardlinkDialog").addEventListener("cancel", event => { if (state.busy) event.preventDefault(); else state.confirmation = null; });
    node("hardlinkForm").addEventListener("submit", submitHardlinks);
  };
})();
