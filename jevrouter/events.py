import asyncio
import json
from typing import Callable

Filter = Callable[[dict], bool]


class Broadcaster:
    """Fans every event out to each SSE subscriber's queue. A subscriber that falls `limit` events behind is cut off
    (None ends its stream) rather than silently missing events; EventSource reconnects and resyncs from `hello`.
    A subscriber may pass a filter: it then only receives the events the filter accepts (sandbox streams use this)."""

    def __init__(self, limit: int = 500):
        self.subscribers: dict[asyncio.Queue, Filter | None] = {}
        self.limit = limit
        self.taps = []  # sync callables that see every event (tests, logging)

    def subscribe(self, accept: Filter | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers[q] = accept
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subscribers.pop(q, None)

    def emit(self, type_: str, **fields) -> dict:
        event = {'type': type_, **fields}
        for tap in self.taps:
            tap(event)
        msg = json.dumps(event)
        for q, accept in list(self.subscribers.items()):
            if accept is not None and not accept(event):
                continue
            if q.qsize() < self.limit:
                q.put_nowait(msg)
            else:
                self.subscribers.pop(q, None)
                q.put_nowait(None)
        return event


def sse(data: dict | str) -> bytes:
    return f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n".encode()
