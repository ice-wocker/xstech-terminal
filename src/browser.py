"""用真浏览器驱动阿里云「拼图滑块」人机验证。

## 为什么是「驱动」而不是「破解」

xstech.one 的那种滑块可以把答案**算出来**：它把形状裁剪块单独返回给客户端，
于是问题变成模板匹配，纯 HTTP 就能解（见 `captcha.py`）。

阿里云这套不一样。这里实测过（`developer.amd.com.cn/register`，SceneId `r7n07m0j`）：

- 拼图块的**正确位置不返回给客户端** —— `mdwxhh.captcha-open.aliyuncs.com`
  只给一张带缺口的底图（`back.png`）和一张碎片图（`shadow.png`），
  判定权在服务端；
- 客户端只上报**拖动轨迹**（`CaptchaVerifyParam`），由服务端拿去阿里云验真；
- 而且服务端还叠了一层**设备/行为画像**（`cloudauth-device-dualstack` 取设备指纹，
  带 `deviceToken`）。**位置对不对决定 F015/F001，画像决定 F001/Success —— 前者本地能算，后者不能。**

所以这个模块干的事是：**把真浏览器跑起来、让 SDK 自己产出 Param**。
这是驱动，不是破解 —— 两者别混为一谈。

## 实测结论（重要，别重踩）


在无头 Linux 容器里，即使把滑块拖到肉眼对齐缺口，验证接口仍然返回失败。
但**失败码是有含义的，不是一团乱码** —— 这一点比上一版写得更准确了：

| 拖动情况 | 服务端返回 |
|---|---|
| 偏离算出的位置（±12px 以上）/ 拖太短 | `F015` |
| 位置算对了（缺口定位正确） | `F001` |
| 连续猛打同一个出口 IP | 一律 `F015`（IP 被限流） |

所以：**`F015` ≈ 「轨迹无效」**（位置差太远、轨迹像脚本，或者 IP 已被限流），
**`F001` ≈ 「轨迹收下了，但整体判定没过」**。

**距离是敏感的，这一点别再搞错。** 用「真鼠标事件」扫距离，两轮独立复现：

    dist=120/180/240/260  → 只有 240 拿到 F001，其余 F015
    dist=180/200/220/240  → 只有 220 拿到 F001，其余 F015

**只有接近正确位置的那个距离拿到 `F001`，其余全 `F015`。** 所以轨迹确实是
逐点校验的，「位置随便拖拖都一样」是错的。

## 三个「看着像失败、其实是代码写错」的坑

这三条都是本轮实测挖出来的，而且**每一个都会让服务端回 `F015`**，
表现得就像「拖错了位置」，实际上根本不是验证的问题：

| # | 坑 | 症状 | 正确做法 |
|---|---|---|---|
| 1 | 鼠标**一步跳到** handle 上再按下 | 滑块不进可拖拽态，拼图块纹丝不动，仍回 `F015` | 分步 `move(..., steps=10)` 经过 hover 再 `down()` |
| 2 | 起点用 **handle** 的 `getBoundingClientRect()` | 偏十几像素（handle 的 `left` 在 `initial` 态是 `0px`，视觉上却在 40px 盒子里居中） | 起点用 `sliding-body`（整条滑道）算 |
| 3 | 拖动距离**超过滑道容量** | 超出的部分被**静默夹住**（300px 滑道 − 40px handle = 最多 260px），于是「多拖一点」变成「拖到最右」 | 按 `body.width - 40` 夹取 |

坑 3 最阴：它让「算出的距离」和「实际能拖到的距离」不一致，而且**没有任何
报错**，只有返回码一直 `F015`。写这段时一度以为「距离算错了」，实际是
「算对了但拖不到位」。

> 另外还遇到一个 `T001`（`spoof.py` 的码表里有解释）：出现在真鼠标事件 +
> 距离算准 + 同一会话连续重试之后，不是位置问题，更像是对重复提交的标记。

> ⚠️ 关于「返回码与拖动距离无关」这个**是错的**结论，已经纠正过两轮，把完整因果写清楚：
>
> 1. 第一版写的是「把 60 扫到 260 返回码一成不变」—— 那是错的。错在方法：验证失败后
>    服务端会**发一张新题**，「在同一张题上扫距离」根本做不到，扫的是不同题。
> 2. 后来改成用 **JS 直接改 handle 的 `style.left`** 拖动，看到「距离变了
>    返回码不变」—— **还是错的**。因为这种拖法对服务端就是无效轨迹，
>    所有距离都落进 `F015`，「全一样」是必然的 —— 这不等于距离不影响判定。
> 3. 正确的测法是用**真鼠标事件**（CDP `Input.dispatchMouseEvent`，
>    即 `page.mouse.move`），并且先把鼠标**分步**移到 handle 上激活它。
>    这样测得 `dist=240 → F001`、`120/180/260 → F015`，距离是敏感的。
>
> 教训：**「所有输入都得到同一个失败码」时，先怀疑输入根本没生效**，
> 而不是直接下「参数不影响判定」的结论。

## 那到底卡在哪：设备/环境画像

位置算对之后还有一层，而且是**这一层**拦住的：

- 裸跑 Playwright 时 `navigator.userAgent` 带 `HeadlessChrome`，
  `WebGL` renderer 是 `SwiftShader`（软件渲染、没有真实 GPU）—— 
  这两条等于自报家门。`_STEALTH_JS` 把它们伪装掉（已实现）。
- 但**伪装指纹仍然过不去**（实测：Windows UA + 假 NVIDIA GPU + 干净
  国外出口 IP，照旧 `F001`）。说明画像里还有本地伪造不了/不该伪造的维度。

实测过的组合（都未通过）：

| 出口 IP | 浏览器指纹 | 结果 |
|---|---|---|
| 本机（腾讯云广州 111.230.93.5） | 裸 | `F001` / `F015` |
| 代理 → AWS 法兰克福 3.68.36.133 | 裸 | `F001` |
| 代理 → AWS 法兰克福 | 伪装（假 GPU + Win UA） | `F001` |
| 本机 | 伪装 | `F001` |
| headed（xvfb） | 伪装 | 与 headless 无差别 |

**结论：在当前这套环境里「全自动过阿里云验证」做不到。** 这不是实现问题，
是服务端策略；能改的变量（IP、指纹、轨迹）我都实测过了，都没撬动。

能做的：
1. `BrowserCaptchaSolver` 把链路完整跑通（含代理、指纹伪装、拟人轨迹）；
2. 拿不到 Param 时**诚实地说停在哪、卡在哪个码**，不假装成功（退出码 3）。

## 关于 IP（针对「可以伪装 IP」的请求）

`--proxy` 已经接好（协议层和浏览器层都能走）。但**必须说清楚：光换 IP 不够。**
理由见上表 —— 我换了干净的国外机房 IP，结果一样是 `F001`。

真正可能起作用的 IP 是**住宅 IP**（residential），因为机房 IP 段（云厂商
ASN）是这类风控的重点拦截对象。我手上没有住宅代理凭据，没法替你验证；
如果你想试，把代理地址填进 `--proxy` 再跑一次 probe 就知道：

    xstech-gateway amd-probe --browser --proxy socks5://user:pass@host:port

> 注：别拿「公开免费的 HTTP 代理列表」当住宅 IP 用 —— 那些大多也是机房
> 出口，而且极不稳定（我实测 60 个里只有十来个能连上 AMD，而且换一个就
> 挂一个）。真要用住宅 IP，得买商业住宅代理。

## 「伪装成阿里云的服务器」能绕过去吗 —— 不能（已实测）

顺着「能不能伪装阿里云的服务器」这个方向，把三条具体做法都跑了一遍
（`amd-spoof` 命令，见 `spoof.py`）：

| 做法 | 结果 |
|---|---|
| 伪造 `CaptchaVerifyParam` 直接喂验证接口 | `REJECT_PARAM`（参数层就拒）|
| `X-Forwarded-For` 伪装成阿里云 IP / 内网 | 与不伪装完全一致 |
| 真浏览器拦截 SDK 请求、本地返回伪造成功 | SDK 不认，产不出 Param |

**根因**：验真发生在 **AMD 服务端 → 阿里云** 那条出网链路，`deviceToken`
由阿里云密钥加密、请求带 HMAC-SHA1 签名。要在那条链路上做 MITM 才能
「伪装它的服务器」，而那是别人的网络，客户端够不着。


## 关于手机号

AMD 注册表单**服务端强制手机号**（选邮箱验证也免不掉，实测返回
`请输入您的手机号`）。验证走邮箱时，这个号填谁的都行（不校验归属），
所以用 `phone.py` 的随机号即可。
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass

from .amd import AMD_ORIGIN
from .phone import random_cn_mobile

REGISTER_URL = f"{AMD_ORIGIN}/register"

# 阿里云 SDK 把 `CaptchaVerifyParam` 经由 `initAliyunCaptcha` 的 success 回调
# 交出来。回调内部字段是混淆过的（版本间会变），所以**不依赖内部字段**，
# 只把公开回调包一层，把 Param 落到一个自己的变量上。
_HOOK_JS = """
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

# 把拼图块往右拖。距离用「缺口位置」，是否命中由服务端判定。
_DRAG_JS = """
(() => {
  const el = document.getElementById('aliyunCaptcha-puzzle');
  return el ? (parseFloat(el.style.left) || 0) : null;
})()
"""


# ---------------------------------------------------------------- 反自动化识别
#
# 为什么需要这段：阿里云的设备指纹 SDK（`cloudauth-device-dualstack`）会读一串
# 浏览器特征做联合画像。裸跑 Playwright 时，**有两个特征是致命的**：
#
#   1. `navigator.userAgent` 里带 `HeadlessChrome`
#   2. `WebGL` 的 renderer 是 `SwiftShader`（软件渲染，没有真实 GPU）
#
# 这两条一叠加，等于直接告诉服务端「我是无头容器」。实测把它们伪装掉之后
# 仍然过不了（说明画像里还有别的维度），但**不伪装是必挂**，
# 所以这层是必要条件、不是充分条件。别把「伪装成功」当成「验证能过」。
#
# 用 `add_init_script` 在页面脚本之前注入，避免被 SDK 先读到真值。
_STEALTH_JS = """
(() => {
  const define = (obj, prop, getter) => {
    try { Object.defineProperty(obj, prop, {get: getter, configurable: true}); } catch (e) {}
  };
  define(navigator, 'webdriver', () => undefined);
  define(navigator, 'platform', () => 'Win32');
  define(navigator, 'hardwareConcurrency', () => 8);
  define(navigator, 'deviceMemory', () => 8);
  define(navigator, 'maxTouchPoints', () => 0);
  define(navigator, 'languages', () => ['zh-CN', 'zh', 'en']);

  const FAKE_GPU = 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
  const FAKE_VENDOR = 'Google Inc. (NVIDIA)';
  const patch = (proto) => {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37446) return FAKE_GPU;      // UNMASKED_RENDERER_WEBGL
      if (p === 37445) return FAKE_VENDOR;   // UNMASKED_VENDOR_WEBGL
      return orig.call(this, p);
    };
  };
  patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  if (!window.chrome) window.chrome = {runtime: {}};
})()
"""

# 与 stealth 配套的桌面 UA —— 不能再用默认的 `HeadlessChrome/...`。
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 默认桌面视口。窗口尺寸也是画像的一维：1280x900 这种「整数字」在真实
# 显示器上不常见，1366x768 / 1920x1080 更自然。
_DESKTOP_VIEWPORT = {"width": 1366, "height": 768}


class BrowserUnavailable(RuntimeError):
    """没装 playwright / 没装浏览器内核。"""


class CaptchaNotPassed(RuntimeError):
    """人机验证没通过（服务端返回失败码），拿不到 Param。"""

    def __init__(self, message: str, codes: list[str] | None = None):
        super().__init__(message)
        self.codes = codes or []


def _as_captcha_required(message: str, codes: list[str]):
    """把浏览器层的失败转成 `amd.CaptchaRequired`。

    这样调用方（`AmdClient.register_with_mailbox`）就能用同一套逻辑处理
    「人机验证没过」，不必知道验证是浏览器驱动的还是别的实现。
    """
    from .amd import CaptchaRequired

    return CaptchaRequired(
        message + "（返回码：%s）" % (", ".join(codes) if codes else "无"),
        status=-1,
        payload={"codes": codes},
    )


@dataclass
class BrowseResult:
    """一次浏览器驱动的产物。"""

    address: str
    param: str = ""
    codes: list[str] = None  # 服务端返回的失败码，便于排查
    screenshots: dict = None

    def __post_init__(self):
        self.codes = self.codes or []
        self.screenshots = self.screenshots or {}


def _require_playwright():
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise BrowserUnavailable(
            "驱动真浏览器需要 playwright。安装：\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
            "（Linux 容器里还需系统依赖：playwright install-deps chromium）"
        ) from exc
    from playwright.async_api import async_playwright

    return async_playwright


class BrowserCaptchaSolver:
    """驱动真浏览器过一遍阿里云拼图滑块。

    与 `amd.py` 里的 `CaptchaSolver` 协议兼容：`solve(purpose, target)` 返回
    `CaptchaVerifyParam` 字符串（拿不到返回空串）。

    用法（同步封装）：

        >>> s = BrowserCaptchaSolver(email="a@b.com", phone="138...")
        >>> param = s.solve("register_send_code", "a@b.com")   # doctest: +SKIP
    """

    def __init__(
        self,
        email: str,
        phone: str = "",
        *,
        channel: str = "chromium",       # playwright 频道，可换 msedge / chrome
        headless: bool = True,
        timeout: int = 90,
        attempts: int = 3,
        executable_path: str = "",
        user_data_dir: str = "",
        proxy: str = "",
        stealth: bool = True,
        user_agent: str = "",
    ):
        self.email = email
        self.phone = phone or random_cn_mobile()
        self.channel = channel
        self.headless = headless
        self.timeout = timeout
        self.attempts = attempts
        self.executable_path = executable_path
        self.user_data_dir = user_data_dir
        # 出口 IP 伪装：`http://host:port` 或 `socks5://host:port`。
        # 空串 = 直连。见模块说明「关于 IP」一节。
        self.proxy = proxy
        # 是否注入反自动化识别脚本（默认开）。只有在你想复现「裸 Playwright
        # 会被怎么拦」时才关掉它。
        self.stealth = stealth
        self.user_agent = user_agent

    # ---------- 同步外壳 ----------

    def solve(self, purpose: str = "register_send_code", target: str = "") -> str:
        """同步入口（`amd.py` 的 `CaptchaSolver` 协议）。

        拿不到 Param 时抛 `amd.CaptchaRequired`（而不是自己的异常类型），
        让调用方按「人机验证没过」统一处理。
        """
        try:
            return asyncio.run(self._solve_async(target or self.email))
        except CaptchaNotPassed as exc:
            raise _as_captcha_required(str(exc), exc.codes) from exc

    # ---------- 异步实现 ----------

    async def _solve_async(self, address: str) -> str:
        async_playwright = _require_playwright()
        async with async_playwright() as pw:
            # 注意：这两个启动参数只减少**本地**的自动化特征，
            # **挡不住阿里云的服务端画像**（实测结论见模块说明）。
            launch_kwargs = _launch_kwargs(self.headless, self.executable_path)
            if not self.executable_path and self.channel:
                launch_kwargs["channel"] = self.channel
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                return await self._run(browser, address)
            finally:
                await browser.close()

    async def _new_context(self, browser):
        """统一的浏览器上下文。

        把「反自动化识别」集中在一处：UA、视口、时区、语言、注入脚本。
        之前这些散落在 `_run` 和 `probe_environment` 里各写一份，改一处漏一处。
        """
        ctx = await browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport=_DESKTOP_VIEWPORT,
            user_agent=self.user_agent or _DESKTOP_UA,
        )
        if self.stealth:
            await ctx.add_init_script(_STEALTH_JS)
        return ctx

    async def _run(self, browser, address: str) -> str:
        ctx = await self._new_context(browser)
        page = await ctx.new_page()
        codes: list[str] = []

        async def on_response(resp):
            if "verify" in resp.url:
                try:
                    body = await resp.json()
                except Exception:
                    return
                result = body.get("Result") or {}
                code = result.get("VerifyCode")
                if code:
                    codes.append(code)
                if result.get("VerifyResult"):
                    codes.append("OK")

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        await page.goto(REGISTER_URL, wait_until="networkidle", timeout=self.timeout * 1000)
        await page.wait_for_timeout(3000)

        # 1) 装钩子（必须在 SDK 初始化之前跑，所以走 add_init_script 更稳，
        #    但注册页是 SPA，SDK 可能在之后才挂上，所以再补一次）。
        await page.evaluate(_HOOK_JS)

        # 2) 填表：手机号用随机号（服务端强制必填，但不校验归属）
        await self._fill_form(page, address)

        # 3) 点「发送验证码」触发人机验证
        await self._click_send_code(page)
        await page.wait_for_timeout(3000)

        # 4) 逐次拖滑块，读取 SDK 回调
        param = ""
        for _ in range(self.attempts):
            await self._drag_onto_gap(page)
            await page.wait_for_timeout(3000)
            param = await page.evaluate("window.__amd_captcha_param || ''")
            if param or "OK" in codes:
                break

        await ctx.close()
        if not param:
            raise CaptchaNotPassed(_explain_codes(codes), codes=codes)
        return str(param)

    async def _fill_form(self, page, address: str) -> None:
        inputs = await page.query_selector_all("input")
        # 顺序实测：姓、名、邮箱、手机号、验证码
        if len(inputs) >= 4:
            await inputs[0].fill("Zhang")
            await inputs[1].fill("San")
            await inputs[2].fill(address)
            await inputs[3].fill(self.phone)

    async def _click_send_code(self, page) -> None:
        for btn in await page.query_selector_all("button"):
            text = (await btn.inner_text()) or ""
            if "发送验证码" in text:
                await btn.click()
                return

    async def _drag_onto_gap(self, page) -> float:
        """把拼图块拖到缺口处，返回实际拖动距离。

        缺口位置从 SDK 返回的底图里算：那块「半透明白」就是要塞进去的地方
        （亮度高 + 饱和度低的区域，见 `slider.py`）。

        ⚠️ 两个踩过的坑写在这儿，免得重踩：

        1. **起点必须用 `sliding-body`（整条滑道）算，不能用 handle。**
           handle 在 `initial` 状态下 `style.left` 是 `0px`，但视觉上并不在
           滑道最左端（它在自己的 40×40 盒子里居中）。拿 handle 的
           `getBoundingClientRect()` 当起点，实际会偏十几像素。
        2. **鼠标必须「分步」移到起点再按下。** 一步跳过去
           （`move(cx, cy)`）不会触发 hover，滑块不进可拖拽态，后面拖动
           拼图块纹丝不动 —— 而服务端照样回 `F015`，看着像「拖了但没拖对」。
           `steps=10` 让它经过 hover 再 `mouse.down()` 才对。
        """
        from .slider import detect_gap

        srcs = await page.evaluate(
            """() => {
              const bg = document.getElementById('aliyunCaptcha-img');
              const pz = document.getElementById('aliyunCaptcha-puzzle');
              const body = document.getElementById('aliyunCaptcha-sliding-body');
              return { bg: bg ? bg.src : '', pz: pz ? pz.src : '',
                       body: body ? body.getBoundingClientRect().toJSON() : null,
                       bgW: bg ? bg.naturalWidth : 0, bgDW: bg ? bg.offsetWidth : 0 };
            }"""
        )
        if not srcs.get("bg") or not srcs.get("pz") or not srcs.get("body"):
            return 0.0
        _gap_display, distance = detect_gap(
            srcs["bg"], srcs["pz"], scale=srcs["bgDW"] / max(1, srcs["bgW"])
        )
        body = srcs["body"]
        cx = body["x"] + 20                     # 起点：手柄中心（半径 20）
        cy = body["y"] + body["height"] / 2
        # 拖动距离 = 鼠标位移量，**但滑道是有长度的**（实测 300px 画布、
        # handle 40px，所以 left 最大 260px）。超出去的部分会被静默夹住，
        # 于是「多拖一点」变成了「拖到最右」，反而永远对不上缺口。
        # 缺口本来也不会出现在最右端，这里只做一次防御性夹取。
        max_dist = max(0.0, body["width"] - 40)
        target = min(distance, max_dist)
        await _human_drag(page, cx, cy, target)
        return target


def _explain_codes(codes: list[str]) -> str:
    """把服务端返回码翻成一句能指导下一步的说明。

    这里刻意**不**再说「这些码跟拖动距离无关」—— 那是之前一版写错的结论。
    后来的实测是：偏离算出来的位置会稳定拿到 `F015`，而位置算对了会稳定
    拿到 `F001`。也就是说**服务端确实在看轨迹**，只是位置对了也还有一层
    过不去的判定（设备/环境画像）。

    区分这两个码，用户才知道该往哪查：
    - `F015`：轨迹被当作无效（位置差太远 / 像脚本）。先修缺口定位。
    - `F001`：轨迹被接受了，但整体验证没过。位置不是瓶颈，换环境再试。
    """
    got = ", ".join(codes) if codes else "无"
    if not codes:
        return "人机验证未通过：迟迟没等到服务端响应（网络或代理问题）。"
    if all(c == "F015" for c in codes):
        return (
            "人机验证未通过（返回码 " + got + "）：拖动轨迹被判为无效。"
            "通常是缺口定位偏差过大，或轨迹不像人手。"
            "用 `amd-probe --browser` 看算出来的拖动距离对不对。"
        )
    if "F001" in codes:
        return (
            "人机验证未通过（返回码 " + got + "）：轨迹已被接受，但整体判定没过。"
            "位置不是瓶颈，卡在服务端的设备/环境画像上。"
            "换个干净的住宅出口 IP、带真实 GPU 的桌面环境再试。"
        )
    return "人机验证未通过（返回码 " + got + "）。"


async def _human_drag(page, cx: float, cy: float, distance: float) -> None:
    """把滑块从 (cx, cy) 拖出 `distance` 像素。

    为什么不「匀速直拖」：轨迹本身就是被判定的输入之一（服务端会看加速度、
    抖动、总时长）。匀速 + 零抖动的轨迹是很强的机器人特征。这里用
    **缓出曲线 + 亚像素抖动 + 末端微回弹**，让它更像人手。

    注意：这**不是**过验证的充分条件 —— 实测伪装轨迹后仍然被服务端拦
    （见模块说明）。但它能消掉一类低成本的「一看就是脚本」判定。
    """
    # 起点：从旁边「分步」移过来再按。一步跳过去不触发 hover，
    # 滑块不进可拖拽态 —— 这是实测踩出来的坑，见 `_drag_onto_gap`。
    await page.mouse.move(cx - random.randint(50, 80), cy, steps=8)
    await page.mouse.move(cx, cy, steps=10)
    await page.wait_for_timeout(random.randint(120, 260))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(70, 150))

    steps = 45
    for i in range(1, steps + 1):
        t = i / steps
        eased = 1 - (1 - t) ** 3          # 缓出：起步快、收尾慢
        x = cx + distance * eased
        y = cy + random.uniform(-1.2, 1.2)  # 纵向抖动
        await page.mouse.move(x, y, steps=1)
        await page.wait_for_timeout(random.randint(9, 20))

    # 末端轻微过冲再回位 —— 真实拖动很少停在整数像素上
    await page.mouse.move(cx + distance + random.uniform(1.0, 3.0), cy, steps=2)
    await page.wait_for_timeout(random.randint(60, 140))
    await page.mouse.move(cx + distance, cy, steps=2)
    await page.wait_for_timeout(random.randint(200, 450))
    await page.mouse.up()


def _launch_kwargs(headless: bool, executable_path: str = "", channel: str = "chromium") -> dict:
    """统一的启动参数。

    优先用显式给的 `executable_path`（容器里常见：系统装了 chromium，
    但 playwright 自带的没下）。都没有时退回 `channel="chromium"` ——
    playwright 会去找注册过的内核；再找不到才报「请 playwright install」。
    """
    kwargs = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    if executable_path:
        kwargs["executable_path"] = executable_path
    return kwargs


async def capture_ordvd(
    *,
    executable_path: str = "",
    headless: bool = True,
    proxy: str = "",
    stealth: bool = True,
) -> dict:
    """把 SDK 自己采集的环境明文 `window.__ORDVD` 抓回来。

    这个东西值得单独抓一次，因为它是**本地能拿到的最接近 deviceToken 明文
    的东西** —— 而「本地能不能自己造 deviceToken」正是「攻破」这件事的
    核心问题（见 `bruteclose.py` 的第 3、4 族）。

    实测：`__ORDVD` 是 SDK 拼出来的一坨混淆键名的环境特征（UA、平台、GPU、
    分辨率、时区、canvas/audio 哈希、`WebGL` 参数串……），**是明文**，
    且**不带签名**。而服务端要的 `deviceToken` 是**密文** + HMAC 签名。
    两者的差距不是「再加点字段」，是「少一把服务端密钥」。

    `__ALIYUN_CRYPT` 也一并抓：用来证明「算法齐全、密钥缺席」。
    """
    async_playwright = _require_playwright()
    async with async_playwright() as pw:
        launch_kwargs = _launch_kwargs(headless, executable_path)
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            solver = BrowserCaptchaSolver(
                email="ordvd@example.com", phone=random_cn_mobile(),
                proxy=proxy, stealth=stealth,
            )
            ctx = await solver._new_context(browser)
            page = await ctx.new_page()
            await page.goto(REGISTER_URL, wait_until="networkidle", timeout=90000)
            await page.wait_for_timeout(6000)
            out = await page.evaluate(
                """() => {
                  const ord = window.__ORDVD || {};
                  const C = window.__ALIYUN_CRYPT || {};
                  return {
                    ordvd: ord,
                    ordvd_keys: Object.keys(ord).length,
                    crypt_keys: Object.keys(C),
                    crypt_has_key: ('key' in C) || ('iv' in C),
                    crypt_has_algo: ('AES' in C) && ('SHA256' in C),
                  };
                }"""
            )
            await ctx.close()
            return out
        finally:
            await browser.close()


async def probe_environment(
    email: str = "",
    phone: str = "",
    *,
    executable_path: str = "",
    headless: bool = True,
    proxy: str = "",
    stealth: bool = True,
) -> dict:
    """无副作用地把浏览器环境摸一遍：能不能起、SDK 挂没挂、验证接口给了什么码。

    给 `amd-probe` 用。它**不会**真的提交注册。

    `proxy` 用于检验「换个出口 IP 有没有用」——这是**唯一**能自己动手验证
    的变量（见模块说明「关于 IP」）。
    """
    async_playwright = _require_playwright()
    email = email or "probe@example.com"
    async with async_playwright() as pw:
        launch_kwargs = _launch_kwargs(headless, executable_path)
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            solver = BrowserCaptchaSolver(
                email=email, phone=phone or random_cn_mobile(), timeout=90,
                proxy=proxy, stealth=stealth,
            )
            ctx = await solver._new_context(browser)
            page = await ctx.new_page()
            codes: list[str] = []

            async def on_response(resp):
                if "verify" in resp.url:
                    try:
                        body = await resp.json()
                    except Exception:
                        return
                    code = (body.get("Result") or {}).get("VerifyCode")
                    if code:
                        codes.append(code)

            page.on("response", lambda r: asyncio.create_task(on_response(r)))
            await page.goto(REGISTER_URL, wait_until="networkidle", timeout=90000)
            await page.wait_for_timeout(4000)
            sdk = await page.evaluate(
                "() => typeof window.initAliyunCaptcha"
            )
            await page.evaluate(_HOOK_JS)
            await solver._fill_form(page, email)
            await solver._click_send_code(page)
            await page.wait_for_timeout(2000)
            distance = await solver._drag_onto_gap(page)
            await page.wait_for_timeout(3000)
            param = await page.evaluate("window.__amd_captcha_param || ''")
            try:
                exit_ip = await page.evaluate(
                    "async () => { const r = await fetch('https://ipinfo.io/json');"
                    " return (await r.json()).ip; }"
                )
            except Exception:
                exit_ip = ""
            await ctx.close()
            return {
                "sdk": sdk,
                "distance": distance,
                "param": bool(param),
                "codes": codes,
                "exit_ip": exit_ip,
            }
        finally:
            await browser.close()


# ---------------------------------------------------------------- 人在环路
#
# 上面整段都在论证「无头容器里全自动过验证做不到」。那么把**人**放回环路，
# 这条路立刻就通了 —— 因为卡住的从来不是「拖动距离」，而是「谁在拖」。
#
# 于是把人放到最省事的位置：人只负责「过验证」这一步（几秒钟），
# 剩下的取票、发码、收信、注册、查额度全自动。见 `ticket.py`。

# 在页面里包一层 SDK 的 success 回调，把 Param 落到自己的变量上，同时把
# 人机验证弹窗**主动拉出来**（不用先去点「发送验证码」）—— 少一步手动操作，
# 就少一处会出错的地方。
_HUMAN_HOOK_JS = """
(() => {
  window.__amd_captcha_param = window.__amd_captcha_param || '';
  window.__amd_captcha_opened = window.__amd_captcha_opened || false;
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
    try {
      window.__amd_captcha_instance =
        window.__amd_captcha_instance || orig.call(this, wrapped);
      if (window.__amd_captcha_instance && window.__amd_captcha_instance.show) {
        window.__amd_captcha_instance.show();
        window.__amd_captcha_opened = true;
      }
      return window.__amd_captcha_instance;
    } catch (e) {
      return undefined;
    }
  };
  return 'hooked';
})()
"""


async def human_captcha_session(
    *,
    email: str = "",
    phone: str = "",
    executable_path: str = "",
    proxy: str = "",
    stealth: bool = True,
    timeout: int = 300,
    screenshot_dir: str = "",
    poll: float = 1.0,
) -> dict:
    """开一个有头浏览器，把表单填好、弹窗拉出来，**等人过验证**。

    这是「真能过」的那条路：无头容器里过不去（见模块开头），
    但同一个脚本在有显示器 / VNC / 你自己的机器上，人拖一下滑块就过了 ——
    因为这时候设备指纹、行为画像、拖动轨迹**都是真的**。

    返回字典：

    - `param`：拿到就返回；超时/没拿到是空串
    - `screenshot`：当时的页面截图（远程排查时看一眼就知道卡在哪）
    - `address` / `phone`：本次会话绑定的邮箱与手机号（表单已替你填好）

    这里**刻意不替你拖滑块**。自动化拖动正是被判 `F015`/`F001` 的原因，
    人在环路的全部意义就是不自动化这一步。
    """
    async_playwright = _require_playwright()
    email = email or f"amd{int(time.time())}@example.com"
    phone = phone or random_cn_mobile()
    shot = ""

    async with async_playwright() as pw:
        launch_kwargs = _launch_kwargs(headless=False, executable_path=executable_path)
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            solver = BrowserCaptchaSolver(
                email=email, phone=phone, proxy=proxy, stealth=stealth,
                timeout=max(timeout, 60),
            )
            ctx = await solver._new_context(browser)
            page = await ctx.new_page()
            await page.goto(REGISTER_URL, wait_until="networkidle", timeout=timeout * 1000)
            await page.wait_for_timeout(3000)
            state = await page.evaluate(_HUMAN_HOOK_JS)
            await solver._fill_form(page, email)

            if state == "no-sdk":
                # SDK 挂在 SPA 初始化之后，等它出现再补一次钩子。
                for _ in range(20):
                    await page.wait_for_timeout(500)
                    state = await page.evaluate(_HUMAN_HOOK_JS)
                    if state != "no-sdk":
                        break

            # 把弹窗拉出来。SDK 实例只在 initAliyunCaptcha 被调用时才存在，
            # 所以先点一次「发送验证码」触发它 —— 反正等下注册也要发码。
            if not await page.evaluate("window.__amd_captcha_opened"):
                await solver._click_send_code(page)
                await page.wait_for_timeout(3000)
                await page.evaluate(_HUMAN_HOOK_JS)

            if screenshot_dir:
                os.makedirs(screenshot_dir, exist_ok=True)
                shot = os.path.join(screenshot_dir, "amd-register.png")
                await page.screenshot(path=shot)

            deadline = time.time() + timeout
            param = ""
            while time.time() < deadline:
                param = await page.evaluate("window.__amd_captcha_param || ''")
                if param:
                    break
                await page.wait_for_timeout(int(poll * 1000))

            await ctx.close()
            return {"param": str(param or ""), "screenshot": shot,
                    "address": email, "phone": phone}
        finally:
            await browser.close()
