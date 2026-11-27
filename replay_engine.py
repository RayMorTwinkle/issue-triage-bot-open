"""Record synthetic, local-only issues using the worker's verification loop."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
import time

from gate import AgentGate
from workflow import Decision, MODES, REVIEW_STAGES, resolve_mode, verified_revision, writable


@dataclass(frozen=True)
class Example:
    number: int
    title: str
    filename: str
    before: str
    after: str
    cases: tuple
    intermediate: str = ""


EXAMPLES = (
    Example(148, "保留队列中的原始顺序", "queue_view.py",
            "def select(items):\n    return sorted(set(items))\n",
            "def select(items):\n    return list(dict.fromkeys(items))\n",
            ((["b", "a", "b"], ["b", "a"]), ([], []))),
    Example(152, "空标签回退到默认名称", "labels.py",
            "def select(value):\n    return value.strip()\n",
            'def select(value):\n    return (value or "").strip() or "untitled"\n',
            (("  ", "untitled"), (None, "untitled"), (" fix ", "fix")),
            'def select(value):\n    return value.strip() or "untitled"\n'),
    Example(155, "缺少复现信息的空报告", "", "", "", ()),
)


def check_lane(lane, source, example):
    if lane == "review_bug":
        namespace = {}
        exec(compile(source, example.filename, "exec"), namespace)
        for argument, expected in example.cases:
            try:
                actual = namespace["select"](argument)
            except (AttributeError, TypeError) as exc:
                return False, f"输入 {argument!r} 触发 {type(exc).__name__}。"
            if actual != expected:
                return False, f"输入 {argument!r} 应返回 {expected!r}。"
        return True, f"{len(example.cases)} 个边界用例返回预期结果。"
    if lane == "review_minimal":
        passed = len(source.splitlines()) == len(example.before.splitlines())
        return passed, "比较修复前后行数，检查修改范围。"
    passed = source.endswith("\n") and all(line.rstrip() == line for line in source.splitlines())
    return passed, "检查文件末尾换行与行尾空格。"


def record(mode="dry"):
    if mode not in MODES:
        raise ValueError("unknown mode")
    gate = AgentGate(2)
    events = []
    lock = Lock()

    def emit(number, stage, note, **fields):
        with lock:
            events.append(dict(issue=number, stage=stage, note=note,
                               gate=gate.snapshot(), **fields))

    def run(example, root):
        n = example.number
        effective = resolve_mode(mode)
        if effective == "paused":
            emit(n, "paused", "权限已暂停，工单停在审核前。")
            return
        emit(n, "audit", "校验报告正文与复现信息。")
        if not example.filename:
            emit(n, "rejected", "报告正文为空，退回补充复现步骤。")
            return
        emit(n, "triage", f"已定位 {example.filename}，进入复杂线审查。")
        target = root / example.filename
        target.write_text(example.before, encoding="utf-8")
        reviews = {}
        revision = 0

        def repair(feedback):
            nonlocal revision
            revision += 1
            source = example.after if feedback else example.intermediate or example.after
            target.write_text(source, encoding="utf-8")
            diff = "".join(unified_diff(example.before.splitlines(True), source.splitlines(True),
                                        "a/" + target.name, "b/" + target.name))
            emit(n, "fix", f"已生成第 {revision} 版补丁。", diff=diff, revision=revision)
            compile(target.read_text(encoding="utf-8"), target.name, "exec")
            emit(n, "gate", "Python 编译通过。")
            return diff

        def review(diff):
            source = target.read_text(encoding="utf-8")
            def one(lane):
                with gate.slot(3):
                    emit(n, "review", "审查者已取得并发名额。", lane=lane)
                    time.sleep(0.015)  # Pace the synthetic workers to expose gate contention.
                    passed, note = check_lane(lane, source, example)
                    emit(n, "review", note, lane=lane, passed=passed)
                    return lane, passed
            with ThreadPoolExecutor(max_workers=3) as pool:
                reviews.update(pool.map(one, REVIEW_STAGES))
            return True

        def decide(diff):
            passed = all(reviews[lane] for lane in REVIEW_STAGES)
            note = "三路意见已汇齐，接受此版补丁。" if passed else "正确性检查未通过，要求返工。"
            emit(n, "final", note, passed=passed)
            return Decision(passed, note)

        result = verified_revision(repair=repair, review=review, decide=decide,
                                   ready=lambda: True, retries_left=1,
                                   on_rework=lambda reason: emit(n, "rework", reason))
        if result is None:
            raise RuntimeError("local verification interrupted")
        outcome = "草稿与补丁已归档。"
        if writable(effective):
            outcome = "模拟创建 PR，等待人工合并。" if effective != "yolo" else "模拟创建并合并 PR。"
        emit(n, "done", outcome)

    with TemporaryDirectory(prefix="triage-replay-") as directory:
        for example in EXAMPLES:
            run(example, Path(directory))
    return {"mode": mode, "issues": [{"number": e.number, "title": e.title,
            "file": e.filename} for e in EXAMPLES], "events": events}
