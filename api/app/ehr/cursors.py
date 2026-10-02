"""Opaque offset cursors, shared by adapters that page over a full result in memory."""

import base64
import binascii

from app.ehr.ports import PermanentSourceError


def encode_offset_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode("ascii")


def decode_offset_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        label, _, offset_text = base64.urlsafe_b64decode(cursor).decode("ascii").partition(":")
        offset = int(offset_text)
        if label != "offset" or offset < 0:
            raise ValueError("not an offset cursor")
        return offset
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise PermanentSourceError("cursor is not one this source issued") from error
