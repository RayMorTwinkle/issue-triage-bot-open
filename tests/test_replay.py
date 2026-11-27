"""The public recording executes the same revision loop and gate as the live worker."""
import unittest
from replay_engine import record
from workflow import MODES, REVIEW_STAGES


class TestReplay(unittest.TestCase):
    def test_each_mode_and_each_revision(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                run = record(mode)
                events = run["events"]
                if mode == "paused":
                    self.assertEqual([e["stage"] for e in events], ["paused"] * 3)
                    continue
                trace = [e for e in events if e["issue"] == 152]
                self.assertEqual([e["stage"] for e in trace if e["stage"] != "review"],
                                 ["audit", "triage", "fix", "gate", "final", "rework",
                                  "fix", "gate", "final", "done"])
                reviews = []
                for event in trace:
                    if event["stage"] == "fix":
                        reviews.append({})
                    if event.get("lane") and "passed" in event:
                        reviews[-1][event["lane"]] = event["passed"]
                self.assertEqual(reviews[0], {s: s != "review_bug" for s in REVIEW_STAGES})
                self.assertEqual(reviews[1], dict.fromkeys(REVIEW_STAGES, True))
                self.assertEqual(events[-1]["stage"], "rejected")
                self.assertEqual(events[-1]["gate"], dict(active=0, limit=2, waiting=0,
                                                         total_acquired=9))
                outcome = {"dry": "归档", "semi": "等待人工", "full": "等待人工", "yolo": "合并 PR"}
                self.assertIn(outcome[mode], trace[-1]["note"])
