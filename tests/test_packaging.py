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
import tomllib
from pathlib import Path

import pytest

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


def test_version_is_consistent():
    with PYPROJECT.open("rb") as f:
        pkg_version = tomllib.load(f)["project"]["version"]

    init_text = (ROOT / "src" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert m, "src/__init__.py 里找不到 __version__"

    cli_text = (ROOT / "src" / "cli.py").read_text(encoding="utf-8")
    assert m.group(1) == pkg_version, (
        f"版本号不一致：pyproject={pkg_version}，__init__={m.group(1)}；"
        "两者必须同步，否则用户没法从 --version 判断自己装的是哪版"
    )
    assert "__version__" in cli_text, "cli.py 应当用 __version__ 输出 --version"


@pytest.mark.skipif(sys.platform == "win32", reason="bash 语法检查，Windows 不适用")
def test_workflow_run_blocks_are_valid_shell():
    """遍历 workflow 里所有 `run` 块，逐个交给 `bash -n`。

    这一次性拦住「YAML 合法但嵌的脚本是坏的」整类问题 ——
    包括 heredoc 结束符被块缩进带歪这种。
    """
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
