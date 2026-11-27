"""github.py 测试：mock subprocess.run，验证命令组装、重试与降级。"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import github as gh


class TestGh(unittest.TestCase):
    def setUp(self):
        self.run = patch("subprocess.run", MagicMock())
        self.mock_run = self.run.start()
        self.addCleanup(self.run.stop)

    def _ok(self, stdout=""):
        self.mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")

    def test_list_open_issues_cmd_shape(self):
        self._ok('[]')
        gh.list_open_issues("test/repo")
        cmd = self.mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "gh")
        self.assertIn("issue", cmd)
        self.assertEqual(cmd[-2:], ["--repo", "test/repo"])

    def test_retry_then_success(self):
        responses = [MagicMock(returncode=1, stdout="", stderr="rate limit"),
                     MagicMock(returncode=0, stdout='[{"number":1}]', stderr="")]
        self.mock_run.side_effect = responses
        with patch("time.sleep"):
            out = gh.list_open_issues("test/repo")
        self.assertEqual(out[0]["number"], 1)
        self.assertEqual(self.mock_run.call_count, 2)

    def test_all_fail_raises(self):
        self.mock_run.side_effect = [MagicMock(returncode=1, stdout="", stderr="x")] * 5
        with patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                gh.list_open_issues("test/repo")

    def test_issue_text_flattens_comments(self):
        issue = {"title": "T", "body": "B", "author": {"login": "a"},
                 "comments": [{"author": {"login": "b"}, "body": "C1"}]}
        t = gh.issue_text(issue)
        self.assertIn("T", t); self.assertIn("B", t); self.assertIn("C1", t)
        self.assertIn("评论 by b", t)

    def test_add_label_creates_missing(self):
        edit_calls = {"n": 0}

        def fake_run(cmd, **kw):
            if "edit" in cmd:
                edit_calls["n"] += 1
                if edit_calls["n"] == 1:  # 首次：label 尚不存在
                    return MagicMock(returncode=1, stdout="", stderr="label not found")
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")  # label create

        self.mock_run.side_effect = fake_run
        with patch("time.sleep"):
            gh.add_label("test/repo", 1, "triage:pending")  # 不应抛异常
        self.assertGreaterEqual(edit_calls["n"], 2)  # 失败一次后重试成功

    def test_comment_file(self):
        self._ok('')
        gh.comment_file("test/repo", 5, "/tmp/x.md")
        cmd = self.mock_run.call_args.args[0]
        self.assertIn("--body-file", cmd)
        self.assertIn("/tmp/x.md", cmd)

    def test_create_pr_returns_url(self):
        self.mock_run.side_effect = [
            MagicMock(returncode=0, stdout='[]', stderr=""),
            MagicMock(returncode=0,
                      stdout="Creating pull request...\nhttps://github.com/x/y/pull/9\n",
                      stderr=""),
        ]
        url = gh.create_pr("test/repo", "fix/issue-1", "t", "/tmp/b.md")
        self.assertEqual(url, "https://github.com/x/y/pull/9")

    def test_create_pr_updates_existing(self):
        """重跑场景：分支已有开放 PR → gh pr edit 更新并复用 URL。"""
        self.mock_run.side_effect = [
            MagicMock(returncode=0,
                      stdout='[{"number": 3, "url": "https://github.com/x/y/pull/3"}]',
                      stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        url = gh.create_pr("test/repo", "fix/issue-2", "t2", "/tmp/b.md")
        self.assertEqual(url, "https://github.com/x/y/pull/3")
        cmd2 = self.mock_run.call_args_list[1].args[0]
        self.assertIn("edit", cmd2)
        self.assertIn("3", cmd2)

    def test_merge_pr_cmd(self):
        self._ok("")
        gh.merge_pr("test/repo", 7)
        cmd = self.mock_run.call_args.args[0]
        self.assertIn("merge", cmd)
        self.assertIn("--squash", cmd)
        self.assertIn("--delete-branch", cmd)
        self.assertIn("7", cmd)

    def test_has_open_pr(self):
        self._ok('[{"number":3}]')
        self.assertTrue(gh.has_open_pr("test/repo", "fix/issue-1"))
        self._ok('[]')
        self.assertFalse(gh.has_open_pr("test/repo", "fix/issue-1"))


if __name__ == "__main__":
    unittest.main()
