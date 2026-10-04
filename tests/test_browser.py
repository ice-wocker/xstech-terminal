"""离线单测：浏览器驱动的「可离线验证」部分。

真起浏览器要联网且慢，不适合放进单测。但这里有几处**必须离线钉死**：

1. 拿不到 Param 时，异常类型要能从 `CaptchaNotPassed` 转成
   `amd.CaptchaRequired` —— 否则调用方按「协议错」处理，
   会把「人机验证没过」误报成「接口挂了」；
2. `BrowserCaptchaSolver` 必须满足 `amd.CaptchaSolver` 协议；
3. 伪装 JS 不能因为 SDK 已挂载就重复挂钩子（重复挂钩会让 param 丢失）。
"""

import pytest

from xstech import browser
from xstech.amd import CaptchaRequired, NoCaptchaSolver


def test_error_maps_to_captcha_required():
    err = browser._as_captcha_required("被拦了", ["F015", "F011"])
    assert isinstance(err, CaptchaRequired)
    assert "F015" in str(err)
    assert err.payload.get("codes") == ["F015", "F011"]


def test_solver_satisfies_protocol():
    # amd.py 的 CaptchaSolver 是 Protocol：只要有 solve(purpose, target) 即可
    s = browser.BrowserCaptchaSolver(email="a@b.com")
    assert hasattr(s, "solve")
    assert callable(s.solve)


def test_solver_defaults_random_phone():
    s = browser.BrowserCaptchaSolver(email="a@b.com")
    assert len(s.phone) == 11 and s.phone.startswith("1")


def test_solver_keeps_explicit_phone():
    s = browser.BrowserCaptchaSolver(email="a@b.com", phone="13800138000")
    assert s.phone == "13800138000"


def test_hook_js_is_idempotent_guarded():
    # 钩子必须在 SDK 已挂钩时提前返回，不能重复包
    assert "__amd_captcha_hooked" in browser._HOOK_JS
    assert "initAliyunCaptcha" in browser._HOOK_JS
    assert "success" in browser._HOOK_JS


def test_launch_kwargs_uses_executable_when_given():
    kw = browser._launch_kwargs(headless=True, executable_path="/usr/bin/chromium")
    assert kw["headless"] is True
    assert kw["executable_path"] == "/usr/bin/chromium"
    assert "--no-sandbox" in kw["args"]


def test_launch_kwargs_omits_executable_when_empty():
    kw = browser._launch_kwargs(headless=False)
    assert "executable_path" not in kw
    assert kw["headless"] is False


def test_solve_translates_failure_to_captcha_required(monkeypatch):
    """拿不到 Param 时，`solve()` 必须抛 CaptchaRequired，而不是原样漏出
    CaptchaNotPassed。漏出去的话调用方按「协议错」处理，就会把
    「人机验证没过」误报成「接口挂了」。"""
    s = browser.BrowserCaptchaSolver(email="a@b.com")

    async def fake_solve(self, address):
        raise browser.CaptchaNotPassed("服务端拦截", codes=["F015"])

    monkeypatch.setattr(browser.BrowserCaptchaSolver, "_solve_async", fake_solve)
    with pytest.raises(CaptchaRequired) as ei:
        s.solve("register_send_code", "a@b.com")
    assert "F015" in str(ei.value)


def test_captcha_not_passed_carries_codes():
    err = browser.CaptchaNotPassed("x", codes=["F001"])
    assert err.codes == ["F001"]


def test_require_playwright_raises_helpful_when_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(browser.BrowserUnavailable) as ei:
        browser._require_playwright()
    assert "playwright install" in str(ei.value)



# ---------- 出口 IP 伪装（--proxy） ----------

def test_proxy_stored_on_solver():
    s = browser.BrowserCaptchaSolver(email="a@b.com", proxy="socks5://1.2.3.4:1080")
    assert s.proxy == "socks5://1.2.3.4:1080"


def test_no_proxy_by_default():
    # 默认直连 —— 不给代理是刻意的：代理不是过验证的充分条件，
    # 默认打开只会让失败原因更难查。
    s = browser.BrowserCaptchaSolver(email="a@b.com")
    assert s.proxy == ""


# ---------- 反自动化指纹伪装 ----------

def test_stealth_on_by_default():
    assert browser.BrowserCaptchaSolver(email="a@b.com").stealth is True


def test_stealth_js_hides_the_two_fatal_tells():
    """这两个特征是实测里最致命的：UA 带 HeadlessChrome、WebGL 是 SwiftShader。

    伪装脚本必须把 webdriver 和 WebGL renderer 都盖掉，否则等于自报家门。
    """
    js = browser._STEALTH_JS
    assert "webdriver" in js
    assert "37446" in js           # UNMASKED_RENDERER_WEBGL
    assert "SwiftShader" not in js  # 不能把真值写进去，那等于没改
    assert "NVIDIA" in js


def test_desktop_ua_is_not_headless():
    """默认 UA 里绝不能出现 HeadlessChrome。"""
    assert "HeadlessChrome" not in browser._DESKTOP_UA
    assert "Chrome/" in browser._DESKTOP_UA
    assert "Windows" in browser._DESKTOP_UA


def test_custom_user_agent_wins():
    s = browser.BrowserCaptchaSolver(email="a@b.com", user_agent="MyUA/1.0")
    assert s.user_agent == "MyUA/1.0"


# ---------- 返回码解释 ----------

def test_explain_f015_points_at_detection():
    """F015 = 轨迹无效 → 应该让人去查缺口定位，而不是去换 IP。"""
    msg = browser._explain_codes(["F015"])
    assert "轨迹" in msg and "缺口" in msg


def test_explain_f001_points_at_environment():
    """F001 = 轨迹收下了但没过 → 位置不是瓶颈，应该指向设备/环境。"""
    msg = browser._explain_codes(["F001"])
    assert "环境" in msg or "画像" in msg


def test_explain_empty_codes_is_network_not_captcha():
    msg = browser._explain_codes([])
    assert "网络" in msg or "代理" in msg


@pytest.mark.parametrize("code", ["F001", "F011", "F015"])
def test_explain_never_claims_distance_is_irrelevant(code):
    """回归测试：上一版文档错误地说「返回码与距离无关」。

    这个错误直接把排错方向带偏了（让人以为位置不重要，于是不去查定位）。
    这里钉住：解释文案里不能再出现那句断言。
    """
    msg = browser._explain_codes([code])
    assert "与拖动距离无关" not in msg
    assert "距离无关" not in msg


def test_docs_do_not_relicense_the_wrong_conclusion():
    """回归：绝不把「返回码与拖动距离无关」当成结论写回去。

    这条错结论曾让排错方向整个歪掉（以为不用查缺口定位）。它现在只能
    出现在「纠错说明」里 —— 一旦有代码注释把它当事实陈述，就该变红。
    """
    import re
    from pathlib import Path

    src = Path(browser.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        if "无关" not in line and "一成不变" not in line:
            continue
        # 允许出现，但必须落在「纠错」上下文里（明确说它是旧版/错误说法）
        correcting = ("错的", "错结论", "是错的", "上一版", "写的是", "那个结论是错")
        assert any(k in line for k in correcting), (
            f"发现疑似把错结论当事实的句子: {line!r}"
        )
