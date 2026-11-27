"""sandbox 单元测试：纯本地 git（bare origin），无网络。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sandbox


def git(args, cwd=None):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 建 bare origin + 首个提交
        self.origin = os.path.join(self.tmp, "origin.git")
        git(["init", "--bare", "-b", "main", self.origin])
        seed = os.path.join(self.tmp, "seed")
        os.makedirs(seed)
        git(["init", "-b", "main", seed])
        git(["-c", "user.name=t", "-c", "user.email=t@t", "checkout", "-b", "main"], cwd=seed) \
            if False else None
        with open(os.path.join(seed, "app.py"), "w") as f:
            f.write("print('hi')\n")
        git(["add", "-A"], cwd=seed)
        git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"], cwd=seed)
        git(["remote", "add", "origin", self.origin], cwd=seed)
        git(["push", "-u", "origin", "main"], cwd=seed)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clone_branch_diff_commit_push(self):
        dest = os.path.join(self.tmp, "sb1")
        sandbox.clone(f"file://{self.origin}", dest, "main")
        self.assertTrue(os.path.exists(os.path.join(dest, "app.py")))

        sandbox.create_branch(dest, "fix/issue-1")
        # 模拟 agent 改文件
        with open(os.path.join(dest, "app.py"), "a") as f:
            f.write("print('fixed')\n")
        sandbox.stage_all(dest)
        self.assertTrue(sandbox.has_changes(dest))
        diff = sandbox.staged_diff(dest)
        self.assertIn("fixed", diff)
        fp1 = sandbox.diff_fingerprint(dest)
        self.assertRegex(fp1, r"^[0-9a-f]{24}$")   # 新格式：裸 sha256 截断
        self.assertEqual(sandbox.diff_fingerprint(dest), fp1)  # 未改动=不变（防误报）

        sandbox.commit(dest, "Fix #1: test")
        # 防篡改比对：commit 后 diff 应为空 → 指纹变化（这正是我们要检测的场景）
        self.assertNotEqual(sandbox.diff_fingerprint(dest), fp1)

        sandbox.push_branch(dest, "fix/issue-1")

    def test_no_changes(self):
        dest = os.path.join(self.tmp, "sb2")
        sandbox.clone(f"file://{self.origin}", dest, "main")
        sandbox.stage_all(dest)
        self.assertFalse(sandbox.has_changes(dest))

    def test_build_runner(self):
        dest = os.path.join(self.tmp, "sb3")
        sandbox.clone(f"file://{self.origin}", dest, "main")
        rc, out = sandbox.run_build(["python3", "-m", "py_compile", "app.py"], dest)
        self.assertEqual(rc, 0)
        rc, out = sandbox.run_build(["python3", "-m", "py_compile", "no_such.py"], dest)
        self.assertNotEqual(rc, 0)

    def test_clone_failure(self):
        with self.assertRaises(RuntimeError):
            sandbox.clone("file:///nonexistent/repo.git",
                          os.path.join(self.tmp, "sb4"), "main")

    def test_push_branch_over_existing_remote(self):
        """重跑场景：远程已有同名分支（non-FF）→ force-with-lease 安全覆盖成功。"""
        dest = os.path.join(self.tmp, "sb5")
        sandbox.clone(f"file://{self.origin}", dest, "main")
        sandbox.create_branch(dest, "fix/issue-2")
        with open(os.path.join(dest, "app.py"), "a") as f:
            f.write("print('v1')\n")
        sandbox.stage_all(dest)
        sandbox.commit(dest, "v1")
        sandbox.push_branch(dest, "fix/issue-2")  # 首次推送

        # 另一处 clone 推了新提交（模拟远程残留/分歧）
        other = os.path.join(self.tmp, "sb6")
        sandbox.clone(f"file://{self.origin}", other, "main")
        git(["fetch", "origin"], cwd=other)
        git(["checkout", "-b", "fix/issue-2", "origin/fix/issue-2"], cwd=other)
        with open(os.path.join(other, "other.txt"), "w") as f:
            f.write("other\n")
        git(["add", "-A"], cwd=other)
        git(["-c", "user.name=o", "-c", "user.email=o@o", "commit", "-m", "other"], cwd=other)
        git(["push", "origin", "fix/issue-2"], cwd=other)

        # 原目录再改再推（non-FF 场景）→ force-with-lease 应成功
        with open(os.path.join(dest, "app.py"), "a") as f:
            f.write("print('v2')\n")
        sandbox.stage_all(dest)
        sandbox.commit(dest, "v2")
        sandbox.push_branch(dest, "fix/issue-2")

        tip_local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest,
                                   capture_output=True, text=True).stdout.strip()
        tip_remote = subprocess.run(["git", "rev-parse", "fix/issue-2"],
                                    cwd=self.origin,
                                    capture_output=True, text=True).stdout.strip()
        self.assertEqual(tip_local, tip_remote)


class TestGitTimeoutWrap(unittest.TestCase):
    """回归：git 子进程超时必须转 RuntimeError（否则 TimeoutExpired 炸穿调用线程）。"""

    def test_timeout_raises_runtime_error(self):
        from unittest.mock import patch
        with patch.object(sandbox.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120)):
            with self.assertRaises(RuntimeError) as ctx:
                sandbox._git(["clone", "x", "y"])
        self.assertIn("超时", str(ctx.exception))


class TestCloneRetry(unittest.TestCase):
    """回归：clone 瞬时失败要重试 3 次（GFW 抖动），全败才抛错。"""

    def test_clone_retries_then_succeeds(self):
        from unittest.mock import patch
        calls = {"n": 0}
        real = sandbox._git

        def flaky(args, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("git clone 失败: Connection reset")
            return real(args, **kw)

        # 用本地 bare 仓库当 origin，保证第 3 次真实成功
        tmp = tempfile.mkdtemp()
        origin = os.path.join(tmp, "o.git")
        git(["init", "--bare", "-b", "main", origin])
        seed = os.path.join(tmp, "s")
        os.makedirs(seed)
        git(["init", "-b", "main", seed], cwd=tmp)
        with open(os.path.join(seed, "f.txt"), "w") as f:
            f.write("x")
        git(["-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=seed)
        git(["-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-m", "init"], cwd=seed)
        git(["push", origin, "main"], cwd=seed)

        with patch.object(sandbox, "_git", side_effect=flaky), \
             patch.object(sandbox.time, "sleep"):
            dest = sandbox.clone(origin, os.path.join(tmp, "work"), "main")
        self.assertTrue(os.path.isfile(os.path.join(dest, "f.txt")))
        self.assertEqual(calls["n"], 3)
        shutil.rmtree(tmp, ignore_errors=True)

    def test_clone_all_fail(self):
        from unittest.mock import patch
        with patch.object(sandbox, "_git",
                          side_effect=RuntimeError("git clone 失败: no route")), \
             patch.object(sandbox.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                sandbox.clone("https://.invalid/x", os.path.join(
                    tempfile.mkdtemp(), "w"), "main")
        self.assertIn("3 次", str(ctx.exception))


class TestSetProxy(unittest.TestCase):
    """回归：git_proxy 注入/清除子进程环境。"""

    def test_set_and_clear(self):
        sandbox.set_proxy("http://127.0.0.1:7897")
        self.assertEqual(sandbox.GIT_ENV.get("HTTPS_PROXY"), "http://127.0.0.1:7897")
        self.assertEqual(sandbox.GIT_ENV.get("HTTP_PROXY"), "http://127.0.0.1:7897")
        sandbox.set_proxy("")
        self.assertIsNone(sandbox.GIT_ENV.get("HTTPS_PROXY"))
        self.assertIsNone(sandbox.GIT_ENV.get("HTTP_PROXY"))

if __name__ == "__main__":
    unittest.main()
