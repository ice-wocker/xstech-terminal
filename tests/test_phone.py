"""离线单测：随机手机号生成。

这里最容易出的错不是「生成不出来」，而是**生成的号不合规**——
服务端会做格式校验，一眼假的号码会被拦，报错还可能被误读成
「手机号已被占用」。所以断言重点在「合规」和「不重复」。
"""

import random

import pytest

from xstech import phone


@pytest.fixture(autouse=True)
def _clean():
    phone.reset_used()
    yield
    phone.reset_used()


def test_generated_numbers_are_valid_cn_mobiles():
    rng = random.Random(42)
    for _ in range(200):
        n = phone.random_cn_mobile(rng)
        assert phone.is_valid_cn_mobile(n), n
        assert n[0] == "1" and n[1] in "3456789"


def test_no_duplicates_within_a_batch():
    # 老板明确要求：同一个手机号不能注册两次。批量里必须互不相同。
    numbers = [phone.random_cn_mobile() for _ in range(500)]
    assert len(set(numbers)) == len(numbers)


def test_dedup_survives_forced_collision():
    """把随机源钉死成「永远返回同一个号」，去重必须仍然生效。

    不这么测的话，随机 11 位数几千次里几乎不会撞，去重逻辑删掉也测不出来
    （实测：删掉 `_used` 检查，上面那个 500 个号的用例照样绿）。
    """
    class FixedRandom(random.Random):
        def choice(self, seq):
            return seq[0]

        def choices(self, seq, k=1):
            return [seq[0]] * k

    rng = FixedRandom(0)
    first = phone.random_cn_mobile(rng)
    second = phone.random_cn_mobile(rng)
    assert first != second, "撞号了还返回同一个，去重没生效"


def test_strict_uses_real_prefixes():
    rng = random.Random(1)
    prefixes = {phone.random_cn_mobile(rng)[:3] for _ in range(300)}
    assert prefixes <= set(phone._PREFIXES)


def test_non_strict_still_format_valid():
    rng = random.Random(2)
    for _ in range(100):
        n = phone.random_cn_mobile(rng, strict=False)
        assert phone.is_valid_cn_mobile(n), n


def test_is_valid_cn_mobile_rejects_bad_input():
    assert not phone.is_valid_cn_mobile("")
    assert not phone.is_valid_cn_mobile("12345")
    assert not phone.is_valid_cn_mobile("23800138000")  # 不以 1 开头
    assert not phone.is_valid_cn_mobile("12800138000")  # 第二位非法
    assert not phone.is_valid_cn_mobile("1380013800a")  # 有非数字
    assert phone.is_valid_cn_mobile("13800138000")
