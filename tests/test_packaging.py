"""打包与构建配置的自检。

这两类错误有个共同点：**本地跑 pytest 完全看不出来**，
只有真的走一次「安装」或「bash 解析」才暴露。它们各自都真实发生过一次：

1. `[project.urls]` 插在 `dependencies` 前面 → TOML 把它解析成
   `project.urls.dependencies`，`pip install -e .` 直接失败。
   而 `pytest` 用的是源码目录，不碰打包元数据，所以照样绿。
2. 构建脚本里的 heredoc 结束符带了缩进 → bash 找不到 `PY`，
   报 `syntax error: unexpected end of file`。肉眼看缩进也像对的。

第 2 条原来在 GitHub Actions 的 workflow 里，现在这些检查搬进了 `Makefile`
（连 CI 一起搬走了，理由见 `.github/workflows/README.md`），所以这里改成
校验 Makefile 的每个 recipe 行 —— 同样的错法，换个地方还是会犯。

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
MAKEFILE = ROOT / "Makefile"


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


@pytest.mark.skipif(sys.platform == "win32", reason="make 语法检查，Windows 不适用")
def test_makefile_recipes_are_valid_shell():
    """把 Makefile 里每个 recipe 行交给 `bash -n`。

    拦的是「Makefile 能读、但里面的 shell 是坏的」这一类 —— 包括 heredoc
    结束符被缩进带歪这种（这坑原来在 workflow 里踩过一次）。
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()

    checked = 0
    lineno = 0
    while lineno < len(lines):
        line = lines[lineno]
        lineno += 1
        if not line.startswith("\t") or not line.strip():
            continue  # 只挑 recipe 行（Makefile 用真 tab 缩进）

        # 续行要拼起来整条跑：单看某一行可能只是 `... \` 的一半，
        # `bash -n` 会把它当语法错误 —— 那是假阳性。
        start = lineno
        script = line[1:]
        while script.rstrip().endswith("\\") and lineno < len(lines):
            script = script.rstrip()[:-1] + lines[lineno].lstrip()
            lineno += 1

        script = re.sub(r"\$\$", "$", script)     # 还原 Make 转义
        # 展开变量：从内到外反复替换，因为 `$(word 1,$(subst :, ,$(X)))` 这种
        # 是嵌套的。只跑一遍会留下半截括号，`bash -n` 判语法错 —— 那是假阳性。
        previous = None
        while previous != script:
            previous = script
            script = re.sub(r"\$\([^()]*\)", "PLACEHOLDER", script)
        proc = subprocess.run(["bash", "-n"], input=script,
                              capture_output=True, text=True)
        checked += 1
        assert proc.returncode == 0, (
            f"Makefile 第 {start} 行的 recipe bash 解析失败：\n{script}\n{proc.stderr.strip()}"
        )

    assert checked > 0, "一个 recipe 行都没检查到，说明 Makefile 结构变了"


def test_all_target_covers_smoke_test_and_e2e():
    """`make all` 必须把三件事都带上，否则改 Makefile 时容易漏掉其中一项。"""
    text = MAKEFILE.read_text(encoding="utf-8")
    m = re.search(r"^all:(.*)$", text, re.M)
    assert m, "Makefile 里找不到 all 目标"
    for target in ("smoke", "test", "e2e"):
        assert target in m.group(1), f"`make all` 少了 {target}"


def test_no_ci_workflow_and_no_dangling_reference():
    """CI 被有意删掉了。哪天有人又加回来，要么是想清楚了，要么是顺手 ——
    两种情况都该在这条断言上停一下，去看 `.github/workflows/README.md`。"""
    workflows = ROOT / ".github" / "workflows"
    assert not list(workflows.glob("*.y*ml")), (
        "又出现了 workflow 文件。这个项目有意不做 CI（见 .github/workflows/README.md），"
        "检查请加进 Makefile 的 all。"
    )
    # 顺带确认没有别的地方还指着已删的 ci.yml。
    # 扫的对象要排除两类：本文件（下面这行断言的原文里就有这个名字），
    # 以及 .github/workflows/README.md（专门解释为什么删的那篇）。
    suspects = [p for p in ROOT.glob("*.md")]
    suspects += [p for p in (ROOT / "src").glob("*.py")]
    suspects += [p for p in (ROOT / "tests").glob("*.py") if p.name != Path(__file__).name]
    for path in suspects:
        assert "ci.yml" not in path.read_text(encoding="utf-8"), (
            f"{path.name} 还引用着已删除的 workflows/ci.yml；"
            "检查请加进 Makefile 的 all"
        )
