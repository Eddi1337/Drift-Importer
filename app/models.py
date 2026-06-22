"""SQLAlchemy ORM models for Drift-Import."""
from __future__ import annotations

import datetime as dt
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# Many-to-many association tables -------------------------------------------

media_tags = Table(
    "media_tags",
    Base.metadata,
    Column("media_id", ForeignKey("media_items.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    filename: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16), default="video")  # video|image
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    codec: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # capture_time drives the Year/Month grouping and remote path templating.
    capture_time: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    thumbnail: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    # source: "device" (still on the camera) or "library" (imported/derived)
    source: Mapped[str] = mapped_column(String(16), default="device")
    derived: Mapped[bool] = mapped_column(Boolean, default=False)  # e.g. merged output
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    tags: Mapped[List["Tag"]] = relationship(
        secondary=media_tags, back_populates="media", lazy="selectin"
    )
    upload_states: Mapped[List["UploadState"]] = relationship(
        back_populates="media", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def year(self) -> Optional[int]:
        return self.capture_time.year if self.capture_time else None

    @property
    def month(self) -> Optional[int]:
        return self.capture_time.month if self.capture_time else None


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)

    media: Mapped[List[MediaItem]] = relationship(
        secondary=media_tags, back_populates="tags"
    )


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[List["AlbumItem"]] = relationship(
        back_populates="album",
        cascade="all, delete-orphan",
        order_by="AlbumItem.position",
        lazy="selectin",
    )


class AlbumItem(Base):
    """Ordered membership of a media item in an album (order matters for merge)."""

    __tablename__ = "album_items"
    __table_args__ = (UniqueConstraint("album_id", "media_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    album_id: Mapped[int] = mapped_column(ForeignKey("albums.id", ondelete="CASCADE"))
    media_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)

    album: Mapped[Album] = relationship(back_populates="items")
    media: Mapped[MediaItem] = relationship(lazy="selectin")


class Destination(Base):
    __tablename__ = "destinations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    type: Mapped[str] = mapped_column(String(32))  # local|nfs|smb|nextcloud|sftp|rsync
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Upload priority: lower rank = higher priority (clips go here first).
    rank: Mapped[int] = mapped_column(Integer, default=100)

    # Connection config. Secrets are stored encrypted (see crypto.py).
    host: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Base URL for nextcloud webdav, or remote/local base directory otherwise.
    base_path: Mapped[str] = mapped_column(String(1024), default="/mnt/NAS")
    # Template applied per upload, e.g. "{year}/{month:02d}".
    path_template: Mapped[str] = mapped_column(String(512), default="{year}/{month:02d}")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    auto_import_on_connect: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_upload_on_import: Mapped[bool] = mapped_column(Boolean, default=False)
    default_destination_ids: Mapped[str] = mapped_column(Text, default="")
    ha_base_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    ha_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ha_entity_prefix: Mapped[str] = mapped_column(String(128), default="drift_import")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class UploadState(Base):
    """Per-(media, destination) upload status, so a clip can go to many targets."""

    __tablename__ = "upload_states"
    __table_args__ = (UniqueConstraint("media_id", "destination_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"))
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|uploading|done|error
    remote_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bytes_uploaded: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    media: Mapped[MediaItem] = relationship(back_populates="upload_states")
    destination: Mapped[Destination] = relationship(lazy="selectin")


class UploadedClip(Base):
    """Deduplicated upload ledger keyed by destination + media checksum."""

    __tablename__ = "uploaded_clips"
    __table_args__ = (UniqueConstraint("destination_id", "checksum"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"), index=True
    )
    source_media_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True
    )
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    remote_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    temp_remote_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    bytes_uploaded: Mapped[int] = mapped_column(Integer, default=0)
    upload_duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    upload_throughput_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uploaded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    destination: Mapped[Destination] = relationship(lazy="selectin")
    media: Mapped[Optional[MediaItem]] = relationship(lazy="selectin")


class Job(Base):
    __tablename__ = "jobs"
    # The jobs list filters by status + dismissed and orders by created_at; these
    # indexes keep it fast as the table grows to thousands of rows.
    __table_args__ = (
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_dismissed", "dismissed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # import|thumbnail|merge|timestamp|upload
    description: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|paused|running|done|error|cancelled
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON args
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    # lazy="select": logs are only loaded when explicitly accessed (the jobs
    # list never touches them). Eager "selectin" loaded thousands of log rows on
    # every /api/jobs poll, which made the page crawl.
    logs: Mapped[List["JobLog"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobLog.created_at",
        lazy="select",
    )


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    progress: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)

    job: Mapped[Job] = relationship(back_populates="logs")


class SystemSample(Base):
    """A periodic snapshot of host CPU and network rates.

    A background sampler writes one row per tick so the stats page can show a
    real history window (e.g. the last 30 minutes) the instant it loads, instead
    of the browser having to accumulate samples live after each page load.
    """

    __tablename__ = "system_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    cpu_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rx_bytes_per_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tx_bytes_per_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    load_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
