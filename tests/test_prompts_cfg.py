import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompts_cfg as pc


class TestPromptsCfg(unittest.TestCase):
    """spec1-M：active 补齐、损坏回退、save_shared 校验。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = pc.ACTIVE_DIR, pc.DEFAULTS_DIR
        real_defaults = self._orig[1]
        pc.DEFAULTS_DIR = os.path.join(self.tmp, "defaults")
        pc.ACTIVE_DIR = os.path.join(self.tmp, "active")
        shutil.copytree(real_defaults, pc.DEFAULTS_DIR)

    def tearDown(self):
        pc.ACTIVE_DIR, pc.DEFAULTS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_active_fills_missing_without_overwrite(self):
        """active 缺文件时从 defaults 补齐，已有（定制过）的文件不被覆盖。"""
        os.makedirs(pc.ACTIVE_DIR)
        custom = os.path.join(pc.ACTIVE_DIR, "triage.system.md")
        with open(custom, "w", encoding="utf-8") as f:
            f.write("CUSTOM")
        pc.ensure_active()
        # 缺失文件已补齐（与 defaults 文件清单一致）
        self.assertEqual(
            sorted(os.listdir(pc.ACTIVE_DIR)), sorted(os.listdir(pc.DEFAULTS_DIR)))
        # 已有文件保持不动
        with open(custom, encoding="utf-8") as f:
            self.assertEqual(f.read(), "CUSTOM")

    def test_load_merges_meta_stages_union(self):
        """active meta 是旧版（stage 声明不全）时与 defaults 并集。"""
        os.makedirs(pc.ACTIVE_DIR)
        with open(os.path.join(pc.ACTIVE_DIR, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "stages": {"triage": {"model": "custom-model"}}}, f)
        meta = pc.load()["meta"]
        self.assertEqual(meta["stages"]["triage"]["model"], "custom-model")  # active 优先
        self.assertIn("merge_fix", meta["stages"])      # defaults 侧声明补齐
        self.assertIn("release_run", meta["stages"])

    def test_corrupt_shared_falls_back(self):
        """active/_shared.json 损坏时 load 不崩，回退 defaults。"""
        os.makedirs(pc.ACTIVE_DIR)
        with open(os.path.join(pc.ACTIVE_DIR, "_shared.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        shared = pc.load()["shared"]
        self.assertIsInstance(shared, dict)
        self.assertIn("injection_guard", shared)

    def test_save_shared_requires_dict(self):
        with self.assertRaises(ValueError):
            pc.save_shared(["not", "a", "dict"])

    def test_six_write_stage_templates_have_guard(self):
        """有写权限的 6 个 stage 模板必须含 injection_guard。"""
        for st in ("fix", "fix_simple", "merge_fix", "release_run",
                   "release_fix", "init_project"):
            body = pc.pick_file(f"{st}.system.md")
            self.assertIn("{{injection_guard}}", body, st)


if __name__ == "__main__":
    unittest.main()
