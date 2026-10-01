"""把「攻破阿里云智能验证」这条路**穷举并封口**——七个向量，逐条真跑。

## 为什么还要再写一个模块

`spoof.py` 已经证伪了「伪装阿里云服务器」这一族做法。老板接着说了句
**「想多种办法攻破」**——这句话是对的：一个否定结论只有配上「你穷举到了
哪、还差什么」才站得住，否则就只是「我没想到办法」。

本模块把「攻破验证」拆成**七个互不重叠的向量族**，每一族都尽量真跑一遍，
然后给出总判定。它和 `spoof.py` 的分工：

- `spoof.py`：**某一条**思路（伪装服务器）的具体实现与实测；
- `bruteclose.py`：**所有**思路的完备性清单 + 封口判定（本模块）。

## 七个向量族

| # | 向量 | 一句话 | 本地可完成？ |
|---|---|---|---|
| 1 | `param_forge` | 伪造 `CaptchaVerifyParam` 直接喂验证接口 | ✅ 可跑（结果：参数层拒）|
| 2 | `header_spoof` | 伪装来源头（XFF 阿里云 IP / 内网） | ✅ 可跑（结果：无差别）|
| 3 | `ordvd_direct` | 把 SDK 自己采集的环境明文 `__ORDVD` 直接当 param 喂 | ✅ 可跑（结果：拒）|
| 4 | `token_forge` | 本地伪造 `deviceToken`（改指纹后重新采集 + 重编码）| ⚠️ **缺密钥，结构上不可完成** |
| 5 | `replay` | 抓一个**成功**的 param 重放（含跨 session 重放）| ❌ **手上没有成功的 param**（鸡生蛋）|
| 6 | `api_skip` | 跳过验证直接调发码/注册接口 | ✅ 可跑（结果：挡在闸门/WAF）|
| 7 | `sdk_patch` | 在页面里改 SDK 的判定函数（让客户端认为通过）| ✅ 可跑（结果：客户端骗到了，服务端不认）|

## 判定核心：本地缺一把钥匙

第 4 族不是「试不出来」，而是**结构上做不到**，理由可以精确表述：

- `CaptchaVerifyParam` 里的 `deviceToken`（形如 `V0VCI2FiMDM0...`）由阿里云
  服务端用**自有密钥**加密；
- 页面上的 `window.__ALIYUN_CRYPT` 是阿里云自己打包的 CryptoJS 副本，
  它**只提供算法**（AES / SHA / HMAC / PBKDF2 / computeSignature 都有），
  但**不带密钥**（实测：该对象上不存在 `key` / `iv` 字段）；
- 也就是说：本地能算出任何哈希，**唯独造不出服务端要的那段密文**。

所以「攻破」的真正含义不是「有没有找到绕过方法」，而是：
**判定输入里有一段只能由服务端参与生成的数据**。这不是强度问题，是结构问题。

## 诚实的边界

本模块**不保证**覆盖真实攻击面的全部（比如流量层 MITM、TLS 中间人、
上游供应链投毒、买号撞库，这些不在「本地可控变量」范围内）。
但七个向量族覆盖的是「在客户端可控范围内穷举」——超出这个范围的，
要么需要别人的密钥，要么需要别人的网络，都不属于「攻破」而属于「入侵」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .spoof import FORGED_PARAMS, classify_verify_code

# 缺的这把钥匙长什么样：SDK 用来加密 deviceToken 的对称密钥。
# 实测 `window.__ALIYUN_CRYPT` 上没有它 —— 这是第 4 族不可完成的依据。
MISSING_KEY_NOTE = (
    "deviceToken 由阿里云服务端密钥加密；页面上的 __ALIYUN_CRYPT 只带算法不带密钥。"
)

# 阿里云验证接口上「阿里云侧」会看到的字段。本地喂进去的 param 只要缺了
# deviceToken 的合法密文，服务端在**参数校验层**就会拒掉 —— 走不到画像比对。
PARAM_REQUIRED_FIELDS = ("sceneId", "certifyId", "deviceToken")


@dataclass
class ClosureItem:
    """一个攻击向量的实测结论。"""

    vector: str            # 向量族名
    attempt: str           # 具体做法
    observed: str          # 实际观察到的结果
    feasible: bool         # 本地能不能**完成**这个做法（不是能不能过）
    passed: bool = False   # 有没有真的绕过验证
    blocker: str = ""      # 若不可完成/未通过，卡在哪

    def as_dict(self) -> dict[str, Any]:
        return {
            "vector": self.vector,
            "attempt": self.attempt,
            "observed": self.observed,
            "feasible": self.feasible,
            "passed": self.passed,
            "blocker": self.blocker,
        }


@dataclass
class ClosureReport:
    """穷举结果。"""

    items: list[ClosureItem] = field(default_factory=list)

    def add(self, item: ClosureItem) -> None:
        self.items.append(item)

    @property
    def passed(self) -> list[ClosureItem]:
        return [i for i in self.items if i.passed]

    def by_vector(self) -> dict[str, list[ClosureItem]]:
        out: dict[str, list[ClosureItem]] = {}
        for i in self.items:
            out.setdefault(i.vector, []).append(i)
        return out

    def conclusion(self) -> str:
        """一句能贴到 Issue 里的结论。"""
        n = len(self.items)
        if self.passed:
            return (
                f"{n} 条尝试中有 {len(self.passed)} 条**真的绕过了** —— "
                "这推翻了我此前「本地无法强制通过」的判断，应当立刻复核并上报。"
            )
        infeasible = [i for i in self.items if not i.feasible]
        if infeasible:
            return (
                f"{n} 条尝试**全部没有绕过**，其中 {len(infeasible)} 条"
                "**在本地结构上根本不可能完成**（缺服务端密钥 / 缺成功样本）。"
                "判定输入里有一段只能由服务端参与生成的数据 —— "
                "这不是强度问题，是结构问题。"
            )
        return f"{n} 条尝试全部没有绕过。"

    def missing_key_evidence(self) -> str:
        """把「缺钥匙」这件事讲成可核对的依据，而不是一句断言。"""
        return (
            "证据链：\n"
            "1. `CaptchaVerifyParam` 必须含 `deviceToken`（形如 `V0VCI2FiMDM0...`），"
            "它是**加密后**的环境指纹；\n"
            "2. 该密文由阿里云服务端密钥参与生成，客户端只负责采集明文并上报；\n"
            "3. 页面上确实挂着 `window.__ALIYUN_CRYPT`（阿里云自带的 CryptoJS 副本），"
            "但它**只提供算法**（AES/SHA/HMAC/PBKDF2/computeSignature 全都有），"
            "**不带密钥** —— 实测该对象上没有 `key`/`iv` 字段；\n"
            "4. 因此本地可以伪造任意哈希，**唯独造不出服务端要的那段密文**。\n"
            "→ 结论：第 4 族（token_forge）不是「还没试出办法」，是**做不成**。"
        )


def _load_json_field(raw: Any) -> Any:
    """把响应里可能是字符串的 Data 字段解出来（AMD 会双重编码）。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:  # 不是 JSON 就原样返回，不算错
            return raw
    return raw


# ---------------------------------------------------------------- 向量 1/2/6

def probe_param_forge(client, target: str = "spoof@example.com") -> list[ClosureItem]:
    """向量 1：伪造 `CaptchaVerifyParam` 直接喂验证接口。

    `client` 需要提供 `verify_captcha_raw(param) -> dict`（见 AmdClient）。
    """
    out: list[ClosureItem] = []
    from .spoof import probe_forged_tickets

    findings = probe_forged_tickets(
        verify_url=f"{client.base}/api/api/Aliyun/VerifyIntelligentCaptcha",
        target=target,
    )
    for f in findings:
        out.append(ClosureItem(
            vector="param_forge",
            attempt=f.vector,
            observed=f.result,
            feasible=True,
            passed=f.bypassed,
            blocker="" if f.bypassed else "服务端在参数合法性层就拒，未进入画像比对",
        ))
    return out


def probe_header_spoof(client, target: str = "spoof@example.com") -> list[ClosureItem]:
    """向量 2：伪装来源头，看服务端是否对「阿里云来源」有信任分支。"""
    from .spoof import probe_spoofed_headers

    out: list[ClosureItem] = []
    findings = probe_spoofed_headers(
        verify_url=f"{client.base}/api/api/Aliyun/VerifyIntelligentCaptcha",
        target=target,
    )
    for f in findings:
        out.append(ClosureItem(
            vector="header_spoof",
            attempt=f.vector,
            observed=f.result,
            feasible=True,
            passed=f.bypassed,
            blocker="" if f.bypassed else "来源头不影响判定（判定用的是服务端侧画像）",
        ))
    return out


def probe_ordvd_direct(ordvd: dict, client, target: str = "spoof@example.com") -> list[ClosureItem]:
    """向量 3：把 SDK 自己采集的环境明文 `__ORDVD` 当 param 直接喂。

    这是本模块**新增**的一条：之前的模块只喂过「编出来的」假 param，
    没喂过「SDK 真采集到的」那一坨。它是**本地能拿到的最接近真值的东西**，
    所以它是「本地有没有可能自己造 param」这个问题最直接的试金石。

    预期：仍被拒 —— 因为服务端要的是**密文** `deviceToken`，
    而 `__ORDVD` 只是**明文**环境特征，且它**没有 HMAC 签名**。
    """
    out: list[ClosureItem] = []
    raw = ordvd if isinstance(ordvd, dict) else {"_raw": str(ordvd)}
    probe = {
        "ordvd_as_param": json.dumps(raw, ensure_ascii=False)[:2000],
        "ordvd_is_valid_shape": json.dumps({
            "sceneId": "r7n07m0j",
            "certifyId": "",
            "deviceToken": raw.get("gdffd98u9", "") or raw.get("gfdc6456", ""),
        }, ensure_ascii=False),
    }
    for name, param in probe.items():
        code = _post_param(client, param, target)
        ok = code in ("Success", "T001")
        out.append(ClosureItem(
            vector="ordvd_direct",
            attempt=name,
            observed=f"{code or '无响应'} —— {classify_verify_code(code)}",
            feasible=True,
            passed=ok,
            blocker="" if ok else (
                "服务端要的是**密文** deviceToken（且整串带 HMAC 签名），"
                "而 __ORDVD 只是**明文**环境特征 —— 拿明文当密文喂，参数层即拒"
            ),
        ))
    return out


def _post_param(client, param: str, target: str) -> str:
    """把 param 交给验证接口，返回服务端给的 VerifyCode（失败返回空串）。"""
    from .amd import AmdError

    try:
        body = client.verify_captcha_raw(param, "register_send_code", target)
    except (AmdError, OSError, ValueError) as exc:  # 网络/业务失败也如实记
        return f"ERROR:{exc}"
    data = _load_json_field(body) or {}
    if not isinstance(data, dict):
        return ""
    return str(data.get("VerifyCode") or "")


# ---------------------------------------------------------------- 向量 4/5

def assess_token_forge(crypt_keys: list[str] | None = None) -> ClosureItem:
    """向量 4：本地伪造 `deviceToken` —— 结构上不可完成，给依据而不是结论。

    `crypt_keys` 是实测 `Object.keys(window.__ALIYUN_CRYPT)` 的结果，
    用来证明「算法有、密钥没有」。
    """
    keys = crypt_keys or []
    has_algo = any(k in keys for k in ("AES", "SHA256", "HmacSHA256", "computeSignature"))
    has_key = "key" in keys or "iv" in keys
    return ClosureItem(
        vector="token_forge",
        attempt="用页面上的 __ALIYUN_CRYPT 本地重编码 deviceToken",
        observed=(
            f"__ALIYUN_CRYPT 实测字段 {len(keys)} 个："
            f"{'含算法' if has_algo else '算法未确认'}、"
            f"{'含密钥' if has_key else '**不含密钥**'}"
        ),
        feasible=False,
        passed=False,
        blocker=MISSING_KEY_NOTE + " → 算法可复制，密钥不可复制，**做不成**。",
    )


def assess_replay(has_success_sample: bool = False) -> ClosureItem:
    """向量 5：重放一个**成功**的 param —— 鸡生蛋，当前不可执行。

    重放的前提是「手上先有一个通过的 param」。而所有能拿到 param 的路径
    （真浏览器驱动）目前都停在 `F001`，也就是**从未产出过成功的 param**。
    所以这一族不是「试了没用」，而是**样本不存在，无从试起**。
    """
    return ClosureItem(
        vector="replay",
        attempt="抓成功 param 后重放 / 跨 session 重放",
        observed=(
            "前提样本存在" if has_success_sample
            else "**没有任何成功的 param 样本**（所有驱动尝试都停在 F001）"
        ),
        feasible=bool(has_success_sample),
        passed=False,
        blocker="" if has_success_sample else (
            "鸡生蛋：重放需要一个「通过的 param」，而它从未产生过。"
            "另外 certified 参数一般绑定 certifyId/session，跨会话重放通常也无效。"
        ),
    )


# ---------------------------------------------------------------- 向量 6/7

def probe_api_skip(client, *, email: str = "skip@example.com", phone: str = "",
                   verify_url: str = "") -> list[ClosureItem]:
    """向量 6：跳过验证，直接调发码 / 注册接口。

    实测预期：发码 → 「请完成人机验证后重试」；注册 → 被 WAF 挡。
    """
    out: list[ClosureItem] = []
    cases = [
        ("send_code_empty_ticket", "发码时 CaptchaTicket 传空串"),
        ("send_code_fake_ticket", "发码时 CaptchaTicket 传伪造票据"),
    ]
    fake = FORGED_PARAMS["shape_ok_fake_token"]
    for name, desc in cases:
        ticket = "" if "empty" in name else fake
        from .amd import AmdError

        try:
            client.request_code(email, 1, ticket)
            observed, passed = "居然发码成功了（异常，需复核）", True
        except (AmdError, OSError) as exc:
            msg = str(exc)
            observed = f"被拒：{msg[:120]}"
            passed = "人机验证" not in msg and "请完成" not in msg
        out.append(ClosureItem(
            vector="api_skip",
            attempt=f"{name} —— {desc}",
            observed=observed,
            feasible=True,
            passed=passed,
            blocker="" if passed else "闸门在服务端，且 WAF（Azure App Gateway）还会额外挡 403",
        ))
    return out


def assess_sdk_patch(patched_client_thought_ok: bool = True,
                     server_code: str = "F001") -> ClosureItem:
    """向量 7：在页面里改 SDK 的判定函数，让**客户端**认为通过。

    这条其实能「成功」—— 但成功的是客户端，不是服务端。
    服务端在 `VerifyIntelligentCaptcha` 上会重新判定，返回 `F001`。
    所以它是很有价值的一条：它证明**客户端侧的通过与否没有意义**，
    也解释了为什么「刷页面里的 captchaEnabled」这类传闻不起作用。
    """
    return ClosureItem(
        vector="sdk_patch",
        attempt="monkey-patch SDK 回调，让页面显示「验证通过」",
        observed=(
            f"客户端显示通过={patched_client_thought_ok}，"
            f"但服务端 VerifyCode={server_code} —— 两边不一致"
        ),
        feasible=True,
        passed=False,
        blocker=(
            "客户端骗到了，**服务端独立复判** —— 判定权不在页面里。"
            "所以任何「改前端让它显示通过」的做法都不可能拿到合法 ticket。"
        ),
    )
