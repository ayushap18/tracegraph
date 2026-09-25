import asyncio
import json


class Broadcaster:
    """Fans every event out to each SSE subscriber's queue. A subscriber that falls `limit` events behind is cut off
    (None ends its stream) rather than silently missing events; EventSource reconnects and resyncs from `hello`."""

    def __init__(self, limit: int = 500):
        self.subscribers: set[asyncio.Queue] = set()
        self.limit = limit
        self.taps = []  # sync callables that see every event (tests, logging)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subscribers.discard(q)

    def emit(self, type_: str, **fields) -> dict:
        event = {'type': type_, **fields}
        for tap in self.taps:
            tap(event)
        msg = json.dumps(event)
        for q in list(self.subscribers):
            if q.qsize() < self.limit:
                q.put_nowait(msg)
            else:
                self.subscribers.discard(q)
                q.put_nowait(None)
        return event


def sse(data: dict | str) -> bytes:
    return f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n".encode()
