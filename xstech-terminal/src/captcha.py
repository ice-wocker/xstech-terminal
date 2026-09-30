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
"""

from __future__ import annotations

import base64

import cv2
import numpy as np

_ERODE_KERNEL = 5
_MIN_STD = 1e-3


def _decode(data_url: str) -> np.ndarray:
    """把 data:image/... 解成 BGR float32 数组（与 OpenCV 默认一致）。"""
    payload = data_url.split(",", 1)[-1]
    raw = np.frombuffer(base64.b64decode(payload), np.uint8)
    img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("验证码图片解码失败")
    return img.astype(np.float32)


def _shape_mask(thumb: np.ndarray) -> np.ndarray:
    """形状区域掩码：滤掉纯黑背景与边缘描边。"""
    mask = (thumb.sum(axis=2) > 20).astype(np.float32)
    return cv2.erode(mask, np.ones((_ERODE_KERNEL, _ERODE_KERNEL), np.float32))


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
