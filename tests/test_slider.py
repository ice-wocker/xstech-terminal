"""离线单测：缺口定位与缓动反解。

缺口定位的输入是两张真实图（底图 + 碎片图）。这里**不联网**，
用程序合成一对：在一张纹理背景上「抠掉」一个形状，生成底图与碎片图，
从而知道真实答案，能断言定位是否准确。
"""

import base64
import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from xstech import slider


def _data_url(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _make_pair(seed=0, gap_x=180, piece_y=80, size=50):
    """合成一对图：背景 + 从 gap_x 处抠下来的碎片。"""
    rng = np.random.default_rng(seed)
    h, w = 200, 296
    # 有纹理的背景（含平滑区和噪声区，模拟真实场景）
    yy, xx = np.mgrid[0:h, 0:w]
    bg = np.clip(rng.integers(30, 220, (h, w, 3)) + (xx // 8)[..., None], 0, 255)
    shape = Image.new("L", (size, size), 0)
    ImageDraw.Draw(shape).ellipse((4, 4, size - 4, size - 4), fill=255)
    mask = np.asarray(shape) > 0

    # 碎片图：整幅透明，只在 (piece_y, 2) 处放抠下来的内容。
    # 宽度做成 size+2，这样内容从 x=2 开始（对齐真实碎片图的边距）。
    pz = np.zeros((h, size + 2, 4), np.uint8)
    patch = bg[piece_y:piece_y + size, gap_x:gap_x + size]
    content = np.dstack([patch, np.full((size, size), 255, np.uint8)])
    sub = pz[piece_y:piece_y + size, 2:2 + size]
    sub[mask] = content[mask]
    pz[piece_y:piece_y + size, 2:2 + size] = sub

    # 底图：把缺口处「涂白」（模拟半透明覆盖）
    bg2 = bg.copy()
    region = bg2[piece_y:piece_y + size, gap_x:gap_x + size]
    region[mask] = (region[mask] * 0.4 + 255 * 0.6).astype(np.uint8)

    return _data_url(bg2), _data_url(pz), gap_x


@pytest.mark.parametrize("seed,gap_x", [(0, 180), (1, 60), (2, 240), (3, 120)])
def test_detect_gap_finds_known_position(seed, gap_x):
    bg_url, pz_url, truth = _make_pair(seed, gap_x)
    gap, distance = slider.detect_gap(bg_url, pz_url)
    assert abs(gap - truth) <= 6, f"期望 ~{truth}，实际 {gap}"
    assert distance > 0


def test_mouse_for_piece_inverts_easing():
    # 缓动是二次的，反解必须精确闭环
    for m in (0, 10, 50, 100, 200, 260):
        piece = slider._EASE_A * m * m + slider._EASE_B * m
        assert abs(slider.mouse_for_piece(piece) - m) < 1e-6


def test_mouse_for_piece_monotonic():
    prev = -1
    for target in range(0, 260, 10):
        cur = slider.mouse_for_piece(target)
        assert cur > prev
        prev = cur


def test_detect_gap_accepts_http_url():
    """SDK 刷新题目时给的是 http 地址而不是 data URL。

    只按 data URL 解会撞 `binascii.Error: Incorrect padding` ——
    这不是图坏了，是把 URL 当 base64 了。这里断言 URL 分支能走通。
    """
    bg_url, pz_url, truth = _make_pair(0, 180)
    caught = {}

    class FakeResp:
        def __init__(self, payload):
            self.content = payload

        def raise_for_status(self):
            return None

    raw_bg = base64.b64decode(bg_url.split(",", 1)[1])
    raw_pz = base64.b64decode(pz_url.split(",", 1)[1])

    import requests

    def fake_get(url, timeout=20):
        caught[url] = True
        return FakeResp(raw_pz if "pz" in url else raw_bg)

    orig = requests.get
    requests.get = fake_get
    try:
        gap, _ = slider.detect_gap("http://x/bg.png", "http://x/pz.png")
    finally:
        requests.get = orig
    assert abs(gap - truth) <= 6
    assert caught, "应该走 HTTP 分支"


def test_detect_gap_tolerates_empty_piece():
    # 碎片图全透明（异常输入）不该崩，返回 0
    bg = np.zeros((200, 296, 3), np.uint8)
    pz = np.zeros((200, 52, 4), np.uint8)
    gap, distance = slider.detect_gap(_data_url(bg), _data_url(pz))
    assert gap == 0 and distance == 0


def test_decode_data_url_rejects_garbage():
    arr = slider._decode_data_url(_data_url(np.zeros((4, 4, 3), np.uint8)))
    assert arr.shape[2] == 4
