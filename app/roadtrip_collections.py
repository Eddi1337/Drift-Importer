"""Named, cross-month clip collections using compact database references only.

Listing, grouping and renaming never probe or open footage, nor read private GPS
files. Camera timestamps remain explicitly unverified; precise bounds come only
from a user-confirmed camera/GPS association.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from . import roadtrips as rt
from .archive_models import ArchiveFile
from .models import MediaItem, UploadedClip, utcnow
from .roadtrip_models import (
    RoadtripClipMetadata, RoadtripCollection, RoadtripCollectionClip, RoadtripMatch, RoadtripOuting,
)

MAX_COLLECTION_CLIPS = 10000
_CLIP_ID = re.compile(r"(?:media|archive|ledger):[1-9]\d*")


def _json(value, fallback):
    try:
        result = json.loads(value or "null")
        return result if isinstance(result, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


def _indexed_clips(session):
    """Use scalar projections instead of ORM media trees or filesystem stats."""
    media = {r.id: r for r in session.query(MediaItem.id, MediaItem.filename, MediaItem.path,
        MediaItem.duration_s, MediaItem.capture_time, MediaItem.checksum, MediaItem.size_bytes).filter(
            MediaItem.kind == "video", MediaItem.derived.is_(False))}
    ledger = {r.id: r for r in session.query(UploadedClip.id, UploadedClip.source_media_id,
        UploadedClip.remote_path, UploadedClip.filename, UploadedClip.checksum, UploadedClip.size_bytes).filter(
            UploadedClip.status == "done")}
    checksums = {r.checksum: r for r in media.values() if r.checksum}
    metadata = {r.archive_id: r for r in session.query(RoadtripClipMetadata)}
    clips, seen, archived_ledger = [], {}, {}
    for archived in session.query(ArchiveFile.id, ArchiveFile.path, ArchiveFile.ledger_id,
            ArchiveFile.size_bytes, ArchiveFile.mtime_ns, ArchiveFile.ctime_ns, ArchiveFile.device,
            ArchiveFile.inode).filter(ArchiveFile.present.is_(True), ArchiveFile.kind == "video").order_by(ArchiveFile.id):
        entry = ledger.get(archived.ledger_id)
        item = (media.get(entry.source_media_id) or checksums.get(entry.checksum)) if entry else None
        identity = f"media:{item.id}" if item else f"archive:{archived.id}"
        aliases = [identity, f"archive:{archived.id}"]
        if entry:
            aliases.append(f"ledger:{entry.id}")
            archived_ledger[entry.id] = identity
        if identity in seen:
            seen[identity]["aliases"].extend(a for a in aliases if a not in seen[identity]["aliases"])
            continue
        cached = metadata.get(archived.id)
        if cached and cached.identity != rt._metadata_identity(archived):
            cached = None
        month, basis = rt.folder_month(archived.path)
        observed = item.capture_time if item and item.capture_time else cached.observed_time if cached else None
        clip = {"id": identity, "media_id": item.id if item else None,
            "archive_id": archived.id, "ledger_id": entry.id if entry else None,
            "aliases": list(dict.fromkeys(aliases)), "filename": item.filename if item else Path(archived.path).name,
            "path": archived.path, "month": month, "month_basis": basis,
            "duration_s": item.duration_s if item and item.duration_s else cached.duration_s if cached else None,
            "size_bytes": archived.size_bytes if archived.size_bytes > 0 else None,
            "observed_time": rt.iso(observed) if observed else None, "date_verified": False,
            "indexed": True}
        clips.append(clip)
        seen[identity] = clip
    for entry in ledger.values():
        item = media.get(entry.source_media_id) or checksums.get(entry.checksum)
        identity = f"media:{item.id}" if item else f"ledger:{entry.id}"
        existing = seen.get(identity) or seen.get(archived_ledger.get(entry.id))
        if existing:
            alias = f"ledger:{entry.id}"
            if alias not in existing["aliases"]:
                existing["aliases"].append(alias)
            continue
        month, basis = rt.folder_month(entry.remote_path or "")
        size = entry.size_bytes or (item.size_bytes if item else 0)
        clip = {"id": identity, "media_id": item.id if item else None, "archive_id": None,
            "ledger_id": entry.id, "aliases": list(dict.fromkeys([identity, f"ledger:{entry.id}"])),
            "filename": item.filename if item else entry.filename, "path": entry.remote_path,
            "month": month, "month_basis": basis, "duration_s": item.duration_s if item else None,
            "size_bytes": size if size > 0 else None,
            "observed_time": rt.iso(item.capture_time) if item and item.capture_time else None,
            "date_verified": False, "indexed": True}
        clips.append(clip)
        seen[identity] = clip
    return clips


def _clip_index(clips):
    return {alias: clip for clip in clips for alias in clip.get("aliases", [clip["id"]])}


def confirmed_alignments(session):
    """Reconstruct confirmed timing from saved metadata without loading GPS."""
    by_clip, matches = {}, {}
    sources = dict(session.query(RoadtripOuting.id, RoadtripOuting.source).all())
    for row in session.query(RoadtripMatch).filter_by(status="confirmed").order_by(RoadtripMatch.updated_at, RoadtripMatch.id):
        mapping = _json(row.mapping, {})
        group = mapping.get("group") or {}
        if not isinstance(group, dict):
            continue
        clips = group.get("clips") or []
        if not clips or not mapping.get("start_time"):
            continue
        try:
            anchor = rt.timestamp(mapping["start_time"])
            relative = bool(group.get("relative_timing_available"))
            origin = rt.timestamp(clips[0]["observed_time"]) if relative else None
            offsets, sequential = mapping.get("clip_offsets") or {}, 0.0
            alignments = []
            for clip in clips:
                clip_id = clip["id"]
                duration = max(0, float(clip.get("duration_s") or 0))
                offset = float(offsets[clip_id]) if clip_id in offsets else (
                    (rt.timestamp(clip["observed_time"]) - origin).total_seconds() if origin else sequential)
                start = anchor + dt.timedelta(seconds=offset)
                alignment = {"match_id": row.id, "outing_id": row.outing_id,
                    "source": sources.get(row.outing_id), "name": row.name, "month": row.month,
                    "start_time": rt.iso(start), "end_time": rt.iso(start + dt.timedelta(seconds=duration)),
                    "status": "confirmed", "provisional": False,
                    "timing_basis": "manual" if clip_id in offsets else "camera_relative" if origin else "continuous_recording_assumption"}
                aliases = {clip_id}
                if clip.get("archive_id"):
                    aliases.add(f"archive:{clip['archive_id']}")
                if clip.get("media_id"):
                    aliases.add(f"media:{clip['media_id']}")
                alignments.extend((alias, alignment) for alias in aliases)
                sequential += duration
        except (ValueError, KeyError, TypeError, OverflowError):
            # An old malformed record must not hide otherwise valid footage.
            continue
        matches[row.id] = rt.match_dict(row)
        for clip_id, alignment in alignments:
            by_clip.setdefault(clip_id, []).append(alignment)
    return by_clip, matches


def _decorate(clips, by_clip):
    result = []
    for clip in clips:
        alignments = {a["match_id"]: a for alias in clip.get("aliases", [clip["id"]])
            for a in by_clip.get(alias, [])}
        # In-place collection references retain their original id after a media
        # row is removed, while aliases recover the currently indexed archive.
        alignments.update({a["match_id"]: a for a in by_clip.get(clip["id"], [])})
        alignments = list(alignments.values())
        conflict = len({(a["outing_id"], a["start_time"], a["end_time"]) for a in alignments}) > 1
        result.append(clip | {"alignment": alignments[-1] if alignments and not conflict else None,
            "alignment_conflict": conflict, "alignments": alignments,
            "match_ids": [a["match_id"] for a in alignments],
            "location_status": "ambiguous" if conflict else "matched" if alignments else "unmatched",
            "location_note": "Conflicting confirmed dates; review the GPS associations" if conflict else
                "User-confirmed GPS association; positions remain approximate" if alignments else "No confirmed location"})
    return result


def library(session, month=None, offset=0, limit=1000):
    if month:
        rt.month_bounds(month)
    if offset < 0 or not 1 <= limit <= MAX_COLLECTION_CLIPS:
        raise ValueError("Choose a valid page of at most 10,000 clips")
    clips = [clip for clip in _indexed_clips(session) if not month or clip["month"] == month]
    clips.sort(key=lambda c: (c["month"] or "", c["path"] or "", c["id"]), reverse=True)
    total = len(clips)
    by_clip, _ = confirmed_alignments(session)
    return {"clips": _decorate(clips[offset:offset + limit], by_clip), "total": total,
        "offset": offset, "limit": limit, "has_more": offset + limit < total}


def _members(session, collection_id):
    return session.query(RoadtripCollectionClip).filter_by(collection_id=collection_id).order_by(RoadtripCollectionClip.position).all()


def collection_detail(session, row, *, indexed=None, alignments=None):
    indexed = indexed if indexed is not None else _clip_index(_indexed_clips(session))
    by_clip, matches = alignments if alignments is not None else confirmed_alignments(session)
    clips = []
    for member in _members(session, row.id):
        clip = indexed.get(member.clip_id)
        snapshot = _json(member.snapshot, {})
        if clip is None:
            clip = next((indexed[a] for a in snapshot.get("aliases", []) if a in indexed), None)
        if clip is None:
            clip = snapshot | {"id": member.clip_id, "indexed": False}
        clips.append(clip | {"current_id": clip["id"], "id": member.clip_id})
    clips = _decorate(clips, by_clip)
    matched = {mid for clip in clips for mid in clip["match_ids"]}
    # Explicit route references were validated at save time; ignored if later rejected.
    matched.update(mid for mid in _json(row.match_ids, []) if mid in matches)
    bounds = [c["alignment"] for c in clips if c["alignment"]]
    months = sorted({c.get("month") for c in clips if c.get("month")})
    known_sizes = [c["size_bytes"] for c in clips if c.get("size_bytes") is not None]
    return {"id": row.id, "name": row.name, "created_at": rt.iso(row.created_at),
        "updated_at": rt.iso(row.updated_at), "clip_count": len(clips), "clip_ids": [c["id"] for c in clips],
        "clips": clips, "months": months, "month_start": months[0] if months else None,
        "month_end": months[-1] if months else None, "matched_clip_count": len(bounds),
        "ambiguous_clip_count": sum(c["alignment_conflict"] for c in clips),
        "unmatched_clip_count": len(clips) - len(bounds), "unavailable_clip_count": sum(not c.get("indexed", False) for c in clips),
        "duration_s": sum(c.get("duration_s") or 0 for c in clips),
        "size_bytes": sum(known_sizes) if len(known_sizes) == len(clips) else None,
        "known_size_bytes": sum(known_sizes), "unknown_size_count": len(clips) - len(known_sizes),
        "start_time": min((a["start_time"] for a in bounds), default=None),
        "end_time": max((a["end_time"] for a in bounds), default=None),
        "match_ids": sorted(matched), "matches": [matches[mid] for mid in sorted(matched)],
        "dates_note": "Exact bounds use confirmed GPS associations only; camera timestamps are unverified."}


def list_collections(session):
    indexed = _clip_index(_indexed_clips(session))
    alignments = confirmed_alignments(session)
    result = []
    for row in session.query(RoadtripCollection).order_by(RoadtripCollection.updated_at.desc(), RoadtripCollection.id.desc()):
        detail = collection_detail(session, row, indexed=indexed, alignments=alignments)
        result.append({key: value for key, value in detail.items() if key not in ("clips", "clip_ids")})
    return {"collections": result}


def save_collection(session, name, clip_ids, match_ids=None, row=None):
    name = name.strip()
    if not name or len(name) > 256:
        raise ValueError("Give the trip a name of 1–256 characters")
    if not clip_ids or len(clip_ids) > MAX_COLLECTION_CLIPS:
        raise ValueError("Choose between 1 and 10,000 video files")
    if any(not isinstance(cid, str) or not _CLIP_ID.fullmatch(cid) for cid in clip_ids):
        raise ValueError("Choose indexed video files")
    clip_ids = list(dict.fromkeys(clip_ids))
    indexed = _clip_index(_indexed_clips(session))
    previous = {m.clip_id: m for m in _members(session, row.id)} if row else {}
    if set(clip_ids) - set(indexed) - set(previous):
        raise ValueError("Some selected files are no longer indexed; refresh the file list")
    # A browser may hold an older archive/ledger alias for the same camera clip.
    # Count that source once while keeping the member's original stable id.
    normalized, seen = [], set()
    for clip_id in clip_ids:
        clip = indexed.get(clip_id)
        if clip is None and clip_id in previous:
            snapshot = _json(previous[clip_id].snapshot, {})
            clip = next((indexed[a] for a in snapshot.get("aliases", []) if a in indexed), None)
        identity = clip["id"] if clip else clip_id
        if identity not in seen:
            normalized.append(clip_id)
            seen.add(identity)
    clip_ids = normalized
    by_clip, matches = confirmed_alignments(session)
    requested_matches = list(dict.fromkeys(match_ids or []))
    selected_matches = {a["match_id"] for cid in clip_ids if cid in indexed
        for alias in indexed[cid].get("aliases", [cid])
        for a in by_clip.get(alias, [])}
    selected_matches.update(a["match_id"] for cid in clip_ids for a in by_clip.get(cid, []))
    if len(requested_matches) > 1000 or set(requested_matches) - set(matches) or set(requested_matches) - selected_matches:
        raise ValueError("Choose confirmed GPS matches belonging to these video files")
    if row is None:
        row = RoadtripCollection(name=name)
        session.add(row)
        session.flush()
    row.name, row.updated_at = name, utcnow()
    row.match_ids = json.dumps(requested_matches, separators=(",", ":"))
    for position, clip_id in enumerate(clip_ids):
        member = previous.pop(clip_id, None)
        if member is None:
            member = RoadtripCollectionClip(collection_id=row.id, clip_id=clip_id, position=position)
            session.add(member)
        member.position = position
        if clip_id in indexed:
            member.snapshot = json.dumps(indexed[clip_id], separators=(",", ":"))
    for member in previous.values():
        session.delete(member)
    session.flush()
    return collection_detail(session, row, indexed=indexed, alignments=(by_clip, matches))
