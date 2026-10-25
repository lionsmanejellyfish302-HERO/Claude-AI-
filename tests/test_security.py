"""資安：115 上的特殊名稱不能讓檔案寫到 NAS 資料夾以外、登入資訊不外洩、匯出報告不被當成公式執行。"""
import csv
import logging
import os
import stat
import sys

import pytest

from nas115sync import manual
from nas115sync.cloud import tokens
from nas115sync.cloud.tokens import TokenStore
from nas115sync.config import TWO_WAY
from nas115sync.paths import is_inside, unsafe_name

ROOT = "/備份/NAS"


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "a\\b", "..\\..\\x.txt", "C:\\Windows\\x.dll", "a:b.txt",
                                  "CON", "nul.txt", "COM1.log", "LPT9", "結尾空白 ", "結尾句點.", "a*b", "a?b",
                                  'a"b', "a<b", "a>b", "a|b", "控制\x01字元"])
def test_unsafe_names_are_rejected(name):
    assert unsafe_name(name)


@pytest.mark.parametrize("name", ["相片.jpg", "a.b.c", ".hidden", "報告 (1).docx", "CONSOLE.txt", "nulls.txt",
                                  "~$temp.docx", "#1 & 2.txt"])
def test_normal_names_are_allowed(name):
    assert unsafe_name(name) is None


def test_is_inside_blocks_traversal_and_links(tmp_path):
    root = tmp_path / "nas"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert is_inside(str(root), str(root / "sub" / "x.txt"))
    assert is_inside(str(root), str(root / "新資料夾" / "y.txt"))  # 尚不存在的路徑也能判斷
    assert not is_inside(str(root), os.path.join(str(root), "..", "outside", "x.txt"))
    assert not is_inside(str(root), str(tmp_path / "nas2" / "x.txt"))  # 前綴相同但不是同一個資料夾
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("無法建立符號連結")
    assert not is_inside(str(root), str(root / "link" / "x.txt"))


def test_unsafe_115_names_are_never_written(env):
    env.mapping.direction = TWO_WAY
    env.config.save()
    for name in ("..\\..\\逃脫.txt", "a:隱藏資料流.txt", "CON.txt", "結尾.", "正常.txt"):
        env.cloud.put_file(f"{ROOT}/{name}", name.encode())
    env.cloud.put_file(f"{ROOT}/../上層.txt", b"x")  # 名為 ".." 的 115 資料夾
    plans, stats = env.sync()
    written = sorted(str(p.relative_to(env.nas)) for p in env.nas.rglob("*") if p.is_file())
    assert written == ["正常.txt"]
    assert not any(p.name in ("逃脫.txt", "上層.txt") for p in env.nas.parent.rglob("*"))
    assert any("無法安全放到 NAS" in w for p in plans for w in p.warnings)


def test_download_refuses_to_follow_link_out_of_nas(env, tmp_path):
    env.mapping.direction = TWO_WAY
    env.config.save()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, env.nas / "捷徑", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("無法建立符號連結")
    env.cloud.put_file(f"{ROOT}/捷徑/x.txt", b"payload")
    env.sync()
    assert list(outside.iterdir()) == []


def test_manual_download_skips_unsafe_names(env, tmp_path):
    dest = tmp_path / "dl"
    dest.mkdir()
    env.cloud.put_file("/手動/..\\..\\逃脫.txt", b"x")
    env.cloud.put_file("/手動/好.txt", b"ok")
    folder = env.cloud.find_child(env.cloud.root_cid, "手動")
    stats = manual.download_entries(env.engine, "default", [folder], str(dest))
    assert stats["failed"] == 1 and stats["downloaded"] == 1
    assert sorted(p.name for p in dest.rglob("*") if p.is_file()) == ["好.txt"]


def test_csv_export_neutralizes_formulas(env, tmp_path):
    env.write("=HYPERLINK(1).txt", "x")
    env.write("+SUM(1).txt", "y")
    plans = env.engine.analyze()
    out = tmp_path / "r.csv"
    manual.export_plans_csv(env.engine, plans, str(out))
    cells = [c for row in csv.reader(out.open(encoding="utf-8-sig")) for c in row]
    assert "'=HYPERLINK(1).txt" in cells and "'+SUM(1).txt" in cells
    assert not any(c[:1] in "=+@" for c in cells if c)


def test_login_cookies_never_logged(tmp_path, caplog):
    from tests.test_web115 import COOKIES, Resp, make

    c = make(tmp_path, {("GET", "webapi.115.com/files"): lambda p, d, h: Resp(
        {"state": False, "errno": 990001, "error": "请重新登录"})})
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as ei:
            c.list_dir("0")
    text = caplog.text + str(ei.value)
    assert not any(v in text for v in COOKIES.values())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 以 DPAPI 加密")
def test_saved_login_is_private_file(tmp_path):
    store = TokenStore(tmp_path / "login.dat")
    store.save({"cookies": {"UID": "1"}})
    assert stat.S_IMODE(os.stat(tmp_path / "login.dat").st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 專用")
def test_dpapi_failure_never_writes_plaintext(tmp_path, monkeypatch):
    def fail(data, protect):
        raise OSError("DPAPI 無法使用")

    monkeypatch.setattr(tokens, "_dpapi", fail)
    with pytest.raises(OSError):
        TokenStore(tmp_path / "login.dat").save({"cookies": {"SEID": "SECRET"}})
    assert not (tmp_path / "login.dat").exists()


def test_log_redaction_filter():
    from nas115sync.app import RedactSecrets

    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "cookie UID=314159_R2_17929; SEID=%s userkey: %s",
                            ("s" * 40, "ABCDEFGH1234"), None)
    RedactSecrets().filter(rec)
    out = rec.getMessage()
    assert "s" * 40 not in out and "ABCDEFGH1234" not in out and "314159_R2" not in out
    assert "SEID=***" in out and "userkey: ***" in out
