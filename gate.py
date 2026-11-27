"""agent 并发闸门：全局信号量（可动态调整上限）。

语义（v3 设计）：
- 每次 LLM/agent 调用（opencode2 或直连 API）acquire，完成后 release；
- issue 可交错并行推进，但超出上限的 agent 调用排队等待（"agent 要排队"）；
- 上限来自配置（agent_limit，默认 3，范围 1-99）；
- 支持动态调整（set_limit），空位按登记顺序交给等待者；
- 配额按"每次调用"粒度占用/释放（三路审查各占各放，一个结束即释放一个）。

统计快照供 UI 显示 running / queued。
"""
from collections import deque
from contextlib import contextmanager
import threading
import time

DEFAULT_LIMIT = 3
MAX_LIMIT = 99


class QueueTimeout(TimeoutError):
    pass


class AgentGate:
    def __init__(self, limit: int = DEFAULT_LIMIT):
        self._lock = threading.RLock()
        self._queue = deque()
        self._limit = self._clamp(limit)
        self._active = 0
        self._total = 0

    @staticmethod
    def _clamp(n) -> int:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = DEFAULT_LIMIT
        return max(1, min(n, MAX_LIMIT))

    def _admit(self) -> bool:
        self._active += 1
        self._total += 1
        return True

    def _wake_head(self) -> None:
        if self._queue and self._active < self._limit:
            self._queue[0].notify()

    def acquire(self, timeout: float = None) -> bool:
        with self._lock:
            if not self._queue and self._active < self._limit:
                return self._admit()
            if timeout is not None and timeout <= 0:
                return False

            deadline = None if timeout is None else time.monotonic() + timeout
            # Each waiter has its own condition on the same lock. A release
            # can signal the oldest registered caller without waking the rest.
            ticket = threading.Condition(self._lock)
            self._queue.append(ticket)
            try:
                while True:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    if self._queue[0] is ticket and self._active < self._limit:
                        return self._admit()
                    ticket.wait(remaining)
            finally:
                if self._queue[0] is ticket:
                    self._queue.popleft()
                    self._wake_head()
                else:
                    self._queue.remove(ticket)

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1
            self._wake_head()

    def set_limit(self, n) -> int:
        with self._lock:
            self._limit = self._clamp(n)
            self._wake_head()
            return self._limit

    def snapshot(self) -> dict:
        with self._lock:
            return {"limit": self._limit, "active": self._active,
                    "waiting": len(self._queue), "total_acquired": self._total}

    @contextmanager
    def slot(self, timeout):
        if not self.acquire(timeout):
            raise QueueTimeout("agent 并发闸门排队超时")
        try:
            yield
        finally:
            self.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()


# 进程级单例（bot 启动后按配置 set_limit）
GATE = AgentGate(DEFAULT_LIMIT)
