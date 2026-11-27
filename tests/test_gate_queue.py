"""Regression checks for registration order, cancellation and slot accounting."""
import threading
import time
import unittest

from gate import AgentGate


class TestGateQueue(unittest.TestCase):
    def wait_count(self, gate, count):
        deadline = time.monotonic() + 4
        while gate.snapshot()["waiting"] != count:
            self.assertLess(time.monotonic(), deadline, "worker registration timed out")
            time.sleep(0.001)

    def test_three_independent_reviews_finish_at_limit_one(self):
        from unittest.mock import patch
        import llm
        gate = AgentGate(1)
        tasks = [dict(prompt=lane, dir=".", title=lane) for lane in ("bug", "style", "minimal")]

        def execute(prompt, *_):
            self.assertEqual(gate.snapshot()["active"], 1)
            return {"session_id": prompt}

        with patch.object(llm, "GATE", gate), patch.object(llm, "_run_single_locked", execute):
            results = llm._run_tasks(tasks, 1, {"agent_queue_timeout_sec": 1})
        self.assertEqual([(r["session_id"], r["title"]) for r in results],
                         [(t["prompt"], t["title"]) for t in tasks])
        self.assertEqual(gate.snapshot(), dict(active=0, limit=1, waiting=0, total_acquired=3))

    def test_waiters_admitted_in_registration_order(self):
        gate = AgentGate(1)
        order = []

        def record(index):
            with gate:
                order.append(index)

        threads = []
        with gate:
            for index in range(8):
                thread = threading.Thread(target=record, args=(index,), daemon=True)
                threads.append(thread)
                thread.start()
                self.wait_count(gate, index + 1)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(order, list(range(8)))
        self.assertEqual(gate.snapshot()["total_acquired"], 9)

    def test_timed_out_middle_ticket_preserves_older_waiter(self):
        gate = AgentGate(1)
        order = []

        def record(label):
            with gate:
                order.append(label)

        with gate:
            first = threading.Thread(target=record, args=("first",), daemon=True)
            first.start()
            self.wait_count(gate, 1)
            self.assertFalse(gate.acquire(0.02))
            self.assertEqual(gate.snapshot()["waiting"], 1)
            last = threading.Thread(target=record, args=("last",), daemon=True)
            last.start()
            self.wait_count(gate, 2)
        for thread in (first, last):
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(order, ["first", "last"])
        self.assertEqual(gate.snapshot(), {"limit": 1, "active": 0,
                                           "waiting": 0, "total_acquired": 3})
