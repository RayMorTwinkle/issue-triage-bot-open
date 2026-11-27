"""Provider-independent policy and verified-revision loop for issue workers."""
from dataclasses import dataclass
from typing import Callable, Optional

MODES = ("dry", "semi", "full", "yolo", "paused")
REVIEW_STAGES = ("review_bug", "review_minimal", "review_style")


def resolve_mode(global_mode, project_mode=None, issue_mode=None):
    for mode in (issue_mode, project_mode, global_mode):
        if mode in MODES:
            return mode
    return "dry"


def writable(mode):
    return mode in ("semi", "full", "yolo")


@dataclass(frozen=True)
class Decision:
    passed: bool
    feedback: str = ""


class ReworkLimit(RuntimeError):
    pass


def verified_revision(*, repair: Callable[[str], Optional[str]],
                      review: Callable[[str], bool],
                      decide: Callable[[str], Optional[Decision]],
                      ready: Callable[[], bool], retries_left: int,
                      on_rework: Callable[[str], None]) -> Optional[str]:
    """Only return the exact diff that passed review and final adjudication.

    Adapters own artifacts and individual call failures. This loop owns the
    order, budget and re-review of every replacement revision. None means a
    stage already checkpointed its pause/error.
    """
    feedback = ""
    while ready():
        diff = repair(feedback)
        if diff is None or not ready() or not review(diff) or not ready():
            return None
        decision = decide(diff)
        if decision is None:
            return None
        if decision.passed:
            return diff
        feedback = decision.feedback
        on_rework(feedback)
        if retries_left <= 0:
            raise ReworkLimit(feedback)
        retries_left -= 1
    return None
