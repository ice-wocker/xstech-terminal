# xstech-gateway

把 [xstech.one](https://xstech.one) 的网页端能力包装成一个 **OpenAI 兼容端点**，
让你能在终端、IDE 和各种本地工具里直接用它。

```bash
pip install -e .
xstech-gateway register          # 自动注册一个账号（含滑块验证码求解）
xstech-gateway serve             # 起一个本地端点
```

然后在任何支持 OpenAI 协议的地方填上 `http://127.0.0.1:8787/v1` 就行了。

---

## 它解决什么问题

xstech.one 是个网页聊天站，能力不错，但只能在浏览器里用：

- 没有公开 API，没法接进 `aichat`、`llm`、Continue、Cursor 这些工具
- 想用就得开网页，脚本化、批处理都做不了
- 网页前端走的是它自己的一套私有协议，不是 OpenAI 协议

这个项目做了三件事：

1. **摸清私有协议** —— 模型清单、鉴权、SSE 流式对话的帧格式
2. **翻译成 OpenAI 协议** —— `/v1/models` 和 `/v1/chat/completions`，流式非流式都支持
3. **把注册也自动化** —— 连滑块验证码的火柴人都替你划了

结果就是你不需要改动任何现有工具，只要换个 `base_url`。

---

## 安装

需要 Python 3.10+：

```bash
git clone <this-repo>
cd xstech-terminal
pip install -e .
```

依赖只有三个：`requests`、`pillow`、`numpy`（见 `pyproject.toml`）。

**OpenCV 是可选的，不用装。** 滑块求解里的形态学腐蚀在装了 `cv2` 时会走
OpenCV 加速，没装则退回等价的纯 numpy 实现（两条路径已比对过逐像素一致）。
想用加速版：

```bash
pip install -e ".[captcha-cv2]"
```

> 说明：旧版本在 `captcha.py` 顶层写死了 `import cv2`，导致没装 OpenCV 的机器
> （比如 Termux 默认环境）连 `xstech-gateway register` 都起不来，只给一段
> `ModuleNotFoundError`。这个问题在 v0.1.1 修复，现在缺依赖时会给可操作的提示。

---

## 用法

### 1. 注册

```bash
xstech-gateway register
```

它会：申请一个临时邮箱 → 解滑块验证码 → 收邮件取注册码 → 完成注册 →
把凭据存到 `~/.config/xstech-gateway/credentials.json`（权限 `0600`）。

想用自己的邮箱：

```bash
xstech-gateway --email you@example.com --password 'your-password' login
```

### 2. 看看有哪些模型

```bash
xstech-gateway models
```

模型 id 形如 `openai::gpt-5.6-terra`，`::` 前面那段是上游分组，
后面的才是真实模型名。**传 id 时要连分组一起传**，这是站内协议的一部分。

### 3. 起端点

```bash
xstech-gateway serve --port 8787 --model 'deepseek::deepseek-v4-flash'
```

```
XSTECH Terminal Gateway 已启动 → http://127.0.0.1:8787/v1
```

想加个本地口令（防止同机器上别的进程白嫖）：

```bash
xstech-gateway serve --api-key sk-local-123
```

### 4. 用起来

**curl**

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek::deepseek-v4-flash","messages":[{"role":"user","content":"你好"}]}'
```

**openai-python**

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="not-needed")
r = client.chat.completions.create(
    model="deepseek::deepseek-v4-flash",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

**不想起服务，就想问一句**

```bash
xstech-gateway ask "用一句话解释什么是闭包"
```

**aichat / llm 之类的 CLI**

```bash
aichat --api-url http://127.0.0.1:8787/v1 --model 'deepseek::deepseek-v4-flash'
```

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `register` | 用临时邮箱自动注册 |
| `login`（配合 `--email/--password`） | 用已有账号登录并缓存 token |
| `models` | 列出全部可用模型 |
| `serve` | 启动 OpenAI 兼容端点 |
| `ask <文本>` | 不起服务，直接问一句 |
| `whoami` | 查看当前账号 |
| `logout` | 清除本地凭据 |

通用参数：`--base`（默认 `https://xstech.one`）、`--model`。

---

## 它是怎么工作的

### 私有协议

站点前端是 GoAmzAI Plus 3.4.0，协议和 OpenAI 不一样：

- 响应统一包一层：`{"code": 0, "data": ..., "msg": ""}`
- 鉴权走 `Authorization: <token>`（注意：**不加 `Bearer` 前缀**）
- 模型清单在 `GET /api/chat/tmpl`
- 流式对话在 `POST /api/chat/completions`，返回 `text/event-stream`，
  每帧再套一层 `{"code":0,"data":"<增量文本>"}`

这个项目替你把上面这些差异都吸收掉了。

### 滑块验证码

注册要过滑块。这里没有用打码平台，也没训练模型 —— 因为站点把
「形状裁剪块」单独返回，而**块内的像素直接取自同一张原图**。

于是问题从「识别形状」变成「模板匹配」：把裁剪块当模板，在原图**相同的 Y 坐标**上
按横向扫描，归一化相关最强的位置就是答案。

两个调参要点（都写在 `src/captcha.py` 的注释里）：

- 原图和裁剪块**必须用同一套通道顺序**。混用 RGB/BGR 会让相关性从 0.9 掉到 0.3
- 裁剪块外圈有描边和抗锯齿，先用 5×5 腐蚀削掉，否则信号被稀释

单次命中率实测 **90%**（30 次采样）。因为每个题目 id **只允许提交一次**，
失败即作废，所以对置信度低于 0.6 的题（通常是匀色天空背景，相关峰很钝）
直接弃掉换一道，而不是硬交。

上面两个调参点里，「5×5 腐蚀」这步**不依赖 OpenCV**：没有 `cv2` 时用纯 numpy
做同样的 5×5 全 1 结构元腐蚀，且刻意对齐 OpenCV 的边界语义（结构元在边界处
只取界内重叠部分，不做零填充）。两条路径在 5000 组随机掩码（尺寸 3×3 ~ 60×60）
上逐像素一致，所以**装不装 OpenCV 命中率相同**。

### 无状态

每次请求都会新开一个上游 session，服务端不保存任何对话状态。
上下文完全由客户端每次重发历史消息决定 —— 这和 OpenAI 的行为一致，
所以任何客户端都不会有意外的「串话」。

---

## 兼容性

已验证：

| 客户端 | 结果 |
|---|---|
| `curl` | ✅ 流式 / 非流式 |
| `openai` Python SDK | ✅ `models.list()`、`chat.completions.create()`（含 `stream=True`） |
| 本站 `ask` / `serve` | ✅ |
| 多轮对话 | ✅ 历史消息正确透传 |

思考块：上游把推理过程包在 `<think>...</think>` 里返回。本项目在交付前会剥掉它，
并且处理了标签被 SSE 分片**从中间切开**的情况（`<thi` + `nk>`），
不会把标签本身漏给客户端。

---

## 边界与风险

说清楚比较好：

- **这是对私有协议的逆向封装，不是官方 API。** 站点改前端就可能失效，
  上游也可能随时调整或封禁这类用法。
- **账号要自己负责。** 自动化注册通常违反站点服务条款。`register` 命令
  是为了让你快速跑通，正式使用建议自己注册账号。
- **凭据等同账号权限。** `credentials.json` 权限是 `0600`，别提交进 git，
  也别贴进聊天记录。
- **不要拿它做大批量调用。** 免费额度是有限的，滥用会连带你自己的账号一起没。
- **不保证可用性。** 上游限流、改协议、封 IP，本项目都无能为力。

换句话说：这是个**工具**，不是**服务**。自己用没问题，
拿去搭公开服务请先想清楚上面这些。

---

## 开发

```bash
pip install -e ".[dev]"
pytest -q
```

测试全部离线（`tests/test_gateway.py` 用假上游），不依赖网络。

```
src/
  captcha.py   滑块求解（相关性模板匹配，OpenCV 可选）
  upstream.py  私有协议客户端
  gateway.py   翻译成 OpenAI 协议
  server.py    HTTP 服务（标准库，零额外依赖）
  account.py   凭据保管
  cli.py       命令行入口
```

---

## License

MIT
