"""Copy the Pi's upload metadata into the processor's separate SQLite database.

The source owns the explicitly listed input tables. Jobs, HA credentials, GPS
coordinates, ride associations and render state never travel in this snapshot.
HA settings and GPS indexes are seeded once; the processor owns later imports.
No helper here opens a camera, video, GPS cache or other NAS file.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
import os
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, bindparam, insert, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from .archive_models import ArchiveFile
from .config import get_settings
from .database import Base
from .models import CorrectedArchive, Destination, MediaItem, RecordingDate, UploadedClip
from .roadtrip_models import LocationDay, RoadtripClipMetadata, RoadtripOuting, RoadtripSettings

VERSION = 1
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_ROWS = 100_000
_SOURCE_PATH = "/api/roadtrips/processor-input"


class ProcessingSyncState(Base):
    __tablename__ = "processing_sync_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    digest: Mapped[str] = mapped_column(String(64), default="")
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    archive_ids: Mapped[str] = mapped_column(Text, default="[]")
    seeded: Mapped[bool] = mapped_column(Boolean, default=False)


@dataclass(frozen=True)
class _Spec:
    model: type
    fields: tuple[str, ...]
    seed_only: bool = False
    natural_keys: tuple[tuple[str, ...], ...] = ()

    @property
    def table(self):
        return self.model.__table__

    @property
    def key(self):
        return next(iter(self.table.primary_key.columns)).name


def _spec(model, fields, **kwargs):
    return _Spec(model, tuple(fields.split()), **kwargs)


# These field names are intentionally literal. A future model column must not
# silently export an authentication token, raw track or unrelated application data.
SPECS = (
    _spec(Destination, "id name type is_default enabled rank base_path path_template created_at"),
    _spec(MediaItem, "id path filename kind size_bytes duration_s codec width height capture_time checksum thumbnail source derived created_at"),
    _spec(UploadedClip, "id destination_id source_media_id checksum filename size_bytes remote_path temp_remote_path status bytes_uploaded upload_duration_s upload_throughput_bps uploaded_at full_verification_failed created_at updated_at"),
    _spec(RecordingDate, "id camera_root path relative_path size_bytes mtime_ns checksum original_time corrected_time duration_s time_source status reasons revision correction_id metadata_copy reuse_offset first_seen"),
    _spec(CorrectedArchive, "id recording_id destination_id corrected_time original_path corrected_path"),
    _spec(ArchiveFile, "id destination_id root_path path kind size_bytes mtime_ns ctime_ns device inode nlink mode uid gid present eligible ledger_id ledger_status sample_sha256 full_sha256 verified_at checked_at last_seen_at"),
    _spec(RoadtripClipMetadata, "archive_id identity duration_s observed_time probed_at error"),
    _spec(RoadtripSettings, "id entity_id timezone home_radius_m max_accuracy_m max_gap_s max_speed_kmh updated_at", seed_only=True),
    _spec(LocationDay, "id source day cache_path point_count rejected_count imported_at", seed_only=True,
          natural_keys=(("source", "day"),)),
    _spec(RoadtripOuting, "id signature source month start_time end_time duration_s distance_m point_count gap_count complete active", seed_only=True,
          natural_keys=(("signature",),)),
)


def _scalar(value):
    if isinstance(value, dt.datetime):
        value = value.replace(tzinfo=value.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return value


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _digest(rows):
    return hashlib.sha256(_encoded({"version": VERSION, "rows": rows})).hexdigest()


def _safe_root(value):
    """Lexical check only; exporting metadata must never wait on the NAS."""
    root = os.path.normpath(str(get_settings().nas_root))
    value = os.path.normpath(str(value or ""))
    return os.path.isabs(value) and os.path.commonpath((root, value)) == root


def export_snapshot(session: Session):
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        # Pysqlite's legacy transaction mode does not BEGIN for SELECT, even
        # after SQLAlchemy's logical autobegin. Hold one real read snapshot so
        # an upload committed between table reads cannot leave dangling IDs.
        # Existing caller transactions are reused and never committed here.
        driver = connection.connection.driver_connection
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")
    destination_spec = SPECS[0]
    destination_rows = list(connection.execute(select(*(destination_spec.table.c[name] for name in destination_spec.fields))
        .where(Destination.__table__.c.type == "local").order_by(Destination.__table__.c.id)).mappings())
    destinations = [row for row in destination_rows if _safe_root(row["base_path"])]
    destination_ids = {row["id"] for row in destinations}
    rows = {}
    archive_ids = set()
    for spec in SPECS:
        if spec.model is Destination:
            values = destinations
        else:
            query = select(*(spec.table.c[name] for name in spec.fields)).order_by(spec.table.c[spec.key])
            if spec.model in (UploadedClip, CorrectedArchive, ArchiveFile):
                query = query.where(spec.table.c.destination_id.in_(destination_ids))
            elif spec.model is RoadtripClipMetadata:
                query = query.where(spec.table.c.archive_id.in_(archive_ids))
            values = list(connection.execute(query).mappings())
        rows[spec.table.name] = [{name: _scalar(row[name]) for name in spec.fields} for row in values]
        if spec.model is ArchiveFile:
            archive_ids = {row["id"] for row in values}
    payload = {"version": VERSION, "digest": _digest(rows), "rows": rows}
    if len(_encoded(payload)) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Upload metadata exceeds the 16 MB processing snapshot limit")
    return payload


def _value(column, value):
    if value is None:
        if not column.nullable:
            raise ValueError(f"Missing required processing field: {column.name}")
        return None
    kind = column.type
    if isinstance(kind, Boolean):
        if type(value) is not bool:
            raise ValueError(f"Invalid boolean processing field: {column.name}")
    elif isinstance(kind, Integer):
        if type(value) is not int or not -(2**63) <= value < 2**63:
            raise ValueError(f"Invalid integer processing field: {column.name}")
    elif isinstance(kind, Float):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Invalid numeric processing field: {column.name}")
    elif isinstance(kind, DateTime):
        if not isinstance(value, str):
            raise ValueError(f"Invalid date processing field: {column.name}")
        try:
            result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise ValueError()
            return result.astimezone(dt.timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError):
            raise ValueError(f"Invalid date processing field: {column.name}") from None
    elif isinstance(kind, (String, Text)):
        if not isinstance(value, str) or len(value) > (kind.length or 262_144):
            raise ValueError(f"Invalid text processing field: {column.name}")
    else:
        raise ValueError("Unsupported processing field type")
    return value


def _validate(payload):
    if not isinstance(payload, dict) or set(payload) != {"version", "digest", "rows"} or type(payload["version"]) is not int or payload["version"] != VERSION:
        raise ValueError("Unsupported processing metadata snapshot")
    if not isinstance(payload["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["digest"]):
        raise ValueError("Invalid processing metadata digest")
    rows = payload["rows"]
    if not isinstance(rows, dict) or set(rows) != {spec.table.name for spec in SPECS}:
        raise ValueError("Processing snapshot must include the complete allowed table set")
    if len(_encoded(payload)) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Processing snapshot exceeds 16 MB")
    if _digest(rows) != payload["digest"]:
        raise ValueError("Processing metadata digest does not match its contents")
    validated, count = {}, 0
    for spec in SPECS:
        table_rows = rows[spec.table.name]
        if not isinstance(table_rows, list):
            raise ValueError("Invalid processing table")
        count += len(table_rows)
        if count > MAX_ROWS:
            raise ValueError("Too many processing metadata rows")
        result, seen = [], set()
        for row in table_rows:
            if not isinstance(row, dict) or set(row) != set(spec.fields):
                raise ValueError(f"Unexpected fields in processing table: {spec.table.name}")
            converted = {name: _value(spec.table.c[name], row[name]) for name in spec.fields}
            key = converted[spec.key]
            if key <= 0 or key in seen:
                raise ValueError("Invalid or repeated processing row identity")
            seen.add(key)
            if spec.model is Destination and (converted["type"] != "local" or not _safe_root(converted["base_path"])):
                raise ValueError("Processing snapshots only accept configured NAS destinations")
            result.append(converted)
        validated[spec.table.name] = result
    identifiers = {spec.table.name: {row[spec.key] for row in validated[spec.table.name]} for spec in SPECS}
    for spec in SPECS:
        for row in validated[spec.table.name]:
            for name in spec.fields:
                for foreign_key in spec.table.c[name].foreign_keys:
                    target = foreign_key.column
                    if row[name] is not None and row[name] not in identifiers.get(target.table.name, set()):
                        raise ValueError("Processing metadata contains a missing parent row")
    return validated


def _upsert(connection, spec, rows, *, seed=False):
    existing = {row[spec.key]: row for row in connection.execute(select(*(spec.table.c[name] for name in spec.fields))).mappings()}
    natural = [{tuple(row[name] for name in names) for row in existing.values()} for names in spec.natural_keys]
    new, groups = [], {}
    for row in rows:
        previous = existing.get(row[spec.key])
        if previous is None:
            if seed and any(tuple(row[name] for name in names) in keys for names, keys in zip(spec.natural_keys, natural)):
                continue
            new.append(row)
            for names, keys in zip(spec.natural_keys, natural):
                keys.add(tuple(row[name] for name in names))
        elif not seed:
            if spec.model is RecordingDate and previous["revision"] > row["revision"]:
                continue  # An older Pi observation must not undo a processor correction.
            changed = tuple(name for name in spec.fields if name != spec.key and previous[name] != row[name])
            if changed:
                groups.setdefault(changed, []).append({"_sync_pk": row[spec.key], **{name: row[name] for name in changed}})
    if new:
        connection.execute(insert(spec.table), new)
    updated = 0
    for fields, values in groups.items():
        query = update(spec.table).where(spec.table.c[spec.key] == bindparam("_sync_pk"))
        connection.execute(query.values({name: bindparam(name) for name in fields}), values)
        updated += len(values)
    return len(new), updated


def import_snapshot(session: Session, payload, *, etag: str | None = None):
    rows = _validate(payload)  # Validate the entire payload before the first write.
    connection = session.connection()
    state_table = ProcessingSyncState.__table__
    state = connection.execute(select(state_table).where(state_table.c.id == 1)).mappings().first()
    result = {"changed": False, "digest": payload["digest"], "inserted": 0, "updated": 0, "retired_archives": 0}
    if state and state["digest"] == payload["digest"]:
        return result
    first = not state or not state["seeded"]
    central_revisions = dict(connection.execute(select(RecordingDate.__table__.c.id, RecordingDate.__table__.c.revision)).all())
    protected_dates = {row["id"] for row in rows[RecordingDate.__tablename__]
                       if central_revisions.get(row["id"], -1) > row["revision"]}
    for spec in SPECS:
        if spec.seed_only and not first:
            continue
        source_rows = rows[spec.table.name]
        if spec.model is CorrectedArchive:
            source_rows = [row for row in source_rows if row["recording_id"] not in protected_dates]
        inserted, updated = _upsert(connection, spec, source_rows, seed=spec.seed_only)
        result["inserted"] += inserted
        result["updated"] += updated
    incoming = {row["id"] for row in rows[ArchiveFile.__tablename__]}
    previous_ids = set(json.loads(state["archive_ids"])) if state else set()
    missing = previous_ids - incoming
    if missing:
        table = ArchiveFile.__table__
        result["retired_archives"] = connection.execute(update(table).where(table.c.id.in_(missing), table.c.present.is_(True)).values(present=False)).rowcount
    values = {"digest": payload["digest"], "etag": etag, "archive_ids": json.dumps(sorted(incoming), separators=(",", ":")), "seeded": True}
    if state:
        connection.execute(update(state_table).where(state_table.c.id == 1).values(**values))
    else:
        connection.execute(insert(state_table).values(id=1, **values))
    return result | {"changed": True, "seeded": first, "preserved_date_revisions": len(protected_dates)}


def _source_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Choose an HTTP processing source without embedded credentials")
    path = parsed.path.rstrip("/")
    if path != _SOURCE_PATH:
        path += _SOURCE_PATH
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def sync_from_source(session: Session, url: str, *, client: httpx.Client | None = None):
    source = _source_url(url)
    table = ProcessingSyncState.__table__
    state = session.connection().execute(select(table).where(table.c.id == 1)).mappings().first()
    tag = state["etag"] or '"' + state["digest"] + '"' if state and state["digest"] else None
    headers = {"If-None-Match": tag} if tag else {}
    timeout = httpx.Timeout(30, connect=10)
    with (nullcontext(client) if client is not None else httpx.Client(follow_redirects=False, timeout=timeout)) as active:
        with active.stream("GET", source, headers=headers, timeout=timeout, follow_redirects=False) as response:
            if response.status_code == 304:
                if not state or not state["digest"]:
                    raise ValueError("Processing source returned no initial snapshot")
                return {"changed": False, "digest": state["digest"], "not_modified": True}
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_SNAPSHOT_BYTES:
                raise ValueError("Processing snapshot exceeds 16 MB")
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Processing snapshot exceeds 16 MB")
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError):
                raise ValueError("Processing source returned invalid JSON") from None
            etag = response.headers.get("ETag")
            if etag and (len(etag) > 256 or any(c in etag for c in "\r\n")):
                etag = None
            return import_snapshot(session, payload, etag=etag)
