"""HTTP 服务：对外暴露 OpenAI 兼容端点。

只依赖标准库，因为部署场景经常是「一台什么都没有的机器 + 一条命令」。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .gateway import ChatGateway
from .upstream import Upstream, UpstreamError

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


def make_handler(gateway: ChatGateway, api_key: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 保持输出干净
            pass

        # ---------- 工具 ----------

        def _send(self, code: int, payload: dict | None = None, ctype="application/json"):
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in CORS.items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _error(self, message: str, code: int = 400, err_type: str = "invalid_request_error"):
            self._send(code, {"error": {"message": message, "type": err_type, "code": code}})

        def _authorized(self) -> bool:
            if not api_key:
                return True
            header = self.headers.get("Authorization", "")
            return header.replace("Bearer ", "").strip() == api_key

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")

        # ---------- 路由 ----------

        def do_OPTIONS(self):
            self._send(204)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/health":
                self._send(200, {"status": "ok", "version": __version__})
            elif path == "/v1/models":
                if not self._authorized():
                    return self._error("API key 无效", 401, "authentication_error")
                try:
                    self._send(200, {"object": "list", "data": gateway.list_models()})
                except UpstreamError as exc:
                    self._error(str(exc), 502, "upstream_error")
                except Exception as exc:  # noqa: BLE001
                    # 上游连不上时 requests 抛的是 ConnectionError 这类裸异常。
                    # 不放兜底会直接冒到 BaseHTTPRequestHandler 外面 —— 客户端
                    # 拿到的是「连接被断开」，看不出是上游的问题。
                    self._error(f"{type(exc).__name__}: {exc}", 502, "upstream_error")
            else:
                self._error("Not Found", 404)

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            if path == "/v1/chat/completions":
                if not self._authorized():
                    return self._error("API key 无效", 401, "authentication_error")
                try:
                    body = self._body()
                except json.JSONDecodeError:
                    return self._error("请求体不是合法 JSON")
                try:
                    if body.get("stream"):
                        self._stream(body)
                    else:
                        self._send(200, gateway.complete(body))
                except UpstreamError as exc:
                    self._error(str(exc), 502, "upstream_error")
                except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                    self._error(f"{type(exc).__name__}: {exc}", 500, "internal_error")
            else:
                self._error("Not Found", 404)

        def _stream(self, body: dict):
            # HTTP/1.1 下没有 Content-Length 就必须用 chunked，否则客户端
            # 会一直等一个永远不会到来的长度头，表现为「卡住不返回」。
            #
            # 生成器要手动推进：`gateway.stream()` 是惰性生成器，第一次 next()
            # 之前什么都还没发生 —— 上游鉴权失败、模型不存在这类错误都在那时才抛。
            # 如果等 end_headers() 之后再抛，头已经出去了，只能断开连接，
            # 客户端看到的是「连接被重置」而不是一条可读的错误。
            gen = gateway.stream(body)
            try:
                first = next(gen)
            except UpstreamError as exc:
                return self._error(str(exc), 502, "upstream_error")
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                return self._error(f"{type(exc).__name__}: {exc}", 500, "internal_error")

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")  # 让 nginx 别缓冲
            self.send_header("Transfer-Encoding", "chunked")
            for k, v in CORS.items():
                self.send_header(k, v)
            self.end_headers()

            def push(payload: str):
                data = payload.encode("utf-8")
                self.wfile.write(f"{len(data):X}\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            try:
                push(first)
                for chunk in gen:
                    push(chunk)
                self.wfile.write(b"0\r\n\r\n")  # 结束块
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端主动断开，正常

    return Handler


def serve(gateway: ChatGateway, host: str, port: int, api_key: str = "") -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(gateway, api_key))
    httpd.daemon_threads = True
    print(f"XSTECH Terminal Gateway 已启动 → http://{host}:{port}/v1")
    print(f"模型列表: curl http://{host}:{port}/v1/models")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
