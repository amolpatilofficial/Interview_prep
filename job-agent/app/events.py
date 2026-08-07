"""In-process pub/sub so the browser UI can watch runs live over SSE."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .db import db


class EventHub:
    def __init__(self, buffer: int = 500) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._recent: list[dict] = []
        self._buffer = buffer

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def replay(self) -> list[dict]:
        return list(self._recent)

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        event = {"kind": kind, **payload}
        self._recent.append(event)
        if len(self._recent) > self._buffer:
            del self._recent[: len(self._recent) - self._buffer]
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def log(
        self,
        message: str,
        *,
        level: str = "info",
        batch_id: str | None = None,
        application_id: str | None = None,
    ) -> None:
        rec = db.log_event(batch_id, application_id, level, message)
        self.publish(
            "log",
            {
                "level": level,
                "message": message,
                "batch_id": batch_id,
                "application_id": application_id,
                "at": rec["created_at"].isoformat(),
            },
        )

    def application_changed(self, app_id: str) -> None:
        self.publish("application", {"application_id": app_id})


hub = EventHub()


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
