"""离线单测：AMD 注册链路不联网，用假 HTTP 层验证协议与判定逻辑。

这个模块最容易错的不是网络，而是**「什么算成功」**：

- AMD 的响应体是 `{Status, Message, Data}` 且 **Status 是大写开头**；
  `Status != 1` 就是失败，但它**同时**用 HTTP 200 返回，所以只看
  状态码会以为成功。
- `-106` 和「请完成人机验证后重试」是**两个不同的信号**，都表示
  「人机验证没过」，但一个走状态码、一个走文案。测试要分别钉住。
- 手机号必填这一点最反直觉：`VerificationMethod="email"` 时服务端
  **依然**校验手机号，靠的是文案「请输入您的手机号」。这不是猜的，
  是实测响应。
"""

import pytest

from xstech.amd import (
    AmdClient,
    AmdError,
    AmdHttpError,
    CaptchaRequired,
    NoCaptchaSolver,
    StaticCaptchaSolver,
    _split_name,
)


class FakeAmd(AmdClient):
    """把 HTTP 层换成脚本化响应，其余逻辑照跑。"""

    def __init__(self, responses, solver=None):
        super().__init__(captcha_solver=solver)
        # responses: list[dict]，按调用顺序弹出
        self._responses = list(responses)
        self.calls = []

    def _post(self, path, payload=None):
        self.calls.append(("POST", path, payload))
        if not self._responses:
            raise AssertionError(f"未预期的第 {len(self.calls)} 次调用: {path}")
        return self._responses.pop(0)

    def _get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if not self._responses:
            raise AssertionError(f"未预期的第 {len(self.calls)} 次调用: {path}")
        return self._responses.pop(0)


def ok(data=None, message="请求成功"):
    return {"Data": data, "Status": 1, "Message": message}


def fail(status, message):
    return {"Data": None, "Status": status, "Message": message}


# ---------- 响应解包 ----------

def test_status_one_is_success_even_with_empty_data():
    fake = FakeAmd([ok(None)])
    assert fake._post("/x")["Status"] == 1


def test_non_one_status_raises_with_message():
    fake = FakeAmd([fail(-100, "请输入您的手机号")])
    with pytest.raises(AmdError) as ei:
        fake._unwrap(fake._post("/x"))
    assert "手机号" in str(ei.value)
    assert ei.value.status == -100


def test_captcha_status_becomes_captcha_required():
    """`-106` 必须单独成类 —— 调用方要根据它决定「去过验证」而不是「改参数」。"""
    fake = FakeAmd([fail(-106, "")])
    with pytest.raises(CaptchaRequired) as ei:
        fake._unwrap(fake._post("/x"))
    assert ei.value.status == -106


# ---------- 人机验证 ----------

def test_empty_captcha_ticket_is_reported_as_captcha_required():
    """实测：`请完成人机验证后重试` 是 **-100** 而不是 -106。

    所以只按状态码分类会把「验证没过」当成「参数错了」。这里钉住文案兜底。
    """
    fake = FakeAmd([fail(-100, "请完成人机验证后重试")])
    with pytest.raises(CaptchaRequired):
        fake.request_code("a@b.com", kind=1, captcha_ticket="")


def test_request_code_payload_shape():
    fake = FakeAmd([ok()])
    fake.request_code("a@b.com", kind=1, captcha_ticket="ticket-1")
    _, path, payload = fake.calls[0]
    assert path == "/User/SendVerificationCode"
    assert payload == {"TelOrEmail": "a@b.com", "Type": 1, "CaptchaTicket": "ticket-1"}


def test_default_solver_yields_empty_ticket():
    """默认 solver 必须返回空串而不是编造一个假串。

    编假串会把「验证没过」伪装成「验证码发不出去」，方向就查错了。
    """
    assert NoCaptchaSolver().solve("register_send_code", "a@b.com") == ""


def test_static_solver_echoes_param():
    assert StaticCaptchaSolver("P").solve("p", "t") == "P"


def test_verify_captcha_returns_ticket_on_success():
    fake = FakeAmd([ok({"VerifyResult": True, "CaptchaTicket": "TK"})])
    assert fake.verify_captcha("param", "register_send_code") == "TK"


def test_verify_captcha_returns_empty_when_result_false():
    """`VerifyResult=False` 时返回空串，让调用方重试 —— 不能带着废票硬闯。"""
    fake = FakeAmd([ok({"VerifyResult": False, "CaptchaTicket": "TK"})])
    assert fake.verify_captcha("param", "register_send_code") == ""


def test_captcha_scene_is_fetched_not_hardcoded():
    """SceneId 会轮换，必须现取。这条测试锁住「不要在代码里写死 r7n07m0j」。"""
    fake = FakeAmd([ok({"SceneId": "r7n07m0j", "EncryptedSceneId": "x"})])
    scene = fake.captcha_scene()
    assert scene["SceneId"] == "r7n07m0j"
    assert fake.calls[0][1] == "/Aliyun/GetEncryptedSceneId"


# ---------- 注册 ----------

def test_register_payload_has_verification_method_email():
    fake = FakeAmd([ok(None, "注册信息已提交")])
    r = fake.register(email="a@b.com", code="123456", phone_number="13800138000")
    _, path, payload = fake.calls[0]
    assert path == "/User/Register"
    assert payload["VerificationMethod"] == "email"
    assert payload["VerificationCode"] == "123456"
    assert payload["PhoneNumber"] == "13800138000"
    assert payload["AcceptedPrivacyStatement"] is True
    assert r.email == "a@b.com"


def test_register_sends_every_field_the_frontend_sends():
    """少字段不会报「缺字段」，而是走到某个默认分支，极难查。

    这条断言把前端 payload 的字段集合钉死 —— 以后谁删了一个字段，
    测试立刻变红，而不是等到线上某个分支行为变了才发现。
    """
    from xstech.amd import REGISTER_FIELDS

    fake = FakeAmd([ok()])
    fake.register(email="a@b.com", code="123456", phone_number="13800138000")
    _, _, payload = fake.calls[0]
    assert set(REGISTER_FIELDS) <= set(payload)


def test_register_rejects_missing_phone():
    """服务端强制手机号：`VerificationMethod=email` 也不能省。"""
    fake = FakeAmd([fail(-100, "请输入您的手机号")])
    with pytest.raises(AmdError) as ei:
        fake.register(email="a@b.com", code="123456", phone_number="")
    assert "手机号" in str(ei.value)


def test_split_name_strips_illegal_chars():
    """前后端都要求名字只含汉字/字母/数字，非法字符会走到校验分支。"""
    assert _split_name("amd-probe_1") == ("amdprobe1", "amdprobe1")
    assert _split_name("") == ("Amd", "Amd")


def test_register_derives_name_from_email_prefix():
    fake = FakeAmd([ok()])
    r = fake.register(email="probe-01@clowmail.com", code="123456",
                      phone_number="13800138000")
    assert r.first_name == "probe01"
    assert r.last_name == "probe01"


# ---------- HTTP 层 ----------

def test_http_403_is_http_error_not_business_error():
    """实测被 Azure App Gateway 用 403 挡过（WAF）。

    「被 WAF 拒了」和「参数错了」是两回事，必须区分开，否则会去改参数
    而真正的问题是请求频率。

    注意：这条测试**不能**用「重写 _post 直接抛异常」的假替身 ——
    那样断言的是替身的行为，而不是被测代码的状态码分支（实测：
    把 403 降级成业务响应后，那种写法依然是绿的）。必须让真实的
    `_post` 跑一遍，只把 requests 那一层换掉。
    """

    class Resp:
        status_code = 403
        text = "<html><center><h1>403 Forbidden</h1></center></html>"

        def json(self):  # pragma: no cover - 403 时不该走到这里
            raise AssertionError("403 时不应该解析 JSON")

    class Session:
        def post(self, url, json=None, timeout=None):  # noqa: A002 - 对齐 requests 签名
            return Resp()

    client = AmdClient()
    client.session = Session()
    with pytest.raises(AmdHttpError) as ei:
        client.request_code("a@b.com")
    assert ei.value.status == 403


# ---------- 完整编排 ----------

class FakeMailboxForAmd:
    def __init__(self, code="778899"):
        self.address = "probe@clowmail.com"
        self._code = code
        self.waited = None

    def create(self, username="", domain=""):
        return self.address

    def wait_for_code(self, pattern, timeout=180, sender_hint="", interval=5):
        self.waited = pattern
        # 调用方传进来的正则必须能从一封典型注册邮件里抓到码
        sample = "您的验证码是 778899，5 分钟内有效。"
        m = pattern.search(sample)
        return m.group(1) if m else None


def test_register_with_mailbox_happy_path():
    """完整编排：取场景 → 过验证 → 发码 → 收信 → 注册。"""
    # solver 直接给出 param，所以不会去取 SceneId —— 响应按真实调用顺序排。
    fake = FakeAmd([
        ok({"VerifyResult": True, "CaptchaTicket": "TK"}),  # verify_captcha
        ok(),                                               # request_code
        ok(None, "注册信息已提交"),                           # register
    ], solver=StaticCaptchaSolver("PARAM"))
    mb = FakeMailboxForAmd()
    r = fake.register_with_mailbox(mb, phone_number="13800138000", code_timeout=5)
    assert r.email == "probe@clowmail.com"
    paths = [c[1] for c in fake.calls]
    assert paths == [
        "/Aliyun/VerifyIntelligentCaptcha",
        "/User/SendVerificationCode",
        "/User/Register",
    ]


def test_register_with_mailbox_stops_when_captcha_fails():
    fake = FakeAmd([ok({"VerifyResult": False, "CaptchaTicket": ""})],
                   solver=StaticCaptchaSolver("PARAM"))
    with pytest.raises(CaptchaRequired):
        fake.register_with_mailbox(FakeMailboxForAmd(), phone_number="13800138000")
    # 没拿到 ticket 就不该去发码 —— 白跑一趟还会触发频率限制
    assert [c[1] for c in fake.calls] == ["/Aliyun/VerifyIntelligentCaptcha"]


def test_register_with_mailbox_raises_when_no_code():
    """收不到码要明确报错，不能返回一个半成品账号。"""
    fake = FakeAmd([ok({"VerifyResult": True, "CaptchaTicket": "TK"}), ok()],
                   solver=StaticCaptchaSolver("PARAM"))
    mb = FakeMailboxForAmd()
    mb.wait_for_code = lambda *a, **k: None
    with pytest.raises(AmdError) as ei:
        fake.register_with_mailbox(mb, phone_number="13800138000", code_timeout=1)
    assert "验证码" in str(ei.value)


def test_code_regex_needs_non_digit_boundaries():
    """`\\b\\d{6}\\b` 这种写法会把长数字串从中间截一段出来。

    注册邮件里常见形如「订单号 2026010112345678」的内容，用错正则就会
    抓出 6 位假码。这里直接验边界。
    """
    from xstech.amd import _AMD_CODE_RE

    assert _AMD_CODE_RE.search("验证码 123456").group(1) == "123456"
    assert _AMD_CODE_RE.search("手机 13800138000") is None
    # 精确 6 位才算
    assert _AMD_CODE_RE.search("code: 1234567") is None
    assert _AMD_CODE_RE.search("前缀 999123456后") is None


# ---------- 浏览器 solver ----------

class FakePage:
    """假页面：记录执行过的 JS，并允许脚本化「SDK 产出了 Param」。"""

    def __init__(self, results=None):
        self.scripts = []
        self._results = list(results or [])

    def run_js(self, expr):
        self.scripts.append(expr)
        if self._results:
            return self._results.pop(0)
        return ""


def test_browser_solver_hooks_sdk_success_callback():
    """必须通过 SDK 的公开 success 回调拿 Param，而不是读混淆的内部字段。

    内部字段名每个版本都会变（SDK 是逐版本重新混淆的），依赖它等于
    给自己埋一个「上游一升级就静默失效」的坑。回调是公开契约。
    """
    from xstech.amd import BrowserCaptchaSolver

    page = FakePage(["hooked", "PARAM-FROM-BROWSER"])
    solver = BrowserCaptchaSolver(page)
    assert solver.solve("register_send_code", "a@b.com") == "PARAM-FROM-BROWSER"
    hook = page.scripts[0]
    assert "initAliyunCaptcha" in hook
    assert "success" in hook
    # 不能去读内部字段
    assert "__ALIYUN_CAPTCHA_UTILS" not in hook


def test_browser_solver_returns_empty_when_sdk_absent():
    """SDK 没加载时返回空串，让上层按「没拿到 ticket」处理，而不是崩。"""
    from xstech.amd import BrowserCaptchaSolver

    page = FakePage(["no-sdk", ""])
    assert BrowserCaptchaSolver(page).solve("p", "t") == ""


def test_browser_solver_is_registered_as_captcha_solver():
    """它必须满足 CaptchaSolver 协议的形状（有 solve(purpose, target)）。"""
    from xstech.amd import BrowserCaptchaSolver

    solver = BrowserCaptchaSolver(FakePage())
    assert callable(getattr(solver, "solve", None))
    assert solver.solve("register_send_code", "x") == ""


# ---------- 出口 IP 伪装（协议层） ----------

def test_amd_client_no_proxy_by_default():
    c = AmdClient()
    assert c.proxy == ""


def test_amd_client_sets_session_proxies():
    """代理必须落到 requests.Session.proxies 上 —— 协议层不需要它，
    但「机房 IP 被重点拦」这件事只有代理能绕，所以两个都支持。"""
    c = AmdClient(proxy="http://1.2.3.4:8080")
    assert c.proxy == "http://1.2.3.4:8080"
    assert c.session.proxies.get("https") == "http://1.2.3.4:8080"
    assert c.session.proxies.get("http") == "http://1.2.3.4:8080"


def test_proxy_survives_full_registration_flow():
    """带代理时业务逻辑不受影响（用假 HTTP 层验证）。"""
    c = FakeAmd([fail(-100, "请完成人机验证后重试")], solver=NoCaptchaSolver())
    c.proxy = "http://1.2.3.4:8080"
    with pytest.raises(CaptchaRequired):
        c.request_code("a@b.com", kind=1)
