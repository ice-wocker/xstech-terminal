"""真人代验证：把「人在浏览器里过验证」这一段的产物接进自动流程。

## 为什么要有这一层

`browser.py` 已经用实测证明：**容器里全自动过阿里云智能验证做不到**
（位置算得再准也只是从 `F015` 变成 `F001`，卡在服务端的设备/行为画像上）。
`bruteclose.py` 又把客户端可控的七个向量族挨个试完，封了口。

那么剩下唯一一条**真能走通**的路，就是路线里本来就写着的那句：

> 在你自己的浏览器里过完验证、从 `initAliyunCaptcha` 的 `success` 回调
> 抓出 Param，喂给命令即可（这条路径**可以全自动走完**）。

这个模块就是把这句话工程化：

    真浏览器（有头 / 你自己的机器）
      ├─ 打开注册页，把表单填好（随机邮箱 + 随机手机号）
      ├─ 人点一下「发送验证码」→ 拖滑块（人到场，画像就是真的）
      └─ 抓到 CaptchaVerifyParam → 当场换 CaptchaTicket → 存盘
    CLI
      └─ 读存盘票据 → 自动发码 → 自动收信 → 自动提交注册 → 打印账号

**这不是破解，是「人在环路」（human-in-the-loop）。** 它的价值不在技术含量，
而在于：一个人花 30 秒过验证，剩下发码、收信、注册、查额度全部自动，
并且票据能在有效期内复用（同一出口 IP 的整批注册只需过一次验证）。

## 票据的真相：短寿命

`CaptchaVerifyParam` 有效期只有几分钟，换来的 `CaptchaTicket` 也是。
所以这里不叫「绕过」，叫**在有效期内把流程跑完**：

- `send` 阶段：过验证 → 立刻 `VerifyIntelligentCaptcha` → 落盘 ticket + 过期时间
- `register` 阶段：读盘 → 若已过期就明确报错让你再过一次，**不硬闯**

## 与 `--captcha-param` 的关系

CLI 上原来就有个 `--captcha-param`，那个要求你自己去浏览器里抓串再粘回来。
`ticket.py` 是它的自动化版本：抓串、换票、存盘、复用，都不用你碰。
`--captcha-param` 保留不变（无浏览器环境时仍可用）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

# 票据文件权限 0600 —— 它等价于「刚过完人机验证的那个会话」，别给同机器别的用户看。
TICKET_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "xstech-gateway",
    "amd-ticket.json",
)

# 阿里云 param 的窗口是分钟级；这里给一个保守的上限，避免拿一张快过期的票去注册。
# 真正的过期判据是服务端返回「人机验证已过期」，所以这只是个提前量。
DEFAULT_TTL = 240


@dataclass
class Ticket:
    """一次真人验证换来的票据。"""

    ticket: str
    email: str = ""
    phone: str = ""
    proxy: str = ""
    created_at: float = 0.0
    ttl: int = DEFAULT_TTL
    scene_id: str = ""

    @property
    def expires_at(self) -> float:
        return (self.created_at or 0) + self.ttl

    def alive(self, now: float | None = None) -> bool:
        return bool(self.ticket) and (now or time.time()) < self.expires_at

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - (self.created_at or 0)

    def describe(self, now: float | None = None) -> str:
        if not self.ticket:
            return "无票据"
        state = "有效" if self.alive(now) else "已过期"
        return f"{state}（{int(self.age(now))}s / {self.ttl}s，目标 {self.email or '?'}）"


def save_ticket(t: Ticket, path: str = TICKET_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(t), fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load_ticket(path: str = TICKET_FILE) -> Ticket | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    known = {f for f in Ticket.__dataclass_fields__}
    return Ticket(**{k: v for k, v in data.items() if k in known})


def clear_ticket(path: str = TICKET_FILE) -> bool:
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def pick_ticket(explicit: str = "", path: str = TICKET_FILE) -> Ticket | None:
    """优先用显式传入的 param，其次用存盘的票。两条路都不新鲜就返回 None。"""
    if explicit:
        return Ticket(ticket=explicit, created_at=time.time(), ttl=DEFAULT_TTL)
    return load_ticket(path)
