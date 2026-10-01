"""把 cv2 从导入系统里彻底藏掉，验证「没装 OpenCV」这条路径真的可用。

这是真出过的故障：`captcha.py` 在模块顶层写死 `import cv2`，
于是 Termux 上连 `xstech-gateway --help` 都跑不起来（裸 ModuleNotFoundError）。

只「不安装 cv2」还不够 —— 有的环境里它可能被别的包带进来。所以这里直接
hook `builtins.__import__` 把它藏掉，再跑一遍导入与一道真实的滑块求解。

原先这段脚本内联在 workflow 的 heredoc 里，YAML 缩进会破坏结束符，
现在落到文件，语法与逻辑都能被常规测试覆盖。
"""

from __future__ import annotations

import base64
import builtins
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image, ImageDraw

_real_import = builtins.__import__


def _fake_import(name, *args, **kwargs):
    if name == "cv2" or name.startswith("cv2."):
        raise ImportError("No module named 'cv2'")
    return _real_import(name, *args, **kwargs)


builtins.__import__ = _fake_import

from src import captcha, upstream  # noqa: E402  （必须在 hook 之后导入）

assert captcha.HAS_CV2 is False, "应该探测到没有 cv2"
assert hasattr(upstream.Upstream, "solve_challenge")


def _data_url(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def main() -> int:
    print("无 cv2 环境下，captcha / upstream 均可正常导入")

    rng = np.random.default_rng(0)
    h, w = 100, 300
    _, xx = np.mgrid[0:h, 0:w]
    bg = np.clip(
        rng.integers(40, 200, (h, w, 3)) + (xx // 3)[..., None], 0, 255
    ).astype(np.uint8)
    sh, sw, ty, tx = 40, 44, 40, 171

    thumb = np.zeros((sh, sw, 3), np.uint8)
    shape = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(shape).ellipse((6, 4, sw - 8, sh - 6), fill=255)
    mask = np.asarray(shape) > 0
    thumb[mask] = bg[ty:ty + sh, tx:tx + sw][mask]

    x, score = captcha.solve_offset_with_confidence(_data_url(bg), _data_url(thumb), ty)
    assert x == tx, f"期望 x={tx}，实际 x={x}"
    assert score > 0.9, f"置信度偏低: {score}"
    print(f"无 cv2 求解正确: x={x} 置信度={score:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
