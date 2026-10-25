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
    assert stats == {"instant": 1, "uploaded": 1, "skipped": 0, "failed": 0}
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
