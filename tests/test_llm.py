import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm
from util import render


class TestParseJson(unittest.TestCase):
    def test_pure(self):
        self.assertEqual(llm.parse_json('{"a":1}'), {"a": 1})

    def test_with_fence(self):
        t = '```json\n{"a": 1, "b": "x"}\n```'
        self.assertEqual(llm.parse_json(t), {"a": 1, "b": "x"})

    def test_with_noise(self):
        t = '好的，以下是结果：\n{"verdict": "accept"}\n希望有帮助'
        self.assertEqual(llm.parse_json(t), {"verdict": "accept"})

    def test_newline_in_string_escaped(self):
        t = '{"reasons":["第一行\\n第二行"]}'
        self.assertEqual(llm.parse_json(t)["reasons"][0], "第一行\n第二行")

    def test_garbage(self):
        self.assertIsNone(llm.parse_json("这不是 JSON"))
        self.assertIsNone(llm.parse_json(""))
        self.assertIsNone(llm.parse_json(None))

    def test_array_not_dict(self):
        self.assertIsNone(llm.parse_json('[1,2,3]'))


class TestValidate(unittest.TestCase):
    def test_triage_ok(self):
        d = {"verdict": "accept", "issue_type": "bug", "reasons": ["x"],
             "minimal_plan": "p", "files_hint": ["a.py"], "confidence": 80}
        self.assertEqual(llm.validate_stage("triage", d), [])

    def test_triage_bad_enum(self):
        d = {"verdict": "maybe", "issue_type": "bug", "reasons": [],
             "minimal_plan": "", "files_hint": [], "confidence": 80}
        errs = llm.validate_stage("triage", d)
        self.assertTrue(any("verdict" in e for e in errs))

    def test_triage_missing(self):
        errs = llm.validate_stage("triage", {"verdict": "accept"})
        self.assertTrue(any("issue_type" in e for e in errs))

    def test_triage_need_info(self):
        d = {"verdict": "need_info", "issue_type": "bug",
             "reasons": ["已确认与版本分支相关"], "minimal_plan": "", "files_hint": [],
             "questions": ["使用的系统版本？", "能否附上完整日志？"], "confidence": 40}
        self.assertEqual(llm.validate_stage("triage", d), [])

    def test_triage_need_info_requires_questions(self):
        d = {"verdict": "need_info", "issue_type": "bug", "reasons": ["x"],
             "minimal_plan": "", "files_hint": [], "questions": [], "confidence": 40}
        errs = llm.validate_stage("triage", d)
        self.assertTrue(any("questions" in e for e in errs))

    def test_triage_questions_forbidden_on_other_verdicts(self):
        d = {"verdict": "accept", "issue_type": "bug", "reasons": ["x"],
             "minimal_plan": "p", "files_hint": [], "questions": ["不该有"], "confidence": 80}
        errs = llm.validate_stage("triage", d)
        self.assertTrue(any("questions" in e for e in errs))
        d2 = dict(d, verdict="reject", minimal_plan="", questions=["也不该有"])
        errs2 = llm.validate_stage("triage", d2)
        self.assertTrue(any("questions" in e for e in errs2))

    def test_triage_questions_must_be_nonempty_strings(self):
        d = {"verdict": "need_info", "issue_type": "bug", "reasons": ["x"],
             "minimal_plan": "", "files_hint": [], "questions": ["", 3], "confidence": 40}
        errs = llm.validate_stage("triage", d)
        self.assertEqual(len([e for e in errs if "questions" in e]), 2)

    def test_final_minimal_ok(self):
        """新 schema：终审只审代码，不再要求 reply_to_author。"""
        d = {"verdict": "pass", "fail_reasons": []}
        self.assertEqual(llm.validate_stage("final", d), [])

    def test_final_bad_verdict(self):
        errs = llm.validate_stage("final", {"verdict": "maybe", "fail_reasons": []})
        self.assertTrue(any("verdict" in e for e in errs))

    def test_final_with_legacy_fields_ok(self):
        """旧格式字段（reply_to_author 等）仍兼容通过。"""
        d = {"verdict": "pass", "fail_reasons": [],
             "reply_to_author": "旧字段", "suggested_label": "已修复"}
        self.assertEqual(llm.validate_stage("final", d), [])

    def test_fix_simple_schema(self):
        d = {"changed_files": ["a.py"], "summary": "s", "self_check": "c"}
        self.assertEqual(llm.validate_stage("fix_simple", d), [])
        errs = llm.validate_stage("fix_simple", {"changed_files": ["a.py"]})
        self.assertTrue(any("summary" in e for e in errs))

    def test_review_severity(self):
        d = {"pass": False, "issues": [{"severity": "critical", "desc": "x"}], "notes": "n"}
        errs = llm.validate_stage("review_bug", d)
        self.assertTrue(any("severity" in e for e in errs))


class TestRender(unittest.TestCase):
    def test_render(self):
        self.assertEqual(render("你好 {{name}}", {"name": "世界"}), "你好 世界")

    def test_render_json_value(self):
        self.assertEqual(render("{{d}}", {"d": {"a": 1}}), '{\n  "a": 1\n}')

    def test_render_missing_keeps_hint(self):
        self.assertIn("未提供", render("{{nope}}", {}))

    def test_render_not_confused_by_json_braces(self):
        t = '输出 {"a":1} 即可，变量：{{x}}'
        self.assertEqual(render(t, {"x": "V"}), '输出 {"a":1} 即可，变量：V')


class TestAssemble(unittest.TestCase):
    def test_assemble_injects_shared(self):
        prompts = {
            "shared": {"injection_guard": "GUARD", "output_discipline": "DISC",
                       "max_reply_chars": 1500},
            "stages": {"triage": {"system": "{{injection_guard}} {{max_reply_chars}} SYS",
                                  "user": "U {{issue_fulltext}}"}},
        }
        system, user = llm.assemble_prompt("triage", prompts, {"issue_fulltext": "ISSUE"})
        self.assertIn("GUARD", system)
        self.assertIn("1500", system)
        self.assertIn("ISSUE", user)

    def test_assemble_wraps_issue_data_boundary(self):
        """spec1-M：issue 正文用 <issue_data> 包裹出显式结构边界
        （== 标题可在正文内伪造，边界判断依赖标签而非标题）。"""
        prompts = {
            "shared": {},
            "stages": {"triage": {"system": "SYS", "user": "U {{issue_fulltext}}"}},
        }
        _, user = llm.assemble_prompt("triage", prompts, {"issue_fulltext": "本文"})
        self.assertIn("<issue_data>\n本文\n</issue_data>", user)
        # 不重复包裹（输入本身已带边界时）
        _, user2 = llm.assemble_prompt(
            "triage", prompts, {"issue_fulltext": "<issue_data>x</issue_data>"})
        self.assertEqual(user2.count("<issue_data>"), 1)

    def test_write_stage_guard_backstop(self):
        """spec1-M：写权限 stage 即使 active 模板缺 {{injection_guard}}，
        system 开头也会带上护栏（代码级兜底）。"""
        prompts = {
            "shared": {"injection_guard": "GUARD"},
            "stages": {"fix": {"system": "仅 SYS", "user": "U"}},
        }
        system, _ = llm.assemble_prompt("fix", prompts, {})
        self.assertTrue(system.startswith("GUARD"))
        # 读类 stage 保持原行为（无占位符则不注入）
        prompts["stages"]["triage"] = {"system": "SYS", "user": "U"}
        system2, _ = llm.assemble_prompt("triage", prompts, {})
        self.assertNotIn("GUARD", system2)

    def test_format_spec(self):
        prompts = {"stages": {"fix": {"system": "前置说明\n输出 JSON 的格式：\n{\"a\":1}"}}}
        spec = llm.format_spec("fix", prompts)
        self.assertTrue(spec.startswith('输出 JSON 的格式'))


class TestOcRun2Base(unittest.TestCase):
    def test_variant_included(self):
        """oc_variant 配置应传入 --variant（实测 low 提速 ~12 倍）。"""
        cmd = llm._oc_run2_base({"model": "m", "oc_variant": "low"})
        self.assertIn("--variant", cmd)
        self.assertIn("low", cmd)
        cmd2 = llm._oc_run2_base({"model": "m"})
        self.assertNotIn("--variant", cmd2)

    def test_oc_run2_path_override(self):
        """oc_run2_path 指向非保护目录副本（TCC 修复）。"""
        cmd = llm._oc_run2_base({"model": "m",
                                 "oc_run2_path": "~/Library/Application Support/x/oc-run2.py"})
        self.assertIn("Library/Application Support/x/oc-run2.py", cmd[0])
        self.assertNotIn("~", cmd[0])
        # 未配置时回退默认
        cmd2 = llm._oc_run2_base({"model": "m"})
        self.assertIn(".local/bin/oc-run2", cmd2[0])


class TestDirectOc2(unittest.TestCase):
    """直连 opencode2 调用层：事件流解析 / 汇总 / 双超时 / 通道开关 / session 查询。"""

    def _mk_stream(self):
        evs = [
            {"type": "step_start", "sessionID": "ses_test1"},
            {"type": "tool_use", "sessionID": "ses_test1", "part": {"tool": "read"}},
            {"type": "tool_use", "sessionID": "ses_test1", "part": {"tool": "read"}},
            {"type": "step_finish", "sessionID": "ses_test1",
             "part": {"tokens": {"input": 100, "output": 50, "reasoning": 30}, "cost": 0.001}},
            {"type": "text", "sessionID": "ses_test1", "part": {"text": "中间说明"}},
            {"type": "text", "sessionID": "ses_test1", "part": {"text": '{"verdict":"accept"}'}},
        ]
        return "\n".join(json.dumps(e, ensure_ascii=False) for e in evs)

    def test_parse_events_skips_noise(self):
        text = 'not-json\n{"type":"a"}\n\n{"type":"b"}'
        evs = llm.parse_events(text)
        self.assertEqual([e["type"] for e in evs], ["a", "b"])
        self.assertEqual(llm.parse_events(""), [])

    def test_summarize_events(self):
        evs = llm.parse_events(self._mk_stream())
        s = llm._summarize_events("t", "/tmp/x", evs, 0)
        self.assertEqual(s["session_id"], "ses_test1")
        self.assertEqual(s["status"], "ok")
        self.assertEqual(s["final_result"], '{"verdict":"accept"}')  # 取最后一个 text
        self.assertEqual(s["actions"], {"read": 2})
        self.assertEqual(s["tokens"], 180)
        self.assertAlmostEqual(s["cost"], 0.001, places=4)

    def test_summarize_failed_without_session(self):
        s = llm._summarize_events("t", "/tmp/x", [], 1)
        self.assertEqual(s["status"], "failed")

    def test_model_with_variant(self):
        self.assertEqual(llm._model_with_variant({"model": "a/b", "oc_variant": "max"}),
                         "a/b#max")
        self.assertEqual(llm._model_with_variant({"model": "a/b"}), "a/b")

    def test_exec_idle_timeout_reports_scene(self):
        """静默超时：进程挂起（无新事件）→ RuntimeError 含现场，进程被清理。"""
        class FakeProc:
            pid = 99999999  # 不存在 → killpg 走 OSError 回退 kill()

            def __init__(self):
                self.stdout = iter([])
                self.stderr = io.StringIO("")
                self.killed = False

            def poll(self):
                return None

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0)

        fp = FakeProc()
        with patch.object(llm.subprocess, "Popen", return_value=fp):
            with self.assertRaises(RuntimeError) as cm:
                llm._exec_opencode2("p", "/tmp", None, "t",
                                    {"model": "m", "agent_idle_timeout_sec": 0,
                                     "agent_timeout_sec": 60})
        self.assertIn("静默", str(cm.exception))
        self.assertTrue(fp.killed)

    def test_exec_success(self):
        """正常路径：事件流完整读取 → 汇总正确。"""
        lines = [
            json.dumps({"type": "step_start", "sessionID": "ses_ok"}) + "\n",
            json.dumps({"type": "text", "sessionID": "ses_ok",
                        "part": {"text": '{"ok":true}'}}) + "\n",
        ]

        class FakeProc:
            pid = 1
            returncode = 0

            def __init__(self):
                self.stdout = iter(lines)
                self.stderr = io.StringIO("")

            def poll(self):
                return 0

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        with patch.object(llm.subprocess, "Popen", return_value=FakeProc()), \
             patch.object(llm, "session_stats", return_value=None):
            out = llm._exec_opencode2("p", "/tmp", None, "t", {"model": "m"})
        self.assertEqual(out["final_result"], '{"ok":true}')
        self.assertEqual(out["session_id"], "ses_ok")
        self.assertEqual(out["status"], "ok")

    def test_run_one_stage_toggle(self):
        """use_direct_oc2 开关：true→直连，false→oc-run2。"""
        calls = []

        def fake_single(*a, **k):
            calls.append("oc-run2")
            return {"final_result": "x"}

        def fake_direct(*a, **k):
            calls.append("direct")
            return {"final_result": "x"}

        with patch.object(llm, "_run_single", side_effect=fake_single), \
             patch.object(llm, "_exec_opencode2", side_effect=fake_direct):
            llm._run_one_stage("p", "fix", "/tmp", None, 10,
                               {"model": "m", "use_direct_oc2": True})
            llm._run_one_stage("p", "fix", "/tmp", None, 10,
                               {"model": "m", "use_direct_oc2": False})
        self.assertEqual(calls, ["direct", "oc-run2"])

    def test_session_stats(self):
        class FakeP:
            returncode = 0
            stdout = json.dumps({"data": {"tokens": {"input": 5, "output": 3},
                                          "cost": 0.0, "time": {"created": 1}}})

        with patch.object(llm.subprocess, "run", return_value=FakeP()):
            st = llm.session_stats("ses_x")
        self.assertEqual(st["tokens"], 8)

        class Fake404:
            returncode = 0
            stdout = '{"data": {"_tag": "NotFound"}}'

        with patch.object(llm.subprocess, "run", return_value=Fake404()):
            self.assertIsNone(llm.session_stats("ses_x"))
        self.assertIsNone(llm.session_stats(""))


class TestRunSingleTimeout(unittest.TestCase):
    def test_failed_review_process_cannot_approve_with_stale_json(self):
        import tempfile
        valid = '{"pass":true,"issues":[],"notes":"ok"}'
        lanes = ["review_bug", "review_minimal", "review_style"]
        tasks = [dict(title="review-" + lane, final_result=valid, status="ok") for lane in lanes]
        tasks[0].update(status="failed", exit_code=1, stderr_tail="worker stopped")
        with tempfile.TemporaryDirectory() as tmp, patch.object(llm, "_run_tasks", return_value=tasks):
            results = llm.run_reviews(lanes, tmp, tmp, {}, {"use_direct_oc2": False})
        self.assertFalse(results["review_bug"].ok)
        self.assertIn("worker stopped", results["review_bug"].errors[0])
        self.assertTrue(all(results[lane].ok for lane in lanes[1:]))

    def test_timeout_reports_tail(self):
        """oc-run2 超时：强杀进程组并回报末尾输出（诊断增强回归）。"""
        import subprocess as sp

        class FakeProc:
            pid = 99999999          # 不存在的 pid → killpg 走 OSError 回退
            killed = False

            def communicate(self, timeout=None):
                if not self.killed:
                    raise sp.TimeoutExpired(cmd="oc-run2", timeout=timeout)
                return ("partial-out", "分诊卡住时最后的输出")

            def kill(self):
                self.killed = True

        with patch.object(llm.subprocess, "Popen", return_value=FakeProc()):
            with self.assertRaises(RuntimeError) as cm:
                llm._run_single("prompt", "/tmp", None, 1, {"model": "m"})
        self.assertIn("超时", str(cm.exception))
        self.assertIn("分诊卡住时最后的输出", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
