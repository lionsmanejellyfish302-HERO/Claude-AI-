import hashlib

from nas115sync.config import Config
from nas115sync.hashing import PREID_SIZE, hash_file, sha1_range
from nas115sync.paths import IgnoreMatcher, cloud_norm, norm_key
from nas115sync.savedata import SavedataDetector


def test_hash_file_md5_sha1_preid(tmp_path):
    data = b"x" * (PREID_SIZE + 12345)
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    h = hash_file(str(p))
    assert h.size == len(data)
    assert h.md5 == hashlib.md5(data).hexdigest()
    assert h.sha1 == hashlib.sha1(data).hexdigest().upper()
    assert h.preid == hashlib.sha1(data[:PREID_SIZE]).hexdigest().upper()


def test_small_file_preid_is_whole_file(tmp_path):
    p = tmp_path / "s.bin"
    p.write_bytes(b"abc")
    h = hash_file(str(p))
    assert h.preid == h.sha1


def test_sha1_range_inclusive(tmp_path):
    p = tmp_path / "r.bin"
    p.write_bytes(bytes(range(100)))
    assert sha1_range(str(p), 10, 19) == hashlib.sha1(bytes(range(10, 20))).hexdigest().upper()


def test_norm_key_case_and_unicode():
    assert norm_key("Photos\\Café.JPG") == norm_key("photos/Café.jpg")
    assert cloud_norm("備份\\NAS//Photos/") == "/備份/NAS/Photos"


def test_ignore_matcher():
    m = IgnoreMatcher(["Thumbs.db", "~$*", "@eaDir"])
    assert m.match_name("thumbs.DB")
    assert m.match_rel("a/@eaDir/b.jpg")
    assert m.match_name("~$報告.docx")
    assert not m.match_rel("a/b.jpg")


def test_savedata_detector():
    d = SavedataDetector(["SaveData", "Saves"], [".sav"])
    assert d.group_of("Games/X/SaveData/slot1/data.bin") == "Games/X/SaveData"
    assert d.group_of("Games/Y/profile.SAV") == "Games/Y"
    assert d.group_of("Docs/readme.txt") is None
    assert d.group_of("GameA/slot.dat", whole_mapping=True) == "GameA"
    assert d.group_of("root.dat", whole_mapping=True) == ""


def test_config_roundtrip(tmp_path):
    c = Config(tmp_path / "c.json")
    m = c.add_mapping("\\\\NAS\\home\\Photos", "備份/Photos")
    c.confirm_mapping(m.id, "123")
    c.settings.savedata_keep_versions = 3
    c.save()
    c2 = Config(tmp_path / "c.json")
    assert c2.mappings[0].cloud_path == "/備份/Photos"
    assert c2.mappings[0].confirmed and c2.mappings[0].cloud_cid == "123"
    assert c2.settings.savedata_keep_versions == 3
    assert c2.active_mappings()[0].id == m.id


# ------------------------------------------------------------------ 介面用的小功能
def test_fake_cloud_copy_space_and_path():
    from nas115sync.cloud.fake import FakeCloud

    c = FakeCloud()
    c.put_file("/a/b/x.txt", b"xyz")
    b = c.resolve_path("/a/b")
    dest = c.ensure_path("/複製到")
    c.copy([b], dest)
    assert set(c.files_in("/複製到/b")) == {"x.txt"} and set(c.files_in("/a/b")) == {"x.txt"}
    assert c.path_of(b) == [(c.resolve_path("/a"), "a"), (b, "b")] and c.path_of("0") == []
    total, used = c.space_info()
    assert used == 6 and total > used


def test_transfer_batches_group_manual_and_sync_transfers():
    from nas115sync.transfers import TransferManager

    mgr = TransferManager()
    background = mgr.start("upload", "m1", "即時.txt", 1)
    manual_batch = mgr.begin_batch("upload", "上傳 2 個項目", "D:/src", "/dest")
    sync_batch = mgr.begin_batch("sync", "同步", mapping_ids=["m1"])
    a = mgr.start("upload", "", "D:/src/a.txt", 1)
    b = mgr.start("upload", "m1", "b.txt", 1)
    c = mgr.start("upload", "m2", "c.txt", 1)
    assert (background.t.batch, a.t.batch, b.t.batch, c.t.batch) == (0, manual_batch, sync_batch, 0)
    mgr.end_batch(manual_batch)
    assert mgr.start("upload", "", "later.txt", 1).t.batch == 0  # 工作結束後的傳輸不再歸給它
    assert {x["id"]: x["status"] for x in mgr.batches()} == {manual_batch: "done", sync_batch: "running"}
    for t in (background, a, b, c):
        t.finish("done")
    mgr.end_batch(sync_batch, "failed")
    mgr.clear_finished()
    assert [x["rel_path"] for x in mgr.snapshot()] == ["later.txt"] and mgr.batches() == []


def test_copy_transfers_are_not_rate_limited():
    from nas115sync.transfers import TransferManager

    mgr = TransferManager(upload_kb=1, download_kb=1)
    assert mgr.start("copy", "", "x", 1).limiter is mgr.no_limiter
    assert mgr.start("upload", "", "x", 1).limiter is mgr.up_limiter


def test_schedule_next_run_and_last_report(env):
    import time

    from nas115sync.scheduler import Scheduler, describe_when, new_schedule, next_run

    now = time.mktime((2026, 11, 10, 12, 0, 0, 0, 0, -1))  # 星期二中午
    s = new_schedule("03:00", [0, 2])  # 週一、週三
    assert describe_when(s) == "每週一、三 03:00"
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(next_run(s, now))) == "2026-11-11 03:00"
    s["time"] = "13:30"
    s["days"] = [1]
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(next_run(s, now))) == "2026-11-10 13:30"
    assert next_run(dict(s, enabled=False), now) is None and next_run(dict(s, days=[]), now) is None
    assert next_run(dict(s, time="亂填"), now) is None

    env.write("a.txt", "A")
    sched = new_schedule("03:00", action="sync")
    env.config.settings.schedules.append(sched)
    Scheduler(env.engine).run_schedule(sched)
    assert sched["last_ok"] is True and "上傳 1" in sched["last_report"] and sched["last_run"] > 0
    saved = type(env.config)(env.config.path).settings.schedules[0]
    assert saved["last_report"] == sched["last_report"]  # 結果有存檔，重新開啟程式後仍看得到


def test_search_local_index(env):
    env.write("相片/2024/旅行_東京.jpg", "x")
    env.write("文件/100%_完成.txt", "y")
    env.sync()
    assert [r.rel_path for r in env.db.search_local("東京")] == ["相片/2024/旅行_東京.jpg"]
    assert [r.rel_path for r in env.db.search_local("100%")] == ["文件/100%_完成.txt"]  # % 不是萬用字元
    assert env.db.search_local("_東") and not env.db.search_local("旅行?東京")
    assert len(env.db.search_local("", limit=1)) == 1


def test_new_settings_survive_reload_and_old_config(tmp_path):
    from nas115sync.config import Config

    cfg = Config(tmp_path / "c.json")
    m = cfg.add_mapping(r"\\NAS\home\Photos", "/備份/Photos")
    assert m.title == "Photos"
    m.name = "我的相片"
    cfg.settings.bookmarks.append({"name": "相片", "kind": "cloud", "account_id": "default", "path": "/備份"})
    cfg.settings.ui["view"] = "two"
    cfg.save()
    again = Config(tmp_path / "c.json")
    assert again.mappings[0].title == "我的相片" and again.settings.bookmarks[0]["path"] == "/備份"
    assert again.settings.ui == {"view": "two"} and again.settings.manual_conflict == "keep_both"
