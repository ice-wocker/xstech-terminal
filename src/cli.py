"""命令行入口。"""

from __future__ import annotations

import argparse
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
