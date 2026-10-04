"""xstech.one 网页端私有 API 的最小客户端。

站点前端（GoAmzAI Plus 3.4.0）用的是自己的一套协议，不是 OpenAI 协议：
所有响应统一包成 {code, data, msg}，流式对话走 POST /api/chat/completions，
返回 text/event-stream，每帧 data 里再套一层 {code, data}。

这个模块只做协议翻译，不含任何业务策略。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterator

import requests

APP_VERSION = "3.4.0"
DEFAULT_BASE = "https://xstech.one"

# 低于这个相关系数就认为匹配不可靠，换一道题重来
MIN_MATCH_CONFIDENCE = 0.60

# 站点前端把这些路径拼在 /api 下面
P_SITE_INFO = "/api/site/info"
P_CAPTCHA = "/api/user/captcha/action"
P_CAPTCHA_VERIFY = "/api/user/captcha/action/ver"
P_SEND_CODE = "/api/user/send/validate-code"
P_REGISTER = "/api/user/register"
P_LOGIN = "/api/user/login"
P_USER_INFO = "/api/user/info"
P_TMPL = "/api/chat/tmpl"
P_SESSION = "/api/chat/session"
P_COMPLETIONS = "/api/chat/completions"


class UpstreamError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class Credentials:
    token: str
    email: str = ""


@dataclass
class Upstream:
    base: str = DEFAULT_BASE
    token: str = ""
    timeout: int = 120
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.base = self.base.rstrip("/")
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            "X-APP-VERSION": APP_VERSION,
            "Origin": self.base,
            "Referer": f"{self.base}/auth",
        })

    # ---------- 基础设施 ----------

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Content-Type": "application/json", "X-Locale": "zh-CN"}
        if self.token:
            h["Authorization"] = self.token
        if extra:
            h.update(extra)
        return h

    def _unwrap(self, resp: requests.Response) -> dict:
        try:
            body = resp.json()
        except ValueError as exc:  # pragma: no cover - 上游返回非 JSON
            raise UpstreamError(f"上游返回非 JSON（HTTP {resp.status_code}）") from exc
        code = body.get("code")
        if code == 2:
            raise UpstreamError("登录已过期，请重新登录", code=2)
        if code != 0:
            raise UpstreamError(body.get("msg") or f"上游错误 code={code}", code=code)
        return body.get("data")

    def _post(self, path: str, payload: dict, headers: dict | None = None) -> dict:
        resp = self.session.post(
            self._url(path), json=payload, headers=self._headers(headers), timeout=self.timeout
        )
        return self._unwrap(resp)

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self.session.get(
            self._url(path), params=params, headers=self._headers(), timeout=self.timeout
        )
        return self._unwrap(resp)

    # ---------- 站点信息与模型 ----------

    def site_info(self) -> dict:
        return self._get(P_SITE_INFO)

    def models(self) -> list[dict]:
        """真实模型清单。value 形如 `openai::gpt-5.6-terra`，前面那段是上游分组。"""
        tmpl = self._get(P_TMPL)
        out = []
        for item in tmpl.get("models", []):
            value = item.get("value", "")
            group, _, model_id = value.partition("::")
            out.append({
                "id": value,
                "group": group,
                "model": model_id or value,
                "label": item.get("label") or value,
                "caps": (item.get("attr") or {}).get("capabilities") or {},
            })
        return out

    # ---------- 账号 ----------

    def _challenge(self) -> dict:
        return self._post(P_CAPTCHA, {"height": 36, "width": 100})

    def _verify(self, captcha_id: str, offset: int, y: int) -> bool:
        data = self._post(
            P_CAPTCHA_VERIFY, {"id": captcha_id, "x": int(offset), "y": int(y)}
        )
        return bool(data)

    def solve_challenge(self, attempts: int = 5) -> str:
        """取一道滑块题、解出来并核验，返回已通过核验的 captchaId。

        每个 id 只允许提交一次核验，失败即作废 —— 所以「重试」只能是
        「换一道新题再来」，不能对同一题重复提交。

        背景是匀色天空时相关峰会变钝，实测单次命中率约 90%，因此对
        低置信度的题直接弃掉重开，不浪费核验机会。
        """
        from .captcha import MissingImagingError, solve_offset_with_confidence

        last_error = "滑块验证未通过"
        for _ in range(attempts):
            ch = self._challenge()
            y = ch.get("thumbY", 0)
            try:
                offset, score = solve_offset_with_confidence(ch["image"], ch["thumb"], y)
            except MissingImagingError as exc:
                # 缺依赖是可修的环境问题，不该伪装成「验证未通过」或漏出裸 traceback
                raise UpstreamError(str(exc)) from exc
            if score < MIN_MATCH_CONFIDENCE:
                continue  # 这题背景太素，重开一道
            if self._verify(ch["id"], offset, y):
                return ch["id"]
            last_error = f"滑块验证未通过（置信度 {score:.2f}）"
        raise UpstreamError(f"{last_error}，已重试 {attempts} 次")

    def send_register_code(self, email: str) -> None:
        cid = self.solve_challenge()
        self._post(
            P_SEND_CODE,
            {"account": email, "type": "register", "captchaId": cid, "captcha": "1"},
        )

    def register(self, email: str, password: str, code: str) -> Credentials:
        cid = self.solve_challenge()
        data = self._post(
            P_REGISTER,
            {
                "account": email,
                "password": password,
                "code": code,
                "captchaId": cid,
                "captcha": "1",
                "agreement": True,
            },
        )
        return Credentials(token=data["token"], email=email)

    def login(self, email: str, password: str) -> Credentials:
        data = self._post(P_LOGIN, {"account": email, "password": password})
        return Credentials(token=data.get("token", ""), email=email)

    def user_info(self) -> dict:
        return self._get(P_USER_INFO)

    # ---------- 对话 ----------

    def open_session(self, model: str) -> int:
        data = self._post(
            P_SESSION,
            {
                "model": model,
                "plugins": [],
                "mcp": [],
                "webSearch": False,
                "nativeTools": [],
                "nativeToolOptions": {},
                "reasoningEffort": "",
            },
        )
        return int(data["id"])

    def stream(
        self,
        text: str,
        session_id: int,
        model: str = "",
        thinking: bool = False,
        web_search: bool = False,
    ) -> Iterator[dict]:
        """逐帧产出 {type: 'delta'|'meta'|'done', ...}。

        上游每帧是 {"id":..., "type":"string"|"object", "data": ..., "code":0}，
        type=string 时 data 是增量文本，type=object 时是最终记录（含 token 统计）。
        """
        payload = {
            "text": text,
            "sessionId": session_id,
            "files": [],
            "thinking": thinking,
            "webSearch": web_search,
            "nativeTools": [],
            "nativeToolOptions": {},
            "reasoningEffort": "",
        }
        headers = self._headers({"Accept": "text/event-stream"})
        with self.session.post(
            self._url(P_COMPLETIONS), json=payload, headers=headers, stream=True, timeout=self.timeout
        ) as resp:
            if resp.status_code >= 400:
                raise UpstreamError(f"上游 HTTP {resp.status_code}")
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype:
                # 上游出错时会退化成 JSON/text
                body = resp.text[:400]
                try:
                    j = json.loads(body)
                    raise UpstreamError(j.get("msg") or body)
                except json.JSONDecodeError:
                    raise UpstreamError(body or "上游未返回事件流")

            for raw in resp.iter_lines(decode_unicode=False):
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    yield {"type": "done"}
                    return
                try:
                    frame = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if frame.get("code") not in (0, None):
                    yield {"type": "error", "message": frame.get("msg") or frame.get("err") or "上游错误"}
                    return
                data = frame.get("data")
                if isinstance(data, str):
                    yield {"type": "delta", "text": data}
                elif isinstance(data, dict):
                    yield {"type": "meta", "raw": data}
            yield {"type": "done"}
