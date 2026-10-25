"""115 端（115 電腦版、App、網頁）的變動即時同步到 NAS：只重新讀取受影響的資料夾。"""
from concurrent.futures import ThreadPoolExecutor

from nas115sync import compare as C
from nas115sync.config import TWO_WAY, UPLOAD
from nas115sync.watcher import RealtimeSync

ROOT = "/備份/NAS"


def two_way(env, policy="auto"):
    env.mapping.direction = TWO_WAY
    env.config.settings.deletion_policy = policy
    env.config.save()
    env.write("相片/a.jpg", "photo")
    env.write("文件/b.txt", "doc")
    env.sync()
    cursor = env.cloud.changes(None)[1]
    return cursor


def apply(env, cursor):
    changes, cursor = env.cloud.changes(cursor)
    calls = env.cloud.api_calls
    n = env.engine.apply_cloud_changes("default", changes)
    return n, cursor, env.cloud.api_calls - calls


def test_new_file_in_115_appears_on_nas(env):
    cursor = two_way(env)
    env.cloud.put_file(f"{ROOT}/相片/從115電腦版.jpg", b"from 115 client")
    n, cursor, calls = apply(env, cursor)
    assert n == 1 and (env.nas / "相片" / "從115電腦版.jpg").read_bytes() == b"from 115 client"
    assert calls <= 3  # 只讀取受影響的資料夾，不做完整比對


def test_new_folder_tree_from_115_is_downloaded(env):
    cursor = two_way(env)
    env.cloud.put_file(f"{ROOT}/新專案/設計/圖.png", b"png")
    env.cloud.put_file(f"{ROOT}/新專案/說明.txt", b"readme")
    apply(env, cursor)
    assert (env.nas / "新專案" / "設計" / "圖.png").read_bytes() == b"png"
    assert (env.nas / "新專案" / "說明.txt").read_bytes() == b"readme"


def test_rename_and_delete_in_115(env):
    cursor = two_way(env, policy="auto")
    files = env.cloud_files("文件")
    env.cloud.rename(files["b.txt"].fid, "改名.txt")
    cursor = apply(env, cursor)[1]
    assert (env.nas / "文件" / "改名.txt").read_bytes() == b"doc"
    assert not (env.nas / "文件" / "b.txt").exists()  # 舊檔移到可復原的資料夾
    env.cloud.delete([env.cloud_files("相片")["a.jpg"].fid])
    apply(env, cursor)
    assert not (env.nas / "相片" / "a.jpg").exists()
    trash = env.nas / env.config.settings.local_trash_dir
    assert any(p.name == "a.jpg" for p in trash.rglob("*"))


def test_delete_in_115_needs_review_by_default(env):
    cursor = two_way(env, policy="review")
    env.cloud.delete([env.cloud_files("相片")["a.jpg"].fid])
    apply(env, cursor)
    assert (env.nas / "相片" / "a.jpg").exists()
    assert [r.kind for r in env.reviews()] == [C.CLOUD_DELETED]


def test_upload_only_mapping_ignores_115_changes(env):
    env.mapping.direction = UPLOAD
    env.config.save()
    env.write("a.txt", "x")
    env.sync()
    cursor = env.cloud.changes(None)[1]
    env.cloud.put_file(f"{ROOT}/只在115.txt", b"y")
    assert apply(env, cursor)[0] == 0
    assert not (env.nas / "只在115.txt").exists()


def _poll(rt):
    rt._pool = ThreadPoolExecutor(1)
    try:
        rt._poll_cloud_changes()
    finally:
        rt._pool.shutdown(wait=True)


def test_realtime_poll_uses_operation_log(env):
    two_way(env)
    rt = RealtimeSync(env.engine)
    _poll(rt)  # 第一次只記下位置
    env.cloud.put_file(f"{ROOT}/文件/新.txt", b"new")
    _poll(rt)
    assert (env.nas / "文件" / "新.txt").read_bytes() == b"new"


def test_realtime_poll_falls_back_to_recent_files(env, monkeypatch):
    two_way(env)

    def unsupported(cursor):
        raise NotImplementedError

    monkeypatch.setattr(env.cloud, "changes", unsupported)
    rt = RealtimeSync(env.engine)
    _poll(rt)
    env.cloud.put_file(f"{ROOT}/相片/新照片.jpg", b"jpg")
    _poll(rt)
    assert (env.nas / "相片" / "新照片.jpg").read_bytes() == b"jpg"


def test_rename_in_115_renames_on_nas_without_download(env):
    cursor = two_way(env, policy="review")
    downloads = env.cloud.download_count
    env.cloud.rename(env.cloud_files("文件")["b.txt"].fid, "改名.txt")
    apply(env, cursor)
    assert (env.nas / "文件" / "改名.txt").read_bytes() == b"doc" and not (env.nas / "文件" / "b.txt").exists()
    assert env.cloud.download_count == downloads and env.reviews() == []
    assert not (env.nas / env.config.settings.local_trash_dir).exists()
    plans, stats = env.sync()  # 完整比對也確認兩邊一致
    assert stats["uploaded"] == stats["downloaded"] == 0 and env.reviews() == []


def test_folder_rename_and_move_in_115(env):
    cursor = two_way(env, policy="review")
    env.write("相片/2024/旅行.jpg", "trip")
    env.sync()
    cursor = env.cloud.changes(None)[1]
    downloads = env.cloud.download_count
    photos = env.cloud.resolve_path(f"{ROOT}/相片")
    env.cloud.rename(photos, "照片")
    cursor = apply(env, cursor)[1]
    assert (env.nas / "照片" / "2024" / "旅行.jpg").read_bytes() == b"trip"
    assert not (env.nas / "相片").exists()
    docs = env.cloud.resolve_path(f"{ROOT}/文件")
    env.cloud.move([env.cloud_files("文件")["b.txt"].fid], env.cloud.resolve_path(f"{ROOT}/照片/2024"))
    apply(env, cursor)
    assert (env.nas / "照片" / "2024" / "b.txt").read_bytes() == b"doc"
    assert not (env.nas / "文件" / "b.txt").exists() and docs
    assert env.cloud.download_count == downloads and env.reviews() == []
    plans, stats = env.sync()
    assert stats["uploaded"] == stats["downloaded"] == 0 and env.reviews() == []


def test_115_rename_does_not_clobber_local_edit(env):
    cursor = two_way(env, policy="review")
    env.write("文件/b.txt", "NAS 上改過")
    env.cloud.rename(env.cloud_files("文件")["b.txt"].fid, "改名.txt")
    apply(env, cursor)
    assert (env.nas / "文件" / "b.txt").read_bytes() == "NAS 上改過".encode()
