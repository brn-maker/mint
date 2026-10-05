import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from config import RATE_LIMIT_PER_MINUTE


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        async with self._lock:
            bucket = self._events[key]
            while bucket and now - bucket[0] > self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again shortly.")
            bucket.append(now)


api_rate_limiter = SlidingWindowRateLimiter(RATE_LIMIT_PER_MINUTE)


async def rate_limit_sensitive(request: Request, user_id: int) -> None:
    client = request.client.host if request.client else "unknown"
    await api_rate_limiter.check(f"{user_id}:{client}")
