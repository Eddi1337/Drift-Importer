// Private GPS/camera alignment. Suggestions never edit the original recordings.
(() => {
  const rt = {state:null, candidates:null, detail:null, selection:null, jobs:new Set(), poll:null, busy:false,
    map:null, route:null, marker:null, clipLayers:[], tiles:null, selectedClip:null};
  const $ = id => document.getElementById(id);
  const zone = () => rt.state?.settings?.timezone || "Europe/London";
  const stamp = value => Date.parse(value);
  const when = value => value ? new Date(value).toLocaleString("en-GB", {timeZone:zone(), dateStyle:"medium",timeStyle:"short"}) : "Unknown";
  const clock = value => value ? new Date(value).toLocaleTimeString("en-GB", {timeZone:zone(),hour:"2-digit",minute:"2-digit"}) : "";
  const duration = value => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Math.round(Number(value)/60) + " min" : "Unknown";
  const speed = value => value === null || value === undefined || !Number.isFinite(Number(value)) ? "Not enough GPS" :
    (Number(value)/1.609344).toFixed(1) + " mph (" + Number(value).toFixed(1) + " km/h)";
  function error(message) { $("rtError").textContent = message || ""; $("rtError").hidden = !message; }
  async function request(url, body, multipart=false) {
    const response = await fetch("/api/roadtrips"+url, {
      method:body === undefined ? "GET":"POST",
      headers:body === undefined || multipart ? {} : {"Content-Type":"application/json"},
      body:body === undefined ? undefined : multipart ? body : JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed ("+response.status+")");
    return data;
  }
  async function action(fn) {
    if (rt.busy) return;
    rt.busy=true; error(""); buttons();
    try { await fn(); } catch(e) { error(e.message); }
    finally { rt.busy=false; buttons(); }
  }
  function buttons() {
    ["rtImportHA","rtImportFile","rtSaveSettings","rtFind","rtProbe","rtPreview","rtConfirm","rtReject"].forEach(id => {
      if ($(id)) $(id).disabled=rt.busy || (rt.jobs.size>0 && ["rtImportHA","rtImportFile","rtProbe"].includes(id));
    });
  }
  function localInput(value) {
    if (!value) return "";
    return new Intl.DateTimeFormat("sv-SE",{timeZone:zone(),year:"numeric",month:"2-digit",day:"2-digit",
      hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(new Date(value)).replace(" ","T");
  }
  async function refresh(discover=false) {
    const state=await request("/state"); rt.state=state;
    const prefs=state.settings || {};
    if (discover) {
      $("rtTimezone").value=prefs.timezone || "Europe/London";
      $("rtRadius").value=prefs.home_radius_m || 150; $("rtGap").value=(prefs.max_gap_s || 900)/60;
      let entities=[];
      if (state.configured_ha) {
        try { entities=(await request("/entities")).entities || []; }
        catch(e) { error("Location tracker discovery failed: "+e.message); }
      }
      $("rtEntity").innerHTML='<option value="">Choose a tracker</option>'+entities.map(e =>
        '<option value="'+esc(e.entity_id)+'">'+esc(e.name || e.entity_id)+" · "+esc(e.entity_id)+"</option>").join("");
      $("rtEntity").value=prefs.entity_id || (entities.find(e=>e.entity_id==="device_tracker.eds_phone_2")?.entity_id) || entities[0]?.entity_id || "";
      const months=(state.available_months || []).map(m=>typeof m==="string"?m:m.month).filter(Boolean).sort();
      const month=months.at(-1) || (state.imports || []).map(x=>x.month).sort().at(-1) || new Date().toISOString().slice(0,7);
      $("rtMonth").value=month; $("rtImportFrom").value=month; $("rtImportTo").value=month;
      $("rtSetup").open=!(state.imports || []).some(x=>x.point_count>0);
    }
    for (const job of state.active_jobs || (state.active_job ? [state.active_job]:[])) {
      if (job.id && ["queued","running","paused"].includes(job.status)) rt.jobs.add(String(job.id));
    }
    $("rtCoverage").textContent=(state.imports || []).length ? (state.imports || []).map(x =>
      x.month+": "+Number(x.point_count || 0).toLocaleString()+" GPS points · "+(x.outing_count || 0)+" outings").join("  |  ") :
      "No location history imported yet. Import a month with footage to begin.";
    $("rtStatus").textContent=rt.jobs.size ? "Location import in progress. You can follow or cancel it in Jobs." :
      state.configured_ha ? "Home Assistant connected. Matches remain suggestions until you confirm them." :
      "Connect Home Assistant in Settings, or import a GPX / Google Timeline export.";
    const saved=(state.matches || []).filter(x=>x.status==="confirmed");
    $("rtSaved").innerHTML=saved.length ? saved.map(x=>'<button type="button" class="ghost" data-rt-match="'+esc(x.id)+'">'+
      esc(x.name || x.month+" roadtrip")+"<br><small>"+esc(when(x.start_time || x.created_at))+"</small></button>").join("") :
      '<p class="hint">No confirmed journeys yet.</p>';
    if (rt.jobs.size) schedule(); buttons();
  }
  function schedule() {
    if (rt.poll) return;
    rt.poll=setTimeout(async()=>{
      rt.poll=null;
      if (document.hidden) {schedule();return;}
      try {
        const results=await Promise.all([...rt.jobs].map(async id=>{
          const res=await fetch("/api/jobs/"+encodeURIComponent(id));
          if (!res.ok) throw new Error("Unable to check location import");
          return [id,await res.json()];
        }));
        let completed=false;
        for (const [id,payload] of results) {
          const job=payload.job || payload;
          if (!["queued","running","paused"].includes(job.status)) {
            rt.jobs.delete(id); completed=true;
            if (job.status!=="done") error(job.error || "Location import "+job.status);
          }
        }
        await refresh();
        if (completed && !rt.jobs.size) await findCandidates();
      } catch(e) {error(e.message);}
      if (rt.jobs.size) schedule();
    },8000);
  }
  async function queue(url,body,multipart=false) {
    const result=await request(url,body,multipart);
    if(result.job_id) rt.jobs.add(String(result.job_id));
    await refresh(); schedule();
  }
  async function saveSettings() {
    const old=rt.state?.settings || {};
    const prefs={entity_id:$("rtEntity").value,timezone:$("rtTimezone").value,
      home_radius_m:Number($("rtRadius").value),max_gap_s:Number($("rtGap").value)*60,
      max_accuracy_m:old.max_accuracy_m || 100,max_speed_kmh:old.max_speed_kmh || 180};
    await request("/settings",prefs); await refresh();
  }
  async function findCandidates() {
    const month=$("rtMonth").value;
    if (!month) throw new Error("Choose the footage month");
    rt.candidates=await request("/candidates?month="+encodeURIComponent(month));
    renderCandidates(); $("rtStatus").textContent=(rt.candidates.outings || []).length ?
      "Compare possible outings below. Similar duration alone cannot identify a ride." :
      "No outings found for this month. Check imported GPS coverage and home settings.";
  }
  function renderCandidates() {
    const data=rt.candidates || {}, groups=data.groups || [], outings=new Map((data.outings || []).map(x=>[String(x.id),x]));
    const suggestions=data.suggestions || [];
    $("rtCandidates").innerHTML=groups.length ? groups.map(group=>{
      const options=suggestions.filter(x=>x.group_key===group.key).slice(0,5);
      return '<article class="roadtrip-group"><h4>'+esc(group.label || group.key)+'</h4><p class="hint">'+
        esc(group.clip_count || group.clips?.length || 0)+" clips · "+esc(duration(group.recorded_duration_s))+
        " recorded"+(group.observed_span_s ? " · "+esc(duration(group.observed_span_s))+" camera span":"")+"</p>"+
        (group.timing_warning ? '<p class="hint">'+esc(group.timing_warning)+"</p>":"")+
        '<details><summary>Choose a smaller clip sequence</summary><p class="hint">Use this when one folder contains several rides.</p>'+
        (group.clips || []).map(c=>'<label style="display:block"><input type="checkbox" data-rt-subset="'+esc(group.key)+'" value="'+esc(c.id)+'" checked> '+
          esc(c.filename || c.path)+" · "+esc(duration(c.duration_s))+"</label>").join("")+
        '<button type="button" class="ghost" data-rt-subset-go="'+esc(group.key)+'">Match selected clips</button></details>'+
        (options.length ? options.map(option=>{
          const outing=outings.get(String(option.outing_id)) || option.outing || {};
          const score=Number(option.score || 0); const percent=score<=1?Math.round(score*100):Math.round(score);
          return '<button type="button" class="roadtrip-option" data-rt-group="'+esc(group.key)+'" data-rt-outing="'+esc(option.outing_id)+'">'+
            esc(when(outing.start_time))+" · "+esc(duration(outing.duration_s))+
            "<small>"+esc(option.confidence || "Uncertain")+" · score "+percent+"%"+
            (option.reasons?.length ? " · "+esc(option.reasons.join(" · ")):"")+"</small></button>";
        }).join(""):'<p class="hint">No plausible match in the imported history.</p>')+"</article>";
    }).join(""):'<div class="roadtrip-empty">No archived clip groups found for this month. Complete the NAS inventory scan in Stats, or import camera recordings first.</div>';
  }
  async function choose(groupKey,outingId) {
    const group=rt.candidates.groups.find(g=>g.key===groupKey);
    rt.selection={month:$("rtMonth").value,group_key:groupKey,outing_id:Number(outingId),
      clip_ids:(group?.clips || []).map(c=>c.id)};
    const detail=await request("/preview",rt.selection);
    renderDetail(detail); $("rtName").value=""; $("rtDetail").scrollIntoView({behavior:"smooth",block:"nearest"});
  }
  function selection() {
    if (!rt.selection) throw new Error("Choose a possible journey first");
    return {...rt.selection,start_time:$("rtAnchor").value || null,
      clip_offsets:Object.fromEntries([...(document.querySelectorAll("[data-rt-offset]"))]
        .filter(input=>input.dataset.rtManual==="true" || Number(input.value)!==Number(input.dataset.rtInitial))
        .map(input=>[input.dataset.rtOffset,Number(input.value)]))};
  }
  function renderDetail(detail) {
    rt.detail=detail; rt.selectedClip=null; $("rtDetail").hidden=false;
    const outing=detail.outing || {}, group=detail.group || {};
    $("rtDetailTitle").textContent=detail.match?.name || group.label || "Journey preview";
    $("rtDetailMeta").textContent=when(outing.start_time)+" → "+when(outing.end_time)+" · "+
      ((outing.distance_m || 0)/1000).toFixed(1)+" km GPS distance · "+duration(outing.duration_s);
    $("rtMatchBadge").textContent=detail.provisional===false ? "Confirmed":"Suggested alignment";
    $("rtAnchor").value=localInput(detail.start_time); $("rtQuality").textContent=detail.warning || group.timing_warning ||
      "GPS gaps are not joined. Speeds describe covered GPS intervals, not a precisely measured road speed.";
    $("rtClips").innerHTML=(detail.clips || []).map(clip=>'<tr data-rt-clip="'+esc(clip.id)+'" tabindex="0">'+
      "<td>"+esc(clip.filename || clip.path)+"<small class=\"hint\" style=\"display:block\">"+
      esc(({manual:"Your alignment",camera_relative:"Relative camera times (unverified)",continuous_recording_assumption:"Assumes continuous recording"})[clip.timing_basis] || "Unverified timing")+"</small>"+
      '<label class="hint">Offset (seconds)<input data-rt-offset="'+esc(clip.id)+'" type="number" step="1" min="0" value="'+
      esc(clip.offset_s || 0)+'" data-rt-initial="'+esc(clip.offset_s || 0)+'" data-rt-manual="'+
      (clip.timing_basis==="manual")+'" style="max-width:110px" aria-label="Offset for '+esc(clip.filename || clip.id)+'"></label></td>'+
      "<td>"+esc(clock(clip.start_time))+"–"+esc(clock(clip.end_time))+"</td><td>"+Math.round((clip.coverage || 0)*100)+
      "%</td><td>"+esc(speed(clip.average_speed_kmh))+"</td></tr>").join("");
    drawMap(); drawGraph(); scrub(0);
  }
  function drawMap() {
    if (!window.L) {$("rtMap").textContent="Map library unavailable. The timeline and clip table still work.";return;}
    if (!rt.map) rt.map=L.map("rtMap",{scrollWheelZoom:false}).setView([51.5,-.1],10);
    if(rt.route) rt.route.remove(); if(rt.marker) {rt.marker.remove();rt.marker=null;}
    rt.clipLayers.forEach(x=>x.remove()); rt.clipLayers=[];
    rt.route=L.geoJSON(rt.detail.geojson || {type:"FeatureCollection",features:[]},{style:{color:"#317d54",weight:4,opacity:.8}}).addTo(rt.map);
    if(rt.route.getBounds().isValid()) rt.map.fitBounds(rt.route.getBounds(),{padding:[22,22],maxZoom:15});
    setTimeout(()=>rt.map.invalidateSize(),0);
  }
  function points() {return (rt.detail?.time_series || []).filter(p=>Number.isFinite(stamp(p.time))).sort((a,b)=>stamp(a.time)-stamp(b.time));}
  function bounds() {
    const outing=rt.detail?.outing || {}, pts=points();
    return [stamp(outing.start_time) || stamp(pts[0]?.time),stamp(outing.end_time) || stamp(pts.at(-1)?.time)];
  }
  function drawGraph() {
    const pts=points(), [start,end]=bounds();
    if(!pts.length || !(end>start)) {$("rtGraph").innerHTML='<p class="hint">Not enough timed GPS data for a graph.</p>';return;}
    const width=720,height=180,left=42,right=12,top=18,bottom=32,graphW=width-left-right,graphH=height-top-bottom;
    const max=Math.max(10,...pts.map(p=>(Number(p.speed_kmh)||0)/1.609344));
    const x=t=>left+Math.max(0,Math.min(1,(t-start)/(end-start)))*graphW;
    const y=v=>top+graphH*(1-v/max); let path="",lastSegment=null;
    const stride=Math.max(1,Math.floor(pts.length/1000));
    pts.forEach((p,i)=>{
      if(i%stride!==0 && i!==pts.length-1) return;
      if(p.speed_kmh===null || p.speed_kmh===undefined) {lastSegment=null;return;}
      const segment=p.segment===undefined?0:p.segment;
      path+=(lastSegment===segment?" L":" M")+x(stamp(p.time)).toFixed(1)+","+y(Number(p.speed_kmh)/1.609344).toFixed(1);
      lastSegment=segment;
    });
    const bands=(rt.detail.clips || []).map(c=>{
      const a=x(stamp(c.start_time)), b=x(stamp(c.end_time));
      return '<rect class="rt-clip-band" x="'+a+'" y="'+top+'" width="'+Math.max(1,b-a)+'" height="'+graphH+'"><title>'+
        esc(c.filename || "Clip")+"</title></rect>";
    }).join("");
    $("rtGraph").innerHTML='<svg viewBox="0 0 '+width+" "+height+'" role="img" aria-label="GPS speed over time with recorded clip intervals">'+
      bands+'<path class="rt-speed" d="'+path+'"/><line id="rtGraphCursor" x1="'+left+'" x2="'+left+'" y1="'+top+'" y2="'+(top+graphH)+'" stroke="#ad722d"/>'+
      '<text x="3" y="'+(top+9)+'">'+Math.round(max)+' mph</text><text x="8" y="'+(top+graphH)+'">0</text>'+
      '<text x="'+left+'" y="'+(height-8)+'">'+esc(clock(new Date(start).toISOString()))+'</text>'+
      '<text text-anchor="end" x="'+(width-right)+'" y="'+(height-8)+'">'+esc(clock(new Date(end).toISOString()))+"</text></svg>";
  }
  function scrub(fraction) {
    if(!rt.detail) return; const [start,end]=bounds(); if(!Number.isFinite(start) || !Number.isFinite(end)) return;
    const time=start+(end-start)*Math.max(0,Math.min(1,fraction)), pts=points();
    let nearest=pts.reduce((best,p)=>!best || Math.abs(stamp(p.time)-time)<Math.abs(stamp(best.time)-time)?p:best,null);
    $("rtCursorTime").textContent=when(new Date(time).toISOString());
    const maxGap=(rt.state?.settings?.max_gap_s || 900)*1000;
    if(nearest && Math.abs(stamp(nearest.time)-time)<=maxGap/2 && rt.map && Number.isFinite(nearest.latitude) && Number.isFinite(nearest.longitude)) {
      if(rt.marker) rt.marker.setLatLng([nearest.latitude,nearest.longitude]);
      else rt.marker=L.circleMarker([nearest.latitude,nearest.longitude],{radius:7,color:"#af762e",fillColor:"#ffd391",fillOpacity:1}).addTo(rt.map);
    } else if(rt.marker) {rt.marker.remove();rt.marker=null;$("rtCursorTime").textContent+=" · GPS gap";}
    const cursor=$("rtGraphCursor"); if(cursor) {const x=42+Math.max(0,Math.min(1,fraction))*666;cursor.setAttribute("x1",x);cursor.setAttribute("x2",x);}
  }
  function selectClip(id) {
    const clip=rt.detail?.clips.find(c=>String(c.id)===String(id)); if(!clip) return;
    const [start,end]=bounds(),f=(stamp(clip.start_time)-start)/(end-start);
    $("rtScrubber").value=Math.max(0,Math.min(100,f*100)); scrub(f);
    document.querySelectorAll("[data-rt-clip]").forEach(row=>row.classList.toggle("rt-selected",row.dataset.rtClip===String(id)));
    rt.clipLayers.forEach(x=>x.remove());rt.clipLayers=[];
    if(rt.map) {
      const selected=points().filter(p=>stamp(p.time)>=stamp(clip.start_time) && stamp(p.time)<=stamp(clip.end_time));
      const bySegment=new Map();
      selected.forEach(p=>{const key=p.segment || 0;if(!bySegment.has(key)) bySegment.set(key,[]);bySegment.get(key).push([p.latitude,p.longitude]);});
      for(const latlngs of bySegment.values()) if(latlngs.length>1) rt.clipLayers.push(L.polyline(latlngs,{color:"#b17b31",weight:7}).addTo(rt.map));
    }
  }
  window.initRoadtrips=async function() {
    if(!$("roadtripTitle")) return;
    $("rtRefresh").addEventListener("click",()=>action(()=>refresh()));
    $("rtSaveSettings").addEventListener("click",()=>action(saveSettings));
    $("rtFind").addEventListener("click",()=>action(findCandidates));
    $("rtProbe").addEventListener("click",()=>action(async()=>queue("/probe",{month:$("rtMonth").value})));
    $("rtImportHA").addEventListener("click",()=>action(async()=>{
      await saveSettings(); const from=$("rtImportFrom").value,to=$("rtImportTo").value || from;
      if(!from || to<from) throw new Error("Choose a valid month range");
      const a=from.split("-").map(Number),b=to.split("-").map(Number);
      if((b[0]-a[0])*12+b[1]-a[1]>=12) throw new Error("Import at most twelve months at a time");
      await queue("/import-ha",{month:from,end_month:to,entity_id:$("rtEntity").value});
    }));
    $("rtImportFile").addEventListener("change",()=>action(async()=>{
      const file=$("rtImportFile").files[0]; if(!file) return;
      const form=new FormData();form.append("month",$("rtMonth").value);form.append("file",file);
      await queue("/import-file",form,true);$("rtImportFile").value="";
    }));
    $("rtPreview").addEventListener("click",()=>action(async()=>renderDetail(await request("/preview",selection()))));
    $("rtConfirm").addEventListener("click",()=>action(async()=>{
      const data=await request("/matches",{...selection(),name:$("rtName").value,status:"confirmed"});
      await refresh(); if(data.id || data.match?.id) renderDetail(await request("/matches/"+(data.id || data.match.id)));
      else {rt.detail.provisional=false;$("rtMatchBadge").textContent="Confirmed";}
      toast("Roadtrip association saved. Video dates are unchanged.");
    }));
    $("rtReject").addEventListener("click",()=>action(async()=>{
      await request("/matches",{...selection(),name:$("rtName").value,status:"rejected"});
      $("rtDetail").hidden=true; await refresh(); await findCandidates();
    }));
    $("rtCandidates").addEventListener("click",e=>{
      const subset=e.target.closest("[data-rt-subset-go]");
      if(subset) { action(async()=>{
        const ids=[...document.querySelectorAll("[data-rt-subset]")].filter(c=>c.dataset.rtSubset===subset.dataset.rtSubsetGo && c.checked).map(c=>c.value);
        if(!ids.length) throw new Error("Choose at least one clip");
        rt.candidates=await request("/candidates?month="+encodeURIComponent($("rtMonth").value)+"&clip_ids="+encodeURIComponent(ids.join(",")));
        renderCandidates();
      });return;}
      const b=e.target.closest("[data-rt-group]");if(b) action(()=>choose(b.dataset.rtGroup,b.dataset.rtOuting));
    });
    $("rtSaved").addEventListener("click",e=>{const b=e.target.closest("[data-rt-match]");if(b) action(async()=>{
      const detail=await request("/matches/"+encodeURIComponent(b.dataset.rtMatch));
      const match=detail.match || {};rt.selection={month:match.month || detail.group.month,group_key:match.group_key || detail.group.key,
        outing_id:match.outing_id || detail.outing.id,clip_ids:(detail.group.clips || []).map(c=>c.id)};
      renderDetail(detail);$("rtName").value=match.name || "";
    });});
    $("rtClips").addEventListener("click",e=>{if(e.target.closest("input")) return;const row=e.target.closest("[data-rt-clip]");if(row) selectClip(row.dataset.rtClip);});
    $("rtClips").addEventListener("keydown",e=>{if(e.key==="Enter" && !e.target.closest("input")){const row=e.target.closest("[data-rt-clip]");if(row) selectClip(row.dataset.rtClip);}});
    $("rtScrubber").addEventListener("input",()=>scrub(Number($("rtScrubber").value)/100));
    $("rtStreetMap").addEventListener("click",()=>{
      if(!rt.map) return;
      if(rt.tiles){rt.tiles.remove();rt.tiles=null;$("rtStreetMap").textContent="Show street map";}
      else {rt.tiles=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,
        attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(rt.map);
        $("rtStreetMap").textContent="Hide street map";}
    });
    await action(async()=>{await refresh(true);if((rt.state.imports || []).length) await findCandidates();});
  };
})();
