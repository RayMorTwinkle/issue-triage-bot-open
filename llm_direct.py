"""直连 LLM 客户端：OpenAI 兼容 /chat/completions（标准库 urllib，零第三方依赖）。

用途（两个廉价环节，不走 oc-run2）：
  - audit  问题审核（注入/恶意筛查 + 复杂度判定）
  - reply  作者回复生成

配置来源（.env > config.json，由 bot.load_cfg 合并）：
  llm_api_base / llm_api_key / llm_model

思考链模型适配（如方舟 glm-5.3-flash 返回 reasoning_content）：
  - 只取 message.content 作为正文；
  - max_tokens 默认给足 4096，避免思考链占满导致 content 被截断为空。
"""
import json
import time
import urllib.error
import urllib.request

from gate import GATE, QueueTimeout
from util import Timer


class DirectResult:
    def __init__(self):
        self.ok = False
        self.content = ""      # 模型正文（不含 reasoning）
        self.data = None       # chat_json 解析/校验后的 dict
        self.errors = []       # 失败原因（中文）
        self.attempts = 0
        self.elapsed = 0.0
        self.usage = {}

    def to_dict(self):
        return {"ok": self.ok, "content": self.content, "errors": self.errors,
                "attempts": self.attempts, "elapsed": self.elapsed, "usage": self.usage}


def available(cfg: dict) -> bool:
    """三件套是否齐备；缺一即视为未配置（调用方应降级）。"""
    return bool((cfg.get("llm_api_base") or "").strip()
                and (cfg.get("llm_api_key") or "").strip()
                and (cfg.get("llm_model") or "").strip())


def _endpoint(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def chat(messages: list, cfg: dict, max_tokens: int = 4096,
         timeout: int = 120, retries: int = 2, temperature: float = None) -> DirectResult:
    """单轮 chat 调用；网络错误/5xx/429 退避重试，其他 4xx 不重试。

    并发闸门按 attempt 获取：退避 sleep 在闸门外，挂死端点不会长期占用额度。
    """
    res = DirectResult()
    base = (cfg.get("llm_api_base") or "").strip()
    key = (cfg.get("llm_api_key") or "").strip()
    model = (cfg.get("llm_model") or "").strip()
    if not (base and key and model):
        res.errors = ["直连 LLM 未配置（LLM_API_BASE / LLM_API_KEY / LLM_MODEL）"]
        return res

    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    body = json.dumps(payload, ensure_ascii=False).encode()
    url = _endpoint(base)

    timer = Timer()
    last_err = None
    for attempt in range(1, retries + 2):
        res.attempts = attempt
        try:
            with GATE.slot(float(cfg.get("agent_queue_timeout_sec", 300))):
                req = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {key}"})
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        raw = json.loads(r.read().decode("utf-8"))
                    choice = (raw.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    content = (msg.get("content") or "").strip()
                    res.usage = raw.get("usage") or {}
                    if content:
                        res.ok, res.content = True, content
                        break
                    if choice.get("finish_reason") == "length":
                        last_err = "输出被 max_tokens 截断（思考链可能占满额度，content 为空）"
                    else:
                        last_err = "模型返回空内容"
                except urllib.error.HTTPError as e:
                    detail = ""
                    try:
                        detail = e.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                    last_err = f"HTTP {e.code}: {detail}"
                    if e.code < 500 and e.code != 429:
                        break  # 4xx（除限流）重试无意义
                except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                    last_err = f"网络错误: {e}"
        except QueueTimeout as exc:
            last_err = str(exc)
            break
        if attempt <= retries:
            time.sleep(2 * attempt)
    if not res.ok and last_err:
        res.errors = [str(last_err)]
    res.elapsed = timer.elapsed()
    return res


def chat_json(messages: list, cfg: dict, parse, validate=None, **kw) -> DirectResult:
    """调用并宽松解析 JSON（parse=llm.parse_json；validate=校验函数 → 错误列表）。"""
    res = chat(messages, cfg, **kw)
    if not res.ok:
        return res
    data = parse(res.content)
    if data is None:
        res.ok = False
        res.errors = ["输出不是合法 JSON 对象"]
        return res
    if validate:
        errs = validate(data)
        if errs:
            res.ok = False
            res.errors = errs
            res.data = data
            return res
    res.data = data
    return res
