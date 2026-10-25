"""遊戲存檔（savedata）偵測與遊戲程序偵測。

存檔以「群組」為單位核對與同步：
- 路徑中任一層資料夾名稱符合 savedata_dir_names（如 SaveData、Saves）→ 群組為該資料夾
- 檔案副檔名符合 savedata_extensions（如 .sav）→ 群組為檔案所在資料夾
- 對應本身標記為 savedata → 群組為第一層子資料夾（檔案位於根目錄時群組為根目錄 ""）
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys

from .paths import name_key


class SavedataDetector:
    def __init__(self, dir_names, extensions):
        self.dir_names = {name_key(d) for d in dir_names if d}
        self.exts = {("." + e.lstrip(".")).casefold() for e in extensions if e}

    def group_of(self, rel_path: str, whole_mapping: bool = False) -> str | None:
        parts = [p for p in rel_path.split("/") if p]
        if not parts:
            return None
        if whole_mapping:
            return parts[0] if len(parts) > 1 else ""
        for i, part in enumerate(parts[:-1]):
            if name_key(part) in self.dir_names:
                return "/".join(parts[: i + 1])
        ext = os.path.splitext(parts[-1])[1].casefold()
        if ext and ext in self.exts:
            return "/".join(parts[:-1])
        return None

    def is_group_dir(self, rel_dir: str, whole_mapping: bool = False) -> bool:
        parts = [p for p in rel_dir.split("/") if p]
        if whole_mapping:
            return len(parts) == 1
        return bool(parts) and name_key(parts[-1]) in self.dir_names


def running_process_names() -> set[str]:
    """目前執行中的程序名稱（小寫）。優先用 psutil，否則 Windows 用 tasklist。"""
    try:
        import psutil  # type: ignore

        return {(p.info.get("name") or "").casefold() for p in psutil.process_iter(["name"])}
    except ImportError:
        pass
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15,
            ).stdout
            return {row[0].casefold() for row in csv.reader(io.StringIO(out)) if row}
        out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, errors="replace",
                             timeout=15).stdout
        return {os.path.basename(line.strip()).casefold() for line in out.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def any_running(names) -> bool:
    wanted = {n.strip().casefold() for n in names if n and n.strip()}
    if not wanted:
        return False
    return bool(wanted & running_process_names())
