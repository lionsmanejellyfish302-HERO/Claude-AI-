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


def build_app(demo: bool = False) -> App:
    home = app_home()
    config = Config(home / "demo_config.json") if demo else Config()
    if demo:
        from .cloud.fake import FakeCloud

        db = Database(home / "demo_state.db")
        cloud: CloudClient = FakeCloud(home / "demo_cloud.json")
    else:
        from .cloud.open115 import Open115Client, TokenStore

        db = Database(home / "state.db")
        s = config.settings
        cloud = Open115Client(TokenStore(home / "tokens.dat"), client_id=s.client_id,
                              min_interval=s.api_min_interval, part_size=max(5, s.part_size_mb) * 1024 * 1024)
    return App(config, db, cloud, SyncEngine(config, db, cloud), demo)


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
