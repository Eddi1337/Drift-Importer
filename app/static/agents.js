function initAgents(){
 const list=document.getElementById('agentsList'),status=document.getElementById('agentsStatus');
 async function load(){
  try{const response=await fetch('/api/agents');if(!response.ok)throw Error('Unable to load camera agents');const data=await response.json();list.replaceChildren();
   for(const node of data.agents){const card=document.createElement('section');card.className='panel';const title=document.createElement('h2');title.textContent=node.name;card.append(title);
    const state=document.createElement('p');state.textContent=!node.enabled?'Disabled':node.online?'Online · '+node.cameras.length+' camera(s)':'Offline · pending uploads will resume when it returns';card.append(state);
    for(const camera of node.cameras){const p=document.createElement('p');p.textContent=camera.label+' · '+camera.file_count.toLocaleString()+' files';card.append(p);}
    const seen=document.createElement('p');seen.className='hint';seen.textContent=node.last_seen?'Last seen '+new Date(node.last_seen).toLocaleString():'Waiting for first connection';card.append(seen);
    const button=document.createElement('button');button.className='ghost';button.textContent=node.enabled?'Disable agent':'Enable agent';button.onclick=async()=>{button.disabled=true;try{const result=await fetch('/api/agents/'+encodeURIComponent(node.id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:node.name,enabled:!node.enabled})});if(!result.ok)throw Error('Could not update agent');await load();}catch(e){status.textContent=e.message;button.disabled=false;}};card.append(button);list.append(card);
   }
   status.textContent=data.agents.length?'':'No camera agents have been enrolled yet.';
  }catch(e){status.textContent=e.message;}
 }
 document.getElementById('agentsRefresh').onclick=load;load();setInterval(()=>{if(!document.hidden)load();},15000);
}
