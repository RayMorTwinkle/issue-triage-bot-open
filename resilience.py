"""Process-local transport circuit. A single probe may run after cooldown."""
from contextlib import contextmanager
from functools import lru_cache
from subprocess import TimeoutExpired
import threading
import time


class ReadServiceError(RuntimeError):
    """The service was unreachable or returned a transient failure."""


class CircuitOpen(RuntimeError):
    pass


class ReadCircuit:
    def __init__(self, threshold=3, cooldown=30, clock=time.monotonic):
        self.threshold = threshold
        self.cooldown = cooldown
        self.clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._generation = 0
        self._retry_at = None
        self._probing = False

    @contextmanager
    def call(self):
        with self._lock:
            if self._retry_at is not None:
                if self._probing or self.clock() < self._retry_at:
                    raise CircuitOpen("读取服务熔断中，请在冷却结束后重试")
                self._probing = True
            generation = self._generation
        failed = False
        try:
            yield
        except (OSError, TimeoutExpired, ReadServiceError):
            failed = True
            raise
        finally:
            with self._lock:
                # Calls admitted before the trip cannot reset a newer circuit.
                if self._generation == generation:
                    self._failures = self._failures + 1 if failed else 0
                    if self._failures >= self.threshold:
                        self._generation += 1
                        self._retry_at = self.clock() + self.cooldown
                    else:
                        self._retry_at = None
                    self._probing = False


@lru_cache(maxsize=256)
def repo_read_circuit(repo):
    return ReadCircuit()
