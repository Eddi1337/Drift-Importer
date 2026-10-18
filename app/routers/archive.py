"""Cached archive reports and explicit background audit/consolidation actions."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..archive import archive_summary, duplicate_report, get_duplicate_group, list_files
from ..archive_tasks import QUEUE_LOCK, active_archive_job, job_summary
from ..database import get_session, session_scope
from ..models import Destination, Job

router = APIRouter()


class ScanRequest(BaseModel):
    destination_ids: list[int] | None = None


class HardlinkRequest(BaseModel):
    keeper_id: int
    file_ids: list[int] = Field(min_length=1, max_length=500)
    confirm: bool = False


def _active(session):
    return job_summary(active_archive_job(session))


@router.get("/summary")
def summary(session: Session = Depends(get_session)):
    return {**archive_summary(session), "active_job": _active(session)}


@router.get("/files")
def files(destination_id: int | None = None, offset: int = 0, limit: int = 100,
          status: Literal["all", "present", "missing", "unchecked"] = "all", session: Session = Depends(get_session)):
    return list_files(session, destination_id, offset, limit, status)


@router.get("/duplicates")
def duplicates(offset: int = 0, limit: int = 500, session: Session = Depends(get_session)):
    return {**duplicate_report(session, offset, limit), "active_job": _active(session)}


def _enqueue(kind, description, payload, group_id=None):
    # One Uvicorn process owns all workers. This lock covers the active check
    # and insertion, including simultaneous browser clicks on separate threads.
    with QUEUE_LOCK, session_scope() as session:
        active = active_archive_job(session)
        if active:
            if kind == "archive_scan" and active.kind == kind:
                return {"job_id": active.id, "existing": True}
            raise HTTPException(409, "Another archive job is active; wait for it to finish")
        if group_id:
            group = get_duplicate_group(session, group_id)
            if not group:
                raise HTTPException(409, "Duplicate report changed; refresh or scan again")
            members = {f["id"]: f for f in group["files"]}
            if kind == "archive_verify":
                payload["file_ids"] = list(members)
            else:
                if payload.get("confirm") is not True:
                    raise HTTPException(400, "Confirm the hardlink operation before continuing")
                if group["status"] != "verified":
                    raise HTTPException(409, "Verify complete file content before hardlinking")
                if payload["keeper_id"] not in members or any(i not in members for i in payload["file_ids"]):
                    raise HTTPException(400, "Selected files are not members of this duplicate group")
                if payload["keeper_id"] in payload["file_ids"]:
                    raise HTTPException(400, "The kept file cannot also be a duplicate target")
            payload["group_id"] = group_id
        if kind == "archive_scan" and payload.get("destination_ids") is not None:
            ids = payload["destination_ids"]
            known = {i for (i,) in session.query(Destination.id).filter(Destination.enabled.is_(True), Destination.id.in_(ids))}
            if not ids or set(ids) != known:
                raise HTTPException(400, "Select existing enabled destinations")
        job = Job(kind=kind, description=description, status="queued", payload=json.dumps(payload))
        session.add(job)
        session.flush()
        return {"job_id": job.id}


@router.post("/scan")
def scan(request: ScanRequest):
    return _enqueue("archive_scan", "Fast NAS inventory and duplicate samples", request.model_dump())


@router.post("/duplicates/{group_id}/verify")
def verify(group_id: str):
    return _enqueue("archive_verify", "Verify complete duplicate content", {}, group_id)


@router.post("/duplicates/{group_id}/hardlink")
def hardlink(group_id: str, request: HardlinkRequest):
    return _enqueue("archive_hardlink", "Consolidate confirmed identical NAS copies", request.model_dump(), group_id)
