"""組裝設定、資料庫、115 客戶端與同步引擎，並設定日誌。"""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import webbrowser
from dataclasses import dataclass

from .cloud.base import CloudClient
from .config import Config, app_home
from .db import Database
from .engine import SyncEngine


@dataclass
class App:
    config: Config
    db: Database
    cloud: CloudClient
    engine: SyncEngine
    demo: bool = False


def setup_logging(verbose: bool = False) -> None:
    log_dir = app_home() / "logs"
    log_dir.mkdir(exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_nas115", False) for h in root.handlers):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_dir / "nas115sync.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    fh._nas115 = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    if sys.stderr is not None:  # pythonw / 無主控台的 exe 沒有 stderr
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh._nas115 = True  # type: ignore[attr-defined]
        root.addHandler(sh)
    for noisy in ("urllib3", "oss2", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_client(config: Config, account_id: str, demo: bool = False) -> CloudClient:
    """每個 115 帳號一個客戶端（登入資訊分開保存）。"""
    home = app_home()
    suffix = "" if account_id == "default" else f"_{account_id}"
    if demo:
        from .cloud.fake import FakeCloud

        return FakeCloud(home / f"demo_cloud{suffix}.json")
    from .cloud.open115 import Open115Client, TokenStore

    s = config.settings
    c = Open115Client(TokenStore(home / f"tokens{suffix}.dat"), client_id=s.client_id,
                      min_interval=s.api_min_interval, part_size=max(5, s.part_size_mb) * 1024 * 1024)
    c.set_proxy(s.proxy)
    return c


def build_app(demo: bool = False) -> App:
    home = app_home()
    config = Config(home / "demo_config.json") if demo else Config()
    db = Database(home / ("demo_state.db" if demo else "state.db"))
    clouds = {a.id: make_client(config, a.id, demo) for a in config.accounts}
    engine = SyncEngine(config, db, clouds=clouds)
    return App(config, db, clouds["default"], engine, demo)


# 115 瀏覽器常見安裝位置（找不到時改用預設瀏覽器）
_115_BROWSER_CANDIDATES = [
    r"%LOCALAPPDATA%\115Chrome\Application\115chrome.exe",
    r"%ProgramFiles%\115Chrome\Application\115chrome.exe",
    r"%ProgramFiles(x86)%\115Chrome\Application\115chrome.exe",
]


def find_115_browser(configured: str = "") -> str | None:
    for cand in ([configured] if configured else []) + _115_BROWSER_CANDIDATES:
        p = os.path.expandvars(cand)
        if p and os.path.isfile(p):
            return p
    return None


def open_in_115_browser(url: str, configured: str = "") -> None:
    exe = find_115_browser(configured) if sys.platform == "win32" else None
    if exe:
        subprocess.Popen([exe, url], close_fds=True)
    else:
        webbrowser.open(url)
