"""离线单测：收信后端不联网，用假后端验证协议与提取逻辑。

收信这一步最容易出错的地方不在网络，而在**字段语义**：上游用极短的键名、
发件人字段还会被截断。这些行为必须被钉死在测试里，否则换个真实邮箱服务
就会静默失效（返回 None 而不是报错，最难查）。
"""

import re

import pytest

from src.mailbox import Mailbox, MailboxError, Message


class FakeMailbox(Mailbox):
    """把 HTTP 层换成内存数据，其余逻辑照跑。"""

    def __init__(self, domains=("clowmail.com",), messages=()):
        super().__init__()
        self._domains = list(domains)
        # messages: list[dict]，模拟 /api/v2/inbox 返回的原始形态
        self._messages = list(messages)
        self.calls = []

    def _get(self, path):
        self.calls.append(path)
        if path == "/api/v2/domain":
            return {"domains": [{"qdn": d} for d in self._domains]}
        if path.startswith("/api/v2/inbox/"):
            # 真实上游的列表接口把发件人截断到 20 字符（尾部补 `...`）。
            # 假后端必须照抄这个行为，否则「用截断字段做过滤」的缺陷
            # 在测试里永远暴露不出来（实测：不截断时变异测试是假绿的）。
            listed = []
            for m in self._messages:
                m2 = dict(m)
                m2["f"] = (m["f"][:17] + "...") if len(m["f"]) > 20 else m["f"]
                listed.append(m2)
            return {"msgs": listed}
        if path.startswith("/api/v2/message/"):
            uid = path.rsplit("/", 1)[-1]
            return next(
                (m for m in self._messages if m["uid"] == uid),
                {},
            )
        raise MailboxError(f"未预期的路径 {path}")


def _raw(uid="u1", f="no-reply@amd.com", s="Your code", text="code is 123456", ph=""):
    return {"uid": uid, "f": f, "s": s, "ph": ph, "cr": "2026-01-01T00:00:00Z",
            "text": text}


def test_domains_survive_missing_qdn():
    mb = FakeMailbox(domains=("a.com",))
    assert mb.domains() == ["a.com"]


def test_create_uses_requested_domain():
    mb = FakeMailbox(domains=("clowmail.com", "other.com"))
    addr = mb.create(username="abc", domain="other.com")
    assert addr == "abc@other.com"


def test_create_picks_from_pool_when_no_domain():
    mb = FakeMailbox(domains=("only.com",))
    addr = mb.create()
    _, _, dom = addr.partition("@")
    assert dom == "only.com"
    assert len(addr.split("@")[0]) == 10


def test_message_maps_short_keys():
    m = Message.from_api(_raw(uid="x", f="a@b.com", s="Sub", ph="pre"))
    assert (m.uid, m.sender, m.subject, m.preview) == ("x", "a@b.com", "Sub", "pre")


def test_wait_for_code_extracts_from_text():
    mb = FakeMailbox(messages=[_raw(text="Your code is 445566")])
    mb.address = "x@y.com"
    assert mb.wait_for_code(re.compile(r"(?<!\d)(\d{6})(?!\d)"), timeout=1) == "445566"


def test_wait_for_code_reads_subject_too():
    mb = FakeMailbox(messages=[_raw(s="Verify 998877", text="")])
    mb.address = "x@y.com"
    assert mb.wait_for_code(re.compile(r"(?<!\d)(\d{6})(?!\d)"), timeout=1) == "998877"


def test_sender_filter_matches_full_address_not_truncated_list_field():
    """这是实测挖到的坑：列表里 `f` 被截断成 20 字符带 `...`。

    如果拿列表字段做过滤，用完整地址当 hint 会永远匹配不上 —— 而且
    表现是「静默返回 None」，不是报错。所以过滤必须用详情里的完整地址。
    """
    mb = FakeMailbox(messages=[_raw(f="no-reply@verify.example", text="code 112233")])
    mb.address = "x@y.com"
    # 列表字段确实是截断的（20 字符 + `...`），这是上游行为
    assert mb.list_messages()[0].sender == "no-reply@verify.e..."
    # 详情里是完整地址
    assert mb.read_message("u1")["f"] == "no-reply@verify.example"
    # 用完整地址过滤仍能命中
    code = mb.wait_for_code(
        re.compile(r"(?<!\d)(\d{6})(?!\d)"), timeout=1, sender_hint="verify.example"
    )
    assert code == "112233"


def test_sender_filter_skips_other_senders():
    mb = FakeMailbox(messages=[
        _raw(uid="a", f="welcome@inboxes.com", text="digits 999999"),
        _raw(uid="b", f="no-reply@amd.com", text="code 121212"),
    ])
    mb.address = "x@y.com"
    code = mb.wait_for_code(
        re.compile(r"(?<!\d)(\d{6})(?!\d)"), timeout=1, sender_hint="amd.com"
    )
    assert code == "121212"


def test_wait_for_code_times_out_without_match():
    mb = FakeMailbox(messages=[_raw(text="nothing numeric here")])
    mb.address = "x@y.com"
    assert mb.wait_for_code(re.compile(r"(?<!\d)(\d{6})(?!\d)"), timeout=0.1, interval=0) is None


def test_list_requires_address():
    mb = FakeMailbox()
    with pytest.raises(MailboxError):
        mb.list_messages()
