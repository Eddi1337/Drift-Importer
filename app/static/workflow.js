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
    const dates = await loadRecordingDates();
    const ready = camera && !camera.error && camera.video_count && dest?.available;
    document.getElementById('importCameraButton').disabled = !ready || workflowState.busy || dates.total <= dates.held || !!dates.job;
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
    if (result.held_count) toast(`${result.held_count} recordings are held for date review.`,5000);
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

const dateReviewState = {files:[], selected:[], plan:null, signature:'', root:'', busy:false};
function invalidateDatePreview() {
  dateReviewState.plan = null;
  document.getElementById('datePreview').replaceChildren();
  document.getElementById('dateConfirmButton').hidden = true;
  document.getElementById('dateConfirmButton').disabled = true;
}
function resetDateReview() {
  dateReviewState.signature = '';
  dateReviewState.files = [];
  invalidateDatePreview();
}
function dateLabel(iso) {
  return iso ? new Date(iso).toLocaleString('en-GB', {timeZone:'Europe/London', hour12:false}) : 'Unknown';
}
async function loadRecordingDates() {
  const root = document.getElementById('workflowCamera').value;
  const status = document.getElementById('dateReviewStatus');
  if (!root) { status.textContent = 'Connect your camera to check recording dates.'; return {total:0,held:0}; }
  if (dateReviewState.root !== root) { dateReviewState.root = root; resetDateReview(); }
  const data = await api.get(`/api/recording-dates?camera_root=${encodeURIComponent(root)}`);
  status.innerHTML = `<div class="verification-banner ${data.held || !data.checked_at ? 'needs-action' : 'verified'}"><strong>${data.checked_at ? `${data.total-data.held} recordings ready · ${data.held} need review` : 'Checking camera dates before upload'}</strong><p>${data.failure ? `Correction stopped: ${esc(data.failure.error)} · <a href="/jobs">Retry job ${data.failure.id}</a>` : data.job ? `${esc(data.job.detail || data.job.status)} · <a href="/jobs">View job</a>` : 'Higher folder and DVR numbers must move forward in time. Recent rides are checked against the day the camera connected.'}</p>${data.job && ['queued','running','paused'].includes(data.job.status) ? `<progress max="1" value="${data.job.progress}" aria-label="Date check progress"></progress>` : ''}</div>`;
  document.getElementById('checkDatesButton').disabled = !!data.job || dateReviewState.busy;
  document.getElementById('dateReviewForm').inert = !!data.job || dateReviewState.busy || !data.files.length;
  const signature = JSON.stringify(data.files.map(f => [f.id,f.revision,f.status]));
  if (signature !== dateReviewState.signature) {
    dateReviewState.signature = signature;
    dateReviewState.files = data.files;
    invalidateDatePreview();
    const folders = [...new Set(data.files.map(f=>f.folder))];
    const folder = document.getElementById('dateFolder');
    const old = folder.value;
    folder.replaceChildren(...folders.map(f=>new Option(f,f)));
    if (folders.filter(f=>/\d+MEDIA$/i.test(f)).length>1) folder.add(new Option('All DCIM folders','__dcim__'));
    folder.value = folders.includes(old) || old==='__dcim__' ? old : folders.filter(f=>/\d+MEDIA$/i.test(f)).at(-1) || folders.at(-1) || '';
    updateDateRange(true);
  }
  document.getElementById('datePreviewButton').disabled = !data.files.length || !!data.job || dateReviewState.busy;
  return data;
}
function updateDateRange(rebuild) {
  const folder = document.getElementById('dateFolder').value;
  const files = dateReviewState.files.filter(f=>folder==='__dcim__' ? /\d+MEDIA$/i.test(f.folder) : f.folder===folder);
  if (rebuild) {
    for (const id of ['dateFirst','dateLast']) {
      const el = document.getElementById(id);
      el.replaceChildren(...files.map(f=>new Option(`${f.path.split('/').at(-1)} · ${dateLabel(f.effective_time)}`,String(f.id))));
    }
    if (files.length) document.getElementById('dateLast').value=String(files.at(-1).id);
  }
  const first = files.findIndex(f=>String(f.id)===document.getElementById('dateFirst').value);
  const last = files.findIndex(f=>String(f.id)===document.getElementById('dateLast').value);
  dateReviewState.selected = first>=0 && last>=first ? files.slice(first,last+1) : [];
  const selected = dateReviewState.selected;
  document.getElementById('dateRangeSummary').textContent = selected.length ? `${selected.length} videos · ${dateLabel(selected[0].effective_time)} → ${dateLabel(selected.at(-1).effective_time)} · UK time. ${[...new Set(selected.flatMap(f=>f.reasons))].join('. ')}` : 'Choose a first and last video in recording order.';
  const keep = document.getElementById('dateMethod').value==='keep';
  document.getElementById('dateAnchorFields').hidden=keep;
  document.getElementById('dateActual').required=!keep;
  document.getElementById('dateReuseOffset').disabled=keep;
  if (keep) document.getElementById('dateReuseOffset').checked=false;
  invalidateDatePreview();
}
async function checkRecordingDates() {
  dateReviewState.busy=true;
  invalidateDatePreview();
  try {
    const result=await api.post('/api/recording-dates/check',{camera_root:document.getElementById('workflowCamera').value});
    toast(`Date check queued · job ${result.job_id}`);
    await loadRecordingDates();
  } catch(e) { workflowError(e.message); }
  finally { dateReviewState.busy=false; }
}
async function previewRecordingDates() {
  if (dateReviewState.busy || !dateReviewState.selected.length) return;
  dateReviewState.busy=true;
  document.getElementById('dateReviewForm').inert=true;
  try {
    const selected=dateReviewState.selected;
    const lastEnd=document.getElementById('dateAnchor').value==='last_end';
    const plan=await api.post('/api/recording-dates/preview',{
      camera_root:document.getElementById('workflowCamera').value,
      recording_ids:selected.map(f=>f.id),
      anchor_id:(lastEnd?selected.at(-1):selected[0]).id,
      anchor_end:lastEnd, anchor_time:document.getElementById('dateActual').value,
      keep_recorded_dates:document.getElementById('dateMethod').value==='keep',
      destination_id:Number(document.getElementById('workflowDestination').value),
      metadata_copy:document.getElementById('dateMetadataCopy').checked,
      reuse_offset:document.getElementById('dateReuseOffset').checked,
    });
    dateReviewState.plan=plan;
    const moves=plan.entries.flatMap(e=>e.moves);
    const candidates=plan.entries.flatMap(e=>e.candidate_moves||[]);
    const rows=plan.entries.length>16 ? [...plan.entries.slice(0,15),plan.entries.at(-1)] : plan.entries;
    document.getElementById('datePreview').innerHTML=`<div class="date-preview"><h3>Review before confirming</h3><p>${plan.entries.length} recording dates · ${moves.length} indexed NAS copies to check${candidates.length?` · ${candidates.length} possible older copies to compare by full SHA-256`:''}</p><div class="table-scroll"><table><thead><tr><th>Video</th><th>Current UK time</th><th>Corrected UK time</th></tr></thead><tbody>${rows.map(e=>`<tr><td>${esc(e.path)}</td><td>${esc(dateLabel(e.old_time))}</td><td>${esc(dateLabel(e.new_time))}</td></tr>`).join('')}</tbody></table></div>${plan.entries.length>16?'<p class="hint">Showing the first 15 videos and the last video; the whole selected range will be corrected.</p>':''}<p class="hint">${plan.metadata_copy?'Original backups remain unchanged in content. Metadata-corrected videos are saved separately under Corrected/YYYY/MM.':'Original backups keep their embedded metadata; correction records are saved beside them.'} ${plan.reuse_offset?'This offset may be reused for later recordings only when all checks pass.':''}</p><details ${moves.length?'open':''}><summary>NAS folders to check and move (${moves.length+candidates.length})</summary>${[...moves,...candidates].map(m=>`<div class="date-move"><code>${esc(m.old)}</code><span>→</span><code>${esc(m.new)}</code></div>`).join('')||'<p>Selected recordings have no existing NAS originals in the checked locations. After confirmation they can upload to the corrected date folders.</p>'}</details><label class="check-row"><input id="dateAcknowledged" type="checkbox" onchange="document.getElementById('dateConfirmButton').disabled=!this.checked"><span>I have checked these times and approve the corrections and matching NAS file moves.</span></label></div>`;
    document.getElementById('dateConfirmButton').hidden=false;
    document.getElementById('dateConfirmButton').disabled=true;
  } catch(e) { workflowError(e.message); }
  finally { dateReviewState.busy=false; document.getElementById('dateReviewForm').inert=false; }
}
async function confirmRecordingDates() {
  const plan=dateReviewState.plan;
  if (!plan || dateReviewState.busy || !document.getElementById('dateAcknowledged')?.checked) return;
  dateReviewState.busy=true;
  document.getElementById('dateConfirmButton').disabled=true;
  try {
    const result=await api.post('/api/recording-dates/confirm',{plan_id:plan.plan_id});
    toast(`Correction queued · job ${result.job_id}`,5000);
    invalidateDatePreview();
    await loadRecordingDates();
  } catch(e) { workflowError(e.message); }
  finally { dateReviewState.busy=false; }
}
async function syncCameraClock() {
  const resultEl=document.getElementById('cameraClockResult');
  resultEl.textContent='Contacting camera…';
  try {
    const result=await api.post('/api/camera-clock/sync',{camera_ip:document.getElementById('cameraClockIp').value});
    resultEl.textContent=`${result.message} Sent ${dateLabel(result.sent_time)} UK time.`;
  } catch(e) { resultEl.textContent=e.message; }
}
