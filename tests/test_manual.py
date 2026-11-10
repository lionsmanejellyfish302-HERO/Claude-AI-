import csv

from nas115sync import manual
from nas115sync.hashing import hash_bytes

ROOT = "/備份/NAS"


def test_upload_paths_folder_with_instant_and_conflicts(env, tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("A")
    (src / "sub" / "b.txt").write_text("B")
    env.cloud.known_sha1.add(hash_bytes(b"A").sha1)
    target = env.cloud.ensure_path("/手動")
    stats = manual.upload_paths(env.engine, "default", [str(src)], target)
    assert stats == {"instant": 1, "uploaded": 1, "skipped": 0, "failed": 0, "not_instant": 0}
    assert set(env.cloud.files_in("/手動/src")) == {"a.txt"}
    assert set(env.cloud.files_in("/手動/src/sub")) == {"b.txt"}

    # 再傳一次：相同內容略過；同名不同內容依選擇處理
    (src / "a.txt").write_text("A2")
    stats = manual.upload_paths(env.engine, "default", [str(src / "a.txt")], env.cloud.resolve_path("/手動/src"),
                                conflict=manual.KEEP_BOTH)
    names = set(env.cloud.files_in("/手動/src"))
    assert "a.txt" in names and any(n.startswith("a (NAS") for n in names)
    stats = manual.upload_paths(env.engine, "default", [str(src / "sub" / "b.txt")],
                                env.cloud.resolve_path("/手動/src/sub"))
    assert stats["skipped"] == 1


def test_upload_paths_overwrite(env, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("old")
    target = env.cloud.ensure_path("/手動")
    manual.upload_paths(env.engine, "default", [str(f)], target)
    f.write_text("new")
    manual.upload_paths(env.engine, "default", [str(f)], target, conflict=manual.OVERWRITE)
    entries = env.cloud.list_dir(target)
    assert [e.sha1 for e in entries] == [hash_bytes(b"new").sha1]


def test_download_entries_folder(env, tmp_path):
    env.cloud.put_file("/雲端/資料夾/a.txt", b"A")
    env.cloud.put_file("/雲端/資料夾/深/b.txt", b"B")
    folder = env.cloud.find_child(env.cloud.resolve_path("/雲端"), "資料夾")
    dest = tmp_path / "dl"
    dest.mkdir()
    stats = manual.download_entries(env.engine, "default", [folder], str(dest))
    assert stats["downloaded"] == 2
    assert (dest / "資料夾" / "深" / "b.txt").read_bytes() == b"B"
    stats = manual.download_entries(env.engine, "default", [folder], str(dest))
    assert stats["skipped"] == 2


def test_compare_folders(env, tmp_path):
    d = tmp_path / "cmp"
    d.mkdir()
    (d / "same.txt").write_text("S")
    (d / "diff.txt").write_text("D1")
    (d / "nas.txt").write_text("N")
    env.cloud.put_file("/c/same.txt", b"S")
    env.cloud.put_file("/c/diff.txt", b"D2")
    env.cloud.put_file("/c/cloud.txt", b"C")
    r = manual.compare_folders(env.engine, str(d), "default", env.cloud.resolve_path("/c"))
    assert r == {"same.txt": "same", "diff.txt": "different", "nas.txt": "nas_only", "cloud.txt": "cloud_only"}


def test_duplicate_report_and_delete(env):
    env.write("a/1.jpg", b"PIC")
    env.write("b/2.jpg", b"PIC")
    env.cloud.put_file(f"{ROOT}/x/1.jpg", b"PIC")
    env.cloud.put_file(f"{ROOT}/y/1.jpg", b"PIC")
    env.engine.analyze()
    [g] = manual.duplicate_report(env.engine)
    assert g["nas_count"] == 2 and g["cloud_count"] == 2 and g["wasted"] == 6
    extra = [i for i in g["items"] if i["where"] == "115" and i["rel_path"] == "y/1.jpg"]
    assert manual.delete_cloud_items(env.engine, extra) == 1
    assert env.cloud_files("y") == {}


def test_export_csv(env, tmp_path):
    env.write("a.txt", "x")
    env.cloud.put_file(f"{ROOT}/a.txt", b"different")
    env.cloud.put_file(f"{ROOT}/only.txt", b"o")
    env.write("new.txt", "n")
    plans = env.engine.analyze()
    out = tmp_path / "report.csv"
    n = manual.export_plans_csv(env.engine, plans, str(out))
    rows = list(csv.reader(out.open(encoding="utf-8-sig")))
    assert n == len(rows) - 1
    kinds = {r[2] for r in rows[1:]}
    assert {"待上傳", "待核對：同檔名內容不同", "僅 115 有"} <= kinds


def test_recycle_bin(tmp_path):
    f = tmp_path / "trash_me.txt"
    f.write_text("bye")
    manual.send_to_recycle_bin(str(f))
    assert not f.exists()


def test_upload_instant_only_never_uploads_real_data(env, tmp_path):
    """只秒傳：115 上已有相同內容的檔案秒傳，其餘的不上傳、也不算失敗。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "known.bin").write_bytes(b"already on 115")
    (src / "new.bin").write_bytes(b"only on NAS")
    env.cloud.known_sha1.add(hash_bytes(b"already on 115").sha1)
    target = env.cloud.ensure_path("/只秒傳")
    stats = manual.upload_paths(env.engine, "default", [str(src / "known.bin"), str(src / "new.bin")], target,
                                instant_only=True)
    assert stats == {"instant": 1, "uploaded": 0, "skipped": 0, "failed": 0, "not_instant": 1}
    assert set(env.cloud.files_in("/只秒傳")) == {"known.bin"} and env.cloud.real_upload_count == 0


def test_copy_local_folders_and_conflicts(env, tmp_path):
    src, dest = tmp_path / "來源", tmp_path / "目的"
    (src / "子").mkdir(parents=True)
    dest.mkdir()
    (src / "a.txt").write_text("A")
    (src / "子" / "b.txt").write_text("B")
    stats = manual.copy_local(env.engine, [str(src)], str(dest))
    assert stats == {"copied": 2, "skipped": 0, "failed": 0}
    assert (dest / "來源" / "子" / "b.txt").read_text() == "B" and (src / "a.txt").exists()
    # 再複製一次：內容相同的略過
    assert manual.copy_local(env.engine, [str(src)], str(dest))["skipped"] == 2
    # 同名不同內容：保留兩者／略過／覆蓋（舊檔進資源回收筒）
    (src / "a.txt").write_text("A2")
    manual.copy_local(env.engine, [str(src / "a.txt")], str(dest / "來源"), conflict=manual.KEEP_BOTH)
    names = sorted(p.name for p in (dest / "來源").iterdir() if p.is_file())
    assert len(names) == 2 and (dest / "來源" / "a.txt").read_text() == "A"
    assert manual.copy_local(env.engine, [str(src / "a.txt")], str(dest / "來源"), conflict=manual.SKIP) == {
        "copied": 0, "skipped": 1, "failed": 0}
    manual.copy_local(env.engine, [str(src / "a.txt")], str(dest / "來源"), conflict=manual.OVERWRITE)
    assert (dest / "來源" / "a.txt").read_text() == "A2"
    # 不能把資料夾複製到它自己裡面
    assert manual.copy_local(env.engine, [str(src)], str(src / "子"))["failed"] == 1
    assert any(t["kind"] == "copy" and t["status"] == "done" for t in env.engine.transfers.snapshot())


def test_move_local(env, tmp_path):
    src, dest = tmp_path / "來源", tmp_path / "目的"
    (src / "子").mkdir(parents=True)
    dest.mkdir()
    (src / "a.txt").write_text("A")
    (src / "子" / "b.txt").write_text("B")
    stats = manual.copy_local(env.engine, [str(src)], str(dest), move=True)
    assert stats == {"copied": 2, "skipped": 0, "failed": 0}
    assert (dest / "來源" / "子" / "b.txt").read_text() == "B" and not src.exists()


def test_transfer_between_two_115_accounts(env, tmp_path):
    from nas115sync.cloud.fake import FakeCloud

    other = FakeCloud()
    env.engine.clouds["other"] = other
    env.cloud.put_file("/來源/資料夾/a.txt", b"AAA")
    env.cloud.put_file("/來源/b.txt", b"BBB")
    other.known_sha1.add(hash_bytes(b"AAA").sha1)  # 另一個帳號的 115 上已有相同內容：秒傳
    items = env.cloud.list_dir(env.cloud.resolve_path("/來源"))
    stats = manual.transfer_between_accounts(env.engine, "default", items, "other", other.ensure_path("/收"),
                                             work_dir=str(tmp_path))
    assert stats == {"instant": 1, "uploaded": 1, "skipped": 0, "failed": 0}
    assert set(other.files_in("/收")) == {"b.txt"} and set(other.files_in("/收/資料夾")) == {"a.txt"}
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("nas115sync_transfer_")]  # 暫存已清除
