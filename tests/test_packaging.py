"""打包与 CI 配置的自检。

这两类错误有个共同点：**本地跑 pytest 完全看不出来**，
只有真的走一次「安装」或「bash 解析」才暴露。它们各自都真实发生过一次：

1. `[project.urls]` 插在 `dependencies` 前面 → TOML 把它解析成
   `project.urls.dependencies`，`pip install -e .` 直接失败。
   而 `pytest` 用的是源码目录，不碰打包元数据，所以照样绿。
2. YAML 块标量里的 heredoc 结束符带了缩进 → bash 找不到 `PY`，
   报 `syntax error: unexpected end of file`。`yaml.safe_load` 能过，
   肉眼看缩进也像对的。

所以这里断言的是「能被解析 / 能被执行」，而不是「看起来像对的」。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+ 标准库
except ModuleNotFoundError:  # pragma: no cover - 3.10 分支
    # 项目声明 requires-python = ">=3.10"，而 tomllib 是 3.11 才进标准库的。
    # 之前这里直接 `import tomllib`，3.10 那条 CI 腿在**收集阶段**就炸了
    # （ModuleNotFoundError，整个文件一条用例都没跑）——
    # 本地是 3.11 所以完全没看见。tomli 已在 pyproject 的 dev extra 里。
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_pyproject_is_parseable():
    with PYPROJECT.open("rb") as f:
        tomllib.load(f)


def test_project_urls_is_not_nested_under_dependencies():
    """`[project.urls]` 必须排在 `[project]` 的表项之后，否则后续裸键会被它吞掉。"""
    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)

    project = data["project"]
    assert "urls" in project, "缺少 [project.urls]"

    urls = project["urls"]
    assert all(isinstance(v, str) for v in urls.values()), (
        "project.urls 的每个值都必须是字符串；"
        "出现非字符串通常意味着 [project.urls] 表头被写在了别的表项前面"
    )
    # 最典型的一种：dependencies 被解析进了 urls
    assert "dependencies" not in urls, (
        "`dependencies` 被解析成了 `project.urls.dependencies` —— "
        "原因是 [project.urls] 表头写在了 dependencies 前面。"
        "把 [project.urls] 移到 dependencies 之后。"
    )
    assert "dependencies" in project, "project.dependencies 丢了"


def test_ci_covers_min_and_max_python():
    """CI 必须同时覆盖 requires-python 的下界与当前主力版本。

    3.10 那条腿曾经因为 `import tomllib` 在**收集阶段**就炸（整个文件零用例），
    而本地是 3.11，所以完全没看见。下界必须在 CI 里被真的跑到。
    """
    if not WORKFLOW.exists():
        pytest.skip("CNB 侧不跑 GitHub workflow")
    yaml = pytest.importorskip("yaml")

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    versions = set()
    for job in workflow["jobs"].values():
        matrix = ((job.get("strategy") or {}).get("matrix") or {})
        versions |= set(matrix.get("python-version") or [])
    assert "3.10" in versions, "CI 没有覆盖 requires-python 的下界 3.10"
    assert "3.12" in versions, "CI 没有覆盖主力版本 3.12"


def test_workflow_has_no_indented_heredoc_terminator():
    """workflow 里不要出现「带缩进的 heredoc 结束符」。

    这是本项目踩过两次的坑：`run: |` 的块缩进会原样进入脚本，
    结束符 `PY` 一旦不顶格，bash 就报 `syntax error: unexpected end of file`。
    用户本地看不出，CI 才会红。

    需要多行 Python 时，写成 `tools/` 下的文件再调用 —— 那样还能被 compileall 检查。
    """
    if not WORKFLOW.exists():
        pytest.skip("CNB 侧不跑 GitHub workflow")
    text = WORKFLOW.read_text(encoding="utf-8")
    offenders = [
        (i, line)
        for i, line in enumerate(text.splitlines(), 1)
        if line.startswith(" ") and line.strip() in {"PY", "EOF", "EOT", "PYEOF"}
    ]
    assert not offenders, (
        "workflow 里出现了带缩进的 heredoc 结束符（bash 会找不到它）："
        f"{offenders}。多行脚本请放到 tools/ 下。"
    )


def test_version_is_consistent():
    """版本号只能有一个真相源。

    两种写法都接受，但不允许两处各写一个数：

    - `dynamic = ["version"]`（当前方案）：pyproject 不写版本，
      只有 `src/__init__.py` 一处，天然不会漂移；
    - 写死字面量（旧方案）：那就必须与 `__init__.py` 一致。

    这里之前直接取 `["project"]["version"]`，前者会 KeyError ——
    那是把「旧方案」当成了唯一合法写法。
    """
    with PYPROJECT.open("rb") as f:
        project = tomllib.load(f)["project"]
    assert project["name"] == "xstech-gateway", "包名不对，本文件的其余断言会跟着错"

    init_text = (ROOT / "xstech" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert m, "src/__init__.py 里找不到 __version__"

    if "version" in project:
        pkg_version = project["version"]
    else:
        assert "version" in project.get("dynamic", []), (
            "pyproject 里既没有静态 version，也没把 version 列进 dynamic："
            "打出来的包会没有版本号"
        )
        pkg_version = m.group(1)


    cli_text = (ROOT / "xstech" / "cli.py").read_text(encoding="utf-8")
    assert m.group(1) == pkg_version, (
        f"版本号不一致：pyproject={pkg_version}，__init__={m.group(1)}；"
        "两者必须同步，否则用户没法从 --version 判断自己装的是哪版"
    )
    assert "__version__" in cli_text, "cli.py 应当用 __version__ 输出 --version"
    assert re.search(
        r'version=f"%(prog)s\s*\{?', cli_text
    ) or "__version__" in cli_text, "cli.py 应当用 __version__ 拼出 --version"


@pytest.mark.skipif(sys.platform == "win32", reason="bash 语法检查，Windows 不适用")
def test_workflow_run_blocks_are_valid_shell():
    """遍历 workflow 里所有 `run` 块，逐个交给 `bash -n`。

    这一次性拦住「YAML 合法但嵌的脚本是坏的」整类问题 ——
    包括 heredoc 结束符被块缩进带歪这种。
    """
    if not WORKFLOW.exists():
        # CNB 侧不跑 GitHub workflow（.github 可能不存在），别在这里判红。
        # 「ci.yml 本身语法合法」由 test_ci_covers_min_and_max_python 在能拿到文件时保证。
        pytest.skip("CNB 侧不跑 GitHub workflow")
    yaml = pytest.importorskip("yaml")

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    checked = 0

    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run")
            if not isinstance(run, str):
                continue  # `uses:` 步骤没有 run
            # 模拟 GHA 展开表达式，避免 `${{ }}` 干扰 bash 解析
            expanded = re.sub(r"\$\{\{[^}]*\}\}", "PLACEHOLDER", run)
            proc = subprocess.run(
                ["bash", "-n"],
                input=expanded,
                capture_output=True,
                text=True,
            )
            checked += 1
            assert proc.returncode == 0, (
                f"workflow 里 [{job_name}] / 「{step.get('name')}」的 run 块 bash 解析失败：\n"
                f"{proc.stderr.strip()}"
            )

    assert checked > 0, "没有检查到任何 run 块，说明 workflow 结构变了"


def test_ci_has_no_network_steps():
    """CI 里不能引入需要真实网络的步骤。

    上游会变、会限流，「验证真实上游」只能是手动跑的脚本
    （`tools/verify_endpoint.py`），不能进 workflow —— 否则哪天上游抖一下，
    CI 就红了，而红的原因跟代码没关系。CONTRIBUTING 里写过这条，这里把它变成断言。
    """
    if not WORKFLOW.exists():
        pytest.skip("CNB 侧不跑 GitHub workflow")
    text = WORKFLOW.read_text(encoding="utf-8")

    banned = ["xstech.one", "guerrillamail", "xstech-gateway register", "xstech-gateway ask"]
    hits = [b for b in banned if b in text]
    assert not hits, f"CI 里出现了要真实网络的步骤：{hits}；请移到 tools/ 下手动运行"


def test_tools_scripts_are_compilable():
    """`tools/` 下的脚本都要能通过语法检查（它们本身就是从 workflow 里抽出来的）。"""
    tools = ROOT / "tools"
    scripts = sorted(tools.glob("*.py"))
    assert scripts, "tools/ 下应该有脚本"
    for s in scripts:
        source = s.read_text(encoding="utf-8")
        compile(source, str(s), "exec")


CNB_PIPELINE = ROOT / ".cnb.yml"


def test_cnb_pipeline_pushes_to_github():
    """CNB 流水线里必须有「把 main 推回 GitHub」这一步。

    这是用户明确要求的行为（「让以后推送自动推 github」），
    写成断言是为了防止后来重构流水线时把它悄悄弄丢 ——
    丢了不会报错，只会「推了但 GitHub 上没动静」，很难发现。
    """
    if not CNB_PIPELINE.exists():
        pytest.skip("没有 CNB 流水线文件")
    yaml = pytest.importorskip("yaml")

    pipeline = yaml.safe_load(CNB_PIPELINE.read_text(encoding="utf-8"))
    entries = pipeline["main"]["push"]
    assert isinstance(entries, list), "main.push 应当是列表（每条一个流水线）"

    scripts = "\n".join(
        st.get("script", "")
        for entry in entries
        for st in entry.get("stages", [])
        if isinstance(st, dict)
    )
    assert "git push" in scripts, "CNB 流水线里没有 git push"
    assert "github" in scripts, "CNB 流水线里的 push 目标不是 GitHub"
    # token 必须来自环境变量，不能硬编码进仓库
    assert "GITHUB_SYNC_TOKEN" in scripts, "推送用的 token 应当来自环境变量"
    assert not re.search(r"gh[pous]_[A-Za-z0-9]{20,}", scripts), (
        "流水线里出现了疑似硬编码的 GitHub token，必须改用环境变量"
    )


def test_cnb_pipeline_run_blocks_are_valid_shell():
    """CNB 流水线的 script 块同样要过 bash -n。"""
    if not CNB_PIPELINE.exists():
        pytest.skip("没有 CNB 流水线文件")
    yaml = pytest.importorskip("yaml")

    pipeline = yaml.safe_load(CNB_PIPELINE.read_text(encoding="utf-8"))
    checked = 0
    for entry in pipeline["main"]["push"]:
        for st in entry.get("stages", []):
            run = st.get("script") if isinstance(st, dict) else None
            if not isinstance(run, str):
                continue
            expanded = re.sub(r"\$\{\{[^}]*\}\}", "PLACEHOLDER", run)
            proc = subprocess.run(["bash", "-n"], input=expanded, capture_output=True, text=True)
            checked += 1
            assert proc.returncode == 0, (
                f"CNB 流水线 [{st.get('name')}] 的 script bash 解析失败：\n{proc.stderr.strip()}"
            )
    assert checked > 0, "没有检查到任何 script 块"
