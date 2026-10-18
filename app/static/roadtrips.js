// Trip membership and GPS associations never move or rewrite the recordings.
(() => {
  const rt = {state:null, candidates:null, detail:null, selection:null, jobs:new Set(), poll:null, busy:false,
    map:null, route:null, marker:null, clipLayers:[], pins:[], tiles:null, selectedClip:null,
    files:null, filesComplete:true, filtered:[], shown:300, selected:new Set(), editingId:null, collection:null, dateFilter:"all"};
  const $ = id => document.getElementById(id);
  const zone = () => rt.state?.settings?.timezone || "Europe/London";
  const stamp = value => value ? Date.parse(value) : NaN;
  const when = value => Number.isFinite(stamp(value)) ? new Date(value).toLocaleString("en-GB", {timeZone:zone(),dateStyle:"medium",timeStyle:"short"}) : "Date not confirmed";
  const day = value => Number.isFinite(stamp(value)) ? new Date(value).toLocaleDateString("en-GB", {timeZone:zone(),weekday:"short",day:"numeric",month:"short",year:"numeric"}) : "Date not confirmed";
  const dateKey = value => Number.isFinite(stamp(value)) ? new Intl.DateTimeFormat("sv-SE",{timeZone:zone(),year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date(value)) : "unconfirmed";
  const clock = value => Number.isFinite(stamp(value)) ? new Date(value).toLocaleTimeString("en-GB", {timeZone:zone(),hour:"2-digit",minute:"2-digit"}) : "";
  const duration = value => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Math.round(Number(value)/60) + " min" : "Duration unknown";
  const speed = value => value === null || value === undefined || !Number.isFinite(Number(value)) ? "Speed unavailable" :
    (Number(value)/1.609344).toFixed(1) + " mph · " + Number(value).toFixed(1) + " km/h";
  const bytes = value => { if(value===null || value===undefined || !Number.isFinite(Number(value))) return "Size unknown";
    const n=Number(value),units=["B","KiB","MiB","GiB","TiB"],i=Math.min(4,Math.floor(Math.log(Math.max(1,n))/Math.log(1024)));return (n/1024**i).toFixed(i>1?1:0)+" "+units[i]; };
  const coordinate = point => point && Number.isFinite(Number(point.latitude)) && Number.isFinite(Number(point.longitude)) &&
    point.latitude!==null && point.longitude!==null ? Number(point.latitude).toFixed(4)+", "+Number(point.longitude).toFixed(4) : "Location not confirmed";
  function error(message) { $("rtError").textContent=message || ""; $("rtError").hidden=!message; }
  async function request(url,body,multipart=false,method) {
    const response=await fetch("/api/roadtrips"+url,{method:method || (body===undefined?"GET":"POST"),
      headers:body===undefined || multipart?{}:{"Content-Type":"application/json"},
      body:body===undefined?undefined:multipart?body:JSON.stringify(body)});
    const data=await response.json().catch(()=>({}));
    if(!response.ok) throw new Error(typeof data.detail==="string"?data.detail:"Request failed ("+response.status+")");
    return data;
  }
  async function action(fn) {
    if(rt.busy) return;rt.busy=true;error("");buttons();
    try {await fn();} catch(e) {error(e.message);} finally {rt.busy=false;buttons();}
  }
  function buttons() {
    ["rtImportHA","rtImportFile","rtSaveSettings","rtFind","rtProbe","rtPreview","rtConfirm","rtReject",
      "rtNewCollection","rtSaveCollection","rtLoadFiles","rtSelectAll","rtAddPreview","rtLocateCollection","rtEditOpenCollection"].forEach(id=>{
      if($(id)) $(id).disabled=rt.busy || (rt.jobs.size>0 && ["rtImportHA","rtImportFile","rtProbe"].includes(id));
    });
    if($("rtLocateCollection")) $("rtLocateCollection").disabled=rt.busy || !$("rtCollectionMonth").value;
  }
  function localInput(value) {
    if(!Number.isFinite(stamp(value))) return "";
    return new Intl.DateTimeFormat("sv-SE",{timeZone:zone(),year:"numeric",month:"2-digit",day:"2-digit",
      hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(new Date(value)).replace(" ","T");
  }
  async function refresh(discover=false) {
    const [state,collections]=await Promise.all([request("/state"),request("/collections")]);rt.state=state;
    const prefs=state.settings || {};
    if(discover) {
      $("rtTimezone").value=prefs.timezone || "Europe/London";$("rtRadius").value=prefs.home_radius_m || 150;
      $("rtGap").value=(prefs.max_gap_s || 900)/60;
      let entities=[];
      if(state.configured_ha) {
        try {entities=(await request("/entities")).entities || [];} catch(e) {error("Location tracker discovery failed: "+e.message);}
      }
      $("rtEntity").innerHTML='<option value="">Choose a tracker</option>'+entities.map(e=>'<option value="'+esc(e.entity_id)+'">'+esc(e.name || e.entity_id)+" · "+esc(e.entity_id)+"</option>").join("");
      $("rtEntity").value=prefs.entity_id || entities.find(e=>e.entity_id==="device_tracker.eds_phone_2")?.entity_id || entities[0]?.entity_id || "";
      const months=(state.available_months || []).map(m=>typeof m==="string"?m:m.month).filter(Boolean).sort();
      const month=months.at(-1) || (state.imports || []).map(x=>x.month).sort().at(-1) || new Date().toISOString().slice(0,7);
      ["rtMonth","rtImportFrom","rtImportTo","rtFilesFrom","rtFilesTo"].forEach(id=>$(id).value=month);
    }
    for(const job of state.active_jobs || (state.active_job?[state.active_job]:[])) {
      if(job.id && ["queued","running","paused"].includes(job.status)) rt.jobs.add(String(job.id));
    }
    $("rtCoverage").textContent=(state.imports || []).length?(state.imports || []).map(x=>x.month+": "+Number(x.point_count || 0).toLocaleString()+" GPS points · "+(x.outing_count || 0)+" outings").join("  |  "):"No location history imported yet. Import a month with footage to begin.";
    $("rtStatus").textContent=rt.jobs.size?"Location import in progress. Follow or cancel it in Jobs.":
      "Group videos into a trip at any time. Only confirmed GPS matches give saved trips a location on the map.";
    const saved=(state.matches || []).filter(x=>x.status==="confirmed");
    $("rtSaved").innerHTML=saved.length?saved.map(x=>'<button type="button" class="ghost" data-rt-match="'+esc(x.id)+'"><strong>'+esc(x.name || x.month+" journey")+'</strong><small>'+esc(when(x.start_time || x.created_at))+"</small></button>").join(""):'<p class="hint">Confirm a proposed date and route to save it here.</p>';
    const trips=Array.isArray(collections)?collections:collections.collections || [];
    $("rtCollections").innerHTML=trips.length?trips.map(c=>'<article class="roadtrip-collection'+(String(rt.collection?.id)===String(c.id)?" rt-active-collection":"")+'"><button type="button" class="roadtrip-collection-open" data-rt-collection="'+esc(c.id)+'"><strong>'+esc(c.name)+"</strong><span>"+esc(c.clip_count || 0)+" videos · "+esc(bytes(c.size_bytes ?? c.total_bytes))+"</span><small>"+
      esc(c.start_time?when(c.start_time)+" → "+when(c.end_time):(c.months || []).join(" · ") || "Dates not confirmed")+'</small></button><button type="button" class="ghost" data-rt-edit="'+esc(c.id)+'" aria-label="Edit '+esc(c.name)+'">Edit videos</button></article>').join(""):
      '<div class="roadtrip-empty"><strong>No saved trips yet</strong><p>Use New trip to put videos from any dates into one named trip. You can match their locations afterwards.</p></div>';
    if(rt.jobs.size) schedule();buttons();
  }
  function schedule() {
    if(rt.poll) return;
    rt.poll=setTimeout(async()=>{
      rt.poll=null;if(document.hidden){schedule();return;}
      try {
        const results=await Promise.all([...rt.jobs].map(async id=>{const res=await fetch("/api/jobs/"+encodeURIComponent(id));if(!res.ok) throw new Error("Unable to check location import");return [id,await res.json()];}));
        let completed=false;
        for(const [id,payload] of results) {const job=payload.job || payload;if(!["queued","running","paused"].includes(job.status)){rt.jobs.delete(id);completed=true;if(job.status!=="done") error(job.error || "Location import "+job.status);}}
        await refresh();if(completed && !rt.jobs.size) await findCandidates();
      } catch(e) {error(e.message);}if(rt.jobs.size) schedule();
    },8000);
  }
  async function queue(url,body,multipart=false) {const result=await request(url,body,multipart);if(result.job_id) rt.jobs.add(String(result.job_id));await refresh();schedule();}
  async function saveSettings() {
    const old=rt.state?.settings || {};
    await request("/settings",{entity_id:$("rtEntity").value,timezone:$("rtTimezone").value,home_radius_m:Number($("rtRadius").value),max_gap_s:Number($("rtGap").value)*60,max_accuracy_m:old.max_accuracy_m || 100,max_speed_kmh:old.max_speed_kmh || 180});await refresh();
  }
  async function loadFiles(force=false,allArchive=false) {
    if(!rt.files || force || (allArchive && !rt.filesComplete)) {
      $("rtFilesStatus").textContent="Loading archive metadata…";
      let payload=await request("/files?limit=10000"),files=payload.clips || [];rt.filesComplete=!payload.has_more;
      if(payload.has_more) {
        if(allArchive) throw new Error("A trip can contain at most 10,000 videos. Select a month range to make a smaller trip.");
        const from=$("rtFilesFrom").value,to=$("rtFilesTo").value || from;
        if(!from || to<from) throw new Error("This archive exceeds 10,000 videos. Choose a month range and click Show videos.");
        const months=[],cursor=new Date(from+"-01T12:00:00Z"),end=new Date(to+"-01T12:00:00Z");
        while(cursor<=end && months.length<=120){months.push(cursor.toISOString().slice(0,7));cursor.setUTCMonth(cursor.getUTCMonth()+1);}
        if(months.length>120) throw new Error("Choose a month range within ten years");
        files=[];
        for(const month of months) {payload=await request("/files?month="+encodeURIComponent(month)+"&limit=10000");if(payload.has_more || files.length+(payload.clips || []).length>10000) throw new Error("This range exceeds 10,000 videos. Choose fewer months for one trip.");files.push(...(payload.clips || []));}
      }
      rt.files=files;
      const aliases=new Map();rt.files.forEach(c=>(c.aliases || [c.id]).forEach(id=>aliases.set(String(id),String(c.id))));
      rt.selected=new Set([...rt.selected].map(id=>aliases.get(id) || id));
    }
    rt.shown=300;renderPicker();
  }
  function filteredFiles() {
    const from=$("rtFilesFrom").value,to=$("rtFilesTo").value,search=$("rtFileSearch").value.trim().toLowerCase();
    return (rt.files || []).filter(c=>(!from || c.month>=from) && (!to || c.month<=to) && (!search || ((c.filename || "")+" "+(c.path || "")).toLowerCase().includes(search)));
  }
  function renderPicker() {
    const all=filteredFiles().sort((a,b)=>(b.month || "").localeCompare(a.month || "") || (a.alignment?.start_time || "z").localeCompare(b.alignment?.start_time || "z") || (a.filename || "").localeCompare(b.filename || ""));
    rt.filtered=all;const visible=all.slice(0,rt.shown);let previous="";
    $("rtFilesStatus").textContent=all.length.toLocaleString()+" videos match these filters · "+(rt.files || []).length.toLocaleString()+(rt.filesComplete?" in archive":" loaded for this month range")+". Archive months are reliable; camera dates may be wrong.";
    $("rtFilePicker").innerHTML=visible.length?visible.map(c=>{
      const alignment=c.alignment,group=alignment?.start_time?day(alignment.start_time):((c.month || "Archive month unknown")+" · recording day unconfirmed");
      const heading=group===previous?"":'<div class="roadtrip-file-date">'+esc(group)+"</div>";previous=group;
      return heading+'<label class="roadtrip-file-choice"><input type="checkbox" data-rt-file="'+esc(c.id)+'"'+(rt.selected.has(String(c.id))?" checked":"")+'><span><strong>'+esc(c.filename || c.path)+'</strong><small>'+esc(c.path || "")+'</small><small>'+esc(duration(c.duration_s))+" · "+esc(bytes(c.size_bytes))+" · "+esc(alignment?.start_time?"Confirmed: "+when(alignment.start_time):c.observed_time?"Unverified camera date: "+when(c.observed_time):"Camera date unknown")+'</small></span><span class="roadtrip-file-location'+(alignment?" rt-located":"")+'">'+(c.alignment_conflict?"Review conflicting dates":alignment?"Route matched":"Location not confirmed")+"</span></label>";
    }).join("")+(all.length>visible.length?'<button type="button" class="ghost roadtrip-more-files" data-rt-more="true">Show next '+Math.min(300,all.length-visible.length)+" videos</button>":""):
      '<p class="hint">No videos match these filters. Clear the month range to see the whole archive.</p>';
    $("rtSelectVisible").textContent="Select filtered videos ("+all.length.toLocaleString()+")";updateSelected();
  }
  function updateSelected() {
    const selected=(rt.files || []).filter(c=>rt.selected.has(String(c.id))),known=selected.filter(c=>c.size_bytes!==null && c.size_bytes!==undefined);
    const total=known.reduce((n,c)=>n+Number(c.size_bytes || 0),0);
    $("rtSelectedCount").textContent=rt.selected.size?rt.selected.size.toLocaleString()+" videos selected"+(known.length?" · "+bytes(total)+(known.length<rt.selected.size?" + unknown sizes":""):""):"No videos selected";
  }
  async function editCollection(id=null,added=[]) {
    rt.editingId=id;const data=id?await request("/collections/"+encodeURIComponent(id)):null;
    rt.selected=new Set((data?.clips || added).map(c=>String(c.current_id || c.id)));
    $("rtCollectionName").value=data?.name || "";$("rtBuilderTitle").textContent=id?"Edit trip videos":"Create a trip from your videos";
    $("rtSaveCollection").textContent=id?"Save trip changes":"Save trip";$("rtCollectionNotice").textContent="";
    if(data?.months?.length){$("rtFilesFrom").value=[...data.months].sort()[0];$("rtFilesTo").value=[...data.months].sort().at(-1);}
    else if(added.length){const months=added.map(c=>c.month || rt.detail?.group?.month).filter(Boolean).sort();if(months.length){$("rtFilesFrom").value=months[0];$("rtFilesTo").value=months.at(-1);}}
    $("rtCollectionBuilder").open=true;await loadFiles(true);$("rtCollectionBuilder").scrollIntoView({behavior:"smooth",block:"start"});$("rtCollectionName").focus();
  }
  async function saveCollection() {
    const name=$("rtCollectionName").value.trim();if(!name) throw new Error("Give your trip a name");if(!rt.selected.size) throw new Error("Select at least one video for this trip");
    const byId=new Map((rt.files || []).map(c=>[String(c.id),c]));
    const ordered=[...rt.selected].sort((a,b)=>{const left=byId.get(a) || {},right=byId.get(b) || {};return (left.month || "9999").localeCompare(right.month || "9999") || (left.alignment?.start_time || "").localeCompare(right.alignment?.start_time || "") || (left.filename || a).localeCompare(right.filename || b,undefined,{numeric:true});});
    const result=await request("/collections"+(rt.editingId?"/"+encodeURIComponent(rt.editingId):""),{name,clip_ids:ordered},false,rt.editingId?"PATCH":"POST");
    const id=result.id || result.collection?.id;if(!id) throw new Error("Trip saved, but its identifier was missing. Refresh the page.");
    $("rtCollectionBuilder").open=false;await refresh();await openCollection(id);toast("Trip saved. Original videos stay in place.");
  }
  async function openCollection(id) {
    const status=$("rtStatus"),previous=status.textContent;status.textContent="Loading this trip’s confirmed video locations…";
    try {const detail=await request("/collections/"+encodeURIComponent(id)+"/map");rt.selection=null;rt.collection=detail.collection || {id,name:detail.group?.label};renderDetail(detail);await refresh();$("rtMapTitle").scrollIntoView({behavior:"smooth",block:"start"});}
    finally {if(status.textContent==="Loading this trip’s confirmed video locations…") status.textContent=previous;}
  }
  async function findCandidates(clipIds) {
    const month=$("rtMonth").value;if(!month) throw new Error("Choose the footage month");
    rt.candidates=await request("/candidates?month="+encodeURIComponent(month)+(clipIds?"&clip_ids="+encodeURIComponent(clipIds.join(",")):""));renderCandidates();
    $("rtStatus").textContent=(rt.candidates.outings || []).length?"Choose a proposed journey date to preview video locations. Review it before confirming.":"No outings found for this month. Check imported GPS coverage and home settings.";
    $("rtMatching").open=true;
  }
  function renderCandidates() {
    const data=rt.candidates || {},groups=data.groups || [],outings=new Map((data.outings || []).map(x=>[String(x.id),x])),suggestions=data.suggestions || [];
    $("rtCandidates").innerHTML=groups.length?groups.map(group=>{
      const options=suggestions.filter(x=>x.group_key===group.key).slice(0,5);
      return '<article class="roadtrip-group"><h4>'+esc(group.label || group.key)+'</h4><p class="hint">'+esc(group.clip_count || group.clips?.length || 0)+" videos · "+esc(duration(group.recorded_duration_s))+" recorded</p>"+
        '<details><summary>Choose videos for this journey</summary><p class="hint">Use this if the folder contains several rides.</p>'+(group.clips || []).map(c=>'<label class="roadtrip-subset"><input type="checkbox" data-rt-subset="'+esc(group.key)+'" value="'+esc(c.id)+'" checked><span>'+esc(c.filename || c.path)+" · "+esc(duration(c.duration_s))+"</span></label>").join("")+'<button type="button" class="ghost" data-rt-subset-go="'+esc(group.key)+'">Find dates for selected videos</button></details>'+
        (options.length?'<p class="roadtrip-candidate-label">Possible journey dates</p>'+options.map(option=>{const outing=outings.get(String(option.outing_id)) || option.outing || {},score=Number(option.score || 0),percent=score<=1?Math.round(score*100):Math.round(score);
          return '<button type="button" class="roadtrip-option" data-rt-group="'+esc(group.key)+'" data-rt-outing="'+esc(option.outing_id)+'"><strong>'+esc(day(outing.start_time))+'</strong><span>'+esc(clock(outing.start_time))+"–"+esc(clock(outing.end_time))+" · "+esc(duration(outing.duration_s))+'</span><small>'+esc(option.confidence || "Uncertain")+" confidence · score "+percent+"%"+(option.reasons?.length?" · "+esc(option.reasons.join(" · ")):"")+"</small></button>";
        }).join(""):'<p class="hint">No plausible match in the imported history.</p>')+"</article>";
    }).join(""):'<div class="roadtrip-empty">No archived video groups found for this month. Complete the NAS inventory in Stats first.</div>';
  }
  async function loadDetail(url,body) {
    const status=$("rtStatus"),previous=status.textContent,message="Loading GPS track… First loads can take about a minute.";status.textContent=message;
    try {const detail=await request(url,body);rt.collection=null;renderDetail(detail);return detail;} finally {if(status.textContent===message) status.textContent=previous;}
  }
  async function choose(groupKey,outingId) {
    const group=rt.candidates.groups.find(g=>g.key===groupKey);rt.selection={month:$("rtMonth").value,group_key:groupKey,outing_id:Number(outingId),clip_ids:(group?.clips || []).map(c=>c.id)};
    await loadDetail("/preview",rt.selection);$("rtName").value="";$("rtDetail").scrollIntoView({behavior:"smooth",block:"start"});
  }
  function selection() {
    if(!rt.selection) throw new Error("Choose a possible journey first");
    return {...rt.selection,start_time:$("rtAnchor").value || null,clip_offsets:Object.fromEntries([...document.querySelectorAll("[data-rt-offset]")].filter(input=>input.dataset.rtManual==="true" || Number(input.value)!==Number(input.dataset.rtInitial)).map(input=>[input.dataset.rtOffset,Number(input.value)]))};
  }
  function visibleClips() {return (rt.detail?.clips || []).filter(c=>rt.dateFilter==="all" || (c.location_date || dateKey(c.start_time))===rt.dateFilter);}
  function points() {return (rt.detail?.time_series || []).filter(p=>Number.isFinite(stamp(p.time)) && p.latitude!==null && p.longitude!==null && Number.isFinite(Number(p.latitude)) && Number.isFinite(Number(p.longitude)) && (rt.dateFilter==="all" || dateKey(p.time)===rt.dateFilter)).sort((a,b)=>stamp(a.time)-stamp(b.time));}
  function bounds() {const outing=rt.detail?.outing || {},pts=points();return rt.dateFilter!=="all"?[stamp(pts[0]?.time),stamp(pts.at(-1)?.time)]:[stamp(outing.start_time) || stamp(pts[0]?.time),stamp(outing.end_time) || stamp(pts.at(-1)?.time)];}
  function clipPoint(clip) {return clip.location && coordinate(clip.location)!=="Location not confirmed"?clip.location:null;}
  function renderDateFilters() {
    const counts=new Map();(rt.detail?.clips || []).forEach(c=>{const key=c.location_date || dateKey(c.start_time);counts.set(key,(counts.get(key)||0)+1);});
    $("rtDateFilters").hidden=!counts.size;
    $("rtDateFilters").innerHTML='<button type="button" class="ghost" data-rt-date="all" aria-pressed="'+(rt.dateFilter==="all")+'">All dates ('+(rt.detail?.clips || []).length+')</button>'+[...counts].sort((a,b)=>a[0].localeCompare(b[0])).map(([key,count])=>'<button type="button" class="ghost" data-rt-date="'+esc(key)+'" aria-pressed="'+(rt.dateFilter===key)+'">'+esc(key==="unconfirmed"?"Date not confirmed":day(key+"T12:00:00Z"))+" · "+count+" videos</button>").join("");
  }
  function filterDate(value) {
    rt.dateFilter=value;renderDateFilters();const visible=new Set(visibleClips().map(c=>String(c.id)));
    const outing=rt.detail?.outing || {},isPreview=!rt.detail?.collection && rt.detail?.provisional!==false;
    $("rtMapDate").hidden=value==="unconfirmed" || !Number.isFinite(stamp(outing.start_time));
    $("rtMapDate").textContent=(isPreview?"Proposed: ":"")+(value==="all"?when(outing.start_time)+" → "+when(outing.end_time):day(value+"T12:00:00Z"));
    document.querySelectorAll("[data-rt-clip]").forEach(row=>row.hidden=!visible.has(row.dataset.rtClip));
    drawMap();drawGraph();$("rtTimeline").hidden=!points().length;scrub(0);const first=visibleClips().find(c=>clipPoint(c)) || visibleClips()[0];if(first) selectClip(first.id);
  }
  function renderDetail(detail) {
    rt.detail=detail;rt.selectedClip=null;rt.dateFilter="all";$("rtMapEmpty").hidden=true;const outing=detail.outing || {},group=detail.group || {},collection=detail.collection,isCollection=Boolean(collection),clips=detail.clips || [];
    $("rtDetailTitle").textContent=collection?.name || detail.match?.name || group.label || "Journey preview";
    $("rtMapTitle").textContent=collection?.name || "Video location map";
    const located=clips.filter(c=>clipPoint(c)).length,unconfirmed=clips.filter(c=>!c.start_time).length;
    const dates=Number.isFinite(stamp(outing.start_time))?when(outing.start_time)+" → "+when(outing.end_time):"No confirmed journey dates yet";
    $("rtDetailMeta").textContent=dates+" · "+clips.length+" videos"+(outing.distance_m?" · "+(outing.distance_m/1000).toFixed(1)+" km GPS distance":"");
    $("rtMapSummary").textContent=isCollection?located+" videos with GPS locations · "+unconfirmed+" with dates awaiting confirmation":"Review the proposed journey date. Numbered pins and video bands follow this alignment.";
    $("rtMatchBadge").textContent=isCollection?"Saved trip":detail.provisional===false?"Confirmed route":"Suggested dates and locations";
    $("rtMatchBadge").classList.toggle("rt-suggested",!isCollection && detail.provisional!==false);
    $("rtMapDate").hidden=!Number.isFinite(stamp(outing.start_time));$("rtMapDate").textContent=(isCollection || detail.provisional===false?"":"Proposed: ")+dates;
    $("rtMapLegend").hidden=false;$("rtPinCoverage").textContent=located+" of "+clips.length+" videos placed";
    $("rtTimeline").hidden=!points().length;$("rtAlignment").hidden=isCollection;$("rtClipTable").hidden=false;
    $("rtCollectionMatchControls").hidden=!isCollection;
    if(isCollection) {const months=[...new Set(clips.map(c=>c.month).filter(Boolean))].sort();$("rtCollectionMonth").innerHTML=months.map(m=>'<option value="'+esc(m)+'">'+esc(m)+' · '+clips.filter(c=>c.month===m).length+' videos</option>').join("");$("rtLocateCollection").disabled=!months.length;}
    $("rtAnchor").value=localInput(detail.start_time);$("rtQuality").textContent=detail.warning || group.timing_warning || "Missing GPS stays a gap. Video speeds are GPS estimates.";
    $("rtUnmatchedNotice").textContent=unconfirmed?unconfirmed+" videos have no confirmed date or location. They remain in this trip; use Find dates and locations to match their archive month.":"Select a numbered pin, timeline band or video row to see its place and date.";
    $("rtClips").innerHTML=clips.map((clip,index)=>{
      const point=clipPoint(clip),hasTime=Number.isFinite(stamp(clip.start_time)),label=hasTime?(isCollection || detail.provisional===false?"Confirmed alignment":"Proposed alignment"):clip.alignment_conflict?"Review conflicting dates":"Date not confirmed";
      return '<tr data-rt-clip="'+esc(clip.id)+'" tabindex="0"><td><span class="rt-video-number">'+(index+1)+'</span><strong>'+esc(clip.filename || clip.path)+'</strong><small class="hint">'+esc(clip.path || "")+'</small>'+
        (!isCollection?'<details class="rt-offset-editor"><summary>Adjust offset</summary><label>Seconds after first video<input data-rt-offset="'+esc(clip.id)+'" type="number" step="1" min="0" value="'+esc(clip.offset_s || 0)+'" data-rt-initial="'+esc(clip.offset_s || 0)+'" data-rt-manual="'+(clip.timing_basis==="manual")+'" aria-label="Offset for '+esc(clip.filename || clip.id)+'"></label></details>':"")+"</td><td>"+(hasTime?'<strong>'+esc(day(clip.start_time))+'</strong><small>'+esc(clock(clip.start_time))+"–"+esc(clock(clip.end_time))+" · "+esc(label)+"</small>":'<strong>'+esc(clip.alignment_conflict?'Review conflicting dates':'Date not confirmed')+'</strong><small>Archive: '+esc(clip.month || group.month || "month unknown")+'</small><small>'+esc(clip.observed_time?"Camera: "+when(clip.observed_time)+" (unverified)":"Camera date unknown")+"</small>")+"</td><td>"+esc(coordinate(point))+(point?'<small>GPS estimate · pin '+(index+1)+"</small>":"<small>No pin placed</small>")+"</td><td>"+(hasTime?Math.round((clip.coverage || 0)*100)+"% covered<small>"+esc(speed(clip.average_speed_kmh))+"</small>":"Awaiting GPS match")+"</td></tr>";
    }).join("");
    renderDateFilters();drawMap();drawGraph();scrub(0);
    if(clips.length) selectClip(clips.find(c=>clipPoint(c))?.id || clips[0].id);
  }
  function ensureMap() {
    if(!window.L) {$("rtMap").textContent="Map library unavailable. The dated video table still works.";return false;}
    if(!rt.map) rt.map=L.map("rtMap",{scrollWheelZoom:false}).setView([54,-2],5);return true;
  }
  function drawMap() {
    if(!ensureMap()) return;
    if(rt.route) rt.route.remove();if(rt.marker){rt.marker.remove();rt.marker=null;}
    rt.clipLayers.forEach(x=>x.remove());rt.clipLayers=[];rt.pins.forEach(x=>x.remove());rt.pins=[];
    const routeData=rt.dateFilter==="all"?rt.detail.geojson:{type:"FeatureCollection",features:(rt.detail.clip_geojson?.features || []).filter(f=>f.properties?.date===rt.dateFilter)};
    rt.route=L.geoJSON(routeData || {type:"FeatureCollection",features:[]},{style:{color:"#73daa2",weight:4,opacity:.7}}).addTo(rt.map);
    const visible=visibleClips(),located=visible.filter(clipPoint),markerLimit=200;
    $("rtMapEmpty").hidden=Boolean(located.length || routeData?.features?.length);
    if(!$("rtMapEmpty").hidden) $("rtMapEmpty").innerHTML='<strong>No video locations confirmed</strong><span>'+visible.length+' videos remain in this trip. Find and confirm their dates and GPS route to place them here.</span><small>No location is guessed from a camera timestamp alone.</small>';
    $("rtPinCoverage").textContent=located.length+" of "+visible.length+" videos placed"+(located.length>markerLimit?" · first 200 pins shown; choose a date or video to focus":"");
    visible.forEach(clip=>{
      const index=(rt.detail.clips || []).indexOf(clip),point=clipPoint(clip);if(!point || rt.pins.length>=markerLimit) return;
      const pin=L.marker([point.latitude,point.longitude],{icon:L.divIcon({className:"rt-map-pin",html:'<span>'+(index+1)+"</span>",iconSize:[30,36],iconAnchor:[15,33]}),title:(index+1)+". "+(clip.filename || "Video")+" · "+when(clip.start_time),keyboard:true}).addTo(rt.map);
      pin.bindTooltip('<strong>'+esc(clip.filename || "Video")+'</strong><br>'+esc(when(clip.start_time))+'<br>'+esc(coordinate(point)),{direction:"top",offset:[0,-28]});
      pin.rtClipId=String(clip.id);pin.on("click",()=>{selectClip(clip.id);$("rtSelectedClip").scrollIntoView({behavior:"smooth",block:"nearest"});});rt.pins.push(pin);
    });
    const pts=points();
    if(pts.length) {
      for(const [point,label] of [[pts[0],"Start"],[pts.at(-1),"Finish"]]) {
        const pin=L.circleMarker([point.latitude,point.longitude],{radius:5,color:"#fff",weight:2,fillColor:label==="Start"?"#80e4a0":"#fbbf24",fillOpacity:1}).addTo(rt.map);
        pin.bindTooltip(label+" · "+esc(when(point.time)),{direction:"right"});rt.pins.push(pin);
      }
    }
    fitMap();setTimeout(()=>rt.map.invalidateSize(),0);
  }
  function fitMap() {
    if(!rt.map) return;
    if(rt.route?.getBounds().isValid()) rt.map.fitBounds(rt.route.getBounds(),{padding:[42,42],maxZoom:15});
    else {const pins=visibleClips().map(clipPoint).filter(Boolean);if(pins.length) rt.map.fitBounds(pins.map(p=>[p.latitude,p.longitude]),{padding:[42,42],maxZoom:15});}
  }
  function drawGraph() {
    const pts=points(),[start,end]=bounds();if(!pts.length || !(end>start)){$("rtGraph").innerHTML='<p class="hint">No continuous GPS timeline is available yet.</p>';return;}
    const width=800,height=205,left=48,right=14,top=42,bottom=34,graphW=width-left-right,graphH=height-top-bottom;
    const max=pts.reduce((n,p)=>Math.max(n,(Number(p.speed_kmh)||0)/1.609344),10),x=t=>left+Math.max(0,Math.min(1,(t-start)/(end-start)))*graphW,y=v=>top+graphH*(1-v/max);
    let path="",lastSegment=null;const stride=Math.max(1,Math.floor(pts.length/1000));
    pts.forEach((p,i)=>{if(i%stride!==0 && i!==pts.length-1) return;if(p.speed_kmh===null || p.speed_kmh===undefined){lastSegment=null;return;}const segment=p.segment===undefined?0:p.segment;path+=(lastSegment===segment?" L":" M")+x(stamp(p.time)).toFixed(1)+","+y(Number(p.speed_kmh)/1.609344).toFixed(1);lastSegment=segment;});
    const bands=visibleClips().map(c=>{const i=(rt.detail.clips || []).indexOf(c);if(!Number.isFinite(stamp(c.start_time)) || !Number.isFinite(stamp(c.end_time))) return "";const a=x(stamp(c.start_time)),b=x(stamp(c.end_time));
      return '<g class="rt-clip-interval" data-rt-band="'+esc(c.id)+'" tabindex="0" role="button" aria-label="Show video '+(i+1)+": "+esc(c.filename || "")+'"><rect class="rt-clip-band" x="'+a+'" y="25" width="'+Math.max(2,b-a)+'" height="'+(graphH+17)+'"/><title>'+esc((i+1)+". "+(c.filename || "Video")+" · "+when(c.start_time))+'</title><text class="rt-band-number" x="'+(a+3)+'" y="37">'+(i+1)+"</text></g>";
    }).join("");
    $("rtGraph").innerHTML='<svg viewBox="0 0 '+width+" "+height+'" role="group" aria-label="Video intervals and GPS speed from '+esc(when(new Date(start).toISOString()))+" to "+esc(when(new Date(end).toISOString()))+'">'+bands+'<path class="rt-speed" d="'+path+'"/><line id="rtGraphCursor" x1="'+left+'" x2="'+left+'" y1="25" y2="'+(top+graphH)+'" stroke="#fbbf24"/>'+'<text x="2" y="'+(top+9)+'">'+Math.round(max)+' mph</text><text x="20" y="'+(top+graphH)+'">0</text><text x="'+left+'" y="'+(height-8)+'">'+esc(when(new Date(start).toISOString()))+'</text><text text-anchor="end" x="'+(width-right)+'" y="'+(height-8)+'">'+esc(when(new Date(end).toISOString()))+"</text></svg>";
  }
  function scrub(fraction) {
    if(!rt.detail) return;const [start,end]=bounds();if(!Number.isFinite(start) || !Number.isFinite(end)){$("rtCursorTime").textContent="No confirmed GPS time";return;}
    const time=start+(end-start)*Math.max(0,Math.min(1,fraction)),pts=points(),nearest=pts.reduce((best,p)=>!best || Math.abs(stamp(p.time)-time)<Math.abs(stamp(best.time)-time)?p:best,null);
    $("rtCursorTime").textContent=when(new Date(time).toISOString());
    const maxGap=(rt.state?.settings?.max_gap_s || 900)*1000;
    if(nearest && Math.abs(stamp(nearest.time)-time)<=maxGap/2 && rt.map) {
      if(rt.marker) rt.marker.setLatLng([nearest.latitude,nearest.longitude]);else rt.marker=L.circleMarker([nearest.latitude,nearest.longitude],{radius:7,color:"#fff",fillColor:"#fbbf24",fillOpacity:1}).addTo(rt.map);
      $("rtCursorTime").textContent+=" · "+coordinate(nearest);
    } else {if(rt.marker){rt.marker.remove();rt.marker=null;}$("rtCursorTime").textContent+=" · GPS gap";}
    const cursor=$("rtGraphCursor");if(cursor){const x=48+Math.max(0,Math.min(1,fraction))*738;cursor.setAttribute("x1",x);cursor.setAttribute("x2",x);}
  }
  function selectClip(id) {
    const clips=rt.detail?.clips || [],index=clips.findIndex(c=>String(c.id)===String(id)),clip=clips[index];if(!clip) return;rt.selectedClip=id;
    const point=clipPoint(clip),hasTime=Number.isFinite(stamp(clip.start_time)),[start,end]=bounds(),f=(stamp(clip.start_time)-start)/(end-start);
    if(hasTime && Number.isFinite(f)){$("rtScrubber").value=Math.max(0,Math.min(100,f*100));scrub(f);}
    $("rtSelectedClip").hidden=false;$("rtSelectedClip").innerHTML='<span class="rt-video-number">'+(index+1)+'</span><div><strong>'+esc(clip.filename || clip.path)+'</strong><span>'+esc(hasTime?when(clip.start_time)+" → "+clock(clip.end_time):"Recording date not confirmed")+'</span><small>'+esc(point?(Math.abs(stamp(point.time)-stamp(clip.start_time))>2000?"First covered position at "+clock(point.time)+": ":"Video starts near ")+coordinate(point)+" · GPS estimate":"Location not confirmed — no marker placed")+'</small></div><div class="rt-selected-speed">'+esc(speed(clip.average_speed_kmh))+'<small>'+esc(duration(clip.duration_s))+(hasTime?" · "+Math.round((clip.coverage || 0)*100)+"% GPS coverage":"")+"</small></div>";
    document.querySelectorAll("[data-rt-clip]").forEach(row=>row.classList.toggle("rt-selected",row.dataset.rtClip===String(id)));
    document.querySelectorAll("[data-rt-band]").forEach(band=>band.classList.toggle("rt-selected",band.dataset.rtBand===String(id)));
    rt.pins.forEach(pin=>{if(pin.rtClipId){const selected=pin.rtClipId===String(id);pin.setZIndexOffset(selected?1000:0);pin.getElement()?.classList.toggle("rt-pin-selected",selected);}});
    rt.clipLayers.forEach(x=>x.remove());rt.clipLayers=[];
    if(rt.map) {
      if(point && !rt.pins.some(pin=>pin.rtClipId===String(id))) {
        rt.clipLayers.push(L.marker([point.latitude,point.longitude],{icon:L.divIcon({className:"rt-map-pin rt-pin-selected",html:'<span>'+(index+1)+"</span>",iconSize:[30,36],iconAnchor:[15,33]}),zIndexOffset:2000,title:(clip.filename || "Video")+" · "+when(clip.start_time)}).addTo(rt.map));
      }
      const features=(rt.detail.clip_geojson?.features || []).filter(f=>String(f.properties?.clip_id)===String(id));
      if(features.length) rt.clipLayers.push(L.geoJSON({type:"FeatureCollection",features},{style:{color:"#fbbf24",weight:7}}).addTo(rt.map));
      if(point) rt.map.panTo([point.latitude,point.longitude]);
    }
  }
  window.initRoadtrips=async function() {
    if(!$("roadtripTitle")) return;ensureMap();
    $("rtRefresh").addEventListener("click",()=>action(async()=>{await refresh();if(rt.collection) await openCollection(rt.collection.id);}));
    $("rtNewCollection").addEventListener("click",()=>action(()=>editCollection()));
    $("rtLoadFiles").addEventListener("click",()=>action(()=>loadFiles(true)));
    ["rtFilesFrom","rtFilesTo","rtFileSearch"].forEach(id=>$(id).addEventListener("input",()=>{rt.shown=300;if(rt.files) renderPicker();}));
    $("rtSelectVisible").addEventListener("click",()=>{rt.filtered.forEach(c=>rt.selected.add(String(c.id)));renderPicker();});
    $("rtSelectAll").addEventListener("click",()=>action(async()=>{await loadFiles(false,true);rt.files.forEach(c=>rt.selected.add(String(c.id)));renderPicker();}));
    $("rtClearSelection").addEventListener("click",()=>{rt.selected.clear();renderPicker();});
    $("rtCancelCollection").addEventListener("click",()=>$("rtCollectionBuilder").open=false);
    $("rtSaveCollection").addEventListener("click",()=>action(saveCollection));
    $("rtFilePicker").addEventListener("change",e=>{const input=e.target.closest("[data-rt-file]");if(!input) return;if(input.checked) rt.selected.add(input.dataset.rtFile);else rt.selected.delete(input.dataset.rtFile);updateSelected();});
    $("rtFilePicker").addEventListener("click",e=>{if(e.target.closest("[data-rt-more]")){rt.shown+=300;renderPicker();}});
    $("rtCollections").addEventListener("click",e=>{const edit=e.target.closest("[data-rt-edit]"),open=e.target.closest("[data-rt-collection]");if(edit) action(()=>editCollection(edit.dataset.rtEdit));else if(open) action(()=>openCollection(open.dataset.rtCollection));});
    $("rtAddPreview").addEventListener("click",()=>action(()=>editCollection(null,rt.detail?.clips || [])));
    $("rtEditOpenCollection").addEventListener("click",()=>action(()=>editCollection(rt.collection?.id)));
    $("rtLocateCollection").addEventListener("click",()=>action(async()=>{const month=$("rtCollectionMonth").value,ids=(rt.detail?.clips || []).filter(c=>c.month===month).map(c=>c.current_id || c.id);if(!ids.length) throw new Error("These videos need a reliable archive month before GPS matching");if(ids.length>2000) throw new Error("Choose a smaller video group of at most 2,000 clips for one GPS alignment");$("rtMonth").value=month;await findCandidates(ids);$("rtMatching").scrollIntoView({behavior:"smooth",block:"start"});}));
    $("rtSaveSettings").addEventListener("click",()=>action(saveSettings));$("rtFind").addEventListener("click",()=>action(()=>findCandidates()));
    $("rtProbe").addEventListener("click",()=>action(()=>queue("/probe",{month:$("rtMonth").value})));
    $("rtImportHA").addEventListener("click",()=>action(async()=>{await saveSettings();const from=$("rtImportFrom").value,to=$("rtImportTo").value || from;if(!from || to<from) throw new Error("Choose a valid month range");const a=from.split("-").map(Number),b=to.split("-").map(Number);if((b[0]-a[0])*12+b[1]-a[1]>=12) throw new Error("Import at most twelve months at a time");await queue("/import-ha",{month:from,end_month:to,entity_id:$("rtEntity").value});}));
    $("rtImportFile").addEventListener("change",()=>action(async()=>{const file=$("rtImportFile").files[0];if(!file) return;const form=new FormData();form.append("month",$("rtMonth").value);form.append("file",file);await queue("/import-file",form,true);$("rtImportFile").value="";}));
    $("rtPreview").addEventListener("click",()=>action(()=>loadDetail("/preview",selection())));
    $("rtConfirm").addEventListener("click",()=>action(async()=>{const data=await request("/matches",{...selection(),name:$("rtName").value,status:"confirmed"});await refresh();if(data.id || data.match?.id) renderDetail(await request("/matches/"+(data.id || data.match.id)));toast("Journey date and route confirmed. Original video dates are unchanged.");}));
    $("rtReject").addEventListener("click",()=>action(async()=>{await request("/matches",{...selection(),name:$("rtName").value,status:"rejected"});await refresh();await findCandidates();$("rtAlignment").hidden=true;$("rtMatchBadge").textContent="Rejected suggestion";}));
    $("rtCandidates").addEventListener("click",e=>{const subset=e.target.closest("[data-rt-subset-go]");if(subset){action(async()=>{const ids=[...document.querySelectorAll("[data-rt-subset]")].filter(c=>c.dataset.rtSubset===subset.dataset.rtSubsetGo && c.checked).map(c=>c.value);if(!ids.length) throw new Error("Choose at least one video");await findCandidates(ids);});return;}const b=e.target.closest("[data-rt-group]");if(b) action(()=>choose(b.dataset.rtGroup,b.dataset.rtOuting));});
    $("rtSaved").addEventListener("click",e=>{const b=e.target.closest("[data-rt-match]");if(b) action(async()=>{const detail=await loadDetail("/matches/"+encodeURIComponent(b.dataset.rtMatch)),match=detail.match || {};rt.selection={month:match.month || detail.group.month,group_key:match.group_key || detail.group.key,outing_id:match.outing_id || detail.outing.id,clip_ids:(detail.group.clips || []).map(c=>c.id)};$("rtName").value=match.name || "";});});
    $("rtClips").addEventListener("click",e=>{if(e.target.closest("input,summary")) return;const row=e.target.closest("[data-rt-clip]");if(row) selectClip(row.dataset.rtClip);});
    $("rtClips").addEventListener("keydown",e=>{if(e.key==="Enter" && !e.target.closest("input,summary")){const row=e.target.closest("[data-rt-clip]");if(row) selectClip(row.dataset.rtClip);}});
    $("rtDateFilters").addEventListener("click",e=>{const b=e.target.closest("[data-rt-date]");if(b) filterDate(b.dataset.rtDate);});
    $("rtGraph").addEventListener("click",e=>{const band=e.target.closest("[data-rt-band]");if(band) selectClip(band.dataset.rtBand);});
    $("rtGraph").addEventListener("keydown",e=>{if(e.key==="Enter" || e.key===" "){const band=e.target.closest("[data-rt-band]");if(band){e.preventDefault();selectClip(band.dataset.rtBand);}}});
    $("rtScrubber").addEventListener("input",()=>scrub(Number($("rtScrubber").value)/100));$("rtFitMap").addEventListener("click",fitMap);
    $("rtStreetMap").addEventListener("click",()=>{if(!ensureMap()) return;if(rt.tiles){rt.tiles.remove();rt.tiles=null;$("rtStreetMap").textContent="Show street names and places";}else{rt.tiles=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(rt.map);$("rtStreetMap").textContent="Hide street map";}});
    await action(()=>refresh(true));
  };
})();
