#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rate_limit.py — Quản lý ngân sách request tập trung (token bucket).

VÌ SAO CẦN
    GeckoTerminal free cho 30 call/phút. Bản gốc giãn cách cứng 2.1s giữa các
    call — an toàn nhưng LÃNG PHÍ: nó không cho phép "bắn dồn" vài call khi
    cần (ví dụ cập nhật giá 40 vị thế) rồi nghỉ bù sau đó.

    Token bucket cho phép đúng điều đó: tích luỹ lượt khi rảnh, tiêu nhanh khi
    cần, miễn là trung bình không vượt trần. Khi chạy 24/7 với 2 nhịp quét,
    đây là khác biệt giữa "theo dõi vị thế mỗi 45s" và "bị 429 liên tục".

CƠ CHẾ
    - Bucket chứa `capacity` token, tự đầy lại `refill_per_sec` token/giây.
    - Mỗi request tiêu 1 token. Hết token -> chờ đúng thời gian cần thiết.
    - Gặp 429 -> `penalize()` rút cạn bucket + cấm bắn trong `cooldown` giây.
      Đây là phanh khẩn cấp: server đã nói "chậm lại", ta tin nó hơn là tin
      phép tính cục bộ của mình.

AN TOÀN LUỒNG
    Có khoá, dùng được từ nhiều thread. Runner hiện chạy 1 thread nhưng để sẵn.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class BucketStats:
    acquired: int = 0            # số lượt lấy được token
    waited_seconds: float = 0.0
    throttle_events: int = 0     # số lần phải chờ
    penalties: int = 0           # số lần bị 429


class TokenBucket:
    """Giới hạn tốc độ kiểu token bucket, có phanh khẩn cấp khi gặp 429."""

    def __init__(self, capacity: float, refill_per_sec: float,
                 name: str = "bucket", start_full: bool = True):
        if capacity <= 0 or refill_per_sec <= 0:
            raise ValueError("capacity và refill_per_sec phải > 0")
        self.name = name
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._tokens = float(capacity) if start_full else 0.0
        self._last = time.monotonic()
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self.stats = BucketStats()

    def _refill_locked(self):
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity,
                               self._tokens + elapsed * self.refill_per_sec)
            self._last = now

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Chờ tới khi đủ token. Trả False nếu quá `timeout` giây.
        timeout=None -> chờ bao lâu cũng được."""
        if tokens > self.capacity:
            raise ValueError(f"xin {tokens} token > sức chứa {self.capacity}")
        deadline = None if timeout is None else time.monotonic() + timeout
        waited_total = 0.0

        while True:
            with self._lock:
                self._refill_locked()
                now = time.monotonic()
                cooldown = max(0.0, self._blocked_until - now)
                if cooldown <= 0 and self._tokens >= tokens:
                    self._tokens -= tokens
                    self.stats.acquired += 1
                    if waited_total > 0:
                        self.stats.throttle_events += 1
                        self.stats.waited_seconds += waited_total
                    return True
                need = tokens - self._tokens
                wait = max(cooldown, need / self.refill_per_sec if need > 0 else 0.0)
                wait = max(wait, 0.01)

            if deadline is not None:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return False
                wait = min(wait, remain)

            time.sleep(wait)
            waited_total += wait

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Lấy token nếu có sẵn, không chờ."""
        with self._lock:
            self._refill_locked()
            if time.monotonic() < self._blocked_until or self._tokens < tokens:
                return False
            self._tokens -= tokens
            self.stats.acquired += 1
            return True

    def penalize(self, cooldown_seconds: float = 5.0):
        """Gọi khi nhận 429: rút cạn bucket và nghỉ `cooldown_seconds`."""
        with self._lock:
            self._tokens = 0.0
            self._last = time.monotonic()
            self._blocked_until = max(self._blocked_until,
                                      time.monotonic() + max(cooldown_seconds, 0.0))
            self.stats.penalties += 1

    @property
    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return round(self._tokens, 2)

    def summary(self) -> str:
        s = self.stats
        return (f"{self.name}: {s.acquired} call · chờ {s.waited_seconds:.0f}s "
                f"({s.throttle_events} lần) · 429 x{s.penalties} · "
                f"còn {self.available:.1f}/{self.capacity:.0f}")


class RateLimiterRegistry:
    """Gom bucket theo host để mọi phần của bot dùng chung ngân sách.

    Điểm mấu chốt: scanner và paper trader phải chia nhau CÙNG một quota
    GeckoTerminal, nếu không mỗi bên tự cho rằng mình còn 30 call/phút.
    """

    # Ngưỡng mặc định, chừa biên an toàn so với trần công bố.
    DEFAULTS = {
        # GeckoTerminal free: 30 call/phút -> dùng 25 để chừa biên
        "api.geckoterminal.com": (25.0, 25.0 / 60.0),
        # GoPlus free: ~30 call/phút
        "api.gopluslabs.io": (25.0, 25.0 / 60.0),
        # Telegram: giới hạn thực tế ~1 msg/giây cho mỗi chat
        "api.telegram.org": (20.0, 1.0),
    }
    FALLBACK = (10.0, 10.0 / 60.0)

    def __init__(self, overrides: dict | None = None):
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._config = dict(self.DEFAULTS)
        if overrides:
            self._config.update(overrides)

    def bucket(self, host: str) -> TokenBucket:
        host = (host or "").lower()
        with self._lock:
            b = self._buckets.get(host)
            if b is None:
                cap, refill = self._config.get(host, self.FALLBACK)
                b = TokenBucket(cap, refill, name=host)
                self._buckets[host] = b
            return b

    def acquire(self, host: str, tokens: float = 1.0,
                timeout: float | None = None) -> bool:
        return self.bucket(host).acquire(tokens, timeout)

    def penalize(self, host: str, cooldown_seconds: float = 5.0):
        self.bucket(host).penalize(cooldown_seconds)

    def report(self) -> str:
        with self._lock:
            buckets = list(self._buckets.values())
        return " | ".join(b.summary() for b in buckets) if buckets else "chưa có call nào"

    def budget_left(self, host: str) -> float:
        return self.bucket(host).available


# Registry dùng chung toàn tiến trình.
GLOBAL = RateLimiterRegistry()


if __name__ == "__main__":
    b = TokenBucket(capacity=5, refill_per_sec=1.0, name="demo")
    t0 = time.monotonic()
    for i in range(8):
        b.acquire()
        print(f"  call {i+1} tại {time.monotonic()-t0:5.2f}s (còn {b.available:.1f})")
    print(b.summary())
