"""通用工具：模板渲染、原子写、时间戳、日志。"""
import json
import os
import re
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))


def render(template: str, mapping: dict) -> str:
    """{{name}} 占位符渲染；未提供的变量替换为空串并保留占位提示。"""
    def sub(m):
        key = m.group(1)
        if key in mapping:
            v = mapping[key]
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2)
        return f"(未提供: {key})"
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, template)


def atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_log(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {"ts": now_ts(), **record}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


LOG_DIR = os.path.join(BASE, "logs")


def log_event(level: str, scope: str, event: str, issue=None,
              log_dir: str = None, **detail) -> None:
    """统一结构化日志：logs/bot-YYYYMMDD.jsonl（按天滚动）。
    level: info|warn|error · scope: poll|audit|agent|gate|web|session…"""
    rec = {"ts": now_ts(), "level": level, "scope": scope, "event": event}
    if issue is not None:
        rec["issue"] = issue
    if detail:
        rec["detail"] = detail
    d = log_dir or LOG_DIR
    try:
        os.makedirs(d, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        with open(os.path.join(d, f"bot-{day}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志失败绝不影响主流程


def cleanup_logs(days: int = 30, log_dir: str = None) -> int:
    """清理超过保留期的按天日志（bot-*.jsonl），返回删除数量。"""
    import glob
    d = log_dir or LOG_DIR
    cutoff = time.time() - max(int(days), 1) * 86400
    removed = 0
    for f in glob.glob(os.path.join(d, "bot-*.jsonl")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                removed += 1
        except OSError:
            pass
    return removed


def notify(title: str, msg: str) -> None:
    """macOS 通知；失败静默（非 macOS / 无 osascript）。

    标题与正文经 argv 传入（on run argv），不做 AppleScript 源码插值：
    issue/LLM 文本中的引号与 do shell script payload 不会被执行。"""
    try:
        import subprocess
        subprocess.run(
            ["osascript", "-e",
             "on run argv\n"
             "display notification (item 1 of argv) with title (item 2 of argv)\n"
             "end run",
             msg[:120], title],
            timeout=10, capture_output=True)
    except Exception:
        pass


def truncate_tail(text: str, lines: int = 200) -> str:
    ls = (text or "").splitlines()
    return "\n".join(ls[-lines:]) if len(ls) > lines else (text or "")


# ---------------- 入参白名单校验（选项注入/路径安全用） ----------------

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def valid_repo_name(s: str) -> bool:
    """GitHub owner/name 形式。"""
    return bool(s) and len(s) <= 200 and bool(_REPO_RE.fullmatch(s))


def valid_repo_url(s: str) -> bool:
    """git clone 源：拒绝以 - 开头（选项注入）与空白/换行；
    允许 https/ssh/git/file 协议、git@ scp 语法与本地路径。"""
    if not s or len(s) > 500 or s.startswith("-") or s.strip() != s:
        return False
    if any(c in s for c in (" ", "\t", "\n", "\r", "\x00")):
        return False
    return bool(re.match(
        r"^(https?|ssh|git|file)://|^git@|^/|^\.|^~|^[A-Za-z0-9_.-]+[/:]", s))


def valid_ref_name(s: str) -> bool:
    """分支/tag 等 git ref 名称（check-ref-format 子集）：禁 - 前缀、..、控制字符。"""
    if not s or len(s) > 200:
        return False
    if s.startswith(("-", "/", ".")) or s.endswith(("/", ".", ".lock")):
        return False
    for bad in ("..", "//", " ", "~", "^", ":", "?", "*", "[", "\\",
                "\t", "\n", "\r", "\x00", "@" + "{"):
        if bad in s:
            return False
    return s != "@"


_VERSION_RE = re.compile(r"^v?\d+(\.\d+){0,3}([-+][A-Za-z0-9._-]+)?$")


def valid_version_tag(s: str) -> bool:
    """发布版本号/tag：宽松 semver（v1.2.3 / 1.2 / 2.0-rc1）。"""
    return bool(s) and len(s) <= 60 and bool(_VERSION_RE.fullmatch(s))


class Timer:
    def __init__(self):
        self.t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.t0
