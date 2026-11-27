"""提示词配置：active/ 优先、defaults/ 兜底；后台编辑 active/，恢复默认=从 defaults 拷贝。

目录约定：
  prompts/defaults/          出厂模板（只读数据源，随代码走）
    _shared.json             共享组件（injection_guard / output_discipline / correction_template）
    meta.json                版本号 + 各 stage 的 model/inputs 声明
    <stage>.system.md / <stage>.user.md
  prompts/active/            生效副本（后台编辑器读写的就是这里）
"""
import json
import os
import shutil

from util import atomic_write_json

BASE = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(BASE, "prompts")
DEFAULTS_DIR = os.path.join(PROMPTS_DIR, "defaults")
ACTIVE_DIR = os.path.join(PROMPTS_DIR, "active")

STAGES = ("audit", "fix_simple", "review_simple", "triage", "fix", "merge_fix",
          "review_bug", "review_minimal", "review_style", "final", "reply",
          "release_run", "release_fix", "init_project")


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return default


def _read_json(path: str, default):
    """JSON 读取。文件不存在或损坏回退 default（避免损坏的 active/_shared.json
    让整个流水线崩掉）。"""
    if not os.path.exists(path):
        return default
    try:
        return json.loads(_read(path, "null")) or default
    except (json.JSONDecodeError, ValueError):
        return default


def ensure_active() -> None:
    """active/ 缺失或为空时，从 defaults/ 完整拷贝；非空时补齐缺失文件。

    补齐按文件粒度进行：用户定制过的同名文件不动，只有 active/ 里不存在
    的才从 defaults 复制（防 active 漂移造成 merge_fix/release_* 等 stage
    在编辑器中不可见——运行时虽有 defaults 兜底，但漂移本身是隐患）。"""
    if not os.path.isdir(ACTIVE_DIR) or not os.listdir(ACTIVE_DIR):
        shutil.copytree(DEFAULTS_DIR, ACTIVE_DIR, dirs_exist_ok=True)
        return
    for fn in os.listdir(DEFAULTS_DIR):
        src = os.path.join(DEFAULTS_DIR, fn)
        dst = os.path.join(ACTIVE_DIR, fn)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)


def reset_to_defaults() -> None:
    if os.path.isdir(ACTIVE_DIR):
        shutil.rmtree(ACTIVE_DIR)
    shutil.copytree(DEFAULTS_DIR, ACTIVE_DIR)


def version() -> int:
    ensure_active()
    meta = _read_json(os.path.join(ACTIVE_DIR, "meta.json"), {})
    return int(meta.get("version", 0))


def bump_version() -> int:
    ensure_active()
    p = os.path.join(ACTIVE_DIR, "meta.json")
    meta = _read_json(p, {"version": 0})
    meta["version"] = int(meta.get("version", 0)) + 1
    atomic_write_json(p, meta)
    return meta["version"]


def pick_file(fname: str) -> str:
    """读取单个提示词文件：active/ 优先，缺失回退 defaults/。"""
    ensure_active()
    a = os.path.join(ACTIVE_DIR, fname)
    if os.path.exists(a):
        return _read(a)
    return _read(os.path.join(DEFAULTS_DIR, fname))


def is_customized(fname: str) -> bool:
    """该文件是否已被后台修改（active 与 defaults 不一致）。"""
    a = os.path.join(ACTIVE_DIR, fname)
    d = os.path.join(DEFAULTS_DIR, fname)
    if not (os.path.exists(a) and os.path.exists(d)):
        return False
    return _read(a) != _read(d)


def load() -> dict:
    """返回 {shared: {...}, meta: {...}, stages: {name: {system, user}}}。
    active/ 优先，缺失文件逐个回退 defaults/。"""
    ensure_active()

    shared = _read_json(os.path.join(ACTIVE_DIR, "_shared.json"), None)
    if not shared:
        shared = _read_json(os.path.join(DEFAULTS_DIR, "_shared.json"), {})
    meta = _read_json(os.path.join(ACTIVE_DIR, "meta.json"), None)
    if not meta:
        meta = _read_json(os.path.join(DEFAULTS_DIR, "meta.json"), {"version": 0, "stages": {}})
    else:
        # active meta 是旧版（stage 声明不全）时，用 defaults 的声明做并集补齐。
        # 值以 active 为准：用户改过 model/inputs 的 stage 保持原样
        dmeta = _read_json(os.path.join(DEFAULTS_DIR, "meta.json"), {}) or {}
        dstages = dict(dmeta.get("stages") or {})
        dstages.update(meta.get("stages") or {})
        meta["stages"] = dstages

    stages = {}
    for st in STAGES:
        stages[st] = {
            "system": pick_file(f"{st}.system.md"),
            "user": pick_file(f"{st}.user.md"),
        }
    return {"shared": shared, "meta": meta, "stages": stages}


def _atomic_write_text(path: str, content: str) -> None:
    """原子文本写入：tmp + fsync + os.replace，防半写文件污染生效提示词。"""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_stage_file(stage: str, kind: str, content: str) -> int:
    """后台保存单个提示词文件；kind ∈ system|user。返回新版本号。"""
    ensure_active()
    if stage not in STAGES or kind not in ("system", "user"):
        raise ValueError(f"bad stage/kind: {stage}/{kind}")
    _atomic_write_text(os.path.join(ACTIVE_DIR, f"{stage}.{kind}.md"), content)
    return bump_version()


def save_shared(shared: dict) -> int:
    ensure_active()
    if not isinstance(shared, dict):
        raise ValueError("shared 配置必须是 JSON 对象")
    atomic_write_json(os.path.join(ACTIVE_DIR, "_shared.json"), shared)
    return bump_version()


def snapshot_to(dest_dir: str) -> None:
    """把当前生效提示词整体快照进 issue 档案（审计/复现用）。
    按 pick_file 逻辑逐文件导出（含 active 缺失但实际生效的 defaults 版本）。"""
    ensure_active()
    os.makedirs(dest_dir, exist_ok=True)
    for st in STAGES:
        for kind in ("system", "user"):
            fn = f"{st}.{kind}.md"
            with open(os.path.join(dest_dir, fn), "w", encoding="utf-8") as f:
                f.write(pick_file(fn))
    for fn in ("_shared.json", "meta.json"):
        with open(os.path.join(dest_dir, fn), "w", encoding="utf-8") as f:
            f.write(pick_file(fn))
