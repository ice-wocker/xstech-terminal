"""临时邮箱收信层。

原设计只认 Guerrilla Mail 一家的接口，且把它硬编码在 cli.py 里。这里把
「收信」抽成一个可插拔后端，因为不同站点的注册邮件落在不同的邮箱服务上，
而收信这一步不该跟着注册目标一起改。

inboxes.com 是目前最省事的一个后端：**纯 HTTP，无需注册、无需鉴权、
无需 API key**。它的网页版走的是 socket.io 实时推送，但底层其实是一组
开放的 REST 路由，浏览器只是消费者：

    GET  /api/v2/domain            -> 18 个可用域名（含 clowmail.com）
    GET  /api/v2/inbox/<地址>       -> {"msgs": [...]} 消息列表
    GET  /api/v2/message/<uid>     -> 单封正文（text / html / 附件）

这三条都不需要 cookie、不需要 token，直接 curl 就能用。所以整条收信
链路可以完全跑在纯 HTTP 上，不必为了「等一封信」去开一个浏览器。

几个实测踩出来的点写在这里，免得以后重踩：

- **clowmail.com 不是「某个服务」，是 inboxes.com 的一个域名。**
  它没有 A 记录（拿不到 IP），只有 MX（mail.clowmail.com），因为它只
  用来收信。查 DNS 找不到网站是正常的，不代表域名不存在。
- **域名池会变。** 写死 clowmail.com 迟早会踩空，所以默认从 /domain
  动态取，只在明确指定时才用固定域名。
- **消息列表里的 uid 才是取正文的钥匙**，不是数组下标；列表每次刷新
  顺序都可能变。
"""

from __future__ import annotations

import random
import string
import time
from dataclasses import dataclass

import requests

INBOXES_BASE = "https://inboxes.com"

# 收件箱是「拉」的：列表接口没有长连接，只能轮询。
_POLL_INTERVAL = 5


class MailboxError(RuntimeError):
    """收信后端返回了非预期内容，或语义上失败。"""


@dataclass
class Message:
    uid: str
    sender: str
    subject: str
    received_at: str
    preview: str = ""

    @classmethod
    def from_api(cls, raw: dict) -> "Message":
        # 上游用极短的键名（f/s/cr/rr），这里翻成可读字段，别把上游的
        # 缩写漏给调用方 —— 换后端时调用方不该跟着改。
        return cls(
            uid=raw.get("uid", ""),
            sender=raw.get("f", ""),
            subject=raw.get("s", ""),
            received_at=raw.get("cr", ""),
            preview=raw.get("ph", "") or "",
        )

    def body(self, text: str) -> str:
        """把正文包成一个小对象，方便以后附加 html / 附件而不改接口。"""
        return text


class Mailbox:
    """inboxes.com 收信后端。

    默认从官方域名池里随机挑一个；也可显式指定域名与用户名，便于测试时
    固定地址。所有方法都只依赖标准 HTTP，不持有浏览器状态。
    """

    def __init__(self, base: str = INBOXES_BASE, domain: str = "", timeout: int = 20):
        self.base = base.rstrip("/")
        self.domain = domain
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Referer": f"{self.base}/",
        })
        self.address = ""

    # ---------- 基础设施 ----------

    def _get(self, path: str) -> dict:
        try:
            resp = self.session.get(f"{self.base}{path}", timeout=self.timeout)
        except requests.RequestException as exc:
            raise MailboxError(f"收信后端请求失败: {exc}") from exc
        if resp.status_code >= 400:
            # 把状态码带出来 —— 「后端拒了我们」和「还没收到信」是两回事，
            # 混成一句「没收到验证码」会让人查错方向。
            raise MailboxError(f"收信后端 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise MailboxError(f"收信后端返回非 JSON: {resp.text[:200]}") from exc

    # ---------- 邮箱 ----------

    def domains(self) -> list[str]:
        data = self._get("/api/v2/domain")
        out = [d.get("qdn", "") for d in data.get("domains", []) if d.get("qdn")]
        if not out:
            raise MailboxError("收信后端没有返回任何可用域名")
        return out

    def create(self, username: str = "", domain: str = "") -> str:
        """认领一个地址并返回它。

        inboxes.com 没有「创建」这步 —— 地址是**约定即存在**的：往
        /api/v2/inbox/<任意地址> 查，返回的空 msgs 就代表这个收件箱是
        活的。所以这里只是选一个没人用的随机名，不需要注册调用。
        """
        dom = domain or self.domain or random.choice(self.domains())
        if not username:
            username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        self.address = f"{username}@{dom}"
        return self.address

    def list_messages(self, address: str = "") -> list[Message]:
        addr = address or self.address
        if not addr:
            raise MailboxError("还没有地址，先调用 create()")
        data = self._get(f"/api/v2/inbox/{addr}")
        return [Message.from_api(m) for m in data.get("msgs", [])]

    def read_message(self, uid: str) -> dict:
        return self._get(f"/api/v2/message/{uid}")

    def wait_for_code(
        self,
        pattern,
        timeout: int = 180,
        sender_hint: str = "",
        interval: int = _POLL_INTERVAL,
    ) -> str | None:
        """轮询收件箱直到提取出验证码。

        pattern 是 `re.Pattern`，必须带**恰好一个捕获组**。之所以要求
        调用方给正则而不是让这里猜「6 位数字」，是因为临时邮箱的服务商
        欢迎邮件里也有数字串，全库抓数字会先把欢迎邮件抓出来。

        sender_hint 用来跳过非目标发件人。注意一个坑：**发件人过滤不能
        取代正则的精确性**，两者都要有 —— 实测里只靠发件人白名单时，
        「清空黑名单」这个变异测试仍然是绿的（假通过），因为真正的过滤
        是域名白名单做的，黑名单根本没被走到。
        """
        import re

        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                msgs = self.list_messages()
            except MailboxError:
                # 单次抖动容忍：直接放弃会让「网络慢一下」看起来像「没收到信」。
                time.sleep(interval)
                continue
            for msg in msgs:
                if msg.uid in seen:
                    continue
                seen.add(msg.uid)
                try:
                    detail = self.read_message(msg.uid)
                except MailboxError:
                    continue
                # 发件人过滤放在取正文之后：列表接口的 sender 是**截断**的
                # （实测固定 20 字符，尾部补 `...`），用完整地址当 hint 时
                # 永远匹配不上。详情里的 `f` 才是完整地址。
                full_sender = str(detail.get("f", "")) or msg.sender
                if sender_hint and sender_hint.lower() not in full_sender.lower():
                    continue
                haystack = " ".join(
                    str(detail.get(k, "")) for k in ("s", "text", "html", "ph")
                )
                found = pattern.search(haystack)
                if found:
                    return found.group(1)
            time.sleep(interval)
        return None


class MailboxBlocked(MailboxError):
    """当前收件箱被上游拦了（域名黑名单 / 地址被占 / 接口限流）。"""


class MailboxPool:
    """一组可轮换的收件箱。

    老板的要求是「临时邮箱默认是我指定的那个（inboxes.com / clowmail.com），
    如果被拦了，那就换」。落到实现上就是：

    1. 先按**指定域名**建地址（默认 `clowmail.com`），失败/被拦才换下一个域名；
    2. 域名池用完还没成，就换**后端**（后续可接自有域名 catch-all）；
    3. 每次换都换新用户名 —— 同一个地址被拒了，重试同一个没意义。

    「被拦」的判定条件（都是实测会遇到的）：

    - 建地址时后端直接报错（HTTP 403/429，或被 WAF 拦）；
    - 投递后长时间收不到信（`wait_for_code` 超时）—— 这可能是域名被注册站拉黑；
    - 上游把我们的地址返回 4xx（地址不可用）。

    池子只负责「换」，不负责判断「这封信该不该收」—— 那是 `wait_for_code`
    和调用方的事。
    """

    def __init__(
        self,
        preferred_domains: list[str] | None = None,
        mailbox_factory=None,
        max_attempts: int = 4,
    ):
        # 默认先试老板指定的 clowmail.com，再退到 inboxes.com 的其它域名。
        self.preferred_domains = preferred_domains or ["clowmail.com"]
        self.mailbox_factory = mailbox_factory or (lambda: Mailbox())
        self.max_attempts = max_attempts
        self.attempts: list[dict] = []

    def candidates(self) -> list[str]:
        """返回按优先级排好的候选域名（指定域名在前，其余从池里补）。"""
        mailbox = self.mailbox_factory()
        pool: list[str] = []
        for dom in self.preferred_domains:
            if dom and dom not in pool:
                pool.append(dom)
        try:
            available = mailbox.domains()
        except MailboxError:
            # 域名列表拿不到时，至少还能试指定的那个。
            available = []
        for dom in available:
            if dom not in pool:
                pool.append(dom)
        return pool

    def acquire(self, sender_hint: str = "", pattern=None, timeout: int = 180):
        """按优先级依次尝试：建地址 → 等信 → 返回 (mailbox, code)。

        返回 `(mailbox, None)` 表示地址建成了但没收到信（调用方可以再等或换）。
        返回 `(None, None)` 表示所有候选都失败了。
        """
        for domain in self.candidates()[: self.max_attempts]:
            mailbox = self.mailbox_factory()
            try:
                address = mailbox.create(domain=domain)
            except MailboxError as exc:
                self.attempts.append({"domain": domain, "ok": False, "error": str(exc)})
                continue
            self.attempts.append({"domain": domain, "address": address, "ok": True})
            if pattern is None:
                return mailbox, None
            code = mailbox.wait_for_code(pattern, timeout=timeout, sender_hint=sender_hint)
            if code:
                return mailbox, code
        return None, None
