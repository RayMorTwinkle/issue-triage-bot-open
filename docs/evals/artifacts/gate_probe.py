"""Replay fixed scheduler interleavings against a checkout. Uses the standard library."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import threading
import time
from unittest.mock import patch


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("synchronization point not reached")
        time.sleep(0.001)


def spawn(action):
    thread = threading.Thread(target=action, daemon=True)
    thread.start()
    return thread


def finish(thread):
    thread.join(4)
    if thread.is_alive():
        raise RuntimeError("worker did not finish")


def no_barging(module):
    gate = module.AgentGate(1)
    gate.acquire()
    old = []

    def queued_call():
        old.append(gate.acquire(3))
        if old[-1]:
            gate.release()

    worker = spawn(queued_call)
    wait_for(lambda: gate.snapshot()["waiting"] == 1)
    # Pin a legal schedule: the releasing thread makes a second, nonblocking
    # request before the notified thread can run. Only this fixture uses the lock.
    guard = gate._lock if hasattr(gate, "_lock") else gate._cond
    with guard:
        gate.release()
        newcomer = gate.acquire(0)
        if newcomer:
            gate.release()
    finish(worker)
    return not newcomer and old == [True], {"newcomer_admitted": newcomer, "queued": old}


def wall_clock_step(module):
    gate = module.AgentGate(1)
    gate.acquire()
    result = []

    class SteppedClock:
        monotonic = staticmethod(time.monotonic)

        def __init__(self):
            self.wall = 0

        def time(self):
            self.wall += 3600
            return self.wall

    def queued_call():
        result.append(gate.acquire(2))
        if result[-1]:
            gate.release()

    # Change only gate.py's view of wall time, never the OS or thread scheduler.
    with patch.object(module, "time", SteppedClock()):
        worker = spawn(queued_call)
        wait_for(lambda: result or gate.snapshot()["waiting"] == 1)
        gate.release()
        finish(worker)
    return result == [True], {"admitted_after_release": result}


def shrink(module):
    gate = module.AgentGate(2)
    gate.acquire()
    gate.acquire()
    gate.set_limit(1)
    active = gate.snapshot()["active"]
    above = gate.acquire(0)
    gate.release()
    at = gate.acquire(0)
    gate.release()
    drained = gate.acquire(0)
    gate.release()
    state = [active, above, at, drained, gate.snapshot()["active"]]
    return state == [2, False, False, True, 0], {"occupancy_and_probes": state}


def timeout_cleanup(module):
    gate = module.AgentGate(1)
    gate.acquire()
    timed = gate.acquire(0.015)
    waiting = gate.snapshot()["waiting"]
    gate.release()
    next_call = gate.acquire(0)
    gate.release()
    return (timed, waiting, next_call) == (False, 0, True), {
        "timed": timed, "waiting": waiting, "next": next_call}


def expand(module):
    gate = module.AgentGate(1)
    gate.acquire()
    done = threading.Event()

    def queued_call():
        if gate.acquire(3):
            done.wait(4)
            gate.release()

    workers = [spawn(queued_call) for _ in range(2)]
    try:
        wait_for(lambda: gate.snapshot()["waiting"] == 2)
        gate.set_limit(3)
        wait_for(lambda: gate.snapshot()["active"] == 3)
        peak = gate.snapshot()
    finally:
        done.set()
        gate.release()
        for thread in workers:
            finish(thread)
    end = gate.snapshot()
    return peak["waiting"] == 0 and end["active"] == 0, {"peak": peak, "end": end}


def error_exit(module):
    gate = module.AgentGate(1)
    try:
        with gate:
            raise ValueError("fixture")
    except ValueError:
        pass
    status = gate.snapshot()
    return status == {"active": 0, "waiting": 0, "limit": 1, "total_acquired": 1}, status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    source = args.checkout / "gate.py"
    spec = importlib.util.spec_from_file_location("target_gate", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = [no_barging, wall_clock_step, shrink, timeout_cleanup, expand, error_exit]
    rows = []
    for round_no in range(1, args.rounds + 1):
        for case in cases:
            try:
                passed, observed = case(module)
            except Exception as exc:
                passed, observed = False, {"error": repr(exc)}
            rows.append(dict(round=round_no, case=case.__name__, passed=passed, observed=observed))
    report = dict(python=platform.python_version(), machine=platform.machine(),
                  gate_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  probe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  rounds=args.rounds, passed=sum(row["passed"] for row in rows), rows=rows)
    print(json.dumps(report, indent=2))
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
