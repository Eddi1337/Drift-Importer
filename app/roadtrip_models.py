"""Compact location index. Raw, private tracks live only on the mounted NAS."""
from __future__ import annotations

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import utcnow


class RoadtripSettings(Base):
    __tablename__ = "roadtrip_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    entity_id: Mapped[str] = mapped_column(String(256), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/London")
    home_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_radius_m: Mapped[float] = mapped_column(Float, default=150)
    max_accuracy_m: Mapped[float] = mapped_column(Float, default=100)
    max_gap_s: Mapped[int] = mapped_column(Integer, default=900)
    max_speed_kmh: Mapped[float] = mapped_column(Float, default=180)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class LocationDay(Base):
    __tablename__ = "location_days"
    __table_args__ = (UniqueConstraint("source", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(256), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    cache_path: Mapped[str] = mapped_column(String(1024))
    point_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class RoadtripOuting(Base):
    __tablename__ = "roadtrip_outings"
    signature: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(256), index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    start_time: Mapped[object] = mapped_column(DateTime)
    end_time: Mapped[object] = mapped_column(DateTime)
    duration_s: Mapped[float] = mapped_column(Float)
    distance_m: Mapped[float] = mapped_column(Float)
    point_count: Mapped[int] = mapped_column(Integer)
    gap_count: Mapped[int] = mapped_column(Integer, default=0)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RoadtripMatch(Base):
    __tablename__ = "roadtrip_matches"
    __table_args__ = (UniqueConstraint("month", "group_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    group_key: Mapped[str] = mapped_column(String(128))
    outing_id: Mapped[int] = mapped_column(ForeignKey("roadtrip_outings.id"))
    name: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    # Explicit mapping, including per-clip anchors. Never modifies file metadata.
    mapping: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class RoadtripClipMetadata(Base):
    """Rebuildable probe cache, valid only for the exact inventoried file."""
    __tablename__ = "roadtrip_clip_metadata"
    archive_id: Mapped[int] = mapped_column(ForeignKey("archive_files.id", ondelete="CASCADE"), primary_key=True)
    identity: Mapped[str] = mapped_column(String(64))
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_time: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    probed_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class RoadtripRejection(Base):
    __tablename__ = "roadtrip_rejections"
    __table_args__ = (UniqueConstraint("month", "group_key", "outing_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    group_key: Mapped[str] = mapped_column(String(128))
    outing_id: Mapped[int] = mapped_column(ForeignKey("roadtrip_outings.id"))
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class RoadtripCollection(Base):
    """A named set of clip references, independent of movie creation or GPS."""
    __tablename__ = "roadtrip_collections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    # Optional route associations supplement automatically discovered matches.
    match_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class RoadtripCollectionClip(Base):
    __tablename__ = "roadtrip_collection_clips"
    __table_args__ = (UniqueConstraint("collection_id", "clip_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("roadtrip_collections.id", ondelete="CASCADE"), index=True)
    clip_id: Mapped[str] = mapped_column(String(64), index=True)
    position: Mapped[int] = mapped_column(Integer)
    # A small fallback keeps membership visible after an archive is unavailable.
    # Raw GPS and video data are never stored in this table.
    snapshot: Mapped[str] = mapped_column(Text, default="{}")
