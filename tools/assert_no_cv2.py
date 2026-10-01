"""断言当前环境里没有 OpenCV。

原本这段是内联在 workflow 的 `run` 块里的一句 `python -c`，
再之前是 heredoc。heredoc 的结束符一旦被 YAML 块缩进带歪，bash 就找不到它，
报 `syntax error: unexpected end of file` —— 这个坑连着踩了两回。

抽成文件的好处是它能被 `compileall` 覆盖、能被 `bash -n` 之外的常规手段检查，
也不会因为 YAML 缩进而改变语义。
"""

from __future__ import annotations

import importlib.util
import sys


def main() -> int:
    if importlib.util.find_spec("cv2") is not None:
        print("这条腿不该装 OpenCV，实际却装上了", file=sys.stderr)
        return 1
    print("cv2 不存在，符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
