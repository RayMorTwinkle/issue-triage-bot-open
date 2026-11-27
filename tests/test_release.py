"""release.py 测试：触发条件（时间/数量/OR/重置）与 dry/yolo 执行路径。"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod
import llm as llm_mod
import release as rel


def _R(ok, data=None):
    r = llm_mod.LLMResult()
    r.ok, r.data = ok, data
    return r


class TestReleaseDue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()
        self.pid = self.db.add_project("a/b", "u", main_branch="main")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _proj(self, **rc):
        cfg = {"time_enabled": False, "interval_hours": 24,
               "count_enabled": False, "count_threshold": 5, "mode": "follow"}
        cfg.update(rc)
        self.db.update_project("a/b", release_cfg=cfg)
        return self.db.get_project("a/b")

    def test_no_conditions_disabled(self):
        proj = self._proj()
        due = rel.check_due(self.db, proj)
        self.assertFalse(due["due"])

    def test_time_condition(self):
        proj = self._proj(time_enabled=True, interval_hours=1)
        with patch.object(rel.gh, "latest_commit_sha", return_value="sha1"):
            due = rel.check_due(self.db, proj)
        self.assertTrue(due["due"])
        rel.set_release_state(self.db, proj, {"last_ts": time.time(), "last_sha": "sha1"})
        with patch.object(rel.gh, "latest_commit_sha", return_value="sha1"):
            due = rel.check_due(self.db, proj)
        self.assertFalse(due["due"])

    def test_time_condition_needs_new_commit(self):
        proj = self._proj(time_enabled=True, interval_hours=1)
        rel.set_release_state(self.db, proj,
                              {"last_ts": time.time() - 2 * 3600, "last_sha": "same"})
        with patch.object(rel.gh, "latest_commit_sha", return_value="same"):
            due = rel.check_due(self.db, proj)
        self.assertFalse(due["due"])   # 时间到但无新提交
        with patch.object(rel.gh, "latest_commit_sha", return_value="new"):
            due = rel.check_due(self.db, proj)
        self.assertTrue(due["due"])

    def test_count_condition_and_reset(self):
        proj = self._proj(count_enabled=True, count_threshold=2)
        self.db.add_pr(self.pid, None, 1, "https://x/1", "b1")
        self.db.add_pr(self.pid, None, 2, "https://x/2", "b2")
        self.db.set_pr_merged("https://x/1")
        self.db.set_pr_merged("https://x/2")
        rel.set_release_state(self.db, proj, {"last_ts": 0, "last_sha": ""})
        due = rel.check_due(self.db, proj)
        self.assertTrue(due["due"])
        self.assertEqual(due["merged_since"], 2)
        # 发布后 last_ts 更新 → 计数归零
        rel.set_release_state(self.db, proj, {"last_ts": time.time(), "last_sha": ""})
        due = rel.check_due(self.db, proj)
        self.assertFalse(due["due"])
        self.assertEqual(due["merged_since"], 0)

    def test_or_semantics(self):
        proj = self._proj(time_enabled=True, interval_hours=10 ** 6,
                          count_enabled=True, count_threshold=1)
        self.db.add_pr(self.pid, None, 1, "https://x/1", "b1")
        self.db.set_pr_merged("https://x/1")
        rel.set_release_state(self.db, proj, {"last_ts": 0})
        with patch.object(rel.gh, "latest_commit_sha", return_value="s"):
            due = rel.check_due(self.db, proj)
        self.assertTrue(due["due"])
        self.assertEqual(len(due["reasons"]), 1)   # 仅数量条件（时间未到）

    def test_resolve_mode(self):
        proj = self._proj(mode="follow")
        self.db.kv_set("global_mode", "semi")
        self.assertEqual(rel.resolve_mode(self.db, proj, rel.get_release_cfg(proj)), "semi")
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        proj2 = self.db.get_project("a/b")
        self.assertEqual(rel.resolve_mode(self.db, proj2, rel.get_release_cfg(proj2)), "yolo")


class TestReleaseRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()
        self.pid = self.db.add_project("a/b", "u", main_branch="main")
        self.logs = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_with_workflow(self):
        self.db.update_project("a/b", release_cfg={"mode": "dry", "time_enabled": True})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release",
                                         "path": ".github/workflows/release.yml"}]), \
             patch.object(rel, "run_workflow") as mw:
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertTrue(r["ok"])
        self.assertTrue(r["dry"])
        mw.assert_not_called()
        plans = []
        for root, _, fs in os.walk(self.logs):
            plans += [f for f in fs if f == "release-plan.json"]
        self.assertTrue(plans)

    def test_dry_no_scheme(self):
        self.db.update_project("a/b", release_cfg={"mode": "dry"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows", return_value=[]), \
             patch.object(rel, "_read_release_md", return_value=""), \
             patch.object(rel, "_needs_human") as mh:
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertFalse(r["ok"])
        mh.assert_called_once()

    def test_workflow_yolo_success_resets_state(self):
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]), \
             patch.object(rel, "trigger_and_wait", return_value=(True, "success url")), \
             patch.object(rel.gh, "latest_release_tag", return_value="v1.0.0"), \
             patch.object(rel.gh, "latest_commit_sha", return_value="s2"):
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertTrue(r["ok"])
        self.assertFalse(r["dry"])
        st = rel.get_release_state(self.db, proj)
        self.assertTrue(st.get("last_ts"))
        self.assertEqual(st.get("last_sha"), "s2")

    def test_workflow_failure_calls_fix_then_retry(self):
        """workflow 失败 → agent 修错 → 重新触发并成功。"""
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        proj = self.db.get_project("a/b")
        seq = [(False, "failure url"), (True, "success url")]
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]), \
             patch.object(rel, "trigger_and_wait", side_effect=seq), \
             patch.object(rel, "_fix_workflow_failure",
                          return_value={"ok": True, "steps": ["fixed"]}) as mfx, \
             patch.object(rel.gh, "latest_release_tag", return_value="v1.0.0"), \
             patch.object(rel.gh, "latest_commit_sha", return_value="s3"):
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertTrue(r["ok"])
        mfx.assert_called_once()

    def test_failure_records_last_error(self):
        """失败留痕：run_release 失败后 state 带 last_error（供 UI 展示）。"""
        self.db.update_project("a/b", release_cfg={"mode": "dry"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows", return_value=[]), \
             patch.object(rel, "_read_release_md", return_value=""), \
             patch.object(rel, "_needs_human"):
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertFalse(r["ok"])
        st = rel.get_release_state(self.db, proj)
        self.assertIn("Release.md", st.get("last_error", ""))
        self.assertTrue(st.get("last_error_ts"))

    def test_success_clears_last_error(self):
        """真实发布成功后清除上次失败痕迹（dry 成功不清除）。"""
        proj = self.db.get_project("a/b")
        rel.set_release_state(self.db, proj,
                              {"last_error": "旧的失败", "last_error_ts": 1.0})
        # dry 成功不清除
        self.db.update_project("a/b", release_cfg={"mode": "dry"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]):
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertTrue(r["dry"])
        st = rel.get_release_state(self.db, proj)
        self.assertEqual(st.get("last_error"), "旧的失败")
        # yolo 真实成功 → 清除
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]), \
             patch.object(rel, "trigger_and_wait", return_value=(True, "success url")), \
             patch.object(rel.gh, "latest_release_tag", return_value="v1.0.0"), \
             patch.object(rel.gh, "latest_commit_sha", return_value="s4"):
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertTrue(r["ok"] and not r["dry"])
        st = rel.get_release_state(self.db, proj)
        self.assertNotIn("last_error", st)


class TestReleaseTimeoutResilience(unittest.TestCase):
    """回归：gh/git 超时（TimeoutExpired）不得炸穿发布线程——要走 needs_human + 留痕。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()
        self.db.add_project("a/b", "u", main_branch="main")
        self.logs = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_workflow_timeout_records_error(self):
        import subprocess
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]), \
             patch.object(rel, "trigger_and_wait",
                          side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=60)), \
             patch.object(rel, "_needs_human") as mh:
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertFalse(r["ok"])
        mh.assert_called_once()
        st = rel.get_release_state(self.db, proj)
        self.assertIn("timed out", st.get("last_error", ""))


class TestReleaseVisibility(unittest.TestCase):
    """回归：发布可观测性——last_result 记录、成功系统通知、进度 kv 清理。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()
        self.db.add_project("a/b", "u", main_branch="main")
        self.logs = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_success_result_and_notify(self):
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]), \
             patch.object(rel, "trigger_and_wait", return_value=(True, "success url")), \
             patch.object(rel.gh, "latest_release_tag", return_value="v1.0.0"), \
             patch.object(rel.gh, "latest_commit_sha", return_value="s9"), \
             patch.object(rel, "notify") as mn:
            rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        r = rel.get_release_state(self.db, proj).get("last_result") or {}
        self.assertTrue(r.get("ok"))
        self.assertFalse(r.get("dry"))
        mn.assert_called_once()          # 成功弹系统通知
        # 进度 kv 已清理
        self.assertFalse(self.db.kv_get("release_progress::a/b", ""))
        self.assertFalse(self.db.kv_get("release_progress::a/b", "") or {})

    def test_dry_result_no_notify(self):
        self.db.update_project("a/b", release_cfg={"mode": "dry"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]), \
             patch.object(rel, "notify") as mn:
            rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        r = rel.get_release_state(self.db, proj).get("last_result") or {}
        self.assertTrue(r.get("ok") and r.get("dry"))
        mn.assert_not_called()           # DRY 只是计划，不打扰
        self.assertTrue(self.db.kv_get("release_progress::a/b", "") is not None)


class TestWorkflowTagBackfill(unittest.TestCase):
    """回归：workflow 路径成功后回填 tag（通知/结果卡显示版本号）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()
        self.db.add_project("a/b", "u", main_branch="main")
        self.logs = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tag_backfilled_and_in_notify(self):
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]), \
             patch.object(rel, "trigger_and_wait", return_value=(True, "success url")), \
             patch.object(rel.gh, "latest_commit_sha", return_value="s1"), \
             patch.object(rel.gh, "latest_release_tag", return_value="v9.9.9"), \
             patch.object(rel, "notify") as mn:
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertEqual(r.get("tag"), "v9.9.9")
        lr = rel.get_release_state(self.db, proj).get("last_result") or {}
        self.assertEqual(lr.get("tag"), "v9.9.9")
        args = mn.call_args[0]
        self.assertIn("v9.9.9", args[1])   # 通知正文带版本号

    def test_tag_empty_not_fatal(self):
        """releases/latest 查询失败（如 404）不影响发布成功判定。"""
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]), \
             patch.object(rel, "trigger_and_wait", return_value=(True, "success url")), \
             patch.object(rel.gh, "latest_commit_sha", return_value="s1"), \
             patch.object(rel.gh, "latest_release_tag", return_value=""), \
             patch.object(rel, "notify"):
            r = rel.run_release(proj, {}, self.db, trigger="manual", log_dir=self.logs)
        self.assertTrue(r["ok"])
        self.assertEqual(r.get("tag"), "")


class TestCheckErrorTrail(unittest.TestCase):
    """spec J 验收：sha 查询失败不触发发布，但要落 check_error 留痕；恢复后清除。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()
        self.db.add_project("a/b", "u", main_branch="main")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sha_failure_records_check_error(self):
        self.db.update_project("a/b", release_cfg={
            "mode": "yolo", "time_enabled": True, "interval_hours": 1})
        proj = self.db.get_project("a/b")
        rel.set_release_state(self.db, proj,
                              {"last_ts": time.time() - 2 * 3600, "last_sha": "old"})
        with patch.object(rel.gh, "latest_commit_sha",
                          side_effect=RuntimeError("gh 超时")):
            due = rel.check_due(self.db, proj)
        self.assertFalse(due["due"])                    # 查询失败 ≠ 有新提交
        st = rel.get_release_state(self.db, proj)
        self.assertIn("check_error", st)                # 失败留痕
        # 恢复后：正常判定 + 旧 check_error 被清除
        with patch.object(rel.gh, "latest_commit_sha", return_value="new"):
            due = rel.check_due(self.db, proj)
        self.assertTrue(due["due"])
        self.assertNotIn("check_error", rel.get_release_state(self.db, proj))


class TestPausedMasterSwitch(unittest.TestCase):
    """总开关语义：paused 下 run_release 任何路径都不起跑——不发 gh 调用、
    不起 agent、不触发 workflow（零 token 零额度消耗）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()
        self.db.add_project("a/b", "u", main_branch="main")
        self.logs = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_global_paused_refuses_even_explicit_release_mode(self):
        """release_cfg.mode=yolo 显式设置时 resolve_mode 不回落全局——
        总开关必须无条件生效（全局直查兜底）。"""
        self.db.update_project("a/b", release_cfg={"mode": "yolo"})
        self.db.kv_set("global_mode", "paused")
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows") as mw:
            r = rel.run_release(proj, {}, self.db, trigger="manual",
                                log_dir=self.logs)
        self.assertFalse(r["ok"])
        self.assertIn("paused", r["error"])
        mw.assert_not_called()          # 连 workflow 探测的 gh 调用都没发

    def test_project_paused_refuses(self):
        self.db.update_project("a/b", mode="paused",
                               release_cfg={"mode": "yolo"})
        proj = self.db.get_project("a/b")
        with patch.object(rel, "list_release_workflows") as mw:
            r = rel.run_release(proj, {}, self.db, trigger="schedule",
                                log_dir=self.logs)
        self.assertFalse(r["ok"])
        mw.assert_not_called()

    def test_resume_after_pause_recovers(self):
        """恢复档位后同一调用正常走通（开关可逆，不残留状态）。"""
        self.db.update_project("a/b", release_cfg={"mode": "dry"})
        self.db.kv_set("global_mode", "paused")
        proj = self.db.get_project("a/b")
        r = rel.run_release(proj, {}, self.db, trigger="manual",
                            log_dir=self.logs)
        self.assertFalse(r["ok"])
        self.db.kv_set("global_mode", "dry")
        with patch.object(rel, "list_release_workflows",
                          return_value=[{"name": "release", "path": "x"}]):
            r = rel.run_release(proj, {}, self.db, trigger="manual",
                                log_dir=self.logs)
        self.assertTrue(r["ok"] and r["dry"])


class TestReleaseAgentTamper(unittest.TestCase):
    """spec H 验收：release agent（写权限）改动 .git 元数据 → 中止，不落地。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.tmp, "bot.db"))
        self.db.init()
        self.db.add_project("a/b", "u", main_branch="main")
        self.logs = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _fake_clone(url, dest, branch="main", **kw):
        import subprocess
        os.makedirs(dest, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=dest, check=True)

    def test_release_run_agent_meta_tamper_aborts(self):
        self.db.update_project("a/b", release_cfg={"mode": "semi"})
        proj = self.db.get_project("a/b")

        def evil_agent(stage, sandbox_dir, issue_dir, **kw):
            with open(os.path.join(sandbox_dir, ".git", "config"), "a") as f:
                f.write('\n[remote "evil"]\n\turl = https://evil/x.git\n')
            return _R(True, {"version": "v1.0.0"})

        with patch.object(rel, "list_release_workflows", return_value=[]), \
             patch.object(rel, "_read_release_md", return_value="# Release\n步骤"), \
             patch.object(rel.sandbox, "clone", side_effect=self._fake_clone), \
             patch.object(rel.llm, "run_stage", side_effect=evil_agent), \
             patch.object(rel, "_needs_human") as mh:
            r = rel.run_release(proj, {}, self.db, trigger="manual",
                                log_dir=self.logs)
        self.assertFalse(r["ok"])
        self.assertIn("元数据", r["error"])
        mh.assert_called_once()

    def test_release_fix_agent_meta_tamper_aborts(self):
        proj = self.db.get_project("a/b")

        def evil_agent(stage, sandbox_dir, issue_dir, **kw):
            with open(os.path.join(sandbox_dir, ".git", "config"), "a") as f:
                f.write("\n[core]\n\thooksPath = .evil-hooks\n")
            return _R(True, {"files": ["workflow.yml"]})

        with patch.object(rel.sandbox, "clone", side_effect=self._fake_clone), \
             patch.object(rel.llm, "run_stage", side_effect=evil_agent):
            r = rel._fix_workflow_failure(proj, {}, "a/b", {"name": "wf"},
                                          self.logs, 1)
        self.assertFalse(r["ok"])
        self.assertIn("元数据", " ".join(r["steps"]))


if __name__ == "__main__":
    unittest.main()
