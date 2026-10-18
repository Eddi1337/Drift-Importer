"""Cached, rebuildable NAS inventory; the upload ledger remains authoritative."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import utcnow


class ArchiveFile(Base):
    __tablename__ = "archive_files"
    __table_args__ = (
        UniqueConstraint("destination_id", "path"),
        Index("ix_archive_present_size", "present", "size_bytes"),
        Index("ix_archive_sample", "sample_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    destination_id: Mapped[int] = mapped_column(ForeignKey("destinations.id", ondelete="CASCADE"), index=True)
    root_path: Mapped[str] = mapped_column(String(1024))
    path: Mapped[str] = mapped_column(String(2048))
    kind: Mapped[str] = mapped_column(String(16), default="video")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mtime_ns: Mapped[int] = mapped_column(Integer, default=0)
    ctime_ns: Mapped[int] = mapped_column(Integer, default=0)
    # Some NFS servers expose unsigned 64-bit inode numbers (SQLite is signed).
    device: Mapped[str] = mapped_column(String(32), default="0")
    inode: Mapped[str] = mapped_column(String(32), default="0")
    nlink: Mapped[int] = mapped_column(Integer, default=1)
    mode: Mapped[int] = mapped_column(Integer, default=0)
    uid: Mapped[int] = mapped_column(Integer, default=0)
    gid: Mapped[int] = mapped_column(Integer, default=0)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    ledger_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ledger_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sample_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    checked_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[object] = mapped_column(DateTime, default=utcnow)

    @property
    def absolute_path(self) -> Path:
        return Path(self.root_path) / self.path


class ArchiveScan(Base):
    __tablename__ = "archive_scans"
    destination_id: Mapped[int] = mapped_column(ForeignKey("destinations.id", ondelete="CASCADE"), primary_key=True)
    root_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    started_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
