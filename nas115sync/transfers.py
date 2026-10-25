"""傳輸佇列：每個上傳/下載都有進度、速度、狀態，可暫停／繼續／取消，並可限制上傳與下載速度。"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


class Cancelled(Exception):
    """使用者取消（整體取消或單一傳輸取消）。"""


class RateLimiter:
    """令牌桶限速；rate_kb <= 0 表示不限速。可在傳輸中即時調整。"""

    def __init__(self, rate_kb: float = 0):
        self._lock = threading.Lock()
        self.set_rate(rate_kb)

    def set_rate(self, rate_kb: float) -> None:
        with self._lock:
            self.rate = max(0.0, float(rate_kb or 0)) * 1024
            self._tokens = self.rate  # 最多累積 1 秒額度
            self._last = time.monotonic()

    def consume(self, n: int, check: Callable[[], None] | None = None) -> None:
        """取得 n bytes 的額度；額度不足時分段等待（每段最多 0.5 秒，期間可被取消）。"""
        remaining = float(n)
        while remaining > 0:
            with self._lock:
                if self.rate <= 0:
                    return
                now = time.monotonic()
                self._tokens = min(self.rate, self._tokens + (now - self._last) * self.rate)
                self._last = now
                take = min(remaining, max(0.0, self._tokens))
                self._tokens -= take
                remaining -= take
                if remaining <= 0:
                    return
                wait = min(0.5, remaining / self.rate)
            if check:
                check()
            time.sleep(wait)


@dataclass
class Transfer:
    id: int
    kind: str  # upload | download | copy
    mapping_id: str
    rel_path: str
    size: int
    done: int = 0
    status: str = "queued"  # queued | running | paused | done | failed | cancelled | skipped
    instant: bool = False
    speed: float = 0.0  # bytes/s
    error: str = ""
    started: float = 0.0
    finished: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def snapshot(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "kind", "mapping_id", "rel_path", "size", "done", "status",
                                               "instant", "speed", "error", "started", "finished")}


class TransferManager:
    def __init__(self, upload_kb: float = 0, download_kb: float = 0, keep_finished: int = 500):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._items: dict[int, Transfer] = {}
        self.keep_finished = keep_finished
        self.paused = threading.Event()
        self.up_limiter = RateLimiter(upload_kb)
        self.down_limiter = RateLimiter(download_kb)
        self.listeners: list[Callable[[dict], None]] = []

    def start(self, kind: str, mapping_id: str, rel_path: str, size: int,
              global_cancel: threading.Event | None = None) -> "TransferControl":
        t = Transfer(next(self._ids), kind, mapping_id, rel_path, size, status="running", started=time.time())
        with self._lock:
            self._items[t.id] = t
            self._trim()
        self._notify(t)
        return TransferControl(self, t, global_cancel)

    def _trim(self) -> None:
        finished = [i for i, t in self._items.items() if t.status not in ("queued", "running", "paused")]
        for i in finished[: max(0, len(finished) - self.keep_finished)]:
            del self._items[i]

    def _notify(self, t: Transfer) -> None:
        snap = t.snapshot()
        for fn in list(self.listeners):
            try:
                fn(snap)
            except Exception:  # noqa: BLE001 - 介面錯誤不影響傳輸
                pass

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [t.snapshot() for t in self._items.values()]

    def active_count(self) -> int:
        with self._lock:
            return sum(t.status in ("running", "paused") for t in self._items.values())

    def pause_all(self) -> None:
        self.paused.set()

    def resume_all(self) -> None:
        self.paused.clear()

    def cancel(self, transfer_id: int) -> None:
        with self._lock:
            t = self._items.get(transfer_id)
        if t:
            t.cancel_event.set()

    def cancel_all(self) -> None:
        with self._lock:
            items = list(self._items.values())
        for t in items:
            t.cancel_event.set()

    def clear_finished(self) -> None:
        with self._lock:
            for i in [i for i, t in self._items.items() if t.status not in ("queued", "running", "paused")]:
                del self._items[i]

    def set_limits(self, upload_kb: float, download_kb: float) -> None:
        self.up_limiter.set_rate(upload_kb)
        self.down_limiter.set_rate(download_kb)


class TransferControl:
    """交給上傳/下載程式碼使用：回報進度、限速、檢查暫停與取消。"""

    def __init__(self, mgr: TransferManager, t: Transfer, global_cancel: threading.Event | None):
        self.mgr = mgr
        self.t = t
        self.global_cancel = global_cancel
        self._last_notify = 0.0
        self._speed_mark = (time.monotonic(), 0)

    @property
    def limiter(self) -> RateLimiter:
        return self.mgr.down_limiter if self.t.kind == "download" else self.mgr.up_limiter

    def check(self) -> None:
        """取消時拋出 Cancelled；全部暫停時在此等待。"""
        while True:
            if self.t.cancel_event.is_set() or (self.global_cancel is not None and self.global_cancel.is_set()):
                raise Cancelled("已取消")
            if not self.mgr.paused.is_set():
                if self.t.status == "paused":
                    self.t.status = "running"
                    self.mgr._notify(self.t)
                return
            if self.t.status != "paused":
                self.t.status = "paused"
                self.mgr._notify(self.t)
            time.sleep(0.2)

    def advance(self, n: int) -> None:
        self.check()
        if n > 0:
            self.limiter.consume(n, self.check)
        self.set_done(self.t.done + n)

    def set_done(self, done: int) -> None:
        self.t.done = max(0, done)
        now = time.monotonic()
        t0, d0 = self._speed_mark
        if now - t0 >= 1.0:
            self.t.speed = max(0.0, (self.t.done - d0) / (now - t0))
            self._speed_mark = (now, self.t.done)
        if now - self._last_notify >= 0.3 or self.t.done >= self.t.size:
            self._last_notify = now
            self.mgr._notify(self.t)

    def progress_callback(self, base: int = 0) -> Callable[[int, int | None], None]:
        """給 oss2 用的進度回呼：在送出資料的同時限速並檢查取消/暫停。"""
        state = {"last": 0}

        def cb(consumed: int, total: int | None) -> None:
            delta = consumed - state["last"]
            state["last"] = consumed
            if delta > 0:
                self.advance(delta)

        self.set_done(base)
        return cb

    def finish(self, status: str = "done", error: str = "", instant: bool = False) -> None:
        self.t.status = status
        self.t.error = error
        self.t.instant = instant
        self.t.finished = time.time()
        if status == "done":
            self.t.done = self.t.size
        self.mgr._notify(self.t)
