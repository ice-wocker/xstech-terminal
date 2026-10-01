"""滑块求解的离线单测：不联网，也不需要装 OpenCV。

重点覆盖两件事：
1. **没装 cv2 也能用** —— 这曾经是「Termux 上敲 register 直接 traceback」的根因；
2. 两条腐蚀路径（cv2 / 纯 numpy 兜底）结果**逐像素一致** —— 否则「装没装
   OpenCV」会悄悄改变命中率，这是最难查的一类 bug。
"""

import base64
import builtins
import importlib
import io
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw


def _png_data_url(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _sample_captcha(seed: int = 42, thumb_y: int = 50, answer_x: int = 173):
    """造一张「背景有纹理 + 形状块取自同一原图」的验证码，结构对齐站点。"""
    rng = np.random.default_rng(seed)
    h, w = 120, 400
    yy, xx = np.mgrid[0:h, 0:w]
    bg = rng.integers(40, 200, (h, w, 3), dtype=np.uint8)
    bg = np.clip(bg.astype(int) + (xx // 3)[..., None] + (yy // 5)[..., None], 0, 255).astype(np.uint8)

    sh, sw = 40, 44
    thumb = np.zeros((sh, sw, 3), np.uint8)
    shape = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(shape).ellipse((6, 4, sw - 8, sh - 6), fill=255)
    m = np.asarray(shape) > 0
    thumb[m] = bg[thumb_y:thumb_y + sh, answer_x:answer_x + sw][m]
    return bg, thumb, thumb_y, answer_x


def _import_captcha_without_cv2(monkeypatch):
    """在「cv2 不存在」的环境里重新导入 src.captcha。"""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "cv2" or name.startswith("cv2."):
            raise ImportError("No module named 'cv2'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for mod in [m for m in sys.modules if m == "src.captcha" or m.startswith("src.captcha.")]:
        del sys.modules[mod]
    return importlib.import_module("src.captcha")


# ---------- 回归：没装 cv2 时不能再炸在 import ----------


def test_import_without_cv2_does_not_raise(monkeypatch):
    """曾经 `import cv2` 写在顶层，没装 OpenCV 的机器直接 ModuleNotFoundError。"""
    captcha = _import_captcha_without_cv2(monkeypatch)
    assert captcha.HAS_CV2 is False


def test_upstream_module_imports_without_cv2(monkeypatch):
    """端到端的关键：没 cv2 时 `register` 走的整条导入链都要能起来。"""
    _import_captcha_without_cv2(monkeypatch)
    for mod in [m for m in sys.modules if m == "src.upstream" or m.startswith("src.upstream.")]:
        del sys.modules[mod]
    upstream = importlib.import_module("src.upstream")
    assert hasattr(upstream.Upstream, "solve_challenge")


def test_solve_works_without_cv2(monkeypatch):
    """不装 OpenCV 也要能解出正确偏移。"""
    captcha = _import_captcha_without_cv2(monkeypatch)
    bg, thumb, ty, answer_x = _sample_captcha()
    x, score = captcha.solve_offset_with_confidence(
        _png_data_url(bg), _png_data_url(thumb), ty
    )
    assert x == answer_x
    assert score > 0.9


# ---------- 两条腐蚀路径必须逐像素一致 ----------


def _erode_both_ways(captcha_module, mask: np.ndarray, cv2_module):
    """返回 (cv2 结果, numpy 兜底结果)。"""
    saved = captcha_module._cv2
    try:
        captcha_module._cv2 = cv2_module
        a = captcha_module._erode(mask)
        captcha_module._cv2 = None
        b = captcha_module._erode(mask)
    finally:
        captcha_module._cv2 = saved
    return a, b


@pytest.mark.parametrize("shape", [(40, 44), (5, 5), (4, 4), (6, 9), (3, 20), (9, 3), (60, 60)])
def test_erode_fallback_matches_cv2(shape):
    cv2 = pytest.importorskip("cv2")
    from src import captcha

    rng = np.random.default_rng(7)
    mismatches = 0
    for _ in range(60):
        mask = (rng.random(shape) > 0.4).astype(np.float32)
        a, b = _erode_both_ways(captcha, mask, cv2)
        if not np.array_equal(a, b):
            mismatches += 1
    assert mismatches == 0, f"{shape} 上 cv2 与 numpy 兜底结果不一致"


def test_erode_all_true_mask_is_fully_kept():
    """全 1 的掩码经 5x5 腐蚀后不应被削（OpenCV 在边界裁结构元，不做零填充）。"""
    from src import captcha

    mask = np.ones((9, 9), np.float32)
    a, b = _erode_both_ways(captcha, mask, captcha._cv2)
    assert a.min() == 1.0
    assert np.array_equal(a, b)


def test_erode_removes_isolated_pixels():
    from src import captcha

    mask = np.zeros((15, 15), np.float32)
    mask[7, 7] = 1
    a, b = _erode_both_ways(captcha, mask, captcha._cv2)
    assert a.sum() == 0
    assert np.array_equal(a, b)


# ---------- 缺依赖时的报错必须可操作 ----------


def test_missing_pillow_raises_actionable_error(monkeypatch):
    """连 Pillow 都没有时，要给「装什么」的提示，而不是裸 traceback。"""
    captcha = _import_captcha_without_cv2(monkeypatch)
    monkeypatch.setattr(captcha, "_cv2", None)
    monkeypatch.setattr(captcha, "_PILImage", None)
    with pytest.raises(captcha.MissingImagingError) as exc:
        captcha.solve_offset_with_confidence(
            _png_data_url(np.zeros((10, 10, 3), np.uint8)),
            _png_data_url(np.zeros((4, 4, 3), np.uint8)),
            0,
        )
    assert "pip install" in str(exc.value)


def test_upstream_wraps_missing_dependency_as_upstream_error(monkeypatch):
    """solve_challenge 要把「缺依赖」翻译成可读提示，而不是让它冒成裸异常。"""
    from src import captcha as real_captcha
    from src import upstream

    def fake_challenge(self):
        return {"id": "x", "thumbY": 0, "image": "", "thumb": ""}

    def fake_solve(*a, **kw):
        raise real_captcha.MissingImagingError(
            "pip install numpy pillow\n（可选加速：pip install opencv-python-headless）"
        )

    monkeypatch.setattr(upstream.Upstream, "_challenge", fake_challenge)
    monkeypatch.setattr(real_captcha, "solve_offset_with_confidence", fake_solve)

    with pytest.raises(upstream.UpstreamError) as exc:
        upstream.Upstream().solve_challenge(attempts=1)
    assert "pip install" in str(exc.value)
