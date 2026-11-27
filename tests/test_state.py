import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state as state_mod
from state import default_state


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = state_mod.STATE_PATH, state_mod.STATE_DIR
        state_mod.STATE_DIR = self.tmp
        state_mod.STATE_PATH = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        state_mod.STATE_PATH, state_mod.STATE_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_and_issue_entry(self):
        st = state_mod.load()
        self.assertEqual(st["global_mode"], "dry")
        e = state_mod.issue_entry(st, 7)
        self.assertEqual(e["stage"], "pending")
        self.assertEqual(e["mode"], "default")
        self.assertEqual(state_mod.effective_mode(st, e), "dry")

    def test_effective_mode_override(self):
        st = state_mod.load()
        e = state_mod.issue_entry(st, 1)
        e["mode"] = "full"
        self.assertEqual(state_mod.effective_mode(st, e), "full")
        e["mode"] = "default"
        st["global_mode"] = "semi"
        self.assertEqual(state_mod.effective_mode(st, e), "semi")

    def test_info_fields_roundtrip(self):
        """v3.1 追问字段经 save_issue/load 往返不丢（白名单映射）+ NULL → -1 防御。"""
        st = state_mod.load()
        e = state_mod.issue_entry(st, 9)
        e["stage"] = "waiting_info"
        e["info_posted"] = True
        e["info_seen_comments"] = 4
        state_mod.save_issue(st, 9)
        e2 = state_mod.load()["issues"]["9"]
        self.assertTrue(e2["info_posted"])
        self.assertEqual(e2["info_seen_comments"], 4)
        # NULL（存量行升级场景）→ -1（基线未知，watcher 首查补记）
        with state_mod._db()._conn() as c:
            c.execute("UPDATE issues SET info_seen_comments=NULL, info_posted=NULL "
                      "WHERE number=9")
        e3 = state_mod.load()["issues"]["9"]
        self.assertEqual(e3["info_seen_comments"], -1)
        self.assertFalse(e3["info_posted"])

    def test_save_roundtrip(self):
        st = state_mod.load()
        e = state_mod.issue_entry(st, 3)
        e["retries"]["gate"] = 2
        e["session_ids"]["s1"] = "ses_abc"
        state_mod.save(st)
        st2 = state_mod.load()
        self.assertEqual(st2["issues"]["3"]["retries"]["gate"], 2)
        self.assertEqual(st2["issues"]["3"]["session_ids"]["s1"], "ses_abc")

    def test_save_does_not_write_kv(self):
        """spec1-C：save()/save_issue() 不得用快照回写 global_mode kv
        （快照可能数小时旧，盲写会把用户运行中改的档位静默回滚）。"""
        st = state_mod.load()
        st["global_mode"] = "paused"           # 只改了内存快照
        st["poll_interval_sec"] = 5
        state_mod.issue_entry(st, 1)
        state_mod.save(st)
        d = state_mod._db()
        self.assertEqual(d.kv_get("global_mode", "dry"), "dry")
        self.assertEqual(int(d.kv_get("poll_interval_sec", 600) or 600), 600)
        # save_issue 同样不写 kv
        e = state_mod.issue_entry(st, 2)
        e["stage"] = "triage"
        state_mod.save_issue(st, 2)
        self.assertEqual(d.kv_get("global_mode", "dry"), "dry")

    def test_interleaved_save_issue_no_rollback(self):
        """spec1-C：交错 save_issue 不回滚其它 issue 的 stage（窄写隔离）。"""
        st1 = state_mod.load()
        st2 = state_mod.load()                  # 并发方拿到的同刻快照
        e3 = state_mod.issue_entry(st2, 3)
        e3["stage"] = "gate"
        state_mod.save_issue(st2, 3)            # 并发方推进 issue 3
        e9 = state_mod.issue_entry(st1, 9)
        e9["stage"] = "fix"
        state_mod.save_issue(st1, 9)            # 本方保存自己的 issue
        st3 = state_mod.load()
        self.assertEqual(st3["issues"]["3"]["stage"], "gate")   # 未被 st1 回滚
        self.assertEqual(st3["issues"]["9"]["stage"], "fix")

    def test_bump_retry(self):
        st = state_mod.load()
        e = state_mod.issue_entry(st, 9)
        self.assertEqual(state_mod.bump_retry(e, "gate"), 1)
        self.assertEqual(state_mod.bump_retry(e, "gate"), 2)

    def test_atomic_write_leaves_no_tmp(self):
        st = state_mod.load()
        state_mod.save(st)
        self.assertFalse(os.path.exists(state_mod.STATE_PATH + ".tmp"))

    def test_corrupt_file_falls_back(self):
        with open(state_mod.STATE_PATH, "w") as f:
            f.write("{broken")
        st = state_mod.load()
        self.assertEqual(st["global_mode"], "dry")


class TestInstanceLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = state_mod.STATE_DIR, state_mod.LOCK_PATH, state_mod.PID_PATH
        state_mod.STATE_DIR = self.tmp
        state_mod.LOCK_PATH = os.path.join(self.tmp, "bot.lock")
        state_mod.PID_PATH = os.path.join(self.tmp, "bot.pid")

    def tearDown(self):
        state_mod.STATE_DIR, state_mod.LOCK_PATH, state_mod.PID_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exclusive(self):
        l1 = state_mod.InstanceLock()
        self.assertTrue(l1.acquire())
        l2 = state_mod.InstanceLock()
        self.assertFalse(l2.acquire())
        l1.release()
        self.assertTrue(l2.acquire())
        l2.release()


if __name__ == "__main__":
    unittest.main()
