"""账号与凭据的本地保管。

凭据是账号密码派生的登录 token，等同账号权限，因此：
- 落盘在 ~/.config/xstech-gateway/ 且权限 0600
- 不进仓库、不进日志
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XSTECH_CONFIG_DIR", Path.home() / ".config" / "xstech-gateway"))
CRED_FILE = CONFIG_DIR / "credentials.json"


@dataclass
class StoredAccount:
    email: str
    password: str
    token: str = ""
    base: str = "https://xstech.one"
    model: str = ""


def save(account: StoredAccount) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CRED_FILE.write_text(json.dumps(asdict(account), ensure_ascii=False, indent=2))
    os.chmod(CRED_FILE, 0o600)
    return CRED_FILE


def load() -> StoredAccount | None:
    if not CRED_FILE.exists():
        return None
    try:
        data = json.loads(CRED_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    allowed = {f for f in StoredAccount.__dataclass_fields__}
    return StoredAccount(**{k: v for k, v in data.items() if k in allowed})


def clear() -> bool:
    if CRED_FILE.exists():
        CRED_FILE.unlink()
        return True
    return False
