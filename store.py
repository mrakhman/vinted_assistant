"""Tiny JSON store for the items each seller has listed.

A plain file rather than a database: a few dozen items per person, and she can
open it, fix a title, or delete a row by hand. The file is the whole schema — no
migrations.

Every row carries the `chat_id` it belongs to, and every read and write is scoped
to one chat. Two people using the same bot each have their own private chat with
it, so they get their own item lists and never see each other's.

Concurrency: these functions are synchronous and contain no `await`, so on the
bot's single event loop a read-modify-write pair cannot interleave with another
handler's. Writes are atomic (temp file + rename). If you ever move a store call
into a thread, add a lock first.
"""

import json
import os
import secrets
from pathlib import Path
from typing import Optional, TypedDict

ITEMS_PATH = Path(os.environ.get("ITEMS_PATH", "items.json"))


class Item(TypedDict):
    id: str
    chat_id: int
    title: str
    description_fr: str
    description_en: str
    facts: str  # her own caption: brand, size, condition, material, flaws
    status: str  # "active" | "sold"


def _load() -> list[Item]:
    if not ITEMS_PATH.exists():
        return []
    try:
        return json.loads(ITEMS_PATH.read_text())
    except json.JSONDecodeError:
        # Don't lose the file to a bad write; keep it and start clean.
        ITEMS_PATH.rename(ITEMS_PATH.with_suffix(".json.corrupt"))
        return []


def _save(items: list[Item]) -> None:
    tmp = ITEMS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    tmp.replace(ITEMS_PATH)


def _owned(item: Item, chat_id: int) -> bool:
    return item.get("chat_id") == chat_id


def add(
    chat_id: int,
    title: str,
    description_fr: str,
    description_en: str,
    facts: str = "",
) -> Item:
    items = _load()
    item: Item = {
        "id": secrets.token_hex(2),
        "chat_id": chat_id,
        "title": title,
        "description_fr": description_fr,
        "description_en": description_en,
        "facts": facts,
        "status": "active",
    }
    items.append(item)
    _save(items)
    return item


def active(chat_id: int) -> list[Item]:
    """This chat's unsold items, newest first."""
    return [
        i
        for i in reversed(_load())
        if _owned(i, chat_id) and i["status"] == "active"
    ]


def get(chat_id: int, item_id: str) -> Optional[Item]:
    """Scoped on purpose: a callback payload can't reach another chat's item."""
    return next(
        (i for i in _load() if i["id"] == item_id and _owned(i, chat_id)), None
    )


def mark_sold(chat_id: int, item_id: str) -> Optional[Item]:
    items = _load()
    for i in items:
        if i["id"] == item_id and _owned(i, chat_id):
            i["status"] = "sold"
            _save(items)
            return i
    return None


def as_context(item: Item) -> str:
    """What the bot knows about this item, for answering a buyer."""
    parts = [
        f"Listing in question — {item['title']}",
        item["description_fr"],
        item["description_en"],
    ]
    # Her own notes often answer a buyer's question that the prose doesn't.
    if item.get("facts"):
        parts.append(f"Facts the seller gave: {item['facts']}")
    return "\n".join(parts)
