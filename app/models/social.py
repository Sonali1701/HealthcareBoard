"""Domain 4 — Social & Community: connections, posts, post_likes, post_comments."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Enum, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, TZDateTime, created_col, uuid_fk, uuid_pk
from .enums import ConnectionStatus


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint(
            "requester_profile_id", "recipient_profile_id", name="uq_connection_pair"
        ),
    )

    connection_id: Mapped[str] = uuid_pk()
    requester_profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    recipient_profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(ConnectionStatus), default=ConnectionStatus.pending, index=True
    )
    requested_at: Mapped[datetime] = created_col()
    accepted_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)


class Post(Base):
    __tablename__ = "posts"

    post_id: Mapped[str] = uuid_pk()
    author_profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media_urls: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_col()

    likes: Mapped[list["PostLike"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    comments: Mapped[list["PostComment"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class PostLike(Base):
    __tablename__ = "post_likes"
    __table_args__ = (
        UniqueConstraint("post_id", "profile_id", name="uq_post_like"),
    )

    like_id: Mapped[str] = uuid_pk()
    post_id: Mapped[str] = uuid_fk("posts.post_id")
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    created_at: Mapped[datetime] = created_col()

    post: Mapped[Post] = relationship(back_populates="likes")


class PostComment(Base):
    __tablename__ = "post_comments"

    comment_id: Mapped[str] = uuid_pk()
    post_id: Mapped[str] = uuid_fk("posts.post_id")
    author_profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_col()

    post: Mapped[Post] = relationship(back_populates="comments")
