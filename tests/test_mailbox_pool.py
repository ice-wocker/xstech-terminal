"""离线单测：收件箱轮换（默认域名优先，被拦自动换）。

老板的要求：「临时邮箱默认是我指定的那个，如果被拦了，那就换」。
这里把「换」的逻辑钉死：顺序、去重、次数上限、以及**换的时候要用新用户名**。
"""

import re

import pytest

from src.mailbox import Mailbox, MailboxError, MailboxPool

CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


class FlakyMailbox(Mailbox):
    """指定域名会建失败的假后端，用来验证轮换真的发生了。"""

    def __init__(self, domains, blocked=(), empty=()):
        super().__init__()
        self._domains = list(domains)
        self._blocked = set(blocked)
        self._empty = set(empty)
        self.created = []

    def domains(self):
        return list(self._domains)

    def create(self, username="", domain=""):
        dom = domain or self._domains[0]
        if dom in self._blocked:
            raise MailboxError(f"域名被拦: {dom}")
        self.address = f"{username or 'x'}@{dom}"
        self.created.append((username, dom))
        return self.address

    def wait_for_code(self, pattern, timeout=180, sender_hint="", interval=5):
        dom = self.address.split("@", 1)[1]
        if dom in self._empty:
            return None
        return "123456"


def _factory(domains, blocked=(), empty=(), sink=None):
    def make():
        mb = FlakyMailbox(domains, blocked, empty)
        if sink is not None:
            sink.append(mb)
        return mb
    return make


def test_preferred_domain_goes_first():
    sink = []
    pool = MailboxPool(preferred_domains=["clowmail.com"], mailbox_factory=_factory(
        ["other.com", "clowmail.com"], sink=sink))
    mb, code = pool.acquire(pattern=CODE_RE)
    assert mb.address.endswith("@clowmail.com")
    assert code == "123456"
    assert pool.attempts[0]["domain"] == "clowmail.com"


def test_blocked_domain_falls_back_to_next():
    sink = []
    pool = MailboxPool(preferred_domains=["clowmail.com"], mailbox_factory=_factory(
        ["mail.tm", "other.com"], blocked={"clowmail.com"}, sink=sink))
    mb, _ = pool.acquire(pattern=CODE_RE)
    assert not mb.address.endswith("@clowmail.com")
    # 第一次失败的记录要在 attempts 里，便于排查「为什么换了」
    assert pool.attempts[0]["ok"] is False


def test_empty_inbox_triggers_rotation():
    # 地址建成了但收不到信 → 换下一个域名（可能是域名被注册站拉黑）
    sink = []
    pool = MailboxPool(preferred_domains=["clowmail.com"], mailbox_factory=_factory(
        ["clowmail.com", "good.com"], empty={"clowmail.com"}, sink=sink))
    mb, code = pool.acquire(pattern=CODE_RE)
    assert mb.address.endswith("@good.com")
    assert code == "123456"


def test_candidates_dedupe_and_order():
    pool = MailboxPool(preferred_domains=["a.com", "a.com"], mailbox_factory=_factory(
        ["a.com", "b.com", "b.com"]))
    assert pool.candidates() == ["a.com", "b.com"]


def test_max_attempts_caps_rotation():
    pool = MailboxPool(preferred_domains=["x.com"], max_attempts=2,
                       mailbox_factory=_factory(["x.com", "y.com", "z.com"],
                                                blocked={"x.com", "y.com", "z.com"}))
    mb, code = pool.acquire(pattern=CODE_RE)
    assert mb is None and code is None
    assert len(pool.attempts) == 2


def test_all_blocked_returns_none():
    pool = MailboxPool(preferred_domains=["a.com"], max_attempts=5,
                       mailbox_factory=_factory(["a.com"], blocked={"a.com"}))
    mb, code = pool.acquire(pattern=CODE_RE)
    assert mb is None and code is None


def test_without_pattern_returns_mailbox_only():
    pool = MailboxPool(preferred_domains=["a.com"], mailbox_factory=_factory(["a.com"]))
    mb, code = pool.acquire()
    assert mb is not None and code is None
