"""gh CLI 封装：issue 拉取、label、comment、PR；统一 --repo 与退避重试。"""
import json
import subprocess
import time

from resilience import ReadServiceError
from util import log_event

MAX_TRY = 3
BACKOFF = 5


def _gh(args: list, repo: str, timeout: int = 60, retries: int = MAX_TRY) -> str:
    """retries：幂等读类操作默认 3 次退避重试；非幂等写（comment/merge）
    调用方传 retries=1——响应丢失时重试会造成重复评论/重复合并。"""
    cmd = ["gh"] + args + ["--repo", repo]
    last = None
    for i in range(retries):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if p.returncode == 0:
                return p.stdout
            detail = (p.stderr or p.stdout)[-300:]
            transient = ("timeout", "could not resolve", "error connecting", "connection reset",
                         "http 429", "http 500", "http 502", "http 503", "http 504")
            failure = ReadServiceError if any(x in detail.lower() for x in transient) else RuntimeError
            last = failure(f"gh exit {p.returncode}: {detail}")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            last = e
        if i < retries - 1:
            time.sleep(BACKOFF * (i + 1))
    raise last


def list_open_issues(repo: str) -> list:
    """全部 open issue（gh issue list 天然不含 PR）。"""
    out = _gh(["issue", "list", "--state", "open", "--limit", "100",
               "--json", "number,title,body,createdAt,updatedAt,author,labels"], repo)
    return json.loads(out or "[]")


def get_issue(repo: str, n: int) -> dict:
    out = _gh(["issue", "view", str(n), "--json",
               "number,title,body,author,labels,comments"], repo)
    return json.loads(out)


ISSUE_BODY_MAX = 8000        # 正文截断：巨型 issue 不稀释提示词中的注入护栏
ISSUE_COMMENT_MAX = 1500     # 单条评论截断
ISSUE_TOTAL_MAX = 24000      # 总长上限（超出留头去尾，标注省略）


def issue_text(issue: dict) -> str:
    """标题+正文+评论 flattening 为一份文本（作为 LLM 的 user_issue 数据块）。
    全部字段截断限长：恶意/巨型输入不能把系统提示的注入护栏稀释到窗口边缘。"""
    body = (issue.get("body") or "")[:ISSUE_BODY_MAX]
    parts = [f"# 标题: {issue.get('title','')}",
             f"# 作者: {(issue.get('author') or {}).get('login','')}",
             "", body]
    total = sum(len(p) for p in parts)
    for c in issue.get("comments") or []:
        who = (c.get("author") or {}).get("login", "?")
        chunk = f"\n--- 评论 by {who} ---\n{(c.get('body') or '')[:ISSUE_COMMENT_MAX]}"
        if total + len(chunk) > ISSUE_TOTAL_MAX:
            parts.append("\n--- （评论过多，其余省略） ---")
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)


def add_label(repo: str, n: int, label: str) -> None:
    try:
        _gh(["issue", "edit", str(n), "--add-label", label], repo)
    except RuntimeError:
        # label 不存在则先建再试一次
        try:
            _gh(["label", "create", label], repo, timeout=30)
            _gh(["issue", "edit", str(n), "--add-label", label], repo)
        except RuntimeError as e:
            # label 失败不阻塞主流程，但必须留痕（否则状态与 GitHub 静默漂移）
            log_event("warn", "github", "add_label_failed", repo=repo, issue=n,
                      label=label, error=str(e)[:150])


def remove_label(repo: str, n: int, label: str) -> None:
    try:
        _gh(["issue", "edit", str(n), "--remove-label", label], repo)
    except RuntimeError as e:
        log_event("warn", "github", "remove_label_failed", repo=repo, issue=n,
                  label=label, error=str(e)[:150])


def comment_file(repo: str, n: int, body_file: str) -> None:
    _gh(["issue", "comment", str(n), "--body-file", body_file], repo,
        timeout=60, retries=1)   # 非幂等：超时重试=重复评论


def create_pr(repo: str, head_branch: str, title: str, body_file: str) -> str:
    """创建 PR；若该分支已有开放 PR（重跑场景）则更新标题/正文并复用。返回 PR URL。"""
    out = _gh(["pr", "list", "--head", head_branch, "--state", "open",
               "--json", "number,url"], repo, timeout=30)
    existing = json.loads(out or "[]")
    if existing:
        n = existing[0]["number"]
        _gh(["pr", "edit", str(n), "--title", title, "--body-file", body_file],
            repo, timeout=60)
        return existing[0].get("url") or ""
    out = _gh(["pr", "create", "--head", head_branch,
               "--title", title, "--body-file", body_file], repo, timeout=90, retries=1)
    for line in out.splitlines():
        if line.startswith("http"):
            return line.strip()
    return out.strip()


def has_open_pr(repo: str, head_branch: str) -> bool:
    try:
        out = _gh(["pr", "list", "--head", head_branch, "--state", "open",
                   "--json", "number"], repo, timeout=30)
        return bool(json.loads(out or "[]"))
    except (RuntimeError, json.JSONDecodeError):
        return False


def merge_pr(repo: str, pr_number: int, method: str = "squash") -> None:
    """自动合并 PR（yolo 档位）：squash + 删除分支；失败抛 RuntimeError。"""
    _gh(["pr", "merge", str(pr_number), f"--{method}", "--delete-branch"],
        repo, timeout=120, retries=1)   # 非幂等：超时重试=重复合并


def latest_commit_sha(repo: str, branch: str) -> str:
    """分支最新 commit SHA（release 时间条件的"有新提交"判定用）。"""
    p = subprocess.run(["gh", "api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"gh api 失败: {(p.stderr or p.stdout)[-200:]}")
    return (p.stdout or "").strip()


def latest_release_tag(repo: str) -> str:
    """仓库最新 GitHub Release 的 tag（workflow 路径发布成功后回填版本号，
    供通知与结果卡展示）。无 release / 查询失败返回空串（不阻塞成功路径）。"""
    p = subprocess.run(["gh", "api", f"repos/{repo}/releases/latest",
                        "--jq", ".tag_name"],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()
