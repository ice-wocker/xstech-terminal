"""离线单测：穷举工具的逻辑与「不把结论写歪」的守护。

这个模块的价值全在**结论的准确性**上：

- 它必须能跑完所有向量（不能在中间炸掉，否则「穷举」就成了空话）；
- 它必须把「做不成」和「没做过」分开 —— 这是两种完全不同的结论，
  混淆会让人以为还有戏，继续白跑；
- 它**绝不能**因为某条路走不通就把 `passed` 写成 True（那是骗自己）。

下面每一条都对应上面一个真实风险。
"""

import pytest

from src import bruteclose
from src.bruteclose import (
    ClosureItem,
    ClosureReport,
    assess_replay,
    assess_sdk_patch,
    assess_token_forge,
    probe_ordvd_direct,
)

# ---------- 报告聚合 ----------

def test_report_counts_passed():
    r = ClosureReport()
    r.add(ClosureItem("a", "x", "过了", feasible=True, passed=True))
    r.add(ClosureItem("b", "y", "没过", feasible=True, passed=False))
    assert len(r.passed) == 1
    assert "1" in r.conclusion() and "推翻" in r.conclusion()


def test_conclusion_all_blocked_mentions_structure():
    """全被挡且含「做不成」时，结论必须点明是**结构问题**而不是强度问题。

    这句是给老板做判断用的：结构问题意味着继续加力度没用。
    """
    r = ClosureReport()
    r.add(ClosureItem("v", "a", "x", feasible=True, passed=False))
    r.add(ClosureItem("v", "b", "y", feasible=False, passed=False))
    c = r.conclusion()
    assert "结构问题" in c
    assert "根本不可能完成" in c


def test_by_vector_groups():
    r = ClosureReport()
    r.add(ClosureItem("a", "1", "", True))
    r.add(ClosureItem("a", "2", "", True))
    r.add(ClosureItem("b", "3", "", True))
    assert set(r.by_vector()) == {"a", "b"}
    assert len(r.by_vector()["a"]) == 2


# ---------- 第 4 族：token_forge 必须判为「做不成」 ----------

def test_token_forge_infeasible_and_cites_the_missing_key():
    """缺密钥 → `feasible` 必须为 False，并说清缺的是什么。

    如果这里返回 True，就会把「结构上做不到」说成「还没试出来」，
    误导后续投入。
    """
    it = assess_token_forge(["AES", "SHA256", "computeSignature", "lib", "enc"])
    assert it.feasible is False
    assert it.passed is False
    assert "密钥" in it.blocker


def test_token_forge_detects_algo_but_no_key():
    it = assess_token_forge(["AES", "SHA256", "computeSignature"])
    assert "含算法" in it.observed
    assert "不含密钥" in it.observed


def test_token_forge_reports_key_when_present():
    """万一哪天 __ALIYUN_CRYPT 真带上了密钥，这条要能反映出来。

    这不是为了「希望它带」，而是为了**这个判断是可证伪的** ——
    如果密钥真的出现在客户端，工具必须立刻说「含密钥」，
    而不是继续输出「做不成」。
    """
    it = assess_token_forge(["AES", "key", "iv"])
    assert "含密钥" in it.observed


def test_missing_key_evidence_is_a_chain_not_a_claim():
    ev = ClosureReport().missing_key_evidence()
    assert "deviceToken" in ev
    assert "__ALIYUN_CRYPT" in ev
    assert "唯独造不出" in ev


# ---------- 第 5 族：replay 是鸡生蛋 ----------

def test_replay_infeasible_without_sample():
    it = assess_replay(has_success_sample=False)
    assert it.feasible is False
    assert it.passed is False
    assert "鸡生蛋" in it.blocker


def test_replay_becomes_feasible_with_sample():
    """有样本时必须改口说「可执行」—— 判据不能写死。"""
    it = assess_replay(has_success_sample=True)
    assert it.feasible is True


# ---------- 第 7 族：客户端通过毫无意义 ----------

def test_sdk_patch_never_counts_as_passed():
    """客户端显示通过也必须判 `passed=False`。

    这是本模块最容易写歪的一处：页面真的会显示「验证通过」，
    如果据此判成功，就会得出「已经攻破」的错误结论。
    """
    it = assess_sdk_patch(patched_client_thought_ok=True, server_code="F001")
    assert it.passed is False
    assert "服务端独立复判" in it.blocker


# ---------- 第 3 族：ordvd 直喂（新增向量） ----------

class _FakeClient:
    """把验证接口换成脚本化响应，其余逻辑照跑（不联网）。"""

    base = "https://example.invalid"

    def __init__(self, code="REJECT_PARAM"):
        self.code = code

    def verify_captcha_raw(self, param, purpose, target=""):
        return {"VerifyCode": self.code, "VerifyResult": False}


def test_ordvd_direct_produces_two_attempts():
    out = probe_ordvd_direct({"gdffd98u9": "ZmFrZQ=="}, _FakeClient())
    assert [i.attempt for i in out] == ["ordvd_as_param", "ordvd_is_valid_shape"]
    assert all(not i.passed for i in out)


def test_ordvd_direct_tolerates_non_dict():
    """抓 __ORDVD 失败时可能拿到字符串，工具不能因此炸掉。

    「一条向量跑不了」和「整个穷举崩了」是两回事，前者可接受，后者不行。
    """
    out = probe_ordvd_direct("not-a-dict", _FakeClient())
    assert len(out) == 2


def test_ordvd_direct_detects_success():
    """万一服务端认了明文，必须判 `passed=True` 并让上层变红。

    这是可证伪性的要求：工具不能只在「失败」时才工作正常。
    """
    out = probe_ordvd_direct({}, _FakeClient(code="Success"))
    assert any(i.passed for i in out)


def test_ordvd_blocker_names_the_plaintext_vs_ciphertext_gap():
    """卡点必须落在「明文 vs 密文」这个真实原因上，而不是一句「被拒了」。"""
    it = probe_ordvd_direct({}, _FakeClient())[0]
    assert "明文" in it.blocker and "密文" in it.blocker


# ---------- 回归：绝不能把「没绕过」写成「绕过了」 ----------

def test_no_vector_hardcodes_passed_true():
    """扫描源码：任何向量族的判定都不能把 `passed` 写死成 True。

    `passed=True` 只允许出现在「真检测到通过」的分支里（比如
    `ok = code in (...)` 之后）。写死一句 `passed=True` 就是最典型的
    自欺 —— 一旦有，就该变红。
    """
    from pathlib import Path

    src = Path(bruteclose.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped.startswith("passed="):
            continue
        assert stripped != "passed=True,", (
            f"发现把 passed 写死为 True 的判定: {line!r}"
        )


@pytest.mark.parametrize("code", ["REJECT_PARAM", "EMPTY_PARAM"])
def test_param_layer_codes_are_not_treated_as_draw(code):
    """参数层拒绝 ≠ 走到画像层。解释文案必须把这两层分开。

    混为一谈会让人以为「画像也拦了」，从而错判该往哪个方向查。
    """
    from src.spoof import classify_verify_code

    msg = classify_verify_code(code)
    assert "参数层" in msg
    assert "未进入判定" in msg


# ---------- 版本号单一真相源 ----------

def test_version_has_single_source_of_truth():
    """版本号只能写在一个地方，且构建时读到的必须是它。

    这条是真出过的故障：`pyproject.toml` 与 `src/__init__.py` 各写了一个版本，
    改一处漏一处，结果 `pip install` 出来的包和 `--version` 打印的不一致 ——
    用户报 bug 时报的版本号跟实际跑的不是一个。

    另外钉住一个 setuptools 的细节：`project.version` **必须是字面量**，
    不能写 `version = {attr = ...}`（那样 `pip install -e .` 会直接报
    「`project.version` must be string」）。要用动态版本，只能走
    `dynamic = ["version"]` + `[tool.setuptools.dynamic]`。
    """
    from pathlib import Path

    import src

    pyproject = Path(src.__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")

    # 1) 不允许在 [project] 里硬写一个版本号（那会和 __init__ 漂移）
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("version =") or "attr" in stripped, (
            f"pyproject.toml 里出现了硬写的版本号，会和 src/__init__.py 漂移: {line!r}"
        )

    # 2) 必须声明动态版本，且指向 __init__ 里的字段
    assert 'dynamic = ["version"]' in text
    assert 'version = {attr = "src.__version__"}' in text
    assert src.__version__
