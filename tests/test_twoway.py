import os
import time

import pytest

from nas115sync import compare as C
from nas115sync.cloud.fake import FakeCloud
from nas115sync.config import DOWNLOAD, TWO_WAY
from nas115sync.hashing import hash_bytes
from nas115sync.scheduler import Scheduler, new_schedule
from nas115sync.transfers import RateLimiter, TransferManager

ROOT = "/備份/NAS"


def set_mode(env, direction, **kw):
    env.mapping.direction = direction
    for k, v in kw.items():
        setattr(env.mapping, k, v)
    env.config.save()


def trash_files(env):
    root = env.nas / ".nas115sync_trash"
    return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ---------------------------------------------------------------- 下載模式
def test_download_mode_mirrors_cloud(env):
    set_mode(env, DOWNLOAD)
    env.cloud.put_file(f"{ROOT}/docs/a.txt", b"from 115")
    env.cloud.put_file(f"{ROOT}/empty_dir_marker/b.bin", b"bbb")
    env.write("local_only.txt", "stays")
    plans, stats = env.sync()
    assert stats["downloaded"] == 2 and stats["uploaded"] == 0
    assert (env.nas / "docs/a.txt").read_bytes() == b"from 115"
    assert plans[0].local_only == ["local_only.txt"]
    assert "local_only.txt" not in env.cloud_files()
    plans, stats = env.sync()
    assert stats["downloaded"] == 0 and plans[0].identical == 2


def test_download_mode_cloud_change_replaces_local_with_backup(env):
    set_mode(env, DOWNLOAD)
    env.cloud.put_file(f"{ROOT}/a.txt", b"v1")
    env.sync()
    env.cloud.delete([env.cloud_files()["a.txt"].fid])
    env.cloud.put_file(f"{ROOT}/a.txt", b"v2 from 115")
    plans, stats = env.sync()
    assert stats["downloaded"] == 1
    assert (env.nas / "a.txt").read_bytes() == b"v2 from 115"
    assert trash_files(env) == ["a.txt"]  # 舊版可復原


def test_download_mode_cloud_delete_review_and_trash(env):
    set_mode(env, DOWNLOAD)
    env.cloud.put_file(f"{ROOT}/a.txt", b"v1")
    env.sync()
    env.cloud.delete([env.cloud_files()["a.txt"].fid])
    env.sync()
    [rv] = env.reviews(C.CLOUD_DELETED)
    assert ("delete_local", "刪除 NAS 檔案（移到可復原資料夾）") in C.available_decisions(rv.kind, rv.details)
    env.engine.apply_review(rv.id, "delete_local")
    assert not (env.nas / "a.txt").exists() and trash_files(env) == ["a.txt"]


def test_download_mode_auto_delete(env):
    set_mode(env, DOWNLOAD, delete_mode="auto")
    env.cloud.put_file(f"{ROOT}/a.txt", b"v1")
    env.cloud.put_file(f"{ROOT}/b.txt", b"v1b")
    env.sync()
    env.cloud.delete([env.cloud_files()["a.txt"].fid])
    plans, stats = env.sync()
    assert stats["local_deleted"] == 1 and not (env.nas / "a.txt").exists()


def test_download_mode_local_edit_is_not_overwritten(env):
    set_mode(env, DOWNLOAD)
    env.cloud.put_file(f"{ROOT}/a.txt", b"v1")
    env.sync()
    env.write("a.txt", "edited on NAS")
    env.sync()
    [rv] = env.reviews(C.CONFLICT)
    labels = dict(C.available_decisions(rv.kind, rv.details))
    assert "download_replace" in labels and "upload_replace" not in labels
    assert (env.nas / "a.txt").read_bytes() == b"edited on NAS"


def test_mass_cloud_delete_guard(env):
    set_mode(env, DOWNLOAD, delete_mode="auto")
    for i in range(25):
        env.cloud.put_file(f"{ROOT}/d/{i}.txt", str(i).encode())
    env.sync()
    for e in env.cloud_files("d").values():
        env.cloud.delete([e.fid])
    plans, stats = env.sync()
    assert stats["local_deleted"] == 0
    [rv] = env.reviews(C.MASS_DELETE)
    assert rv.details["side"] == "cloud" and rv.details["count"] == 25
    assert len(list((env.nas / "d").iterdir())) == 25


# ---------------------------------------------------------------- 雙向
def test_two_way_sync(env):
    set_mode(env, TWO_WAY)
    env.write("nas.txt", "from NAS")
    env.cloud.put_file(f"{ROOT}/cloud.txt", b"from 115")
    plans, stats = env.sync()
    assert stats["uploaded"] == 1 and stats["downloaded"] == 1
    assert (env.nas / "cloud.txt").read_bytes() == b"from 115"
    assert "nas.txt" in env.cloud_files()

    env.write("nas.txt", "NAS v2")
    env.cloud.delete([env.cloud_files()["cloud.txt"].fid])
    env.cloud.put_file(f"{ROOT}/cloud.txt", b"115 v2")
    plans, stats = env.sync()
    assert stats["uploaded"] == 1 and stats["downloaded"] == 1
    assert env.cloud_files()["nas.txt"].sha1 == hash_bytes(b"NAS v2").sha1
    assert (env.nas / "cloud.txt").read_bytes() == b"115 v2"
    assert env.reviews() == []


def test_two_way_both_changed_conflict_download_keep_both(env):
    set_mode(env, TWO_WAY)
    env.write("a.txt", "v1")
    env.sync()
    env.write("a.txt", "NAS edit")
    env.cloud.delete([env.cloud_files()["a.txt"].fid])
    env.cloud.put_file(f"{ROOT}/a.txt", b"115 edit")
    env.sync()
    [rv] = env.reviews(C.CONFLICT)
    env.engine.apply_review(rv.id, "download_keep_both")
    assert (env.nas / "a.txt").read_bytes() == b"115 edit"
    kept = [p for p in env.nas.iterdir() if p.name.startswith("a (NAS舊版")]
    assert len(kept) == 1 and kept[0].read_bytes() == b"NAS edit"
    plans, stats = env.sync()  # 改名保留的 NAS 舊版在雙向模式會上傳
    assert stats["uploaded"] == 1


def test_two_way_conflict_download_replace(env):
    set_mode(env, TWO_WAY)
    env.write("a.txt", "NAS")
    env.cloud.put_file(f"{ROOT}/a.txt", b"115")
    env.sync()
    [rv] = env.reviews(C.CONFLICT)
    env.engine.apply_review(rv.id, "download_replace")
    assert (env.nas / "a.txt").read_bytes() == b"115"
    assert trash_files(env) == ["a.txt"]


def test_download_copies_identical_nas_file_instead_of_downloading(env):
    set_mode(env, TWO_WAY)
    env.write("photos/original.jpg", b"JPEG" * 1000)
    env.sync()
    env.cloud.put_file(f"{ROOT}/backup/copy.jpg", b"JPEG" * 1000)
    env.sync()
    [rv] = env.reviews(C.DUPLICATE)
    assert rv.details["side"] == "cloud"
    assert [d for d, _ in C.available_decisions(rv.kind, rv.details)] == ["download", "skip", "ignore"]
    before = env.cloud.download_count
    env.engine.apply_review(rv.id, "download")
    assert (env.nas / "backup/copy.jpg").read_bytes() == b"JPEG" * 1000
    assert env.cloud.download_count == before  # 從 NAS 複製，沒有下載


def test_download_resume_and_corruption_recovery(env):
    set_mode(env, DOWNLOAD)
    data = os.urandom(300_000)
    env.cloud.put_file(f"{ROOT}/big.bin", data)
    (env.nas / "big.bin.115part").write_bytes(data[:100_000])  # 上次中斷留下的部分
    plans, stats = env.sync()
    assert (env.nas / "big.bin").read_bytes() == data
    assert not (env.nas / "big.bin.115part").exists()

    env.cloud.put_file(f"{ROOT}/other.bin", data[::-1])
    (env.nas / "other.bin.115part").write_bytes(b"garbage" * 1000)  # 錯誤的部分內容
    env.sync()
    assert (env.nas / "other.bin").read_bytes() == data[::-1]


def test_savedata_two_way_pulls_other_pc_save(env, monkeypatch):
    set_mode(env, TWO_WAY)
    env.write("Saves/slot1.sav", "pc1 v1")
    env.sync()
    [rv] = env.reviews(C.SAVEDATA_GROUP)
    env.engine.apply_review(rv.id, "approve", {"process_names": ["game.exe"]})
    # 另一台電腦上傳了新存檔
    env.cloud.delete([env.cloud_files("Saves")["slot1.sav"].fid])
    env.cloud.put_file(f"{ROOT}/Saves/slot1.sav", b"pc2 v2")
    monkeypatch.setattr("nas115sync.engine.any_running", lambda names: True)
    env.engine.sync_savedata_group(env.mapping, "Saves")
    assert (env.nas / "Saves/slot1.sav").read_bytes() == b"pc1 v1"  # 遊戲執行中不覆蓋
    monkeypatch.setattr("nas115sync.engine.any_running", lambda names: False)
    env.engine.sync_savedata_group(env.mapping, "Saves")
    assert (env.nas / "Saves/slot1.sav").read_bytes() == b"pc2 v2"


# ---------------------------------------------------------------- 篩選、版本、驗證
def test_filters_exclude_and_size(env):
    set_mode(env, "upload", exclude_patterns=["*.iso", "tmp"], max_size=1000)
    env.write("a.txt", "ok")
    env.write("disk.iso", "iso")
    env.write("tmp/x.txt", "in excluded folder")
    env.write("big.bin", b"x" * 2000)
    env.sync()
    assert set(env.cloud_files()) == {"a.txt"}
    assert env.cloud_files("tmp") == {}


def test_adding_filter_after_sync_never_deletes(env):
    env.config.settings.deletion_policy = "auto"
    env.write("keep.txt", "k")
    env.write("movie.iso", "m")
    env.write(".hidden", "h")
    env.sync()
    set_mode(env, "upload", exclude_patterns=["*.iso"], skip_hidden=True)
    plans, stats = env.sync()
    assert stats["deleted"] == 0
    assert {"keep.txt", "movie.iso", ".hidden"} <= set(env.cloud_files())


def test_keep_versions_for_normal_files(env):
    env.config.settings.keep_versions = 1
    env.write("doc.txt", "v1")
    env.sync()
    env.write("doc.txt", "v2")
    env.sync()
    env.write("doc.txt", "v3")
    env.sync()
    assert env.cloud_files()["doc.txt"].sha1 == hash_bytes(b"v3").sha1
    versions = env.cloud_files(".savedata_versions")
    assert [e.sha1 for e in versions.values()] == [hash_bytes(b"v2").sha1]


def test_upload_verification_failure_keeps_old_version(env, monkeypatch):
    env.write("a.txt", "v1")
    env.sync()
    env.write("a.txt", "v2")
    real = env.cloud.get_entry

    def corrupt(fid):
        e = real(fid)
        if e and not e.is_dir and e.sha1 == hash_bytes(b"v2").sha1:
            e.sha1 = "0" * 40
        return e

    monkeypatch.setattr(env.cloud, "get_entry", corrupt)
    plans, stats = env.sync()
    assert stats["failed"] == 1
    all_entries = env.cloud.list_dir(env.cloud.resolve_path(ROOT))
    assert hash_bytes(b"v1").sha1 in {e.sha1 for e in all_entries}  # 驗證失敗時舊版不會被刪除
    assert env.reviews(C.ERROR)


# ---------------------------------------------------------------- 多帳號、傳輸、排程
def test_second_account(env):
    other = FakeCloud()
    env.engine.clouds["acc2"] = other
    acc = env.config.add_account("第二個帳號")
    env.engine.clouds[acc.id] = env.engine.clouds.pop("acc2")
    tmp_dir = env.nas.parent / "nas_b"
    tmp_dir.mkdir()
    (tmp_dir / "b.txt").write_text("B")
    m2 = env.config.add_mapping(str(tmp_dir), "/B", account_id=acc.id)
    env.config.confirm_mapping(m2.id, other.ensure_path("/B"))
    env.sync()
    assert "b.txt" in other.files_in("/B")
    assert env.cloud.files_in("/B") == {}
    with pytest.raises(ValueError):
        env.config.remove_account(acc.id)


def test_transfer_queue_records_uploads(env):
    env.write("a.bin", os.urandom(50_000))
    env.sync()
    snap = env.engine.transfers.snapshot()
    assert any(t["rel_path"] == "a.bin" and t["status"] == "done" for t in snap)


def test_rate_limiter_and_pause():
    lim = RateLimiter(100)  # 100 KB/s
    lim.consume(100 * 1024)  # 第一秒額度
    t0 = time.monotonic()
    lim.consume(50 * 1024)
    assert time.monotonic() - t0 >= 0.4
    mgr = TransferManager()
    ctrl = mgr.start("upload", "m", "x", 10)
    mgr.pause_all()
    import threading

    threading.Timer(0.5, mgr.resume_all).start()
    t0 = time.monotonic()
    ctrl.advance(5)
    assert time.monotonic() - t0 >= 0.4 and ctrl.t.done == 5


def test_scheduler_runs_sync(env):
    s = new_schedule("03:00", days=[0], action="sync")
    env.config.settings.schedules = [s]
    sch = Scheduler(env.engine)
    monday_3am = time.strptime("2026-10-26 03:00", "%Y-%m-%d %H:%M")
    tuesday = time.strptime("2026-10-27 03:00", "%Y-%m-%d %H:%M")
    assert [x["id"] for x in sch.due(monday_3am)] == [s["id"]]
    assert sch.due(monday_3am) == []  # 同一分鐘只跑一次
    assert sch.due(tuesday) == []
    env.write("scheduled.txt", "x")
    stats = sch.run_schedule(s)
    assert stats["uploaded"] == 1 and "scheduled.txt" in env.cloud_files()
