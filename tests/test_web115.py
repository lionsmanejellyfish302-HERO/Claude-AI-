"""115 網盤客戶端（與 115 電腦版相同的介面）：以假的 115 伺服器測試每個請求的格式與回應處理。"""
import hashlib
import json
from urllib.parse import parse_qs

import pytest
import requests

from nas115sync.cloud import web115
from nas115sync.cloud import web115_codec as codec
from nas115sync.cloud.base import AuthError, NotFoundError
from nas115sync.cloud.tokens import TokenStore
from nas115sync.cloud.web115 import Web115Client
from nas115sync.hashing import hash_file, sha1_range

COOKIES = {"UID": "314159_R2_1792946228", "CID": "c" * 32, "SEID": "s" * 64, "KID": "k" * 32}


class Resp:
    def __init__(self, payload=None, status=200, content: bytes | None = None, chunks=None):
        self.status_code = status
        self.content = content if content is not None else json.dumps(payload).encode()
        self._chunks = chunks or []
        self.headers = {}

    def json(self):
        return json.loads(self.content)

    def iter_content(self, n):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def encrypted(payload) -> bytes:
    """模擬 115 加密回應：LZ4（只含字面值的區塊）→ 2 位元組長度 → AES。"""
    raw = json.dumps(payload).encode()
    n = len(raw)
    block = bytearray([min(n, 15) << 4])
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            block.append(255)
            rest -= 255
        block.append(rest)
    block += raw
    return codec.aes_encrypt(len(block).to_bytes(2, "little") + bytes(block))


class FakeSession:
    """依 (method, host+path) 路由的假 HTTP session，記錄所有請求。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.proxies = {}

    def request(self, method, url, params=None, data=None, headers=None, timeout=None, stream=False):
        key = (method, url.split("://", 1)[1].split("?", 1)[0])
        self.calls.append({"method": method, "key": key, "params": params or {}, "data": data, "headers": headers})
        if key not in self.routes:
            raise AssertionError(f"未預期的請求：{key}")
        return self.routes[key](params or {}, data, headers or {})

    def get(self, url, params=None, timeout=None, headers=None, stream=False):
        return self.request("GET", url, params=params, headers=headers)


def ok(**fields):
    return Resp({"state": True, "errno": 0, **fields})


def make(tmp_path, routes, logged_in=True):
    store = TokenStore(tmp_path / "login.dat")
    if logged_in:
        store.save({"cookies": COOKIES, "device": "alipaymini"})
    return Web115Client(store, min_interval=0, session=FakeSession(routes))


def file_item(fid, name, sha1, size=3, cid="100"):
    return {"fid": fid, "cid": cid, "n": name, "s": size, "sha": sha1, "pc": "pc" + fid, "te": 1700000000}


def dir_item(cid, name, pid="100"):
    return {"cid": cid, "pid": pid, "n": name, "te": 1700000000}


# ------------------------------------------------------------------ 登入
def test_qr_login_uses_separate_device_slot(tmp_path):
    seen = {}

    def scan(params, data, headers):
        seen["account"] = data["account"]
        return Resp({"state": 1, "data": {"cookie": COOKIES, "user_name": "測試者"}})

    c = make(tmp_path, {
        ("GET", "qrcodeapi.115.com/api/1.0/web/1.0/token/"): lambda p, d, h: Resp(
            {"state": 1, "data": {"uid": "QR1", "time": 1792946228, "sign": "SG", "qrcode": "https://115.com/scan/x"}}),
        ("GET", "qrcodeapi.115.com/get/status/"): lambda p, d, h: Resp({"state": 1, "data": {"status": 2}}),
        ("POST", "qrcodeapi.115.com/app/1.0/alipaymini/1.0/login/qrcode/"): scan,
    }, logged_in=False)
    assert not c.is_logged_in()
    login = c.start_qr_login()
    assert login.qrcode == "https://115.com/scan/x" and login.device == "alipaymini"
    assert c.poll_qr_status(login) == web115.QR_CONFIRMED
    c.finish_qr_login(login)
    assert seen["account"] == "QR1"
    assert c.is_logged_in() and c.user_id == 314159
    assert c.session.cookies.get("SEID", domain=".115.com") == "s" * 64
    saved = TokenStore(tmp_path / "login.dat").load()
    assert saved["cookies"]["UID"] == COOKIES["UID"] and saved["device"] == "alipaymini"
    # 不可使用 115 電腦版（Windows）的裝置欄位，否則會把使用者的 115 電腦版登出
    assert "os_windows" not in web115.LOGIN_DEVICES and "windows" not in web115.LOGIN_DEVICES


def test_logout_removes_saved_login(tmp_path):
    c = make(tmp_path, {})
    assert (tmp_path / "login.dat").exists()
    c.logout()
    assert not c.is_logged_in() and not (tmp_path / "login.dat").exists()
    with pytest.raises(AuthError):
        c.list_dir("0")


def test_expired_login_raises_auth_error(tmp_path):
    c = make(tmp_path, {("GET", "webapi.115.com/files"): lambda p, d, h: Resp(
        {"state": False, "errno": 990001, "error": "登录超时，请重新登录"})})
    with pytest.raises(AuthError, match="重新掃碼登入"):
        c.list_dir("0")


# ------------------------------------------------------------------ 檔案操作
def test_list_dir_paginates_and_parses(tmp_path, monkeypatch):
    monkeypatch.setattr(web115, "PAGE_LIMIT", 2)
    pages = {0: [dir_item("200", "子資料夾"), file_item("301", "a.txt", "AA" * 20)],
             2: [file_item("302", "b.txt", "bb" * 20, size=10)]}
    seen = []

    def files(params, data, headers):
        seen.append(params)
        return ok(cid="100", count=3, data=pages[params["offset"]])

    c = make(tmp_path, {("GET", "webapi.115.com/files"): files})
    entries = c.list_dir("100")
    assert [(e.name, e.is_dir, e.fid, e.pid) for e in entries] == [
        ("子資料夾", True, "200", "100"), ("a.txt", False, "301", "100"), ("b.txt", False, "302", "100")]
    assert entries[2].sha1 == "BB" * 20 and entries[2].size == 10 and entries[2].pick_code == "pc302"
    assert seen[0]["record_open_time"] == 0  # 不要改動使用者在 115 電腦版看到的「最近開啟」


def test_list_dir_detects_silent_root_fallback(tmp_path):
    c = make(tmp_path, {("GET", "webapi.115.com/files"): lambda p, d, h: ok(cid=0, count=0, data=[])})
    with pytest.raises(NotFoundError):
        c.list_dir("999")


def test_mkdir_rename_move_delete_payloads(tmp_path):
    c = make(tmp_path, {
        ("POST", "webapi.115.com/files/add"): lambda p, d, h: ok(cid="555", cname=d["cname"]),
        ("POST", "webapi.115.com/files/batch_rename"): lambda p, d, h: ok(),
        ("POST", "webapi.115.com/files/move"): lambda p, d, h: ok(),
        ("POST", "webapi.115.com/rb/delete"): lambda p, d, h: ok(),
    })
    assert c.mkdir("100", "新資料夾") == "555"
    c.rename("301", "新名稱.txt")
    c.move(["301", "302"], "555")
    c.delete(["301"], "100")
    calls = {k["key"][1]: k["data"] for k in c.session.calls}
    assert calls["webapi.115.com/files/add"] == {"pid": "100", "cname": "新資料夾"}
    assert calls["webapi.115.com/files/batch_rename"] == {"files_new_name[301]": "新名稱.txt"}
    assert calls["webapi.115.com/files/move"] == {"fid[0]": "301", "fid[1]": "302", "pid": "555"}
    assert calls["webapi.115.com/rb/delete"] == {"fid[0]": "301", "ignore_warn": 1, "pid": "100"}


def test_mkdir_existing_folder_returns_it(tmp_path):
    c = make(tmp_path, {
        ("POST", "webapi.115.com/files/add"): lambda p, d, h: Resp(
            {"state": False, "errno": 20004, "error": "该目录名称已存在。"}),
        ("GET", "webapi.115.com/files"): lambda p, d, h: ok(cid="100", count=1, data=[dir_item("777", "已有")]),
    })
    assert c.mkdir("100", "已有") == "777"


def test_resolve_path_with_getid(tmp_path):
    c = make(tmp_path, {("GET", "webapi.115.com/files/getid"): lambda p, d, h: ok(
        id="4321" if p["path"] == "/備份/相片" else 0)})
    assert c.resolve_path("/備份/相片") == "4321"
    assert c.resolve_path("/不存在") is None
    assert c.resolve_path("/") == "0"


def test_search(tmp_path):
    c = make(tmp_path, {("GET", "webapi.115.com/files/search"): lambda p, d, h: ok(
        count=1, data=[file_item("301", "報告.docx", "CC" * 20)])})
    res = c.search("報告", "100")
    assert [e.name for e in res] == ["報告.docx"]
    assert c.session.calls[0]["params"]["search_value"] == "報告"


def test_user_label(tmp_path):
    c = make(tmp_path, {("GET", "my.115.com/"): lambda p, d, h: ok(data={"user_name": "小明"})})
    assert c.user_label() == "小明（314159）"


# ------------------------------------------------------------------ 上傳／秒傳
def _decode_form(body: bytes) -> dict:
    return {k: v[0] for k, v in parse_qs(codec.aes_decrypt(body).decode("latin-1")).items()}


def test_upload_instant_with_sign_check(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(bytes(range(256)) * 10)
    h = hash_file(str(p))
    forms = []

    def init(params, data, headers):
        assert params["k_ec"] and headers["User-Agent"].startswith("Mozilla/5.0 115disk/")
        form = _decode_form(data)
        forms.append(form)
        if len(forms) == 1:
            return Resp(content=encrypted({"status": 7, "statuscode": 701, "sign_key": "SK", "sign_check": "10-19"}))
        return Resp(content=encrypted({"status": 2, "statuscode": 0, "pickcode": "pcNEW"}))

    c = make(tmp_path, {
        ("GET", "webapi.115.com/files/uploadinfo"): lambda p_, d, h_: ok(user_id=314159, userkey="UKEY"),
        ("POST", "uplb.115.com/4.0/initupload.php"): init,
        ("GET", "webapi.115.com/files"): lambda p_, d, h_: ok(cid="100", count=1, data=[
            file_item("901", "big.bin", h.sha1, size=h.size)]),
    })
    res = c.try_instant(str(p), "100", "big.bin", h)
    assert res is not None and res.instant and res.fid == "901"
    first, second = forms
    assert first["fileid"] == h.sha1 and first["target"] == "U_1_100" and first["filesize"] == str(h.size)
    assert first["userid"] == "314159" and first["userkey"] == "UKEY" and first["filename"] == "big.bin"
    expected_sig = hashlib.sha1(b"UKEY" + hashlib.sha1(f"314159{h.sha1}U_1_1000".encode()).hexdigest().encode()
                                + b"000000").hexdigest().upper()
    assert first["sig"] == expected_sig and "sign_val" not in first
    assert second["sign_key"] == "SK" and second["sign_val"] == sha1_range(str(p), 10, 19)


def test_upload_falls_back_to_oss(tmp_path, monkeypatch):
    p = tmp_path / "new.bin"
    p.write_bytes(b"hello world")
    h = hash_file(str(p))
    captured = {}

    def fake_oss(local_path, size, token_provider, bucket, obj, callback, callback_var, part_size, control,
                 upload_id, on_upload_id, proxies):
        captured.update(bucket=bucket, obj=obj, callback=callback, callback_var=callback_var,
                        token=token_provider())
        return {"state": True, "data": {"file_id": "F9", "pick_code": "pc9"}}

    from nas115sync.cloud import oss_upload
    monkeypatch.setattr(oss_upload, "upload_to_oss", fake_oss)
    cb = {"callback": '{"callbackUrl":"https://uplb.115.com/3.0/callback.php"}', "callback_var": '{"x:a":"1"}'}
    c = make(tmp_path, {
        ("GET", "webapi.115.com/files/uploadinfo"): lambda p_, d, h_: ok(userkey="UKEY"),
        ("POST", "uplb.115.com/4.0/initupload.php"): lambda p_, d, h_: Resp(content=encrypted(
            {"status": 1, "statuscode": 0, "pickcode": "pc9", "bucket": "fhnfile", "object": "obj/key",
             "callback": cb})),
        ("GET", "uplb.115.com/3.0/gettoken.php"): lambda p_, d, h_: Resp(
            {"StatusCode": "200", "AccessKeyId": "AK", "AccessKeySecret": "SEC", "SecurityToken": "TOK",
             "Expiration": "2099-01-01T00:00:00Z"}),
    })
    res = c.upload(str(p), "100", "new.bin", h)
    assert res.fid == "F9" and not res.instant
    assert captured["bucket"] == "fhnfile" and captured["obj"] == "obj/key"
    assert captured["callback"] == cb["callback"] and captured["callback_var"] == cb["callback_var"]
    assert captured["token"]["endpoint"] == web115.OSS_ENDPOINT and captured["token"]["AccessKeyId"] == "AK"


# ------------------------------------------------------------------ 下載
def test_download_url_decodes_m115(tmp_path, monkeypatch):
    sent = {}

    def downurl(params, data, headers):
        sent["plain"] = data["data"]
        return ok(data="ENCRYPTED")

    monkeypatch.setattr(codec, "m115_decode", lambda text: json.dumps(
        {"301": {"file_name": "a.txt", "url": {"url": "https://cdn.115cdn.net/a.txt?t=1"}}}).encode())
    c = make(tmp_path, {("POST", "proapi.115.com/app/chrome/downurl"): downurl})
    assert c.download_url("pc301") == "https://cdn.115cdn.net/a.txt?t=1"
    assert sent["plain"] == codec.m115_encode_json({"pickcode": "pc301"})


def test_download_falls_back_to_web_and_resumes(tmp_path):
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"hello ")
    got = {}

    def cdn(params, data, headers):
        got.update(headers)
        return Resp(status=206, content=b"", chunks=[b"world"])

    c = make(tmp_path, {
        ("POST", "proapi.115.com/app/chrome/downurl"): lambda p, d, h: Resp(
            {"state": False, "errno": 50028, "error": "暂不支持"}),
        ("POST", "webapi.115.com/files/download"): lambda p, d, h: ok(file_url="https://cdn.115cdn.net/x"),
        ("GET", "cdn.115cdn.net/x"): cdn,
    })
    c.download("301", "pc301", str(dest))
    assert dest.read_bytes() == b"hello world"
    assert got["Range"] == "bytes=6-" and got["User-Agent"] == web115.USER_AGENT


# ------------------------------------------------------------------ 115 端變動
def test_changes_returns_only_new_content_events(tmp_path):
    events = [{"id": 10, "type": 2, "file_id": "301", "parent_id": "100", "file_name": "a.txt"}]

    def detail(params, data, headers):
        return ok(data={"count": len(events), "list": list(reversed(events))})

    c = make(tmp_path, {("GET", "webapi.115.com/behavior/detail"): detail})
    changes, cursor = c.changes(None)
    assert changes == [] and cursor == "10"  # 第一次只記下位置
    events += [{"id": 11, "type": 8, "file_id": "301"},  # 瀏覽影片：忽略
               {"id": 12, "type": 24, "file_id": "302", "parent_id": "100", "file_name": "改名.txt"},
               {"id": 13, "type": 22, "file_id": "303", "parent_id": "100"}]
    changes, cursor = c.changes(cursor)
    assert [(x.fid, x.kind) for x in changes] == [("303", "22"), ("302", "24")] and cursor == "13"
    assert c.changes(cursor) == ([], "13")
