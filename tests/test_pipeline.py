"""pipeline 状态机测试：mock LLM 与 gh，沙箱走本地 bare git。覆盖：
accept→done（semi/dry）、reject、门禁返工、终审返工、终审超限 needs_human。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm
import llm_direct
import pipeline as pl
import sandbox


def R(ok, data=None, errors=None, sid="ses_x"):
    r = llm.LLMResult()
    r.ok, r.data, r.errors, r.session_id = ok, data, errors or [], sid
    return r


TRIAGE_ACCEPT = {"verdict": "accept", "issue_type": "bug", "reasons": ["确认存在"],
                 "minimal_plan": "改 app.py 加一行", "files_hint": ["app.py"], "confidence": 90}
TRIAGE_REJECT = {"verdict": "reject", "issue_type": "invalid", "reasons": ["不成立"],
                 "minimal_plan": "", "files_hint": [], "confidence": 70}
TRIAGE_NEED_INFO = {"verdict": "need_info", "issue_type": "bug",
                    "reasons": ["已确认与点击分支相关，但缺少环境信息"],
                    "minimal_plan": "", "files_hint": [],
                    "questions": ["使用的系统版本？", "能否附上完整日志？"],
                    "confidence": 40}
FIX_OK = {"changed_files": ["app.py"], "summary": "修复了打印", "self_check": "逻辑推演"}
REVIEW_OK = {"pass": True, "issues": [], "notes": "ok"}
FINAL_PASS = {"verdict": "pass", "fail_reasons": [],
              "reply_to_author": "感谢反馈！问题确认并已修复，请见 PR。", "suggested_label": "已修复"}
FINAL_FAIL = {"verdict": "fail", "fail_reasons": ["app.py 第 2 行逻辑不对"],
              "reply_to_author": "", "suggested_label": ""}

ISSUE = {"number": 1, "title": "打印错误", "body": "print 输出少了 fixed",
         "author": {"login": "alice"}, "comments": []}


def git(args, cwd=None):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.origin = os.path.join(self.tmp, "origin.git")
        git(["init", "--bare", "-b", "main", self.origin])
        seed = os.path.join(self.tmp, "seed")
        os.makedirs(seed)
        git(["init", "-b", "main", seed])
        with open(os.path.join(seed, "app.py"), "w") as f:
            f.write("print('hi')\n")
        git(["add", "-A"], cwd=seed)
        git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"], cwd=seed)
        git(["remote", "add", "origin", self.origin], cwd=seed)
        git(["push", "-u", "origin", "main"], cwd=seed)

        self.state_dir = os.path.join(self.tmp, "state")
        self._orig_state = state_mod_paths()
        set_state_dir(self.state_dir)
        # 多项目隔离（v3.1）：明确建 test/repo 项目行，compat 视角随之解析到它
        state_mod._db().add_project("test/repo", f"file://{self.origin}")

        self.cfg = {
            "repo": "test/repo", "repo_url": f"file://{self.origin}",
            "model": "volcengine/glm-5.3-flash", "poll_interval_sec": 0,
            "build_cmd": ["python3", "-m", "py_compile", "app.py"],
            "main_branch": "main", "issue_time_budget_sec": 600,
            "sandbox_root": os.path.join(self.tmp, "sb"),
            "max_json_retries": 2, "max_rework_loops": 2,
        }
        self.gh = MagicMock()
        self.gh.get_issue.return_value = ISSUE
        self.gh.create_pr.return_value = "https://github.com/test/repo/pull/1"
        self.gh.issue_text.return_value = "标题: 打印错误\n\nprint 输出少了 fixed"
        self.p = pl.Pipeline(self.cfg, log_dir=os.path.join(self.tmp, "logs"))
        self._patches = []
        # 直连环节 mock（audit / reply）；测试可覆盖这三个属性
        self._direct_audit = {"verdict": "ok", "category": "safe", "complexity": "complex",
                              "reason": "测试", "reject_reply": ""}
        self._direct_audit_ok = True
        self._direct_reply = "感谢反馈，问题已修复。"
        self.direct_calls = {"audit": 0, "reply": 0}
        for target, side in (("available", lambda cfg: True),
                             ("chat_json", self._fake_audit),
                             ("chat", self._fake_reply)):
            p = patch.object(pl.llm_direct, target, side_effect=side)
            self._patches.append(p)
            p.start()

    def _fake_audit(self, messages, cfg, parse=None, validate=None, **kw):
        self.direct_calls["audit"] += 1
        r = llm_direct.DirectResult()
        if self._direct_audit_ok:
            r.data = dict(self._direct_audit)
            r.ok, r.content = True, json.dumps(r.data)
            if validate:
                errs = validate(r.data)
                if errs:
                    r.ok, r.errors = False, errs
        else:
            r.ok, r.errors = False, ["mock 审核失败"]
        r.usage = {"total_tokens": 10}
        return r

    def _fake_reply(self, messages, cfg, **kw):
        self.direct_calls["reply"] += 1
        r = llm_direct.DirectResult()
        r.ok, r.content = True, self._direct_reply
        r.usage = {"total_tokens": 20}
        return r

    def tearDown(self):
        for p in self._patches:
            p.stop()
        restore_state_dir(self._orig_state)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch_gh(self):
        p = patch.object(pl, "gh", self.gh)
        self._patches.append(p)
        p.start()

    def patch_llm(self, stage_seqs: dict, reviews_result=None):
        """stage_seqs: {stage: [R(...), ...]} 按调用次数依次出队，之后重复最后一个。"""
        counters = {}

        import copy

        def fake_run_stage(stage, sandbox_dir, issue_dir, session_id=None,
                           inputs=None, cfg=None, timeout=None, progress=None):
            i = counters[stage] = counters.get(stage, 0) + 1
            if stage not in stage_seqs:  # 未配置的 stage 给通用成功
                defaults = {"fix_simple": FIX_OK, "review_simple": REVIEW_OK}
                r = copy.deepcopy(R(True, dict(defaults.get(stage, {}))))
            else:
                seq = stage_seqs[stage]
                r = copy.deepcopy(seq[min(i - 1, len(seq) - 1)])
            # 模拟 agent 真实改文件（否则门禁"无改动"会卡住）
            if stage in ("fix", "fix_simple") and sandbox_dir and os.path.isdir(sandbox_dir):
                with open(os.path.join(sandbox_dir, "app.py"), "a") as f:
                    f.write(f"print('fixed-{i}')\n")
            return r

        p1 = patch.object(pl.llm, "run_stage", side_effect=fake_run_stage)
        p2 = patch.object(pl.llm, "run_reviews",
                          return_value=reviews_result or {s: R(True, dict(REVIEW_OK))
                                                          for s in pl.REVIEW_STAGES})
        self._patches += [p1, p2]
        p1.start()
        p2.start()

    def set_mode(self, mode):
        # 全局档位唯一权威是 kv（save() 已不写 kv，防快照回滚）——
        # 与 /api/mode 实际写入路径一致
        state_mod._db().kv_set("global_mode", mode)


# state 路径切换辅助（state.py 用模块级常量）
import state as state_mod


def state_mod_paths():
    return state_mod.STATE_PATH, state_mod.STATE_DIR, state_mod.LOCK_PATH, state_mod.PID_PATH


def set_state_dir(d):
    state_mod.STATE_DIR = d
    state_mod.STATE_PATH = os.path.join(d, "state.json")
    state_mod.LOCK_PATH = os.path.join(d, "bot.lock")
    state_mod.PID_PATH = os.path.join(d, "bot.pid")


def restore_state_dir(t):
    state_mod.STATE_PATH, state_mod.STATE_DIR, state_mod.LOCK_PATH, state_mod.PID_PATH = t


class TestPipeline(Base):
    def test_accept_full_flow_semi(self):
        self.set_mode("semi")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)],
                        "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        stage = self.p.process(1)
        self.assertEqual(stage, "done")
        # semi：triage 评论 + 终审回复 = 2 次评论；PR 创建 1 次
        self.assertEqual(self.gh.comment_file.call_count, 2)
        self.gh.create_pr.assert_called_once()
        self.gh.remove_label.assert_any_call("test/repo", 1, "triage:needs-human")
        head_arg = self.gh.create_pr.call_args.kwargs.get("head") or self.gh.create_pr.call_args[0][1]
        self.assertEqual(head_arg, "fix/issue-1")
        st = state_mod.load()
        self.assertEqual(st["issues"]["1"]["stage"], "done")
        self.assertIn("s5", st["issues"]["1"]["session_ids"])
        # 阶段时间线（流程图数据源）：全链路 done（含新 audit/reply 环节）
        hist = st["issues"]["1"]["history"]
        self.assertEqual([h["stage"] for h in hist if h["status"] == "done"],
                         ["audit", "triage", "fix", "gate", "review", "final",
                          "reply", "execute"])
        # outbox 齐全
        out = os.path.join(self.p._issue_dir(1), "outbox")
        for f in ("triage-comment.md", "final-reply.md", "pr-body.md"):
            self.assertTrue(os.path.exists(os.path.join(out, f)), f)
        # 回复含披露头
        with open(os.path.join(out, "final-reply.md"), encoding="utf-8") as f:
            self.assertIn("🤖", f.read())

    def test_accept_dry_no_gh_writes(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        stage = self.p.process(2)
        self.assertEqual(stage, "done")
        self.gh.comment_file.assert_not_called()
        self.gh.create_pr.assert_not_called()
        self.gh.add_label.assert_not_called()

    def test_issue_downgrade_after_triage_comment_survives_worker_checkpoints(self):
        self.set_mode("yolo")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        db = state_mod._db()
        project = db.get_project("test/repo")
        self.gh.comment_file.side_effect = lambda *_: db.upsert_issue(project["id"], 71, mode="dry")
        self.assertEqual(self.p.process(71), "done")
        row = db.get_issue(project["id"], 71)
        self.assertEqual(row["mode"], "dry")
        self.assertTrue(row["patch_saved"])
        self.gh.comment_file.assert_called_once()
        self.gh.add_label.assert_not_called()
        self.gh.create_pr.assert_not_called()
        self.gh.merge_pr.assert_not_called()

    def test_yolo_fallback_rechecks_downgrade_after_merge_error(self):
        self.set_mode("yolo")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})

        def conflict(*args):
            self.set_mode("semi")
            raise RuntimeError("conflict")

        self.gh.merge_pr.side_effect = conflict
        self.assertEqual(self.p.process(72), "needs_human")
        row = state_mod.load_project("test/repo")["issues"]["72"]
        self.assertTrue(row["pr_url"])
        self.assertFalse(row["merged"])
        self.assertIn("semi", row["error"])
        self.gh.merge_pr.assert_called_once()

    def test_restart_requeues_local_work_and_flags_uncertain_writes(self):
        db = state_mod._db()
        pid = db.get_project("test/repo")["id"]
        db.upsert_issue(pid, 61, stage="review", mode="dry", verdict=TRIAGE_ACCEPT)
        db.upsert_issue(pid, 62, stage="execute", mode="paused", pr_url="https://example.org/pr/62")
        db.upsert_issue(pid, 63, stage="done", publish_status="running")
        state_mod.recover_interrupted(self.tmp)
        state_mod.recover_interrupted(self.tmp)
        row = db.get_issue(pid, 61)
        self.assertEqual((row["stage"], row["paused_from"], row["verdict"]),
                         ("pending", "review", TRIAGE_ACCEPT))
        row = db.get_issue(pid, 62)
        self.assertEqual((row["stage"], row["mode"], row["pr_url"]),
                         ("needs_human", "paused", "https://example.org/pr/62"))
        self.assertEqual(db.get_issue(pid, 63)["publish_status"], "error")

    def test_reject_semi(self):
        self.set_mode("semi")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_REJECT)]})
        stage = self.p.process(3)
        self.assertEqual(stage, "rejected")
        self.assertEqual(self.gh.comment_file.call_count, 1)
        self.gh.create_pr.assert_not_called()

    def test_build_rework_then_done(self):
        """第一次构建失败 → 续跑 fix → 第二次构建通过。"""
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        calls = {"n": 0}
        real_build = sandbox.run_build

        def flaky_build(cmd, cwd, env_extra=None, timeout=600):
            if "py_compile" not in " ".join(cmd):
                return real_build(cmd, cwd, env_extra, timeout)
            calls["n"] += 1
            if calls["n"] == 1:
                return 1, "SyntaxError"
            return real_build(cmd, cwd, env_extra, timeout)

        p = patch.object(pl.sandbox, "run_build", side_effect=flaky_build)
        self._patches.append(p)
        p.start()
        stage = self.p.process(4)
        self.assertEqual(stage, "done")
        st = state_mod.load()
        self.assertEqual(st["issues"]["4"]["retries"].get("gate"), 1)
        self.assertEqual(st["issues"]["4"]["session_ids"]["s1"], "ses_x")

    def test_final_fail_rework_then_done(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, dict(FINAL_FAIL)), R(True, dict(FINAL_PASS))]})
        stage = self.p.process(5)
        self.assertEqual(stage, "done")
        st = state_mod.load()
        self.assertEqual(st["issues"]["5"]["retries"].get("final_fail"), 1)
        # spec E 验收：终审返工后的新 diff 必须替换旧 diff——fix.patch
        # 应含第二次 fix（fixed-2），而非只有第一次被否决的 fixed-1
        patch_path = os.path.join(self.p._issue_dir(5), "outbox", "fix.patch")
        with open(patch_path) as f:
            patch_text = f.read()
        self.assertIn("fixed-2", patch_text)
        self.assertEqual(pl.llm.run_reviews.call_count, 2)
        with open(os.path.join(self.p._issue_dir(5), "review-snapshot.json")) as f:
            snapshot = json.load(f)
        self.assertEqual(snapshot["diff"], patch_text)
        self.assertEqual(set(snapshot["reviews"]), set(pl.REVIEW_STAGES))

    def test_final_fail_exhausted_needs_human(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, dict(FINAL_FAIL))]})
        self.cfg["max_rework_loops"] = 1
        stage = self.p.process(6)
        self.assertEqual(stage, "needs_human")
        st = state_mod.load()
        self.assertIn("超限", st["issues"]["6"]["error"])

    def test_review_json_persisted_for_final(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        self.p.process(7)
        st = state_mod.load()
        rj = st["issues"]["7"].get("review_json", {})
        self.assertEqual(set(rj.keys()), set(pl.REVIEW_STAGES))

    def test_failed_review_lane_keeps_other_opinions_and_blocks_final(self):
        self.set_mode("dry")
        self.patch_gh()
        reviews = {lane: R(True, REVIEW_OK) for lane in pl.REVIEW_STAGES}
        reviews["review_style"] = R(False, errors=["queue timeout"])
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)]}, reviews)
        self.assertEqual(self.p.process(76), "needs_human")
        with open(os.path.join(self.p._issue_dir(76), "review-snapshot.json")) as handle:
            snapshot = json.load(handle)
        self.assertEqual(set(snapshot["reviews"]), {"review_bug", "review_minimal"})
        self.assertEqual(snapshot["errors"], {"review_style": ["queue timeout"]})
        self.assertFalse(any(call.args[0] == "final" for call in pl.llm.run_stage.call_args_list))

    def test_reentry_guard(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_REJECT)]})
        self.p.process(8)
        stage = self.p.process(8)  # 再次处理同一 issue 应被防重入挡下
        self.assertEqual(stage, "rejected")

    def test_needs_human_on_llm_fail(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(False, None, ["输出不是合法的 JSON 对象"])]})
        stage = self.p.process(9)
        self.assertEqual(stage, "needs_human")

    def test_session_cleanup_on_done(self):
        """done 后按 config.cleanup_sessions 删除对应 session。"""
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        deleted = []
        p = patch.object(pl.llm, "delete_session",
                         side_effect=lambda sid: deleted.append(sid) or True)
        self._patches.append(p)
        p.start()
        self.cfg["cleanup_sessions"] = ["s1", "s5"]  # 只删 s1/s5
        stage = self.p.process(10)
        self.assertEqual(stage, "done")
        self.assertEqual(deleted, ["ses_x", "ses_x"])  # s1 与 s5 各删一次
        st = state_mod.load()
        self.assertEqual(st["issues"]["10"]["stage"], "done")

    def test_no_cleanup_when_disabled(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_REJECT)]})
        p = patch.object(pl.llm, "delete_session", side_effect=AssertionError("不应删除"))
        self._patches.append(p)
        p.start()
        self.cfg["cleanup_sessions"] = []    # 关闭
        stage = self.p.process(11)
        self.assertEqual(stage, "rejected")

    def test_time_budget_stops_before_fix(self):
        """时间预算必须使用全局 budget（回归：曾用新建 Timer 导致预算恒不触发）。"""
        self.set_mode("dry")
        self.patch_gh()
        calls = []

        def fake_run_stage(stage, sandbox_dir, issue_dir, session_id=None,
                           inputs=None, cfg=None, timeout=None, progress=None):
            calls.append(stage)
            if stage == "triage":
                return R(True, dict(TRIAGE_ACCEPT))
            raise AssertionError(f"预算超限后不应到达 {stage}")

        p1 = patch.object(pl.llm, "run_stage", side_effect=fake_run_stage)
        self._patches.append(p1)
        p1.start()
        self.cfg["issue_time_budget_sec"] = 0  # 任何耗时都算超限
        stage = self.p.process(12)
        self.assertEqual(stage, "needs_human")
        self.assertEqual(calls, ["triage"])   # fix 从未被调用
        st = state_mod.load()
        self.assertIn("时间预算", st["issues"]["12"]["error"])

    def test_simple_flow_done(self):
        """审核判 simple：走简单线（fix_simple → gate → review_simple → 直连回复 → execute）。"""
        self.set_mode("dry")
        self.patch_gh()
        self._direct_audit = {"verdict": "ok", "category": "safe", "complexity": "simple",
                              "reason": "简单问题", "reject_reply": ""}
        self.patch_llm({"fix_simple": [R(True, FIX_OK)],
                        "review_simple": [R(True, REVIEW_OK)]})
        stage = self.p.process(22)
        self.assertEqual(stage, "done")
        st = state_mod.load()
        e = st["issues"]["22"]
        self.assertEqual(set(e["session_ids"].keys()), {"s1", "s2"})  # 主线路仅 2 个 session
        self.assertEqual(e["complexity"], "simple")
        with open(os.path.join(self.p._issue_dir(22), "outbox", "final-reply.md"),
                  encoding="utf-8") as f:
            self.assertIn("感谢反馈", f.read())

    def test_audit_reject_flow(self):
        """审核 reject：发不处理回复 + triage:rejected，不进入修复。"""
        self.set_mode("semi")
        self.patch_gh()
        self._direct_audit = {"verdict": "reject", "category": "injection",
                              "complexity": "complex", "reason": "注入尝试",
                              "reject_reply": "该内容不在本项目处理范围。"}
        self.patch_llm({})
        stage = self.p.process(21)
        self.assertEqual(stage, "rejected")
        self.gh.comment_file.assert_called_once()
        self.gh.add_label.assert_any_call("test/repo", 21, "triage:rejected")
        self.gh.remove_label.assert_any_call("test/repo", 21, "triage:needs-human")
        st = state_mod.load()
        self.assertEqual(st["issues"]["21"]["stage"], "rejected")
        with open(os.path.join(self.p._issue_dir(21), "outbox", "final-reply.md"),
                  encoding="utf-8") as f:
            self.assertIn("该内容不在本项目处理范围", f.read())
        self.assertEqual(self.direct_calls["reply"], 0)  # 不再调用回复环节

    def test_audit_failure_needs_human(self):
        """审核调用失败（含纠偏重试仍失败）→ needs_human。"""
        self.set_mode("dry")
        self.patch_gh()
        self._direct_audit_ok = False
        stage = self.p.process(23)
        self.assertEqual(stage, "needs_human")
        st = state_mod.load()
        self.assertIn("问题审核失败", st["issues"]["23"]["error"])
        self.assertEqual(self.direct_calls["audit"], 2)  # 首发 + 一次纠偏

    def test_direct_not_configured_needs_human(self):
        """直连未配置 → needs_human 并提示配置。"""
        self.set_mode("dry")
        self.patch_gh()
        p = patch.object(pl.llm_direct, "available", return_value=False)
        self._patches.append(p)
        p.start()
        stage = self.p.process(24)
        self.assertEqual(stage, "needs_human")
        st = state_mod.load()
        self.assertIn("未配置", st["issues"]["24"]["error"])

    def test_validate_audit_fn(self):
        """audit schema 校验：合法通过；reject 缺回复/坏复杂度报错。"""
        self.assertEqual(pl._validate_audit({"verdict": "ok", "category": "safe",
                                             "complexity": "simple", "reason": "r",
                                             "reject_reply": ""}), [])
        errs = pl._validate_audit({"verdict": "reject", "category": "spam",
                                   "complexity": "simple", "reason": "r",
                                   "reject_reply": " "})
        self.assertTrue(any("reject_reply" in e for e in errs))
        errs2 = pl._validate_audit({"verdict": "ok", "category": "safe",
                                    "complexity": "wat", "reason": "r", "reject_reply": ""})
        self.assertTrue(any("complexity" in e for e in errs2))

    def test_yolo_auto_merge(self):
        """yolo：PR 自动 squash 合并；回复注明已合并；entry.merged=True。"""
        self.set_mode("yolo")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        stage = self.p.process(25)
        self.assertEqual(stage, "done")
        self.gh.merge_pr.assert_called_once_with("test/repo", 1)
        st = state_mod.load()
        self.assertTrue(st["issues"]["25"]["merged"])
        with open(os.path.join(self.p._issue_dir(25), "outbox", "final-reply.md"),
                  encoding="utf-8") as f:
            self.assertIn("已自动合并", f.read())

    def test_yolo_merge_failure_needs_human(self):
        """yolo：合并失败 → needs_human（PR 链接保留），回复不发出。"""
        self.set_mode("yolo")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        self.gh.merge_pr.side_effect = RuntimeError("conflict")
        stage = self.p.process(26)
        self.assertEqual(stage, "needs_human")
        st = state_mod.load()
        self.assertIn("自动合并失败", st["issues"]["26"]["error"])
        self.assertIn("pull/1", st["issues"]["26"].get("pr_url") or "")
        self.assertEqual(self.gh.comment_file.call_count, 1)  # 仅 triage 评论，回复未发

    def test_disclosure_can_be_disabled(self):
        """披露头可关：关闭后回复不含 🤖 披露行。"""
        self.set_mode("dry")
        self.patch_gh()
        self.cfg["disclosure_enabled"] = False
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        stage = self.p.process(27)
        self.assertEqual(stage, "done")
        with open(os.path.join(self.p._issue_dir(27), "outbox", "final-reply.md"),
                  encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("🤖", content)
        self.assertIn("感谢反馈", content)  # mock 回复本体仍在

    def test_publish_requires_conditions(self):
        """补发前置条件：dry / 缺 patch 一律拒绝。"""
        self.set_mode("dry")
        self.patch_gh()
        st = state_mod.load()
        e = state_mod.issue_entry(st, 12)
        e["stage"] = "done"
        state_mod.save(st)
        r = self.p.publish(12)
        self.assertFalse(r["ok"])
        self.assertIn("dry", r["error"])
        self.set_mode("paused")
        r = self.p.publish(12)
        self.assertFalse(r["ok"])
        self.assertIn("paused", r["error"])
        self.set_mode("semi")
        r = self.p.publish(12)
        self.assertFalse(r["ok"])
        self.assertIn("fix.patch", r["error"])

    def test_publish_patch_replay_semi(self):
        """补发核心：patch 重放（真 git 沙箱）→ 门禁复验 → PR → 评论（semi 不合并）。"""
        self.set_mode("semi")
        self.patch_gh()
        st = state_mod.load()
        e = state_mod.issue_entry(st, 13)
        e["stage"] = "done"
        e["summary"] = "修复测试"
        state_mod.save(st)
        outbox = os.path.join(self.p._issue_dir(13), "outbox")
        os.makedirs(outbox, exist_ok=True)
        with open(os.path.join(outbox, "triage-comment.md"), "w") as f:
            f.write("分诊评论")
        with open(os.path.join(outbox, "final-reply.md"), "w") as f:
            f.write("回复正文")
        # 生成真实 patch：clone origin → 改 app.py → staged diff
        tmp_clone = os.path.join(self.tmp, "patchgen")
        sandbox.clone(f"file://{self.origin}", tmp_clone, "main")
        with open(os.path.join(tmp_clone, "app.py"), "a") as f:
            f.write("print('published-fix')\n")
        git(["add", "-A"], cwd=tmp_clone)
        diff = subprocess.run(["git", "diff", "--cached"], cwd=tmp_clone,
                              capture_output=True, text=True).stdout
        with open(os.path.join(outbox, "fix.patch"), "w") as f:
            f.write(diff)
        import hashlib
        with open(os.path.join(outbox, "fix.patch.sha256"), "w") as f:
            f.write(hashlib.sha256(diff.encode()).hexdigest())
        # 执行补发
        r = self.p.publish(13)
        self.assertTrue(r["ok"], r)
        self.gh.create_pr.assert_called_once()
        self.gh.merge_pr.assert_not_called()          # semi：不自动合并
        self.assertEqual(self.gh.comment_file.call_count, 2)   # triage + reply
        # 分支已推到本地 origin
        out = subprocess.run(["git", "rev-parse", "fix/issue-13"], cwd=self.origin,
                             capture_output=True, text=True).stdout.strip()
        self.assertTrue(out)
        st = state_mod.load()
        self.assertIn("pull/1", st["issues"]["13"].get("pr_url") or "")
        with open(os.path.join(outbox, "final-reply.md")) as f:
            self.assertIn("🔗", f.read())             # 尾注补上了 PR 链接

    def _stage_publishable_patch(self, n: int, with_sha: bool = True) -> str:
        """造一个可补发的 issue：done + fix.patch（+sha256 副署）。"""
        st = state_mod.load()
        e = state_mod.issue_entry(st, n)
        e["stage"] = "done"
        state_mod.save(st)
        outbox = os.path.join(self.p._issue_dir(n), "outbox")
        os.makedirs(outbox, exist_ok=True)
        diff = "x" * 64
        with open(os.path.join(outbox, "fix.patch"), "w") as f:
            f.write(diff)
        if with_sha:
            import hashlib
            with open(os.path.join(outbox, "fix.patch.sha256"), "w") as f:
                f.write(hashlib.sha256(diff.encode()).hexdigest())
        return outbox

    def test_publish_rejects_tampered_patch(self):
        """spec E 验收：patch 与 sha256 副署不匹配（被篡改）→ 拒绝补发。"""
        self.set_mode("semi")
        self.patch_gh()
        outbox = self._stage_publishable_patch(14)
        with open(os.path.join(outbox, "fix.patch"), "a") as f:
            f.write("恶意追加\n")
        r = self.p.publish(14)
        self.assertFalse(r["ok"])
        self.assertIn("sha256", r["error"])
        self.gh.create_pr.assert_not_called()

    def test_publish_rejects_missing_sha(self):
        """spec E 验收：sha256 副署缺失（删除副署也是篡改手段）→ 拒绝补发。"""
        self.set_mode("semi")
        self.patch_gh()
        self._stage_publishable_patch(15, with_sha=False)
        r = self.p.publish(15)
        self.assertFalse(r["ok"])
        self.assertIn("sha256", r["error"])
        self.gh.create_pr.assert_not_called()

    def test_paused_at_boundary(self):
        """运行中切 paused：环节边界停下（stage 回 pending + paused_from 记录）。"""
        self.set_mode("dry")
        self.patch_gh()

        def fake_triage_then_pause(stage, sandbox_dir, issue_dir, session_id=None,
                                   inputs=None, cfg=None, timeout=None, progress=None):
            if stage == "triage":
                # 模拟用户运行中切 paused：实际写入路径是 kv（/api/mode），
                # save() 已不写 kv（防快照回滚）
                state_mod._db().kv_set("global_mode", "paused")
                return R(True, dict(TRIAGE_ACCEPT))
            raise AssertionError("不应到达 " + stage)

        p = patch.object(pl.llm, "run_stage", side_effect=fake_triage_then_pause)
        self._patches.append(p)
        p.start()
        stage = self.p.process(30)
        self.assertEqual(stage, "pending")
        st = state_mod.load()
        self.assertEqual(st["issues"]["30"]["stage"], "pending")
        self.assertEqual(st["issues"]["30"].get("paused_from"), "triage")
        # spec1-C：检测到 paused 后的 save_issue 不得把 global_mode 写回旧值
        self.assertEqual(state_mod._db().kv_get("global_mode"), "paused")

    def test_merge_fix_conflict_resolution(self):
        """merge 修复：真实冲突 → agent（mock）解决 → push → 重试合并成功。"""
        self.set_mode("yolo")
        self.patch_gh()
        st = state_mod.load()
        e = state_mod.issue_entry(st, 40)
        e["stage"] = "done"
        e["pr_url"] = "https://github.com/test/repo/pull/1"
        e["branch"] = "fix/issue-40"
        state_mod.save(st)
        # 造冲突：分支加一行，main 也加同一区域
        g = os.path.join(self.tmp, "mf")
        sandbox.clone(f"file://{self.origin}", g, "main")
        git(["checkout", "-b", "fix/issue-40"], cwd=g)
        with open(os.path.join(g, "app.py"), "a") as f:
            f.write("print('fix-side')\n")
        git(["add", "-A"], cwd=g)
        git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "fix"], cwd=g)
        git(["push", "-u", "origin", "fix/issue-40"], cwd=g)
        git(["checkout", "main"], cwd=g)
        with open(os.path.join(g, "app.py"), "a") as f:
            f.write("print('main-side')\n")
        git(["add", "-A"], cwd=g)
        git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "m2"], cwd=g)
        git(["push", "origin", "main"], cwd=g)

        def fake_merge_agent(stage, sandbox_dir, issue_dir, session_id=None,
                             inputs=None, cfg=None, timeout=None, progress=None):
            if stage == "merge_fix":
                with open(os.path.join(sandbox_dir, "app.py"), "w") as f:
                    f.write("print('hi')\nprint('main-side')\nprint('fix-side')\n")
                return R(True, {"resolved": True, "summary": "两行都保留", "files": ["app.py"]})
            raise AssertionError("不应到达 " + stage)

        p = patch.object(pl.llm, "run_stage", side_effect=fake_merge_agent)
        self._patches.append(p)
        p.start()
        result = self.p.merge_fix(40)
        self.assertTrue(result["ok"], result)
        self.gh.merge_pr.assert_called_once()
        st = state_mod.load()
        self.assertTrue(st["issues"]["40"]["merged"])

    def test_init_project_refused_when_paused(self):
        """总开关：paused 下 init_project 不起 agent——零 token 零外写。"""
        self.set_mode("paused")
        self.patch_gh()
        with patch.object(pl.llm, "run_stage") as m:
            r = self.p.init_project(landing="pr")
        self.assertFalse(r["ok"])
        self.assertIn("paused", r["error"])
        m.assert_not_called()           # 连 agent 都没起

    def test_merge_fix_refused_in_dry_and_paused(self):
        """spec F 验收：merge_fix 做真实 push/merge，dry/paused 下必须在入口拒绝。"""
        self.patch_gh()
        st = state_mod.load()
        e = state_mod.issue_entry(st, 41)
        e["stage"] = "done"
        e["pr_url"] = "https://github.com/test/repo/pull/41"
        state_mod.save(st)
        for mode in ("dry", "paused"):
            self.set_mode(mode)
            r = self.p.merge_fix(41)
            self.assertFalse(r["ok"], mode)
            self.assertIn(mode, r["error"])
            self.gh.merge_pr.assert_not_called()

    def test_init_project_pr_landing(self):
        """初始化 agent：生成 AGENTS.md + workflow → PR 落地（mock agent + 真实 git 沙箱）。"""
        self.patch_gh()

        def fake_init(stage, sandbox_dir, issue_dir, session_id=None, inputs=None,
                      cfg=None, timeout=None, progress=None):
            if stage == "init_project":
                with open(os.path.join(sandbox_dir, "AGENTS.md"), "w") as f:
                    f.write("# AGENTS.md\n测试项目导览")
                wf = os.path.join(sandbox_dir, ".github", "workflows")
                os.makedirs(wf, exist_ok=True)
                with open(os.path.join(wf, "release.yml"), "w") as f:
                    f.write("name: release\non: {workflow_dispatch: {}}\n")
                return R(True, {"agents_md": "created", "release_scheme": "workflow",
                                "files": ["AGENTS.md", ".github/workflows/release.yml"],
                                "summary": "测试生成"})
            raise AssertionError("不应到达 " + stage)

        p = patch.object(pl.llm, "run_stage", side_effect=fake_init)
        self._patches.append(p)
        p.start()
        r = self.p.init_project(landing="pr")
        self.assertTrue(r["ok"], r)
        self.gh.create_pr.assert_called_once()
        self.gh.merge_pr.assert_not_called()
        out = subprocess.run(["git", "branch"], cwd=self.origin,
                             capture_output=True, text=True).stdout
        self.assertIn("chore/init-bot", out)

    def test_build_env_passed_to_gate(self):
        """build_env 应透传给门禁构建（如 ANDROID_HOME/JAVA_HOME）。"""
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        self.cfg["build_env"] = {"ANDROID_HOME": "/fake/sdk", "JAVA_HOME": "/fake/jdk"}
        captured = {}
        real_build = sandbox.run_build

        def spy(cmd, cwd, env_extra=None, timeout=600):
            captured["env_extra"] = env_extra
            return real_build(cmd, cwd, env_extra, timeout)

        p = patch.object(pl.sandbox, "run_build", side_effect=spy)
        self._patches.append(p)
        p.start()
        stage = self.p.process(14)
        self.assertEqual(stage, "done")
        self.assertEqual(captured.get("env_extra"),
                         {"ANDROID_HOME": "/fake/sdk", "JAVA_HOME": "/fake/jdk"})


class TestNeedsInfo(Base):
    """v3.1 needs-info 回环：triage 三档 → waiting_info → 作者回复自动重审。"""

    def test_need_info_semi_posts_and_waits(self):
        self.set_mode("semi")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_NEED_INFO)]})
        stage = self.p.process(1)
        self.assertEqual(stage, "waiting_info")
        # semi：追问评论发出 + 标签；无 PR、无其它评论
        self.gh.comment_file.assert_called_once()
        self.gh.add_label.assert_called_once_with("test/repo", 1, "triage:needs-info")
        self.gh.create_pr.assert_not_called()
        st = state_mod.load()
        e = st["issues"]["1"]
        self.assertEqual(e["stage"], "waiting_info")
        self.assertTrue(e["info_posted"])
        self.assertEqual(e["info_seen_comments"], len(ISSUE["comments"]))
        self.assertEqual(e["retries"]["info_rounds"], 1)
        # 追问文件：@作者 + 问题 + 披露头
        with open(os.path.join(self.p._issue_dir(1), "outbox", "needs-info.md"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("@alice", body)
        self.assertIn("使用的系统版本？", body)
        self.assertIn("🤖", body)

    def test_need_info_dry_preview_only(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_NEED_INFO)]})
        stage = self.p.process(2)
        self.assertEqual(stage, "waiting_info")
        self.gh.comment_file.assert_not_called()
        self.gh.add_label.assert_not_called()
        st = state_mod.load()
        self.assertFalse(st["issues"]["2"]["info_posted"])
        self.assertTrue(os.path.exists(
            os.path.join(self.p._issue_dir(2), "outbox", "needs-info.md")))

    def test_need_info_rounds_exceeded_needs_human(self):
        self.set_mode("semi")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_NEED_INFO)]})
        st = state_mod.load()
        state_mod.issue_entry(st, 3)["retries"]["info_rounds"] = 2   # 已问过 2 轮
        state_mod.save_issue(st, 3)
        stage = self.p.process(3)
        self.assertEqual(stage, "needs_human")
        st = state_mod.load()
        self.assertIn("追问", st["issues"]["3"]["error"])
        self.gh.comment_file.assert_not_called()   # 第 3 轮不再发追问

    def test_check_waiting_reply_resets_to_pending(self):
        self.set_mode("semi")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_NEED_INFO)]})
        self.p.process(4)
        st = state_mod.load()
        seen = st["issues"]["4"]["info_seen_comments"]
        self.gh.get_issue.return_value = dict(
            ISSUE, comments=[{"author": {"login": "alice"}, "body": "版本 33"}] * (seen + 1))
        reset = self.p.check_waiting()
        self.assertEqual(reset, [4])
        st = state_mod.load()
        self.assertEqual(st["issues"]["4"]["stage"], "pending")
        self.assertEqual(st["issues"]["4"]["retries"]["info_rounds"], 1)   # 轮数保留
        self.assertEqual(self.p.check_waiting(), [])   # 已重置，不再重复触发

    def test_check_waiting_no_reply_no_action(self):
        self.set_mode("semi")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_NEED_INFO)]})
        self.p.process(5)
        self.assertEqual(self.p.check_waiting(), [])   # 评论数 == 基线 → 不动
        self.assertEqual(state_mod.load()["issues"]["5"]["stage"], "waiting_info")

    def test_check_waiting_repost_after_dry(self):
        """dry 期间无评论 → 切档补发追问；已有评论 → 直接回 pending 重审（spec1-L）。"""
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_NEED_INFO)]})
        self.p.process(6)
        self.assertFalse(state_mod.load()["issues"]["6"]["info_posted"])
        self.set_mode("semi")   # 切档 → watcher 补发
        # dry 期间已有作者评论 → 不补发，直接 pending 重审（回复不再被吞）
        self.gh.get_issue.return_value = dict(
            ISSUE, comments=[{"author": {"login": "alice"}, "body": "此前评论"}])
        self.assertEqual(self.p.check_waiting(), [6])
        self.gh.comment_file.assert_not_called()
        self.assertEqual(state_mod.load()["issues"]["6"]["stage"], "pending")

        # 无评论场景 → 补发追问，基线含刚发出的那条
        st = state_mod.load()
        e = state_mod.issue_entry(st, 6)
        e["stage"] = "waiting_info"
        e["info_posted"] = False
        state_mod.save_issue(st, 6)
        self.gh.get_issue.reset_mock()
        self.gh.comment_file.reset_mock()
        self.gh.add_label.reset_mock()
        self.gh.get_issue.side_effect = [
            dict(ISSUE, comments=[]),                                  # 首查：无评论
            dict(ISSUE, comments=[{"author": {"login": "bot"}}]),      # 补发后重抓
        ]
        self.assertEqual(self.p.check_waiting(), [])
        self.gh.comment_file.assert_called_once()
        self.gh.add_label.assert_called_once_with("test/repo", 6, "triage:needs-info")
        st = state_mod.load()
        self.assertTrue(st["issues"]["6"]["info_posted"])
        self.assertEqual(st["issues"]["6"]["info_seen_comments"], 1)   # 重抓基线

    def test_check_waiting_skips_dry_and_paused(self):
        self.set_mode("dry")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_NEED_INFO)]})
        self.p.process(7)
        self.gh.get_issue.reset_mock()
        self.assertEqual(self.p.check_waiting(), [])    # 全局 dry → 完全跳过
        self.gh.get_issue.assert_not_called()
        # issue 级 paused → 跳过
        self.set_mode("semi")
        st = state_mod.load()
        state_mod.issue_entry(st, 7)["mode"] = "paused"
        state_mod.save_issue(st, 7)
        self.gh.get_issue.reset_mock()
        self.assertEqual(self.p.check_waiting(), [])
        self.gh.get_issue.assert_not_called()

    def test_rejected_is_terminal_even_if_author_replies(self):
        self.set_mode("semi")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_REJECT)]})
        self.assertEqual(self.p.process(8), "rejected")
        self.gh.get_issue.reset_mock()
        # 作者再回复：watcher 不追踪 rejected，process 防重入也不复活
        self.gh.get_issue.return_value = dict(
            ISSUE, comments=[{"author": {"login": "alice"}, "body": "再说说"}])
        self.assertEqual(self.p.check_waiting(), [])
        self.assertEqual(self.p.process(8), "rejected")
        st = state_mod.load()
        self.assertEqual(st["issues"]["8"]["stage"], "rejected")

    def test_done_removes_needs_info_label(self):
        self.set_mode("semi")
        self.patch_gh()
        self.patch_llm({"triage": [R(True, TRIAGE_ACCEPT)], "fix": [R(True, FIX_OK)],
                        "final": [R(True, FINAL_PASS)]})
        self.assertEqual(self.p.process(9), "done")
        self.gh.remove_label.assert_any_call("test/repo", 9, "triage:needs-info")


if __name__ == "__main__":
    unittest.main()
