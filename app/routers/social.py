"""Social/community: connections (network graph) and posts/feed."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, or_, select

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    Connection,
    Notification,
    Post,
    PostComment,
    PostLike,
    Profile,
)
from ..models.enums import ConnectionStatus, NotificationType
from ..schemas.common import Page
from ..schemas.social import (
    CommentCreate,
    CommentOut,
    ConnectionCreate,
    ConnectionOut,
    ConnectionRespond,
    PostCreate,
    PostOut,
)

router = APIRouter(prefix="/api/social", tags=["social"])


def _my_profile(db: DbSession, user: CurrentUser) -> Profile:
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        raise HTTPException(status_code=400, detail="No profile for this account")
    return profile


# --- Connections ----------------------------------------------------------

@router.post("/connections", response_model=ConnectionOut, status_code=201)
def request_connection(body: ConnectionCreate, user: CurrentUser, db: DbSession):
    me = _my_profile(db, user)
    if body.recipient_profile_id == me.profile_id:
        raise HTTPException(status_code=400, detail="Cannot connect to yourself")
    recipient = db.get(Profile, body.recipient_profile_id)
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient profile not found")

    existing = db.scalar(
        select(Connection).where(
            or_(
                and_(Connection.requester_profile_id == me.profile_id,
                     Connection.recipient_profile_id == body.recipient_profile_id),
                and_(Connection.requester_profile_id == body.recipient_profile_id,
                     Connection.recipient_profile_id == me.profile_id),
            )
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="Connection already exists")

    conn = Connection(requester_profile_id=me.profile_id,
                      recipient_profile_id=body.recipient_profile_id)
    db.add(conn)
    if recipient.user_id:
        db.add(Notification(
            user_id=recipient.user_id,
            type=NotificationType.connection,
            title="New connection request",
            body=f"{me.first_name} {me.last_name} wants to connect",
            data={"connection_id": conn.connection_id},
        ))
    db.commit()
    db.refresh(conn)
    return conn


@router.get("/connections", response_model=list[ConnectionOut])
def list_connections(user: CurrentUser, db: DbSession,
                     status_filter: Optional[ConnectionStatus] = Query(None, alias="status")):
    me = _my_profile(db, user)
    stmt = select(Connection).where(
        or_(Connection.requester_profile_id == me.profile_id,
            Connection.recipient_profile_id == me.profile_id)
    )
    if status_filter:
        stmt = stmt.where(Connection.status == status_filter)
    return db.scalars(stmt).all()


@router.post("/connections/{connection_id}/respond", response_model=ConnectionOut)
def respond_connection(connection_id: str, body: ConnectionRespond,
                       user: CurrentUser, db: DbSession):
    me = _my_profile(db, user)
    conn = db.get(Connection, connection_id)
    if not conn or conn.recipient_profile_id != me.profile_id:
        raise HTTPException(status_code=404, detail="Connection request not found")
    conn.status = body.status
    if body.status == ConnectionStatus.accepted:
        conn.accepted_at = utcnow()
    db.commit()
    db.refresh(conn)
    return conn


# --- Posts / feed ---------------------------------------------------------

@router.get("/posts", response_model=Page[PostOut])
def feed(db: DbSession, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    from sqlalchemy import func
    total = db.scalar(select(func.count()).select_from(Post)) or 0
    rows = db.scalars(
        select(Post).order_by(Post.created_at.desc()).limit(limit).offset(offset)
    ).all()
    authors = {p.profile_id: p for p in db.scalars(
        select(Profile).where(Profile.profile_id.in_([r.author_profile_id for r in rows]))
    )} if rows else {}
    items = []
    for r in rows:
        out = PostOut.model_validate(r)
        a = authors.get(r.author_profile_id)
        if a:
            out.author_name = f"{a.first_name} {a.last_name}"
            out.author_headline = a.headline or a.specialty or a.profession_type
            out.author_initials = (a.first_name[:1] + a.last_name[:1]).upper()
        items.append(out)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("/posts", response_model=PostOut, status_code=201)
def create_post(body: PostCreate, user: CurrentUser, db: DbSession):
    me = _my_profile(db, user)
    post = Post(author_profile_id=me.profile_id, **body.model_dump())
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: str, user: CurrentUser, db: DbSession):
    me = _my_profile(db, user)
    post = db.get(Post, post_id)
    if post and (post.author_profile_id == me.profile_id or user.role.value == "admin"):
        db.delete(post)
        db.commit()


@router.post("/posts/{post_id}/like", response_model=PostOut)
def like_post(post_id: str, user: CurrentUser, db: DbSession):
    me = _my_profile(db, user)
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    existing = db.scalar(
        select(PostLike).where(and_(PostLike.post_id == post_id,
                                    PostLike.profile_id == me.profile_id))
    )
    if not existing:
        db.add(PostLike(post_id=post_id, profile_id=me.profile_id))
        post.like_count += 1
        db.commit()
        db.refresh(post)
    return post


@router.delete("/posts/{post_id}/like", response_model=PostOut)
def unlike_post(post_id: str, user: CurrentUser, db: DbSession):
    me = _my_profile(db, user)
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    existing = db.scalar(
        select(PostLike).where(and_(PostLike.post_id == post_id,
                                    PostLike.profile_id == me.profile_id))
    )
    if existing:
        db.delete(existing)
        post.like_count = max(0, post.like_count - 1)
        db.commit()
        db.refresh(post)
    return post


@router.post("/posts/{post_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(post_id: str, body: CommentCreate, user: CurrentUser, db: DbSession):
    me = _my_profile(db, user)
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    comment = PostComment(post_id=post_id, author_profile_id=me.profile_id, body=body.body)
    db.add(comment)
    post.comment_count += 1
    db.commit()
    db.refresh(comment)
    return comment


@router.get("/posts/{post_id}/comments", response_model=list[CommentOut])
def list_comments(post_id: str, db: DbSession):
    return db.scalars(
        select(PostComment).where(PostComment.post_id == post_id)
        .order_by(PostComment.created_at.asc())
    ).all()
