"""115 連線測試：用你的 115 帳號實際走一遍所有會用到的功能，確認在你的電腦與網路上都能運作。

只在 115 上建立一個「NAS115Sync連線測試-時間」資料夾，測完整個刪除（進 115 回收站），不會動到其他檔案。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from typing import Callable

from .cloud.base import CloudClient, CloudError
from .hashing import hash_file

StepFn = Callable[[str, bool, str], None]


def run_check(cloud: CloudClient, report: StepFn | None = None) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    work = tempfile.mkdtemp(prefix="nas115sync_check_")
    folder_cid = None

    def step(name: str, fn) -> object:
        try:
            detail = fn()
            ok, text = True, str(detail if detail is not None else "")
        except NotImplementedError:
            ok, text, detail = True, "此雲端不支援（略過）", None
        except Exception as e:  # noqa: BLE001 - 每一步都要回報
            ok, text, detail = False, f"{type(e).__name__}: {e}", None
        results.append((name, ok, text))
        if report:
            report(name, ok, text)
        if not ok:
            raise _Stop
        return detail

    try:
        step("讀取帳號資訊", lambda: cloud.user_label() or "（無名稱）")
        step("列出 115 根目錄", lambda: f"{len(cloud.list_dir(cloud.root_cid))} 個項目")
        name = time.strftime("NAS115Sync連線測試-%Y%m%d-%H%M%S")
        folder_cid = step("建立測試資料夾", lambda: cloud.ensure_path("/" + name))
        src = os.path.join(work, "測試檔案.bin")
        with open(src, "wb") as f:  # 隨機內容：一定要實際上傳
            f.write(os.urandom(256 * 1024) + name.encode())
        h = hash_file(src)
        up = step("實際上傳（OSS）", lambda: cloud.upload(src, folder_cid, "測試檔案.bin", h))

        def instant():
            r = cloud.try_instant(src, folder_cid, "秒傳測試.bin", h)
            if r is None or not r.instant:
                raise CloudError("相同內容沒有觸發秒傳")
            return "相同內容第二次上傳：秒傳成功"

        step("秒傳", instant)

        def verify_list():
            files = {e.name: e for e in cloud.list_dir(folder_cid) if not e.is_dir}
            bad = [n for n in ("測試檔案.bin", "秒傳測試.bin") if n not in files or files[n].sha1 != h.sha1]
            if bad:
                raise CloudError(f"115 上的 SHA-1 與本機不符：{bad}")
            return f"SHA-1 {h.sha1} 一致"

        step("核對 115 上的 SHA-1", verify_list)

        def download():
            dest = os.path.join(work, "下載.bin")
            cloud.download(up.fid, up.pick_code, dest)
            got = hash_file(dest).sha1
            if got != h.sha1:
                raise CloudError(f"下載內容的 SHA-1 不符：{got}")
            return "下載並驗證 SHA-1 成功"

        step("下載", download)

        def rename():
            cloud.rename(up.fid, "已改名.bin")
            if "已改名.bin" not in {e.name for e in cloud.list_dir(folder_cid)}:
                raise CloudError("改名後找不到檔案")
            return "改名成功"

        step("改名", rename)

        def changes():
            items, cursor = cloud.changes(None)
            return f"可讀取操作紀錄（位置 {cursor}），115 電腦版上的變動會即時同步到 NAS"

        step("讀取 115 操作紀錄（即時偵測 115 端變動）", changes)
    except _Stop:
        pass
    finally:
        if folder_cid:
            try:
                cloud.delete([folder_cid])
                results.append(("刪除測試資料夾", True, "已移到 115 回收站"))
            except Exception as e:  # noqa: BLE001
                results.append(("刪除測試資料夾", False, f"請手動刪除：{e}"))
            if report:
                report(*results[-1])
        shutil.rmtree(work, ignore_errors=True)
    return results


class _Stop(Exception):
    pass
