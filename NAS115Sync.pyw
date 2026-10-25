"""雙擊啟動 NAS115Sync 圖形介面（不顯示主控台視窗）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas115sync.__main__ import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
