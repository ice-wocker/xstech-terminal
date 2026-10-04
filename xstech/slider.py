"""阿里云拼图滑块的「缺口定位」。

## 这里解决的问题

拿到的两张图是：

- **底图**（`back.png`）：整张场景图，其中**缺一块** —— 缺口被盖了一层
  半透明白色（实测不是纯白，会透出底下内容，所以按颜色找会失败）；
- **碎片图**（`shadow.png`）：拼图块的形状 + 该处的内容，四周透明。

要算的就是「缺口横向在哪」，因为拖动距离基本等于它。

## 走过的弯路（写下来免得重踩）

1. **按颜色找缺口**：缺口是一层半透明白，颜色会随底图内容变，且场景里
   本来就有雪/云/天空等亮区 → 误命中。失败。
2. **纯 SSD 模板匹配**：碎片内容与底图同源，理论上能对上；但缺口处已被
   白色覆盖，**原始内容不存在了**，所以匹配的是「碎片 vs 白化后的区域」，
   在平滑区（天空、雪）会匹配到错误位置。失败。
3. **拼图块轮廓做形状匹配**（当前方案）：缺口的外轮廓是硬的，把它当
   模板、在底图的「白化度」图上做归一化互相关，就能稳定命中。
   白化度 = `亮度 - k × 局部对比度` —— 半透明覆盖既**提亮**又**降对比**，
   这两个特征是缺口独有的。

同时利用一个先验：碎片图里不透明像素的 **y 范围**就是缺口的 y 范围。
只在那一带搜索，把干扰砍掉大半（天空、地面都不在那一条）。

> 说明：按 y 带裁剪是**优化**，不是正确性前提 —— 实测在 8 张真图上，
> 裁剪与不裁剪的结果最多差 1px。留着它是因为它把搜索空间砍掉一半以上，
> 而且把「诱饵区域」（远处同样发白的平滑区）排除在外，更稳。
"""

from __future__ import annotations

import base64

import numpy as np

try:
    from PIL import Image as _PILImage

    _IO_OK = True
except ImportError:  # pragma: no cover
    _IO_OK = False

# 拼图块拖动的缓动系数（实测拟合，见下面的说明）。
#
# 实测：鼠标位移 m 与拼图块视觉 left 的关系**不是线性**，是二次的：
#     piece_left = 0.0035503·m² + 0.0769222·m
# 拟合残差 < 0.001px（400 点），所以是精确关系而非近似。
# 想反解「让拼图块停在 target 处需要把鼠标拖多远」就得解这个二次方程。
_EASE_A = 3.5502976e-3
_EASE_B = 7.69222291e-2

# 白化度里对比度的权重。1.5~3 之间都稳定，取 2 折中。
_CONTRAST_WEIGHT = 2.0
_WINDOW = 7  # 局部对比度的窗口边长


def _decode_data_url(data_url: str) -> np.ndarray:
    """把 data URL（或 http(s) URL）取回并解成 RGBA 数组。

    注意：SDK 有时给的是 `data:image/png;base64,...`，有时给的是
    `https://static-captcha.aliyuncs.com/...`（刷新出来的题目走后者）。
    只按 data URL 解会撞上 `Incorrect padding` —— 这不是图片坏了，
    是把 URL 当成 base64 了。
    """
    import io

    if not _IO_OK:
        raise RuntimeError("缺口定位需要 Pillow：pip install pillow")

    if data_url.startswith("data:"):
        raw = base64.b64decode(data_url.split(",", 1)[-1])
    else:
        import requests

        resp = requests.get(data_url, timeout=20)
        resp.raise_for_status()
        raw = resp.content

    with _PILImage.open(io.BytesIO(raw)) as im:
        return np.asarray(im.convert("RGBA"))


def _whiteness(gray: np.ndarray) -> np.ndarray:
    """白化度图：半透明覆盖会「提亮 + 降局部对比」，两者相减即为特征。"""
    h, w = gray.shape
    pad = _WINDOW // 2
    padded = np.pad(gray, pad, mode="edge")
    # 用滑动窗口算局部标准差（纯 numpy，避免引入 scipy）
    win = np.lib.stride_tricks.sliding_window_view(padded, (_WINDOW, _WINDOW))
    local_std = win.std(axis=(2, 3))
    return np.clip(gray - _CONTRAST_WEIGHT * local_std, 0, 255)


def _erode(mask: np.ndarray, radius: int = 2) -> np.ndarray:
    """二值腐蚀（削掉碎片图的描边/抗锯齿，避免污染轮廓匹配）。"""
    out = mask.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            out &= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


def detect_gap(bg_data_url: str, pz_data_url: str, scale: float = 1.0) -> tuple[float, float]:
    """定位缺口。

    返回 `(gap_display_left, mouse_distance)`：

    - `gap_display_left`：缺口左边界在**显示坐标**下的位置（已乘 `scale`）；
    - `mouse_distance`：把拼图块拖到该处需要拖多少像素（已反解缓动）。

    两个值都按「拼图块内容左边缘对准缺口左边缘」来对齐。
    """
    bg = _decode_data_url(bg_data_url)
    pz = _decode_data_url(pz_data_url)

    alpha = pz[..., 3] > 127
    if not alpha.any():
        return 0.0, 0.0

    ys, xs = np.where(alpha)
    y0, y1 = int(ys.min()), int(ys.max())
    x_left = int(xs.min())

    # 只在碎片的 y 带里搜（缺口只可能在这条带上）
    gray = bg[..., :3].mean(axis=2)
    whiteness = _whiteness(gray)

    sil = _erode(alpha, 2)
    ys2, xs2 = np.where(sil)
    sy0, sy1, sx0, sx1 = int(ys2.min()), int(ys2.max()), int(xs2.min()), int(xs2.max())
    band = whiteness[sy0:sy1 + 1]
    template = sil[sy0:sy1 + 1, sx0:sx1 + 1].astype(float)
    template = template - template.mean()
    th, tw = template.shape

    best_off, best_score = 0, -np.inf
    for off in range(0, max(1, whiteness.shape[1] - tw + 1)):
        window = band[:, off:off + tw]
        window = window - window.mean()
        denom = np.sqrt((window * window).sum() * (template * template).sum())
        score = (window * template).sum() / denom if denom else -np.inf
        if score > best_score:
            best_score, best_off = score, off

    # best_off 是「腐蚀后轮廓」左边缘在底图中的位置；真实缺口左边缘往左
    # 偏一个腐蚀半径 + 碎片内部偏移。
    gap_natural = best_off + (x_left - sx0)
    gap_display = gap_natural * scale

    # 拼图块内容左边缘在自身坐标系里是 x_left（碎片图未缩放，1:1 显示）
    piece_target = gap_display - x_left
    mouse_distance = mouse_for_piece(max(0.0, piece_target))
    return float(gap_display), float(mouse_distance)


def mouse_for_piece(target: float) -> float:
    """反解缓动：`piece_left = A·m² + B·m`，求 m。"""
    if target <= 0:
        return 0.0
    disc = _EASE_B * _EASE_B + 4 * _EASE_A * target
    return (-_EASE_B + disc ** 0.5) / (2 * _EASE_A)
