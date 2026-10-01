"""命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from . import __version__
from .account import CRED_FILE, StoredAccount, clear, load, save
from .gateway import ChatGateway
from .upstream import DEFAULT_BASE, Upstream, UpstreamError


def _mailbox(kind: str) -> tuple[str, object]:
    """申请临时邮箱（Guerrilla Mail 公开接口）。"""
    import requests

    r = requests.get(
        "https://api.guerrillamail.com/ajax.php",
        params={"f": "get_email_address"},
        timeout=20,
    )
    data = r.json()
    return data["email_addr"], data["sid_token"]


def _fetch_code(sid: str, timeout: int = 180) -> str | None:
    """从 Guerrilla Mail 收件箱里取出 XSTECH 的注册验证码。

    坑点：新邮箱的第一封信永远是 Guerrilla Mail 自己的欢迎邮件，
    正文里带着 sharklasers.com 这类含数字的字符串，直接全库正则抓数字
    会抓到它。所以必须按发件人/主题先筛出目标邮件。
    """
    import json
    import re
    import time

    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(6)
        listing = requests.get(
            "https://api.guerrillamail.com/ajax.php",
            params={"f": "check_email", "sid_token": sid, "seq": 0},
            timeout=25,
        ).json()
        for meta in listing.get("list") or []:
            subject = meta.get("mail_subject") or ""
            sender = (meta.get("mail_from") or "").lower()
            if "guerrillamail" in sender:
                continue  # 欢迎邮件，跳过
            if "XSTECH" not in subject.upper() and "验证码" not in subject:
                continue
            body = requests.get(
                "https://api.guerrillamail.com/ajax.php",
                params={"f": "fetch_email", "sid_token": sid, "email_id": meta["mail_id"]},
                timeout=25,
            ).json()
            text = json.dumps(body, ensure_ascii=False)
            found = re.findall(r"(?<!\d)(\d{6})(?!\d)", text)
            if found:
                return found[0]
    return None


def cmd_register(args) -> int:
    up = Upstream(base=args.base)
    email, sid = _mailbox("guerrilla")
    print(f"申请临时邮箱: {email}")
    up.send_register_code(email)
    print("验证码已发送，等待收信……")
    code = _fetch_code(sid, timeout=args.mail_timeout)
    if not code:
        print("未在超时内收到验证码，请改用 --email/--password 手动注册", file=sys.stderr)
        return 1
    print(f"收到验证码 {code}")
    creds = up.register(email, args.password, code)
    account = StoredAccount(email=email, password=args.password, token=creds.token, base=args.base)
    path = save(account)
    print(f"注册成功，凭据已写入 {path}")
    return 0


def _ensure_token(args) -> StoredAccount:
    account = load()
    if args.email and args.password:
        account = StoredAccount(email=args.email, password=args.password, base=args.base)
    if account is None:
        raise SystemExit("尚未登录，先运行 xstech-gateway register")
    if account.token:
        return account
    up = Upstream(base=account.base)
    account.token = up.login(account.email, account.password).token
    save(account)
    return account


def cmd_models(args) -> int:
    account = _ensure_token(args)
    up = Upstream(base=account.base, token=account.token)
    for m in up.models():
        mark = " " if m["id"] != account.model else "*"
        print(f"{mark} {m['id']:<44} {m['label']}")
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    account = _ensure_token(args)
    model = args.model or account.model
    if model and model != account.model:
        account.model = model
        save(account)
    if not model:
        try:
            model = Upstream(base=account.base, token=account.token).models()[0]["id"]
        except UpstreamError as exc:
            print(f"无法获取默认模型: {exc}", file=sys.stderr)
            return 1
    gateway = ChatGateway(
        Upstream(base=account.base, token=account.token), default_model=model
    )
    serve(gateway, args.host, args.port, api_key=args.api_key)
    return 0


def cmd_ask(args) -> int:
    """直接问一句。走 gateway 而不是裸调 upstream，才能复用同一套
    思考块过滤与协议翻译 —— 否则这里会漏出 <think>。"""
    from .gateway import visible_prefix

    account = _ensure_token(args)
    model = args.model or account.model
    up = Upstream(base=account.base, token=account.token)
    if not model:
        model = up.models()[0]["id"]
    sid = up.open_session(model)

    buffer = ""
    emitted = 0
    for frame in up.stream(" ".join(args.prompt), sid, model=model):
        if frame["type"] != "delta":
            continue
        buffer += frame["text"]
        clean = visible_prefix(buffer)
        if len(clean) > emitted:
            sys.stdout.write(clean[emitted:])
            sys.stdout.flush()
            emitted = len(clean)
    print()
    return 0


def cmd_login(args) -> int:
    up = Upstream(base=args.base)
    creds = up.login(args.email, args.password)
    if not creds.token:
        print("登录成功但未拿到 token", file=sys.stderr)
        return 1
    save(StoredAccount(
        email=args.email, password=args.password, token=creds.token, base=args.base
    ))
    print(f"登录成功，凭据已写入 {CRED_FILE}")
    return 0


def cmd_whoami(args) -> int:
    account = _ensure_token(args)
    info = Upstream(base=account.base, token=account.token).user_info()
    print(f"邮箱: {info.get('email')}")
    print(f"昵称: {info.get('nickname')}")
    print(f"用户 ID: {info.get('id')}")
    print(f"默认模型: {account.model or '（未设置）'}")
    return 0


def cmd_logout(args) -> int:
    print("已清除本地凭据" if clear() else "没有本地凭据")
    return 0


def cmd_amd_register(args) -> int:
    """在 AMD 开发者计划上注册一个账号。

    收信走 `mailbox.py`（默认 inboxes.com / clowmail.com，被拦自动换域名），
    手机号默认随机（服务端强制必填但不校验归属），人机验证走真浏览器驱动。

    关于「全自动」的边界（实测结论，写在 `src/browser.py` 的模块说明里）：
    阿里云验证除了拼图位置，还叠了一层**服务端设备/行为画像**。位置错了
    直接 `F015`；位置对了也只是变成 `F001`，**永远差一层** —— 所以位置
    算准只是入场券，过不过由服务端的画像定。
    所以拿不到 Param 时这个命令会诚实地说停在哪（退出码 3），不假装成功。

    想继续，把一次有效的 `CaptchaVerifyParam` 用 `--captcha-param` 传进来
    （在你自己的浏览器里过完验证后抓出来，有效期几分钟）。
    """
    from .amd import AmdClient, AmdError, CaptchaRequired, StaticCaptchaSolver, NoCaptchaSolver
    from .mailbox import Mailbox, MailboxPool, MailboxError
    from .phone import random_cn_mobile

    phone = args.phone or random_cn_mobile()
    if not args.phone:
        print(f"手机号: 未指定，用随机号 {phone}（服务端只校验格式，不校验归属）")

    # 人机验证 solver：给了 Param 就用静态的，否则按需起真浏览器。
    if args.captcha_param:
        solver = StaticCaptchaSolver(args.captcha_param)
    elif args.browser:
        from .browser import BrowserCaptchaSolver, BrowserUnavailable

        try:
            solver = BrowserCaptchaSolver(
                email="",  # 每次注册会重设
                phone=phone,
                headless=not args.headful,
                executable_path=args.browser_path,
                attempts=args.browser_attempts,
                proxy=args.proxy,
                stealth=not args.no_stealth,
            )
        except BrowserUnavailable as exc:
            print(f"无法驱动浏览器: {exc}", file=sys.stderr)
            return 2
    else:
        solver = NoCaptchaSolver()

    # 先建收件箱：默认域名（clowmail.com）在前，被拦自动换。
    preferred = [args.mail_domain] if args.mail_domain else ["clowmail.com"]
    pool = MailboxPool(preferred_domains=preferred)

    try:
        mailbox, _ = pool.acquire()
    except MailboxError as exc:
        print(f"收信后端不可用: {exc}", file=sys.stderr)
        return 1
    if mailbox is None:
        print("所有候选域名都建不出收件箱，收信后端可能整体不可用。", file=sys.stderr)
        return 1

    address = mailbox.address
    if hasattr(solver, "email"):
        solver.email = address
    print(f"临时邮箱: {address}")
    print(f"手机号: {phone}")

    client = AmdClient(captcha_solver=solver, proxy=args.proxy)
    try:
        print(f"人机验证场景: {client.captcha_scene().get('SceneId', '(取不到)')}")
        result = client.register_with_mailbox(
            mailbox, phone_number=phone, code_timeout=args.mail_timeout,
        )
    except CaptchaRequired as exc:
        print(f"停在人机验证: {exc}", file=sys.stderr)
        print("这不是 bug —— 阿里云验证的判定权在服务端，还叠了设备/行为画像，"
              "本地算不出来。用 --captcha-param 传一次有效验证结果可继续。",
              file=sys.stderr)
        return 3
    except (AmdError, MailboxError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1

    print(f"注册提交成功: {result.message or '(无消息)'}")
    print(f"  邮箱: {result.email}")
    print(f"  手机: {result.phone_number}")
    if args.save_email:
        with open(args.save_email, "w", encoding="utf-8") as fh:
            fh.write(f"{result.email}\t{phone}\n")
        print(f"邮箱+手机已写入 {args.save_email}")
    return 0


def cmd_amd_session(args) -> int:
    """真人过验证 → 自动取票 → 自动注册 → 查额度。一条命令走完。

    这是 README 里那句话的工程化版本（「在你自己的浏览器里过完验证、
    把 Param 抓出来喂进来，这条路径可以全自动走完」）：

    1. 开一个**有头**浏览器，把注册表单填好（随机邮箱 + 随机手机号）；
    2. 把人机验证弹窗主动拉出来，**剩下就等人拖一下滑块**；
    3. SDK 的 success 回调一触发，立刻拿 Param 去换 `CaptchaTicket` 并落盘；
    4. 之后发码、收信、提交注册、查额度全自动，不用再碰浏览器。

    为什么非要人拖一下：`src/browser.py` 用实测说明了容器里全自动过不去
    （`F015` → `F001`，卡在服务端设备/行为画像）。卡住的从来不是拖动距离，
    而是「谁在拖」。人只花几秒，剩下的交给程序 —— 这就是人在环路
    （human-in-the-loop），不是破解。

    票据能复用：同一出口 IP 的有效期内，后面的注册直接读盘里的票，
    不用再过验证。`--reuse` 可以跳过浏览器只用存票。
    """
    from .amd import AmdClient, AmdError, CaptchaRequired, StaticCaptchaSolver
    from .mailbox import MailboxError, MailboxPool
    from .phone import random_cn_mobile
    from .ticket import Ticket, clear_ticket, load_ticket, save_ticket

    phone = args.phone or random_cn_mobile()
    client = AmdClient(proxy=args.proxy)

    # ---------- 1. 拿一张票：优先复用存票，其次开浏览器等人 ----------
    param = ""
    reused = None
    if args.reuse:
        reused = load_ticket()
        if not reused or not reused.alive():
            print("盘里没有可用票据（不存在或已过期），去掉 --reuse 重新过一次验证。",
                  file=sys.stderr)
            return 3
        param = reused.ticket
        print(f"复用票据: {reused.describe()}（{reused.age():.0f}s 前拿到的）")
    else:
        from .browser import BrowserUnavailable, human_captcha_session

        print("开一个有头浏览器，请在弹出的窗口里把滑块拖过去……")
        print("（这一步故意不自动：自动拖动正是被判 F001 的原因）")
        try:
            info = __import__("asyncio").run(human_captcha_session(
                email=args.email or "",
                phone=phone,
                executable_path=args.browser_path,
                proxy=args.proxy,
                stealth=not args.no_stealth,
                timeout=args.captcha_timeout,
                screenshot_dir=args.screenshot_dir,
            ))
        except BrowserUnavailable as exc:
            print(f"无法驱动浏览器: {exc}", file=sys.stderr)
            return 2

        param = info.get("param") or ""
        if not param:
            print(f"没等到验证通过的 Param（{args.captcha_timeout}s 超时）。", file=sys.stderr)
            if info.get("screenshot"):
                print(f"当时的页面截图: {info['screenshot']}", file=sys.stderr)
            return 3
        print("✅ 验证通过，拿到 Param")

    # ---------- 2. 拿 Param 换 CaptchaTicket，立刻落盘 ----------
    scene = client.captcha_scene()
    ticket = ""
    try:
        ticket = client.verify_captcha(param, "register_send_code", args.email or "")
    except AmdError as exc:
        print(f"换票失败: {exc}", file=sys.stderr)
    if not ticket:
        print("验证结果被服务端拒了（VerifyResult=False）。"
              "再用一次浏览器，或换个出口 IP 重试。", file=sys.stderr)
        return 3
    print("✅ 换到 CaptchaTicket")

    if not args.reuse:
        save_ticket(Ticket(
            ticket=ticket, email=args.email or "", phone=phone,
            proxy=args.proxy, created_at=__import__("time").time(),
            ttl=args.ticket_ttl, scene_id=str(scene.get("SceneId") or ""),
        ))
        print(f"票据已存盘（TTL {args.ticket_ttl}s），后面 --reuse 可复用")

    # ---------- 3. 全自动：发码 → 收信 → 注册 ----------
    preferred = [args.mail_domain] if args.mail_domain else ["clowmail.com"]
    pool = MailboxPool(preferred_domains=preferred)
    try:
        mailbox, _ = pool.acquire()
    except MailboxError as exc:
        print(f"收信后端不可用: {exc}", file=sys.stderr)
        return 1
    if mailbox is None:
        print("所有候选域名都建不出收件箱，收信后端可能整体不可用。", file=sys.stderr)
        return 1

    address = mailbox.address
    print(f"临时邮箱: {address}")
    print(f"手机号: {phone}")

    client.captcha = StaticCaptchaSolver(ticket)
    try:
        result = client.register_with_mailbox(
            mailbox, phone_number=phone, code_timeout=args.mail_timeout,
        )
    except CaptchaRequired as exc:
        print(f"票据在服务端被拒: {exc}", file=sys.stderr)
        print("多半是过期了 —— 去掉 --reuse 重新过一次验证。", file=sys.stderr)
        clear_ticket()
        return 3
    except (AmdError, MailboxError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1

    print(f"注册提交成功: {result.message or '(无消息)'}")
    print(f"  邮箱: {result.email}")
    print(f"  手机: {result.phone_number}")
    if args.save_email:
        with open(args.save_email, "a", encoding="utf-8") as fh:
            fh.write(f"{result.email}\t{phone}\n")
        print(f"邮箱+手机已追加到 {args.save_email}")

    # 试着领额度：注册完通常要登录一次才看得到余额，失败了不算致命。
    try:
        creds = client.login_by_code(result.email, "")
        print(f"额度摘要: {creds if creds else '(登录未返回数据)'}")
    except Exception:
        pass
    return 0


def cmd_amd_ticket(args) -> int:
    """看一眼本地那张票还在不在、还有多久过期。"""
    from .ticket import TICKET_FILE, clear_ticket, load_ticket

    if args.clear:
        print("已删除" if clear_ticket() else "本就没有票据")
        return 0
    t = load_ticket()
    if not t:
        print(f"没有票据（{TICKET_FILE} 不存在）")
        return 1
    print(f"票据: {t.describe()}")
    print(f"  绑定邮箱: {t.email or '(未记录)'}")
    print(f"  场景: {t.scene_id or '(未记录)'}   TTL: {t.ttl}s")
    print(f"  剩余: {max(0, int(t.expires_at - __import__('time').time()))}s")
    return 0 if t.alive() else 1


def cmd_amd_bruteclose(args) -> int:
    """穷举「攻破阿里云智能验证」的七个向量族，逐条真跑，然后封口。

    这是对老板那句「想多种办法攻破」的**完备性回答**：不是再试一条路，
    而是把客户端可控范围内的路**列全**，跑一遍，标出哪条根本走不通。

    它和 `amd-spoof` 的区别：`amd-spoof` 是「伪装服务器」这一条思路的实现，
    这里是**七个互不重叠向量族**的清单 + 封口判定。

    不发码、不注册时的向量走真实接口但无副作用；`--ordvd` 会额外起一次
    真浏览器抓 SDK 采集的环境明文（这是「本地能不能自造 deviceToken」的
    最直接的试金石）。
    """
    from .amd import AmdClient
    from .bruteclose import (
        ClosureReport,
        assess_replay,
        assess_sdk_patch,
        assess_token_forge,
        probe_api_skip,
        probe_header_spoof,
        probe_ordvd_direct,
        probe_param_forge,
    )

    client = AmdClient(proxy=args.proxy)
    report = ClosureReport()

    print("=== 穷举「攻破阿里云智能验证」七个向量族 ===\n")

    print("① param_forge —— 伪造 CaptchaVerifyParam 直接喂验证接口")
    for it in probe_param_forge(client, target=args.email or "spoof@example.com"):
        report.add(it)
        print(f"   [{it.attempt}] → {it.observed}")

    print("\n② header_spoof —— 伪装来源头（XFF 阿里云 IP / 内网）")
    for it in probe_header_spoof(client, target=args.email or "spoof@example.com"):
        report.add(it)
        print(f"   [{it.attempt}] → {it.observed}")

    ordvd: dict = {}
    if args.ordvd:
        print("\n③ ordvd_direct —— 把 SDK 采集的环境明文当 param 喂（先抓 __ORDVD）")
        try:
            from .browser import capture_ordvd
            cap = asyncio.run(capture_ordvd(
                executable_path=args.browser_path, proxy=args.proxy,
                stealth=not args.no_stealth,
            ))
            token_item = assess_token_forge(cap.get("crypt_keys") or [])
            ordvd = cap.get("ordvd") or {}
            print(f"   抓到 __ORDVD：{cap.get('ordvd_keys')} 个字段；"
                  f"__ALIYUN_CRYPT 无密钥={not cap.get('crypt_has_key')}")
            for it in probe_ordvd_direct(ordvd, client,
                                         target=args.email or "spoof@example.com"):
                report.add(it)
                print(f"   [{it.attempt}] → {it.observed}")
        except Exception as exc:
            print(f"   跳过（抓取失败）：{exc}")
            token_item = assess_token_forge([])
    else:
        print("\n③ ordvd_direct —— 跳过（加 --ordvd 会用真浏览器抓 __ORDVD）")
        token_item = assess_token_forge([])

    print("\n④ token_forge —— 本地伪造 deviceToken")
    report.add(token_item)
    print(f"   [{token_item.attempt}] → {token_item.observed}")
    print(f"     卡点：{token_item.blocker}")

    print("\n⑤ replay —— 重放一个成功的 param")
    rep = assess_replay(has_success_sample=False)
    report.add(rep)
    print(f"   [{rep.attempt}] → {rep.observed}")
    print(f"     卡点：{rep.blocker}")

    print("\n⑥ api_skip —— 跳过验证直接调发码/注册接口")
    for it in probe_api_skip(client, email=args.email or "skip@example.com"):
        report.add(it)
        print(f"   [{it.attempt}] → {it.observed}")

    print("\n⑦ sdk_patch —— 改页面里 SDK 的判定函数（让客户端认为通过）")
    pat = assess_sdk_patch()
    report.add(pat)
    print(f"   [{pat.attempt}] → {pat.observed}")
    print(f"     卡点：{pat.blocker}")

    print("\n" + "=" * 62)
    print(report.conclusion())
    print("=" * 62)
    if not report.passed:
        print("\n" + report.missing_key_evidence())
    print("\n原始数据（含判定字段，便于复核）：")
    print(json.dumps([i.as_dict() for i in report.items], ensure_ascii=False, indent=2))
    return 0 if not report.passed else 2


def cmd_amd_spoof(args) -> int:
    """实测「伪装成阿里云的服务器」能不能绕过人机验证。

    这是对老板那句具体要求的**可复现回答**：三条做法逐条真打接口跑一遍，
    把结果摊开。不发码、不注册，无副作用。

    结论（实测）：**都不能。** 判定权和密钥都在阿里云服务端，
    「伪装它的服务器」要在**别人的出网链路**上做 MITM —— 客户端够不着。
    详见 `src/spoof.py` 的模块说明。
    """
    from .amd import AMD_ORIGIN, API_PREFIX
    from .spoof import (
        probe_forged_tickets,
        probe_spoofed_headers,
        probe_local_response_spoof,
        summarize_findings,
    )

    verify_url = f"{AMD_ORIGIN}{API_PREFIX}/Aliyun/VerifyIntelligentCaptcha"
    findings = []

    print("① 伪造票据（直接给 AMD 的验证接口塞假 param）……")
    for f in probe_forged_tickets(verify_url, target=args.email or "spoof@example.com"):
        print(f"   [{f.vector}] {f.detail}")
        print(f"     → {f.result}")
        findings.append(f)

    print("② 伪装请求来源（X-Forwarded-For 打成阿里云 IP / 内网）……")
    for f in probe_spoofed_headers(verify_url, target=args.email or "spoof@example.com"):
        print(f"   [{f.vector}] {f.detail}")
        print(f"     → {f.result}")
        findings.append(f)

    if args.browser:
        print("③ 本地伪装阿里云的响应（真浏览器拦截 SDK 请求，返回假成功）……")
        try:
            from .spoof import probe_local_response_spoof

            for f in probe_local_response_spoof(executable_path=args.browser_path):
                print(f"   [{f.vector}] {f.detail}")
                print(f"     → {f.result}")
                findings.append(f)
        except RuntimeError as exc:
            print(f"   ⚠️  跳过：{exc}")

    print()
    print("结论：" + summarize_findings(findings))
    print()
    print("根因：CaptchaVerifyParam 里的 deviceToken 由阿里云服务端用**它自己的密钥**")
    print("      加密，且 SDK 发给阿里云的验真请求带 HMAC-SHA1 签名。")
    print("      「伪装阿里云的服务器」= 在阿里云自己的出网链路上做 MITM，")
    print("      这件事发生在 AMD 服务端与阿里云之间，本地完全够不着。")
    return 0


def cmd_amd_probe(args) -> int:
    """只探测可行性，不产生任何副作用。

    打印：验证场景 id、发码接口是否要求人机验证、注册接口对手机号的强制程度；
    加 `--browser` 时再驱动一次真浏览器，报告人机验证到底卡在哪一步
    （拿到 Param / 被服务端拦截、拦截码是什么）。
    用来在动手之前把「哪一段能自动化」讲清楚，而不是靠猜。
    """
    from .amd import AmdClient, AmdError, CaptchaRequired

    client = AmdClient()
    scene = client.captcha_scene()
    print(f"人机验证: SceneId={scene.get('SceneId')} "
          f"有效期={scene.get('ExpireTimeSec')}s")

    try:
        client.request_code(args.email or "probe@example.com", kind=1, captcha_ticket="")
        print("发码接口: 无需人机验证 ✅")
    except CaptchaRequired:
        print("发码接口: **需要人机验证**（服务端返回「请完成人机验证后重试」）")
    except AmdError as exc:
        print(f"发码接口: 业务错误 {exc}")

    try:
        client.register(email=args.email or "probe@example.com", code="000000",
                        phone_number="")
        print("注册接口: 手机号可空 ✅")
    except AmdError as exc:
        if "手机号" in str(exc):
            print("注册接口: **手机号必填**（就算 VerificationMethod=email 也一样）")
        else:
            print(f"注册接口: 其他错误 {exc}")

    if args.browser:
        from .browser import probe_environment, BrowserUnavailable

        print("\n真浏览器探测中……")
        import asyncio

        try:
            info = asyncio.run(probe_environment(
                email=args.email or "probe@example.com",
                phone=args.phone or "",
                executable_path=args.browser_path,
                proxy=args.proxy,
                stealth=not args.no_stealth,
            ))
        except BrowserUnavailable as exc:
            print(f"  ❌ 浏览器不可用: {exc}")
            return 2
        print(f"  出口 IP: {info.get('exit_ip') or '(取不到)'}")
        print(f"  SDK 已挂载: {info['sdk'] == 'function'}")
        print(f"  拖动距离: {info['distance']:.1f}px")
        print(f"  拿到 Param: {'✅' if info['param'] else '❌'}")
        codes = info["codes"] or []
        print(f"  服务端返回码: {codes or '(无)'}")
        if not info["param"]:
            from .browser import _explain_codes

            print("  → " + _explain_codes(codes))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xstech-gateway",
        description="把 xstech.one 包装成 OpenAI 兼容端点，供终端与本地工具使用",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--base", default=os.environ.get("XSTECH_BASE", DEFAULT_BASE))
    p.add_argument("--email")
    p.add_argument("--password")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register", help="用临时邮箱自动注册一个账号")
    r.add_argument("--password", default=os.environ.get("XSTECH_PASSWORD", ""))
    r.add_argument("--mail-timeout", type=int, default=180)
    r.set_defaults(func=cmd_register, password=os.environ.get("XSTECH_PASSWORD", "Test123456"))

    m = sub.add_parser("models", help="列出可用模型")
    m.set_defaults(func=cmd_models)

    s = sub.add_parser("serve", help="启动本地 OpenAI 兼容端点")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--model", default="")
    s.add_argument("--api-key", default=os.environ.get("XSTECH_API_KEY", ""))
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("ask", help="直接在终端问一句")
    a.add_argument("prompt", nargs="+")
    a.add_argument("--model", default="")
    a.set_defaults(func=cmd_ask)

    l = sub.add_parser("login", help="用已有账号登录并缓存 token")
    l.add_argument("--email", required=True)
    l.add_argument("--password", required=True)
    l.set_defaults(func=cmd_login)

    w = sub.add_parser("whoami", help="查看当前账号")
    w.set_defaults(func=cmd_whoami)

    o = sub.add_parser("logout", help="清除本地凭据")
    o.set_defaults(func=cmd_logout)

    ar = sub.add_parser("amd-register", help="在 AMD 开发者计划上注册账号（收信自动、手机号随机）")
    ar.add_argument("--phone", default=os.environ.get("AMD_PHONE", ""),
                    help="手机号。不填则自动生成随机的中国大陆号码"
                         "（服务端强制必填，但选邮箱验证时不校验归属）")
    ar.add_argument("--captcha-param", default=os.environ.get("AMD_CAPTCHA_PARAM", ""),
                    help="一次有效的阿里云 CaptchaVerifyParam（从真实浏览器里过完验证抓出来）")
    ar.add_argument("--browser", action="store_true",
                    help="起真浏览器驱动人机验证（拿不到 Param 时会诚实报停）")
    ar.add_argument("--headful", action="store_true",
                    help="浏览器显示窗口（需要 X 显示，比如 xvfb-run）")
    ar.add_argument("--browser-path", default="",
                    help="chromium/chrome 可执行文件路径（默认用 playwright 自带的）")
    ar.add_argument("--browser-attempts", type=int, default=3)
    ar.add_argument("--mail-domain", default="",
                    help="首选收件域名，默认 clowmail.com；被拦会自动换其它域名")
    ar.add_argument("--mail-timeout", type=int, default=180)
    ar.add_argument("--proxy", default=os.environ.get("AMD_PROXY", ""),
                    help="出口 IP 伪装，如 socks5://user:pass@host:port 或 http://host:port。"
                         "注意：光换 IP 不足以过验证，见 README「关于 IP」一节")
    ar.add_argument("--no-stealth", action="store_true",
                    help="关掉反自动化指纹伪装（只为复现「裸 Playwright 被怎么拦」，一般不用）")
    ar.add_argument("--save-email", default="")
    ar.set_defaults(func=cmd_amd_register)

    ap = sub.add_parser("amd-probe", help="探测 AMD 链路的可行性（无副作用）")
    ap.add_argument("--email", default="")
    ap.add_argument("--phone", default="")
    ap.add_argument("--browser", action="store_true", help="同时驱动一次真浏览器探测人机验证")
    ap.add_argument("--browser-path", default="", help="chromium/chrome 可执行文件路径")
    ap.add_argument("--proxy", default=os.environ.get("AMD_PROXY", ""),
                    help="出口 IP 伪装（用于检验「换个 IP 有没有用」）")
    ap.add_argument("--no-stealth", action="store_true",
                    help="关掉反自动化指纹伪装")
    ap.set_defaults(func=cmd_amd_probe)

    ase = sub.add_parser(
        "amd-session",
        help="真人过验证 + 全自动注册（人在环路，剩下的交给程序）",
    )
    ase.add_argument("--email", default="", help="注册用邮箱（默认随机生成）")
    ase.add_argument("--phone", default=os.environ.get("AMD_PHONE", ""),
                     help="手机号（服务端强制必填且不校验归属，默认随机）")
    ase.add_argument("--browser-path", default="", help="chromium/chrome 可执行文件路径")
    ase.add_argument("--captcha-timeout", type=int, default=300,
                     help="等人过验证的秒数上限")
    ase.add_argument("--screenshot-dir", default="",
                     help="把验证弹窗截图存到这里（远程/无头排查用）")
    ase.add_argument("--ticket-ttl", type=int, default=240,
                     help="存盘票据的有效期秒数（保守值，服务端另有真实过期）")
    ase.add_argument("--reuse", action="store_true",
                     help="跳过浏览器，直接用盘里的票据（有效期内可连续注册）")
    ase.add_argument("--mail-domain", default="", help="指定收件箱域名（默认 clowmail.com）")
    ase.add_argument("--mail-timeout", type=int, default=180, help="等注册验证码的秒数")
    ase.add_argument("--proxy", default=os.environ.get("AMD_PROXY", ""),
                     help="出口 IP 伪装（协议层与浏览器层都走）")
    ase.add_argument("--no-stealth", action="store_true",
                     help="关掉反自动化指纹伪装（仅复现问题时用）")
    ase.add_argument("--save-email", default="", help="把邮箱+手机号追加到这个文件")
    ase.set_defaults(func=cmd_amd_session)

    atk = sub.add_parser("amd-ticket", help="查看/清除本地那张真人验证票据")
    atk.add_argument("--clear", action="store_true", help="删除票据文件")
    atk.set_defaults(func=cmd_amd_ticket)

    abc = sub.add_parser(
        "amd-bruteclose",
        help="穷举「攻破人机验证」的七个向量族并封口（无副作用，--ordvd 会起浏览器）",
    )
    abc.add_argument("--email", default="")
    abc.add_argument("--ordvd", action="store_true",
                     help="用真浏览器抓 SDK 采集的环境明文 __ORDVD（第 3/4 族的试金石）")
    abc.add_argument("--browser-path", default="", help="chromium/chrome 可执行文件路径")
    abc.add_argument("--proxy", default=os.environ.get("AMD_PROXY", ""),
                     help="出口 IP（检验「换 IP 有没有用」）")
    abc.add_argument("--no-stealth", action="store_true", help="关掉反自动化指纹伪装")
    abc.set_defaults(func=cmd_amd_bruteclose)

    asp = sub.add_parser("amd-spoof", help="实测「伪装阿里云服务器」能否绕过人机验证（无副作用）")
    asp.add_argument("--email", default="")
    asp.add_argument("--browser", action="store_true",
                     help="额外做一次真浏览器的本地响应伪装实验")
    asp.add_argument("--browser-path", default="", help="chromium/chrome 可执行文件路径")
    asp.set_defaults(func=cmd_amd_spoof)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UpstreamError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
