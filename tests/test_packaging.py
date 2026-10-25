"""打包相關檔案：套件清單必須是純 ASCII（英文版 Windows 的工具會用 cp1252 讀取，中文會讀取失敗）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_requirement_files_are_ascii():
    for name in ("requirements.txt", "requirements-dev.txt"):
        (ROOT / name).read_bytes().decode("ascii")
