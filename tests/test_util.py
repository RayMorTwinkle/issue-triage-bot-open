"""util 测试：统一日志（写入/级别/清理）、render、Timer。"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

from util import cleanup_logs, log_event, notify, now_ts, render, Timer


class TestLogEvent(unittest.TestCase):
    def test_writes_daily_file(self):
        with tempfile.TemporaryDirectory() as d:
            log_event("info", "pipeline", "test_event", issue=7, log_dir=d, foo="bar")
            files = [f for f in os.listdir(d) if f.startswith("bot-")]
            self.assertEqual(len(files), 1)
            with open(os.path.join(d, files[0]), encoding="utf-8") as f:
                rec = json.loads(f.readline())
            self.assertEqual(rec["level"], "info")
            self.assertEqual(rec["scope"], "pipeline")
            self.assertEqual(rec["event"], "test_event")
            self.assertEqual(rec["issue"], 7)
            self.assertEqual(rec["detail"]["foo"], "bar")
            self.assertIn("ts", rec)

    def test_no_issue_field_when_none(self):
        with tempfile.TemporaryDirectory() as d:
            log_event("warn", "web", "x", log_dir=d)
            with open(os.path.join(d, os.listdir(d)[0]), encoding="utf-8") as f:
                rec = json.loads(f.readline())
            self.assertNotIn("issue", rec)
            self.assertEqual(rec["level"], "warn")

    def test_cleanup_removes_old_keeps_recent(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "bot-20200101.jsonl")
            new = os.path.join(d, "bot-20990101.jsonl")
            other = os.path.join(d, "run-1.jsonl")
            for p in (old, new, other):
                with open(p, "w") as f:
                    f.write("{}")
            os.utime(old, (time.time() - 40 * 86400,) * 2)
            removed = cleanup_logs(30, d)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))
            self.assertTrue(os.path.exists(other))  # 非 bot-*.jsonl 不动


class TestMisc(unittest.TestCase):
    def test_render(self):
        self.assertEqual(render("a{{x}}b", {"x": "1"}), "a1b")

    def test_timer(self):
        t = Timer()
        time.sleep(0.01)
        self.assertGreater(t.elapsed(), 0)

    def test_now_ts_shape(self):
        self.assertRegex(now_ts(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestNotify(unittest.TestCase):
    def test_payload_not_interpolated_into_source(self):
        """回归：含引号/AppleScript payload 的 msg 必须经 argv 传递，不进 -e 源码。"""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)

        payload = 'x" & do shell script "rm -rf ~'
        title = 't" & do shell script "id'
        with patch("subprocess.run", fake_run):
            notify(title, payload)
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertEqual(cmd[0], "osascript")
        src = cmd[cmd.index("-e") + 1]
        self.assertNotIn(payload, src)          # payload 不得插值进脚本源码
        self.assertNotIn(title, src)
        self.assertIn("on run argv", src)       # 走 argv 通道
        self.assertIn(payload, cmd)             # payload 作为独立 argv 元素
        self.assertIn(title, cmd)

    def test_notify_swallows_oserror(self):
        with patch("subprocess.run", side_effect=OSError("no osascript")):
            notify("t", "m")                    # 不抛


if __name__ == "__main__":
    unittest.main()
