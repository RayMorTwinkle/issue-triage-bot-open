"""gate.py 测试：agent 并发闸门（限流/释放/动态调整/唤醒/统计）。"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gate import AgentGate


class TestAgentGate(unittest.TestCase):
    def test_limit_blocks(self):
        g = AgentGate(1)
        self.assertTrue(g.acquire())
        self.assertFalse(g.acquire(timeout=0.1))   # 满额：等待超时拿不到
        g.release()
        self.assertTrue(g.acquire(timeout=0.5))
        g.release()

    def test_dynamic_limit(self):
        g = AgentGate(1)
        g.acquire()
        g.set_limit(2)                              # 放宽 → 第二个可进
        self.assertTrue(g.acquire(timeout=0.5))
        snap = g.snapshot()
        self.assertEqual(snap["active"], 2)
        self.assertEqual(snap["limit"], 2)
        g.release()
        g.release()

    def test_waiting_thread_wakes(self):
        g = AgentGate(1)
        g.acquire()
        woken = []

        def worker():
            g.acquire()
            woken.append(1)
            g.release()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.1)
        self.assertEqual(len(woken), 0)             # 被挡住
        g.release()
        t.join(timeout=2)
        self.assertEqual(len(woken), 1)             # 释放后唤醒

    def test_clamp(self):
        self.assertEqual(AgentGate(0)._limit, 1)
        self.assertEqual(AgentGate(1000)._limit, 99)
        self.assertEqual(AgentGate("x")._limit, 3)

    def test_context_manager(self):
        g = AgentGate(1)
        with g:
            self.assertEqual(g.snapshot()["active"], 1)
        self.assertEqual(g.snapshot()["active"], 0)

    def test_stats(self):
        g = AgentGate(2)
        g.acquire()
        g.acquire()
        snap = g.snapshot()
        self.assertEqual(snap["active"], 2)
        self.assertEqual(snap["total_acquired"], 2)
        g.release()
        g.release()
        self.assertEqual(g.snapshot()["active"], 0)


if __name__ == "__main__":
    unittest.main()
