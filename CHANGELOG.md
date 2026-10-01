# 更新日志

版本号跟 `pyproject.toml` 和 `src/__init__.py` 同步。判断自己装的是不是修过的那版，
跑 `xstech-gateway --version`。

## v0.1.2

### 新增：CNB 侧推送自动同步到 GitHub

以后往 CNB 的 `main` 推送（含合并 PR），流水线会自动把改动推到 GitHub 权威仓库
`ice-wocker/xstech-terminal`。方向是**单向 CNB → GitHub**，绝不反向拉。

- `.cnb.yml` 里两条流水线：一条跑测试，一条做同步
- 同步用 `--force-with-lease`，不会悄悄覆盖别人新推的提交
- token 走环境变量 `GITHUB_SYNC_TOKEN`，**不落仓库**；缺配置时明确失败，
  不静默跳过（静默跳过等于「以为配好了，其实从来没推过」）

### 新增：全模型可用性验证脚本

`tools/verify_endpoint.py` —— 逐模型实调，打印表格与汇总，支持 `--json` 出明细。
它**不进 CI**（CI 里不允许有需要真实网络的步骤，已有断言盯着），定位是升级后的人工验收。
失败会被分成「账号额度类」与「上游可用性类」，这两类不是代码回归，
只有「端点真的跑不通」才说明代码坏了。

### 修复：workflow 里的内联脚本改为落盘文件

`ci.yml` 里原本内联 heredoc 跑一段多行 Python。heredoc 的结束符一旦被 YAML 块缩进
带歪，bash 就找不到它，报 `syntax error: unexpected end of file` —— 这个坑已经踩了两次
（v0.1.1 修过一次，另一处还留着）。现在一律抽成 `tools/` 下的脚本：

- `tools/assert_no_cv2.py`
- `tools/check_no_cv2_path.py`

好处不只是不再踩坑：**脚本能被 `compileall` 检查语法**，
而内联在 YAML 块里的代码此前只能靠 `bash -n` 间接兜住。

### 新增守卫

- CI 至少覆盖 `requires-python` 的下界（3.10）与主力版本（3.12）——
  上次 3.10 那条腿在**收集阶段**就炸了，本地是 3.11 所以完全没看见
- workflow 里不许出现带缩进的 heredoc 结束符
- CI 里不许出现需要真实网络的步骤
- CNB 流水线必须保留「推回 GitHub」这一步（丢了不会报错，只会没动静）
- CNB 流水线的 `script` 块也要过 `bash -n`
- `tools/` 下的脚本都要能编译

## v0.1.1

### 修复：没装 OpenCV 时连 CLI 都起不来

**现象**（真实用户报告，环境是 Termux）：

```
$ xstech-gateway register
申请临时邮箱: nocvumuj@guerrillamailblock.com
Traceback (most recent call last):
  ...
  File ".../src/captcha.py", line 20, in <module>
    import cv2
ModuleNotFoundError: No module named 'cv2'
```

**根因不是「滑块解不了」，而是 `captcha.py` 在模块顶层写死了 `import cv2`。**

于是整条导入链（`cli` → `upstream` → `captcha`）在 import 阶段就断。
只有 `register` 用到滑块，但 `--help`、`models` 这些命令**一起陪葬**，
而且抛的是裸 `ModuleNotFoundError` —— 看不出缺什么、能不能装、这其实可修。

按 README 的 `pip install -e .` 安装必然踩中：`cv2` 从来没写进依赖。
Termux 默认环境也没有 OpenCV，所以对 Termux 用户是**必然触发**。

**改法**：

- 顶层不再 `import cv2`，只探测 `HAS_CV2`
- 解码改用 Pillow（本来就是声明依赖）：解成 RGB 后手工翻成 BGR + `float32`，
  **通道顺序和 dtype 与 `cv2.imdecode` 完全对齐**
- 形态学腐蚀两条路径：有 `cv2` 走 `cv2.erode`，没有走纯 numpy 等价实现，
  且对齐了 OpenCV 的边界语义（结构元在边界处只取界内重叠部分，**不做零填充**）
- 缺依赖时给可照着做的提示，不再是裸 traceback
- `cv2` 收进可选 extra：`pip install -e ".[captcha-cv2]"`
- 新增 14 例离线单测（共 32 例），并把 `cv2` 从导入系统里藏掉跑真实上游验证

**两条路径等价性**（不是「看起来差不多」）：

| 比对项 | 样本 | 结果 |
|---|---|---|
| `_erode` cv2 vs numpy | 5000 组随机掩码（3×3 ~ 60×60，含比核小、含各种贴边） | 逐像素一致，0 处差异 |
| 解码通道/dtype | 50 组随机 PNG | 输出完全相同 |
| 真实上游同一道题 | 1 道 | 同一个 x（123）、同一个置信度（0.7729） |

### 修复：CI 矩阵里 `pip install -e ""` 是非法参数

原本写成 `captcha-extra: ["", ".[captcha-cv2]"]`，展开后「不装 cv2」那条腿
会执行 `pip install -e ""`，一启动就挂。

挂着不算大事，问题在于**它把「无 cv2」这条路径又变回没被测到** ——
跟上面那个故障的成因一模一样。已改成用变量拼 extra，并显式断言
`cv2` 的存在状态符合预期。

## v0.1.0

首个版本。

- 摸清 xstech.one（GoAmzAI Plus 3.4.0）的私有协议：响应包装、鉴权头、
  模型清单、SSE 帧格式、`::` 分组命名
- 翻译成 OpenAI 协议：`/v1/models` + `/v1/chat/completions`，流式非流式
- 自动注册：临时邮箱 → 滑块求解 → 收码 → 注册 → 凭据落盘（`0600`）
- 滑块求解：模板匹配（不是打码平台、不训模型），单次命中率 90%
- 剥除 `<think>` 思考块，处理 SSE 从中间切开标签的情况（无状态纯函数）
