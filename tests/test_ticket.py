"""离线单测：真人验证票据的存取与有效期判定。

这一层必须离线钉死，因为它是**唯一一条真能走通的路**（`browser.py` 已实测
无头容器里全自动过不去）。票据的时间语义出错会有两个恶果：

- 判定过宽 → 拿一张早就过期的票去发码，报出来的错是「人机验证已过期」，
  用户以为是站点挂了；
- 判定过严 → 明明还能用却让人再过一次验证，白白浪费一张票。

所以「什么时候算过期」这件事要有测试，不能只靠 `time.time() < x` 一行随手写。
"""

import json
import os
import time

import pytest

from xstech import ticket as T


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "t.json")
    t = T.Ticket(ticket="PARAM-XYZ", email="a@b.com", phone="13800000000",
                 created_at=time.time(), ttl=120, scene_id="r7n07m0j")
    T.save_ticket(t, path)
    got = T.load_ticket(path)
    assert got is not None
    assert got.ticket == "PARAM-XYZ"
    assert got.email == "a@b.com"
    assert got.scene_id == "r7n07m0j"


def test_file_mode_is_0600(tmp_path):
    """票据等价于「刚过完人机验证的会话」，不能给同机器其他用户读。"""
    path = str(tmp_path / "t.json")
    T.save_ticket(T.Ticket(ticket="P", created_at=time.time()), path)
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_expiry_boundary():
    now = 1_000_000.0
    t = T.Ticket(ticket="P", created_at=now - 100, ttl=240)
    assert t.alive(now) is True
    assert t.alive(now + 139) is True
    # 正好到点就该算过期 —— 留着 0 秒余量没有意义，服务端那边也在倒计时
    assert t.alive(now + 140) is False
    assert t.alive(now + 10_000) is False


def test_empty_ticket_is_never_alive():
    """空票据不能因为「时间还没到」就被当成有效。"""
    t = T.Ticket(ticket="", created_at=time.time(), ttl=10_000)
    assert t.alive() is False
    assert "无票据" in t.describe()


def test_missing_file_returns_none(tmp_path):
    assert T.load_ticket(str(tmp_path / "nope.json")) is None


def test_corrupt_file_returns_none_not_raise(tmp_path):
    """磁盘上的票可能被截断/手改。这里要「当作没有」，不能把 CLI 炸掉。"""
    path = str(tmp_path / "bad.json")
    with open(path, "w") as fh:
        fh.write("{ this is not json")
    assert T.load_ticket(path) is None


def test_unknown_fields_are_ignored(tmp_path):
    """票据文件是跨版本共享的，旧版多出来的字段不能让新版读不了。"""
    path = str(tmp_path / "t.json")
    with open(path, "w") as fh:
        json.dump({"ticket": "P", "created_at": time.time(), "ttl": 60,
                   "some_future_field": 1}, fh)
    got = T.load_ticket(path)
    assert got is not None and got.ticket == "P"


def test_pick_ticket_prefers_explicit():
    """显式传入的 param 优先于存盘的票 —— 否则 `--captcha-param` 会被无声忽略。"""
    picked = T.pick_ticket("EXPLICIT")
    assert picked is not None and picked.ticket == "EXPLICIT"


def test_clear_is_idempotent(tmp_path):
    path = str(tmp_path / "t.json")
    assert T.clear_ticket(path) is False
    T.save_ticket(T.Ticket(ticket="P", created_at=time.time()), path)
    assert T.clear_ticket(path) is True
    assert T.clear_ticket(path) is False
