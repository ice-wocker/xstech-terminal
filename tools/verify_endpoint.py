"""对全部模型做一次真实调用，验证 OpenAI 兼容端点是否真的能用。

**这个脚本会打网络，所以不进 CI**（见 CONTRIBUTING 与 tests/test_packaging.py
里的 `test_ci_has_no_network_steps`）。它的定位是「升级后的人工验收」：

    xstech-gateway register          # 或 login
    python tools/verify_endpoint.py  # 逐模型实调，输出表格
    python tools/verify_endpoint.py --json report.json

退出码：0 = 全部可用；1 = 有模型失败（失败原因会逐条打出来）。
注意「失败」分两类，脚本会分别标注：
- `账号额度/套餐` 类（普通账号必然如此），不是代码问题；
- `上游无渠道 / 上游报错` 类，属于上游侧的可用性，也不是代码问题。
两类都不该被当成回归，但需要人看一眼。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.account import load  # noqa: E402
from src.gateway import ChatGateway  # noqa: E402
from src.upstream import Upstream  # noqa: E402

PROMPT = "只回答两个字：可用"

# 这些失败原因与「端点是否能用」无关，属于账号或上游侧
BENIGN_MARKERS = [
    ("套餐余量不足", "账号额度（普通账号无此模型额度）"),
    ("无可用渠道", "上游暂无渠道"),
    ("version", "上游自带组件版本过旧"),
]


def classify(err: str) -> str:
    for marker, label in BENIGN_MARKERS:
        if marker.lower() in err.lower():
            return label
    return "需人工看一下"


def main() -> int:
    ap = argparse.ArgumentParser(description="逐模型实调，验证端点可用性")
    ap.add_argument("--json", help="把结果写成 JSON")
    ap.add_argument("--only", help="只测匹配这个子串的模型")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    account = load()
    if account is None:
        print("还没有凭据，先跑 xstech-gateway register 或 login", file=sys.stderr)
        return 2

    gw = ChatGateway(
        Upstream(base=account.base, token=account.token, timeout=args.timeout),
        account.model,
    )
    models = [m for m in gw.list_models() if not args.only or args.only in m["id"]]
    print(f"共 {len(models)} 个模型待验证（base={account.base}）\n")

    rows = []
    for m in models:
        mid = m["id"]
        t0 = time.time()
        try:
            out = gw.complete({"model": mid, "messages": [{"role": "user", "content": PROMPT}]})
            content = (out["choices"][0]["message"]["content"] or "").strip()
            rows.append({
                "model": mid, "ok": True, "sec": round(time.time() - t0, 1),
                "sample": content[:60], "tokens": (out.get("usage") or {}).get("total_tokens", 0),
                "err": "", "kind": "",
            })
            print(f"OK   {mid:<40} {rows[-1]['sec']:>5}s  {content[:40]}")
        except Exception as exc:  # noqa: BLE001 - 逐个模型的失败不该中断整轮
            err = f"{type(exc).__name__}: {exc}"
            rows.append({
                "model": mid, "ok": False, "sec": round(time.time() - t0, 1),
                "sample": "", "tokens": 0, "err": err, "kind": classify(err),
            })
            print(f"FAIL {mid:<40} {rows[-1]['sec']:>5}s  [{rows[-1]['kind']}] {err[:80]}")

    okc = sum(1 for r in rows if r["ok"])
    print(f"\n可用 {okc}/{len(rows)}")
    for r in rows:
        if not r["ok"]:
            print(f"  - {r['model']}：{r['kind']}｜{r['err'][:100]}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        print(f"\n明细已写入 {args.json}")

    return 0 if okc == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
