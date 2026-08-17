"""Public shared chat view — no auth required."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from partb.config import MONGO_DB
from partb.db import get_mongo
from partb.logger import time_it
from partb.services.messages import get_all_messages

router = APIRouter(prefix="/api/shared", tags=["shared"])


@time_it
def chats_col():
    return get_mongo()[MONGO_DB]["chats"]


@time_it
def library_col():
    return get_mongo()[MONGO_DB]["library"]


@router.get("/{share_token}")
@time_it
def get_shared_chat(share_token: str):
    chat = chats_col().find_one(
        {"share_token": share_token}, {"_id": 0, "chat_id": 1, "title": 1, "book_ids": 1, "shared_message_ids": 1}
    )
    if not chat:
        raise HTTPException(404, "Shared chat not found or link has been revoked.")

    book_ids = chat.get("book_ids", [])
    books_map = {}
    if book_ids:
        for b in library_col().find({"book_id": {"$in": book_ids}}, {"_id": 0, "book_id": 1, "book_title": 1}):
            books_map[b["book_id"]] = b.get("book_title", b["book_id"])

    messages = get_all_messages(chat["chat_id"])
    shared_ids = chat.get("shared_message_ids")
    if shared_ids:
        shared_set = set(shared_ids)
        idxs = {i for i, m in enumerate(messages) if m.get("message_id") in shared_set}
        for i in sorted(idxs):
            if i > 0 and messages[i - 1]["role"] == "user":
                idxs.add(i - 1)
        messages = [messages[i] for i in sorted(idxs)]

    return {
        "chat_id": chat["chat_id"],
        "title": chat.get("title", "Shared Chat"),
        "book_ids": book_ids,
        "books": books_map,
        "messages": [
            {
                "role": m["role"],
                "content": m["content"],
                "sources": m.get("sources", []),
            }
            for m in messages
        ],
    }
