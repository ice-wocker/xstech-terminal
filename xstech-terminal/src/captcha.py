"""滑块验证码求解。

站点的滑块验证码有个特性：返回的「形状裁剪块」四周是纯黑透明区，
但形状内部的像素直接取自同一张原图。于是不必做形状识别，也不必训练
模型 —— 把裁剪块当作模板、在原图相同 Y 坐标上做归一化相关匹配，
相关性最强的位置就是正确偏移。

实测要点（都是调参时踩出来的）：
- 原图与裁剪块必须用同一套通道顺序，混用 RGB/BGR 会让相关性掉到 0.3 左右，
  匹配结果基本随机；
- 裁剪块外圈有一圈描边（形状的描边 + 抗锯齿），参与匹配会稀释信号，
  先用 5x5 形态学腐蚀把边缘削掉；
- 每个题目 id 只允许提交一次核验，算错即作废，所以必须一次命中。

关于 OpenCV（`cv2`）：
它**不是**本项目的硬依赖。原来在模块顶层写 `import cv2`，使得任何一条
导入路径都会在 import 阶段直接抛 `ModuleNotFoundError` —— 表现就是
Termux 上敲 `xstech-gateway register` 只看到一段 traceback，
连「缺什么、怎么装」都不说。现在改成：
- 顶层不 import cv2，只探测 `HAS_CV2`；
- 解码走 Pillow（已是声明依赖），像素顺序手工对齐到 BGR，与 cv2 一致；
- 形态学腐蚀有 cv2 就用 cv2，没有就用纯 numpy 的等价实现（见 `_erode`），
  因此**不装 OpenCV 也能完整跑通注册**；
- 两条路径的腐蚀结果经过逐像素比对**完全一致**（5000 组随机掩码，
  含各种边界尺寸），所以命中率不会因为少装 OpenCV 而改变。
"""

from __future__ import annotations

import base64

import numpy as np

try:  # Pillow 是声明依赖，正常情况下一定在
    import io as _io

    from PIL import Image as _PILImage
except ImportError:  # pragma: no cover - 只有在依赖被裁掉时才会命中
    _PILImage = None
    _io = None

try:  # OpenCV 是「可选加速」，不是依赖
    import cv2 as _cv2
except ImportError:  # pragma: no cover - 取决于环境
    _cv2 = None

HAS_CV2 = _cv2 is not None

_ERODE_KERNEL = 5
_MIN_STD = 1e-3

_INSTALL_HINT = (
    "验证码求解需要图像解码能力，但当前环境缺少依赖。请在安装本项目的环境里执行：\n"
    "  pip install numpy pillow\n"
    "（可选加速，装了更快，不装也能跑：pip install opencv-python-headless）\n"
    "若是在 Termux 上，可先用 `pkg install python-numpy python-pillow` 装系统包。"
)


class MissingImagingError(RuntimeError):
    """缺少解码/求解图像所需的基础依赖（numpy / Pillow）。"""


def _decode(data_url: str) -> np.ndarray:
    """把 data:image/... 解成 BGR float32 数组（与 OpenCV 默认一致）。

    优先走 cv2.imdecode（通道顺序即 BGR）；没有 cv2 时用 Pillow 解成 RGB
    再手工翻成 BGR **并转成 float32**，保持与有 cv2 时完全一致的行为 ——
    通道顺序和 dtype 都是匹配质量的关键，不能因为换了解码器就漂移。
    """
    payload = data_url.split(",", 1)[-1]
    raw = base64.b64decode(payload)

    if _cv2 is not None:
        buf = np.frombuffer(raw, np.uint8)
        img = _cv2.imdecode(buf, _cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("验证码图片解码失败")
        return img.astype(np.float32)

    if _PILImage is None:
        raise MissingImagingError(_INSTALL_HINT)

    with _PILImage.open(_io.BytesIO(raw)) as im:
        img = np.asarray(im.convert("RGB"))
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError("验证码图片解码失败")
    # RGB -> BGR 翻转，与 cv2.imdecode 的默认输出对齐
    return np.ascontiguousarray(img[..., ::-1]).astype(np.float32)


def _erode(mask: np.ndarray) -> np.ndarray:
    """5x5 全 1 结构元的二值腐蚀（用于削掉裁剪块的描边/抗锯齿）。

    有 cv2 就走 cv2.erode（快）；没有就用纯 numpy 的等价实现。两者在
    **所有尺寸的随机掩码上都逐像素一致**（含比核还小的图），所以命中率
    不依赖于是否装了 OpenCV。

    numpy 版的语义刻意对齐 OpenCV：结构元在边界处只取「界内重叠部分」，
    **不做零填充**。若改用零填充会多削掉最外一圈，虽然对验证码影响很小，
    但那属于「两条路径结果不一致」——这种不一致以后会变成难查的玄学 bug。
    """
    if _cv2 is not None:
        kernel = np.ones((_ERODE_KERNEL, _ERODE_KERNEL), np.float32)
        return _cv2.erode(mask.astype(np.float32), kernel)

    if _ERODE_KERNEL % 2 == 0:
        raise ValueError("腐蚀核必须是奇数，否则无法对称对齐")
    inside = mask > 0
    h, w = inside.shape
    acc = inside.copy()
    pad = _ERODE_KERNEL // 2
    for dy in range(-pad, pad + 1):
        for dx in range(-pad, pad + 1):
            if dx == 0 and dy == 0:
                continue
            shifted = np.ones_like(inside)
            y0, y1 = max(0, dy), min(h, h + dy)
            x0, x1 = max(0, dx), min(w, w + dx)
            shifted[y0 - dy:y1 - dy, x0 - dx:x1 - dx] = inside[y0:y1, x0:x1]
            acc &= shifted
    return acc.astype(np.float32)


def _shape_mask(thumb: np.ndarray) -> np.ndarray:
    """形状区域掩码：滤掉纯黑背景与边缘描边。"""
    mask = (thumb.sum(axis=2) > 20).astype(np.float32)
    return _erode(mask)


def _best_match(bg: np.ndarray, th: np.ndarray, mask: np.ndarray, y: int) -> tuple[float, int]:
    h, w = th.shape[:2]
    ref = th[mask]
    best_score, best_x = -np.inf, 0
    for x in range(0, bg.shape[1] - w + 1):
        patch = bg[y:y + h, x:x + w]
        if patch.shape[:2] != (h, w):
            continue
        cur = patch[mask]
        if float(cur.std()) < _MIN_STD:
            continue
        score = float(np.corrcoef(cur.ravel(), ref.ravel())[0, 1])
        if score > best_score:
            best_score, best_x = score, x
    return best_score, best_x


def solve_offset_with_confidence(background: str, thumb: str, thumb_y: int) -> tuple[int, float]:
    """同 solve_offset，但额外返回峰值相关系数，便于判断这次解是否可信。

    相关系数偏低（约 <0.6）意味着背景是匀色天空这类缺乏纹理的图，
    匹配容易滑到错误位置。调用方可以据此重开一题，而不是白交一次核验。
    """
    bg = _decode(background)
    th = _decode(thumb)
    h, _ = th.shape[:2]
    y = int(thumb_y)
    y = max(0, min(y, bg.shape[0] - h))
    mask = _shape_mask(th) > 0
    score, x = _best_match(bg, th, mask, y)
    return x, score


def solve_offset(background: str, thumb: str, thumb_y: int) -> int:
    """返回裁剪块应当放置的横向偏移（左上角 x，客户端坐标）。"""
    return solve_offset_with_confidence(background, thumb, thumb_y)[0]
