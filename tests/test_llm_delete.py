"""llm.delete_session 测试（mock subprocess）。"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm


class TestDeleteSession(unittest.TestCase):
    def _run(self, returncode, stdout):
        self.mock_run.return_value = MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def setUp(self):
        self.run = patch("subprocess.run", MagicMock())
        self.mock_run = self.run.start()
        self.addCleanup(self.run.stop)

    def test_delete_ok(self):
        self._run(0, '{"deleted":true}')
        self.assertTrue(llm.delete_session("ses_abc"))

    def test_delete_not_found_counts_as_deleted(self):
        self._run(0, '{"_tag":"SessionNotFoundError","message":"..."}')
        self.assertTrue(llm.delete_session("ses_gone"))

    def test_delete_failure(self):
        self._run(1, "opencode2 error")
        self.assertFalse(llm.delete_session("ses_x"))

    def test_delete_empty_id(self):
        self.assertFalse(llm.delete_session(""))
        self.mock_run.assert_not_called()

    def test_delete_cmd_shape(self):
        self._run(0, "{}")
        llm.delete_session("ses_abc")
        cmd = self.mock_run.call_args.args[0]
        self.assertEqual(cmd[:3], [llm.OC2_BIN, "api", "delete"])
        self.assertEqual(cmd[3], "/api/session/ses_abc")


if __name__ == "__main__":
    unittest.main()
