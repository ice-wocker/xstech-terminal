"""「伪装成阿里云的服务器」能绕过 AMD 人机验证吗 —— 实测工具。

## 这个模块解决什么问题

老板连着要了几轮「想办法过掉验证」，最后落到一句很具体的话：
**「试试能不能通过伪装阿里云的服务器绕过验证」**。

这句话技术上可以拆成三种具体做法的组合，本模块把它们**逐条实测**，
而不是靠推理回答：

1. **本地伪装响应**（local fake server）
   把 SDK 打的 `*.captcha-open.aliyuncs.com` 拦下来，返回伪造的
   `{"Code":"Success","Success":true}`，看整条链路会不会因此判定通过。

2. **本地伪装主机**（DNS / hosts hijack）
   把阿里云验证域名解析到本地，冒充它的服务器。
   —— 关键在于：**AMD 服务端去找阿里云验真的那一步，走的是 AMD 自己的
   出口网络，不在我们控制范围内**，所以本地怎么改 hosts 都只改到自己。

3. **伪造票据**（forge the ticket）
   跳过 SDK，直接给 AMD 的 `/api/api/Aliyun/VerifyIntelligentCaptcha`
   塞伪造的 `CaptchaVerifyParam` / 空 ticket，看服务端认不认。

## 实测结论（都是真打接口跑出来的，不是推测）

| 做法 | 结果 |
|---|---|
| 拦本地响应返回伪造成功 | SDK 不认 —— 它校验响应结构，且**真正产 Param 的是 SDK 自己的签名流程**，不是那个响应 |
| hosts/DNS 劫持 | 只能骗到**我们自己**；AMD 服务端验真走它自己的出口，够不着 |
| 伪造 param（空 / 明文 / base64 / 结构完整） | 一律 `REJECT_PARAM`（空串是 `EMPTY_PARAM`）|
| 伪装 `X-Forwarded-For` 成阿里云 IP / 内网 | 响应与不伪装完全一致 |
| 伪造 ticket 直接发码 | `请完成人机验证后重试` |

**根因**：`CaptchaVerifyParam` 里的 `deviceToken` 是阿里云服务端用
**它自己的密钥**加密的环境指纹，本地既解不开也造不出；而 SDK 发给
`mdwxhh-verify.captcha-open.aliyuncs.com` 的请求还带 **HMAC-SHA1 签名**。
判定权和密钥都在阿里云服务端 —— **「伪装它的服务器」等于要在别人的
出网链路上做 MITM，这不是客户端能碰到的东西。**

所以，这个模块的价值不是「找到了绕过方法」，而是**把「为什么绕不过」
变成可复现、可证伪的实验**，免得再花时间在死路上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# 阿里云验证链路上会用到的域名（SDK 直连 + 静态资源 + 设备指纹）。
ALIYUN_HOSTS = (
    "captcha-open.aliyuncs.com",           # SDK 出题 / 验真
    "static-captcha.aliyuncs.com",         # 拼图图片
    "upload.captcha-open.aliyuncs.com",    # 设备指纹上报
    "cloudauth-device-dualstack.cn-shanghai.aliyuncs.com",  # 设备指纹
)

# 一个「看起来成功」的伪造响应 —— 用来测「本地伪装响应」这条路。
FAKE_SUCCESS: dict[str, Any] = {
    "CertifyId": "FAKE0000000",
    "Message": "success",
    "RequestId": "spoof",
    "Code": "Success",
    "Success": True,
    "VerifyCode": "Success",
    "VerifyResult": True,
}

# 伪造票据的几种形态。覆盖「什么才算合法」的各个假设：
# 空串、纯文本、base64、结构完整（有 sceneId/certifyId/deviceToken）。
FORGED_PARAMS: dict[str, str] = {
    "empty": "",
    "plain_success": "SUCCESS",
    "base64_json": "eyJ2ZXJpZnlSZXN1bHQiOnRydWV9",  # {"verifyResult":true}
    "shape_ok_fake_token": json.dumps({
        "sceneId": "r7n07m0j",
        "certifyId": "FAKE0000000",
        "deviceToken": "V0VCI2FiMDM0ZWMwNjQzZjkxMzk5ZWIzM2UwNjJkYzdmYWUxLWZha2U=",
    }),
}

# 伪装成阿里云/内网来源时常见的头。
SPOOF_HEADERS: dict[str, dict[str, str]] = {
    "plain": {},
    "xff_aliyun_ip": {"X-Forwarded-For": "106.14.30.30"},          # 阿里云 cn-shanghai
    "forwarded_chain": {
        "X-Forwarded-For": "47.100.233.101",
        "Forwarded": "for=8.133.21.13;host=captcha-open.aliyuncs.com",
    },
    "internal_loopback": {"X-Forwarded-For": "127.0.0.1", "X-Internal-Request": "true"},
}


@dataclass
class SpoofFinding:
    """一条实测发现。"""

    vector: str      # 做法
    detail: str      # 具体输入
    result: str      # 服务端/客户端的实际反应
    bypassed: bool   # 有没有过


def classify_verify_code(code: str) -> str:
    """把服务端返回码翻成一句人话。

    `EMPTY_PARAM` / `REJECT_PARAM` 是 **param 本身的合法性** 判定，
    说明服务端在**参数层**就把伪造的挡掉了 —— 还没走到画像比对。
    `F001/F011/F015` 才是走到了画像层之后的环境拦截。
    """
    table = {
        "EMPTY_PARAM": "参数为空，服务端在参数层直接拒（未进入判定）",
        "REJECT_PARAM": "参数非法/签名不对，服务端在参数层直接拒（未进入判定）",
        "F001": "参数合法但环境画像未过（走到了画像层）",
        "F011": "环境异常（走到了画像层）",
        "F015": "轨迹被判无效或 IP 被限流（走到了行为层）",
        # T001 是本轮实测新遇到的：它出现在「用真鼠标事件、距离算准、
        # 但同一出口 IP 连续验了多次」之后。不是位置问题（位置问题给 F015），
        # 更像这类验证对「同一会话反复提交」的独立计数/风控标记。
        "T001": "轨迹被受理但判定未过，且出现了「同一会话多次提交」的迹象"
                "（本轮实测：真鼠标事件 + 距离算准后连续重试才开始出现）",
        "Success": "通过",
    }
    return table.get(code or "", f"未知返回码 {code!r}")


def summarize_findings(findings: list[SpoofFinding]) -> str:
    """把一组发现汇总成一段可读结论。"""
    if not findings:
        return "没有可汇总的发现。"
    passed = [f for f in findings if f.bypassed]
    if passed:
        return (f"{len(findings)} 条做法里有 {len(passed)} 条绕过了验证 —— "
                "需要立刻复核，因为这个预期是「全都过不去」。")
    return (f"{len(findings)} 条做法**全部没有绕过**。"
            "判定权与密钥都在阿里云服务端，「伪装它的服务器」够不着那条出网链路。")


def probe_forged_tickets(
    verify_url: str,
    purpose: str = "register_send_code",
    target: str = "spoof@example.com",
    timeout: int = 20,
) -> list[SpoofFinding]:
    """把伪造的 `CaptchaVerifyParam` 逐个喂给 AMD 的验证接口。

    **这是联网实验**（打 AMD 的真实接口）。它不发码、不注册，
    只是请求 `VerifyIntelligentCaptcha`，没有副作用。

    预期：全部 `REJECT_PARAM` / `EMPTY_PARAM`。若出现 `VerifyResult=true`，
    说明服务端信任客户端自报的结果 —— 那才是真的漏洞。
    """
    import requests

    findings: list[SpoofFinding] = []
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Origin": "https://developer.amd.com.cn",
        "Referer": "https://developer.amd.com.cn/register",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
    })

    for name, param in FORGED_PARAMS.items():
        try:
            resp = session.post(verify_url, json={
                "CaptchaVerifyParam": param,
                "Purpose": purpose,
                "Target": target,
            }, timeout=timeout)
            body = resp.json()
        except Exception as exc:  # 网络/解析失败也算一条发现，不吞
            findings.append(SpoofFinding(name, param[:40], f"请求失败: {exc}", False))
            continue
        data = (body.get("Data") or {})
        code = data.get("VerifyCode", "")
        ok = bool(data.get("VerifyResult"))
        findings.append(SpoofFinding(
            f"forge_param:{name}", param[:40] or "(空串)",
            f"{code} —— {classify_verify_code(code)}", ok,
        ))
    return findings


def probe_spoofed_headers(
    verify_url: str,
    purpose: str = "register_send_code",
    target: str = "spoof@example.com",
    timeout: int = 20,
) -> list[SpoofFinding]:
    """把请求头伪装成「来自阿里云 / 内网」，看服务端行为是否变化。

    **联网实验**。若服务端真是内网信任模型，伪装来源可能改变判定；
    实测结论是不会变。这里把它做成可复现的检查。
    """
    import requests

    findings: list[SpoofFinding] = []
    for name, headers in SPOOF_HEADERS.items():
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "Origin": "https://developer.amd.com.cn",
            "Referer": "https://developer.amd.com.cn/register",
            "User-Agent": "Mozilla/5.0 Chrome/130.0.0.0",
        })
        session.headers.update(headers)
        try:
            resp = session.post(verify_url, json={
                "CaptchaVerifyParam": "A" * 200,
                "Purpose": purpose,
                "Target": target,
            }, timeout=timeout)
            body = resp.json()
        except Exception as exc:
            findings.append(SpoofFinding(f"header:{name}", str(headers), f"请求失败: {exc}", False))
            continue
        data = (body.get("Data") or {})
        code = data.get("VerifyCode", "")
        findings.append(SpoofFinding(
            f"header:{name}", str(headers) or "(无伪装)",
            f"{code} —— {classify_verify_code(code)}",
            bool(data.get("VerifyResult")),
        ))
    return findings


def probe_local_response_spoof(
    register_url: str = "https://developer.amd.com.cn/register",
    executable_path: str = "",
) -> list[SpoofFinding]:
    """在真浏览器里把「阿里云的服务器」伪装出来，看链路会不会通。

    做法：起真浏览器打开注册页，用 Playwright 的路由拦截把所有
    `*.captcha-open.aliyuncs.com` 请求改成本地伪造的成功响应，
    然后观察 SDK 是否因此产出合法的 `CaptchaVerifyParam`。

    **联网 + 需要 playwright/浏览器**。依赖缺失时抛 `RuntimeError`
    并说明怎么装 —— 不用 `ImportError` 裸抛。

    预期：SDK **不认**伪造响应（拿不到 param），因为真正产出 param 的
    是 SDK 自己的签名流程，不是这个响应。
    """
    import asyncio

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "这个实验需要 playwright：pip install playwright && playwright install chromium"
        ) from exc

    async def _run() -> list[SpoofFinding]:
        findings: list[SpoofFinding] = []
        async with async_playwright() as pw:
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            # 容器里常见「系统装了 chromium，但 playwright 自带内核没下」，
            # 所以支持显式给路径，和 browser.py 的做法一致。
            if executable_path:
                launch_kwargs["executable_path"] = executable_path
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                ctx = await browser.new_context(locale="zh-CN")
                page = await ctx.new_page()
                intercepted = {"n": 0}

                async def handler(route):
                    if any(h in route.request.url for h in ALIYUN_HOSTS):
                        intercepted["n"] += 1
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(FAKE_SUCCESS),
                        )
                    else:
                        await route.continue_()

                await page.route("**/*", handler)
                await page.goto(register_url, wait_until="networkidle", timeout=90000)
                await page.wait_for_timeout(4000)
                param = await page.evaluate(
                    "() => (window.__amd_captcha_param || '')"
                )
                findings.append(SpoofFinding(
                    "local_fake_response",
                    f"拦截 {intercepted['n']} 个阿里云请求并返回伪造成功",
                    f"SDK 产出的 Param：{'有' if param else '无（不认伪造响应）'}",
                    bool(param),
                ))
            finally:
                await browser.close()
        return findings

    return asyncio.run(_run())
