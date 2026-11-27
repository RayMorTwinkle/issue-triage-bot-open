"""沙箱：fresh clone、分支、diff、commit、push、防篡改比对、清理。"""
import hashlib
import os
import shutil
import time
import subprocess

from util import truncate_tail, valid_ref_name, valid_repo_url

GIT_ENV = dict(os.environ)
GIT_ENV["GIT_TERMINAL_PROMPT"] = "0"      # 绝不交互
_PROXY = ""   # 由 set_proxy() 注入（config: git_proxy）；空 = 直连


def set_proxy(proxy: str) -> None:
    """设置 git 代理（如 http://127.0.0.1:7897 或 socks5://...）；空串清除。
    经 HTTPS_PROXY 注入子进程环境，libcurl 支持 http/socks5 两种 scheme。"""
    global _PROXY
    _PROXY = (proxy or "").strip()
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        GIT_ENV.pop(k, None)
    if _PROXY:
        GIT_ENV["HTTPS_PROXY"] = _PROXY
        GIT_ENV["HTTP_PROXY"] = _PROXY


def _git(args: list, cwd: str = None, timeout: int = 120) -> str:
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, env=GIT_ENV)
    except subprocess.TimeoutExpired:
        # 统一转 RuntimeError：调用方的 except (RuntimeError, OSError) 才能接住，
        # 否则 TimeoutExpired 会炸穿线程（发布/流水线静默死亡，无 needs_human 留痕）
        raise RuntimeError(f"git {args[0]} 超时（≥{timeout}s）")
    if p.returncode != 0:
        raise RuntimeError(f"git {args[0]} 失败: {(p.stderr or p.stdout)[-400:]}")
    return p.stdout


def sandbox_path(root: str, n: int) -> str:
    return os.path.join(os.path.expanduser(root), f"issue-{n}")


_CLONE_URLS = {}   # dest → clone 源 URL（push 前断言 remote 未被改写）


def clone(repo_url: str, dest: str, main_branch: str = "main") -> str:
    # 入参白名单：repo_url/main_branch 来自 config 与 API，进了 argv 位置——
    # 以 - 开头的值会被 git 当成选项（--upload-pack=<cmd> 是经典 RCE 通道）
    if not valid_repo_url(repo_url):
        raise RuntimeError(f"非法 repo_url: {repo_url[:80]!r}")
    if not valid_ref_name(main_branch):
        raise RuntimeError(f"非法 main_branch: {main_branch[:80]!r}")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last_err = None
    for attempt in range(1, 4):   # 网络抖动（GFW 对 github.com 间歇干扰）重试 3 次
        try:
            _git(["clone", "--branch", main_branch, "--", repo_url, dest],
                 timeout=300)
            _CLONE_URLS[os.path.abspath(dest)] = repo_url
            return dest
        except RuntimeError as e:
            last_err = e
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)
            if attempt < 3:
                time.sleep(8 * attempt)
    raise RuntimeError(f"clone 重试 3 次仍失败: {last_err}")


def create_branch(dest: str, branch: str) -> None:
    if not valid_ref_name(branch):
        raise RuntimeError(f"非法分支名: {branch[:80]!r}")
    _git(["checkout", "-b", branch], cwd=dest)


def stage_all(dest: str) -> None:
    _git(["add", "-A"], cwd=dest)


def staged_diff(dest: str) -> str:
    out = _git(["diff", "--cached"], cwd=dest)
    return out or ""


def has_changes(dest: str) -> bool:
    return bool(staged_diff(dest).strip())


def diff_stat(dest: str) -> str:
    return _git(["diff", "--cached", "--stat"], cwd=dest).strip()


def commit(dest: str, message: str) -> None:
    # core.hooksPath=/dev/null：agent 可写工作区，pre-commit 等 hook 不能在提交时执行
    _git(["-c", "user.name=issue-triage-bot",
          "-c", "user.email=bot@users.noreply.github.com",
          "-c", "core.hooksPath=/dev/null",
          "commit", "-m", message], cwd=dest)


def push_branch(dest: str, branch: str) -> None:
    """推送分支；若远程已存在同名分支（重跑/残留场景），fetch 后用
    --force-with-lease 安全更新（lease 以刚 fetch 到的当前值为准，可防并发未知改写）。"""
    if not valid_ref_name(branch):
        raise RuntimeError(f"非法分支名: {branch[:80]!r}")
    # push 前断言 remote 未被改写：agent 可写 .git/config，改 origin URL 即可偷代码
    want = _CLONE_URLS.get(os.path.abspath(dest))
    if want:
        got = _git_config(dest, "remote.origin.url")
        if got != want:
            raise RuntimeError(
                f"remote.origin.url 被篡改（{got[:80]!r} ≠ {want[:80]!r}），拒绝 push")
    try:
        _git(["fetch", "--quiet", "origin", branch], cwd=dest, timeout=120)
    except RuntimeError:
        pass
    remote = None
    try:
        remote = _git(["rev-parse", f"refs/remotes/origin/{branch}"], cwd=dest).strip()
    except RuntimeError:
        remote = None
    if remote:
        _git(["push", f"--force-with-lease=refs/heads/{branch}:{remote}",
              "origin", branch], cwd=dest, timeout=180)
    else:
        _git(["push", "-u", "origin", branch], cwd=dest, timeout=180)


def _git_config(dest: str, key: str) -> str:
    try:
        return _git(["config", "--get", key], cwd=dest).strip()
    except RuntimeError:
        return ""


def _hash_path(path: str) -> str:
    """文件/目录内容哈希（.git 元数据防篡改用）；不存在/不可读返回空串。"""
    try:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        if os.path.isdir(path):
            h = hashlib.sha256()
            for root, _, fs in os.walk(path):
                for name in sorted(fs):
                    fp = os.path.join(root, name)
                    try:
                        h.update(os.path.relpath(fp, path).encode())
                        with open(fp, "rb") as f:
                            h.update(f.read())
                    except OSError:
                        continue
            return h.hexdigest()[:16]
    except OSError:
        pass
    return ""


def git_meta_fingerprint(dest: str) -> str:
    """.git 元数据指纹：remote URL、hooksPath、config、hooks 目录内容。
    覆盖的篡改面：改 remote 指向攻击者仓库、配 textconv 伪造 diff 输出、
    植入 hook 在 commit/push 时执行。"""
    parts = [_git_config(dest, "remote.origin.url"),
             _git_config(dest, "core.hooksPath"),
             _git_config(dest, "core.autocrlf"),
             _hash_path(os.path.join(dest, ".git", "config")),
             _hash_path(os.path.join(dest, ".git", "hooks"))]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:24]


def diff_fingerprint(dest: str) -> str:
    """工作区+元数据指纹，用于审查前后防篡改比对。

    覆盖 staged diff 之外的盲区：unstaged/untracked 改动（status --porcelain）、
    HEAD 位置（防 reset 盗移）、remote/hooks/config（git_meta_fingerprint）。"""
    parts = []
    for args in (["status", "--porcelain"], ["rev-parse", "HEAD"]):
        try:
            parts.append(_git(args, cwd=dest))
        except RuntimeError:
            parts.append("")
    parts.append(git_meta_fingerprint(dest))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:24]


def apply_patch(dest: str, patch_path: str) -> None:
    """应用 patch 文件（git apply --index；补发重放用）。失败抛 RuntimeError。"""
    _git(["apply", "--index", patch_path], cwd=dest, timeout=120)


def fetch_branch(dest: str, branch: str) -> None:
    """fetch 指定远程分支（merge 修复用）。"""
    if not valid_ref_name(branch):
        raise RuntimeError(f"非法分支名: {branch[:80]!r}")
    _git(["fetch", "origin", branch], cwd=dest, timeout=120)


def checkout_tracking(dest: str, branch: str) -> None:
    """checkout -B 到 origin/<branch>（强制重置本地分支）。"""
    if not valid_ref_name(branch):
        raise RuntimeError(f"非法分支名: {branch[:80]!r}")
    _git(["checkout", "-B", branch, f"origin/{branch}"], cwd=dest)


def merge_branch(dest: str, ref: str):
    """git merge ref --no-edit；返回 (rc, output)（冲突时 rc!=0，不抛异常）。"""
    if not valid_ref_name(ref):
        return 1, f"非法 merge ref: {ref[:80]!r}"
    p = subprocess.run(["git", "merge", ref, "--no-edit"], cwd=dest,
                       capture_output=True, text=True, timeout=120, env=GIT_ENV)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def conflict_files(dest: str) -> list:
    """处于冲突（unmerged）状态的文件清单。"""
    out = _git(["diff", "--name-only", "--diff-filter=U"], cwd=dest)
    return [x for x in out.splitlines() if x.strip()]


def check_merge_markers(dest: str, files: list = None):
    """扫描冲突标记残留（<<<<<<< / ======= / >>>>>>>）。

    用 git grep 直查工作区文件内容，而非 `git diff --check`——后者只比
    worktree↔index，agent 若把文件 add 进暂存区，标记会从 diff 视野中逃逸。
    files 指定待查文件（merge_fix 的冲突清单）；缺省查全部 tracked 文件。
    返回 (rc, out)：rc=0 无残留；rc=1 为误报保护下也按异常处理。"""
    args = ["grep", "-nE", r"^(<{7}|={7}|>{7})( |$)", "--"]
    args += [f for f in (files or []) if f and not f.startswith("-")] or ["."]
    p = subprocess.run(["git"] + args, cwd=dest,
                       capture_output=True, text=True, timeout=60, env=GIT_ENV)
    if p.returncode == 1:
        return 0, ""                     # git grep: 1 = 无匹配（干净）
    if p.returncode == 0:
        return 2, (p.stdout or "")[-400:]  # 命中冲突标记
    return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-400:]


def build_result_text(returncode: int, output: str) -> str:
    ok = "通过" if returncode == 0 else "失败"
    return f"构建命令返回码 {returncode}（{ok}）\n输出尾部:\n{truncate_tail(output, 60)}"


def run_build(build_cmd: list, cwd: str, env_extra: dict = None, timeout: int = 600):
    """执行门禁构建命令；返回 (returncode, output)。"""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(build_cmd, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 1, f"构建命令执行异常: {e}"


def cleanup(dest: str) -> None:
    if dest:
        _CLONE_URLS.pop(os.path.abspath(dest), None)
        if os.path.exists(dest):
            shutil.rmtree(dest, ignore_errors=True)
