"""db.py 与 v2→v3 迁移测试（SQLite 数据层）。"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod
import state as state_mod


class TestDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kv(self):
        self.assertIsNone(self.db.kv_get("nope"))
        self.assertEqual(self.db.kv_get("nope", 42), 42)
        self.db.kv_set("global_mode", "semi")
        self.assertEqual(self.db.kv_get("global_mode"), "semi")
        self.db.kv_set("poll_interval_sec", 300)
        self.assertEqual(self.db.kv_get("poll_interval_sec"), 300)

    def test_projects_crud(self):
        pid = self.db.add_project("a/b", "https://x/a/b.git",
                                  build_cmd=["true"], build_env={"K": "V"},
                                  release_cfg={"time_enabled": True})
        p = self.db.get_project("a/b")
        self.assertEqual(p["id"], pid)
        self.assertEqual(p["build_cmd"], ["true"])
        self.assertEqual(p["build_env"], {"K": "V"})
        self.assertEqual(p["release_cfg"]["time_enabled"], True)
        self.assertEqual(p["mode"], "default")
        self.db.update_project("a/b", mode="yolo", intro="hi")
        p = self.db.get_project("a/b")
        self.assertEqual(p["mode"], "yolo")
        self.assertEqual(p["intro"], "hi")
        self.assertEqual(len(self.db.list_projects()), 1)

    def test_issues_upsert_roundtrip(self):
        pid = self.db.add_project("a/b", "u")
        self.db.upsert_issue(pid, 5, title="t", stage="done",
                             verdict={"verdict": "accept"},
                             retries={"gate": 2}, history=[{"stage": "fix"}],
                             session_ids={"s1": "ses_x"}, merged=1,
                             cost=0.5, tokens=100)
        i = self.db.get_issue(pid, 5)
        self.assertEqual(i["title"], "t")
        self.assertEqual(i["verdict"]["verdict"], "accept")
        self.assertEqual(i["retries"]["gate"], 2)
        self.assertEqual(i["session_ids"]["s1"], "ses_x")
        self.assertTrue(i["merged"])
        self.assertEqual(i["tokens"], 100)
        # update 路径：只动传入字段
        self.db.upsert_issue(pid, 5, stage="rejected")
        i2 = self.db.get_issue(pid, 5)
        self.assertEqual(i2["stage"], "rejected")
        self.assertEqual(i2["title"], "t")
        self.assertEqual(len(self.db.list_issues(pid)), 1)

    def test_info_fields_roundtrip(self):
        """v3.1 追问字段：roundtrip + 局部 update 不丢 + NULL 防御。"""
        pid = self.db.add_project("a/b", "u")
        self.db.upsert_issue(pid, 7, stage="waiting_info",
                             info_posted=1, info_seen_comments=3)
        i = self.db.get_issue(pid, 7)
        self.assertEqual(i["info_posted"], 1)
        self.assertEqual(i["info_seen_comments"], 3)
        # update 只动传入字段，不丢 info 字段
        self.db.upsert_issue(pid, 7, title="t")
        i2 = self.db.get_issue(pid, 7)
        self.assertEqual(i2["info_posted"], 1)
        self.assertEqual(i2["info_seen_comments"], 3)
        # NULL（防御路径；-1 映射在 state 层做）
        with self.db._conn() as c:
            c.execute("UPDATE issues SET info_seen_comments=NULL WHERE number=7")
        self.assertIsNone(self.db.get_issue(pid, 7)["info_seen_comments"])

    def test_conn_closed_after_context(self):
        """回归（2026-09-12 生产事故）：连接必须确定性关闭，fd 不留给 GC。"""
        import sqlite3 as _sq
        with self.db._conn() as c:
            c.execute("SELECT 1").fetchone()
        with self.assertRaises(_sq.ProgrammingError):
            c.execute("SELECT 1")   # 已 close 的连接再执行必然报错

    def test_sessions(self):
        pid = self.db.add_project("a/b", "u")
        self.db.add_session(pid, 5, "s1", "ses_1", "triage")
        self.db.add_session(pid, 5, "s2", "ses_2", "review")
        self.db.add_session(pid, 5, "s1", "ses_1", "fix")  # 同 session_id 去重更新
        rows = self.db.list_sessions(pid)
        self.assertEqual(len(rows), 2)
        self.db.delete_session_row("ses_1")
        self.assertEqual(len(self.db.list_sessions(pid)), 1)

    def test_publish_fields_second_project(self):
        """spec D：publish/merge_fix 4 列在非默认项目同样可写可读（原 bug：
        缺列时非首项目 publish/merge-fix 端点必 500）。"""
        self.db.add_project("a/b", "u")              # 默认/首项目
        pid2 = self.db.add_project("c/d", "u")       # 非默认项目
        self.db.upsert_issue(pid2, 9, stage="done",
                             publish_status="done",
                             publish_result={"pr_url": "https://x/9"},
                             merge_fix_status="failed",
                             merge_fix_result={"error": "conflict"})
        i = self.db.get_issue(pid2, 9)
        self.assertEqual(i["publish_status"], "done")
        self.assertEqual(i["publish_result"]["pr_url"], "https://x/9")
        self.assertEqual(i["merge_fix_status"], "failed")
        self.assertEqual(i["merge_fix_result"]["error"], "conflict")

    def test_save_issue_full_session_mirror(self):
        """spec K：issue+sessions 同事务，sessions 收敛为 session_ids 的镜像——
        不在集合中的本 issue 行软删除，墓碑行不复活。"""
        pid = self.db.add_project("a/b", "u")
        self.db.save_issue_full(pid, 5, {"stage": "fix"},
                                {"s1": "ses_a", "s2": "ses_b"}, "fix")
        self.assertEqual(len(self.db.list_sessions(pid)), 2)
        # 镜像收敛：session_ids 只剩 s1 → ses_b 行被软删
        self.db.save_issue_full(pid, 5, {"stage": "review"},
                                {"s1": "ses_a"}, "review")
        rows = self.db.list_sessions(pid)
        self.assertEqual([r["session_id"] for r in rows], ["ses_a"])
        # 墓碑防御：显式删除后，stale 快照重放 ses_a 也不复活
        self.db.delete_session_row("ses_a")
        self.db.save_issue_full(pid, 5, {"stage": "review"},
                                {"s1": "ses_a"}, "review")
        self.assertEqual(len(self.db.list_sessions(pid)), 0)

    def test_prs_branches_releases_runs(self):
        pid = self.db.add_project("a/b", "u")
        self.db.add_pr(pid, 5, 3, "https://x/pull/3", "fix/issue-5")
        self.db.set_pr_merged("https://x/pull/3")
        pr = self.db.list_prs(pid)[0]
        self.assertEqual(pr["state"], "merged")
        self.assertTrue(pr["merged_at"])
        self.db.add_branch(pid, 5, "fix/issue-5")
        self.db.add_release(pid, "v1.0.1", "https://x/releases/v1.0.1", "schedule")
        self.assertEqual(self.db.list_releases(pid)[0]["tag"], "v1.0.1")
        self.db.add_run(pid, 5, "fix", True, {"note": "ok"})


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (state_mod.STATE_DIR, state_mod.STATE_PATH)
        state_mod.STATE_DIR = self.tmp
        state_mod.STATE_PATH = os.path.join(self.tmp, "state.json")
        state_mod._initialized.clear()

    def tearDown(self):
        state_mod.STATE_DIR, state_mod.STATE_PATH = self._orig
        state_mod._initialized.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migrate_v2_e2e(self):
        # 构造 v2 环境
        logs = os.path.join(self.tmp, "logs")
        os.makedirs(os.path.join(logs, "issues", "1", "outbox"))
        with open(os.path.join(logs, "issues", "1", "outbox", "final-reply.md"), "w") as f:
            f.write("hi")
        os.makedirs(os.path.join(logs, "issues", "2"))
        with open(os.path.join(logs, "issues", "not-a-dir"), "w") as f:
            f.write("x")  # 非目录不应被搬
        with open(os.path.join(logs, "run-1.jsonl"), "w") as f:
            f.write("{}\n")
        with open(state_mod.STATE_PATH, "w") as f:
            json.dump({"global_mode": "semi", "poll_interval_sec": 120,
                       "issues": {"1": {"stage": "done", "title": "T1",
                                        "retries": {"gate": 1},
                                        "session_ids": {"s1": "ses_a"}}}}, f)
        cfg = {"repo": "x/y", "repo_url": "https://x/x/y.git",
               "build_cmd": ["true"], "build_env": {}, "main_branch": "main"}
        mig = state_mod.migrate_if_needed(cfg, logs)
        self.assertIn("migrated", mig)
        # db 验证
        st = state_mod.load()
        self.assertEqual(st["global_mode"], "semi")
        self.assertEqual(st["poll_interval_sec"], 120)
        self.assertEqual(st["issues"]["1"]["stage"], "done")
        self.assertEqual(st["issues"]["1"]["retries"]["gate"], 1)
        self.assertEqual(st["issues"]["1"]["session_ids"]["s1"], "ses_a")
        # 档案搬迁
        self.assertTrue(os.path.isdir(
            os.path.join(logs, "projects", "x__y", "issues", "1", "outbox")))
        self.assertFalse(os.path.exists(os.path.join(logs, "issues", "1")))
        self.assertTrue(os.path.exists(
            os.path.join(logs, "projects", "x__y", "run-1.jsonl")))
        # 旧 state.json 已退休（改名留档，不再作为数据源）
        self.assertTrue(any(f.startswith("state-migrated-v2-")
                            for f in os.listdir(self.tmp)))
        self.assertFalse(os.path.exists(state_mod.STATE_PATH))
        # 幂等
        self.assertEqual(state_mod.migrate_if_needed(cfg, logs), "already")

    def test_migrate_empty(self):
        cfg = {"repo": "x/y"}
        mig = state_mod.migrate_if_needed(cfg, os.path.join(self.tmp, "logs"))
        self.assertIn("project created", mig)
        self.assertEqual(len(state_mod.load()["issues"]), 0)
        # 第二次调用幂等
        self.assertEqual(state_mod.migrate_if_needed(cfg, os.path.join(self.tmp, "logs")),
                         "already")


class TestConcurrentSave(unittest.TestCase):
    """多 issue 并行：save_issue 细粒度写入不丢更新（v3 并发正确性）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (state_mod.STATE_DIR, state_mod.STATE_PATH)
        state_mod.STATE_DIR = self.tmp
        state_mod.STATE_PATH = os.path.join(self.tmp, "state.json")
        state_mod._initialized.clear()

    def tearDown(self):
        state_mod.STATE_DIR, state_mod.STATE_PATH = self._orig
        state_mod._initialized.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_lost_update_across_issues(self):
        st = state_mod.load()
        state_mod.issue_entry(st, 1)["title"] = "init1"
        state_mod.issue_entry(st, 2)["title"] = "init2"
        state_mod.save(st)

        barrier = threading.Barrier(2)

        def worker(n, title):
            st = state_mod.load()
            e = state_mod.issue_entry(st, n)
            e["title"] = title
            barrier.wait()          # 两个线程同时持有各自快照
            state_mod.save_issue(st, n)

        ts = [threading.Thread(target=worker, args=(1, "A1")),
              threading.Thread(target=worker, args=(2, "A2"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        st = state_mod.load()
        self.assertEqual(st["issues"]["1"]["title"], "A1")
        self.assertEqual(st["issues"]["2"]["title"], "A2")

    def test_project_isolation(self):
        """多项目隔离：两个项目的 issue 行互不串写（v3.1）。"""
        st_a = state_mod.load_project("a/x")
        state_mod.issue_entry(st_a, 1)["title"] = "A1"
        state_mod.save_issue(st_a, 1, repo="a/x")
        st_b = state_mod.load_project("b/y")
        state_mod.issue_entry(st_b, 1)["title"] = "B1"
        state_mod.save_issue(st_b, 1, repo="b/y")
        # 各自读回各自的行（同号 issue 不串）
        a = state_mod.load_project("a/x")
        b = state_mod.load_project("b/y")
        self.assertEqual(a["issues"]["1"]["title"], "A1")
        self.assertEqual(b["issues"]["1"]["title"], "B1")
        # save_project 批量路径同样隔离
        st_a["issues"]["2"] = dict(state_mod.issue_entry(st_a, 1))
        st_a["issues"]["2"]["title"] = "A2"
        state_mod.save_project(st_a, "a/x")
        a = state_mod.load_project("a/x")
        b = state_mod.load_project("b/y")
        self.assertEqual(a["issues"]["2"]["title"], "A2")
        self.assertNotIn("2", b["issues"])


if __name__ == "__main__":
    unittest.main()
