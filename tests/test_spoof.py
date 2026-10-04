"""离线单测：把「伪装阿里云服务器」的三条路线钉成可复现的实验。

这些测试**不联网**：网络部分（真打接口 / 真起浏览器）在
`probe_forged_tickets` / `probe_spoofed_headers` / `probe_local_response_spoof`
里，由 `amd-spoof` 命令现场跑。这里只钉死**判定逻辑与数据结构**，
确保「全部失败」这个结论是可断言的，而不是靠嘴说。
"""

import pytest

from xstech import spoof
from xstech.spoof import (
    FORGED_PARAMS,
    SPOOF_HEADERS,
    SpoofFinding,
    classify_verify_code,
    summarize_findings,
)


# ---------- 返回码语义 ----------

def test_empty_and_reject_are_param_layer():
    """EMPTY/REJECT 是**参数层**拒绝，说明服务端在比对画像之前就挡掉了伪造。"""
    assert "参数层" in classify_verify_code("EMPTY_PARAM")
    assert "参数层" in classify_verify_code("REJECT_PARAM")


def test_f_codes_are_portrait_layer():
    """F001/F011/F015 才是走到画像/行为层之后的拦截码 —— 和参数层要分清。"""
    for code in ("F001", "F011", "F015"):
        assert "画像" in classify_verify_code(code) or "轨迹" in classify_verify_code(code)


def test_success_code_recognized():
    assert "通过" in classify_verify_code("Success")


def test_unknown_code_does_not_crash():
    assert "未知" in classify_verify_code("WHO_KNOWS")


# ---------- 伪造形态覆盖 ----------

def test_forged_params_cover_key_hypotheses():
    """伪造形态必须覆盖：空 / 明文 / base64 / 结构完整，四个假设都试到。"""
    assert FORGED_PARAMS["empty"] == ""
    assert FORGED_PARAMS["plain_success"] == "SUCCESS"
    assert "base64_json" in FORGED_PARAMS
    # 结构完整那一条必须带 deviceToken，否则测的不是「伪造得像」
    shape_ok = FORGED_PARAMS["shape_ok_fake_token"]
    assert "deviceToken" in shape_ok and "sceneId" in shape_ok


def test_spoof_headers_include_aliyun_and_internal():
    """伪装来源要覆盖「阿里云 IP」和「内网」两种信任假设。"""
    joined = str(SPOOF_HEADERS)
    assert "106.14.30.30" in joined          # 阿里云 cn-shanghai
    assert "127.0.0.1" in joined             # 内网回环
    assert "plain" in SPOOF_HEADERS          # 要有基线对照


def test_aliyun_hosts_listed():
    """要拦截的域名清单必须含 SDK 出题/验真与设备指纹域名。"""
    assert any("captcha-open.aliyuncs.com" in h for h in spoof.ALIYUN_HOSTS)
    assert any("cloudauth-device" in h for h in spoof.ALIYUN_HOSTS)


# ---------- 结论汇总 ----------

def test_summary_all_failed_is_the_expected_verdict():
    findings = [SpoofFinding(f"v{i}", "d", "REJECT_PARAM", False) for i in range(3)]
    out = summarize_findings(findings)
    assert "全部" in out and "没有绕过" in out


def test_summary_flags_any_bypass_loudly():
    """任何一条‘过了’都必须被显著标出 —— 那才是真漏洞。"""
    findings = [
        SpoofFinding("a", "d", "Success", True),
        SpoofFinding("b", "d", "REJECT_PARAM", False),
    ]
    out = summarize_findings(findings)
    assert "复核" in out


def test_summary_empty_is_safe():
    assert "没有" in summarize_findings([])


# ---------- 网络伪函数的错误处理 ----------

def test_probe_forged_tickets_does_not_swallow_network_error(monkeypatch):
    """网络失败要变成一条 finding（bypassed=False），而不是抛出去或静默吞掉。"""
    import types

    def boom(*a, **k):
        raise OSError("network down")

    fake_requests = types.SimpleNamespace(Session=lambda: types.SimpleNamespace(
        headers={}, post=boom))
    monkeypatch.setattr(spoof, "requests", fake_requests, raising=False)
    findings = spoof.probe_forged_tickets("http://x", timeout=1)
    assert len(findings) == len(spoof.FORGED_PARAMS)
    assert all(not f.bypassed for f in findings)
    assert all("请求失败" in f.result for f in findings)


def test_local_response_spoof_without_playwright_raises_runtime_error(monkeypatch):
    """缺 playwright 时要抛 RuntimeError 并给安装提示，不能裸抛 ImportError。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError) as ei:
        spoof.probe_local_response_spoof()
    assert "playwright" in str(ei.value)
