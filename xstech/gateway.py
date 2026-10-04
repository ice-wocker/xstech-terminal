"""把上游协议翻译成 OpenAI Chat Completions 协议。

这样任何 OpenAI 客户端（openai-python、LangChain、Continue、aichat……）
只要改 base_url 就能直接用，不需要为本项目写适配层。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Iterator

from .upstream import Upstream, UpstreamError

# 上游会把推理过程包在 <think> 里返回，交付给客户端前要剥掉，
# 否则支持 reasoning 的客户端会把思考内容当正文显示。
THINK_RE = re.compile(r"<think>.*?</think>", re.S)
THINK_OPEN_RE = re.compile(r"<think>", re.S)
TAG_OPEN = "<think>"
TAG_CLOSE = "</think>"


class ChatGateway:
    def __init__(self, upstream: Upstream, default_model: str = ""):
        self.up = upstream
        self.default_model = default_model

    def list_models(self) -> list[dict]:
        models = self.up.models()
        now = int(time.time())
        return [
            {
                "id": m["id"],
                "object": "model",
                "created": now,
                "owned_by": m["group"] or "xstech",
                "permission": [],
                "root": m["id"],
                "parent": None,
            }
            for m in models
        ]

    @staticmethod
    def _last_user_message(messages: list[dict]) -> str:
        """上游按会话保存上下文，所以只需要把最新一条用户消息发过去。

        系统提示没有独立字段，用分隔标记拼进正文。
        """
        system = "\n".join(
            m.get("content", "")
            for m in messages
            if m.get("role") == "system" and isinstance(m.get("content"), str)
        )
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, list):
                    user = "\n".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                else:
                    user = content or ""
                break
        if system:
            return f"{system}\n\n{user}"
        return user

    def _prepare(self, body: dict) -> tuple[str, int, str]:
        model = body.get("model") or self.default_model
        if not model:
            raise UpstreamError("未指定模型")
        text = self._last_user_message(body.get("messages") or [])
        if not text.strip():
            raise UpstreamError("messages 里没有可发送的用户内容")
        sid = self.up.open_session(model)
        return text, sid, model

    def complete(self, body: dict) -> dict:
        text, sid, model = self._prepare(body)
        parts: list[str] = []
        usage = {}
        for frame in self.up.stream(
            text, sid, model=model, thinking=bool(body.get("thinking"))
        ):
            if frame["type"] == "delta":
                parts.append(frame["text"])
            elif frame["type"] == "meta":
                raw = frame["raw"]
                usage = {
                    "prompt_tokens": raw.get("promptTokens", 0),
                    "completion_tokens": raw.get("completionTokens", 0),
                    "total_tokens": raw.get("useTokens", 0),
                }
            elif frame["type"] == "error":
                raise UpstreamError(frame["message"])

        content = strip_think("".join(parts))
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def stream(self, body: dict) -> Iterator[str]:
        text, sid, model = self._prepare(body)
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        def frame(delta: dict, finish=None) -> str:
            payload = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield frame({"role": "assistant", "content": ""})

        buffer = ""
        emitted = 0
        for event in self.up.stream(text, sid, model=model, thinking=bool(body.get("thinking"))):
            if event["type"] == "delta":
                buffer += event["text"]
                # 每帧对整个缓冲重算可见前缀。标签可能被任意切开，
                # 只要过滤是无状态的，就不会因为某一帧判断失误而一路错下去。
                clean = visible_prefix(buffer)
                if len(clean) > emitted:
                    yield frame({"content": clean[emitted:]})
                    emitted = len(clean)
            elif event["type"] == "error":
                yield f"data: {json.dumps({'error': {'message': event['message']}})}\n\n"
                yield "data: [DONE]\n\n"
                return

        yield frame({}, finish="stop")
        yield "data: [DONE]\n\n"


def strip_think(text: str) -> str:
    """去掉思考块；未闭合的 `<think>...` 整段丢弃。"""
    text = THINK_RE.sub("", text)
    idx = text.find(TAG_OPEN)
    return text[:idx] if idx != -1 else text


def visible_prefix(text: str) -> str:
    """从「可能含思考块的缓冲」里取出当前可安全交付的可见部分。

    流式下要挡住三种情况：
    1. `<think>` 已出现但未闭合 —— 其后全是思考内容，不能交付；
    2. `<think>...</think>` 已完整出现 —— 剥掉，保留其后的正文；
    3. 缓冲结尾是 `<thi` 这类半个标签 —— 先不吐，否则下一分片补上 `nk>`
       就把标签本身漏出去了(`</think>` 同理)。

    刻意做成无状态纯函数：每个分片都对整个缓冲重新求值，
    避免「某帧状态判断错了、后面一直错」这类流式经典 bug。
    """
    out = text
    while True:  # 上游一轮里可能有多段思考
        m = THINK_OPEN_RE.search(out)
        if not m:
            break
        closing = out.find("</think>", m.end())
        if closing == -1:
            return out[: m.start()]
        out = out[: m.start()] + out[closing + len(TAG_CLOSE):]

    # 结尾若是某个标签的「真前缀」就截掉（`<thi` / `</thi`），
    # 但只在没有任何完整标签时才这么做 —— 否则会把已经剥离干净的正文再切一刀。
    for tag in (TAG_OPEN, TAG_CLOSE):
        for size in range(len(tag) - 1, 0, -1):
            prefix = tag[:size]
            if out.endswith(prefix) and TAG_OPEN not in out and TAG_CLOSE not in out:
                return out[:-size]
    return out
