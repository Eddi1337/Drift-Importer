"""Camera-agent status and reversible administrative enable/disable controls."""
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy.orm import Session
from ..database import get_session
from ..camera_agents import CameraAgent,devices,_lock,_nodes
import time

router=APIRouter()

@router.get('')
def agent_status(session:Session=Depends(get_session)):
    connected=devices();result=[]
    with _lock:online={id:node['online'] and time.monotonic()-node['seen']<=35 for id,node in _nodes.items()}
    for row in session.query(CameraAgent).order_by(CameraAgent.name):
        cameras=[d for d in connected if d['agent_id']==row.id]
        result.append({'id':row.id,'name':row.name,'enabled':row.enabled,'online':bool(row.enabled and online.get(row.id)),
            'last_seen':row.last_seen.isoformat()+'Z' if row.last_seen else None,
            'error':row.last_error,'cameras':[{k:d[k] for k in ('path','label','file_count','free_bytes','total_bytes')} for d in cameras]})
    return {'agents':result}

class AgentEdit(BaseModel):
    name:str=Field(min_length=1,max_length=128)
    enabled:bool=True

@router.patch('/{agent_id}')
def edit_agent(agent_id:str,request:AgentEdit,session:Session=Depends(get_session)):
    row=session.get(CameraAgent,agent_id)
    if row is None:raise HTTPException(404,'Camera agent not found')
    if not request.name.strip():raise HTTPException(422,'Agent name cannot be blank')
    row.name=request.name.strip();row.enabled=request.enabled;session.commit()
    return {'saved':True}
