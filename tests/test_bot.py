"""bot.py Web API 冒烟测试：真起 HTTP server（随机端口），打 API 验证。"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as bot_mod
import state as state_mod
from util import atomic_write_json


class TestBotAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        # 状态/配置全部指到临时目录
        cls._orig = (state_mod.STATE_PATH, state_mod.STATE_DIR,
                     state_mod.LOCK_PATH, state_mod.PID_PATH)
        state_mod.STATE_DIR = cls.tmp
        state_mod.STATE_PATH = os.path.join(cls.tmp, "state.json")
        state_mod.LOCK_PATH = os.path.join(cls.tmp, "bot.lock")
        state_mod.PID_PATH = os.path.join(cls.tmp, "bot.pid")
        cls.cfg_path = os.path.join(cls.tmp, "config.json")
        atomic_write_json(cls.cfg_path, {"repo": "test/repo", "repo_url": "file:///x",
                                         "model": "volcengine/glm-5.3-flash",
                                         "web_port": 0, "build_cmd": ["true"],
                                         "poll_interval_sec": 0})
        cls._orig_cfg = bot_mod.CFG_PATH
        bot_mod.CFG_PATH = cls.cfg_path
        cls._orig_base = bot_mod.BASE
        bot_mod.BASE = cls.tmp  # 档案/logs 路径全部指向临时目录
        cls._orig_env_path = bot_mod.ENV_PATH
        bot_mod.ENV_PATH = os.path.join(cls.tmp, ".env")  # 环境凭据隔离（不读真实 .env）
        # 提示词目录隔离：防止测试（修改/恢复出厂）污染真实 prompts/active
        import prompts_cfg as pc
        cls._orig_prompts = (pc.ACTIVE_DIR, pc.DEFAULTS_DIR)
        tmp_prompts = os.path.join(cls.tmp, "prompts")
        pc.DEFAULTS_DIR = os.path.join(tmp_prompts, "defaults")
        pc.ACTIVE_DIR = os.path.join(tmp_prompts, "active")
        shutil.copytree(cls._orig_prompts[1], pc.DEFAULTS_DIR)

        cls.srv = bot_mod.ThreadingHTTPServer(("127.0.0.1", 0), bot_mod.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        bot_mod.CFG_PATH = cls._orig_cfg
        bot_mod.BASE = cls._orig_base
        bot_mod.ENV_PATH = cls._orig_env_path
        import prompts_cfg as pc
        pc.ACTIVE_DIR, pc.DEFAULTS_DIR = cls._orig_prompts
        state_mod.STATE_PATH, state_mod.STATE_DIR, state_mod.LOCK_PATH, state_mod.PID_PATH = cls._orig
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        data = json.loads(r.read().decode() or "{}")
        c.close()
        return r.status, data

    def test_review_endpoint_returns_one_snapshot_then_falls_back_to_legacy_patch(self):
        db = state_mod._db()
        repo = "demo/review"
        project = db.get_or_add_project(repo, "")
        reviews = {"review_bug": {"pass": True, "notes": "older revision", "issues": []}}
        db.upsert_issue(project["id"], 87, review_json=reviews)
        folder = bot_mod.issue_log_dir({}, 87, repo)
        os.makedirs(os.path.join(folder, "outbox"), exist_ok=True)
        with open(os.path.join(folder, "outbox", "fix.patch"), "w") as f:
            f.write("x" * 160005)
        url = f"/api/issue/87?repo={repo}&review=1"
        code, data = self.req("GET", url)
        self.assertEqual((code, data["truncated"], len(data["diff"])), (200, True, 160000))
        self.assertEqual(data["reviews"], reviews)
        current = {"diff": "new diff", "reviews": {}, "truncated": False}
        atomic_write_json(os.path.join(folder, "review-snapshot.json"), current)
        self.assertEqual(self.req("GET", url), (200, current))

    def test_overview_shape(self):
        code, d = self.req("GET", "/api/overview")
        self.assertEqual(code, 200)
        self.assertIn("global_mode", d)
        self.assertIn("issues", d)
        self.assertEqual(d["model"], "volcengine/glm-5.3-flash")

    def test_mode_switch(self):
        code, d = self.req("POST", "/api/mode", {"mode": "semi"})
        self.assertTrue(d["ok"])
        code, d = self.req("GET", "/api/overview")
        self.assertEqual(d["global_mode"], "semi")
        code, d = self.req("POST", "/api/mode", {"mode": "nope"})
        self.assertEqual(code, 400)

    def test_issue_mode_and_rerun(self):
        st = state_mod.load()
        e = state_mod.issue_entry(st, 11)
        e["stage"] = "done"
        e["retries"] = {"final_fail": 2, "gate": 1}  # 旧预算残留
        state_mod.save(st)
        code, d = self.req("POST", "/api/issue-mode", {"issue": 11, "mode": "full"})
        self.assertTrue(d["ok"])
        st = state_mod.load()
        self.assertEqual(st["issues"]["11"]["mode"], "full")
        code, d = self.req("POST", "/api/issue-mode", {"issue": 11, "mode": "wrong"})
        self.assertEqual(code, 400)
        # rerun 会起后台线程跑真流水线（mock 掉避免副作用）
        with patch.object(bot_mod.pl.Pipeline, "process", return_value="pending"):
            code, d = self.req("POST", "/api/rerun", {"issue": 11})
            self.assertTrue(d["ok"])
            self.assertTrue(d["triggered"])
            st = state_mod.load()
            self.assertEqual(st["issues"]["11"]["stage"], "pending")
            self.assertEqual(st["issues"]["11"]["retries"], {})  # 重跑重置重试预算
        # 防重入生效（该 issue 已在运行）时如实返回 triggered=False，不再假成功
        with patch.dict(bot_mod._running, {state_mod._compat_repo() + "#11": {}}):
            code, d = self.req("POST", "/api/rerun", {"issue": 11})
            self.assertTrue(d["ok"])
            self.assertFalse(d["triggered"])

    def test_interval(self):
        code, d = self.req("POST", "/api/interval", {"sec": 60})
        self.assertEqual(d["poll_interval_sec"], 60)

    def test_prompts_roundtrip(self):
        code, d = self.req("GET", "/api/prompts")
        self.assertEqual(code, 200)
        v0 = d["version"]
        self.assertIn("triage.system.md", d["files"])
        new_content = d["files"]["triage.system.md"] + "\n<!-- test edit -->"
        code, d = self.req("POST", "/api/prompts",
                           {"name": "triage.system.md", "content": new_content})
        self.assertTrue(d["ok"])
        self.assertEqual(d["version"], v0 + 1)
        code, d = self.req("POST", "/api/prompts", {"name": "evil.path", "content": "x"})
        self.assertEqual(code, 400)
        code, d = self.req("POST", "/api/prompts/reset", {})
        self.assertTrue(d["ok"])

    def test_file_traversal_blocked(self):
        code, _ = self.req("GET", "/api/file?path=/etc/passwd")
        self.assertEqual(code, 403)
        code, _ = self.req("GET", "/api/file?path=../../config.json")
        self.assertEqual(code, 403)

    def test_token_auth(self):
        """设置令牌后：无令牌 401，带 X-Bot-Token 或 ?token= 通过。"""
        old = bot_mod.Handler.token
        bot_mod.Handler.token = "test-token-xyz"
        try:
            code, _ = self.req("GET", "/api/overview")
            self.assertEqual(code, 401)
            c = HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", "/api/overview", headers={"X-Bot-Token": "test-token-xyz"})
            self.assertEqual(c.getresponse().status, 200)
            c.close()
            code, _ = self.req("GET", "/api/overview?token=test-token-xyz")
            self.assertEqual(code, 200)
            code, _ = self.req("GET", "/api/overview?token=wrong")
            self.assertEqual(code, 401)
        finally:
            bot_mod.Handler.token = old

    def test_cleanup_sessions_api(self):
        code, d = self.req("POST", "/api/cleanup-sessions", {"keys": ["s1", "s6", "s99"]})
        self.assertTrue(d["ok"])
        self.assertEqual(d["cleanup_sessions"], ["s1", "s6"])
        with open(bot_mod.CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["cleanup_sessions"], ["s1", "s6"])

    def test_env_roundtrip(self):
        """env API 往返；必须打在临时 .env 上，不得污染真实凭据。"""
        tmp_env = os.path.join(self.tmp, "test.env")
        self._orig_env = bot_mod.ENV_PATH
        bot_mod.ENV_PATH = tmp_env
        self.addCleanup(lambda: setattr(bot_mod, "ENV_PATH", self._orig_env))
        code, d = self.req("GET", "/api/env")
        self.assertEqual(code, 200)
        self.assertIn("BOT_MODEL", d["env"])
        code, d = self.req("POST", "/api/env/save",
                           {"BOT_MODEL": "volcengine/glm-5.3-flash",
                            "BOT_TOKEN": "newsecret", "BOT_AUTH_ENABLED": False})
        self.assertTrue(d["ok"])
        self.assertTrue(d["restart_required"])
        env = bot_mod.load_env()
        self.assertEqual(env["BOT_TOKEN"], "newsecret")
        self.assertEqual(env["BOT_AUTH_ENABLED"], "0")
        # 注：真实 .env 已被 setUpClass 全局隔离（bot_mod.ENV_PATH=tmp），无需额外防污染断言

    def test_env_masked_value_does_not_overwrite(self):
        """回归：GET 返回的部分打码值回传后不得覆盖真实凭据（含 `*` 即拒绝）。"""
        tmp_env = os.path.join(self.tmp, "test-masked.env")
        self._orig_env2 = bot_mod.ENV_PATH
        bot_mod.ENV_PATH = tmp_env
        self.addCleanup(lambda: setattr(bot_mod, "ENV_PATH", self._orig_env2))
        code, d = self.req("POST", "/api/env/save", {"BOT_TOKEN": "real-token-123456"})
        self.assertTrue(d["ok"])
        code, d = self.req("GET", "/api/env")
        masked = d["env"]["BOT_TOKEN"]
        self.assertIn("*", masked)
        self.assertNotEqual(masked, "real-token-123456")
        # 前端把打码回显原样回传 → 服务端必须忽略
        code, d = self.req("POST", "/api/env/save", {"BOT_TOKEN": masked})
        self.assertTrue(d["ok"])
        self.assertEqual(bot_mod.load_env()["BOT_TOKEN"], "real-token-123456")
        # 全 * 与含 * 的任意值同理拒绝；真实新值正常写入
        self.req("POST", "/api/env/save", {"BOT_TOKEN": "***"})
        self.assertEqual(bot_mod.load_env()["BOT_TOKEN"], "real-token-123456")
        self.req("POST", "/api/env/save", {"BOT_TOKEN": "brand-new"})
        self.assertEqual(bot_mod.load_env()["BOT_TOKEN"], "brand-new")

    def test_issue_detail_api(self):
        """/api/issue/<n> 返回 parsed 详情；档案含 dict 文件也能排序（回归）。"""
        st = state_mod.load()
        e = state_mod.issue_entry(st, 21)
        e["title"] = "测试"
        e["stage"] = "done"
        e["verdict"] = {"verdict": "accept", "issue_type": "bug", "reasons": ["r"],
                        "minimal_plan": "p", "files_hint": ["a.py"], "confidence": 90}
        e["fix_json"] = {"changed_files": ["a.py"], "summary": "s", "self_check": "c"}
        e["review_json"] = {"review_bug": {"pass": True, "issues": [], "notes": "n"}}
        e["final_json"] = {"verdict": "pass", "fail_reasons": [],
                           "reply_to_author": "R", "suggested_label": "L"}
        e["history"] = [{"stage": "triage", "status": "done", "ts": "t"}]
        state_mod.save(st)
        # 造档案目录（v3 项目分域路径；含两个文件，乱序创建以触发排序路径）
        logd = os.path.join(self.tmp, "logs", "projects", "test__repo", "issues", "21")
        os.makedirs(os.path.join(logd, "outbox"), exist_ok=True)
        for name in ("llm-triage-1.txt", "outbox/final-reply.md"):
            with open(os.path.join(logd, name), "w") as f:
                f.write("x")
        code, d = self.req("GET", "/api/issue/21")
        self.assertEqual(code, 200)
        self.assertEqual(d["parsed"]["triage"]["verdict"], "accept")
        self.assertEqual(d["parsed"]["fix"]["changed_files"], ["a.py"])
        self.assertIn("reply", d["parsed"])
        self.assertEqual(len(d["files"]), 2)  # dict 文件排序不抛 TypeError

    def test_auth_disabled_open(self):
        """auth_enabled=False 时无令牌也放行。"""
        old_t, old_a = bot_mod.Handler.token, bot_mod.Handler.auth_enabled
        bot_mod.Handler.token, bot_mod.Handler.auth_enabled = "whatever", False
        try:
            code, _ = self.req("GET", "/api/overview")
            self.assertEqual(code, 200)
        finally:
            bot_mod.Handler.token, bot_mod.Handler.auth_enabled = old_t, old_a

    def test_post_with_query_string(self):
        """POST 带 query string 仍可路由（回归：do_POST 需剥离 ?）。"""
        code, d = self.req("POST", "/api/interval?token=x&y=1", {"sec": 30})
        self.assertEqual(code, 200)
        self.assertEqual(d["poll_interval_sec"], 30)

    def test_wizard_apis(self):
        code, d = self.req("GET", "/api/wizard/status")
        self.assertEqual(code, 200)
        self.assertIn("done", d)
        code, d = self.req("GET", "/api/wizard/opencode")
        self.assertIn("installed", d)
        code, d = self.req("POST", "/api/wizard/complete", {})
        self.assertTrue(d["ok"])
        code, d = self.req("GET", "/api/wizard/status")
        self.assertTrue(d["done"])
        code, d = self.req("POST", "/api/wizard/reset", {})
        self.assertTrue(d["ok"])

    def test_models_requires_config(self):
        code, d = self.req("GET", "/api/models")
        self.assertEqual(code, 200)
        self.assertFalse(d["ok"])   # 未配置 → ok=false + 提示

    def test_projects_add(self):
        code, d = self.req("POST", "/api/projects/add", {"repo": "x/y", "intro": "t"})
        self.assertTrue(d["ok"])
        code, d = self.req("POST", "/api/projects/add", {"repo": "x/y"})
        self.assertEqual(code, 409)
        code, d = self.req("POST", "/api/projects/add", {"repo": "bad"})
        self.assertEqual(code, 400)
        code, d = self.req("GET", "/api/projects")
        self.assertTrue(any(p.get("repo") == "x/y" for p in d["projects"]))

    def test_project_update(self):
        code, d = self.req("POST", "/api/projects/add", {"repo": "u/v"})
        self.assertTrue(d["ok"])
        code, d = self.req("POST", "/api/project/update",
                           {"repo": "u/v", "intro": "测试项目", "mode": "semi",
                            "build_cmd": ["true"]})
        self.assertTrue(d["ok"])
        self.assertEqual(d["project"]["mode"], "semi")
        self.assertEqual(d["project"]["intro"], "测试项目")
        code, d = self.req("POST", "/api/project/update", {"repo": "nope/x"})
        self.assertEqual(code, 404)

    def test_stats_and_activity(self):
        code, d = self.req("GET", "/api/stats?days=14")
        self.assertEqual(code, 200)
        self.assertEqual(len(d["days"]), 14)
        self.assertIn("totals", d)
        code, d = self.req("GET", "/api/activity")
        self.assertEqual(code, 200)
        self.assertIn("events", d)
        self.assertIn("agent_gate", d)
        self.assertIn("running", d)
        code, d = self.req("GET", "/api/issues")
        self.assertEqual(code, 200)
        self.assertIn("issues", d)

    def test_dashboard_attention_classification(self):
        """待办聚合：needs_human / waiting / publishable / 在跑 各归其位。"""
        st = state_mod.load()
        e = state_mod.issue_entry(st, 901)
        e["stage"] = "needs_human"; e["error"] = "merge conflict"
        e = state_mod.issue_entry(st, 902)
        e["stage"] = "waiting_info"
        e["retries"] = {"info_waiting_since": time.time() - 3600}
        e = state_mod.issue_entry(st, 903)
        e["stage"] = "done"; e["patch_saved"] = True; e["pr_url"] = None
        e = state_mod.issue_entry(st, 904)
        e["stage"] = "fix"; e["history"] = [{"stage": "audit", "status": "done"}]
        state_mod.save(st)
        code, d = self.req("GET", "/api/dashboard")
        self.assertEqual(code, 200)
        kinds = {x["number"]: x["kind"] for x in d["attention"]}
        self.assertEqual(kinds.get(901), "needs_human")
        self.assertEqual(kinds.get(902), "waiting")
        self.assertEqual(kinds.get(903), "publishable")
        self.assertIn(904, [x["number"] for x in d["in_flight"]])
        self.assertTrue({901, 902, 903, 904}.issubset(x["number"] for x in d["board"]))
        self.assertIn("agent_gate", d)

    def test_sessions_api_and_delete(self):
        code, d = self.req("GET", "/api/sessions")
        self.assertEqual(code, 200)
        self.assertIn("sessions", d)
        with patch.object(bot_mod.llm, "delete_session", lambda sid: True):
            code, d = self.req("POST", "/api/sessions/delete",
                               {"session_ids": ["ses_x", "ses_y"]})
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["deleted"]), 2)
        self.assertTrue(d["deleted"][0]["oc2_deleted"])

    def test_agent_limit(self):
        code, d = self.req("GET", "/api/agent-limit")
        self.assertEqual(code, 200)
        code, d = self.req("POST", "/api/agent-limit", {"limit": 5})
        self.assertEqual(d["limit"], 5)
        code, d = self.req("GET", "/api/agent-limit")
        self.assertEqual(d["limit"], 5)
        self.req("POST", "/api/agent-limit", {"limit": 3})  # 恢复

    def test_log_filter(self):
        """日志 API：级别过滤 + issue 过滤（统一日志格式）。"""
        logd = os.path.join(self.tmp, "logs")
        os.makedirs(logd, exist_ok=True)
        from util import log_event as le
        le("info", "web", "ev_info_marker", log_dir=logd, issue=5)
        le("error", "pipeline", "ev_err_marker", log_dir=logd, issue=6)
        code, d = self.req("GET", "/api/log")
        self.assertEqual(code, 200)
        self.assertTrue(any("ev_info_marker" in x for x in d["lines"]))
        self.assertTrue(any("ev_err_marker" in x for x in d["lines"]))
        code, d = self.req("GET", "/api/log?level=error")
        self.assertTrue(any("ev_err_marker" in x for x in d["lines"]))
        self.assertFalse(any("ev_info_marker" in x for x in d["lines"]))
        code, d = self.req("GET", "/api/log?issue=5")
        self.assertTrue(all(json.loads(x).get("issue") == 5
                            for x in d["lines"] if x.strip()))
        code, d = self.req("GET", "/api/log?q=ev_err")
        self.assertTrue(any("ev_err_marker" in x for x in d["lines"]))
        self.assertFalse(any("ev_info_marker" in x for x in d["lines"]))

    def test_poll_lock_serializes(self):
        """并发「立即轮询」被锁串行化（连点竞态回归）。"""
        entered = []

        def slow_poll(self_):
            entered.append(1)
            time.sleep(0.6)
            return []

        with patch.object(bot_mod.pl.Pipeline, "poll", slow_poll):
            code, d = self.req("POST", "/api/poll", {})
            self.assertTrue(d["ok"])
            time.sleep(0.15)
            code, d = self.req("POST", "/api/poll", {})   # 应被锁挡住并如实返回 busy
            self.assertFalse(d["ok"])
            self.assertEqual(d["error"], "poll_busy")
            time.sleep(1.2)
        self.assertEqual(len(entered), 1)


if __name__ == "__main__":
    unittest.main()
