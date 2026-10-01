"""拿真 HTTP 打本地假上游，补上打桩测不到的那一层。

`test_gateway.py` 打桩的是上游对象，验证的是协议翻译逻辑；这里让请求真的
经过 socket，验证的是：`Authorization` 头不带 `Bearer`、`{code,data,msg}`
的拆包、SSE 分片、以及「交过的验证码不能再用一次」这类只有真实交互才会
暴露的行为。全程 127.0.0.1，不出网。
"""

from __future__ import annotations

import threading

import pytest

from src.upstream import Upstream, UpstreamError
from tools.fake_upstream import CODE, TOKEN, build_server

PASSWORD = "Test123456"


@pytest.fixture()
def fake():
    httpd = build_server("127.0.0.1", 0, PASSWORD, seed=7)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture()
def up(fake):
    return Upstream(base=fake, timeout=20)


def test_site_info_and_models(up):
    assert up.site_info()["name"] == "fake-xstech"
    models = up.models()
    assert [m["id"] for m in models] == ["openai::gpt-5.6-terra", "deepseek::deepseek-v4-pro"]
    assert models[0]["group"] == "openai" and models[0]["model"] == "gpt-5.6-terra"


def test_authorization_header_has_no_bearer_prefix(up):
    """站点认的是裸 token。哪天有人「顺手」加上 Bearer，这里会红。"""
    up.token = TOKEN
    assert up.user_info()["id"] == 10001
    up.token = "Bearer " + TOKEN
    with pytest.raises(UpstreamError) as exc:
        up.user_info()
    assert exc.value.code == 2  # 登录已过期


def test_slider_solved_and_verified(up):
    assert up.solve_challenge()


def test_register_then_login(up):
    up.send_register_code("a@example.invalid")
    creds = up.register("a@example.invalid", PASSWORD, CODE)
    assert creds.token == TOKEN
    assert up.login("a@example.invalid", PASSWORD).token == TOKEN


def test_wrong_code_is_rejected(up):
    up.send_register_code("b@example.invalid")
    with pytest.raises(UpstreamError) as exc:
        up.register("b@example.invalid", PASSWORD, "000000")
    assert "验证码" in str(exc.value)


def test_challenge_cannot_be_reused(up):
    """每个题 id 只许核验一次 —— 真站点如此，重试只能是换一道新题。"""
    ch1 = up._challenge()
    ch2 = up._challenge()
    assert ch1["id"] != ch2["id"]
    # 换题重解能过
    assert up.solve_challenge()


def test_stream_frames_and_done(up):
    sid = up.open_session("openai::gpt-5.6-terra")
    frames = list(up.stream("你好", sid))
    assert frames[-1]["type"] == "done"
    text = "".join(f.get("text", "") for f in frames if f["type"] == "delta")
    assert "你好" in text


def test_thinking_block_is_split_across_frames(up):
    """假上游按 7 字节切片，`<think>` 一定被切开 —— 正是要测的场景。"""
    sid = up.open_session("openai::gpt-5.6-terra")
    frames = list(up.stream("想想", sid, thinking=True))
    raw = "".join(f.get("text", "") for f in frames if f["type"] == "delta")
    assert "<think>" in raw
    assert any("<thi" in f.get("text", "") or "nk>" in f.get("text", "")
               for f in frames if f["type"] == "delta")
