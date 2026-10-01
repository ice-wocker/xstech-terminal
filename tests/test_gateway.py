"""离线单测：不联网，只验证协议翻译逻辑。"""

import json

import pytest

from src.gateway import ChatGateway, strip_think, visible_prefix
from src.upstream import Upstream, UpstreamError


class FakeUpstream(Upstream):
    def __init__(self, frames):
        super().__init__()
        self.frames = frames
        self.sent = []

    def open_session(self, model):
        self.sent.append(("open", model))
        return 42

    def models(self):
        return [
            {"id": "openai::gpt-5.6-terra", "group": "openai",
             "model": "gpt-5.6-terra", "label": "GPT-5.6-Terra", "caps": {}},
            {"id": "deepseek::deepseek-v4-pro", "group": "deepseek",
             "model": "deepseek-v4-pro", "label": "Deepseek-V4-Pro", "caps": {}},
        ]

    def stream(self, text, session_id, model="", thinking=False, web_search=False):
        self.sent.append(("stream", text, session_id, model))
        yield from self.frames


def _frames(*texts, meta=None):
    out = [{"type": "delta", "text": t} for t in texts]
    if meta:
        out.append({"type": "meta", "raw": meta})
    out.append({"type": "done"})
    return out


def test_strip_think_removes_closed_block():
    assert strip_think("<think>a\nb</think>答案") == "答案"


def test_strip_think_removes_unclosed_block():
    # 流式过程中 <think> 可能还没闭合，正文不能在思考期漏出去
    assert strip_think("<think>还在想") == ""
    assert strip_think("正文<think>还在想") == "正文"


def test_complete_returns_openai_shape():
    up = FakeUpstream(_frames("你好", "，世界", meta={"promptTokens": 3, "completionTokens": 4, "useTokens": 7}))
    gw = ChatGateway(up, default_model="deepseek::deepseek-v4-pro")
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = gw.complete(body)

    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "你好，世界"
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"]["total_tokens"] == 7


def test_complete_hides_think_block():
    up = FakeUpstream(_frames("<think>推理</think>", "最终答案"))
    gw = ChatGateway(up, default_model="m")
    out = gw.complete({"messages": [{"role": "user", "content": "q"}]})
    assert out["choices"][0]["message"]["content"] == "最终答案"


def test_system_prompt_is_merged_into_user_text():
    up = FakeUpstream(_frames("ok"))
    gw = ChatGateway(up, default_model="m")
    gw.complete({
        "messages": [
            {"role": "system", "content": "只回答数字"},
            {"role": "user", "content": "1+1"},
        ]
    })
    sent_text = up.sent[1][1]
    assert "只回答数字" in sent_text and "1+1" in sent_text


def test_only_last_user_message_is_sent():
    up = FakeUpstream(_frames("ok"))
    gw = ChatGateway(up, default_model="m")
    gw.complete({
        "messages": [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": "第二句"},
        ]
    })
    assert up.sent[1][1] == "第二句"


def test_multimodal_content_parts_are_flattened():
    up = FakeUpstream(_frames("ok"))
    gw = ChatGateway(up, default_model="m")
    gw.complete({
        "messages": [{"role": "user", "content": [{"type": "text", "text": "看图"}]}]
    })
    assert up.sent[1][1] == "看图"


def test_stream_emits_openai_chunks_and_done():
    up = FakeUpstream(_frames("你", "好"))
    gw = ChatGateway(up, default_model="m")
    chunks = list(gw.stream({"messages": [{"role": "user", "content": "hi"}], "stream": True}))

    assert chunks[-1] == "data: [DONE]\n\n"
    payloads = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    text = "".join(p["choices"][0]["delta"].get("content", "") for p in payloads)
    assert text == "你好"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_never_leaks_partial_think():
    up = FakeUpstream(_frames("<thi", "nk>思考", "</thi", "nk>答", "案"))
    gw = ChatGateway(up, default_model="m")
    chunks = list(gw.stream({"messages": [{"role": "user", "content": "q"}], "stream": True}))
    payloads = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    text = "".join(p["choices"][0]["delta"].get("content", "") for p in payloads)
    assert text == "答案"


def test_list_models_maps_to_openai_objects():
    gw = ChatGateway(FakeUpstream([]), default_model="m")
    models = gw.list_models()
    assert models[0]["object"] == "model"
    assert models[0]["id"] == "openai::gpt-5.6-terra"
    assert models[0]["owned_by"] == "openai"


def test_missing_model_raises():
    gw = ChatGateway(FakeUpstream(_frames("x")), default_model="")
    with pytest.raises(UpstreamError):
        gw.complete({"messages": [{"role": "user", "content": "hi"}]})


# --- 回归：思考块被分片切开时的边界（这几个 case 是真实踩过的坑） ---


@pytest.mark.parametrize("chunks,expected", [
    # 开标签被逐字切开，正文在闭合后才吐
    (["<thi", "nk>思考", "</thi", "nk>答", "案"], "答案"),
    # 闭合标签完整但正文与它同帧
    (["<think>思考</think>答", "案"], "答案"),
    # 多段思考
    (["<think>a</think>正<think>b</think>文"], "正文"),
    # 没有思考块
    (["答", "案"], "答案"),
])
def test_partial_think_tag_never_leaks(chunks, expected):
    buffer = ""
    for chunk in chunks:
        buffer += chunk
    assert visible_prefix(buffer) == expected


def test_tail_tag_prefix_is_held_back():
    # 结尾是半个开标签时先不吐（否则标签会漏出去）
    assert visible_prefix("正文<thi") == "正文"
    # 开标签到齐后，因为还没闭合，后面仍按思考内容挡掉
    assert visible_prefix("正文<think>思考") == "正文"


def test_strip_think_keeps_text_before_open_tag():
    assert strip_think("答案<think>还在想") == "答案"
    assert strip_think("<think>想完了</think>答案") == "答案"


def test_cli_ask_reuses_visible_prefix(monkeypatch, capsys):
    """ask 必须复用 gateway 的可见前缀逻辑，否则会漏出 <think>。"""
    from src import cli

    class FakeUp(Upstream):
        def __init__(self, *a, **k):
            super().__init__()

        def models(self):
            return [{"id": "m", "group": "", "model": "m", "label": "M", "caps": {}}]

        def open_session(self, model):
            return 1

        def stream(self, text, session_id, model="", thinking=False, web_search=False):
            for f in ["<think>想", "一下</think>", "你好"]:
                yield {"type": "delta", "text": f}
            yield {"type": "done"}

    account = cli.StoredAccount(email="a@b.c", password="p", token="t", model="m")
    monkeypatch.setattr(cli, "_ensure_token", lambda args: account)
    monkeypatch.setattr(cli, "Upstream", lambda **kw: FakeUp())

    class Args:
        model = "m"
        prompt = ["hi"]
        base = "https://xstech.one"

    assert cli.cmd_ask(Args()) == 0
    captured = capsys.readouterr().out
    assert captured.strip() == "你好"
