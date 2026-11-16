"""Cursor pagination over UUIDv7 ids (newest first)."""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Sequence
from typing import TypeVar

from fastapi import HTTPException

T = TypeVar("T")


def encode_cursor(last_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(last_id.bytes).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> uuid.UUID | None:
    if not cursor:
        return None
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, binascii.Error) as e:
        raise HTTPException(400, "Invalid cursor") from e


def page(rows: Sequence[T], limit: int) -> tuple[list[T], bool]:
    """Split a limit+1 query result into the page and whether more follow."""
    items = list(rows[:limit])
    return items, len(rows) > limit
