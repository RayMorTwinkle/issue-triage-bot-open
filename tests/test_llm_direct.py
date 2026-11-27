"""直连 LLM 客户端测试：mock urllib，覆盖 未配置/正常/思考链截断/重试/4xx 不重试/JSON 校验。"""
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm
import llm_direct

CFG = {"llm_api_base": "https://example.com/api/v3",
       "llm_api_key": "sk-test",
       "llm_model": "glm-5.3-flash"}


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def resp(content="正常", finish="stop", reasoning=None, usage=None):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": usage or {"total_tokens": 42}}


def _urlopen(**kw):
    return patch.object(llm_direct.urllib.request, "urlopen", **kw)


class TestChat(unittest.TestCase):
    def test_saturated_gate_returns_failure_without_network_retry(self):
        from gate import AgentGate
        gate = AgentGate(1)
        with patch.object(llm_direct, "GATE", gate), gate, _urlopen() as send:
            result = llm_direct.chat([], {**CFG, "agent_queue_timeout_sec": 0})
            self.assertFalse(result.ok)
            self.assertIn("排队超时", result.errors[0])
            self.assertEqual(result.attempts, 1)
            send.assert_not_called()

    def test_not_configured(self):
        r = llm_direct.chat([{"role": "user", "content": "hi"}], {})
        self.assertFalse(r.ok)
        self.assertIn("未配置", r.errors[0])
        self.assertFalse(llm_direct.available({}))

    def test_ok(self):
        with _urlopen(return_value=FakeResp(resp("你好"))):
            r = llm_direct.chat([{"role": "user", "content": "hi"}], CFG)
        self.assertTrue(r.ok)
        self.assertEqual(r.content, "你好")
        self.assertEqual(r.usage["total_tokens"], 42)
        self.assertTrue(llm_direct.available(CFG))

    def test_thinking_truncated(self):
        """思考链占满 max_tokens：content 空 + finish_reason=length → 明确报错。"""
        with _urlopen(return_value=FakeResp(resp(content="", finish="length",
                                                       reasoning="想了好久……"))):
            r = llm_direct.chat([{"role": "user", "content": "hi"}], CFG, retries=0)
        self.assertFalse(r.ok)
        self.assertIn("截断", r.errors[0])

    def test_server_error_retry_then_ok(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError("u", 500, "boom", {}, None)
            return FakeResp(resp("好"))

        with _urlopen(side_effect=flaky), \
             patch.object(llm_direct.time, "sleep", lambda s: None):
            r = llm_direct.chat([{"role": "user", "content": "hi"}], CFG)
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 2)

    def test_401_no_retry(self):
        calls = {"n": 0}

        def auth_err(*a, **k):
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 401, "unauth", {}, None)

        with _urlopen(side_effect=auth_err), \
             patch.object(llm_direct.time, "sleep", lambda s: None):
            r = llm_direct.chat([{"role": "user", "content": "hi"}], CFG)
        self.assertFalse(r.ok)
        self.assertEqual(calls["n"], 1)  # 4xx 不重试
        self.assertIn("401", r.errors[0])

    def test_network_retry_exhausted(self):
        calls = {"n": 0}

        def netfail(*a, **k):
            calls["n"] += 1
            raise urllib.error.URLError("down")

        with _urlopen(side_effect=netfail), \
             patch.object(llm_direct.time, "sleep", lambda s: None):
            r = llm_direct.chat([{"role": "user", "content": "hi"}], CFG)
        self.assertFalse(r.ok)
        self.assertEqual(calls["n"], 3)  # 首发 + 2 次重试


class TestChatJson(unittest.TestCase):
    def test_ok(self):
        with _urlopen(return_value=FakeResp(resp('{"a": 1}'))):
            r = llm_direct.chat_json([{"role": "user", "content": "hi"}], CFG,
                                     parse=llm.parse_json,
                                     validate=lambda d: [] if d.get("a") == 1 else ["bad"])
        self.assertTrue(r.ok)
        self.assertEqual(r.data, {"a": 1})

    def test_validate_fail(self):
        with _urlopen(return_value=FakeResp(resp('{"a": 2}'))):
            r = llm_direct.chat_json([{"role": "user", "content": "hi"}], CFG,
                                     parse=llm.parse_json,
                                     validate=lambda d: ["bad"])
        self.assertFalse(r.ok)
        self.assertEqual(r.data, {"a": 2})  # 保留原始数据便于诊断
        self.assertIn("bad", r.errors)

    def test_bad_json(self):
        with _urlopen(return_value=FakeResp(resp("这不是 JSON"))):
            r = llm_direct.chat_json([{"role": "user", "content": "hi"}], CFG,
                                     parse=llm.parse_json)
        self.assertFalse(r.ok)
        self.assertIn("JSON", r.errors[0])

    def test_fenced_json(self):
        with _urlopen(return_value=FakeResp(resp('```json\n{"b": 2}\n```'))):
            r = llm_direct.chat_json([{"role": "user", "content": "hi"}], CFG,
                                     parse=llm.parse_json)
        self.assertTrue(r.ok)
        self.assertEqual(r.data, {"b": 2})


if __name__ == "__main__":
    unittest.main()
