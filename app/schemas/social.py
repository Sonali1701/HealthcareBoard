"""Connection and post/feed schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from ..models.enums import ConnectionStatus
from .common import ORMModel


class ConnectionCreate(BaseModel):
    recipient_profile_id: str


class ConnectionRespond(BaseModel):
    status: ConnectionStatus  # accepted | declined | blocked


class ConnectionOut(ORMModel):
    connection_id: str
    requester_profile_id: str
    recipient_profile_id: str
    status: ConnectionStatus
    requested_at: datetime
    accepted_at: Optional[datetime] = None


class PostCreate(BaseModel):
    body: str = Field(min_length=1, max_length=3000)
    media_urls: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class CommentOut(ORMModel):
    comment_id: str
    post_id: str
    author_profile_id: str
    body: str
    created_at: datetime


class PostOut(ORMModel):
    post_id: str
    author_profile_id: str
    author_name: Optional[str] = None
    author_headline: Optional[str] = None
    author_initials: Optional[str] = None
    body: str
    media_urls: list[str]
    tags: list[str]
    like_count: int
    comment_count: int
    created_at: datetime
