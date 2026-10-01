// The main camera-to-NAS flow. No frameworks or thumbnail work on these pages.
const workflowState = { days: new Set(), clipCounts: {}, verification: null, busy: false };

function workflowError(message) { toast(message, 7000); }
function numberOrDash(value, suffix = '') { return value == null ? '—' : `${value}${suffix}`; }
function activityMarkup(jobs) {
  return `<div class="row spread"><span>${jobs.active ? `${jobs.running} running · ${jobs.queued} queued · ${jobs.paused} paused` : 'All jobs finished'}</span><span class="hint">${jobs.error ? `${jobs.error} jobs need attention` : 'Queue ready'}</span></div>`;
}
function pollWorkflow(fn, interval = 15000) {
  let running = false;
  async function tick() {
    if (running || document.hidden) return;
    running = true;
    try { await fn(); } catch (e) { workflowError(e.message); }
    finally { running = false; }
  }
  tick();
  const timer = setInterval(tick, interval);
  window.addEventListener('pagehide', () => clearInterval(timer), { once: true });
}

function initWorkflowOverview() {
  ensureGlobalJobPolling();
  pollWorkflow(async () => {
    const data = await api.get('/api/workflow/overview');
    const cpu = data.cpu;
    document.getElementById('overviewMetrics').innerHTML = [
      ['Camera videos', data.cameras.reduce((n, c) => n + c.video_count, 0), 'On connected cameras'],
      ['Archived clips', data.library.uploaded_clip_count, `${fmtBytes(data.library.uploaded_bytes)} transferred`],
      ['Recording days', data.recording_days, 'Ready to organise into trips'],
      ['Pi CPU', numberOrDash(cpu.percent, '%'), `Load ${numberOrDash(cpu.load_1m)} · Sending ${fmtBitrate(data.network.tx_bytes_per_s)}`],
    ].map(([label, value, hint]) => `<div class="metric-card"><span>${esc(label)}</span><strong>${esc(String(value))}</strong><small>${esc(hint)}</small></div>`).join('');
    document.getElementById('cameraStatus').textContent = data.cameras.length ? 'Connected' : 'Not connected';
    document.getElementById('cameraOverview').innerHTML = data.cameras.length ? data.cameras.map(c => `<div class="storage-row"><h3>${esc(c.label)}</h3><p>${c.video_count} videos · ${fmtBytes(c.video_bytes)}</p><p class="hint">${esc(c.path)}<br>${c.free_bytes == null ? '' : `${fmtBytes(c.free_bytes)} free of ${fmtBytes(c.total_bytes)}`}</p>${c.error ? `<p class="error-text">${esc(c.error)}</p>` : ''}</div>`).join('') : '<p class="empty-state">Connect your Drift camera over USB to start importing.</p>';
    document.getElementById('nasOverview').innerHTML = data.destinations.length ? data.destinations.map(d => `<div class="storage-row"><div class="row spread"><h3>${esc(d.name)}</h3><span class="status-pill">${d.available ? 'Available' : 'Offline'}</span></div><p class="hint">${esc(d.base_path)}</p>${d.available ? `<p>${fmtBytes(d.free_bytes)} free of ${fmtBytes(d.total_bytes)}</p><progress max="${d.total_bytes}" value="${d.total_bytes - d.free_bytes}" aria-label="NAS storage used"></progress>` : `<p class="error-text">${esc(d.error)}</p>`}</div>`).join('') : '<p class="empty-state">Add a mounted NAS destination in Destinations.</p>';
    document.getElementById('overviewActivity').innerHTML = activityMarkup(data.jobs);
    document.getElementById('hostOverview').textContent = `Memory: ${data.host.memory_total_bytes == null ? 'unavailable' : `${fmtBytes(data.host.memory_used_bytes)} / ${fmtBytes(data.host.memory_total_bytes)}`} · Temperature: ${numberOrDash(data.host.temperature_c, '°C')} · Uptime: ${data.host.uptime_s == null ? '—' : `${Math.floor(data.host.uptime_s / 3600)} hours`}`;
  });
}

function setOptions(id, values, key, label) {
  const select = document.getElementById(id);
  const previous = select.value;
  const signature = JSON.stringify(values.map(v => [v[key], label(v)]));
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  select.replaceChildren(...values.map(value => new Option(label(value), value[key])));
  if (values.some(v => String(v[key]) === previous)) select.value = previous;
}

function initWorkflowImport() {
  ensureGlobalJobPolling();
  pollWorkflow(async () => {
    const data = await api.get('/api/workflow/overview');
    setOptions('workflowCamera', data.cameras, 'path', c => `${c.label} · ${c.video_count} videos`);
    setOptions('workflowDestination', data.destinations, 'id', d => `${d.name}${d.available ? '' : ' (offline)'}`);
    const camera = data.cameras.find(c => c.path === document.getElementById('workflowCamera').value);
    const dest = data.destinations.find(d => String(d.id) === document.getElementById('workflowDestination').value);
    const ready = camera && !camera.error && camera.video_count && dest?.available;
    document.getElementById('importCameraButton').disabled = !ready || workflowState.busy;
    document.getElementById('verifyCameraButton').disabled = !ready || workflowState.busy;
    document.getElementById('importSummary').textContent = !camera ? 'Connect your Drift camera over USB.' : !dest ? 'Add a mounted NAS destination first.' : `${camera.video_count} videos · ${fmtBytes(camera.video_bytes)} on camera → ${dest.name}${dest.available ? ` · ${fmtBytes(dest.free_bytes)} free` : ` · ${dest.error}`}`;
    document.getElementById('importActivity').innerHTML = activityMarkup(data.jobs);
    await refreshVerification();
  }, 10000);
}

async function startCameraWorkflow(action) {
  if (workflowState.busy) return;
  workflowState.busy = true;
  document.getElementById('importCameraButton').disabled = true;
  document.getElementById('verifyCameraButton').disabled = true;
  try {
    const result = await api.post(`/api/workflow/${action}`, {
      camera_root: document.getElementById('workflowCamera').value,
      destination_id: Number(document.getElementById('workflowDestination').value),
    });
    toast(`${action === 'verify' ? 'Full verification' : `Import of ${result.file_count} videos`} queued · job ${result.job_id}`, 5000);
    if (action === 'verify') await refreshVerification();
  } catch (e) { workflowError(e.message); }
  finally { workflowState.busy = false; }
}

async function refreshVerification() {
  const el = document.getElementById('verificationReport');
  if (!el) return;
  const camera = document.getElementById('workflowCamera').value;
  const destination = document.getElementById('workflowDestination').value;
  if (!camera || !destination) { el.textContent = 'Connect a camera and choose a NAS destination.'; return; }
  const job = await api.get(`/api/workflow/verification?camera_root=${encodeURIComponent(camera)}&destination_id=${destination}`);
  if (!job) { el.textContent = 'Run Verify after importing to check every video against its NAS copy.'; return; }
  if (job.status !== 'done') {
    el.innerHTML = `<p>${esc(job.status)} · ${esc(job.detail || job.description)}</p>${job.error ? `<p class="error-text">${esc(job.error)}</p>` : `<progress max="1" value="${job.progress}" aria-label="Verification progress"></progress>`}`;
    return;
  }
  const r = job.result;
  if (!r) { el.textContent = 'No report available. Run Verify again.'; return; }
  const failures = r.files.filter(file => file.status !== 'verified');
  el.innerHTML = `<div class="verification-banner ${r.ok ? 'verified' : 'needs-action'}"><strong>${r.ok ? 'All camera videos match their NAS copies' : 'Some videos still need attention'}</strong><p>${r.verified} / ${r.total} verified · ${r.missing} missing · ${r.mismatch} mismatched · ${r.errors} errors</p><small>${esc(fmtDateTime(r.checked_at))} · ${esc(r.method)} · camera snapshot at this time</small></div>${failures.length ? `<details open><summary>Videos needing attention (${failures.length})</summary>${failures.map(f => `<div class="verification-file"><strong>${esc(f.filename)}</strong><span>${esc(f.status)}${f.error ? ' · ' + esc(f.error) : ''}</span></div>`).join('')}</details>` : ''}`;
}

function initDayTrips() {
  ensureGlobalJobPolling();
  document.getElementById('customTrips').addEventListener('toggle', event => {
    if (event.target.open) loadAlbums().catch(e => workflowError(e.message));
  });
  pollWorkflow(async () => {
    const data = await api.get('/api/workflow/overview');
    setOptions('workflowDestination', data.destinations, 'id', d => `${d.name}${d.available ? '' : ' (offline)'}`);
    await loadDaySuggestions();
    await loadTripMovies();
  }, 20000);
}

async function loadDaySuggestions() {
  const dest = document.getElementById('workflowDestination').value;
  const el = document.getElementById('daySuggestions');
  if (!dest) { el.textContent = 'Add a mounted NAS destination, then import footage to discover recording days.'; return; }
  const days = await api.get(`/api/trips/suggestions?destination_id=${dest}`);
  workflowState.clipCounts = Object.fromEntries(days.map(day => [day.day, day.clip_count]));
  for (const selected of workflowState.days) {
    if (!days.some(d => d.day === selected && d.ready)) workflowState.days.delete(selected);
  }
  el.replaceChildren();
  if (!days.length) el.innerHTML = '<p class="empty-state">No recording days yet. Import your camera footage to get started.</p>';
  for (const day of days) {
    const row = document.createElement('div');
    row.className = 'day-row';
    row.innerHTML = `<label class="day-choice"><input type="checkbox" ${workflowState.days.has(day.day) ? 'checked' : ''} ${!day.ready ? 'disabled' : ''}><span><strong>${esc(day.day)}</strong><small>${day.clip_count} clips · ${day.duration_s ? fmtDur(day.duration_s) : 'Duration unknown'} · ${fmtBytes(day.size_bytes)}</small></span></label><div class="day-status"><span class="status-pill">${esc(day.status)}</span><small>${day.archived_count} / ${day.clip_count} on NAS</small></div><button ${!day.ready || day.clip_count < 2 || ['queued', 'running', 'paused'].includes(day.status) ? 'disabled' : ''}>${day.status === 'complete' ? 'Create again' : 'Create day movie'}</button>${day.clip_count < 2 ? '<small class="hint">Select with another day to combine at least two clips.</small>' : ''}${day.error ? `<p class="error-text day-error">${esc(day.error)}</p>` : ''}`;
    row.querySelector('input').onchange = event => {
      if (event.target.checked) workflowState.days.add(day.day); else workflowState.days.delete(day.day);
      updateDaySelection();
    };
    row.querySelector('button').onclick = () => createDayMovie([day.day]);
    el.append(row);
  }
  updateDaySelection();
}

function updateDaySelection() {
  const clips = [...workflowState.days].reduce((n, day) => n + (workflowState.clipCounts[day] || 0), 0);
  document.getElementById('combineDaysButton').disabled = clips < 2 || workflowState.busy;
  document.getElementById('selectedDaysCount').textContent = `${workflowState.days.size} days · ${clips} clips selected`;
}
function createSelectedDayTrip() { createDayMovie([...workflowState.days], document.getElementById('dayTripName').value); }
async function createDayMovie(days, name = '') {
  if (workflowState.busy) return;
  workflowState.busy = true;
  updateDaySelection();
  try {
    const result = await api.post('/api/trips/from-days', { days, name, destination_id: Number(document.getElementById('workflowDestination').value) });
    toast(`Trip ${result.already_queued ? 'already queued' : 'queued'} · job ${result.job_id}`);
    workflowState.days.clear();
    await loadDaySuggestions();
  } catch (e) { workflowError(e.message); }
  finally { workflowState.busy = false; updateDaySelection(); }
}

async function loadTripMovies() {
  const movies = await api.get('/api/trips/movies');
  const el = document.getElementById('tripMovies');
  el.replaceChildren();
  if (!movies.length) { el.innerHTML = '<p class="empty-state">Your completed day and multi-day movies will appear here.</p>'; return; }
  for (const movie of movies) {
    const row = document.createElement('div');
    row.className = 'movie-row';
    row.innerHTML = `<div><h3>${esc(movie.name)}</h3><p class="hint">${esc(movie.days.join(' · '))} · ${fmtBytes(movie.size_bytes)} · ${fmtDur(movie.duration_s)}</p><small class="movie-path">${esc(movie.path)}</small></div><div class="row"><button class="ghost">Watch</button><a href="/api/media/${movie.media_id}/stream" download>Download</a></div>`;
    row.querySelector('button').onclick = () => {
      document.getElementById('tripPlayer').src = `/api/media/${movie.media_id}/stream`;
      document.getElementById('tripPlayerDlg').showModal();
    };
    el.append(row);
  }
}
function closeTripPlayer() {
  const player = document.getElementById('tripPlayer');
  player.pause(); player.removeAttribute('src'); player.load();
  document.getElementById('tripPlayerDlg').close();
}
