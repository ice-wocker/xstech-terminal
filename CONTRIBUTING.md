# 贡献指南

## 开发环境

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## 几条约定

- **测试必须离线。** 单元测试不能打网络 —— 上游会变、会限流，
  让 CI 依赖真实站点等于把构建押在别人的稳定性上。需要真实上游的验证
  放脚本里手动跑，不要塞进 `pytest`。
- **协议差异写在 `upstream.py`，别漏到 `gateway.py`。** 前者只管把私有协议
  翻译成内部结构，后者只管输出 OpenAI 格式。混在一起以后上游一改就到处改。
- **滑块求解别改通道顺序。** `captcha.py` 里原图和裁剪块都解码成 BGR：
  有 `cv2` 时走 `cv2.imdecode`，没有时走 Pillow 再手工翻成 BGR + `float32`。
  两条路径**必须产出同样的通道顺序和 dtype**。混用 RGB 会让相关性从 0.9
  掉到 0.3，看起来「还能跑」但命中率腰斩。
- **OpenCV 是可选加速，不是依赖。** 不要在 `captcha.py` 顶层写 `import cv2`
  —— 之前就是这么写的，结果没装 OpenCV 的机器（Termux 默认环境）连 CLI
  都起不来。用 `HAS_CV2` 探测，需要时在函数内取。`_erode` 的两条路径
  （cv2 / numpy）必须逐像素一致，改了要跑 `tests/test_captcha.py` 里的比对用例。
- **流式过滤必须无状态。** `visible_prefix()` 每帧对完整缓冲重算，
  不要退回「用外部标志记住状态」的写法 —— 标签会被 SSE 从中间切开，
  有状态版本一旦某帧判断错就会一路错到底。

## 两套 CI 的分工

这个仓库同时有 GitHub Actions（`.github/workflows/ci.yml`）和 CNB 流水线（`.cnb.yml`），
**不是重复劳动**：

| 平台 | 跑什么 | 为什么 |
|---|---|---|
| GitHub Actions | 3.10 × 3.12，装/不装 OpenCV 两条腿 | 有 cv2 的加速路径要真的被跑到 |
| CNB | 默认环境就是无 OpenCV | 天然等于 Termux，顺手验证无 cv2 路径 |
| CNB | 合并后把改动推回 GitHub | CNB 是镜像侧，GitHub 是权威侧 |

要配的环境变量（**只存在 CNB 仓库设置里，不进仓库**）：

- `GITHUB_SYNC_TOKEN`：对 `ice-wocker/xstech-terminal` 有 `contents:write` 权限的
  fine-grained PAT。CNB 的 import 变量默认「仅 import 时可用」，这条流水线要用到，
  必须改成允许在流水线中读取，否则推送步骤会因变量为空而失败。
- `GITHUB_SYNC_REPO`（可选）：默认 `ice-wocker/xstech-terminal`。

方向是单向的 **CNB → GitHub**。CNB 侧是镜像，谁也别反向拉，否则两边互相覆盖。

## 手动验收「模型都能用」

CI 里**不允许**出现需要真实网络的步骤（有 `test_ci_has_no_network_steps` 盯着）。
验证端点可用性是手动活：

```bash
xstech-gateway register            # 或 login
python tools/verify_endpoint.py    # 逐模型实调，输出表格 + 汇总
python tools/verify_endpoint.py --json report.json
```

它会把失败分成两类：**账号额度类**（普通账号没有某些模型的额度，必然如此）
和**上游可用性类**（无渠道、上游自带组件版本过旧）。这两类都不是代码回归，
但需要人看一眼；只有第三类（端点真的跑不通）才说明代码坏了。

## 提交 PR

带上「为什么」：改了什么、怎么验证的。涉及验证码或协议的部分，
请贴出实测样本数（例如「30 次采样命中 27 次」），不要只说「已测试」。
