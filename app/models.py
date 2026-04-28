from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class VideoStatus(str, Enum):
    idea = "idea"
    drafted = "drafted"
    needs_review = "needs_review"
    approved = "approved"
    packaged = "packaged"
    publish_ready = "publish_ready"
    published = "published"
    rejected = "rejected"


class ContentType(str, Enum):
    long = "long"
    short = "short"


class AssetType(str, Enum):
    brief = "brief"
    script = "script"
    shorts = "shorts"
    description = "description"
    thumbnail_prompt = "thumbnail_prompt"
    youtube_metadata = "youtube_metadata"
    package_manifest = "package_manifest"


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    niche: Mapped[str] = mapped_column(String(240))
    audience: Mapped[str] = mapped_column(String(500))
    brand_voice: Mapped[str] = mapped_column(String(500))
    visual_style: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    videos: Mapped[list["Video"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    title: Mapped[str] = mapped_column(String(240), index=True)
    content_type: Mapped[ContentType] = mapped_column(SAEnum(ContentType), default=ContentType.long)
    pillar: Mapped[str] = mapped_column(String(120), default="AI call handling")
    target_viewer: Mapped[str] = mapped_column(String(240), default="Local business owner")
    pain_point: Mapped[str] = mapped_column(String(500), default="Missed calls and slow follow-up")
    demo_idea: Mapped[str] = mapped_column(String(500), default="Dashboard demo")
    thumbnail_text: Mapped[str] = mapped_column(String(80), default="AI BUSINESS SYSTEM")
    status: Mapped[VideoStatus] = mapped_column(SAEnum(VideoStatus), default=VideoStatus.idea, index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    channel: Mapped[Channel] = relationship(back_populates="videos")
    assets: Mapped[list["ContentAsset"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    reviews: Mapped[list["Review"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    publish_records: Mapped[list["PublishRecord"]] = relationship(back_populates="video", cascade="all, delete-orphan")


class ContentAsset(Base):
    __tablename__ = "content_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    asset_type: Mapped[AssetType] = mapped_column(SAEnum(AssetType), index=True)
    body: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="assets")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer: Mapped[str] = mapped_column(String(120), default="operator")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="reviews")


class PublishRecord(Base):
    __tablename__ = "publish_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    platform: Mapped[str] = mapped_column(String(60), default="youtube")
    external_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    metadata_body: Mapped[str] = mapped_column(Text)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="publish_records")
