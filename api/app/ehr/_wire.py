"""Wire helpers shared by adapters that speak JSON over HTTP.

Numbers keep their source token, so a payload re-serialized here hashes the same as the
bytes the source sent. Nothing here puts payload content into an exception message.
"""

import json
import math
from typing import Any

import httpx

from app.ehr.ports import PermanentSourceError

MAX_RETRY_AFTER_SECONDS = 300.0
MAX_RESPONSE_BYTES = 24 * 1024 * 1024


class Number(str):
    """A JSON number kept as the text the source sent."""

    __slots__ = ()


def parse_json_object(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(content, parse_float=Number, parse_int=Number)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise PermanentSourceError("the source returned a response that is not JSON") from None
    if not isinstance(document, dict):
        raise PermanentSourceError("the source returned an unexpected response")
    return document


def dump_compact(value: Any) -> str:
    """Compact JSON that keeps key order and every number token as received."""
    match value:
        case dict():
            members = (
                f"{json.dumps(key, ensure_ascii=False)}:{dump_compact(item)}"
                for key, item in value.items()
            )
            return "{" + ",".join(members) + "}"
        case list():
            return "[" + ",".join(dump_compact(item) for item in value) + "]"
        case Number():
            return str(value)
        case str() | bool() | None:
            return json.dumps(value, ensure_ascii=False)
    raise PermanentSourceError(f"unexpected JSON value of type {type(value).__name__}")


async def read_capped(
    response: httpx.Response, what: str, limit: int = MAX_RESPONSE_BYTES
) -> bytes:
    declared = response.headers.get("Content-Length", "")
    if declared.isdecimal() and int(declared) > limit:
        raise PermanentSourceError(f"{what}: the response is larger than this adapter will read")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise PermanentSourceError(
                f"{what}: the response is larger than this adapter will read"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def retry_after(response: httpx.Response) -> float | None:
    """The server's retry hint in seconds, kept finite and bounded so a caller can honor it."""
    try:
        seconds = float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return min(max(0.0, seconds), MAX_RETRY_AFTER_SECONDS)
