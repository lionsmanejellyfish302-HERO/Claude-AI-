"""設定檔與路徑對應（Mapping）管理。

設定存放於 %APPDATA%\\NAS115Sync\\config.json（可用環境變數 NAS115SYNC_HOME 覆寫）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "NAS115Sync"

# NAS / Windows 常見的系統垃圾檔與暫存檔，預設不比對、不上傳
DEFAULT_IGNORE = [
    "Thumbs.db", "desktop.ini", ".DS_Store", "._*", "@eaDir", "#recycle", "#snapshot",
    "@Recycle", "@Recently-Snapshot", ".@__thumb", ".SynologyWorkingDirectory",
    "$RECYCLE.BIN", "System Volume Information", "~$*", "*.tmp", "*.part",
    "*.crdownload", "*.!qB", ".savedata_versions",
]

# 資料夾名稱符合任一項（不分大小寫）即視為遊戲存檔群組
DEFAULT_SAVEDATA_DIRS = [
    "savedata", "save data", "save", "saves", "savegame", "savegames", "saved games",
    "存檔", "存档",
]

# 副檔名符合任一項即視為遊戲存檔，群組為其所在資料夾
DEFAULT_SAVEDATA_EXTS = [".sav", ".save", ".sl2", ".ess", ".srm", ".rvdata2", ".rpgsave", ".state"]


def app_home() -> Path:
    env = os.environ.get("NAS115SYNC_HOME")
    if env:
        p = Path(env)
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    else:
        p = Path.home() / ".config" / "nas115sync"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Mapping:
    """一組 NAS 資料夾 ↔ 115 資料夾的對應。只有 confirmed=True（本人核對過）才會同步。"""

    local_path: str
    cloud_path: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    confirmed: bool = False
    confirmed_at: str = ""
    enabled: bool = True
    cloud_cid: str = ""
    # 整個對應都是遊戲存檔（例如 NAS 上的「Saved Games」）；每個第一層子資料夾為一個存檔群組
    savedata: bool = False
    note: str = ""

    @property
    def active(self) -> bool:
        return self.confirmed and self.enabled


@dataclass
class Settings:
    client_id: str = ""  # 115 開放平台 AppID
    # 已核對連結（有同步基準）的檔案，之後 NAS 端修改時自動上傳新版並刪除雲端舊版
    auto_update_linked: bool = True
    # NAS 端刪除檔案時：review=本人核對 / auto=自動刪除雲端(進115回收站) / ignore=不處理
    deletion_policy: str = "review"
    # 一次掃描出現大量刪除時（疑似 NAS 離線），一律暫停並要求本人確認
    mass_delete_guard_ratio: float = 0.3
    mass_delete_guard_min: int = 20
    review_same_name_elsewhere: bool = True
    ignore_patterns: list = field(default_factory=lambda: list(DEFAULT_IGNORE))
    savedata_dir_names: list = field(default_factory=lambda: list(DEFAULT_SAVEDATA_DIRS))
    savedata_extensions: list = field(default_factory=lambda: list(DEFAULT_SAVEDATA_EXTS))
    # 存檔群組最後一次變動後需靜置多少秒才上傳（模擬 Steam 在遊戲結束後才同步）
    savedata_quiet_seconds: int = 60
    # 上傳新存檔後雲端舊版處理：0=直接刪除；N>0=移到 .savedata_versions 保留 N 份
    savedata_keep_versions: int = 0
    savedata_versions_dir: str = ".savedata_versions"
    savedata_mirror_deletes: bool = True
    stable_seconds: float = 5.0
    rescan_interval_minutes: int = 15
    use_polling_observer: bool = False
    polling_interval_seconds: int = 30
    upload_workers: int = 2
    hash_workers: int = 2
    api_min_interval: float = 0.4
    part_size_mb: int = 20
    # 先對所有待上傳檔案嘗試秒傳，再處理需實際上傳的檔案
    instant_first: bool = True
    browser_path: str = ""


def _filtered(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in names}


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_home() / "config.json"
        self.settings = Settings()
        self.mappings: list[Mapping] = []
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.settings = Settings(**_filtered(Settings, data.get("settings", {})))
            self.mappings = [Mapping(**_filtered(Mapping, m)) for m in data.get("mappings", [])]

    def save(self) -> None:
        with self._lock:
            data = {
                "settings": asdict(self.settings),
                "mappings": [asdict(m) for m in self.mappings],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def get_mapping(self, mapping_id: str) -> Mapping | None:
        return next((m for m in self.mappings if m.id == mapping_id), None)

    def add_mapping(self, local_path: str, cloud_path: str, savedata: bool = False) -> Mapping:
        from .paths import cloud_norm

        with self._lock:
            m = Mapping(local_path=local_path, cloud_path=cloud_norm(cloud_path), savedata=savedata)
            self.mappings.append(m)
            self.save()
            return m

    def remove_mapping(self, mapping_id: str) -> None:
        with self._lock:
            self.mappings = [m for m in self.mappings if m.id != mapping_id]
            self.save()

    def confirm_mapping(self, mapping_id: str, cid: str) -> None:
        with self._lock:
            m = self.get_mapping(mapping_id)
            if m is None:
                raise KeyError(mapping_id)
            m.confirmed = True
            m.cloud_cid = cid
            m.confirmed_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save()

    def unconfirm_mapping(self, mapping_id: str) -> None:
        with self._lock:
            m = self.get_mapping(mapping_id)
            if m:
                m.confirmed = False
                m.confirmed_at = ""
                self.save()

    def active_mappings(self) -> list[Mapping]:
        return [m for m in self.mappings if m.active]
