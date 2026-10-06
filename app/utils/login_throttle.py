"""In-memory throttling for credential-guessing endpoints.

Counts failures per key (client IP, and IP+username for logins) inside a
sliding window and refuses further attempts once the limit is reached. The
state is per process; with several workers each worker enforces the limit on
its own, which still bounds the total rate.
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, status

LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 300
SETUP_MAX_FAILURES = 10
SETUP_WINDOW_SECONDS = 600


class FailureThrottle:
    def __init__(self, max_failures: int, window_seconds: int):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._failures[key]
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._failures.pop(key, None)
        return attempts

    def retry_after(self, key: str) -> int:
        """Seconds until another attempt is allowed, 0 when not throttled."""
        now = time.monotonic()
        with self._lock:
            attempts = self._prune(key, now)
            if len(attempts) < self.max_failures:
                return 0
            return max(1, int(attempts[0] + self.window_seconds - now) + 1)

    def check(self, *keys: str) -> None:
        """Raise 429 if any key is currently throttled."""
        wait = max(self.retry_after(key) for key in keys)
        if wait:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts, try again later",
                headers={"Retry-After": str(wait)},
            )

    def record_failure(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                self._prune(key, now)
                self._failures[key].append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)

    def clear(self) -> None:
        """Forget every recorded failure (used by tests)."""
        with self._lock:
            self._failures.clear()


login_throttle = FailureThrottle(LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS)
setup_throttle = FailureThrottle(SETUP_MAX_FAILURES, SETUP_WINDOW_SECONDS)
