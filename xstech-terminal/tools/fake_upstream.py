#!/usr/bin/env python3
"""一个假的 xstech.one 上游，跑在本地，用来开发和验证，不碰真站点。

为什么要有它：

- 上游是别人的私有协议，随时会改、会限流、会封 IP。把开发和测试押在
  它的稳定性上，等于每改一行代码都得求人。
- 单测（`tests/`）打桩上游对象，验证的是协议翻译逻辑；这个假上游验证的是
  **真的走了一遍 HTTP**：登录头、`{code,data,msg}` 包装、SSE 帧格式、
  被分片切开的 `<think>` —— 这些只有在真实 socket 上才会暴露。

行为尽量对齐真站点（路径常量见 src/upstream.py）：
- 响应统一包成 {"code": 0, "data": ..., "msg": ""}
- 鉴权走 `Authorization: <token>`，**不带 Bearer 前缀**
- 验证码接口返回一张真图，块内的像素直接从原图同一坐标抠出来，
  所以滑块求解是真的在解题，不是走过场
- 流式对话返回 text/event-stream，每帧 data 里再套一层

用法：
    python tools/fake_upstream.py              # 前台跑 127.0.0.1:9999
    python tools/fake_upstream.py --detach     # 后台跑，pid 写进 --pidfile
    python tools/fake_upstream.py --port 0     # 随机端口（测试用）
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

# 和真站点一样的形状：value 是 `分组::模型名`
MODELS = [
    {"value": "openai::gpt-5.6-terra", "label": "GPT-5.6-Terra"},
    {"value": "deepseek::deepseek-v4-pro", "label": "Deepseek-V4-Pro"},
]

TOKEN = "fake-token-local"
CODE = "123456"


def _png_data_url(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def make_challenge(rng: random.Random) -> dict:
    """造一道真滑块题：先画背景，再把其中一块抠出来当模板。

    关键约束（和真站点一致）：块内像素**直接取自原图同一坐标**。求解之所以能
    退化成「同一 Y 坐标上的横向模板匹配」，全靠这一点。造错了本地怎么测都是绿的，
    接上真站点就瞎。
    """
    h, w = 160, 320
    yy, xx = np.mgrid[0:h, 0:w]
    base = rng.randint(30, 90)
    arr = np.clip(base + (xx // 4) + rng.randint(0, 40), 0, 255).astype(np.uint8)
    arr = np.stack([arr] * 3, axis=-1)
    noise = np.random.default_rng(rng.getrandbits(32)).integers(-25, 25, (h, w, 3))
    arr = np.clip(arr.astype(int) + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr)
    draw = ImageDraw.Draw(img)
    for _ in range(12):
        r = rng.randint(8, 28)
        x, y = rng.randint(0, w - 1), rng.randint(0, h - 1)
        draw.ellipse((x - r, y - r, x + r, y + r),
                     fill=tuple(rng.randint(0, 255) for _ in range(3)))
    arr = np.asarray(img)

    sh, sw = rng.randint(36, 48), rng.randint(36, 48)
    ty = rng.randint(10, h - sh - 10)
    tx = rng.randint(60, w - sw - 10)
    thumb = np.zeros((sh, sw, 3), np.uint8)
    shape = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(shape).ellipse((5, 3, sw - 6, sh - 4), fill=255)
    mask = np.asarray(shape) > 0
    thumb[mask] = arr[ty:ty + sh, tx:tx + sw][mask]  # 同一坐标，别动

    return {
        "id": f"cap-{rng.getrandbits(48):x}",
        "image": _png_data_url(arr),
        "thumb": _png_data_url(thumb),
        "thumbY": int(ty),
        "width": sw,
        "_answerX": int(tx),  # 只给假上游自己判分用，不给客户端
    }


class State:
    """内存态：够跑一遍 register → login → 对话，进程退出即消失。"""

    def __init__(self, password: str):
        self.password = password
        self.challenges: dict[str, dict] = {}
        self.used: set[str] = set()          # 每个题目只允许核验一次，跟真站点一样
        self.accounts: dict[str, str] = {}
        self.lock = threading.Lock()

    def new_challenge(self, rng: random.Random) -> dict:
        ch = make_challenge(rng)
        with self.lock:
            self.challenges[ch["id"]] = ch
        return ch


def _frames(text: str, chunk: int = 5) -> list[str]:
    """把回复切碎成 SSE 帧。

    默认切得很碎（5 个字符一帧），且 **`<think>` 这个标签本身也跨帧**
    —— 专门用来撞流式过滤的经典坑：标签被从中间切开，有状态实现一旦某帧
    判断错就一路错到底。别把切片调大，调大了这个用例就白写了。
    """
    out = [json.dumps({"id": 1, "type": "string", "data": text[i:i + chunk], "code": 0},
                      ensure_ascii=False)
           for i in range(0, len(text), chunk)]
    out.append(json.dumps({"id": 1, "type": "object",
                           "data": {"usage": {"prompt_tokens": 3,
                                              "completion_tokens": len(text)}},
                           "code": 0}, ensure_ascii=False))
    out.append("[DONE]")
    return out


def make_handler(state: State, rng: random.Random):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if os.environ.get("FAKE_UPSTREAM_VERBOSE"):
                sys.stderr.write("[fake-upstream] " + fmt % args + "\n")

        def handle_one_request(self):
            # 客户端（尤其 curl）经常不等响应读完就断开，默认实现会把一坨
            # 栈回溯打到 stderr，噪音盖过真正想看的东西。
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def _json(self, data, code: int = 0, msg: str = "", status: int = 200):
            body = json.dumps({"code": code, "data": data, "msg": msg},
                              ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _authed(self) -> bool:
            # 真站点这里**不带 Bearer 前缀**
            if (self.headers.get("Authorization") or "").strip() == TOKEN:
                return True
            self._json(None, code=2, msg="登录已过期")
            return False

        # ---------- GET ----------

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path == "/api/site/info":
                return self._json({"name": "fake-xstech", "version": "3.4.0"})
            if path == "/api/chat/tmpl":
                return self._json({"models": MODELS})
            if path == "/api/user/info":
                if not self._authed():
                    return
                return self._json({"id": 10001, "email": "fake@example.com",
                                   "nickname": "假上游同学"})
            if path == "/health":  # 给探活用
                return self._json({"status": "ok"})
            self._json(None, code=404, msg="Not Found")

        # ---------- POST ----------

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            body = self._body()
            if path == "/api/user/captcha/action":
                return self._json(state.new_challenge(rng))
            if path == "/api/user/captcha/action/ver":
                with state.lock:
                    ch = state.challenges.get(body.get("id", ""))
                    if ch is None or ch["id"] in state.used:
                        return self._json(False)  # 交过的题作废
                    state.used.add(ch["id"])
                    ok = (int(body.get("x", -1)) == ch["_answerX"]
                          and int(body.get("y", -1)) == ch["thumbY"])
                return self._json(ok)
            if path == "/api/user/send/validate-code":
                return self._json(None)
            if path == "/api/user/register":
                email = body.get("account", "")
                if body.get("code") != CODE:
                    return self._json(None, code=4001, msg="验证码错误")
                state.accounts[email] = body.get("password", "")
                return self._json({"token": TOKEN, "email": email})
            if path == "/api/user/login":
                email = body.get("account", "")
                if state.accounts.get(email) != body.get("password"):
                    return self._json(None, code=4001, msg="账号或密码错误")
                return self._json({"token": TOKEN, "email": email})
            if path == "/api/chat/session":
                return self._json({"id": rng.randint(1, 10 ** 6)})
            if path == "/api/chat/completions":
                return self._completions(body)
            self._json(None, code=404, msg="Not Found")

        def _completions(self, body: dict):
            text = body.get("text", "")
            reply = f"假上游收到：「{text}」，这是一条本地回显。"
            if body.get("thinking"):
                # 思考块刻意做长，保证会被分片从中间切开
                reply = ("<think>这里是推理过程，本地假上游随口编的，"
                         "长度做够以便被 SSE 分片从中间切开。</think>") + reply
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for item in _frames(reply):
                    data = f"data: {item}\n\n".encode()
                    self.wfile.write(f"{len(data):X}\r\n".encode())
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.003)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def build_server(host: str, port: int, password: str, seed: int | None = None,
                 token: str = TOKEN, code: str = CODE):
    """给测试用的构造入口：随机端口传 port=0，从 httpd.server_port 取真实端口。"""
    state = State(password)
    state.token = token
    state.code = code
    rng = random.Random(seed)
    return ThreadingHTTPServer((host, port), make_handler(state, rng))


def main() -> int:
    ap = argparse.ArgumentParser(description="本地假 xstech.one 上游")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--password", default=os.environ.get("XSTECH_PASSWORD", "Test123456"),
                    help="注册/登录用的密码，要和客户端一致")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--detach", action="store_true", help="后台运行")
    ap.add_argument("--pidfile", default="/tmp/fake-upstream.pid")
    args = ap.parse_args()

    if args.detach:
        pid = os.fork()
        if pid:
            with open(args.pidfile, "w") as f:
                f.write(str(pid))
            print(f"假上游 pid={pid} → http://{args.host}:{args.port}")
            return 0
        os.setsid()

    httpd = build_server(args.host, args.port, args.password, args.seed)
    print(f"假上游已启动 → http://{args.host}:{args.port}（Ctrl-C 退出）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
