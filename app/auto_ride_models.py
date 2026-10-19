"""Rebuildable automatic associations; raw GPS stays on the NAS."""
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import utcnow


class AutoRide(Base):
    __tablename__ = "auto_rides"
    id: Mapped[int] = mapped_column(primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    input_signature: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(256))
    outing_id: Mapped[int] = mapped_column(ForeignKey("roadtrip_outings.id"))
    name: Mapped[str] = mapped_column(String(256))
    start_time: Mapped[object] = mapped_column(DateTime)
    end_time: Mapped[object] = mapped_column(DateTime)
    clip_count: Mapped[int] = mapped_column(Integer)
    duration_s: Mapped[float] = mapped_column(Float)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_label: Mapped[str] = mapped_column(String(64), default="High confidence")
    status: Mapped[str] = mapped_column(String(16), default="ready")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class AutoRideClip(Base):
    __tablename__ = "auto_ride_clips"
    __table_args__ = (UniqueConstraint("ride_id", "clip_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("auto_rides.id", ondelete="CASCADE"), index=True)
    clip_id: Mapped[str] = mapped_column(String(64))
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("archive_files.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class AutoRideDiscovery(Base):
    __tablename__ = "auto_ride_discovery"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    input_signature: Mapped[str] = mapped_column(String(64), default="")
    report: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class AutoRideHARefresh(Base):
    """Check bookkeeping is separate from actual private GPS revisions."""
    __tablename__ = "auto_ride_ha_refresh"
    source: Mapped[str] = mapped_column(String(256), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    checked_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    latest_clip_end: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str] = mapped_column(String(32), default="empty")
