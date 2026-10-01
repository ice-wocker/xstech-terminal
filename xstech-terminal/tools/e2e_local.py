#!/usr/bin/env python3
"""本地端到端：假上游 + 真 HTTP 端点，全程不出网。

跑的是「用户会怎么用」那条完整链路，只是把别人的站点换成了本机的假上游：

    假上游(127.0.0.1:随机端口)
        ↑ 私有协议 / SSE
    ChatGateway → OpenAI 兼容 HTTP 端点(127.0.0.1:随机端口)
        ↑ OpenAI 协议
    curl / openai SDK / 本项目 CLI

覆盖：
- 滑块求解真解一道题（图现场生成），并走完 register → token 落盘
- 端点 /health、/v1/models、非流式、流式、404
- 上游连不上时透传 502，而不是 500 或者连接挂死
- 思考块被 SSE 分片切开时不漏给客户端

用法：
    python tools/e2e_local.py
    python tools/e2e_local.py -v     # 每一步都打明细
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# account.py 在 import 时就读 CONFIG_DIR，所以指向临时目录要赶在它之前
os.environ.setdefault("XSTECH_CONFIG_DIR", tempfile.mkdtemp(prefix="xstech-e2e-"))

from src.account import CRED_FILE, StoredAccount, load, save  # noqa: E402
from src.gateway import ChatGateway  # noqa: E402
from src.server import serve  # noqa: E402
from src.upstream import Upstream  # noqa: E402
from tools.fake_upstream import CODE, TOKEN, build_server  # noqa: E402

PASSWORD = "Test123456"
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASSED if cond else FAILED).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))
    return cond


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_fake_upstream() -> str:
    httpd = build_server("127.0.0.1", 0, PASSWORD, seed=20261001)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_port}"


def start_gateway(base: str, model: str) -> str:
    port = free_port()
    gateway = ChatGateway(Upstream(base=base, token=TOKEN), default_model=model)
    threading.Thread(target=serve, args=(gateway, "127.0.0.1", port), daemon=True).start()
    return f"http://127.0.0.1:{port}"


def http(url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def gateway_stream_text(gw: ChatGateway, model: str, text: str) -> str:
    """走 gateway 的 OpenAI 流式接口，把逐帧内容拼回来。

    注意 gateway.stream() 产出的是 **SSE 文本行**（`data: {...}\n\n`），
    不是 dict —— 抄成后者会一路静默拼出空串，看起来像「正文丢了」。
    """
    out = []
    for line in gw.stream({"model": model, "stream": True,
                           "messages": [{"role": "user", "content": text}]}):
        if not line.startswith("data: "):
            continue
        chunk = line[6:].strip()
        if chunk == "[DONE]":
            continue
        delta = json.loads(chunk).get("choices", [{}])[0].get("delta", {}).get("content")
        if delta:
            out.append(delta)
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print("== 启动本地假上游 ==")
    base = start_fake_upstream()
    print(f"  假上游: {base}（随机端口，不出网）")
    up = Upstream(base=base, timeout=20)
    check("站点信息可读", up.site_info().get("name") == "fake-xstech")

    models = up.models()
    check("模型清单带分组前缀", models and models[0]["id"] == "openai::gpt-5.6-terra",
          str([m["id"] for m in models]))
    model = models[0]["id"]

    print("== 注册链路（真解一道滑块）==")
    email = "local-e2e@example.invalid"
    up.send_register_code(email)
    creds = up.register(email, PASSWORD, CODE)
    check("注册拿到 token", creds.token == TOKEN)
    save(StoredAccount(email=email, password=PASSWORD, token=creds.token, base=base))
    check("凭据落盘且权限 0600",
          CRED_FILE.exists() and oct(CRED_FILE.stat().st_mode)[-3:] == "600",
          str(CRED_FILE))
    back = load()
    check("凭据能读回来", back is not None and back.token == TOKEN)

    print("== 启动 OpenAI 兼容端点 ==")
    ep = start_gateway(base, model)
    print(f"  端点: {ep}/v1")
    time.sleep(0.3)

    status, body = http(f"{ep}/health")
    check("/health 正常", status == 200 and json.loads(body)["status"] == "ok")

    status, body = http(f"{ep}/v1/models")
    ids = [m["id"] for m in json.loads(body).get("data", [])]
    check("/v1/models 返回清单", status == 200 and model in ids, str(ids))

    status, body = http(f"{ep}/v1/chat/completions",
                        {"model": model, "messages": [{"role": "user", "content": "本地你好"}]})
    payload = json.loads(body)
    content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
    check("非流式对话回显正确",
          status == 200 and "本地你好" in content and "假上游收到" in content, content[:60])
    check("非流式带 usage", bool(payload.get("usage")))

    status, body = http(f"{ep}/v1/chat/completions",
                        {"model": model, "stream": True,
                         "messages": [{"role": "user", "content": "流式"}]})
    check("流式走 SSE 且以 [DONE] 收尾",
          status == 200 and "chat.completion.chunk" in body and 'data: [DONE]' in body)

    status, body = http(f"{ep}/v1/does-not-exist")
    check("未定义路由 404", status == 404)

    print("== 上游连不上时 ==")
    broken = start_gateway(f"http://127.0.0.1:{free_port()}", model)
    time.sleep(0.3)
    status, body = http(f"{broken}/v1/models")
    check("给 502 而不是 500 或挂死",
          status == 502 and "upstream_error" in body, f"HTTP {status}")

    print("== 思考块不被漏出（上游按 5 字符切片发）==")
    gw = ChatGateway(Upstream(base=base, token=TOKEN), default_model=model)
    sid = gw.up.open_session(model)
    raw = "".join(f.get("text", "") for f in gw.up.stream("想想", sid, thinking=True))
    check("上游确实返回了 <think> 原文", "<think>" in raw)
    delivered = gateway_stream_text(gw, model, "想想")
    check("交付给客户端的内容不含标签且正文完整",
          "<think>" not in delivered and "假上游收到" in delivered, delivered[:60])

    print()
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项: " + ", ".join(FAILED))
        return 1
    print("本地端到端全绿：假上游 + 真端点，全程不出网。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
