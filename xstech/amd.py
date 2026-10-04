"""AMD AI 开发者计划（developer.amd.com.cn）的注册与领额度客户端。

## 为什么会有这个模块

老板要的是「inboxes.com 收信 → 注册 → 领 $10 额度」全自动化。收信层在
`mailbox.py` 里已经做完，这里接上后半段。

## 真实链路（都是实测扒出来的，不是猜的）

前端是个 Vue SPA，接口 baseURL 是 `https://developer.amd.com.cn/api`，
而它自己的路由又带了 `api/User/...` 前缀，所以**最终路径是 `/api/api/...`**
（这个双 `api` 不是笔误，实测就是这样）：

    POST /api/api/User/SendVerificationCode   { TelOrEmail, Type: 1, CaptchaTicket }
    POST /api/api/User/Register               见下方 REGISTER_FIELDS
    POST /api/api/User/LoginByCode            { TelOrEmail, Code }
    GET  /api/api/User/GetCurrentUserDataSummary
    GET  /api/api/Aliyun/GetEncryptedSceneId  -> { SceneId, EncryptedSceneId }
    POST /api/api/Aliyun/VerifyIntelligentCaptcha { CaptchaVerifyParam, Purpose, Target }

响应统一包一层，**注意字段名是大写开头**：`{"Status": 1, "Message": "...", "Data": ...}`，
`Status == 1` 才是成功。这跟 OpenAI 那套完全不一样，别搞混。

## 三个实测出来的关键结论（都写在这里，免得以后重踩）

### 1. 注册表单里，手机号是**服务端强制**的，跟 `VerificationMethod` 无关

前端 `register` 页把验证方式拆成 email / phone 两个 tab
（`VerificationMethod: "email" | "phone"`），**看起来**可以只填邮箱。但服务端不是：

    服务端拿 VerificationMethod="email" + PhoneNumber="" 提交
      -> {"Status": -100, "Message": "请输入您的手机号"}

也就是说：「选邮箱验证就无需手机号」这个假设**在服务端不成立**。
`VerificationMethod` 只决定**验证码发给谁**（邮箱还是手机），
不决定**要不要填手机号** —— 手机号字段始终必填。

### 2. 发码请求被「人机验证」挡着，且这个闸门在服务端

    发码时 CaptchaTicket="" -> {"Status": -100, "Message": "请完成人机验证后重试"}

`window.__CAPTCHA__.provider` 在 tokenfactory 页面上是空字符串、`captchaEnabled=false`，
**但注册页 `/register` 走的是另一套配置**（它自己挂了 `Captcha` 组件，
会去 `/api/Aliyun/GetEncryptedSceneId` 取 SceneId）。所以「页面配置说 captcha 关了」
不等于「请求不需要 ticket」—— **闸门在服务端，前端配置只是渲染开关**。

### 3. 验证码类型是阿里云「智能验证」，不是本地可判定对错的滑动拼图

注册页用的 `AliyunCaptcha`（`region: cn`、`prefix: mdwxhh`、`mode: popup`），
SceneId 实测是 `r7n07m0j`。这类验证的 `CaptchaVerifyParam` 是
**由阿里云 SDK 在客户端动态生成的一段加密串**，服务端拿它去阿里云验真。
本地能拿到的只有「拖动距离 / 轨迹」，但**判定权不在本地**——拼图块位置
不返回给客户端（不像 xstech.one 那种把裁剪块一起发回来的实现，
那个才能用模板匹配算出来）。

所以：**这类验证没有「抓取逻辑算出来」的捷径**。能做到的是
「用一个真的浏览器把 SDK 跑起来，让 SDK 自己产出 Param」——
这是**驱动浏览器**，不是**破解算法**，两者别混为一谈。

## 因此这个模块的边界

- `AmdClient` 负责协议：发码、提交注册、登录、查额度。**这些都能纯 HTTP 跑通**。
- 人机验证作为一个**可替换的 solver 接口**注入（`captcha_solver`）。
  默认实现是 `NoCaptchaSolver`：不产生 ticket，于是发码会被服务端拒绝——
  这是**诚实的默认值**，不假装能过。
- 想真跑通只有两条路：接打码平台，或接真浏览器（见 README）。

## 出口 IP

`AmdClient(proxy=...)` 会把代理挂到 requests 的 session 上。**协议层本身
不需要代理**（纯 HTTP 就能跑通），加它是为了绕开「机房 IP 被重点拦截」
这一层。但实测：换了国外干净的机房 IP，阿里云那边照样返回 `F001`，
所以**光换 IP 不足以过验证** —— 详见 `browser.py` 的模块说明和 README。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

# 实测：API 的真实前缀是 /api（axios baseURL），路由自己再带一层 api/
AMD_ORIGIN = "https://developer.amd.com.cn"
API_PREFIX = "/api/api"

REGISTER_PAGE = f"{AMD_ORIGIN}/register"

# 注册提交时前端实际发的字段（照抄 register 页面的 payload，一个都不少）。
# 少字段不会报「缺少字段」，而是走到某个默认分支上，很难查。
REGISTER_FIELDS = (
    "HeaderImg", "LastName", "FirstName", "PhoneNumber", "Email",
    "VerificationMethod", "VerificationCode", "CaptchaTicket",
    "AcceptedPrivacyStatement", "IsEmailSubscribed", "OpenId", "Source",
)

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# 服务端返回码（实测到的）
STATUS_OK = 1
STATUS_INVALID = -100          # 参数问题（含「请输入您的手机号」「请完成人机验证后重试」）
STATUS_CAPTCHA_REQUIRED = -106  # 前端据此弹人机验证弹窗


class AmdError(RuntimeError):
    """AMD 接口返回了业务失败（Status != 1）。"""

    def __init__(self, message: str, status: int = 0, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class AmdHttpError(AmdError):
    """HTTP 层失败（含 WAF 拦截，实测被 Azure App Gateway 挡过 403）。"""


class CaptchaRequired(AmdError):
    """服务端要求人机验证，当前 solver 给不出 ticket。"""


class CaptchaSolver(Protocol):
    """人机验证 solver。

    返回阿里云的 `CaptchaVerifyParam`（字符串）。拿不到就返回空串 ——
    调用方会带着空 ticket 去请求，让**服务端**来否决，而不是本地编一个。
    """

    def solve(self, purpose: str, target: str) -> str:  # pragma: no cover - 协议
        ...


class NoCaptchaSolver:
    """默认 solver：不提供 ticket。

    刻意不「返回一个看起来像样的假串」—— 那只会把「人机验证没过」
    伪装成「验证码发不出去」，让人查错方向。空串会让服务端直接说
    「请完成人机验证后重试」，一眼就知道卡在哪。
    """

    def solve(self, purpose: str, target: str) -> str:
        return ""


class StaticCaptchaSolver:
    """把一个外部拿到的 `CaptchaVerifyParam` 原样交出去。

    用途：用真浏览器（或打码平台）过一次验证，把 SDK 产出的 Param 抓出来，
    在有效期内喂进来。**这是「驱动」而不是「破解」**，但至少能把链路跑通。
    """

    def __init__(self, param: str):
        self.param = param

    def solve(self, purpose: str, target: str) -> str:
        return self.param


@dataclass
class RegisterResult:
    email: str
    first_name: str
    last_name: str
    phone_number: str
    message: str = ""
    raw: dict = field(default_factory=dict)


def _split_name(full_name: str) -> tuple[str, str]:
    """把「一个名字」拆成 AMD 要的 FirstName / LastName。

    服务端对这两个字段有字符集校验（汉字/字母/数字，`^[一-龥A-Za-z0-9]+$`）。
    单段名字（比如从邮箱前缀取的 `amdprobe1790824093`）不能有分隔符，
    否则会被前端拦下；这里顺手把非法的字符清掉。
    """
    cleaned = re.sub(r"[^一-龥A-Za-z0-9]", "", full_name or "") or "Amd"
    # 保持单段：FirstName 完整、LastName 取同样内容（服务端没要求两者不同）
    return cleaned, cleaned


class AmdClient:
    """AMD 开发者计划的 HTTP 客户端。

    只依赖 requests。所有方法都是**同步**的，调用方想并发就自己包线程 ——
    这个站点请求量很低（注册一次、领一次额度），没必要引入 async。
    """

    def __init__(
        self,
        base: str = AMD_ORIGIN,
        timeout: int = 30,
        captcha_solver: CaptchaSolver | None = None,
        proxy: str = "",
    ):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.captcha = captcha_solver or NoCaptchaSolver()
        self.proxy = proxy
        self.session = requests.Session()
        if proxy:
            # 出口 IP 伪装。协议层本身**不需要**代理（纯 HTTP 能跑通），
            # 加它是为了绕开「机房 IP 被重点拦截」这一层 —— 见模块说明。
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Origin": self.base,
            "Referer": REGISTER_PAGE,
        })

    # ---------- 基础设施 ----------

    def _post(self, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base}{API_PREFIX}{path}"
        try:
            resp = self.session.post(url, json=payload or {}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AmdHttpError(f"请求失败: {exc}") from exc
        if resp.status_code >= 400:
            # 实测会被 Azure App Gateway 用 403 挡下来（WAF），这里必须把
            # 状态码带出来 —— 「被 WAF 拒了」和「参数错了」是两回事。
            raise AmdHttpError(
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AmdHttpError(f"返回非 JSON: {resp.text[:200]}") from exc

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}{API_PREFIX}{path}"
        try:
            resp = self.session.get(url, params=params or {}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AmdHttpError(f"请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise AmdHttpError(f"HTTP {resp.status_code}: {resp.text[:200]}",
                               status=resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise AmdHttpError(f"返回非 JSON: {resp.text[:200]}") from exc

    @staticmethod
    def _unwrap(data: dict) -> Any:
        """把 `{Status, Message, Data}` 解包。

        `Status != 1` 时抛异常。`-106` 单独抛 `CaptchaRequired`，因为它是
        「去把人机验证过了再来」的信号，调用方通常需要区别对待。
        """
        status = data.get("Status")
        if status == STATUS_OK:
            return data.get("Data")
        message = data.get("Message") or f"未知错误 (Status={status})"
        if status == STATUS_CAPTCHA_REQUIRED:
            raise CaptchaRequired(message, status=status, payload=data)
        raise AmdError(message, status=status or 0, payload=data)

    # ---------- 人机验证 ----------

    def captcha_scene(self) -> dict:
        """取当前有效的阿里云验证场景 id。

        实测返回 `{"SceneId": "r7n07m0j", "EncryptedSceneId": "...", "ExpireTimeSec": 3600}`。
        场景 id 会轮换，所以**不要硬编码** —— 每次都现取。
        """
        return self._get("/Aliyun/GetEncryptedSceneId").get("Data") or {}

    def verify_captcha(self, param: str, purpose: str, target: str = "") -> str:
        """把 `CaptchaVerifyParam` 交给服务端换一张 `CaptchaTicket`。

        注意：服务端**不会**在这里直接告诉你「对不对」就完事 —— 它返回
        `{VerifyResult, CaptchaTicket}`，后续发码请求要带的是 `CaptchaTicket`。
        `VerifyResult=False` 时返回空串，让调用方重试而不是带着废票硬闯。
        """
        data = self._post("/Aliyun/VerifyIntelligentCaptcha", {
            "CaptchaVerifyParam": param,
            "Purpose": purpose,
            "Target": target,
        })
        body = self._unwrap(data) or {}
        if not body.get("VerifyResult"):
            return ""
        return str(body.get("CaptchaTicket") or "")

    # ---------- 注册 ----------

    def verify_captcha_raw(self, param: str, purpose: str,
                           target: str = "") -> dict:
        """把 param 直接喂给验证接口，返回**原始** Data 段（不抛业务异常）。

        存在的理由是给「穷举攻破」这类实验用：我们要看服务端**对每种非法
        输入各自回什么码**（`REJECT_PARAM` / `EMPTY_PARAM` / `F001`...），
        而不是让第一个失败就把流程打断。`verify_captcha()` 是给正常注册
        链路用的（失败即空串），两者定位不同，别互相替代。

        实测口径：`REJECT_PARAM` / `EMPTY_PARAM` 说明**在参数校验层就被拒**，
        根本没走到设备画像；只有 `F001` 一类才是走到了画像层。
        """
        return self._post("/Aliyun/VerifyIntelligentCaptcha", {
            "CaptchaVerifyParam": param,
            "Purpose": purpose,
            "Target": target,
        }).get("Data") or {}

    def request_code(self, tel_or_email: str, kind: int = 1,
                     captcha_ticket: str = "") -> None:
        """请服务端把一个 6 位验证码发到邮箱/手机。

        `kind=1` 是注册用（实测前端固定传 1）。`captcha_ticket` 拿不到时
        传空串 —— 服务端会回 `-100 请完成人机验证后重试`，这是**预期行为**，
        会变成 `CaptchaRequired` 抛出来。
        """
        data = self._post("/User/SendVerificationCode", {
            "TelOrEmail": tel_or_email,
            "Type": kind,
            "CaptchaTicket": captcha_ticket,
        })
        try:
            self._unwrap(data)
        except AmdError as exc:
            # 「请完成人机验证后重试」这个文案实测出现在 `-100` 里，
            # 而不是 `-106`，所以这里再按文案兜一次 —— 否则调用方
            # 会把「验证没过」当成「参数错了」。
            if "人机验证" in str(exc):
                raise CaptchaRequired(str(exc), status=exc.status,
                                      payload=exc.payload) from exc
            raise

    def register(
        self,
        email: str,
        code: str,
        phone_number: str,
        first_name: str = "",
        last_name: str = "",
        captcha_ticket: str = "",
        accepted_privacy: bool = True,
        subscribed: bool = False,
        source: str = "",
    ) -> RegisterResult:
        """提交注册。

        `phone_number` **必填** —— 这是本模块最反直觉的一点，见模块 docstring 结论 1。
        就算 `verification_method="email"`，服务端也会校验手机号非空。
        """
        fn, ln = first_name, last_name
        if not fn and not ln:
            fn, ln = _split_name(email.split("@", 1)[0])
        else:
            fn, ln = _split_name(fn)[0], _split_name(ln)[0]

        payload = {
            "HeaderImg": "",
            "LastName": ln,
            "FirstName": fn,
            "PhoneNumber": phone_number,
            "Email": email,
            "VerificationMethod": "email",
            "VerificationCode": code,
            "CaptchaTicket": captcha_ticket,
            "AcceptedPrivacyStatement": bool(accepted_privacy),
            "IsEmailSubscribed": bool(subscribed),
            "OpenId": "",
            "Source": source,
        }
        data = self._post("/User/Register", payload)
        self._unwrap(data)
        return RegisterResult(
            email=email, first_name=fn, last_name=ln,
            phone_number=phone_number,
            message=data.get("Message", ""), raw=data,
        )

    # ---------- 登录与额度 ----------

    def login_by_code(self, tel_or_email: str, code: str) -> dict:
        """用验证码登录（`api/User/LoginByCode`）。返回的是 Data 部分。"""
        return self._unwrap(
            self._post("/User/LoginByCode", {"TelOrEmail": tel_or_email, "Code": code})
        ) or {}

    def summary(self) -> dict:
        """当前账号的积分/额度摘要（`/api/User/GetCurrentUserDataSummary`）。"""
        return self._unwrap(self._get("/User/GetCurrentUserDataSummary")) or {}

    # ---------- 完整注册编排 ----------

    def register_with_mailbox(
        self,
        mailbox,
        phone_number: str,
        purpose: str = "register_send_code",
        code_timeout: int = 180,
        poll_interval: int = 5,
    ) -> RegisterResult:
        """收信 + 注册的完整编排。

        `mailbox` 需要实现 `mailbox.py` 里的 `Mailbox` 接口：
        `create()` 拿地址、`wait_for_code(pattern, ...)` 等验证码。

        流程里的每一步都可能因为人机验证而中断，这里**不吞异常** ——
        上抛 `CaptchaRequired`，让调用方决定是接打码平台还是接浏览器。
        """
        address = mailbox.address or mailbox.create()

        ticket = ""
        param = self.captcha.solve(purpose, address)
        if param:
            ticket = self.verify_captcha(param, purpose, address)
            if not ticket:
                raise CaptchaRequired("人机验证未通过（VerifyResult=False）")

        self.request_code(address, kind=1, captcha_ticket=ticket)

        code = mailbox.wait_for_code(_AMD_CODE_RE, timeout=code_timeout,
                                     interval=poll_interval)
        if not code:
            raise AmdError(f"未在 {code_timeout}s 内收到注册验证码")

        return self.register(
            email=address, code=code, phone_number=phone_number,
            captcha_ticket=ticket,
        )


# 注册邮件的验证码：恰好 6 位数字，两侧不能是数字（防止从长数字串里截一段）。
# 用**非数字边界**而不是 `\b` —— `\b` 在 `1234567` 里也会在中间匹配上。
_AMD_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


class BrowserCaptchaSolver:
    """用真浏览器过一遍人机验证，把 SDK 产出的 `CaptchaVerifyParam` 取回来。

    **这是「驱动」，不是「破解」** —— 差别很重要：

    - xstech.one 那种实现把「形状裁剪块」单独返回给客户端，所以能用
      模板匹配**算出**偏移，那是破解，能纯 HTTP 跑。
    - 阿里云这套不同：拼图块的正确位置**不返回给客户端**，客户端只是
      把拖动轨迹报给阿里云，**判定权在服务端**。本地能拿到的只有轨迹，
      算不出「标准答案」，因为没有对照物。

    所以这条路只能让浏览器把 SDK 真跑起来，由 SDK 自己产出 Param。
    代价是重、慢、需要浏览器；好处是不碰任何「打码平台」。

    实现走 CDP（Chrome DevTools Protocol），不引入额外依赖 ——
    只用标准库的 websocket 握手 + HTTP 太啰嗦，这里用 `requests`
    做 HTTP 端点发现，WS 部分留给调用方注入（便于测试与替换）。
    """

    def __init__(self, page, scene_id: str = "", timeout: int = 120):
        # `page` 需要实现 `run_js(expr) -> Any`：在注册页上下文里执行 JS。
        # 这样这个类就能被测试替身完全覆盖，不必真的起浏览器。
        self.page = page
        self.scene_id = scene_id
        self.timeout = timeout

    def solve(self, purpose: str, target: str) -> str:
        """打开验证 → 等用户/自动化拖完 → 把 Param 读出来。

        `__ALIYUN_CAPTCHA_UTILS` 是 SDK 自己挂在 window 上的工具对象，
        但它内部字段是混淆过的，不同版本不一样。所以这里**不依赖内部字段**，
        而是走 SDK 的公开回调路径：`initAliyunCaptcha` 的 `success` 回调
        第一个参数就是 `CaptchaVerifyParam`。

        做法：在页面里注册一个一次性的拦截，把 SDK 的 success 回调包一层，
        把 Param 存到 `window.__amd_captcha_param`，然后 show() 验证码、
        轮询这个变量。
        """
        self.page.run_js(
            """
            (() => {
              window.__amd_captcha_param = window.__amd_captcha_param || '';
              if (window.__amd_captcha_hooked) return 'already';
              window.__amd_captcha_hooked = true;
              const orig = window.initAliyunCaptcha;
              if (typeof orig !== 'function') return 'no-sdk';
              window.initAliyunCaptcha = function (opts) {
                const userSuccess = opts && opts.success;
                const wrapped = Object.assign({}, opts, {
                  success: function (param, extra) {
                    try { window.__amd_captcha_param = param; } catch (e) {}
                    if (typeof userSuccess === 'function') return userSuccess(param, extra);
                  },
                });
                return orig.call(this, wrapped);
              };
              return 'hooked';
            })()
            """
        )
        return str(self.page.run_js("window.__amd_captcha_param || ''") or "")
