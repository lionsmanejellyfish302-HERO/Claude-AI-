import os
import shutil

from nas115sync import compare as C
from nas115sync.hashing import hash_bytes
from nas115sync.paths import norm_key


def test_new_files_upload_with_instant_first(env):
    env.write("a.txt", "hello")
    env.write("sub/b.txt", "already on 115 somewhere")
    env.cloud.known_sha1.add(hash_bytes(b"already on 115 somewhere").sha1)

    plans, stats = env.sync()
    assert plans[0].summary()["uploads"] == 2
    assert stats["uploaded"] == 2 and stats["instant"] == 1
    assert env.cloud.instant_count == 1 and env.cloud.real_upload_count == 1
    assert set(env.cloud_files()) == {"a.txt"}
    assert set(env.cloud_files("sub")) == {"b.txt"}

    plans, stats = env.sync()
    assert plans[0].summary()["uploads"] == 0
    assert plans[0].identical == 2
    assert env.reviews() == []


def test_identical_existing_files_are_linked_not_uploaded(env):
    env.write("docs/x.pdf", b"PDF")
    env.cloud.put_file("/備份/NAS/docs/x.pdf", b"PDF")
    plans, stats = env.sync()
    assert stats["linked"] == 1 and stats["uploaded"] == 0
    assert env.cloud.real_upload_count == 0
    st = env.db.get_state(env.mapping.id, norm_key("docs/x.pdf"))
    assert st and st.local_sha1 == st.cloud_sha1


def test_same_name_different_content_needs_review(env):
    env.write("a.txt", "NAS version")
    env.cloud.put_file("/備份/NAS/a.txt", b"115 version")
    plans, stats = env.sync()
    assert stats["uploaded"] == 0
    [rv] = env.reviews(C.CONFLICT)
    assert rv.details["local"]["sha1"] == hash_bytes(b"NAS version").sha1
    assert rv.details["cloud"][0]["sha1"] == hash_bytes(b"115 version").sha1

    env.engine.apply_review(rv.id, "upload_replace")
    files = env.cloud_files()
    assert list(files) == ["a.txt"]
    assert files["a.txt"].sha1 == hash_bytes(b"NAS version").sha1
    assert env.reviews() == []


def test_conflict_keep_both_renames_cloud_copy(env):
    env.write("a.txt", "NAS version")
    env.cloud.put_file("/備份/NAS/a.txt", b"115 version")
    env.sync()
    [rv] = env.reviews(C.CONFLICT)
    env.engine.apply_review(rv.id, "upload_keep_both")
    files = env.cloud_files()
    assert files["a.txt"].sha1 == hash_bytes(b"NAS version").sha1
    old = [n for n in files if n.startswith("a (115舊版")]
    assert len(old) == 1 and files[old[0]].sha1 == hash_bytes(b"115 version").sha1
    plans, _ = env.sync()
    assert env.reviews() == []


def test_keep_cloud_then_local_edit_syncs(env):
    env.write("a.txt", "NAS v1")
    env.cloud.put_file("/備份/NAS/a.txt", b"115 version")
    env.sync()
    [rv] = env.reviews(C.CONFLICT)
    env.engine.apply_review(rv.id, "keep_cloud")
    plans, stats = env.sync()
    assert stats["uploaded"] == 0 and env.reviews() == []

    env.write("a.txt", "NAS v2")
    plans, stats = env.sync()
    assert stats["uploaded"] == 1
    files = env.cloud_files()
    assert list(files) == ["a.txt"] and files["a.txt"].sha1 == hash_bytes(b"NAS v2").sha1


def test_linked_file_modified_replaces_cloud_version(env):
    env.write("notes.txt", "v1")
    env.sync()
    env.write("notes.txt", "v2")
    plans, stats = env.sync()
    [task] = plans[0].uploads
    assert task.reason == "modified" and len(task.replace_fids) == 1
    files = env.cloud_files()
    assert list(files) == ["notes.txt"]
    assert files["notes.txt"].sha1 == hash_bytes(b"v2").sha1


def test_strict_mode_reviews_linked_changes(env):
    env.config.settings.auto_update_linked = False
    env.write("notes.txt", "v1")
    env.sync()
    env.write("notes.txt", "v2")
    plans, stats = env.sync()
    assert stats["uploaded"] == 0
    assert len(env.reviews(C.CONFLICT)) == 1


def test_duplicate_hash_needs_review(env):
    env.write("x/photo.jpg", b"JPEGDATA")
    env.sync()
    env.write("y/photo_copy.jpg", b"JPEGDATA")
    plans, stats = env.sync()
    assert stats["uploaded"] == 0
    [rv] = env.reviews(C.DUPLICATE)
    assert rv.rel_path == "y/photo_copy.jpg"
    assert rv.details["matches"]
    assert not rv.details["move_candidate"]  # 原檔仍在，不是搬移
    env.engine.apply_review(rv.id, "upload")
    assert env.cloud.instant_count == 1  # 相同內容 → 秒傳
    assert "photo_copy.jpg" in env.cloud_files("y")


def test_two_new_identical_local_files_both_reviewed(env):
    env.write("a/1.bin", b"same")
    env.write("b/2.bin", b"same")
    plans, stats = env.sync()
    assert stats["uploaded"] == 0
    assert {r.rel_path for r in env.reviews(C.DUPLICATE)} == {"a/1.bin", "b/2.bin"}


def test_same_name_elsewhere_needs_review(env):
    env.write("2023/report.docx", "old year")
    env.sync()
    env.write("2024/report.docx", "new year")
    plans, stats = env.sync()
    [rv] = env.reviews(C.SAME_NAME)
    assert rv.rel_path == "2024/report.docx"
    env.engine.apply_review(rv.id, "upload")
    assert "report.docx" in env.cloud_files("2024")


def test_ignore_patterns_skip_junk(env):
    env.write("Thumbs.db", "junk")
    env.write("@eaDir/x/SYNOINDEX", "junk")
    env.write("real.txt", "data")
    plans, stats = env.sync()
    assert set(env.cloud_files()) == {"real.txt"}


def test_permanent_ignore(env):
    env.write("a.txt", "NAS")
    env.cloud.put_file("/備份/NAS/a.txt", b"115")
    env.sync()
    [rv] = env.reviews(C.CONFLICT)
    env.engine.apply_review(rv.id, "ignore")
    env.sync()
    assert env.reviews() == []


def test_savedata_requires_approval_then_steam_like_sync(env):
    env.write("Games/Elden Ring/SaveData/ER0000.sl2", "save v1")
    env.write("Games/Elden Ring/SaveData/settings.ini", "cfg")
    plans, stats = env.sync()
    assert stats["uploaded"] == 0
    [rv] = env.reviews(C.SAVEDATA_GROUP)
    assert rv.rel_path == "Games/Elden Ring/SaveData"
    assert rv.details["file_count"] == 2

    env.engine.apply_review(rv.id, "approve", {"process_names": ["eldenring.exe"]})
    files = env.cloud_files("Games/Elden Ring/SaveData")
    assert set(files) == {"ER0000.sl2", "settings.ini"}
    g = env.db.get_group(env.mapping.id, "Games/Elden Ring/SaveData")
    assert g.status == "approved" and g.process_names == ["eldenring.exe"]

    # 遊戲存檔更新 → 上傳新版、刪除 115 舊版
    env.write("Games/Elden Ring/SaveData/ER0000.sl2", "save v2")
    env.engine.sync_savedata_group(env.mapping, "Games/Elden Ring/SaveData")
    files = env.cloud_files("Games/Elden Ring/SaveData")
    assert set(files) == {"ER0000.sl2", "settings.ini"}
    assert files["ER0000.sl2"].sha1 == hash_bytes(b"save v2").sha1

    # NAS 端刪除存檔 → 115 同步刪除
    os.remove(env.nas / "Games/Elden Ring/SaveData/settings.ini")
    env.engine.sync_savedata_group(env.mapping, "Games/Elden Ring/SaveData")
    assert set(env.cloud_files("Games/Elden Ring/SaveData")) == {"ER0000.sl2"}
    assert env.reviews() == []


def test_savedata_keep_versions(env):
    env.config.settings.savedata_keep_versions = 2
    env.write("Saves/slot.sav", "s1")
    env.sync()
    [rv] = env.reviews(C.SAVEDATA_GROUP)
    assert rv.rel_path == "Saves"
    env.engine.apply_review(rv.id, "approve")
    for v in ("s2", "s3", "s4"):
        env.write("Saves/slot.sav", v)
        env.engine.sync_savedata_group(env.mapping, "Saves")
    files = env.cloud_files("Saves")
    assert files["slot.sav"].sha1 == hash_bytes(b"s4").sha1
    versions = env.cloud_files("Saves/.savedata_versions")
    assert len(versions) == 2
    assert {e.sha1 for e in versions.values()} == {hash_bytes(b"s2").sha1, hash_bytes(b"s3").sha1}
    # 歷史版本資料夾不應出現在比對結果中
    plans, _ = env.sync()
    assert plans[0].cloud_only == [] and env.reviews() == []


def test_savedata_exclude_and_normal(env):
    env.write("Saves/a.sav", "x")
    env.sync()
    [rv] = env.reviews(C.SAVEDATA_GROUP)
    env.engine.apply_review(rv.id, "exclude")
    env.sync()
    assert env.cloud_files("Saves") == {} and env.reviews() == []

    env.db.upsert_group(env.mapping.id, "Saves", "normal")
    env.sync()
    assert set(env.cloud_files("Saves")) == {"a.sav"}


def test_local_delete_requires_review(env):
    env.write("a.txt", "x")
    env.write("b.txt", "y")
    env.sync()
    os.remove(env.nas / "a.txt")
    env.sync()
    [rv] = env.reviews(C.LOCAL_DELETED)
    assert "a.txt" in env.cloud_files()
    env.engine.apply_review(rv.id, "delete_cloud")
    assert set(env.cloud_files()) == {"b.txt"}


def test_local_delete_auto_policy(env):
    env.config.settings.deletion_policy = "auto"
    env.write("a.txt", "x")
    env.write("b.txt", "y")
    env.sync()
    os.remove(env.nas / "a.txt")
    plans, stats = env.sync()
    assert stats["deleted"] == 1
    assert set(env.cloud_files()) == {"b.txt"}


def test_mass_delete_guard(env):
    env.config.settings.deletion_policy = "auto"
    for i in range(25):
        env.write(f"d/{i}.txt", str(i))
    env.sync()
    shutil.rmtree(env.nas / "d")
    plans, stats = env.sync()
    assert stats["deleted"] == 0
    [rv] = env.reviews(C.MASS_DELETE)
    assert rv.details["count"] == 25
    assert len(env.cloud_files("d")) == 25
    env.engine.apply_review(rv.id, "keep_all")
    assert len(env.cloud_files("d")) == 25
    assert env.db.states(env.mapping.id) == {}


def test_nas_offline_never_deletes(env):
    env.config.settings.deletion_policy = "auto"
    env.write("a.txt", "x")
    env.sync()
    env.nas.rename(env.nas.with_name("nas_offline"))
    plans, stats = env.sync()
    assert plans[0].warnings and stats["deleted"] == 0
    assert "a.txt" in env.cloud_files()


def test_cloud_changed_needs_review(env):
    env.write("a.txt", "v1")
    env.sync()
    fid = env.cloud_files()["a.txt"].fid
    env.cloud.delete([fid])
    env.cloud.put_file("/備份/NAS/a.txt", b"edited on 115")
    env.sync()
    [rv] = env.reviews(C.CLOUD_CHANGED)
    env.engine.apply_review(rv.id, "keep_cloud")
    env.sync()
    assert env.reviews() == []


def test_cloud_deleted_needs_review(env):
    env.write("a.txt", "v1")
    env.sync()
    env.cloud.delete([env.cloud_files()["a.txt"].fid])
    env.sync()
    [rv] = env.reviews(C.CLOUD_DELETED)
    env.engine.apply_review(rv.id, "upload")
    assert "a.txt" in env.cloud_files()


def test_cloud_duplicate_names(env):
    env.write("a.txt", "same")
    env.cloud.put_file("/備份/NAS/a.txt", b"same")
    env.cloud.put_file("/備份/NAS/a.txt", b"other")
    env.sync()
    [rv] = env.reviews(C.CLOUD_DUPS)
    env.engine.apply_review(rv.id, "delete_extra")
    files = env.cloud_files()
    assert len(files) == 1 and files["a.txt"].sha1 == hash_bytes(b"same").sha1


def test_replace_restores_name_when_cloud_auto_renames(env_rename):
    env = env_rename
    env.write("a.txt", "v1")
    env.sync()
    env.write("a.txt", "v2")
    env.sync()
    files = env.cloud_files()
    assert list(files) == ["a.txt"]
    assert files["a.txt"].sha1 == hash_bytes(b"v2").sha1


def test_move_detected_in_full_scan(env):
    env.write("old/report.pdf", b"PDF-CONTENT")
    env.sync()
    os.makedirs(env.nas / "new")
    os.rename(env.nas / "old/report.pdf", env.nas / "new/report.pdf")
    env.sync()
    [rv] = env.reviews(C.DUPLICATE)
    assert rv.details["move_candidate"]["from_rel"] == "old/report.pdf"
    uploads_before = env.cloud.real_upload_count + env.cloud.instant_count
    env.engine.apply_review(rv.id, "move_cloud")
    assert env.cloud.real_upload_count + env.cloud.instant_count == uploads_before
    assert "report.pdf" in env.cloud_files("new")
    assert env.cloud_files("old") == {}
    env.sync()
    assert env.reviews() == []


def test_realtime_change_delete_and_move(env):
    m = env.mapping
    env.sync()
    env.write("live/doc.txt", "v1")
    env.engine.handle_local_change(m, "live/doc.txt")
    assert "doc.txt" in env.cloud_files("live")

    env.write("live/doc.txt", "v2")
    env.engine.handle_local_change(m, "live/doc.txt")
    files = env.cloud_files("live")
    assert list(files) == ["doc.txt"] and files["doc.txt"].sha1 == hash_bytes(b"v2").sha1

    uploads = env.cloud.real_upload_count + env.cloud.instant_count
    os.rename(env.nas / "live/doc.txt", env.nas / "live/renamed.txt")
    env.engine.handle_local_move(m, "live/doc.txt", "live/renamed.txt")
    assert list(env.cloud_files("live")) == ["renamed.txt"]
    assert env.cloud.real_upload_count + env.cloud.instant_count == uploads

    os.rename(env.nas / "live", env.nas / "moved_dir")
    env.engine.handle_local_move(m, "live", "moved_dir")
    assert list(env.cloud_files("moved_dir")) == ["renamed.txt"]
    assert env.cloud.real_upload_count + env.cloud.instant_count == uploads

    os.remove(env.nas / "moved_dir/renamed.txt")
    env.engine.handle_local_delete(m, "moved_dir/renamed.txt")
    [rv] = env.reviews(C.LOCAL_DELETED)
    assert rv.rel_path == "moved_dir/renamed.txt"
    plans, _ = env.sync()
    assert [r.rel_path for r in env.reviews()] == ["moved_dir/renamed.txt"]


def test_realtime_new_folder_moved_in(env):
    env.sync()
    outside = env.nas.parent / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "inner" / "f.txt").write_text("x")
    shutil.move(str(outside), str(env.nas / "outside"))
    env.engine.handle_local_change(env.mapping, "outside")
    assert "f.txt" in env.cloud_files("outside/inner")


def test_empty_dirs_created(env):
    (env.nas / "empty" / "nested").mkdir(parents=True)
    plans, stats = env.sync()
    assert stats["mkdirs"] == 2
    assert env.cloud.resolve_path("/備份/NAS/empty/nested")


def test_cloud_only_reported(env):
    env.cloud.put_file("/備份/NAS/only_on_115.txt", b"x")
    plans, _ = env.sync()
    assert [c.rel_path for c in plans[0].cloud_only] == ["only_on_115.txt"]


def test_error_review_and_retry(env, monkeypatch):
    env.write("a.txt", "x")
    original = env.cloud.upload

    def boom(*a, **k):
        raise RuntimeError("網路斷線")

    monkeypatch.setattr(env.cloud, "upload", boom)
    plans, stats = env.sync()
    assert stats["failed"] == 1
    [rv] = env.reviews(C.ERROR)
    monkeypatch.setattr(env.cloud, "upload", original)
    env.engine.apply_review(rv.id, "retry")
    assert "a.txt" in env.cloud_files()


def test_unconfirmed_mapping_is_not_synced(env):
    env.config.unconfirm_mapping(env.mapping.id)
    env.write("a.txt", "x")
    plans, stats = env.sync()
    assert plans == [] and env.cloud_files() == {}


def test_cross_mapping_duplicate(env, tmp_path):
    other = tmp_path / "nas2"
    other.mkdir()
    (other / "dup.bin").write_bytes(b"SAME")
    m2 = env.config.add_mapping(str(other), "/備份/NAS2")
    env.config.confirm_mapping(m2.id, env.cloud.ensure_path("/備份/NAS2"))
    env.write("dup.bin", b"SAME")
    env.sync()
    assert {(r.mapping_id, r.rel_path) for r in env.reviews(C.DUPLICATE)} == {
        (env.mapping.id, "dup.bin"), (m2.id, "dup.bin")}
