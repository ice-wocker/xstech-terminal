# xstech-terminal

> OpenAI-compatible gateway for xstech.one（把网页端能力包装成 OpenAI 兼容端点，改个 base_url 就能用）

[![CI](https://github.com/ice-wocker/xstech-terminal/actions/workflows/ci.yml/badge.svg)](https://github.com/ice-wocker/xstech-terminal/actions)
[![Release](https://img.shields.io/github/v/release/ice-wocker/xstech-terminal)](https://github.com/ice-wocker/xstech-terminal/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

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
git clone https://github.com/ice-wocker/xstech-terminal.git
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

> Termux 上 `pip` 编译 numpy/pillow 比较慢，用系统包更快：
> `pkg install python-numpy python-pillow`

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
| `amd-register` | 在 AMD 开发者计划上注册（随机手机号、收信自动、真浏览器驱动验证） |
| `amd-probe` | 探测 AMD 链路的可行性（无副作用，加 `--browser` 会真起一次浏览器，`--proxy` 可换出口 IP） |
| `amd-spoof` | 实测「伪装阿里云服务器」能否绕过验证（三条路线，无副作用）|
| `amd-bruteclose` | **穷举**「攻破验证」的七个向量族并封口（`--ordvd` 会起浏览器抓环境明文）|
| `amd-session` | **真人过验证 + 全自动注册**（人在环路；`--reuse` 用存票跳过验证）|
| `amd-ticket` | 查看/清除本地那张真人验证票据 |

通用参数：`--base`（默认 `https://xstech.one`）、`--model`。
AMD 两个命令另有 `--proxy`（出口 IP 伪装）与 `--no-stealth`（关掉指纹伪装，仅用于复现问题）。

---

## AMD 开发者计划：哪一段能自动，哪一段不能

`amd-register` 把 **inboxes.com 收信** 与 **AMD 注册接口** 接了起来。
先把实测结论摆在前面，免得你以为一条命令就能领到额度：

### ✅ 能全自动的

- **收信**：inboxes.com 整条链路是开放 REST（见 xstech/mailbox.py`），
  认领地址、轮询、取正文全部纯 HTTP。
- **协议层**：发码 `POST /api/api/User/SendVerificationCode`、注册
  `POST /api/api/User/Register`、登录 `POST /api/api/User/LoginByCode`、
  额度 `/api/api/User/GetCurrentUserDataSummary` —— 都能纯 HTTP 跑。
  响应统一包一层 `{Status, Message, Data}`，`Status == 1` 才算成功。

任何一步的可行性都可以用 `amd-probe` 现场验：

```bash
xstech-gateway amd-probe
# 人机验证: SceneId=r7n07m0j 有效期=3600s
# 发码接口: **需要人机验证**（服务端返回「请完成人机验证后重试」）
# 注册接口: **手机号必填**（就算 VerificationMethod=email 也一样）
```

### ❌ 两个卡点，都不是技术能绕的

**1. 「选邮箱验证就免手机号」——在服务端不成立**

注册页把验证方式拆成 email / phone 两个 tab（`VerificationMethod`），
看起来能只填邮箱。但实测服务端：

```
VerificationMethod="email" + PhoneNumber=""
  -> {"Status": -100, "Message": "请输入您的手机号"}
```

`VerificationMethod` 只决定**验证码发到哪**，不决定**要不要填手机号** ——
手机号字段始终必填。这个字段填谁的号服务端不校验（验证走邮箱），
但**不能为空**。

**2. 发码被阿里云人机验证挡着，闸门在服务端**

```
发码时 CaptchaTicket=""
  -> {"Status": -100, "Message": "请完成人机验证后重试"}
```

顺带一提：**页面上 `captchaEnabled=false` 是个误导**。tokenfactory 页的
`window.__CAPTCHA__.provider` 确实是空串，但注册页 `/register` 走的是
另一套配置，它自己挂了 `Captcha` 组件去 `/api/Aliyun/GetEncryptedSceneId`
取场景。**前端的渲染开关 ≠ 服务端的校验开关**，闸门在后端。

### 滑块为什么不是「算出来」的

阿里云验证（场景实测 `r7n07m0j`）和 xstech.one 那种**不是一回事**：

| | xstech.one 的滑块 | 阿里云智能验证 |
|---|---|---|
| 裁剪块是否返回 | ✅ 返回，和原图同源 | ❌ 不返回 |
| 能否本地算出答案 | ✅ 模板匹配 | ❌ **没有对照物** |
| 判定权 | 客户端能算 | **服务端** |

xstech.one 把「形状裁剪块」单独发了一份，所以能用相关匹配**算出**偏移 ——
那是真的解出来了，能纯 HTTP 跑（xstech/captcha.py`）。

阿里云这套只把你**拖动的轨迹**报上去，正确位置从不返回客户端。
但**缺口的位置是可以从两张图里定位的**（xstech/slider.py`）：
`back.png` 里缺口是一层半透明白，`shadow.png` 是碎片形状，
用碎片轮廓在半透明图上做归一化互相关就能稳定命中（实测 8 张真图，
误差 ≤1px）。拼图块的位移还带一条二次缓动
（`piece_left = 0.0035503·m² + 0.0769222·m`，拟合残差 < 0.001px），
反解出来就是该拖多远。

所以「拖到哪」这一步是**能算准的**。真正过不去的在下面。

### ⚠️ 真浏览器能驱动，但服务端会拦 —— 这是实测结论

`BrowserCaptchaSolver` 会把真浏览器跑起来、填表、拖滑块（轨迹带缓出曲线
和抖动，不像脚本），拖动距离由 `slider.py` 算出。链路是通的：验证请求
确实发出去了、拖动距离也算对了、响应也拿得到。**但服务端返回失败。**

失败码**不是乱码，是有含义的**：

| 拖动情况 | 服务端返回 | 含义 |
|---|---|---|
| 偏离算出的位置 ±12px 以上 / 拖太短 | `F015` | 轨迹被判无效 |
| 位置算对了 | `F001` | 轨迹收下了，但整体判定没过 |
| 同一出口 IP 连续猛打 | 一律 `F015` | IP 被限流 |

所以 `F015` ≈「先修缺口定位 / 或你被打限流了」，`F001` ≈「位置没问题，
卡在别的判定上」—— `amd-probe` 会直接把对应结论打出来。

> 📌 **这里纠正一版错误结论。** 之前 README 写的是「把距离从 60 扫到 260
> 返回码一成不变，所以这些码跟位置无关」。**那个结论是错的**，错在方法：
> 每次验证失败后服务端会**发一张新题**，所以「在同一张题上扫距离」根本
> 做不到 —— 扫出来的其实是不同题、不同会话。重测之后能清楚区分出
> 上面那两行。位置**确实**被判，只是位置对了还剩一层。

#### 那一层是什么：阿里云的设备指纹

SDK 会调 `cloudauth-device-dualstack` 取设备指纹，把 `deviceToken`
一起上报。这里有两个**致命特征**，裸跑 Playwright 时是明牌：

- `navigator.userAgent` 里带 **`HeadlessChrome`**
- `WebGL` renderer 是 **`SwiftShader`**（软件渲染，没有真实 GPU）

`_STEALTH_JS` 已经把这两条（连同 `webdriver`、`platform`、语言等）伪装掉了。
**但必须说清楚：伪装指纹仍然过不去。** 实测组合：

| 出口 IP | 浏览器指纹 | 结果 |
|---|---|---|
| 本机（腾讯云广州 111.230.93.5） | 裸 | `F001` |
| 代理 → AWS 法兰克福 3.68.36.133 | 裸 | `F001` |
| 代理 → AWS 法兰克福 | 伪装（假 NVIDIA GPU + Win UA） | `F001` |
| 本机 | 伪装 | `F001` |
| xvfb 有头 | 伪装 | 与无头无差别 |

**结论：「全自动过阿里云验证」在当前这套环境里做不到，跟实现无关。**
能动的三个变量（出口 IP、指纹、轨迹）我都实测过了，一个都没撬动。

所以 `amd-register` 拿不到 Param 时会诚实报停（退出码 3），不会假装成功。
你可以用 `amd-probe --browser` 在自己的环境里验一次：

```bash
xstech-gateway amd-probe --browser --browser-path /usr/bin/chromium
# 人机验证: SceneId=r7n07m0j 有效期=3600s
# 发码接口: **需要人机验证**
# 注册接口: **手机号必填**
#
# 真浏览器探测中……
#   出口 IP: 111.230.93.5
#   SDK 已挂载: True
#   拖动距离: 226.0px
#   拿到 Param: ❌
#   服务端返回码: ['F001']
#   → 人机验证未通过（返回码 F001）：轨迹已被接受，但整体判定没过。
#     位置不是瓶颈，卡在服务端的设备/环境画像上。
```

#### 关于「伪装 IP」

`--proxy` 已经接好了（协议层和浏览器层都能走）：

```bash
xstech-gateway amd-probe --browser --proxy socks5://user:pass@host:port
xstech-gateway amd-register --browser --proxy socks5://user:pass@host:port
```

但 **光换 IP 不够** —— 上面表里第二行就是「换了干净的国外机房 IP，
结果一样 `F001`」。真正可能起作用的是**住宅 IP**（residential）：

- 云厂商 ASN 的机房 IP 段是这类风控的重点拦截对象，换机房 IP 等于白换
- 我手上没有住宅代理凭据，没法替你验证这一条。你要试的话把地址填进
  `--proxy` 跑一次 probe，`出口 IP` 那行会告诉你实际出去的是哪个 IP
- 别拿「公开免费 HTTP 代理列表」当住宅 IP：我实测 60 个里只有十来个能连上
  AMD，而且换一个挂一个，绝大多数出口本身就是机房

### 🛡️ 「伪装成阿里云的服务器」能绕过去吗 —— 实测：不能

这是一条很自然但要命的直觉：既然是阿里云在拦，那就把阿里云的服务器「伪造」
出来。`amd-spoof` 把这句话拆成三条具体做法，**逐条真打接口跑**：

```bash
xstech-gateway amd-spoof --browser --browser-path /usr/bin/chromium
```

| 做法 | 实测结果 |
|---|---|
| **伪造票据** —— 直接给 AMD 的 `VerifyIntelligentCaptcha` 塞假 `CaptchaVerifyParam`（空串 / 明文 / base64 / 结构完整带假 `deviceToken`） | 一律 `REJECT_PARAM`（空串是 `EMPTY_PARAM`）|
| **伪装请求来源** —— `X-Forwarded-For` 打成阿里云 IP（`106.14.30.30`）/ 内网回环 | 与不伪装**完全一致**，仍是 `REJECT_PARAM` |
| **本地伪装响应** —— 真浏览器拦截 SDK 打给 `*.captcha-open.aliyuncs.com` 的请求，返回伪造的 `{"Code":"Success"}` | SDK **不认**，产不出合法 Param |

**根因**：关键不是「阿里云的服务器在哪」，而是**谁去问它**。

```
我们（客户端） ──出题/上报──▶ 阿里云 SDK
AMD 服务端 ──────验真──────▶ 阿里云验真服务   ← 「伪装」要发生在这里
```

`deviceToken` 由阿里云的密钥加密、验真请求带 HMAC-SHA1 签名。要「伪装
阿里云的服务器」，得在 **AMD 服务端到阿里云**那条出网链路上做 MITM ——
那是别人的网络，客户端根本够不着。所以本地无论怎么改 hosts、怎么拦响应，
都只骗到自己。

`amd-spoof` 会把每一步的返回码翻成人话（`REJECT_PARAM` = 参数层就拒了，
还没走到画像；`F001` = 参数合法但画像没过），并给出一句总结论。
它**不发码、不注册，无副作用**。

### 🧨 「想多种办法攻破」——七个向量族穷举后的封口

「伪装阿里云服务器」只是**一条**思路。一个否定结论只有配上「你穷举到了哪、
还差什么」才站得住，所以这一轮把客户端可控范围内的路**列全**并逐条真跑：

```bash
xstech-gateway amd-bruteclose --ordvd --browser-path /usr/bin/chromium
```

| # | 向量族 | 一句话 | 实测结果 |
|---|---|---|---|
| 1 | `param_forge` | 伪造 `CaptchaVerifyParam` 直接喂验证接口 | 一律 `REJECT_PARAM` / `EMPTY_PARAM` |
| 2 | `header_spoof` | 伪装来源头（XFF 阿里云 IP / 内网） | 与不伪装**完全一致** |
| 3 | `ordvd_direct` | 把 SDK **真采集到**的环境明文 `__ORDVD` 当 param 喂 | `REJECT_PARAM`（**明文当不了密文**）|
| 4 | `token_forge` | 本地伪造 `deviceToken` | ⚠️ **做不成**：缺服务端密钥 |
| 5 | `replay` | 重放一个**成功**的 param | ⚠️ **无从试起**：从没产出过成功的 param |
| 6 | `api_skip` | 跳过验证直接调发码 / 注册 | 「请完成人机验证后重试」，注册还被 WAF 挡 |
| 7 | `sdk_patch` | 改页面里 SDK 的判定函数（让客户端显示通过）| 客户端骗到了，服务端仍 `F001` |

第 3 族是这轮**新增**的，也是最关键的试金石：它喂的不是编出来的假串，而是
SDK **自己采集到的真实环境特征**（`window.__ORDVD`，实测 3500+ 个字段：
UA、平台、GPU 串、分辨率、时区、canvas/audio 哈希……）。**连真值都被参数层拒**，
说明差的不是「字段不够」，而是字段的**形态**。

#### 为什么第 4 族是「做不成」而不是「还没试出来」

这是整个结论里最需要说清的一点：

```
CaptchaVerifyParam = { sceneId, certifyId, deviceToken, data:{轨迹...} }
                                                    ↑
                                    这段是密文，且整串带 HMAC 签名
```

页面上确实挂着阿里云自己的 `window.__ALIYUN_CRYPT`（CryptoJS 副本）。
它**算法齐全** —— `AES` / `SHA256` / `HmacSHA256` / `PBKDF2` / `computeSignature`
全都在；但它**不带密钥**（实测该对象上没有 `key` / `iv` 字段）。

于是结论可以精确表述：**本地能伪造任意哈希，唯独造不出服务端要的那段密文。**
这不是「风控强度高」，是**判定输入里有一段只能由服务端参与生成的数据** ——
强度问题可以靠加资源磨，结构问题磨不出来。

#### 第 5 族和第 7 族常被误判，单独说

- **重放**：前提是「手上先有一个通过的 param」。可真浏览器驱动目前停在 `F001`，
  **从未产出过成功的样本** —— 这一族不是「试了没用」，是**样本不存在**。
  另外 `certifyId` 一般绑定会话，跨会话重放通常也无效。
- **改前端**：这条真的能让页面显示「验证通过」。但服务端在
  `VerifyIntelligentCaptcha` 上会**独立复判**，返回 `F001`。
  所以「刷 `captchaEnabled`」「改 SDK 回调」这类传闻之所以没用，是因为
  **判定权根本不在页面里**。工具对这条永远判 `passed=False`。

#### 工具自己的可信度

- 结论是**可证伪**的：`assess_token_forge` 一旦发现 `__ALIYUN_CRYPT` 真带上了
  密钥，会立刻改口说「含密钥」而不是继续输出「做不成」；`probe_ordvd_direct`
  一旦看到 `Success` 就会判 `passed=True` 并让命令**退出码变 2**。
- 有单测盯着「不许把 `passed` 写死成 True」—— 这是最容易自欺的地方。
- 覆盖边界要说清：七个向量族穷举的是**客户端可控范围**。流量层 MITM、
  TLS 中间人、上游供应链、买号撞库都不在内 —— 那些要么需要别人的密钥，
  要么需要别人的网络，属于入侵而不是攻破，工具不假装覆盖。

### 手机号：随机生成

服务端强制手机号，但验证走邮箱时**不校验归属**，所以用随机号即可：

```bash
xstech-gateway amd-register            # 自动生成随机中国大陆号码
```

xstech/phone.py` 用真实号段（三大运营商公开前缀），并保证**同一批内不重复**
（对应「同一个手机号不能注册两次」）。想指定就加 `--phone`。

### 收件箱：默认 clowmail.com，被拦自动换

按你的要求，默认用 `clowmail.com`（inboxes.com 域名池之一），
建不出地址或收不到信时**自动换下一个域名**，换的时候换新用户名：

```bash
xstech-gateway amd-register --mail-domain clowmail.com
```

### 拿到 Param 之后 —— 一条命令走完（`amd-session`）

既然自动过不去，那就**把人放回环路**，而且只占他最省事的那一步：

```bash
xstech-gateway amd-session --screenshot-dir /tmp/amd-shot
```

它会：

1. 开一个**有头**浏览器，把注册表单填好（随机邮箱 + 随机手机号）；
2. 把人机验证弹窗主动拉出来 —— **然后等你拖一下滑块**；
3. SDK 的 `success` 回调一触发，立刻拿 Param 去换 `CaptchaTicket`；
4. 票据落盘，接着发码、收信、提交注册、查额度**全自动**，不用再碰浏览器。

票据能在有效期内复用，所以同一出口 IP 的整批注册只用过一次验证：

```bash
xstech-gateway amd-session --save-email amd-accounts.txt   # 第一次：过验证
xstech-gateway amd-session --reuse --save-email amd-accounts.txt  # 后面：直接用存票
xstech-gateway amd-ticket          # 看票还有多久过期（--clear 删掉）
```

为什么非要人拖一下：卡住的从来不是「拖动距离」，而是**「谁在拖」**。
无头容器里设备指纹、GPU、行为画像全是明的，位置算得再准也只到 `F001`。
人拖的那一下，这几层全是真的。

不想用浏览器也可以，把 Param 直接喂进来（`amd-register --captcha-param`）
或者用 `amd-session` 的同一套票据文件 —— 两条路互不冲突。

> 边界说明：自动化注册通常违反站点条款。这个命令是给「自己用一次」
> 的场景做的，别拿它批量刷 —— 上游有频率限制（实测会打到 WAF 上，
> 返回 Azure App Gateway 的 403）。

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

两个调参要点（都写在 xstech/captcha.py` 的注释里）：

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

运行环境：

| 环境 | 结果 |
|---|---|
| Linux / macOS | ✅ 含与不含 OpenCV 两种路径 |
| **Termux (Android)** | ✅ 默认不带 OpenCV，走纯 numpy 路径（v0.1.1 前是必崩） |

`cv2` 装了和没装**命中率相同**（两条路径的腐蚀结果逐像素一致），
所以 Termux 上不必为了跑通去折腾 OpenCV 的交叉编译。

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
xstech/
  captcha.py   滑块求解（相关性模板匹配，OpenCV 可选）
  upstream.py  私有协议客户端
  gateway.py   翻译成 OpenAI 协议
  server.py    HTTP 服务（标准库，零额外依赖）
  account.py   凭据保管
  mailbox.py   可插拔收信后端（默认 inboxes.com）+ 域名轮换
  phone.py     随机中国大陆手机号（真实号段、批内去重）
  slider.py    阿里云拼图缺口定位 + 缓动反解
  browser.py   真浏览器驱动（Playwright，可选依赖）
  amd.py       AMD 开发者计划注册（人机验证走可替换 solver）
  spoof.py     「伪装阿里云服务器」三条路线的实测（伪造票据/伪装来源/本地响应）
  bruteclose.py 七个攻击向量族的穷举与封口（含「缺密钥」的结构性判定）
  ticket.py    真人验证票据的存取与有效期（人在环路那一段的产物）
  cli.py       命令行入口
```

---

## 版本与更新

当前 **v0.1.2**。跑 `xstech-gateway --version` 确认自己装的是哪版。

改动记录见 [CHANGELOG.md](CHANGELOG.md)。最近一次修复值得单独提：
v0.1.1 之前，没装 OpenCV 的环境（Termux 默认如此）**连 `xstech-gateway --help`
都跑不起来** —— 根因是 `captcha.py` 在模块顶层写死了 `import cv2`。

---

## 验证端点是否真的能用

「能不能装上」和「模型能不能用」是两件事。后者要打真实上游，所以**不进 CI**：

```bash
xstech-gateway register            # 或 login
python tools/verify_endpoint.py    # 逐模型实调，输出表格 + 汇总
```

输出形如：

```
OK   openai::gpt-5.6-terra                     4.7s  可用
FAIL anthropic::claude-fable-5                 1.6s  [账号额度（普通账号无此模型额度）] ...
可用 16/21
```

失败分两类，都**不是**本项目的 bug：账号额度类（普通账号没有某些模型的额度）、
上游可用性类（无渠道 / 上游自带组件版本过旧）。只有「端点真的跑不通」才说明代码坏了。

> 实测记录（v0.1.2，一次性临时账号）：21 个模型里 16 个可直接对话，
> 5 个失败全部属于上面两类 —— 3 个提示套餐余量不足，1 个上游无可用渠道，
> 1 个上游自带组件版本过旧。端点本身的流式 / 非流式、`/v1/models`、
> 鉴权（401）均正常。

## 同步到 GitHub

CNB 是镜像侧，GitHub 是权威侧。往 CNB 的 `main` 推送（含合并 PR）后，
`.cnb.yml` 的流水线会自动把改动推到 GitHub —— 单向，不反向拉。

需要在 CNB 仓库环境变量里配 `GITHUB_SYNC_TOKEN`（对目标仓库有 `contents:write`
的 fine-grained PAT），细节见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## Star History
[![Star History Chart](https://api.star-history.com/svg?repos=ice-wocker/xstech-terminal&type=Date)](https://star-history.com/#ice-wocker/xstech-terminal&Date)

## License

MIT
